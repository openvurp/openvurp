# skills/voice.py
# openvurp Voice - Sintesi vocale

import subprocess
from pathlib import Path

# Config
VOICE_NAME = "Microsoft Elsa Desktop"
VOICE_LANG = "it-IT"

def speak(text: str, voice: str = None) -> bool:
    """Parla il testo usando la voce selezionata."""
    voice_name = voice or VOICE_NAME
    
    # Escape per PowerShell
    text_escaped = text.replace("'", "''").replace('"', '`"')
    
    ps_script = f"""
Add-Type -AssemblyName System.Speech
$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speak.SelectVoice('{voice_name}')
$speak.Speak('{text_escaped}')
"""
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_script],
            capture_output=True,
            timeout=30
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Errore voce: {e}")
        return False

def list_voices() -> list:
    """Lista voci disponibili."""
    ps_script = """
Add-Type -AssemblyName System.Speech
$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speak.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }
"""
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_script],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return [v.strip() for v in result.stdout.strip().split('\n') if v.strip()]
        return []
    except:
        return []

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        text = ' '.join(sys.argv[1:])
        print(f"Dico: {text}")
        success = speak(text)
        print("OK" if success else "Errore")
    else:
        # Test
        print("Voci disponibili:")
        for v in list_voices():
            print(f"  - {v}")
        print(f"\nVoce selezionata: {VOICE_NAME}")
        print("\nTest...")
        speak("Ciao Mario, sono openvurp. La mia voce è Elsa.")
        print("Fatto.")