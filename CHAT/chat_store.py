"""SQLite-backed chat identities, messages, moderation, and push subscriptions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sqlite3
import string
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
URL_RE = re.compile(r"https?://", re.IGNORECASE)
PUBLIC_ID_ALPHABET = string.ascii_uppercase + string.digits
ROOMS = {"free", "admin"}


class ChatError(Exception):
    """Safe error that can be shown to a chat user."""


class ChatPermissionError(ChatError):
    pass


class ChatRateLimitError(ChatError):
    pass


class ChatStore:
    def __init__(self, db_path: str, secret: bytes | str):
        self.db_path = os.path.abspath(db_path)
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("chat identity secret is required")
        self._secret = bytes(secret)
        os.makedirs(os.path.dirname(self.db_path), mode=0o700, exist_ok=True)
        try:
            os.chmod(os.path.dirname(self.db_path), 0o700)
        except OSError:
            pass
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

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS anonymous_identities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_hash TEXT NOT NULL UNIQUE,
                    public_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room TEXT NOT NULL CHECK (room IN ('free', 'admin')),
                    author_key TEXT NOT NULL,
                    author_ip_hash TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    author_avatar TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    deleted_at REAL,
                    deleted_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_room_id ON messages(room, id);
                CREATE INDEX IF NOT EXISTS idx_messages_author_time
                    ON messages(author_key, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_ip_time
                    ON messages(author_ip_hash, created_at);
                CREATE TABLE IF NOT EXISTS deletion_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    room TEXT NOT NULL,
                    original_author_key TEXT NOT NULL,
                    deleted_by TEXT NOT NULL,
                    deleted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity_key TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_bans (
                    identity_key TEXT PRIMARY KEY,
                    reason TEXT,
                    expires_at REAL,
                    created_at REAL NOT NULL,
                    target_id TEXT,
                    target_name TEXT,
                    created_by TEXT
                );
                CREATE TABLE IF NOT EXISTS admin_fanout_claims (
                    message_id INTEGER PRIMARY KEY,
                    claimed_at REAL NOT NULL,
                    completed_at REAL,
                    discord_sent INTEGER,
                    push_requested INTEGER,
                    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
                );
            """)
            self._ensure_column(conn, "chat_bans", "target_id", "TEXT")
            self._ensure_column(conn, "chat_bans", "target_name", "TEXT")
            self._ensure_column(conn, "chat_bans", "created_by", "TEXT")
            conn.execute("DROP TABLE IF EXISTS message_reports")
            conn.execute(
                "INSERT OR IGNORE INTO chat_settings(key,value) VALUES('message_day',?)",
                (datetime.now(JST).date().isoformat(),),
            )
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _today() -> str:
        return datetime.now(JST).date().isoformat()

    def _daily_reset_locked(self, conn: sqlite3.Connection) -> None:
        today = self._today()
        row = conn.execute(
            "SELECT value FROM chat_settings WHERE key='message_day'"
        ).fetchone()
        previous = str(row[0]) if row else ""
        if previous == today:
            return
        conn.execute("DELETE FROM admin_fanout_claims")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM deletion_audit")
        conn.execute(
            "INSERT INTO chat_settings(key,value) VALUES('message_day',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (today,),
        )

    @staticmethod
    def _normalise_ip(raw_ip: str) -> str:
        try:
            return ipaddress.ip_address(str(raw_ip or "").strip()).compressed
        except ValueError as exc:
            raise ChatError("接続元を確認できませんでした。") from exc

    @staticmethod
    def _normalise_fingerprint(raw: str) -> str:
        value = str(raw or "").strip().lower()
        if not FINGERPRINT_RE.fullmatch(value):
            raise ChatError("ブラウザ情報を確認できませんでした。再読み込みしてください。")
        return value

    def _anonymous_hash(self, raw_ip: str, fingerprint: str) -> str:
        value = f"{self._normalise_ip(raw_ip)}|{self._normalise_fingerprint(fingerprint)}"
        return hmac.new(self._secret, f"chat:{value}".encode(), hashlib.sha256).hexdigest()

    def _ip_hash(self, raw_ip: str) -> str:
        value = self._normalise_ip(raw_ip)
        return hmac.new(self._secret, f"chat-ip:{value}".encode(), hashlib.sha256).hexdigest()

    def dm_token_for_key(self, actor_key: str) -> str:
        digest = hmac.new(
            self._secret, f"chat-dm:{actor_key}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _restriction_token(self, actor_key: str) -> str:
        digest = hmac.new(
            self._secret, f"chat-restriction:{actor_key}".encode(), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _unlock_code(self, actor_key: str, created_at: float) -> str:
        digest = hmac.new(
            self._secret,
            f"chat-unlock:{actor_key}:{float(created_at):.6f}".encode(),
            hashlib.sha256,
        ).hexdigest().upper()[:16]
        return "-".join(digest[index:index + 4] for index in range(0, 16, 4))

    @staticmethod
    def _avatar_url(user: dict) -> str | None:
        user_id = str(user.get("id") or "")
        avatar = str(user.get("avatar") or "")
        if not user_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9_]+", avatar):
            return None
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"

    def resolve_actor(
        self,
        raw_ip: str,
        fingerprint: str,
        discord_user: dict | None,
        admin_ids: set[str],
    ) -> dict:
        ip_hash = self._ip_hash(raw_ip)
        if discord_user and str(discord_user.get("id") or "").isdigit():
            user_id = str(discord_user["id"])
            return {
                "key": f"discord:{user_id}",
                "id": user_id,
                "name": str(discord_user.get("username") or user_id)[:80],
                "avatar": self._avatar_url(discord_user),
                "is_admin": user_id in admin_ids,
                "discord": True,
                "ip_hash": ip_hash,
            }

        identity_hash = self._anonymous_hash(raw_ip, fingerprint)
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT public_id FROM anonymous_identities WHERE identity_hash=?",
                (identity_hash,),
            ).fetchone()
            if row:
                public_id = str(row[0])
                conn.execute(
                    "UPDATE anonymous_identities SET last_seen_at=? WHERE identity_hash=?",
                    (now, identity_hash),
                )
            else:
                public_id = ""
                for _ in range(20):
                    candidate = "Guest-" + "".join(
                        secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(8)
                    )
                    try:
                        conn.execute(
                            "INSERT INTO anonymous_identities(identity_hash,public_id,created_at,last_seen_at) "
                            "VALUES(?,?,?,?)",
                            (identity_hash, candidate, now, now),
                        )
                        public_id = candidate
                        break
                    except sqlite3.IntegrityError:
                        continue
                if not public_id:
                    raise ChatError("チャットIDを発行できませんでした。")
        return {
            "key": f"anonymous:{identity_hash}",
            "id": public_id,
            "name": public_id,
            "avatar": None,
            "is_admin": False,
            "discord": False,
            "ip_hash": ip_hash,
        }

    @staticmethod
    def _clean_content(raw: object) -> str:
        if not isinstance(raw, str):
            raise ChatError("メッセージが不正です。")
        value = unicodedata.normalize("NFKC", raw).replace("\r\n", "\n").replace("\r", "\n")
        value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32).strip()
        if not value:
            raise ChatError("メッセージを入力してください。")
        if len(value) > 300:
            raise ChatError("メッセージは300文字以内にしてください。")
        if len(URL_RE.findall(value)) > 2:
            raise ChatError("URLは1投稿につき2件までです。")
        return value

    @staticmethod
    def _assert_room(room: str) -> str:
        value = str(room or "")
        if value not in ROOMS:
            raise ChatError("チャットルームが不正です。")
        return value

    @staticmethod
    def _active_restriction_locked(
        conn: sqlite3.Connection, actor_key: str, now: float
    ) -> sqlite3.Row | None:
        row = conn.execute(
            "SELECT * FROM chat_bans WHERE identity_key=?", (actor_key,)
        ).fetchone()
        if not row:
            return None
        expires = row["expires_at"]
        if expires is not None and float(expires) <= now:
            conn.execute("DELETE FROM chat_bans WHERE identity_key=?", (actor_key,))
            return None
        return row

    @classmethod
    def _assert_not_banned(
        cls, conn: sqlite3.Connection, actor_key: str, now: float
    ) -> None:
        row = cls._active_restriction_locked(conn, actor_key, now)
        if not row:
            return
        if row["expires_at"] is None:
            raise ChatPermissionError("このIDはBANされています。")
        remaining = max(1, int(float(row["expires_at"]) - now))
        minutes = (remaining + 59) // 60
        raise ChatPermissionError(f"このIDはタイムアウト中です（残り約{minutes}分）。")

    def ensure_actor_can_post(self, actor: dict) -> None:
        with self._transaction() as conn:
            self._assert_not_banned(conn, actor["key"], time.time())

    def restriction_for_actor(self, actor: dict) -> dict | None:
        now = time.time()
        with self._transaction() as conn:
            row = self._active_restriction_locked(conn, actor["key"], now)
            if not row:
                return None
            expires_at = row["expires_at"]
            result = {
                "type": "ban" if expires_at is None else "timeout",
                "reason": str(row["reason"] or "管理者による制限"),
                "expires_at": float(expires_at) if expires_at is not None else None,
            }
            if expires_at is None:
                result["unlock_code"] = self._unlock_code(
                    actor["key"], float(row["created_at"])
                )
            return result

    def create_message(self, room: str, actor: dict, content: object) -> dict:
        room = self._assert_room(room)
        clean = self._clean_content(content)
        if room == "admin" and not actor["is_admin"]:
            raise ChatPermissionError("管理者チャットへ投稿できません。")
        now = time.time()
        with self._transaction() as conn:
            self._assert_not_banned(conn, actor["key"], now)
            latest = conn.execute(
                "SELECT created_at,content FROM messages WHERE author_key=? "
                "ORDER BY id DESC LIMIT 1",
                (actor["key"],),
            ).fetchone()
            if latest and now - float(latest["created_at"]) < 3:
                raise ChatRateLimitError("投稿間隔を3秒以上あけてください。")
            minute_count = int(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE author_key=? AND created_at>=?",
                (actor["key"], now - 60),
            ).fetchone()[0])
            if minute_count >= 12:
                raise ChatRateLimitError("1分間の投稿上限に達しました。")
            ip_minute_count = int(conn.execute(
                "SELECT COUNT(*) FROM messages WHERE author_ip_hash=? AND created_at>=?",
                (actor["ip_hash"], now - 60),
            ).fetchone()[0])
            if ip_minute_count >= 30:
                raise ChatRateLimitError("この回線からの投稿上限に達しました。")
            if latest and str(latest["content"]) == clean:
                raise ChatRateLimitError("同じメッセージを連続投稿できません。")
            cur = conn.execute(
                "INSERT INTO messages(room,author_key,author_ip_hash,author_id,author_name,author_avatar,"
                "is_admin,content,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    room, actor["key"], actor["ip_hash"], actor["id"], actor["name"], actor["avatar"],
                    int(bool(actor["is_admin"])), clean, now,
                ),
            )
            message_id = int(cur.lastrowid)
        return {
            "id": message_id,
            "room": room,
            "author_id": actor["id"],
            "author_name": actor["name"],
            "author_avatar": actor["avatar"],
            "is_admin": bool(actor["is_admin"]),
            "content": clean,
            "created_at": now,
            "deleted": False,
            "can_delete": True,
        }

    def list_messages(self, room: str, actor: dict, after_id: int = 0) -> list[dict]:
        room = self._assert_room(room)
        after_id = max(0, int(after_id or 0))
        with self._transaction() as conn:
            if after_id:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE room=? AND id>? ORDER BY id ASC LIMIT 100",
                    (room, after_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE room=? "
                    "ORDER BY id DESC LIMIT 100) ORDER BY id ASC",
                    (room,),
                ).fetchall()
            banned_rows = conn.execute(
                "SELECT identity_key,expires_at FROM chat_bans"
            ).fetchall() if actor["is_admin"] else []
            now = time.time()
            restrictions = {
                str(r["identity_key"]): (
                    "ban" if r["expires_at"] is None else "timeout",
                    float(r["expires_at"]) if r["expires_at"] is not None else None,
                ) for r in banned_rows
                if r["expires_at"] is None or float(r["expires_at"]) > now
            }
        messages = []
        for row in rows:
            deleted = row["deleted_at"] is not None
            messages.append({
                "id": int(row["id"]),
                "room": str(row["room"]),
                "author_id": str(row["author_id"]),
                "author_name": str(row["author_name"]),
                "author_avatar": row["author_avatar"],
                "is_admin": bool(row["is_admin"]),
                "content": "" if deleted else str(row["content"]),
                "created_at": float(row["created_at"]),
                "deleted": deleted,
                "can_delete": bool(
                    not deleted
                    and (actor["is_admin"] or str(row["author_key"]) == actor["key"])
                ),
                "can_dm": bool(not deleted and str(row["author_key"]) != actor["key"]),
                "dm_token": self.dm_token_for_key(str(row["author_key"])),
                "can_moderate": bool(
                    actor["is_admin"]
                    and not row["is_admin"]
                    and str(row["author_key"]) != actor["key"]
                ),
                "restriction_type": restrictions.get(str(row["author_key"]), (None, None))[0],
                "timeout_until": restrictions.get(str(row["author_key"]), (None, None))[1],
            })
        return messages

    def delete_message(self, message_id: int, actor: dict) -> dict:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id,room,author_key,deleted_at FROM messages WHERE id=?",
                (int(message_id),),
            ).fetchone()
            if not row:
                raise ChatError("メッセージが存在しません。")
            if row["deleted_at"] is not None:
                return {"id": int(row["id"]), "deleted": True}
            if not actor["is_admin"] and str(row["author_key"]) != actor["key"]:
                raise ChatPermissionError("このメッセージは削除できません。")
            conn.execute(
                "UPDATE messages SET content='',deleted_at=?,deleted_by=? WHERE id=?",
                (now, actor["key"], int(message_id)),
            )
            conn.execute(
                "INSERT INTO deletion_audit(message_id,room,original_author_key,deleted_by,deleted_at) "
                "VALUES(?,?,?,?,?)",
                (int(row["id"]), str(row["room"]), str(row["author_key"]), actor["key"], now),
            )
        return {"id": int(message_id), "deleted": True}

    def set_author_restriction(
        self,
        message_id: int,
        actor: dict,
        action: str,
        duration_seconds: int | None = None,
    ) -> dict:
        if not actor["is_admin"]:
            raise ChatPermissionError("管理者のみ操作できます。")
        action = str(action or "")
        allowed_durations = {600, 3600, 86400, 604800}
        if action not in {"ban", "timeout", "unban"}:
            raise ChatError("制限操作が不正です。")
        if action == "timeout":
            try:
                duration_seconds = int(duration_seconds or 0)
            except (TypeError, ValueError) as exc:
                raise ChatError("タイムアウト時間が不正です。") from exc
            if duration_seconds not in allowed_durations:
                raise ChatError("タイムアウト時間が不正です。")
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT author_key,author_id,author_name,is_admin FROM messages WHERE id=?",
                (int(message_id),),
            ).fetchone()
            if not row:
                raise ChatError("対象メッセージが存在しません。")
            target_key = str(row["author_key"])
            if bool(row["is_admin"]) or target_key == actor["key"]:
                raise ChatPermissionError("管理者はBANできません。")
            if action == "unban":
                conn.execute("DELETE FROM chat_bans WHERE identity_key=?", (target_key,))
                return {"id": int(message_id), "restriction": None}
            expires_at = None if action == "ban" else now + int(duration_seconds or 0)
            reason = "管理者によるBAN" if action == "ban" else "管理者によるタイムアウト"
            conn.execute(
                "INSERT INTO chat_bans(identity_key,reason,expires_at,created_at,target_id,target_name,created_by) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET "
                "reason=excluded.reason,expires_at=excluded.expires_at,created_at=excluded.created_at,"
                "target_id=excluded.target_id,target_name=excluded.target_name,created_by=excluded.created_by",
                (
                    target_key,
                    reason,
                    expires_at,
                    now,
                    str(row["author_id"]),
                    str(row["author_name"]),
                    actor["key"],
                ),
            )
        return {
            "id": int(message_id),
            "restriction": action,
            "expires_at": expires_at,
        }

    def list_restrictions(self, actor: dict) -> list[dict]:
        if not actor["is_admin"]:
            raise ChatPermissionError("管理者のみ操作できます。")
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM chat_bans WHERE expires_at IS NOT NULL AND expires_at<=?", (now,)
            )
            rows = conn.execute(
                "SELECT identity_key,target_id,target_name,reason,expires_at,created_at "
                "FROM chat_bans ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "target_id": str(row["target_id"] or "不明"),
                "target_name": str(row["target_name"] or row["target_id"] or "不明"),
                "type": "ban" if row["expires_at"] is None else "timeout",
                "expires_at": float(row["expires_at"]) if row["expires_at"] is not None else None,
                "created_at": float(row["created_at"]),
                "token": self._restriction_token(str(row["identity_key"])),
            }
            for row in rows
        ]

    def unban_by_code(self, actor: dict, raw_code: object) -> dict:
        if not actor["is_admin"]:
            raise ChatPermissionError("管理者のみ操作できます。")
        code = re.sub(r"[^A-Fa-f0-9]", "", str(raw_code or "")).upper()
        if len(code) != 16:
            raise ChatError("BAN解除コードが不正です。")
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT identity_key,target_id,target_name,created_at FROM chat_bans "
                "WHERE expires_at IS NULL"
            ).fetchall()
            for row in rows:
                expected = self._unlock_code(
                    str(row["identity_key"]), float(row["created_at"])
                ).replace("-", "")
                if hmac.compare_digest(expected, code):
                    conn.execute(
                        "DELETE FROM chat_bans WHERE identity_key=?",
                        (str(row["identity_key"]),),
                    )
                    return {
                        "unbanned": True,
                        "target_id": str(row["target_id"] or "不明"),
                        "target_name": str(row["target_name"] or row["target_id"] or "不明"),
                    }
        raise ChatError("該当するBAN解除コードはありません。")

    def clear_restriction(self, actor: dict, raw_token: object) -> dict:
        if not actor["is_admin"]:
            raise ChatPermissionError("管理者のみ操作できます。")
        token = str(raw_token or "").strip()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT identity_key,target_id,target_name FROM chat_bans"
            ).fetchall()
            for row in rows:
                if hmac.compare_digest(
                    self._restriction_token(str(row["identity_key"])), token
                ):
                    conn.execute(
                        "DELETE FROM chat_bans WHERE identity_key=?",
                        (str(row["identity_key"]),),
                    )
                    return {
                        "cleared": True,
                        "target_id": str(row["target_id"] or "不明"),
                        "target_name": str(row["target_name"] or row["target_id"] or "不明"),
                    }
        raise ChatError("対象の制限が存在しません。")

    def set_author_ban(self, message_id: int, actor: dict, banned: bool) -> dict:
        """Compatibility wrapper for older callers."""
        result = self.set_author_restriction(
            message_id, actor, "ban" if banned else "unban"
        )
        result["banned"] = bool(banned)
        return result

    def save_subscription(self, actor: dict, raw: object) -> None:
        if not isinstance(raw, dict):
            raise ChatError("通知登録が不正です。")
        endpoint = str(raw.get("endpoint") or "").strip()
        keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if not endpoint.startswith("https://") or len(endpoint) > 2048:
            raise ChatError("通知先が不正です。")
        if not (20 <= len(p256dh) <= 512 and 8 <= len(auth) <= 256):
            raise ChatError("通知キーが不正です。")
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO push_subscriptions(identity_key,endpoint,p256dh,auth,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET "
                "identity_key=excluded.identity_key,p256dh=excluded.p256dh,"
                "auth=excluded.auth,updated_at=excluded.updated_at",
                (actor["key"], endpoint, p256dh, auth, now, now),
            )

    def remove_subscription(self, endpoint: str) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (str(endpoint),))

    def subscriptions(self) -> list[dict]:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT endpoint,p256dh,auth FROM push_subscriptions"
            ).fetchall()
        return [
            {"endpoint": str(r["endpoint"]), "keys": {"p256dh": str(r["p256dh"]), "auth": str(r["auth"])}}
            for r in rows
        ]

    def claim_admin_fanout(self, message_id: int) -> bool:
        """Atomically allow only one process to fan out an administrator post."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT room,deleted_at FROM messages WHERE id=?", (int(message_id),)
            ).fetchone()
            if not row or str(row["room"]) != "admin" or row["deleted_at"] is not None:
                return False
            try:
                conn.execute(
                    "INSERT INTO admin_fanout_claims(message_id,claimed_at) VALUES(?,?)",
                    (int(message_id), time.time()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def complete_admin_fanout(
        self, message_id: int, discord_sent: bool, push_requested: bool
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE admin_fanout_claims SET completed_at=?,discord_sent=?,push_requested=? "
                "WHERE message_id=?",
                (
                    time.time(),
                    int(bool(discord_sent)),
                    int(bool(push_requested)),
                    int(message_id),
                ),
            )
