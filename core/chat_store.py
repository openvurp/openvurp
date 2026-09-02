"""Archivio SQLite per chat web, messaggi e agenti multiplayer persistenti."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
import uuid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# La rubrica nasce vuota: gli agenti li crea l'utente, uno alla volta.
# Riempirla di default significa decidere al posto suo chi gli serve.
DEFAULT_AGENTS: tuple = ()


class ChatStore:
    """Connessioni brevi + WAL: sicuro con ThreadingHTTPServer."""

    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.root_dir = os.path.join(memory_dir, "chats")
        self.path = os.path.join(self.root_dir, "chats.db")
        self._lock = threading.RLock()
        self._local = threading.local()
        os.makedirs(self.root_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """One connection per thread, reused.

        Opening a new one per query costs little on a local disk and a great
        deal on a network mount (e.g. /mnt/c under WSL): a single roster read
        opened six. The `with conn:` pattern opens a transaction, it does not
        close the connection, so reusing it is safe.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'solo' CHECK(mode IN ('solo','team')),
                    backend TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author_type TEXT NOT NULL DEFAULT 'user',
                    author_id TEXT NOT NULL DEFAULT '',
                    author_name TEXT NOT NULL DEFAULT '',
                    recipient_id TEXT NOT NULL DEFAULT 'room',
                    run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_messages_chat_time
                    ON messages(chat_id, created_at);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    backend TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_agents (
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL DEFAULT 0,
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "backend" not in columns:
                conn.execute("ALTER TABLE chats ADD COLUMN backend TEXT NOT NULL DEFAULT ''")
            if "model" not in columns:
                conn.execute("ALTER TABLE chats ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            if "last_read_at" not in columns:
                # Quando hai guardato l'ultima volta questa conversazione.
                # Lets us say "unread" without keeping per-device state.
                conn.execute(
                    "ALTER TABLE chats ADD COLUMN last_read_at TEXT NOT NULL DEFAULT ''"
                )
            if "direct_agent_id" not in columns:
                # One-to-one chat with a single agent. A column rather than a
                # new `mode` value: the CHECK on mode cannot be altered in
                # SQLite without rebuilding the table, and existing data is
                # worth more than a tidier enum.
                conn.execute(
                    "ALTER TABLE chats ADD COLUMN direct_agent_id TEXT NOT NULL DEFAULT ''"
                )
            now = _now()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            seeded = conn.execute(
                "SELECT value FROM meta WHERE key='agents_seeded'"
            ).fetchone()
            if seeded is None:
                # Only on first start. With INSERT OR IGNORE on every launch, an
                # a deleted default agent came back on the next start:
                # l'utente lo elimina, torna, e se lo ritrova li'.
                for agent_id, name, role, instructions in DEFAULT_AGENTS:
                    conn.execute(
                        """INSERT OR IGNORE INTO agents
                           (id,name,role,instructions,created_at,updated_at)
                           VALUES (?,?,?,?,?,?)""",
                        (agent_id, name, role, instructions, now, now),
                    )
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('agents_seeded',?)", (now,)
                )
            self._migrate_legacy_dashboard(conn, now)

    def _migrate_legacy_dashboard(self, conn: sqlite3.Connection, now: str) -> None:
        """One-time import of the previews from the old single dashboard chat."""
        count = conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()
        if count and int(count["n"]) > 0:
            return
        legacy = os.path.join(
            self.memory_dir, "session_store", "dashboard_sender_dashboard.json"
        )
        try:
            with open(legacy, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
            previews = snapshot.get("recent_messages", [])
            if not isinstance(previews, list) or not previews:
                return
        except (OSError, ValueError, TypeError):
            return

        chat_id = "chat_legacy_dashboard"
        updated = str(snapshot.get("updated_at") or now)
        conn.execute(
            """INSERT OR IGNORE INTO chats(id,title,mode,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (chat_id, "Chat precedente", "solo", updated, updated),
        )
        for index, item in enumerate(previews):
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = str(item.get("preview", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            author_type = "user" if role == "user" else "assistant"
            author_name = "Tu" if role == "user" else "openvurp"
            conn.execute(
                """INSERT OR IGNORE INTO messages
                   (id,chat_id,role,content,author_type,author_id,author_name,
                    recipient_id,created_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (f"msg_legacy_{index:03d}", chat_id, role, content, author_type,
                 "owner" if role == "user" else "main", author_name, "room",
                 updated, json.dumps({"migrated_preview": True})),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def create_chat(self, title: str = "Nuova chat", mode: str = "solo",
                    backend: str = "", model: str = "") -> dict:
        mode = mode if mode in {"solo", "team"} else "solo"
        chat_id, now = _id("chat"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO chats(id,title,mode,backend,model,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (chat_id, (title or "Nuova chat").strip()[:120], mode,
                 backend.strip().lower()[:40], model.strip()[:160], now, now),
            )
            if mode == "team":
                self._join_defaults(conn, chat_id, now)
        return self.get_chat(chat_id) or {}

    def ensure_chat(self, chat_id: str = "", mode: str = "solo") -> dict:
        if chat_id:
            chat = self.get_chat(chat_id)
            if chat:
                return chat
        return self.create_chat(mode=mode)

    def get_chat(self, chat_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT c.*,
                   COALESCE((SELECT SUM(input_tokens) FROM runs r WHERE r.chat_id=c.id),0) AS input_tokens,
                   COALESCE((SELECT SUM(output_tokens) FROM runs r WHERE r.chat_id=c.id),0) AS output_tokens,
                   (SELECT COUNT(*) FROM runs r WHERE r.chat_id=c.id) AS run_count
                   FROM chats c WHERE c.id=?""", (chat_id,),
            ).fetchone()
        return self._row(row)

    def list_chats(self, include_archived: bool = False, limit: int = 100) -> list[dict]:
        where = "" if include_archived else "WHERE archived=0"
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT c.*,
                    (SELECT content FROM messages m WHERE m.chat_id=c.id
                     ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS preview,
                    (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count
                    ,COALESCE((SELECT SUM(input_tokens) FROM runs r WHERE r.chat_id=c.id),0) AS input_tokens
                    ,COALESCE((SELECT SUM(output_tokens) FROM runs r WHERE r.chat_id=c.id),0) AS output_tokens
                    FROM chats c {where} ORDER BY updated_at DESC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_chat(self, chat_id: str, *, title: str | None = None,
                    mode: str | None = None, backend: str | None = None,
                    model: str | None = None,
                    archived: bool | None = None) -> dict | None:
        updates, values = [], []
        if title is not None:
            updates.append("title=?")
            values.append((title.strip() or "Nuova chat")[:120])
        if mode is not None and mode in {"solo", "team"}:
            updates.append("mode=?")
            values.append(mode)
        if backend is not None:
            updates.append("backend=?")
            values.append(backend.strip().lower()[:40])
        if model is not None:
            updates.append("model=?")
            values.append(model.strip()[:160])
        if archived is not None:
            updates.append("archived=?")
            values.append(1 if archived else 0)
        if not updates:
            return self.get_chat(chat_id)
        now = _now()
        updates.append("updated_at=?")
        values.extend([now, chat_id])
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE chats SET {', '.join(updates)} WHERE id=?", values)
            if mode == "team":
                self._join_defaults(conn, chat_id, now)
        return self.get_chat(chat_id)

    def _join_defaults(self, conn: sqlite3.Connection, chat_id: str, now: str) -> None:
        for position, (agent_id, *_rest) in enumerate(DEFAULT_AGENTS):
            conn.execute(
                """INSERT OR IGNORE INTO chat_agents(chat_id,agent_id,position,joined_at)
                   VALUES(?,?,?,?)""",
                (chat_id, agent_id, position, now),
            )

    def add_message(self, chat_id: str, role: str, content: str, *,
                    author_type: str = "user", author_id: str = "",
                    author_name: str = "", recipient_id: str = "room",
                    run_id: str = "", metadata: dict | None = None) -> dict:
        message_id, now = _id("msg"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO messages
                   (id,chat_id,role,content,author_type,author_id,author_name,
                    recipient_id,run_id,created_at,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (message_id, chat_id, role, str(content), author_type, author_id,
                 author_name, recipient_id, run_id, now,
                 json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.execute("UPDATE chats SET updated_at=? WHERE id=?", (now, chat_id))
            # Il primo messaggio utile diventa un titolo leggibile.
            row = conn.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
            if row and row["title"] == "Nuova chat" and author_type == "user":
                title = " ".join(str(content).split())[:64] or "Nuova chat"
                conn.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
        return self.get_message(message_id) or {}

    def get_message(self, message_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._decode_message(row)

    @staticmethod
    def _decode_message(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        item.pop("_rowid", None)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json", "{}"))
        except ValueError:
            item["metadata"] = {}
        return item

    def list_messages(self, chat_id: str, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM (SELECT rowid AS _rowid, * FROM messages WHERE chat_id=?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?)
                   ORDER BY created_at ASC, _rowid ASC""",
                (chat_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [self._decode_message(row) or {} for row in rows]

    def count_agent_messages_since(self, iso_prefix: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM messages
                   WHERE author_type='agent' AND created_at>=?""",
                (str(iso_prefix),),
            ).fetchone()
        return int(row["n"] if row else 0)

    def list_agents(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled=1" if enabled_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM agents {where} ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return self._row(row)

    def create_agent(self, name: str, role: str, instructions: str,
                     backend: str = "", model: str = "") -> dict:
        agent_id, now = _id("agent"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO agents(id,name,role,instructions,backend,model,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (agent_id, name.strip()[:80], role.strip()[:80],
                 instructions.strip()[:4000], backend.strip()[:40], model.strip()[:160],
                 now, now),
            )
        return self.get_agent(agent_id) or {}

    def update_agent(self, agent_id: str, *, name: str | None = None,
                     role: str | None = None, instructions: str | None = None,
                     backend: str | None = None, model: str | None = None,
                     enabled: bool | None = None) -> dict | None:
        updates, values = [], []
        limits = {"name": 80, "role": 80, "instructions": 4000,
                  "backend": 40, "model": 160}
        for column, value in (
            ("name", name), ("role", role), ("instructions", instructions),
            ("backend", backend), ("model", model),
        ):
            if value is not None:
                cleaned = str(value).strip()[:limits[column]]
                if column == "backend":
                    cleaned = cleaned.lower()
                if column == "name" and not cleaned:
                    cleaned = "Agente"
                updates.append(f"{column}=?")
                values.append(cleaned)
        if enabled is not None:
            updates.append("enabled=?")
            values.append(1 if enabled else 0)
        if not updates:
            return self.get_agent(agent_id)
        updates.append("updated_at=?")
        values.extend([_now(), agent_id])
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id=?", values)
        return self.get_agent(agent_id)

    def delete_agent(self, agent_id: str) -> bool:
        """Really delete an agent.

        Its conversation is archived, not destroyed: deleting a correspondent
        must not delete what the two of you said to each other.
        """
        agent = self.get_agent(agent_id)
        if agent is None:
            return False
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE chats SET archived=1 WHERE direct_agent_id=?", (agent_id,)
            )
            conn.execute("DELETE FROM chat_agents WHERE agent_id=?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        return True

    def chat_agents(self, chat_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.*, ca.position, ca.joined_at FROM chat_agents ca
                   JOIN agents a ON a.id=ca.agent_id
                   WHERE ca.chat_id=? AND a.enabled=1 ORDER BY ca.position, a.name""",
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_chat_agents(self, chat_id: str, agent_ids: list[str]) -> list[dict]:
        unique = list(dict.fromkeys(str(item) for item in agent_ids if item))[:12]
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM chat_agents WHERE chat_id=?", (chat_id,))
            for position, agent_id in enumerate(unique):
                conn.execute(
                    """INSERT OR IGNORE INTO chat_agents(chat_id,agent_id,position,joined_at)
                       VALUES(?,?,?,?)""",
                    (chat_id, agent_id, position, now),
                )
        return self.chat_agents(chat_id)

    def direct_chat_for_agent(self, agent_id: str) -> dict | None:
        """The one-to-one conversation with this agent, created if missing.

        This is what makes the roster an address book and not a list: every
        agent has a thread of its own, which stays there while you talk to the
        others.
        """
        agent = self.get_agent(agent_id)
        if agent is None:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM chats WHERE direct_agent_id=? AND archived=0 LIMIT 1",
                (agent_id,),
            ).fetchone()
        if row is not None:
            return self.get_chat(row["id"])

        chat_id, now = _id("chat"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO chats(id,title,mode,backend,model,direct_agent_id,
                                     created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (chat_id, str(agent["name"])[:120], "solo",
                 str(agent.get("backend", "") or ""), str(agent.get("model", "") or ""),
                 agent_id, now, now),
            )
            conn.execute(
                """INSERT OR IGNORE INTO chat_agents(chat_id,agent_id,position,joined_at)
                   VALUES(?,?,0,?)""",
                (chat_id, agent_id, now),
            )
        return self.get_chat(chat_id)

    def mark_read(self, chat_id: str) -> None:
        """Segna la conversazione come letta fino ad adesso."""
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE chats SET last_read_at=? WHERE id=?", (_now(), chat_id))

    TEAM_ROOM_TITLE = "Tutti insieme"

    def team_room(self, create: bool = True) -> dict | None:
        """The room where you talk to every agent at once.

        A chat like the others, but single and recognisable: the roster puts it
        on top, so "writing to everyone" is a place, not a mode hidden behind a
        switch.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM chats WHERE mode='team' AND title=? AND archived=0 LIMIT 1",
                (self.TEAM_ROOM_TITLE,),
            ).fetchone()
        chat_id = row["id"] if row is not None else ""
        if not chat_id:
            if not create:
                return None
            chat_id = self.create_chat(title=self.TEAM_ROOM_TITLE, mode="team")["id"]
        # "All together" has to mean all: agents created after the room join
        # it by themselves. But only when something changed: rewriting the list
        # on every read means a disk write for every roster refresh, and on a
        # slow mount that is seconds.
        wanted = [a["id"] for a in self.list_agents(enabled_only=True)]
        current = [a["id"] for a in self.chat_agents(chat_id)]
        if set(wanted) != set(current):
            self.set_chat_agents(chat_id, wanted)
        return self.get_chat(chat_id)

    def chat_activity(self, chat_id: str) -> dict:
        """Preview, time and unread count of a chat, for list views."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT c.updated_at AS last_at, c.last_read_at,
                       (SELECT m.content FROM messages m WHERE m.chat_id=c.id
                        ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS preview,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id
                        AND m.author_type<>'user' AND m.created_at>c.last_read_at) AS unread
                    FROM chats c WHERE c.id=?""", (chat_id,),
            ).fetchone()
        if row is None:
            return {"preview": "", "last_at": "", "message_count": 0, "unread": 0}
        item = dict(row)
        item["preview"] = " ".join(str(item.get("preview") or "").split())[:160]
        return item

    def main_chat(self) -> dict:
        """The conversation with openvurp itself.

        In the wallet the host agent is a roster entry too: without a row of
        its own, the only way to talk to it would be "none of the others",
        which is not a place.
        """
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id FROM chats
                   WHERE archived=0 AND direct_agent_id='' AND mode='solo'
                   ORDER BY updated_at DESC LIMIT 1""",
            ).fetchone()
        if row is not None:
            return self.get_chat(row["id"]) or {}
        return self.create_chat(title="openvurp", mode="solo")

    def agent_roster(self, enabled_only: bool = True) -> list[dict]:
        """The agents with their latest message: the address-book view.

        Ordered by recent activity, like a list of conversations — the agents
        you spoke to lately are on top, the never-used ones at the bottom but
        still visible.
        """
        where = "WHERE a.enabled=1" if enabled_only else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT a.*,
                       c.id AS chat_id,
                       COALESCE(c.updated_at, '') AS last_at,
                       (SELECT m.content FROM messages m WHERE m.chat_id=c.id
                        ORDER BY m.created_at DESC, m.rowid DESC LIMIT 1) AS preview,
                       (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS message_count,
                       COALESCE((SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id
                        AND m.author_type<>'user'
                        AND m.created_at > COALESCE(c.last_read_at,'')),0) AS unread
                    FROM agents a
                    LEFT JOIN chats c
                       ON c.direct_agent_id = a.id AND c.archived = 0
                    {where}
                    ORDER BY last_at DESC, a.name COLLATE NOCASE""",
            ).fetchall()
        roster = []
        for row in rows:
            item = dict(row)
            item["preview"] = " ".join(str(item.get("preview") or "").split())[:160]
            roster.append(item)
        return roster

    def start_run(self, chat_id: str) -> str:
        run_id, now = _id("run"), _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO runs(id,chat_id,status,started_at) VALUES(?,?,?,?)",
                (run_id, chat_id, "running", now),
            )
        return run_id

    def finish_run(self, run_id: str, *, input_tokens: int = 0,
                   output_tokens: int = 0, error: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE runs SET status=?,finished_at=?,input_tokens=?,output_tokens=?,error=?
                   WHERE id=?""",
                ("failed" if error else "completed", _now(), input_tokens,
                 output_tokens, error[:1000], run_id),
            )
