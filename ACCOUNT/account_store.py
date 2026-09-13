"""Persistent site-account, VIP contract, and purchase-DM storage."""

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
from typing import Callable

from Paython import (
    PaythonAmountMismatchError,
    PaythonLinkLookupError,
    PaythonLinkNotPendingError,
    validate_payment_link,
)

FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_ID_RE = re.compile(r"^[A-Z0-9]{16}$")
PAYPAY_LINK_RE = re.compile(r"https://pay\.paypay\.ne\.jp/[A-Za-z0-9]{16}(?=$|[\s<>\"'。、,.)）\]】])")
ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
OPEN_TICKET_STATUSES = {"pending", "awaiting_payment", "payment_review"}
TICKET_STATUSES = OPEN_TICKET_STATUSES | {"completed", "cancelled"}
TICKET_STATUS_LABELS = {
    "pending": "申請受付",
    "awaiting_payment": "支払い待ち",
    "payment_review": "支払い確認中",
    "completed": "VIP付与完了",
    "cancelled": "キャンセル",
}
DEFAULT_VIP_PLAN_PRICES = {30: 300, 60: 600, 90: 900}


class AccountError(Exception):
    """A safe validation error that may be shown to the user."""


class AccountPermissionError(AccountError):
    pass


class AccountRateLimitError(AccountError):
    pass


class AccountExternalServiceError(AccountError):
    pass


class AccountStore:
    def __init__(
        self,
        db_path: str,
        secret: bytes | str,
        vip_plan_prices: dict[int, int] | None = None,
        paypay_link_validator: Callable[[str, int], object] | None = None,
    ):
        self.db_path = os.path.abspath(db_path)
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("account identity secret is required")
        self._secret = bytes(secret)
        supplied_prices = vip_plan_prices or DEFAULT_VIP_PLAN_PRICES
        try:
            self._vip_plan_prices = {
                days: int(supplied_prices[days]) for days in (30, 60, 90)
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("VIP plan prices must define 30, 60, and 90 days") from exc
        if any(price < 1 or price > 1_000_000 for price in self._vip_plan_prices.values()):
            raise ValueError("VIP plan prices are out of range")
        self._paypay_link_validator = paypay_link_validator or validate_payment_link
        os.makedirs(os.path.dirname(self.db_path), mode=0o700, exist_ok=True)
        self._initialise()
        try:
            os.chmod(os.path.dirname(self.db_path), 0o700)
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _connect(self) -> sqlite3.Connection:
        return self._configure(sqlite3.connect(self.db_path, timeout=10, isolation_level=None))

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    fingerprint_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    vip_active INTEGER NOT NULL DEFAULT 0 CHECK(vip_active IN (0,1)),
                    vip_started_at REAL,
                    vip_expires_at REAL,
                    created_at REAL NOT NULL,
                    last_login_at REAL NOT NULL,
                    deleted_at REAL,
                    suspended_at REAL,
                    banned_at REAL,
                    gban_active INTEGER NOT NULL DEFAULT 0 CHECK(gban_active IN (0,1)),
                    free_identity_account_id INTEGER,
                    trial_vip_claimed_at REAL,
                    trial_source_redemption_id INTEGER,
                    trial_vip_uses_remaining INTEGER NOT NULL DEFAULT 0 CHECK(trial_vip_uses_remaining BETWEEN 0 AND 1)
                );
                CREATE TABLE IF NOT EXISTS purchase_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    purchase_key TEXT NOT NULL UNIQUE,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL,
                    requested_days INTEGER NOT NULL DEFAULT 30,
                    price_yen INTEGER NOT NULL DEFAULT 300,
                    chat_closed_at REAL,
                    chat_closed_by TEXT,
                    payment_reported_at REAL,
                    payment_link TEXT,
                    payment_link_sent_at REAL,
                    deleted_at REAL,
                    deleted_by TEXT,
                    last_user_seen_id INTEGER NOT NULL DEFAULT 0,
                    last_admin_seen_id INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_purchase_tickets_account
                    ON purchase_tickets(account_id,updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_purchase_tickets_status
                    ON purchase_tickets(status,updated_at DESC);
                CREATE TABLE IF NOT EXISTS purchase_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_role TEXT NOT NULL,
                    sender_label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES purchase_tickets(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_purchase_messages_ticket
                    ON purchase_messages(ticket_id,id);
                CREATE TABLE IF NOT EXISTS vip_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    admin_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    duration_days INTEGER,
                    previous_expires_at REAL,
                    new_expires_at REAL,
                    purchase_key TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_vip_audit_account
                    ON vip_audit_logs(account_id,id DESC);
                CREATE TABLE IF NOT EXISTS registration_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip_hash TEXT NOT NULL,
                    account_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_registration_events_ip_time
                    ON registration_events(ip_hash,created_at);
                CREATE TABLE IF NOT EXISTS trial_vip_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('reserved','consumed','refunded')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_trial_vip_reservations_account
                    ON trial_vip_reservations(account_id,status);
                CREATE TABLE IF NOT EXISTS account_admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    admin_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS account_push_subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_account_push_owner
                    ON account_push_subscriptions(owner_key);
                CREATE TABLE IF NOT EXISTS global_bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    created_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    revoked_by TEXT,
                    revoked_at REAL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_global_bans_account
                    ON global_bans(account_id,active);
                CREATE TABLE IF NOT EXISTS global_ban_identifiers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gban_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('ip','fingerprint')),
                    key_hash TEXT NOT NULL,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    UNIQUE(gban_id,kind,key_hash),
                    FOREIGN KEY(gban_id) REFERENCES global_bans(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_global_ban_identifiers_lookup
                    ON global_ban_identifiers(kind,key_hash,gban_id);
            """)
            self._migrate_legacy_unique_ip(conn)
            self._migrate_accounts_v19(conn)
            self._migrate_purchase_tickets_v12(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_ip_hash ON accounts(ip_hash)")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_free_identity "
                "ON accounts(free_identity_account_id) WHERE free_identity_account_id IS NOT NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_purchase_tickets_deleted ON purchase_tickets(deleted_at,updated_at DESC)")

    @staticmethod
    def _migrate_accounts_v19(conn: sqlite3.Connection) -> None:
        """Add moderation state and a one-use invitation VIP trial."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(accounts)")}
        had_trial_use_column = "trial_vip_uses_remaining" in columns
        additions = {
            "deleted_at": "REAL",
            "suspended_at": "REAL",
            "banned_at": "REAL",
            "gban_active": "INTEGER NOT NULL DEFAULT 0",
            "free_identity_account_id": "INTEGER",
            "trial_vip_claimed_at": "REAL",
            "trial_source_redemption_id": "INTEGER",
            "trial_vip_uses_remaining": "INTEGER NOT NULL DEFAULT 0 CHECK(trial_vip_uses_remaining BETWEEN 0 AND 1)",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")
        conn.execute(
            "UPDATE accounts SET suspended_at=COALESCE(suspended_at,last_login_at) "
            "WHERE status='suspended' AND deleted_at IS NULL"
        )
        if not had_trial_use_column:
            # v18の未期限切れ1時間体験だけを、未使用の1回権利へ安全に引き継ぐ。
            # 有料VIP履歴がないアカウントでは、旧時間制VIPフラグも取り除く。
            now = time.time()
            conn.execute(
                "UPDATE accounts SET trial_vip_uses_remaining=1,"
                "vip_active=CASE WHEN EXISTS(SELECT 1 FROM vip_audit_logs l WHERE l.account_id=accounts.id AND l.action='grant') THEN vip_active ELSE 0 END,"
                "vip_expires_at=CASE WHEN EXISTS(SELECT 1 FROM vip_audit_logs l WHERE l.account_id=accounts.id AND l.action='grant') THEN vip_expires_at ELSE NULL END "
                "WHERE trial_vip_claimed_at IS NOT NULL AND (vip_expires_at IS NULL OR vip_expires_at>?)",
                (now,),
            )

    @staticmethod
    def _migrate_purchase_tickets_v12(conn: sqlite3.Connection) -> None:
        """Add structured plan, chat, payment, and archived-ticket fields."""
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(purchase_tickets)")}
        additions = {
            "requested_days": "INTEGER NOT NULL DEFAULT 30",
            "price_yen": "INTEGER NOT NULL DEFAULT 300",
            "chat_closed_at": "REAL",
            "chat_closed_by": "TEXT",
            "payment_reported_at": "REAL",
            "payment_link": "TEXT",
            "payment_link_sent_at": "REAL",
            "deleted_at": "REAL",
            "deleted_by": "TEXT",
        }
        for name, definition in additions.items():
            if name in columns:
                continue
            try:
                conn.execute(f"ALTER TABLE purchase_tickets ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                # Multiple production workers can initialise the same DB at once.
                if "duplicate column name" not in str(exc).lower():
                    raise

    def vip_plans(self) -> list[dict]:
        return [
            {
                "days": days,
                "price_yen": self._vip_plan_prices[days],
                "label": f"{days}日",
                "recommended": days == 90,
            }
            for days in (30, 60, 90)
        ]

    def _plan(self, duration_days: object) -> tuple[int, int]:
        try:
            days = int(duration_days)
        except (TypeError, ValueError) as exc:
            raise AccountError("VIP期間が不正です。") from exc
        if days not in self._vip_plan_prices:
            raise AccountError("VIP期間は30・60・90日から選択してください。")
        return days, self._vip_plan_prices[days]

    def plan_quote(self, duration_days: object) -> dict:
        days, price_yen = self._plan(duration_days)
        return {
            "days": days,
            "price_yen": price_yen,
            "label": f"{days}日",
            "recommended": days == 90,
        }

    @staticmethod
    def _migrate_legacy_unique_ip(conn: sqlite3.Connection) -> None:
        """v7のIP UNIQUE制約を、同一IP最大3件の通常インデックスへ安全に移行する。"""
        unique_ip = False
        for index in conn.execute("PRAGMA index_list(accounts)").fetchall():
            if not bool(index[2]):
                continue
            columns = [str(row[2]) for row in conn.execute(f"PRAGMA index_info('{index[1]}')")]
            if columns == ["ip_hash"]:
                unique_ip = True
                break
        if not unique_ip:
            return

        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE accounts_ip_limit_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    ip_hash TEXT NOT NULL,
                    fingerprint_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'active',
                    vip_active INTEGER NOT NULL DEFAULT 0 CHECK(vip_active IN (0,1)),
                    vip_started_at REAL,
                    vip_expires_at REAL,
                    created_at REAL NOT NULL,
                    last_login_at REAL NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO accounts_ip_limit_v2(
                    id,public_id,username,username_key,password_hash,ip_hash,fingerprint_hash,
                    status,vip_active,vip_started_at,vip_expires_at,created_at,last_login_at
                )
                SELECT id,public_id,username,username_key,password_hash,ip_hash,fingerprint_hash,
                    status,vip_active,vip_started_at,vip_expires_at,created_at,last_login_at
                FROM accounts
            """)
            conn.execute("DROP TABLE accounts")
            conn.execute("ALTER TABLE accounts_ip_limit_v2 RENAME TO accounts")
            conn.execute("CREATE INDEX idx_accounts_ip_hash ON accounts(ip_hash)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("account.db foreign key migration failed")

    @staticmethod
    def _normalise_ip(raw_ip: str) -> str:
        try:
            return ipaddress.ip_address(str(raw_ip or "").strip()).compressed
        except ValueError as exc:
            raise AccountError("接続元を確認できませんでした。") from exc

    @staticmethod
    def _normalise_fingerprint(raw: object) -> str:
        value = str(raw or "").strip().lower()
        if not FINGERPRINT_RE.fullmatch(value):
            raise AccountError("ブラウザ情報を確認できませんでした。再読み込みしてください。")
        return value

    def _identity_hash(self, label: str, value: str) -> str:
        return hmac.new(self._secret, f"account:{label}:{value}".encode(), hashlib.sha256).hexdigest()

    def identity_hashes(self, raw_ip: str, fingerprint: object) -> tuple[str, str]:
        ip_value = self._normalise_ip(raw_ip)
        fp_value = self._normalise_fingerprint(fingerprint)
        return self._identity_hash("ip", ip_value), self._identity_hash("fingerprint", fp_value)

    def identity_ip_hash(self, raw_ip: str) -> str:
        return self._identity_hash("ip", self._normalise_ip(raw_ip))

    def identity_fingerprint_hash(self, fingerprint: object) -> str:
        return self._identity_hash("fingerprint", self._normalise_fingerprint(fingerprint))

    @staticmethod
    def _clean_username(raw: object) -> tuple[str, str]:
        value = unicodedata.normalize("NFKC", str(raw or "")).strip()
        if not 2 <= len(value) <= 24:
            raise AccountError("ユーザー名は2〜24文字で入力してください。")
        if any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise AccountError("ユーザー名に空白や制御文字は使用できません。")
        if not all(ch.isalnum() or ch in "_.-々ー" for ch in value):
            raise AccountError("ユーザー名に使用できない文字が含まれています。")
        return value, value.casefold()

    @staticmethod
    def _clean_password(raw: object) -> str:
        value = str(raw or "")
        if not 8 <= len(value) <= 128:
            raise AccountError("パスワードは8〜128文字で入力してください。")
        return value

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        encoded_salt = base64.urlsafe_b64encode(salt).rstrip(b"=").decode("ascii")
        encoded_digest = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"scrypt$16384$8$1${encoded_salt}${encoded_digest}"

    @staticmethod
    def _verify_password(stored: str, password: str) -> bool:
        try:
            algorithm, raw_n, raw_r, raw_p, encoded_salt, encoded_digest = str(stored).split("$", 5)
            if algorithm != "scrypt":
                return False
            n, r, p = int(raw_n), int(raw_r), int(raw_p)
            if (n, r, p) != (2**14, 8, 1):
                return False
            pad_salt = "=" * ((4 - len(encoded_salt) % 4) % 4)
            pad_digest = "=" * ((4 - len(encoded_digest) % 4) % 4)
            salt = base64.urlsafe_b64decode(encoded_salt + pad_salt)
            expected = base64.urlsafe_b64decode(encoded_digest + pad_digest)
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _new_identifier(conn: sqlite3.Connection, table: str, column: str) -> str:
        for _ in range(40):
            value = "".join(secrets.choice(ID_ALPHABET) for _ in range(16))
            if not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone():
                return value
        raise AccountError("IDを発行できませんでした。もう一度お試しください。")

    @staticmethod
    def _vip_state(row: sqlite3.Row, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else float(now)
        if not bool(row["vip_active"]):
            return False, "free"
        expires = row["vip_expires_at"]
        if expires is None:
            return True, "permanent"
        if float(expires) > now:
            return True, "active"
        return False, "expired"

    @classmethod
    def _account_public(cls, row: sqlite3.Row) -> dict:
        paid_vip_active, vip_state = cls._vip_state(row)
        row_keys = set(row.keys())
        deleted_at = row["deleted_at"] if "deleted_at" in row_keys else None
        suspended_at = row["suspended_at"] if "suspended_at" in row_keys else None
        banned_at = row["banned_at"] if "banned_at" in row_keys else None
        gban_active = bool(row["gban_active"]) if "gban_active" in row_keys else False
        trial_claimed_at = row["trial_vip_claimed_at"] if "trial_vip_claimed_at" in row_keys else None
        trial_uses = int(row["trial_vip_uses_remaining"] or 0) if "trial_vip_uses_remaining" in row_keys else 0
        created_at = float(row["created_at"])
        trial_offer_expires_at = created_at + 3600
        if deleted_at is not None:
            status = "deleted"
        elif gban_active:
            status = "gban"
        elif banned_at is not None:
            status = "banned"
        elif suspended_at is not None:
            status = "suspended"
        else:
            status = str(row["status"])
        usable = status == "active" and deleted_at is None
        trial_vip_active = usable and trial_uses > 0
        is_vip = usable and (paid_vip_active or trial_vip_active)
        return {
            "id": int(row["id"]),
            "public_id": str(row["public_id"]),
            "username": str(row["username"]),
            "status": status,
            "deleted": deleted_at is not None,
            "deleted_at": float(deleted_at) if deleted_at is not None else None,
            "suspended_at": float(suspended_at) if suspended_at is not None else None,
            "banned_at": float(banned_at) if banned_at is not None else None,
            "gban_active": gban_active,
            "trial_vip_claimed_at": float(trial_claimed_at) if trial_claimed_at is not None else None,
            "trial_vip_uses_remaining": trial_uses,
            "is_trial_vip": trial_vip_active,
            "trial_offer_expires_at": trial_offer_expires_at,
            "trial_offer_active": trial_claimed_at is None and time.time() < trial_offer_expires_at,
            "is_paid_vip": usable and paid_vip_active,
            "is_vip": is_vip,
            "vip_state": "deleted" if deleted_at is not None else ("trial" if trial_vip_active and not paid_vip_active else vip_state),
            "vip_started_at": float(row["vip_started_at"]) if row["vip_started_at"] is not None else None,
            "vip_expires_at": float(row["vip_expires_at"]) if row["vip_expires_at"] is not None else None,
            "created_at": created_at,
            "last_login_at": float(row["last_login_at"]),
        }

    def create_account(self, raw_ip: str, fingerprint: object, username: object, password: object) -> dict:
        clean_name, username_key = self._clean_username(username)
        clean_password = self._clean_password(password)
        ip_hash, fp_hash = self.identity_hashes(raw_ip, fingerprint)
        now = time.time()
        with self._transaction() as conn:
            conn.execute("DELETE FROM registration_events WHERE created_at<?", (now - 30 * 86400,))
            recent_ip_registrations = int(conn.execute(
                "SELECT COUNT(*) FROM registration_events WHERE ip_hash=? AND created_at>=?",
                (ip_hash, now - 3600),
            ).fetchone()[0])
            if recent_ip_registrations >= 3:
                raise AccountRateLimitError("この回線では1時間に3アカウントまで作成できます。")
            ip_accounts = int(conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE ip_hash=? AND status!='released'", (ip_hash,)
            ).fetchone()[0])
            if ip_accounts >= 3:
                raise AccountError("この回線で作成できるサイトアカウントは最大3つです。")
            if conn.execute("SELECT 1 FROM accounts WHERE fingerprint_hash=?", (fp_hash,)).fetchone():
                raise AccountError("このブラウザでは既にサイトアカウントが作成されています。")
            if conn.execute("SELECT 1 FROM accounts WHERE username_key=?", (username_key,)).fetchone():
                raise AccountError("そのユーザー名は既に使用されています。")
            public_id = self._new_identifier(conn, "accounts", "public_id")
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(public_id,username,username_key,password_hash,ip_hash,fingerprint_hash,created_at,last_login_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (public_id, clean_name, username_key, self._hash_password(clean_password), ip_hash, fp_hash, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountError("アカウントを作成できませんでした。入力内容をご確認ください。") from exc
            conn.execute(
                "INSERT INTO registration_events(ip_hash,account_id,created_at) VALUES(?,?,?)",
                (ip_hash, int(cur.lastrowid), now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(cur.lastrowid),)).fetchone()
        return self._account_public(row)

    def authenticate(self, username: object, password: object) -> dict:
        _, username_key = self._clean_username(username)
        clean_password = self._clean_password(password)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE username_key=?", (username_key,)).fetchone()
            if not row or not self._verify_password(str(row["password_hash"]), clean_password):
                raise AccountError("ユーザー名またはパスワードが違います。")
            if str(row["status"]) != "active":
                raise AccountPermissionError("このサイトアカウントは利用できません。")
            now = time.time()
            conn.execute("UPDATE accounts SET last_login_at=? WHERE id=?", (now, int(row["id"])))
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(row["id"]),)).fetchone()
        return self._account_public(row)

    def get_account(self, account_id: object) -> dict | None:
        try:
            value = int(account_id)
        except (TypeError, ValueError):
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (value,)).fetchone()
        return self._account_public(row) if row else None

    def bind_free_identity(self, account_id: object, free_identity_account_id: object) -> dict:
        """Bind one site account to one Free identity; bindings can never be moved."""
        try:
            site_id = int(account_id)
            free_id = int(free_identity_account_id)
        except (TypeError, ValueError) as exc:
            raise AccountError("招待用IDを確認できませんでした。") from exc
        if site_id <= 0 or free_id <= 0:
            raise AccountError("招待用IDを確認できませんでした。")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (site_id,)).fetchone()
            if not row:
                raise AccountError("サイトアカウントを確認できませんでした。")
            current = row["free_identity_account_id"]
            if current is None:
                try:
                    conn.execute(
                        "UPDATE accounts SET free_identity_account_id=? WHERE id=? AND free_identity_account_id IS NULL",
                        (free_id, site_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AccountError("この招待用IDは別のサイトアカウントに登録されています。") from exc
            elif int(current) != free_id:
                # A login from another network/browser must not move the trial eligibility.
                return self._account_public(row)
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (site_id,)).fetchone()
        return self._account_public(row)

    def grant_invitation_trial(
        self,
        free_identity_account_id: object,
        redemption_id: object,
        rewarded_at: object,
        now: float | None = None,
    ) -> dict:
        """Grant one successful VIP execution when an invitation completes in the first hour."""
        try:
            free_id = int(free_identity_account_id)
            event_id = int(redemption_id)
        except (TypeError, ValueError) as exc:
            raise AccountError("招待成立情報が不正です。") from exc
        current_time = time.time() if now is None else float(now)
        try:
            event_time = float(rewarded_at)
        except (TypeError, ValueError) as exc:
            raise AccountError("招待成立日時が不正です。") from exc
        with self._transaction() as conn:
            account = conn.execute(
                "SELECT * FROM accounts WHERE free_identity_account_id=?",
                (free_id,),
            ).fetchone()
            if not account:
                return {"status": "no_account", "granted": False}
            account_id = int(account["id"])
            if account["trial_vip_claimed_at"] is not None:
                return {"status": "already_claimed", "granted": False, "account_id": account_id}
            created_at = float(account["created_at"])
            if event_time < created_at or event_time > created_at + 3600:
                return {"status": "expired", "granted": False, "account_id": account_id}
            if account["deleted_at"] is not None or str(account["status"]) == "released":
                return {"status": "unavailable", "granted": False, "account_id": account_id}
            conn.execute(
                "UPDATE accounts SET trial_vip_uses_remaining=1,"
                "trial_vip_claimed_at=?,trial_source_redemption_id=? WHERE id=? AND trial_vip_claimed_at IS NULL",
                (event_time, event_id, account_id),
            )
            conn.execute(
                "INSERT INTO vip_audit_logs(account_id,admin_id,action,duration_days,previous_expires_at,new_expires_at,purchase_key,created_at) "
                "VALUES(?,?,'invitation_trial',NULL,?,?,NULL,?)",
                (account_id, "system:invitation", None, None, current_time),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return {"status": "granted", "granted": True, "account": self._account_public(row)}

    def reserve_vip_job(self, account_id: object, reservation_id: object) -> dict:
        """Reserve paid VIP access or atomically hold the one-use invitation trial."""
        try:
            site_id = int(account_id)
        except (TypeError, ValueError) as exc:
            raise AccountPermissionError("サイトアカウントを確認できませんでした。") from exc
        key = str(reservation_id or "").strip()
        if site_id <= 0 or not key or len(key) > 100:
            raise AccountPermissionError("VIP利用権を確認できませんでした。")
        now = time.time()
        with self._transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE id=?", (site_id,)).fetchone()
            if not account or str(account["status"]) != "active" or account["deleted_at"] is not None:
                raise AccountPermissionError("サイトアカウントを確認できませんでした。")
            paid_active, _ = self._vip_state(account, now)
            if paid_active:
                return {"access": "paid", "reservation_id": None}
            existing = conn.execute(
                "SELECT account_id,status FROM trial_vip_reservations WHERE reservation_id=?",
                (key,),
            ).fetchone()
            if existing:
                if int(existing["account_id"]) == site_id and str(existing["status"]) == "reserved":
                    return {"access": "trial", "reservation_id": key}
                raise AccountPermissionError("このVIP利用予約は使用できません。")
            cur = conn.execute(
                "UPDATE accounts SET trial_vip_uses_remaining=trial_vip_uses_remaining-1 "
                "WHERE id=? AND trial_vip_uses_remaining>0",
                (site_id,),
            )
            if cur.rowcount != 1:
                raise AccountPermissionError("VIP無料体験の残り回数がありません。")
            conn.execute(
                "INSERT INTO trial_vip_reservations(reservation_id,account_id,status,created_at,updated_at) "
                "VALUES(?,?,'reserved',?,?)",
                (key, site_id, now, now),
            )
        return {"access": "trial", "reservation_id": key}

    def settle_vip_job(self, reservation_id: object, success: bool) -> bool:
        """Consume a trial on success; return it exactly once when the job fails."""
        key = str(reservation_id or "").strip()
        if not key:
            return False
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT account_id,status FROM trial_vip_reservations WHERE reservation_id=?",
                (key,),
            ).fetchone()
            if not row or str(row["status"]) != "reserved":
                return False
            target = "consumed" if bool(success) else "refunded"
            cur = conn.execute(
                "UPDATE trial_vip_reservations SET status=?,updated_at=? "
                "WHERE reservation_id=? AND status='reserved'",
                (target, now, key),
            )
            if cur.rowcount != 1:
                return False
            if not success:
                conn.execute(
                    "UPDATE accounts SET trial_vip_uses_remaining=MIN(1,trial_vip_uses_remaining+1) WHERE id=?",
                    (int(row["account_id"]),),
                )
        return True

    @staticmethod
    def _ticket_public(row: sqlite3.Row) -> dict:
        row_keys = set(row.keys())
        return {
            "purchase_key": str(row["purchase_key"]),
            "status": str(row["status"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "closed_at": float(row["closed_at"]) if row["closed_at"] is not None else None,
            "requested_days": int(row["requested_days"]),
            "price_yen": int(row["price_yen"]),
            "chat_closed": row["chat_closed_at"] is not None,
            "chat_closed_at": float(row["chat_closed_at"]) if row["chat_closed_at"] is not None else None,
            "chat_closed_by": str(row["chat_closed_by"] or ""),
            "payment_reported_at": float(row["payment_reported_at"]) if row["payment_reported_at"] is not None else None,
            "payment_link": str(row["payment_link"] or ""),
            "payment_link_sent_at": float(row["payment_link_sent_at"]) if row["payment_link_sent_at"] is not None else None,
            "deleted": row["deleted_at"] is not None,
            "deleted_at": float(row["deleted_at"]) if row["deleted_at"] is not None else None,
            "deleted_by": str(row["deleted_by"] or ""),
            "account_public_id": str(row["account_public_id"]),
            "username": str(row["username"]),
            "is_vip": bool(row["vip_is_active"]),
            "vip_expires_at": float(row["vip_expires_at"]) if row["vip_expires_at"] is not None else None,
            "user_unread": int(row["user_unread"]) if "user_unread" in row_keys else 0,
            "admin_unread": int(row["admin_unread"]) if "admin_unread" in row_keys else 0,
        }

    @staticmethod
    def _ticket_select() -> str:
        return (
            "SELECT t.*,a.public_id AS account_public_id,a.username,a.vip_expires_at,"
            "CASE WHEN a.vip_active=1 AND (a.vip_expires_at IS NULL OR a.vip_expires_at>?) THEN 1 ELSE 0 END AS vip_is_active,"
            "(SELECT COUNT(*) FROM purchase_messages um WHERE um.ticket_id=t.id AND um.id>t.last_user_seen_id AND um.sender_role IN ('admin','system')) AS user_unread,"
            "(SELECT COUNT(*) FROM purchase_messages am WHERE am.ticket_id=t.id AND am.id>t.last_admin_seen_id AND am.sender_role='user') AS admin_unread "
            "FROM purchase_tickets t JOIN accounts a ON a.id=t.account_id "
        )

    def create_purchase_ticket(self, account_id: int, duration_days: object) -> tuple[dict, bool, dict | None]:
        days, price_yen = self._plan(duration_days)
        now = time.time()
        with self._transaction() as conn:
            existing = conn.execute(
                self._ticket_select() + "WHERE t.account_id=? AND t.deleted_at IS NULL AND t.status IN ('pending','awaiting_payment','payment_review') ORDER BY t.id DESC LIMIT 1",
                (now, int(account_id)),
            ).fetchone()
            if existing:
                return self._ticket_public(existing), False, None
            account = conn.execute("SELECT * FROM accounts WHERE id=? AND status='active'", (int(account_id),)).fetchone()
            if not account:
                raise AccountPermissionError("サイトアカウントを確認できませんでした。")
            purchase_key = self._new_identifier(conn, "purchase_tickets", "purchase_key")
            cur = conn.execute(
                "INSERT INTO purchase_tickets(purchase_key,account_id,status,requested_days,price_yen,created_at,updated_at) VALUES(?,?,'pending',?,?,?,?)",
                (purchase_key, int(account_id), days, price_yen, now, now),
            )
            ticket_id = int(cur.lastrowid)
            request_message = (
                "VIPの購入を申請します。\n"
                f"プラン: {days}日\n"
                f"金額: {price_yen:,}円\n"
                f"ユーザー名: {account['username']}\n"
                f"サイトID: {account['public_id']}\n"
                f"購入KEY: {purchase_key}"
            )
            msg = conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (ticket_id, "user", str(account["username"]), request_message, now),
            )
            conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (
                    ticket_id,
                    "system",
                    "AUTOCAT JP",
                    "PayPayの送金リンクを作成してチャット内に送信してお待ちください",
                    now + 0.0001,
                ),
            )
            row = conn.execute(self._ticket_select() + "WHERE t.id=?", (now, ticket_id)).fetchone()
            message = {"id": int(msg.lastrowid), "content": request_message, "created_at": now}
        return self._ticket_public(row), True, message

    def list_user_tickets(self, account_id: int) -> list[dict]:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                self._ticket_select() + "WHERE t.account_id=? AND t.deleted_at IS NULL ORDER BY t.updated_at DESC LIMIT 30",
                (now, int(account_id)),
            ).fetchall()
        return [self._ticket_public(row) for row in rows]

    @staticmethod
    def _message_public(row: sqlite3.Row) -> dict:
        return {
            "id": int(row["id"]),
            "sender_role": str(row["sender_role"]),
            "sender_label": str(row["sender_label"]),
            "content": str(row["content"]),
            "created_at": float(row["created_at"]),
        }

    def ticket_state(self, purchase_key: object, account_id: int | None = None, admin: bool = False) -> dict:
        key = str(purchase_key or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(key):
            raise AccountError("購入KEYが不正です。")
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(self._ticket_select() + "WHERE t.purchase_key=?", (now, key)).fetchone()
            if not row:
                raise AccountError("購入申請が存在しません。")
            if not admin and int(row["account_id"]) != int(account_id or 0):
                raise AccountPermissionError("この購入申請は表示できません。")
            if not admin and row["deleted_at"] is not None:
                raise AccountError("この購入申請は削除されています。")
            messages = conn.execute(
                "SELECT * FROM purchase_messages WHERE ticket_id=? ORDER BY id ASC LIMIT 500",
                (int(row["id"]),),
            ).fetchall()
            last_id = int(messages[-1]["id"]) if messages else 0
            seen_column = "last_admin_seen_id" if admin else "last_user_seen_id"
            conn.execute(f"UPDATE purchase_tickets SET {seen_column}=? WHERE id=?", (last_id, int(row["id"])))
            audit = conn.execute(
                "SELECT action,duration_days,new_expires_at,created_at FROM vip_audit_logs WHERE account_id=? ORDER BY id DESC LIMIT 20",
                (int(row["account_id"]),),
            ).fetchall()
        result = self._ticket_public(row)
        if admin:
            result["admin_unread"] = 0
        else:
            result["user_unread"] = 0
        result["messages"] = [self._message_public(item) for item in messages]
        result["audit"] = [
            {
                "action": str(item["action"]),
                "duration_days": int(item["duration_days"]) if item["duration_days"] is not None else None,
                "new_expires_at": float(item["new_expires_at"]) if item["new_expires_at"] is not None else None,
                "created_at": float(item["created_at"]),
            }
            for item in audit
        ]
        return result

    @staticmethod
    def _clean_message(raw: object) -> str:
        value = unicodedata.normalize("NFKC", str(raw or "")).replace("\r\n", "\n").replace("\r", "\n").strip()
        value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
        if not value:
            raise AccountError("メッセージを入力してください。")
        if len(value) > 500:
            raise AccountError("メッセージは500文字以内にしてください。")
        return value

    def send_purchase_message(
        self,
        purchase_key: object,
        content: object,
        sender_role: str,
        sender_label: str,
        account_id: int | None = None,
    ) -> tuple[dict, int]:
        if sender_role not in {"user", "admin"}:
            raise AccountPermissionError("送信者を確認できませんでした。")
        clean = self._clean_message(content)
        key = str(purchase_key or "").strip().upper()
        payment_match = PAYPAY_LINK_RE.search(clean) if sender_role == "user" else None
        payment_link = (
            self._clean_paypay_link(payment_match.group(0).rstrip("。、,.)）]】"))
            if payment_match else None
        )
        if payment_link:
            with self._connect() as conn:
                current = conn.execute(
                    "SELECT * FROM purchase_tickets WHERE purchase_key=?", (key,)
                ).fetchone()
            if not current:
                raise AccountError("購入申請が存在しません。")
            if int(current["account_id"]) != int(account_id or 0):
                raise AccountPermissionError("この購入申請へ送信できません。")
            if current["deleted_at"] is not None:
                raise AccountError("削除済みの購入チケットには送信できません。")
            if current["chat_closed_at"] is not None:
                raise AccountError("この購入DMは閉じられているため送信できません。")
            if str(current["status"]) in {"completed", "cancelled"}:
                raise AccountError("受け取り済またはキャンセルされています")
            self._verify_paypay_link(payment_link, int(current["price_yen"]))
        now = time.time()
        with self._transaction() as conn:
            ticket = conn.execute("SELECT * FROM purchase_tickets WHERE purchase_key=?", (key,)).fetchone()
            if not ticket:
                raise AccountError("購入申請が存在しません。")
            if sender_role == "user" and int(ticket["account_id"]) != int(account_id or 0):
                raise AccountPermissionError("この購入申請へ送信できません。")
            if ticket["deleted_at"] is not None:
                raise AccountError("削除済みの購入チケットには送信できません。")
            if ticket["chat_closed_at"] is not None:
                raise AccountError("この購入DMは閉じられているため送信できません。")
            if payment_link and str(ticket["status"]) in {"completed", "cancelled"}:
                raise AccountError("受け取り済またはキャンセルされています")
            latest = conn.execute(
                "SELECT content,created_at FROM purchase_messages WHERE ticket_id=? AND sender_role=? ORDER BY id DESC LIMIT 1",
                (int(ticket["id"]), sender_role),
            ).fetchone()
            latest_is_automatic_request = bool(
                latest and str(latest["content"]).startswith("VIPの購入を申請します。\n")
            )
            if latest and not latest_is_automatic_request and now - float(latest["created_at"]) < 2:
                raise AccountRateLimitError("送信間隔を2秒以上あけてください。")
            if latest and not latest_is_automatic_request and str(latest["content"]) == clean:
                raise AccountRateLimitError("同じメッセージを連続送信できません。")
            cur = conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (int(ticket["id"]), sender_role, str(sender_label)[:40], clean, now),
            )
            if payment_link:
                conn.execute(
                    "UPDATE purchase_tickets SET status='payment_review',payment_reported_at=?,payment_link=?,payment_link_sent_at=?,updated_at=?,closed_at=NULL WHERE id=?",
                    (now, payment_link, now, now, int(ticket["id"])),
                )
            else:
                conn.execute("UPDATE purchase_tickets SET updated_at=? WHERE id=?", (now, int(ticket["id"])))
        return {
            "id": int(cur.lastrowid),
            "sender_role": sender_role,
            "sender_label": str(sender_label)[:40],
            "content": clean,
            "created_at": now,
        }, int(ticket["account_id"])

    def close_purchase_ticket(
        self,
        purchase_key: object,
        actor_role: str,
        account_id: int | None = None,
    ) -> dict:
        """Remove a purchase ticket from active views while preserving its log.

        A purchaser may only delete their own ticket. Administrators are
        authorised by the route before this method is called. Deleted tickets
        remain available to administrators as read-only records.
        """
        if actor_role not in {"user", "admin"}:
            raise AccountPermissionError("操作権限を確認できませんでした。")
        key = str(purchase_key or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(key):
            raise AccountError("購入KEYが不正です。")
        with self._transaction() as conn:
            ticket = conn.execute(
                "SELECT id,account_id,status,deleted_at FROM purchase_tickets WHERE purchase_key=?",
                (key,),
            ).fetchone()
            if not ticket:
                return {
                    "purchase_key": key,
                    "status": "deleted",
                    "account_id": 0,
                    "deleted": False,
                    "changed": False,
                }
            if actor_role == "user" and int(ticket["account_id"]) != int(account_id or 0):
                raise AccountPermissionError("この購入チケットは削除できません。")
            if ticket["deleted_at"] is not None:
                return {
                    "purchase_key": key,
                    "status": "deleted",
                    "account_id": int(ticket["account_id"]),
                    "deleted": True,
                    "changed": False,
                }
            now = time.time()
            conn.execute(
                "UPDATE purchase_tickets SET deleted_at=?,deleted_by=?,updated_at=? WHERE id=?",
                (now, actor_role, now, int(ticket["id"])),
            )
        return {
            "purchase_key": key,
            "status": "deleted",
            "account_id": int(ticket["account_id"]),
            "deleted": True,
            "changed": True,
        }

    def report_purchase_payment(self, purchase_key: object, account_id: int) -> dict:
        key = str(purchase_key or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(key):
            raise AccountError("購入KEYが不正です。")
        now = time.time()
        with self._transaction() as conn:
            ticket = conn.execute("SELECT * FROM purchase_tickets WHERE purchase_key=?", (key,)).fetchone()
            if not ticket:
                raise AccountError("購入申請が存在しません。")
            if int(ticket["account_id"]) != int(account_id):
                raise AccountPermissionError("この購入申請は更新できません。")
            if ticket["deleted_at"] is not None:
                raise AccountError("削除済みの購入チケットは更新できません。")
            if ticket["chat_closed_at"] is not None:
                raise AccountError("この購入DMは閉じられています。")
            if str(ticket["status"]) in {"completed", "cancelled"}:
                raise AccountError("この申請は既に処理済みです。")
            if ticket["payment_reported_at"] is not None and str(ticket["status"]) == "payment_review":
                return {
                    "purchase_key": key,
                    "status": "payment_review",
                    "account_id": int(ticket["account_id"]),
                    "changed": False,
                }
            conn.execute(
                "UPDATE purchase_tickets SET status='payment_review',payment_reported_at=?,updated_at=?,closed_at=NULL WHERE id=?",
                (now, now, int(ticket["id"])),
            )
            conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (
                    int(ticket["id"]),
                    "system",
                    "AUTOCAT JP",
                    "利用者から支払い完了の連絡がありました。管理者が確認します。",
                    now,
                ),
            )
        return {
            "purchase_key": key,
            "status": "payment_review",
            "account_id": int(ticket["account_id"]),
            "changed": True,
        }

    @staticmethod
    def _clean_paypay_link(raw: object) -> str:
        value = str(raw or "").strip()
        if not PAYPAY_LINK_RE.fullmatch(value):
            raise AccountError("PayPayリンクは https://pay.paypay.ne.jp/ の後に16文字の英数字が続く形式で入力してください。")
        return value

    def _verify_paypay_link(self, payment_link: str, expected_amount: int) -> object:
        try:
            return self._paypay_link_validator(payment_link, int(expected_amount))
        except PaythonAmountMismatchError as exc:
            raise AccountError("金額不一致エラー") from exc
        except PaythonLinkNotPendingError as exc:
            raise AccountError("受け取り済またはキャンセルされています") from exc
        except PaythonLinkLookupError as exc:
            raise AccountExternalServiceError("送金リンクの状態を確認できませんでした") from exc

    def submit_paypay_link(
        self,
        purchase_key: object,
        payment_link: object,
        account_id: int,
        sender_label: str,
    ) -> dict:
        key = str(purchase_key or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(key):
            raise AccountError("購入KEYが不正です。")
        clean_link = self._clean_paypay_link(payment_link)
        with self._connect() as conn:
            current = conn.execute("SELECT * FROM purchase_tickets WHERE purchase_key=?", (key,)).fetchone()
        if not current:
            raise AccountError("購入申請が存在しません。")
        if int(current["account_id"]) != int(account_id):
            raise AccountPermissionError("この購入申請は更新できません。")
        if current["deleted_at"] is not None:
            raise AccountError("削除済みの購入チケットは更新できません。")
        if current["chat_closed_at"] is not None:
            raise AccountError("この購入DMは閉じられています。")
        if str(current["status"]) in {"completed", "cancelled"}:
            raise AccountError("受け取り済またはキャンセルされています")
        self._verify_paypay_link(clean_link, int(current["price_yen"]))
        now = time.time()
        with self._transaction() as conn:
            ticket = conn.execute("SELECT * FROM purchase_tickets WHERE purchase_key=?", (key,)).fetchone()
            if not ticket:
                raise AccountError("購入申請が存在しません。")
            if int(ticket["account_id"]) != int(account_id):
                raise AccountPermissionError("この購入申請は更新できません。")
            if ticket["deleted_at"] is not None:
                raise AccountError("削除済みの購入チケットは更新できません。")
            if ticket["chat_closed_at"] is not None:
                raise AccountError("この購入DMは閉じられています。")
            if str(ticket["status"]) in {"completed", "cancelled"}:
                raise AccountError("受け取り済またはキャンセルされています")
            if str(ticket["payment_link"] or "") == clean_link:
                return {
                    "purchase_key": key,
                    "status": str(ticket["status"]),
                    "account_id": int(ticket["account_id"]),
                    "changed": False,
                }
            content = f"PayPay送金リンク\n{clean_link}"
            conn.execute(
                "UPDATE purchase_tickets SET status='payment_review',payment_reported_at=?,payment_link=?,payment_link_sent_at=?,updated_at=?,closed_at=NULL WHERE id=?",
                (now, clean_link, now, now, int(ticket["id"])),
            )
            conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (int(ticket["id"]), "user", str(sender_label)[:40], content, now),
            )
            conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (int(ticket["id"]), "system", "AUTOCAT JP", "PayPay送金リンクを受け付けました。管理者の確認をお待ちください。", now + 0.0001),
            )
        return {
            "purchase_key": key,
            "status": "payment_review",
            "account_id": int(ticket["account_id"]),
            "changed": True,
        }

    def admin_overview(self, purchase_key: object = "") -> dict:
        now = time.time()
        with self._connect() as conn:
            tickets = conn.execute(
                self._ticket_select() + "WHERE t.deleted_at IS NULL ORDER BY CASE t.status WHEN 'pending' THEN 0 WHEN 'awaiting_payment' THEN 1 WHEN 'payment_review' THEN 2 ELSE 3 END,t.updated_at DESC LIMIT 300",
                (now,),
            ).fetchall()
            deleted_tickets = conn.execute(
                self._ticket_select() + "WHERE t.deleted_at IS NOT NULL ORDER BY t.deleted_at DESC LIMIT 300",
                (now,),
            ).fetchall()
            accounts = conn.execute(
                "SELECT * FROM accounts WHERE vip_active=1 AND (vip_expires_at IS NULL OR vip_expires_at>?) "
                "ORDER BY CASE WHEN vip_expires_at IS NULL THEN 0 ELSE 1 END,vip_expires_at DESC LIMIT 200",
                (now,),
            ).fetchall()
            total_accounts = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            active_vip = int(conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE vip_active=1 AND (vip_expires_at IS NULL OR vip_expires_at>?)", (now,)
            ).fetchone()[0])
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE deleted_at IS NULL AND status IN ('pending','awaiting_payment','payment_review')"
            ).fetchone()[0])
            unhandled = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE deleted_at IS NULL AND status IN ('pending','awaiting_payment')"
            ).fetchone()[0])
            working = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE deleted_at IS NULL AND status='payment_review'"
            ).fetchone()[0])
            done = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE deleted_at IS NULL AND status IN ('completed','cancelled')"
            ).fetchone()[0])
            deleted = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE deleted_at IS NOT NULL"
            ).fetchone()[0])
            requests_30d = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE created_at>=?", (now - 30 * 86400,)
            ).fetchone()[0])
            completed_30d = int(conn.execute(
                "SELECT COUNT(*) FROM purchase_tickets WHERE status='completed' AND created_at>=?", (now - 30 * 86400,)
            ).fetchone()[0])
        result = {
            "summary": {
                "accounts": total_accounts,
                "active_vip": active_vip,
                "pending": pending,
                "unhandled": unhandled,
                "working": working,
                "done": done,
                "deleted": deleted,
                "requests_30d": requests_30d,
                "grants_30d": completed_30d,
                "conversion_30d": round((completed_30d / requests_30d) * 100, 1) if requests_30d else 0,
            },
            "tickets": [self._ticket_public(row) for row in tickets],
            "deleted_tickets": [self._ticket_public(row) for row in deleted_tickets],
            "subscribers": [self._account_public(row) for row in accounts],
            "plans": self.vip_plans(),
        }
        with self._connect() as conn:
            all_accounts = conn.execute("SELECT * FROM accounts ORDER BY created_at DESC LIMIT 300").fetchall()
        result["accounts"] = [self._account_public(row) for row in all_accounts]
        selected_key = str(purchase_key or "").strip().upper()
        if not selected_key and tickets:
            selected_key = str(tickets[0]["purchase_key"])
        elif not selected_key and deleted_tickets:
            selected_key = str(deleted_tickets[0]["purchase_key"])
        if selected_key:
            result["selected"] = self.ticket_state(selected_key, admin=True)
        return result

    def set_ticket_status(self, purchase_key: object, status: object, admin_id: str) -> dict:
        key = str(purchase_key or "").strip().upper()
        clean_status = str(status or "").strip()
        if clean_status not in TICKET_STATUSES:
            raise AccountError("申請状態が不正です。")
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT id,account_id,status,deleted_at FROM purchase_tickets WHERE purchase_key=?", (key,)).fetchone()
            if not row:
                raise AccountError("購入申請が存在しません。")
            if row["deleted_at"] is not None:
                raise AccountError("削除済みの購入チケットは更新できません。")
            closed_at = now if clean_status in {"completed", "cancelled"} else None
            conn.execute(
                "UPDATE purchase_tickets SET status=?,updated_at=?,closed_at=? WHERE id=?",
                (clean_status, now, closed_at, int(row["id"])),
            )
            conn.execute(
                "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                (
                    int(row["id"]), "system", "AUTOCAT JP",
                    f"申請状態が「{TICKET_STATUS_LABELS[clean_status]}」に更新されました。", now,
                ),
            )
        return {"purchase_key": key, "status": clean_status, "account_id": int(row["account_id"])}

    def grant_vip(self, public_id: object, duration_days: object, admin_id: str, purchase_key: object = "") -> dict:
        account_public_id = str(public_id or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(account_public_id):
            raise AccountError("アカウントIDは16桁の英数字です。")
        days, _ = self._plan(duration_days)
        key = str(purchase_key or "").strip().upper()
        if key and not PUBLIC_ID_RE.fullmatch(key):
            raise AccountError("購入KEYが不正です。")
        now = time.time()
        with self._transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE public_id=?", (account_public_id,)).fetchone()
            if not account:
                raise AccountError("該当するサイトアカウントがありません。")
            if account["deleted_at"] is not None or str(account["status"]) == "released":
                raise AccountError("削除・登録枠解放済みのサイトアカウントにはVIPを付与できません。")
            ticket = None
            if key:
                ticket = conn.execute(
                    "SELECT id,account_id,deleted_at FROM purchase_tickets WHERE purchase_key=?", (key,)
                ).fetchone()
                if not ticket or int(ticket["account_id"]) != int(account["id"]):
                    raise AccountError("購入KEYとアカウントIDが一致しません。")
                if ticket["deleted_at"] is not None:
                    raise AccountError("削除済みの購入チケットからVIPは付与できません。")
                already_granted = conn.execute(
                    "SELECT 1 FROM vip_audit_logs WHERE action='grant' AND purchase_key=? LIMIT 1", (key,)
                ).fetchone()
                if already_granted:
                    return self._account_public(account)
            previous = float(account["vip_expires_at"]) if account["vip_expires_at"] is not None else None
            base = max(now, previous or 0.0) if bool(account["vip_active"]) else now
            new_expires = base + days * 86400
            started = float(account["vip_started_at"]) if account["vip_started_at"] is not None else now
            conn.execute(
                "UPDATE accounts SET vip_active=1,vip_started_at=?,vip_expires_at=? WHERE id=?",
                (started, new_expires, int(account["id"])),
            )
            conn.execute(
                "INSERT INTO vip_audit_logs(account_id,admin_id,action,duration_days,previous_expires_at,new_expires_at,purchase_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (int(account["id"]), str(admin_id), "grant", days, previous, new_expires, key or None, now),
            )
            if key:
                conn.execute(
                    "UPDATE purchase_tickets SET status='completed',updated_at=?,closed_at=? WHERE id=?",
                    (now, now, int(ticket["id"])),
                )
                period = f"{days}日間"
                conn.execute(
                    "INSERT INTO purchase_messages(ticket_id,sender_role,sender_label,content,created_at) VALUES(?,?,?,?,?)",
                    (int(ticket["id"]), "system", "AUTOCAT JP", f"VIP（{period}）が付与されました。VIPページをご利用いただけます。", now),
                )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account["id"]),)).fetchone()
        return self._account_public(row)

    def public_vip_stats(self) -> dict:
        now = time.time()
        with self._connect() as conn:
            active_vip = int(conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE vip_active=1 AND (vip_expires_at IS NULL OR vip_expires_at>?)",
                (now,),
            ).fetchone()[0])
            total_grants = int(conn.execute(
                "SELECT COUNT(*) FROM vip_audit_logs WHERE action='grant'"
            ).fetchone()[0])
            grants_30d = int(conn.execute(
                "SELECT COUNT(*) FROM vip_audit_logs WHERE action='grant' AND created_at>=?",
                (now - 30 * 86400,),
            ).fetchone()[0])
        return {
            "active_vip": active_vip,
            "total_grants": total_grants,
            "grants_30d": grants_30d,
        }

    def revoke_vip(self, public_id: object, admin_id: str) -> dict:
        account_public_id = str(public_id or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(account_public_id):
            raise AccountError("アカウントIDは16桁の英数字です。")
        now = time.time()
        with self._transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE public_id=?", (account_public_id,)).fetchone()
            if not account:
                raise AccountError("該当するサイトアカウントがありません。")
            previous = float(account["vip_expires_at"]) if account["vip_expires_at"] is not None else None
            conn.execute(
                "UPDATE accounts SET vip_active=0,vip_expires_at=?,trial_vip_uses_remaining=0 WHERE id=?",
                (now, int(account["id"])),
            )
            conn.execute(
                "INSERT INTO vip_audit_logs(account_id,admin_id,action,previous_expires_at,new_expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (int(account["id"]), str(admin_id), "revoke", previous, now, now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account["id"]),)).fetchone()
        return self._account_public(row)

    def rename_account(self, public_id: object, username: object, admin_id: str) -> dict:
        account_public_id = str(public_id or "").strip().upper()
        if not PUBLIC_ID_RE.fullmatch(account_public_id):
            raise AccountError("アカウントIDは16桁の英数字です。")
        clean_name, username_key = self._clean_username(username)
        now = time.time()
        with self._transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE public_id=?", (account_public_id,)).fetchone()
            if not account:
                raise AccountError("該当するサイトアカウントがありません。")
            if account["deleted_at"] is not None:
                raise AccountError("削除済みのサイトアカウントは変更できません。")
            duplicate = conn.execute(
                "SELECT 1 FROM accounts WHERE username_key=? AND id!=?", (username_key, int(account["id"]))
            ).fetchone()
            if duplicate:
                raise AccountError("そのユーザー名は既に使用されています。")
            conn.execute(
                "UPDATE accounts SET username=?,username_key=? WHERE id=?",
                (clean_name, username_key, int(account["id"])),
            )
            conn.execute(
                "INSERT INTO account_admin_audit(account_id,admin_id,action,created_at) VALUES(?,?,?,?)",
                (int(account["id"]), str(admin_id), "rename", now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account["id"]),)).fetchone()
        return self._account_public(row)

    def manage_account(self, public_id: object, action: object, admin_id: str) -> dict:
        account_public_id = str(public_id or "").strip().upper()
        clean_action = str(action or "").strip()
        if not PUBLIC_ID_RE.fullmatch(account_public_id):
            raise AccountError("アカウントIDは16桁の英数字です。")
        if clean_action not in {"suspend", "activate", "ban", "unban", "gban", "ungban"}:
            raise AccountError("アカウント操作が不正です。")
        now = time.time()
        with self._transaction() as conn:
            account = conn.execute("SELECT * FROM accounts WHERE public_id=?", (account_public_id,)).fetchone()
            if not account:
                raise AccountError("該当するサイトアカウントがありません。")
            if account["deleted_at"] is not None:
                raise AccountError("このサイトアカウントは削除済みです。")
            if clean_action == "suspend":
                conn.execute(
                    "UPDATE accounts SET suspended_at=?,status=CASE WHEN gban_active=1 THEN 'gban' "
                    "WHEN banned_at IS NOT NULL THEN 'banned' ELSE 'suspended' END WHERE id=?",
                    (now, int(account["id"])),
                )
            elif clean_action == "activate":
                if str(account["status"]) == "released":
                    raise AccountError("登録枠を解放済みのアカウントは再有効化できません。")
                conn.execute(
                    "UPDATE accounts SET suspended_at=NULL,status=CASE WHEN gban_active=1 THEN 'gban' "
                    "WHEN banned_at IS NOT NULL THEN 'banned' ELSE 'active' END WHERE id=?",
                    (int(account["id"]),),
                )
            elif clean_action == "ban":
                conn.execute(
                    "UPDATE accounts SET banned_at=?,status=CASE WHEN gban_active=1 THEN 'gban' ELSE 'banned' END WHERE id=?",
                    (now, int(account["id"])),
                )
            elif clean_action == "unban":
                conn.execute(
                    "UPDATE accounts SET banned_at=NULL,status=CASE WHEN gban_active=1 THEN 'gban' "
                    "WHEN suspended_at IS NOT NULL THEN 'suspended' ELSE 'active' END WHERE id=?",
                    (int(account["id"]),),
                )
            elif clean_action == "gban":
                existing = conn.execute(
                    "SELECT id FROM global_bans WHERE account_id=? AND active=1 ORDER BY id DESC LIMIT 1",
                    (int(account["id"]),),
                ).fetchone()
                if existing:
                    gban_id = int(existing["id"])
                else:
                    cur = conn.execute(
                        "INSERT INTO global_bans(account_id,active,created_by,created_at) VALUES(?,1,?,?)",
                        (int(account["id"]), str(admin_id), now),
                    )
                    gban_id = int(cur.lastrowid)
                for kind, key_hash in (("ip", str(account["ip_hash"])), ("fingerprint", str(account["fingerprint_hash"]))):
                    conn.execute(
                        "INSERT OR IGNORE INTO global_ban_identifiers(gban_id,kind,key_hash,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)",
                        (gban_id, kind, key_hash, now, now),
                    )
                conn.execute("UPDATE accounts SET gban_active=1,status='gban' WHERE id=?", (int(account["id"]),))
            else:
                conn.execute(
                    "UPDATE global_bans SET active=0,revoked_by=?,revoked_at=? WHERE account_id=? AND active=1",
                    (str(admin_id), now, int(account["id"])),
                )
                conn.execute(
                    "UPDATE accounts SET gban_active=0,status=CASE WHEN banned_at IS NOT NULL THEN 'banned' "
                    "WHEN suspended_at IS NOT NULL THEN 'suspended' ELSE 'active' END WHERE id=?",
                    (int(account["id"]),),
                )
            conn.execute(
                "INSERT INTO account_admin_audit(account_id,admin_id,action,created_at) VALUES(?,?,?,?)",
                (int(account["id"]), str(admin_id), clean_action, now),
            )
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account["id"]),)).fetchone()
        return self._account_public(row)

    def check_global_ban(self, raw_ip: str, fingerprint: object | None = None) -> dict:
        """Check active GBAN groups and extend every matched group with the observed pair."""
        ip_hash = self.identity_ip_hash(raw_ip)
        fp_hash = self.identity_fingerprint_hash(fingerprint) if fingerprint else None
        identifiers = [("ip", ip_hash)]
        if fp_hash:
            identifiers.append(("fingerprint", fp_hash))
        clauses = " OR ".join("(i.kind=? AND i.key_hash=?)" for _ in identifiers)
        params = [value for pair in identifiers for value in pair]
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT b.id,b.account_id FROM global_bans b "
                "JOIN global_ban_identifiers i ON i.gban_id=b.id "
                f"WHERE b.active=1 AND ({clauses})",
                params,
            ).fetchall()
        if not rows:
            return {"blocked": False, "gban_ids": [], "account_ids": []}

        now = time.time()
        matched_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in matched_ids)
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT id,account_id FROM global_bans WHERE active=1 AND id IN ({placeholders})",
                matched_ids,
            ).fetchall()
            for row in rows:
                gban_id = int(row["id"])
                for kind, key_hash in identifiers:
                    conn.execute(
                        "INSERT INTO global_ban_identifiers(gban_id,kind,key_hash,first_seen_at,last_seen_at) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(gban_id,kind,key_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                        (gban_id, kind, key_hash, now, now),
                    )
        return {
            "blocked": bool(rows),
            "gban_ids": [int(row["id"]) for row in rows],
            "account_ids": sorted({int(row["account_id"]) for row in rows}),
        }

    @staticmethod
    def _subscription(raw: object) -> tuple[str, str, str]:
        if not isinstance(raw, dict):
            raise AccountError("通知登録が不正です。")
        endpoint = str(raw.get("endpoint") or "").strip()
        keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if not endpoint.startswith("https://") or len(endpoint) > 2048:
            raise AccountError("通知先が不正です。")
        if not (20 <= len(p256dh) <= 512 and 8 <= len(auth) <= 256):
            raise AccountError("通知キーが不正です。")
        return endpoint, p256dh, auth

    def save_subscription(self, owner_key: str, raw: object) -> None:
        endpoint, p256dh, auth = self._subscription(raw)
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO account_push_subscriptions(endpoint,owner_key,p256dh,auth,created_at,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(endpoint) DO UPDATE SET owner_key=excluded.owner_key,p256dh=excluded.p256dh,auth=excluded.auth,updated_at=excluded.updated_at",
                (endpoint, owner_key, p256dh, auth, now, now),
            )

    def remove_subscription(self, owner_key: str, endpoint: object) -> None:
        value = str(endpoint or "").strip()
        with self._transaction() as conn:
            conn.execute("DELETE FROM account_push_subscriptions WHERE endpoint=? AND owner_key=?", (value, owner_key))

    def subscriptions(self, owner_key: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT endpoint,p256dh,auth FROM account_push_subscriptions WHERE owner_key=?", (owner_key,)
            ).fetchall()
        return [
            {"endpoint": str(row["endpoint"]), "keys": {"p256dh": str(row["p256dh"]), "auth": str(row["auth"])}}
            for row in rows
        ]

    def remove_subscription_endpoint(self, endpoint: str) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM account_push_subscriptions WHERE endpoint=?", (str(endpoint),))
