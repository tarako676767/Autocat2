"""Private direct-message storage for AUTOCAT JP chat."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from CHAT.chat_store import ChatError, ChatPermissionError, ChatRateLimitError, ChatStore


JST = ZoneInfo("Asia/Tokyo")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class DMStore:
    def __init__(self, db_path: str, secret: bytes | str):
        self.db_path = os.path.abspath(db_path)
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("DM secret is required")
        self._secret = bytes(secret)
        self._initialise()

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _connect(self) -> sqlite3.Connection:
        return self._configure(
            sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        )

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._daily_reset_locked(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def token_for_key(self, actor_key: str) -> str:
        digest = hmac.new(
            self._secret, f"chat-dm:{actor_key}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dm_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dm_contacts (
                    identity_key TEXT PRIMARY KEY,
                    dm_token TEXT NOT NULL UNIQUE,
                    public_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    avatar TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dm_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_key TEXT NOT NULL,
                    recipient_key TEXT NOT NULL,
                    sender_ip_hash TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    deleted_at REAL,
                    deleted_by TEXT,
                    FOREIGN KEY(sender_key) REFERENCES dm_contacts(identity_key),
                    FOREIGN KEY(recipient_key) REFERENCES dm_contacts(identity_key)
                );
                CREATE INDEX IF NOT EXISTS idx_dm_participants_id
                    ON dm_messages(sender_key,recipient_key,id);
                CREATE INDEX IF NOT EXISTS idx_dm_sender_time
                    ON dm_messages(sender_key,created_at);
                CREATE INDEX IF NOT EXISTS idx_dm_ip_time
                    ON dm_messages(sender_ip_hash,created_at);
            """)
            conn.execute(
                "INSERT OR IGNORE INTO dm_settings(key,value) VALUES('message_day',?)",
                (datetime.now(JST).date().isoformat(),),
            )
            old_contacts = conn.execute(
                "SELECT DISTINCT author_key,author_id,author_name,author_avatar,is_admin "
                "FROM messages"
            ).fetchall()
            now = time.time()
            for row in old_contacts:
                actor_key = str(row["author_key"])
                conn.execute(
                    "INSERT OR IGNORE INTO dm_contacts(identity_key,dm_token,public_id,display_name,avatar,is_admin,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        actor_key,
                        self.token_for_key(actor_key),
                        str(row["author_id"]),
                        str(row["author_name"]),
                        row["author_avatar"],
                        int(bool(row["is_admin"])),
                        now,
                    ),
                )

    @staticmethod
    def _today() -> str:
        return datetime.now(JST).date().isoformat()

    def _daily_reset_locked(self, conn: sqlite3.Connection) -> None:
        today = self._today()
        row = conn.execute(
            "SELECT value FROM dm_settings WHERE key='message_day'"
        ).fetchone()
        if row and str(row[0]) == today:
            return
        conn.execute("DELETE FROM dm_messages")
        conn.execute(
            "INSERT INTO dm_settings(key,value) VALUES('message_day',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (today,),
        )

    def register_actor(self, actor: dict) -> None:
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO dm_contacts(identity_key,dm_token,public_id,display_name,avatar,is_admin,last_seen_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET "
                "dm_token=excluded.dm_token,public_id=excluded.public_id,display_name=excluded.display_name,"
                "avatar=excluded.avatar,is_admin=excluded.is_admin,last_seen_at=excluded.last_seen_at",
                (
                    actor["key"],
                    self.token_for_key(actor["key"]),
                    actor["id"],
                    actor["name"],
                    actor.get("avatar"),
                    int(bool(actor.get("is_admin"))),
                    now,
                ),
            )

    @staticmethod
    def _resolve_target_locked(conn: sqlite3.Connection, raw_token: object) -> sqlite3.Row:
        token = str(raw_token or "").strip()
        if not TOKEN_RE.fullmatch(token):
            raise ChatError("DM対象が不正です。")
        row = conn.execute(
            "SELECT * FROM dm_contacts WHERE dm_token=?", (token,)
        ).fetchone()
        if not row:
            raise ChatError("DM対象が存在しません。")
        return row

    @staticmethod
    def _contact(row: sqlite3.Row) -> dict:
        return {
            "id": str(row["public_id"]),
            "name": str(row["display_name"]),
            "avatar": row["avatar"],
            "is_admin": bool(row["is_admin"]),
            "dm_token": str(row["dm_token"]),
        }

    @staticmethod
    def _message(row: sqlite3.Row, actor: dict) -> dict:
        deleted = row["deleted_at"] is not None
        return {
            "id": int(row["id"]),
            "room": "dm",
            "author_id": str(row["sender_id"]),
            "author_name": str(row["sender_name"]),
            "author_avatar": row["sender_avatar"],
            "is_admin": bool(row["sender_admin"]),
            "content": "" if deleted else str(row["content"]),
            "created_at": float(row["created_at"]),
            "deleted": deleted,
            "can_delete": bool(
                not deleted
                and (
                    str(row["sender_key"]) == actor["key"]
                    or bool(actor.get("is_admin"))
                )
            ),
            "can_dm": False,
            "can_moderate": False,
        }

    def list_state(self, actor: dict, raw_target_token: object = "") -> dict:
        token = str(raw_target_token or "").strip()
        with self._transaction() as conn:
            if token:
                target = self._resolve_target_locked(conn, token)
                target_key = str(target["identity_key"])
                if target_key == actor["key"]:
                    raise ChatError("自分自身にDMは送れません。")
                rows = conn.execute(
                    "SELECT d.*,c.public_id AS sender_id,c.display_name AS sender_name,"
                    "c.avatar AS sender_avatar,c.is_admin AS sender_admin "
                    "FROM dm_messages d JOIN dm_contacts c ON c.identity_key=d.sender_key "
                    "WHERE (d.sender_key=? AND d.recipient_key=?) "
                    "OR (d.sender_key=? AND d.recipient_key=?) "
                    "ORDER BY d.id DESC LIMIT 100",
                    (actor["key"], target_key, target_key, actor["key"]),
                ).fetchall()
                return {
                    "target": self._contact(target),
                    "messages": [self._message(row, actor) for row in reversed(rows)],
                    "conversations": [],
                }

            rows = conn.execute(
                "SELECT * FROM dm_messages WHERE sender_key=? OR recipient_key=? "
                "ORDER BY id DESC LIMIT 1000",
                (actor["key"], actor["key"]),
            ).fetchall()
            seen: set[str] = set()
            conversations = []
            for row in rows:
                other_key = str(
                    row["recipient_key"]
                    if str(row["sender_key"]) == actor["key"]
                    else row["sender_key"]
                )
                if other_key in seen:
                    continue
                contact = conn.execute(
                    "SELECT * FROM dm_contacts WHERE identity_key=?", (other_key,)
                ).fetchone()
                if not contact:
                    continue
                seen.add(other_key)
                item = self._contact(contact)
                item.update({
                    "last_content": "削除されたメッセージ"
                    if row["deleted_at"] is not None
                    else str(row["content"])[:80],
                    "last_created_at": float(row["created_at"]),
                })
                conversations.append(item)
            return {"target": None, "messages": [], "conversations": conversations}

    def send_message(self, actor: dict, raw_target_token: object, raw_content: object) -> dict:
        clean = ChatStore._clean_content(raw_content)
        now = time.time()
        with self._transaction() as conn:
            target = self._resolve_target_locked(conn, raw_target_token)
            target_key = str(target["identity_key"])
            if target_key == actor["key"]:
                raise ChatError("自分自身にDMは送れません。")
            latest = conn.execute(
                "SELECT created_at,content FROM dm_messages WHERE sender_key=? "
                "ORDER BY id DESC LIMIT 1",
                (actor["key"],),
            ).fetchone()
            if latest and now - float(latest["created_at"]) < 3:
                raise ChatRateLimitError("投稿間隔を3秒以上あけてください。")
            if latest and str(latest["content"]) == clean:
                raise ChatRateLimitError("同じメッセージを連続送信できません。")
            actor_count = int(conn.execute(
                "SELECT COUNT(*) FROM dm_messages WHERE sender_key=? AND created_at>=?",
                (actor["key"], now - 60),
            ).fetchone()[0])
            if actor_count >= 12:
                raise ChatRateLimitError("1分間のDM送信上限に達しました。")
            ip_count = int(conn.execute(
                "SELECT COUNT(*) FROM dm_messages WHERE sender_ip_hash=? AND created_at>=?",
                (actor["ip_hash"], now - 60),
            ).fetchone()[0])
            if ip_count >= 30:
                raise ChatRateLimitError("この回線からのDM送信上限に達しました。")
            cur = conn.execute(
                "INSERT INTO dm_messages(sender_key,recipient_key,sender_ip_hash,content,created_at) "
                "VALUES(?,?,?,?,?)",
                (actor["key"], target_key, actor["ip_hash"], clean, now),
            )
            message_id = int(cur.lastrowid)
        return {
            "id": message_id,
            "room": "dm",
            "author_id": actor["id"],
            "author_name": actor["name"],
            "author_avatar": actor.get("avatar"),
            "is_admin": bool(actor.get("is_admin")),
            "content": clean,
            "created_at": now,
            "deleted": False,
            "can_delete": True,
            "can_dm": False,
            "can_moderate": False,
        }

    def delete_message(self, message_id: int, actor: dict) -> dict:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT sender_key,recipient_key,deleted_at FROM dm_messages WHERE id=?",
                (int(message_id),),
            ).fetchone()
            if not row:
                raise ChatError("DMが存在しません。")
            participants = {str(row["sender_key"]), str(row["recipient_key"])}
            if actor["key"] not in participants:
                raise ChatPermissionError("このDMは操作できません。")
            if row["deleted_at"] is not None:
                return {"id": int(message_id), "deleted": True}
            if str(row["sender_key"]) != actor["key"] and not actor["is_admin"]:
                raise ChatPermissionError("他の利用者のDMは削除できません。")
            conn.execute(
                "UPDATE dm_messages SET content='',deleted_at=?,deleted_by=? WHERE id=?",
                (now, actor["key"], int(message_id)),
            )
        return {"id": int(message_id), "deleted": True}
