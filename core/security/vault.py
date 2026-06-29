"""
openvurp Security — Vault

Gestione sicura di secrets e credenziali.
Cripta tutto a riposo con Fernet (AES-128-CBC + HMAC-SHA256).
Mai chiavi in chiaro su disco.
"""

from __future__ import annotations

import os
import json
import base64
import hashlib
import getpass
from typing import Optional
from pathlib import Path


# Fernet è opzionale — se non presente, usa fallback XOR (meno sicuro ma funzionale)
try:
    from cryptography.fernet import Fernet
    HAS_FERNET = True
except ImportError:
    HAS_FERNET = False


class Vault:
    """
    Vault per secrets. Cripta chiavi API, token, password.

    Uso:
        vault = Vault("/path/to/vault")
        vault.unlock("master_password")  # o genera chiave automatica
        vault.set("TELEGRAM_TOKEN", "123456:ABC")
        token = vault.get("TELEGRAM_TOKEN")
    """

    VAULT_FILE = "secrets.vault"
    KEY_FILE = ".vault_key"  # Chiave derivata, non il master password

    def __init__(self, vault_dir: str):
        self.vault_dir = vault_dir
        self._vault_path = os.path.join(vault_dir, self.VAULT_FILE)
        self._key_path = os.path.join(vault_dir, self.KEY_FILE)
        self._fernet = None
        self._secrets: dict[str, str] = {}
        self._unlocked = False

        os.makedirs(vault_dir, exist_ok=True)

    @property
    def is_unlocked(self) -> bool:
        return self._unlocked

    @property
    def is_initialized(self) -> bool:
        return os.path.exists(self._vault_path)

    def init(self, master_password: str = None) -> str:
        """
        Inizializza il vault. Se nessuna password, genera chiave automatica.
        Returns: messaggio di stato.
        """
        if self.is_initialized:
            return "Vault già inizializzato."

        if master_password:
            key = self._derive_key(master_password)
        else:
            key = Fernet.generate_key() if HAS_FERNET else self._generate_simple_key()

        # Salva chiave con permessi restrittivi
        self._write_key_file(key)

        # Crea vault vuoto
        self._fernet = self._create_cipher(key)
        self._secrets = {}
        self._save()
        self._unlocked = True

        return "Vault inizializzato."

    def unlock(self, master_password: str = None) -> bool:
        """
        Sblocca il vault.
        Se nessuna password e esiste key file, usa quello.
        """
        if not self.is_initialized:
            # Auto-init
            self.init(master_password)
            return True

        # Prova key file
        if not master_password and os.path.exists(self._key_path):
            try:
                key = self._read_key_file()
                self._fernet = self._create_cipher(key)
                self._load()
                self._unlocked = True
                return True
            except Exception:
                pass

        # Prova con password
        if master_password:
            try:
                key = self._derive_key(master_password)
                self._fernet = self._create_cipher(key)
                self._load()
                self._unlocked = True
                # Aggiorna key file
                self._write_key_file(key)
                return True
            except Exception:
                return False

        return False

    def auto_unlock(self) -> bool:
        """Tenta unlock automatico con key file. Silenzioso."""
        if self._unlocked:
            return True
        if not self.is_initialized:
            self.init()
            return True
        return self.unlock()

    def get(self, key: str, default: str = "") -> str:
        """Ottieni un secret."""
        if not self._unlocked:
            self.auto_unlock()
        return self._secrets.get(key, default)

    def set(self, key: str, value: str):
        """Imposta un secret."""
        if not self._unlocked:
            self.auto_unlock()
        self._secrets[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        """Rimuovi un secret."""
        if not self._unlocked:
            self.auto_unlock()
        if key in self._secrets:
            del self._secrets[key]
            self._save()
            return True
        return False

    def list_keys(self) -> list[str]:
        """Lista chiavi (senza valori)."""
        if not self._unlocked:
            self.auto_unlock()
        return list(self._secrets.keys())

    def has(self, key: str) -> bool:
        """Controlla se un secret esiste."""
        if not self._unlocked:
            self.auto_unlock()
        return key in self._secrets

    def get_or_env(self, key: str, env_var: str = None) -> str:
        """
        Ottieni un secret dal vault, fallback su variabile d'ambiente.
        Se trovato in env e non in vault, salva nel vault.
        """
        # Prima dal vault
        val = self.get(key)
        if val:
            return val

        # Poi da env
        env_name = env_var or key
        val = os.environ.get(env_name, "")
        if val:
            # Salva nel vault per prossima volta
            self.set(key, val)
            return val

        return ""

    def export_env(self, keys: list[str] = None) -> dict[str, str]:
        """Esporta secrets come dict per subprocess env (temporaneo)."""
        if not self._unlocked:
            self.auto_unlock()
        if keys:
            return {k: v for k, v in self._secrets.items() if k in keys}
        return dict(self._secrets)

    # ── Internals ──

    def _derive_key(self, password: str) -> bytes:
        """Deriva chiave da password con PBKDF2."""
        salt = b"openvurp_vault_salt_v1"  # Salt fisso per semplicità
        if HAS_FERNET:
            import hashlib
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
            return base64.urlsafe_b64encode(dk)
        else:
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
            return dk

    def _generate_simple_key(self) -> bytes:
        """Genera chiave random per fallback senza cryptography."""
        return os.urandom(32)

    def _create_cipher(self, key: bytes):
        """Crea cipher object."""
        if HAS_FERNET:
            # Assicura che la chiave sia valida per Fernet
            if len(key) != 44:  # Fernet key è 44 bytes base64
                key = base64.urlsafe_b64encode(key[:32])
            return Fernet(key)
        return _SimpleCipher(key)

    def _encrypt(self, data: str) -> bytes:
        """Cripta stringa."""
        if HAS_FERNET:
            return self._fernet.encrypt(data.encode())
        return self._fernet.encrypt(data)

    def _decrypt(self, data: bytes) -> str:
        """Decripta stringa."""
        if HAS_FERNET:
            return self._fernet.decrypt(data).decode()
        return self._fernet.decrypt(data)

    def _save(self):
        """Salva vault criptato su disco."""
        if not self._fernet:
            return
        plaintext = json.dumps(self._secrets, ensure_ascii=False)
        encrypted = self._encrypt(plaintext)
        with open(self._vault_path, "wb") as f:
            f.write(encrypted)
        # Permessi restrittivi
        try:
            os.chmod(self._vault_path, 0o600)
        except OSError:
            pass

    def _load(self):
        """Carica e decripta vault da disco."""
        if not os.path.exists(self._vault_path):
            self._secrets = {}
            return
        with open(self._vault_path, "rb") as f:
            encrypted = f.read()
        if not encrypted:
            self._secrets = {}
            return
        plaintext = self._decrypt(encrypted)
        self._secrets = json.loads(plaintext)

    def _write_key_file(self, key: bytes):
        """Salva chiave con permessi 0600."""
        with open(self._key_path, "wb") as f:
            f.write(key)
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            pass

    def _read_key_file(self) -> bytes:
        """Leggi chiave da file."""
        with open(self._key_path, "rb") as f:
            return f.read()


class _SimpleCipher:
    """
    Cipher fallback se cryptography non è installato.
    Usa XOR con chiave — NON sicuro quanto Fernet, ma meglio di plaintext.
    """

    def __init__(self, key: bytes):
        self._key = key if isinstance(key, bytes) else key.encode()

    def encrypt(self, data: str) -> bytes:
        data_bytes = data.encode() if isinstance(data, str) else data
        encrypted = bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(data_bytes))
        return base64.b64encode(encrypted)

    def decrypt(self, data: bytes) -> str:
        decoded = base64.b64decode(data)
        decrypted = bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(decoded))
        return decrypted.decode()
