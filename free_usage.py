"""無料版の週間利用枠と招待報酬をSQLiteで安全に管理する。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sqlite3
import string
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
INVITATION_CODE_RE = re.compile(r"^[A-Za-z0-9]{30}$")
INVITATION_ALPHABET = string.ascii_letters + string.digits
RESERVATION_TTL_SECONDS = 2 * 60 * 60
MAX_STORED_CREDITS = 20
WEEKLY_FREE_CREDITS = 10
WEEKLY_POLICY_VERSION = 3
INVITER_REWARD = 3
INVITEE_REWARD = 2
DAILY_INVITER_LIMIT = 2
MONTHLY_INVITER_LIMIT = 10
DAILY_INVITEE_LIMIT = 1
MONTHLY_INVITEE_LIMIT = 3


class FreeUsageError(Exception):
    """無料版回数管理でユーザーに通知できるエラー。"""


class IdentityConflict(FreeUsageError):
    pass


class QuotaExhausted(FreeUsageError):
    pass


class InvitationError(FreeUsageError):
    pass


def _legacy_daily_bonus_for_login(streak_day: int, value: date) -> tuple[int, bool]:
    """旧DBの未移行レコードだけに使用する従来の連続ログイン付与量。"""
    if value.day == 31:
        return 0, True
    return {7: 3, 14: 4, 21: 5, 30: 10}.get(int(streak_day), 1), False


def _as_jst(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(JST)
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


class FreeUsageManager:
    def __init__(self, bonus_db_path: str, invitation_db_path: str, secret: bytes | str):
        self.bonus_db_path = os.path.abspath(bonus_db_path)
        self.invitation_db_path = os.path.abspath(invitation_db_path)
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("identity hash secret is required")
        self._secret = bytes(secret)
        self._prepare_paths()
        self._initialise_bonus_db()
        self._initialise_invitation_db()

    def _prepare_paths(self) -> None:
        for path in (self.bonus_db_path, self.invitation_db_path):
            directory = os.path.dirname(path)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    @staticmethod
    def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _bonus_connect(self) -> sqlite3.Connection:
        return self._configure_connection(
            sqlite3.connect(self.bonus_db_path, timeout=10, isolation_level=None)
        )

    def _invitation_connect(self) -> sqlite3.Connection:
        return self._configure_connection(
            sqlite3.connect(self.invitation_db_path, timeout=10, isolation_level=None)
        )

    @contextmanager
    def _bonus_transaction(self):
        conn = self._bonus_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def _invitation_transaction(self):
        conn = self._invitation_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialise_bonus_db(self) -> None:
        with self._bonus_connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS free_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    credits INTEGER NOT NULL DEFAULT 0 CHECK (credits >= 0),
                    weekly_credits INTEGER NOT NULL DEFAULT 0 CHECK (weekly_credits >= 0),
                    weekly_period TEXT,
                    weekly_policy_version INTEGER NOT NULL DEFAULT 3,
                    unlimited_date TEXT,
                    last_login_date TEXT,
                    login_streak INTEGER NOT NULL DEFAULT 0 CHECK (login_streak >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identity_keys (
                    kind TEXT NOT NULL CHECK (kind IN ('ip', 'fingerprint')),
                    key_hash TEXT NOT NULL,
                    account_id INTEGER NOT NULL REFERENCES free_accounts(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (kind, key_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_identity_account
                    ON identity_keys(account_id);
                CREATE TABLE IF NOT EXISTS daily_claims (
                    account_id INTEGER NOT NULL REFERENCES free_accounts(id) ON DELETE CASCADE,
                    claim_date TEXT NOT NULL,
                    reward INTEGER NOT NULL CHECK (reward >= 0),
                    unlimited INTEGER NOT NULL DEFAULT 0 CHECK (unlimited IN (0, 1)),
                    streak_day INTEGER NOT NULL DEFAULT 0 CHECK (streak_day >= 0),
                    claimed_at REAL NOT NULL,
                    PRIMARY KEY (account_id, claim_date)
                );
                CREATE TABLE IF NOT EXISTS credit_ledger (
                    event_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL REFERENCES free_accounts(id) ON DELETE CASCADE,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (event_id, account_id)
                );
                CREATE TABLE IF NOT EXISTS quota_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES free_accounts(id) ON DELETE CASCADE,
                    cost INTEGER NOT NULL CHECK (cost IN (0, 1)),
                    credit_source TEXT NOT NULL DEFAULT 'bonus',
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'consumed', 'refunded', 'expired_refunded')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quota_status_created
                    ON quota_reservations(status, created_at);
            """)
            account_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(free_accounts)")
            }
            if "weekly_credits" not in account_columns:
                conn.execute("ALTER TABLE free_accounts ADD COLUMN weekly_credits INTEGER NOT NULL DEFAULT 0")
            if "weekly_period" not in account_columns:
                conn.execute("ALTER TABLE free_accounts ADD COLUMN weekly_period TEXT")
            if "weekly_policy_version" not in account_columns:
                conn.execute("ALTER TABLE free_accounts ADD COLUMN weekly_policy_version INTEGER NOT NULL DEFAULT 0")
            reservation_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(quota_reservations)")
            }
            if "credit_source" not in reservation_columns:
                conn.execute("ALTER TABLE quota_reservations ADD COLUMN credit_source TEXT NOT NULL DEFAULT 'bonus'")
            claim_columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(daily_claims)"
                ).fetchall()
            }
            if "streak_day" not in claim_columns:
                conn.execute(
                    "ALTER TABLE daily_claims ADD COLUMN streak_day INTEGER NOT NULL DEFAULT 0"
                )
            self._migrate_calendar_bonus_claims(conn)
            # 異常値は常に上限内へ補正する。
            conn.execute(
                "UPDATE free_accounts SET credits=MIN(credits,?),updated_at=? WHERE credits>?",
                (MAX_STORED_CREDITS, time.time(), MAX_STORED_CREDITS),
            )
            conn.execute(
                "UPDATE free_accounts SET weekly_credits=MIN(?,MAX(0,?-credits),weekly_credits),updated_at=? "
                "WHERE weekly_credits>MIN(?,MAX(0,?-credits))",
                (
                    WEEKLY_FREE_CREDITS, MAX_STORED_CREDITS, time.time(),
                    WEEKLY_FREE_CREDITS, MAX_STORED_CREDITS,
                ),
            )
            # v3移行時だけ全利用者の旧残高をリセットする。IP・招待・利用履歴は残す。
            # 次回アクセス時に、その週の週間枠10回が通常どおり付与される。
            conn.execute(
                "UPDATE free_accounts SET credits=0,weekly_credits=0,weekly_period=NULL,"
                "unlimited_date=NULL,weekly_policy_version=?,updated_at=? "
                "WHERE weekly_policy_version<?",
                (WEEKLY_POLICY_VERSION, time.time(), WEEKLY_POLICY_VERSION),
            )
        try:
            os.chmod(self.bonus_db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_calendar_bonus_claims(conn: sqlite3.Connection) -> None:
        """旧版の日付基準付与を連続ログイン基準へ一度だけ補正する。"""
        account_rows = conn.execute(
            "SELECT DISTINCT account_id FROM daily_claims WHERE streak_day=0"
        ).fetchall()
        for (account_id_raw,) in account_rows:
            account_id = int(account_id_raw)
            claims = conn.execute(
                "SELECT claim_date,reward,unlimited,streak_day FROM daily_claims "
                "WHERE account_id=? ORDER BY claim_date",
                (account_id,),
            ).fetchall()
            previous_date = None
            streak = 0
            credit_delta = 0
            for claim_date_text, old_reward, old_unlimited, old_streak_day in claims:
                try:
                    claim_date = date.fromisoformat(str(claim_date_text))
                except ValueError:
                    continue
                same_month = bool(
                    previous_date
                    and previous_date.year == claim_date.year
                    and previous_date.month == claim_date.month
                )
                streak = (
                    streak + 1
                    if same_month and previous_date == claim_date - timedelta(days=1)
                    else 1
                )
                previous_date = claim_date
                if int(old_streak_day or 0) != 0:
                    continue
                new_reward, new_unlimited = _legacy_daily_bonus_for_login(streak, claim_date)
                credit_delta += int(new_reward) - int(old_reward or 0)
                conn.execute(
                    "UPDATE daily_claims SET reward=?,unlimited=?,streak_day=? "
                    "WHERE account_id=? AND claim_date=? AND streak_day=0",
                    (new_reward, int(new_unlimited), streak, account_id, claim_date_text),
                )
            if previous_date is not None:
                conn.execute(
                    "UPDATE free_accounts SET credits=MAX(0,credits+?),last_login_date=?,"
                    "login_streak=?,updated_at=? WHERE id=?",
                    (credit_delta, previous_date.isoformat(), streak, time.time(), account_id),
                )

    def _initialise_invitation_db(self) -> None:
        with self._invitation_connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS invitation_codes (
                    code TEXT PRIMARY KEY,
                    issuer_account_id INTEGER NOT NULL UNIQUE,
                    issuer_ip_hash TEXT NOT NULL,
                    issuer_fingerprint_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitation_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL REFERENCES invitation_codes(code),
                    inviter_account_id INTEGER NOT NULL,
                    invitee_account_id INTEGER NOT NULL,
                    invitee_ip_hash TEXT NOT NULL,
                    invitee_fingerprint_hash TEXT NOT NULL,
                    redemption_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'rewarding', 'rewarded')),
                    created_at REAL NOT NULL,
                    last_attempt_at REAL NOT NULL,
                    rewarded_at REAL,
                    vip_trial_processed_at REAL,
                    UNIQUE(code, invitee_account_id),
                    UNIQUE(code, invitee_ip_hash),
                    UNIQUE(code, invitee_fingerprint_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_invitation_code
                    ON invitation_redemptions(code);
            """)
            columns = {
                str(row[1]) for row in conn.execute(
                    "PRAGMA table_info(invitation_redemptions)"
                ).fetchall()
            }
            table_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='invitation_redemptions'"
            ).fetchone()
            table_sql = str(table_row[0] or "") if table_row else ""
            if (
                "last_attempt_at" not in columns
                or "redemption_date" not in columns
                or "'rewarding'" not in table_sql
                or "invitee_account_id INTEGER NOT NULL UNIQUE" in table_sql
            ):
                last_attempt_expr = (
                    "COALESCE(last_attempt_at,created_at)"
                    if "last_attempt_at" in columns else "created_at"
                )
                redemption_date_expr = (
                    "redemption_date"
                    if "redemption_date" in columns
                    else "date(created_at,'unixepoch','+9 hours')"
                )
                conn.execute("PRAGMA foreign_keys=OFF")
                try:
                    conn.executescript(f"""
                        BEGIN IMMEDIATE;
                        DROP INDEX IF EXISTS idx_invitation_code;
                        DROP INDEX IF EXISTS idx_invitation_inviter_day;
                        DROP INDEX IF EXISTS idx_invitation_invitee_day;
                        ALTER TABLE invitation_redemptions RENAME TO invitation_redemptions_legacy;
                        CREATE TABLE invitation_redemptions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            code TEXT NOT NULL REFERENCES invitation_codes(code),
                            inviter_account_id INTEGER NOT NULL,
                            invitee_account_id INTEGER NOT NULL,
                            invitee_ip_hash TEXT NOT NULL,
                            invitee_fingerprint_hash TEXT NOT NULL,
                            redemption_date TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('pending', 'rewarding', 'rewarded')),
                            created_at REAL NOT NULL,
                            last_attempt_at REAL NOT NULL,
                            rewarded_at REAL,
                            vip_trial_processed_at REAL,
                            UNIQUE(code, invitee_account_id),
                            UNIQUE(code, invitee_ip_hash),
                            UNIQUE(code, invitee_fingerprint_hash)
                        );
                        INSERT INTO invitation_redemptions(
                            id,code,inviter_account_id,invitee_account_id,
                            invitee_ip_hash,invitee_fingerprint_hash,redemption_date,status,
                            created_at,last_attempt_at,rewarded_at
                        )
                        SELECT id,code,inviter_account_id,invitee_account_id,
                               invitee_ip_hash,invitee_fingerprint_hash,{redemption_date_expr},
                               CASE WHEN status='rewarded' THEN 'rewarded' ELSE 'pending' END,
                               created_at,{last_attempt_expr},rewarded_at
                        FROM invitation_redemptions_legacy;
                        DROP TABLE invitation_redemptions_legacy;
                        CREATE INDEX idx_invitation_code ON invitation_redemptions(code);
                        CREATE INDEX idx_invitation_inviter_day
                            ON invitation_redemptions(inviter_account_id, redemption_date);
                        CREATE INDEX idx_invitation_invitee_day
                            ON invitation_redemptions(invitee_account_id, redemption_date);
                        COMMIT;
                    """)
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                finally:
                    conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_invitation_inviter_day
                    ON invitation_redemptions(inviter_account_id, redemption_date);
                CREATE INDEX IF NOT EXISTS idx_invitation_invitee_day
                    ON invitation_redemptions(invitee_account_id, redemption_date);
            """)
            refreshed_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(invitation_redemptions)")
            }
            if "vip_trial_processed_at" not in refreshed_columns:
                conn.execute("ALTER TABLE invitation_redemptions ADD COLUMN vip_trial_processed_at REAL")
        try:
            os.chmod(self.invitation_db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _normalise_ip(raw_ip: str) -> str:
        value = str(raw_ip or "").strip()
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError as exc:
            raise FreeUsageError("接続元IPを確認できませんでした。") from exc

    @staticmethod
    def _normalise_fingerprint(raw_fingerprint: str) -> str:
        value = str(raw_fingerprint or "").strip().lower()
        if not FINGERPRINT_RE.fullmatch(value):
            raise FreeUsageError("端末情報を確認できませんでした。ページを再読み込みしてください。")
        return value

    def _hash_identity(self, kind: str, value: str) -> str:
        return hmac.new(
            self._secret,
            f"{kind}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _identity_hashes(self, raw_ip: str, raw_fingerprint: str) -> tuple[str, str]:
        ip_value = self._normalise_ip(raw_ip)
        fingerprint = self._normalise_fingerprint(raw_fingerprint)
        return (
            self._hash_identity("ip", ip_value),
            self._hash_identity("fingerprint", fingerprint),
        )

    @staticmethod
    def _resolve_identity_locked(
        conn: sqlite3.Connection,
        ip_hash: str,
        fingerprint_hash: str,
        now_ts: float,
    ) -> int:
        rows = conn.execute(
            "SELECT account_id FROM identity_keys "
            "WHERE (kind='ip' AND key_hash=?) OR (kind='fingerprint' AND key_hash=?)",
            (ip_hash, fingerprint_hash),
        ).fetchall()
        account_ids = {int(row[0]) for row in rows}
        if len(account_ids) > 1:
            raise IdentityConflict(
                "IPと端末情報が別々の利用記録に紐づいています。同じ端末・回線で再度お試しください。"
            )
        if account_ids:
            account_id = account_ids.pop()
        else:
            cur = conn.execute(
                "INSERT INTO free_accounts(credits,created_at,updated_at) VALUES(0,?,?)",
                (now_ts, now_ts),
            )
            account_id = int(cur.lastrowid)

        for kind, key_hash in (("ip", ip_hash), ("fingerprint", fingerprint_hash)):
            conn.execute(
                "INSERT OR IGNORE INTO identity_keys(kind,key_hash,account_id,created_at) "
                "VALUES(?,?,?,?)",
                (kind, key_hash, account_id, now_ts),
            )
            owner = conn.execute(
                "SELECT account_id FROM identity_keys WHERE kind=? AND key_hash=?",
                (kind, key_hash),
            ).fetchone()
            if not owner or int(owner[0]) != account_id:
                raise IdentityConflict(
                    "IPまたは端末情報が別の利用記録で使用されています。"
                )
        conn.execute(
            "UPDATE free_accounts SET updated_at=? WHERE id=?",
            (now_ts, account_id),
        )
        return account_id

    @staticmethod
    def _release_stale_reservations_locked(conn: sqlite3.Connection, now_ts: float) -> None:
        stale_before = now_ts - RESERVATION_TTL_SECONDS
        rows = conn.execute(
            "SELECT reservation_id,account_id,cost,credit_source FROM quota_reservations "
            "WHERE status='reserved' AND created_at<?",
            (stale_before,),
        ).fetchall()
        for reservation_id, account_id, cost, credit_source in rows:
            cur = conn.execute(
                "UPDATE quota_reservations SET status='expired_refunded',updated_at=? "
                "WHERE reservation_id=? AND status='reserved'",
                (now_ts, reservation_id),
            )
            if cur.rowcount and int(cost):
                if str(credit_source) == "weekly":
                    conn.execute(
                        "UPDATE free_accounts SET weekly_credits=MIN(?,MAX(0,?-credits),weekly_credits+?),updated_at=? WHERE id=?",
                        (
                            WEEKLY_FREE_CREDITS, MAX_STORED_CREDITS,
                            int(cost), now_ts, int(account_id),
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE free_accounts SET credits=MIN(MAX(0,?-weekly_credits),credits+?),updated_at=? WHERE id=?",
                        (MAX_STORED_CREDITS, int(cost), now_ts, int(account_id)),
                    )

    @staticmethod
    def _refresh_weekly_locked(
        conn: sqlite3.Connection,
        account_id: int,
        now_jst: datetime,
        allow_month_end_unlimited: bool = False,
    ) -> dict:
        today = now_jst.date()
        today_text = today.isoformat()
        week_start = today - timedelta(days=today.weekday())
        week_period = week_start.isoformat()
        next_week = week_start + timedelta(days=7)
        next_reset_at = datetime(
            next_week.year, next_week.month, next_week.day, tzinfo=JST
        ).timestamp()
        account = conn.execute(
            "SELECT credits,weekly_credits,weekly_period,unlimited_date FROM free_accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if not account:
            raise FreeUsageError("利用回数情報を確認できませんでした。")
        banked_credits = int(account[0] or 0)
        weekly_credits = int(account[1] or 0)
        reset = str(account[2] or "") != week_period
        weekly_grant = 0
        if reset:
            weekly_credits = min(
                WEEKLY_FREE_CREDITS,
                max(0, MAX_STORED_CREDITS - banked_credits),
            )
            weekly_grant = weekly_credits
            conn.execute(
                "UPDATE free_accounts SET weekly_credits=?,weekly_period=?,updated_at=? WHERE id=?",
                (weekly_credits, week_period, now_jst.timestamp(), account_id),
            )
        month_end_day = today.day == 31
        unlimited = month_end_day and bool(allow_month_end_unlimited)
        unlimited_date = str(account[3] or "")
        unlimited_awarded = unlimited and unlimited_date != today_text
        if unlimited_awarded:
            conn.execute(
                "UPDATE free_accounts SET unlimited_date=?,updated_at=? WHERE id=?",
                (today_text, now_jst.timestamp(), account_id),
            )
            unlimited_date = today_text
        elif not unlimited and unlimited_date == today_text:
            conn.execute(
                "UPDATE free_accounts SET unlimited_date=NULL,updated_at=? WHERE id=?",
                (now_jst.timestamp(), account_id),
            )
            unlimited_date = ""
        return {
            "claimed": reset,
            "bonus": 0,
            "weekly_reset": reset,
            "weekly_grant": weekly_grant,
            "weekly_remaining": weekly_credits,
            "banked_remaining": banked_credits,
            "next_weekly_reset_at": next_reset_at,
            "month_end_unlimited_available": month_end_day,
            "month_end_account_required": month_end_day and not bool(allow_month_end_unlimited),
            "unlimited_awarded": bool(unlimited_awarded),
            "unlimited": unlimited_date == today_text,
            "remaining": banked_credits + weekly_credits,
            "login_streak": 0,
            "jst_date": today_text,
        }

    def _status_and_identity(
        self,
        raw_ip: str,
        raw_fingerprint: str,
        now: datetime | None = None,
        allow_month_end_unlimited: bool = False,
    ) -> tuple[dict, int, str, str]:
        now_jst = _as_jst(now)
        now_ts = now_jst.timestamp()
        ip_hash, fingerprint_hash = self._identity_hashes(raw_ip, raw_fingerprint)
        with self._bonus_transaction() as conn:
            self._release_stale_reservations_locked(conn, now_ts)
            account_id = self._resolve_identity_locked(
                conn, ip_hash, fingerprint_hash, now_ts
            )
            result = self._refresh_weekly_locked(
                conn, account_id, now_jst, allow_month_end_unlimited
            )
            result["credit_cap"] = MAX_STORED_CREDITS
            result["weekly_allowance"] = WEEKLY_FREE_CREDITS
        return result, account_id, ip_hash, fingerprint_hash

    def status(
        self,
        raw_ip: str,
        raw_fingerprint: str,
        now: datetime | None = None,
        allow_month_end_unlimited: bool = False,
    ) -> dict:
        result, account_id, ip_hash, fingerprint_hash = self._status_and_identity(
            raw_ip, raw_fingerprint, now, allow_month_end_unlimited
        )
        with self._invitation_connect() as conn:
            pending = conn.execute(
                "SELECT 1 FROM invitation_redemptions WHERE status IN ('pending','rewarding') AND "
                "(invitee_account_id=? OR invitee_ip_hash=? OR invitee_fingerprint_hash=?) LIMIT 1",
                (account_id, ip_hash, fingerprint_hash),
            ).fetchone()
        result["invitation_pending"] = bool(pending)
        return result

    def identity_account_id(self, raw_ip: str, raw_fingerprint: str) -> int:
        """Resolve the stable Free identity without changing invitation or quota state."""
        now_ts = time.time()
        ip_hash, fingerprint_hash = self._identity_hashes(raw_ip, raw_fingerprint)
        with self._bonus_transaction() as conn:
            return self._resolve_identity_locked(conn, ip_hash, fingerprint_hash, now_ts)

    def reserve(
        self,
        raw_ip: str,
        raw_fingerprint: str,
        reservation_id: str,
        now: datetime | None = None,
        allow_month_end_unlimited: bool = False,
    ) -> dict:
        now_jst = _as_jst(now)
        now_ts = now_jst.timestamp()
        today_text = now_jst.date().isoformat()
        ip_hash, fingerprint_hash = self._identity_hashes(raw_ip, raw_fingerprint)
        with self._bonus_transaction() as conn:
            self._release_stale_reservations_locked(conn, now_ts)
            account_id = self._resolve_identity_locked(
                conn, ip_hash, fingerprint_hash, now_ts
            )
            claim = self._refresh_weekly_locked(
                conn, account_id, now_jst, allow_month_end_unlimited
            )
            row = conn.execute(
                "SELECT credits,weekly_credits,unlimited_date FROM free_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not row:
                raise FreeUsageError("利用回数情報を確認できませんでした。")
            banked_credits = int(row[0])
            weekly_credits = int(row[1])
            unlimited_date = str(row[2] or "")
            unlimited = unlimited_date == today_text
            cost = 0 if unlimited else 1
            if cost and weekly_credits + banked_credits < cost:
                raise QuotaExhausted(
                    "残り使用可能回数がありません。次週の無料枠または招待報酬をお待ちください。"
                )
            credit_source = "unlimited"
            if cost:
                if weekly_credits > 0:
                    credit_source = "weekly"
                    conn.execute(
                        "UPDATE free_accounts SET weekly_credits=weekly_credits-1,updated_at=? WHERE id=?",
                        (now_ts, account_id),
                    )
                    weekly_credits -= 1
                else:
                    credit_source = "bonus"
                    conn.execute(
                        "UPDATE free_accounts SET credits=credits-1,updated_at=? WHERE id=?",
                        (now_ts, account_id),
                    )
                    banked_credits -= 1
            conn.execute(
                "INSERT INTO quota_reservations(reservation_id,account_id,cost,credit_source,status,created_at,updated_at) "
                "VALUES(?,?,?,?,'reserved',?,?)",
                (str(reservation_id), account_id, cost, credit_source, now_ts, now_ts),
            )
            claim.update({
                "remaining": weekly_credits + banked_credits,
                "weekly_remaining": weekly_credits,
                "banked_remaining": banked_credits,
                "unlimited": unlimited,
            })
            return claim

    def settle(self, reservation_id: str | None, success: bool) -> bool:
        if not reservation_id:
            return False
        now_ts = time.time()
        settled = False
        reward_account_id = None
        with self._bonus_transaction() as conn:
            row = conn.execute(
                "SELECT account_id,cost,status,credit_source FROM quota_reservations WHERE reservation_id=?",
                (str(reservation_id),),
            ).fetchone()
            if not row:
                return False
            account_id, cost = int(row[0]), int(row[1])
            current_status = str(row[2])
            credit_source = str(row[3] or "bonus")
            if current_status == "reserved":
                target_status = "consumed" if success else "refunded"
                cur = conn.execute(
                    "UPDATE quota_reservations SET status=?,updated_at=? "
                    "WHERE reservation_id=? AND status='reserved'",
                    (target_status, now_ts, str(reservation_id)),
                )
                settled = cur.rowcount == 1
                if settled and not success and cost:
                    if credit_source == "weekly":
                        conn.execute(
                            "UPDATE free_accounts SET weekly_credits=MIN(?,MAX(0,?-credits),weekly_credits+?),updated_at=? WHERE id=?",
                            (
                                WEEKLY_FREE_CREDITS, MAX_STORED_CREDITS,
                                cost, now_ts, account_id,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE free_accounts SET credits=MIN(MAX(0,?-weekly_credits),credits+?),updated_at=? WHERE id=?",
                            (MAX_STORED_CREDITS, cost, now_ts, account_id),
                        )
            elif success and current_status == "consumed":
                # ジョブ状態の再取得時も、途中で止まった招待確定だけ安全に再試行する。
                settled = False
            else:
                return False
            if success:
                reward_account_id = account_id
        if reward_account_id is not None:
            self._complete_pending_invitation(reward_account_id, now_ts)
        return settled

    def invitation_link(
        self,
        raw_ip: str,
        raw_fingerprint: str,
        public_base_url: str,
        now: datetime | None = None,
        allow_month_end_unlimited: bool = False,
    ) -> dict:
        status, account_id, ip_hash, fingerprint_hash = self._status_and_identity(
            raw_ip, raw_fingerprint, now, allow_month_end_unlimited
        )
        now_ts = _as_jst(now).timestamp()
        today = _as_jst(now).date().isoformat()
        with self._invitation_transaction() as conn:
            row = conn.execute(
                "SELECT code FROM invitation_codes WHERE issuer_account_id=? AND active=1",
                (account_id,),
            ).fetchone()
            if row:
                code = str(row[0])
            else:
                for _ in range(10):
                    code = "".join(secrets.choice(INVITATION_ALPHABET) for _ in range(30))
                    try:
                        conn.execute(
                            "INSERT INTO invitation_codes(code,issuer_account_id,issuer_ip_hash,"
                            "issuer_fingerprint_hash,active,created_at) VALUES(?,?,?,?,1,?)",
                            (code, account_id, ip_hash, fingerprint_hash, now_ts),
                        )
                        break
                    except sqlite3.IntegrityError:
                        code = ""
                if not code:
                    raise InvitationError("招待リンクを発行できませんでした。")
            invited_today = int(conn.execute(
                "SELECT COUNT(*) FROM invitation_redemptions "
                "WHERE inviter_account_id=? AND redemption_date=?",
                (account_id, today),
            ).fetchone()[0])
        base = str(public_base_url or "https://autocat.jp").rstrip("/")
        status.update({
            "code": code,
            "url": f"{base}/?code={code}",
            "daily_invited": min(invited_today, DAILY_INVITER_LIMIT),
            "daily_invite_remaining": max(0, DAILY_INVITER_LIMIT - invited_today),
        })
        return status

    @staticmethod
    def _credit_once_locked(
        conn: sqlite3.Connection,
        event_id: str,
        account_id: int,
        amount: int,
        reason: str,
        now_ts: float,
    ) -> int:
        row = conn.execute(
            "SELECT credits,weekly_credits FROM free_accounts WHERE id=?", (account_id,)
        ).fetchone()
        if not row:
            raise InvitationError("招待報酬の付与先を確認できませんでした。")
        granted = min(
            int(amount),
            max(0, MAX_STORED_CREDITS - int(row[0]) - int(row[1] or 0)),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO credit_ledger(event_id,account_id,amount,reason,created_at) "
            "VALUES(?,?,?,?,?)",
            (event_id, account_id, granted, reason, now_ts),
        )
        if cur.rowcount:
            updated = conn.execute(
                "UPDATE free_accounts SET credits=credits+?,updated_at=? WHERE id=?",
                (granted, now_ts, account_id),
            )
            if updated.rowcount != 1:
                raise InvitationError("招待報酬の付与先を確認できませんでした。")
            return granted
        return 0

    def redeem_invitation(
        self,
        code: str,
        raw_ip: str,
        raw_fingerprint: str,
        now: datetime | None = None,
        allow_month_end_unlimited: bool = False,
    ) -> dict:
        code = str(code or "").strip()
        if not INVITATION_CODE_RE.fullmatch(code):
            raise InvitationError("招待コードの形式が不正です。")
        status, invitee_account_id, ip_hash, fingerprint_hash = self._status_and_identity(
            raw_ip, raw_fingerprint, now, allow_month_end_unlimited
        )
        now_ts = _as_jst(now).timestamp()
        today = _as_jst(now).date().isoformat()
        month = today[:7]

        with self._invitation_transaction() as conn:
            code_row = conn.execute(
                "SELECT issuer_account_id,issuer_ip_hash,issuer_fingerprint_hash "
                "FROM invitation_codes WHERE code=? AND active=1",
                (code,),
            ).fetchone()
            if not code_row:
                raise InvitationError("招待コードが存在しないか、無効です。")
            inviter_account_id = int(code_row[0])
            if (
                inviter_account_id == invitee_account_id
                or str(code_row[1]) == ip_hash
                or str(code_row[2]) == fingerprint_hash
            ):
                raise InvitationError("自分の招待リンクは使用できません。")

            existing = conn.execute(
                "SELECT id,code,invitee_account_id,invitee_ip_hash,invitee_fingerprint_hash,status,last_attempt_at "
                "FROM invitation_redemptions WHERE code=? AND "
                "(invitee_account_id=? OR invitee_ip_hash=? OR invitee_fingerprint_hash=?) LIMIT 1",
                (code, invitee_account_id, ip_hash, fingerprint_hash),
            ).fetchone()
            if existing:
                status_value = str(existing[5])
                if status_value == "pending":
                    status.update({
                        "invitation_reward": INVITEE_REWARD,
                        "invitation_pending": True,
                        "pending": True,
                        "redeemed": False,
                        "daily_redeem_remaining": 0,
                    })
                    return status
                if status_value == "rewarding":
                    raise InvitationError("招待報酬を処理中です。少し待ってから再読み込みしてください。")
                raise InvitationError("この招待リンクの報酬は受け取り済みです。")

            active_pending = conn.execute(
                "SELECT 1 FROM invitation_redemptions WHERE status IN ('pending','rewarding') AND "
                "(invitee_account_id=? OR invitee_ip_hash=? OR invitee_fingerprint_hash=?) LIMIT 1",
                (invitee_account_id, ip_hash, fingerprint_hash),
            ).fetchone()
            if active_pending:
                raise InvitationError("前の招待が確定待ちです。無料版を1回成功させてから次の招待リンクを使用してください。")

            daily_redeemed = int(conn.execute(
                "SELECT COUNT(*) FROM invitation_redemptions WHERE redemption_date=? AND "
                "(invitee_account_id=? OR invitee_ip_hash=? OR invitee_fingerprint_hash=?)",
                (today, invitee_account_id, ip_hash, fingerprint_hash),
            ).fetchone()[0])
            if daily_redeemed >= DAILY_INVITEE_LIMIT:
                raise InvitationError("本日受け取れる招待は1人分までです。")
            monthly_redeemed = int(conn.execute(
                "SELECT COUNT(*) FROM invitation_redemptions WHERE substr(redemption_date,1,7)=? AND "
                "(invitee_account_id=? OR invitee_ip_hash=? OR invitee_fingerprint_hash=?)",
                (month, invitee_account_id, ip_hash, fingerprint_hash),
            ).fetchone()[0])
            if monthly_redeemed >= MONTHLY_INVITEE_LIMIT:
                raise InvitationError("今月受け取れる招待は3人分までです。")

            inviter_today = int(conn.execute(
                "SELECT COUNT(*) FROM invitation_redemptions WHERE inviter_account_id=? AND redemption_date=?",
                (inviter_account_id, today),
            ).fetchone()[0])
            if inviter_today >= DAILY_INVITER_LIMIT:
                raise InvitationError("この招待リンクは本日の2人上限に達しています。")
            inviter_month = int(conn.execute(
                "SELECT COUNT(*) FROM invitation_redemptions WHERE inviter_account_id=? "
                "AND substr(redemption_date,1,7)=?",
                (inviter_account_id, month),
            ).fetchone()[0])
            if inviter_month >= MONTHLY_INVITER_LIMIT:
                raise InvitationError("この招待リンクは今月の10人上限に達しています。")

            try:
                conn.execute(
                    "INSERT INTO invitation_redemptions(code,inviter_account_id,invitee_account_id,"
                    "invitee_ip_hash,invitee_fingerprint_hash,redemption_date,status,created_at,last_attempt_at) "
                    "VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (
                        code, inviter_account_id, invitee_account_id, ip_hash,
                        fingerprint_hash, today, now_ts, now_ts,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvitationError("この招待リンクは既に使用されています。") from exc

        status.update({
            "invitation_reward": INVITEE_REWARD,
            "invitation_pending": True,
            "pending": True,
            "redeemed": False,
            "daily_redeemed": daily_redeemed + 1,
            "daily_redeem_remaining": max(0, DAILY_INVITEE_LIMIT - daily_redeemed - 1),
            "monthly_redeemed": monthly_redeemed + 1,
            "monthly_redeem_remaining": max(0, MONTHLY_INVITEE_LIMIT - monthly_redeemed - 1),
        })
        return status

    def _complete_pending_invitation(self, invitee_account_id: int, now_ts: float) -> dict | None:
        """招待リンクを踏んだ利用者の次の成功時に、双方へ一度だけ報酬を付与する。"""
        with self._invitation_transaction() as conn:
            row = conn.execute(
                "SELECT id,inviter_account_id,status,last_attempt_at FROM invitation_redemptions "
                "WHERE invitee_account_id=? AND status IN ('pending','rewarding') "
                "ORDER BY id LIMIT 1",
                (int(invitee_account_id),),
            ).fetchone()
            if not row:
                return None
            redemption_id, inviter_account_id = int(row[0]), int(row[1])
            status_value, last_attempt_at = str(row[2]), float(row[3] or 0)
            if status_value == "rewarding" and now_ts - last_attempt_at < 60:
                return None
            cur = conn.execute(
                "UPDATE invitation_redemptions SET status='rewarding',last_attempt_at=? "
                "WHERE id=? AND (status='pending' OR (status='rewarding' AND last_attempt_at<=?))",
                (now_ts, redemption_id, now_ts - 60),
            )
            if cur.rowcount != 1:
                return None

        event_id = f"invitation:{redemption_id}"
        with self._bonus_transaction() as conn:
            inviter_granted = self._credit_once_locked(
                conn, event_id, inviter_account_id, INVITER_REWARD, "invitation_issuer", now_ts
            )
            invitee_granted = self._credit_once_locked(
                conn, event_id, int(invitee_account_id), INVITEE_REWARD, "invitation_invitee", now_ts
            )

        with self._invitation_transaction() as conn:
            conn.execute(
                "UPDATE invitation_redemptions SET status='rewarded',rewarded_at=? "
                "WHERE id=? AND status='rewarding'",
                (now_ts, redemption_id),
            )
        return {
            "redemption_id": redemption_id,
            "inviter_reward": inviter_granted,
            "invitee_reward": invitee_granted,
        }

    def pending_vip_trial_events(self, limit: int = 20) -> list[dict]:
        """Return rewarded invitation events not yet handled by the site-account trial."""
        safe_limit = max(1, min(100, int(limit)))
        with self._invitation_connect() as conn:
            rows = conn.execute(
                "SELECT id,inviter_account_id,rewarded_at FROM invitation_redemptions "
                "WHERE status='rewarded' AND vip_trial_processed_at IS NULL "
                "ORDER BY id LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [
            {
                "redemption_id": int(row[0]),
                "inviter_account_id": int(row[1]),
                "rewarded_at": float(row[2] or 0),
            }
            for row in rows
        ]

    def mark_vip_trial_processed(self, redemption_id: object, now: float | None = None) -> None:
        try:
            event_id = int(redemption_id)
        except (TypeError, ValueError) as exc:
            raise InvitationError("招待成立情報が不正です。") from exc
        with self._invitation_transaction() as conn:
            conn.execute(
                "UPDATE invitation_redemptions SET vip_trial_processed_at=? "
                "WHERE id=? AND status='rewarded' AND vip_trial_processed_at IS NULL",
                (time.time() if now is None else float(now), event_id),
            )
