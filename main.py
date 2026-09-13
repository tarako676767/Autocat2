from flask import Flask, request, jsonify, render_template, session, redirect
import os
import re
import copy
import base64
import gzip
import json
import uuid
import secrets
import hashlib
import threading
import traceback
import time
import requests
from datetime import timedelta
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from ACCOUNT.access_guard import register_access_guard
from ACCOUNT.account_routes import register_account_routes
from ACCOUNT.account_store import AccountPermissionError, AccountStore
from CHAT.chat_routes import register_chat_routes
from CHAT.chat_store import ChatStore
from CHAT.dm_routes import register_dm_routes
from CHAT.dm_store import DMStore
from CHAT.moderation_routes import register_moderation_routes
from DISCORD.discord_log import send_usage_log
from free_usage import (
    FreeUsageError,
    FreeUsageManager,
    IdentityConflict,
    InvitationError,
    QuotaExhausted,
)

load_dotenv()

if __name__ == '__main__':
    # os.environ.get('PORT') でサーバー側が割り当てるポート番号を取得する
    port = int(os.environ.get('PORT', 5000))
    # host='0.0.0.0' に設定しないと外部（スマホなど）からアクセスできません
    app.run(host='0.0.0.0', port=port)

# =====================
# プロキシ設定（全通信共通）
# =====================
PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {}
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["http_proxy"] = PROXY_URL
    os.environ["https_proxy"] = PROXY_URL

# ── requests.Session を継承してプロキシを強制注入 ──
import functools as _ft
_orig_requests_Session = requests.Session
class _ProxiedRequestsSession(_orig_requests_Session):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.proxies.update(PROXIES)
requests.Session = _ProxiedRequestsSession

# ── requests モジュールレベル関数にもプロキシを強制付与 ──
for _mn in ("get", "post", "put", "patch", "delete", "head", "options", "request"):
    _orig_fn = getattr(requests, _mn)
    def _make_proxied(fn):
        @_ft.wraps(fn)
        def _wrapped(*a, **kw):
            kw.setdefault("proxies", PROXIES)
            return fn(*a, **kw)
        return _wrapped
    setattr(requests, _mn, _make_proxied(_orig_fn))

# ── urllib.request グローバル opener にプロキシを強制設定 ──
import urllib.request as _urllib_req
_urllib_proxy_handler = _urllib_req.ProxyHandler(PROXIES)
_urllib_req.install_opener(_urllib_req.build_opener(_urllib_proxy_handler))

# プロキシ疎通確認（外部通信を伴うため明示有効時のみ）
if PROXY_URL and os.getenv("PROXY_CHECK_ON_STARTUP", "0") == "1":
    try:
        _r = requests.get("https://httpbin.org/ip", proxies=PROXIES, timeout=10)
        if _r.status_code == 200:
            print(f"[PROXY CHECK] OK - 外部IP: {_r.json()['origin']}")
        else:
            print(f"[PROXY CHECK] FAILED - HTTPステータス: {_r.status_code}")
    except Exception as _e:
        print(f"[PROXY CHECK] FAILED - {type(_e).__name__}")
elif not PROXY_URL:
    print("[PROXY CHECK] SKIPPED - PROXY_URLが未設定です")

# =====================
# 設定値
# =====================
MAX_API_KEYS = 5000          # api_keys 辞書の上限
API_KEY_TTL = 600            # APIキーの有効期間(秒)
MAX_WORKERS = 10              # ジョブ実行ワーカー数
MAX_INFLIGHT_JOBS = 30       # 同時に受け付ける未完了ジョブ数
JOB_TIMEOUT = 300            # 進捗が更新されないジョブのタイムアウト(秒)
JOB_ABSOLUTE_TIMEOUT = 600   # 進捗が続いていても待つ絶対上限(秒)
CLONE_JOB_TIMEOUT = 600      # 複製中の連続した認証・保存通信は最大10分待つ
CLONE_JOB_ABSOLUTE_TIMEOUT = 1800  # 複数コピーの連続通信を考慮して30分
MAX_CHAR_LIST = 300          # char_list の最大要素数
MAX_CHARACTER_SETTINGS = 1000  # レアリティ単位・全871体の名前選択に対応
MAX_COUNT = 5                # 作成/複製の最大個数
MAX_LEGEND_SELECTIONS = 5000 # レジェンド系の章/星/ステージ選択数上限

# にゃんこ大戦争 JP 15.5.1。通信・新規作成・複製・ゲームデータ参照で
# 別々の値を使うと、新キャラ枠や第四形態の判定が旧版へ戻るため一元管理する。
TARGET_GAME_VERSION_NUMBER = 150501

# 通常ページから非表示にするだけでなく、APIへ直接送られてもVIP確認なしでは適用しない。
VIP_ONLY_SYSTEM_ACTIONS = {
    "user_rank_rewards_claimed",
    "user_rank_rewards_unclaimed",
    "catguide_rewards_claimed",
    "catguide_rewards_unclaimed",
    "cat_scratcher_reset",
    "all_missions_clear",
    "labyrinth_medals",
    "ototo_detailed",
    "dojo_score_detailed",
    "future_score_detailed",
}

# =====================
# Discord OAuth2（チャット・管理者認証用）
# =====================
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

def _positive_env_int(name, fallback):
    try:
        value = int(os.getenv(name, str(fallback)))
        return value if 1 <= value <= 1_000_000 else fallback
    except (TypeError, ValueError):
        return fallback


VIP_PLAN_PRICES = {
    30: _positive_env_int("VIP_PRICE_30", 300),
    60: _positive_env_int("VIP_PRICE_60", 600),
    90: _positive_env_int("VIP_PRICE_90", 900),
}
VIP_PRICE_LABEL = f"30日{VIP_PLAN_PRICES[30]:,}円から"
VIP_PLAN_LABEL = os.getenv("VIP_PLAN_LABEL", "VIPプラン").strip() or "VIPプラン"

DISCORD_API = "https://discord.com/api"
OAUTH_SCOPES = "identify"

# =====================
# 入力フォーマット定義
# =====================
TRANSFER_CODE_RE = re.compile(r'^[0-9a-fA-F]{9}$')
AUTH_CODE_RE = re.compile(r'^\d{4}$')
# 「時間:分」形式のみ許可。時間は最大4桁(9999まで)、分は0〜59のみ。
PLAYTIME_RE = re.compile(r'^(\d{1,4}):([0-5]?\d)$')
OPERATION_ID_RE = re.compile(r'^[A-Za-z0-9]{30}$')
ADMIN_USER = (os.getenv("ADMIN_USER") or os.getenv("Admin_USER", "")).strip()
ADMIN_SNAPSHOT_KEY = os.getenv("ADMIN_SNAPSHOT_KEY", "").strip()


def validate_transfer_auth_codes(data):
    """引き継ぎコード(9桁数字)・認証番号(4桁数字)を厳密に検証。問題なければ None。"""
    tc = str(data.get("transfer_code", "")).strip()
    ac = str(data.get("auth_code", "")).strip()
    if not TRANSFER_CODE_RE.match(tc):
        return jsonify({"error": "引き継ぎコードは9桁の16進数（0-9,a-f）で入力してください"}), 400
    if not AUTH_CODE_RE.match(ac):
        return jsonify({"error": "認証番号は4桁の数字（0-9）で入力してください"}), 400
    return None


def validate_playtime_str(value):
    """'時間:分' 形式のみ許可。不正なら None、OKなら (hours, minutes) のタプルを返す。"""
    if not isinstance(value, str):
        return None
    m = PLAYTIME_RE.match(value.strip())
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2))
    if hours > 9999 or minutes > 59:
        return None
    return hours, minutes


def safe_custom_playtime(data):
    """custom_playtime を検証済みの文字列として返す。不正・未指定なら空文字。"""
    raw = data.get("custom_playtime", "")
    if raw in (None, ""):
        return ""
    raw_str = str(raw).strip()
    return raw_str if validate_playtime_str(raw_str) else ""


# =====================
# APIキー管理
# =====================
def generate_api_key():
    key = str(uuid.uuid4())
    with _job_db_connect() as conn:
        conn.execute(
            "INSERT INTO api_request_keys(api_key,created_at,used) VALUES(?,?,0)",
            (key, time.time()),
        )
        conn.execute(
            "DELETE FROM api_request_keys WHERE api_key IN "
            "(SELECT api_key FROM api_request_keys ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
            (MAX_API_KEYS,),
        )
    return key


def validate_and_consume_api_key(key: str) -> bool:
    with _job_db_connect() as conn:
        cur = conn.execute(
            "UPDATE api_request_keys SET used=1 "
            "WHERE api_key=? AND used=0 AND created_at>=?",
            (str(key), time.time() - API_KEY_TTL),
        )
    return cur.rowcount == 1


def cleanup_api_keys():
    while True:
        time.sleep(60)
        with _job_db_connect() as conn:
            conn.execute(
                "DELETE FROM api_request_keys WHERE created_at < ? OR used=1",
                (time.time() - API_KEY_TTL,),
            )


try:
    from bcsfe import core
    from bcsfe.core.server.server_handler import ServerHandler
    from bcsfe.core.game.catbase.cat import Talent
    from bcsfe.core.game.catbase.user_rank_rewards import Reward
    from bcsfe.core.game.catbase.playtime import PlayTime
    from bcsfe.core.game.map.outbreaks import (
        Outbreak as ZombieOutbreak,
        Chapter as ZombieChapter,
    )
except ImportError:
    import core
    from core.server.server_handler import ServerHandler

# BCSFE 3.6.0は保存ファイルのアップロードだけタイムアウトなしで、その他は
# 既定30秒。複製では保存回数が増えるため、接続15秒・通常45秒・保存90秒の
# 上限を明示する。TimeoutもConnectionErrorと同様にNoneへ変換し、BCSFE側の
# 安全な段階別失敗処理へ戻す。
def _bounded_timeout_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


BCSFE_CONNECT_TIMEOUT = _bounded_timeout_env("BCSFE_CONNECT_TIMEOUT", 15, 5, 60)
BCSFE_API_TIMEOUT = _bounded_timeout_env("BCSFE_API_TIMEOUT", 45, 15, 120)
BCSFE_UPLOAD_TIMEOUT = _bounded_timeout_env("BCSFE_UPLOAD_TIMEOUT", 90, 30, 300)


def _bounded_bcsfe_request_post(self, no_timeout=False):
    read_timeout = BCSFE_UPLOAD_TIMEOUT if self.form is not None else BCSFE_API_TIMEOUT
    try:
        return requests.post(
            self.url,
            headers=self.headers,
            data=self.data.data,
            timeout=(BCSFE_CONNECT_TIMEOUT, read_timeout),
            files=None if self.form is None else self.form.into_files(),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        print(f"[BCSFE NETWORK] POST failed: {type(exc).__name__}")
        return None


core.RequestHandler.post = _bounded_bcsfe_request_post

core.core_data.init_data()

TARGET_GAME_VERSION = core.GameVersion(TARGET_GAME_VERSION_NUMBER)
BCSFE_EDIT_LOCK = threading.RLock()

app = Flask(__name__, template_folder="HTML")
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(32))
# VIPのイベント詳細は、全選択時に数千ステージ分の指定を送る。
# 64KBでは正常な操作でも413になるため、既存機能が収まる上限へ更新する。
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
# 同一ドメイン上の別Flaskアプリが既定の ``session`` Cookieを上書きしても、
# Discord認証・サイトアカウント・チャットのセッションが分断されないよう専用名にする。
app.config["SESSION_COOKIE_NAME"] = "autocat_session"
app.config["SESSION_COOKIE_PATH"] = "/"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://autocat.jp").strip().rstrip("/")


# =====================
# 実IP取得（Cloudflare 前提）
# =====================
def real_ip():
    # オリジンを Cloudflare 経由のみに絞っている前提。
    # そうでない環境では CF-Connecting-IP は信頼できない点に注意。
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


limiter = Limiter(
    key_func=real_ip,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# =====================
# ジョブ実行プール
# =====================
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

jobs: dict = {}
jobs_lock = threading.Lock()
JOBS_MAX = 200
JOBS_TTL = 60 * 30
OPERATION_RETENTION = 2 * 24 * 60 * 60  # 受付IDと失敗セーブは48時間で失効

# =====================
# 使用回数カウンター（代行・作成・複製の成功回数を全種合算。count.dbに永続化し再起動後も保持）
# =====================
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, "BACKUP")
os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
try:
    os.chmod(BACKUP_DIR, 0o700)
except OSError:
    pass
USAGE_DB_PATH = os.path.join(BASE_DIR, "count.db")
try:
    if os.path.exists(USAGE_DB_PATH):
        os.chmod(USAGE_DB_PATH, 0o600)
except OSError:
    pass
OPERATION_DB_PATH = os.path.join(BACKUP_DIR, "admin_recovery.db")
BONUS_DB_PATH = os.path.join(BASE_DIR, "Login", "Bonus.db")
INVITATION_DB_PATH = os.path.join(BASE_DIR, "invitation", "invitation.db")
CHAT_DB_PATH = os.path.join(BASE_DIR, "CHAT", "chat.db")
ACCOUNT_DB_PATH = os.path.join(BASE_DIR, "ACCOUNT", "account.db")
usage_count_lock = threading.Lock()

_identity_secret = os.getenv("IDENTITY_HASH_KEY") or os.getenv("FLASK_SECRET_KEY")
if not _identity_secret:
    # FLASK_SECRET_KEY未設定時の開発用フォールバック。本番では再起動をまたいで
    # 同じ利用者を識別できるよう、必ずいずれかの環境変数を固定する。
    _identity_secret = app.secret_key if isinstance(app.secret_key, bytes) else str(app.secret_key)
    print("[WARNING] IDENTITY_HASH_KEY/FLASK_SECRET_KEY is not configured; identity hashes may change after restart.")
free_usage_manager = FreeUsageManager(
    BONUS_DB_PATH,
    INVITATION_DB_PATH,
    _identity_secret,
)
chat_store = ChatStore(
    CHAT_DB_PATH,
    os.getenv("CHAT_IDENTITY_HASH_KEY") or _identity_secret,
)
dm_store = DMStore(
    CHAT_DB_PATH,
    os.getenv("CHAT_IDENTITY_HASH_KEY") or _identity_secret,
)
account_store = AccountStore(
    ACCOUNT_DB_PATH,
    os.getenv("ACCOUNT_IDENTITY_HASH_KEY") or _identity_secret,
    vip_plan_prices=VIP_PLAN_PRICES,
)


def _process_invitation_vip_trials() -> None:
    """Idempotently turn newly rewarded invitations into one-use VIP trials."""
    try:
        events = free_usage_manager.pending_vip_trial_events(20)
    except Exception as exc:
        print(f"[VIP TRIAL] event lookup failed: {type(exc).__name__}")
        return
    for event in events:
        try:
            account_store.grant_invitation_trial(
                event["inviter_account_id"], event["redemption_id"], event["rewarded_at"]
            )
            free_usage_manager.mark_vip_trial_processed(event["redemption_id"])
        except Exception as exc:
            # Leave the event unprocessed so a later status/job request retries it.
            print(f"[VIP TRIAL] grant failed: {type(exc).__name__}")


def _usage_db_connect():
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_count (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("INSERT OR IGNORE INTO usage_count (id, count) VALUES (1, 0)")
    conn.commit()
    return conn


_usage_db_conn = _usage_db_connect()
try:
    os.chmod(USAGE_DB_PATH, 0o600)
except OSError:
    pass


# ジョブ状態はメモリだけだと、Gunicorn等の別ワーカーに
# /api/job が割り当てられた時やワーカー再起動後に404になる。
# count.db内にJSONとして保存し、プロセス間で共有する。
@contextmanager
def _job_db_connect():
    conn = sqlite3.connect(USAGE_DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS background_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_request_keys (
                api_key TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


with _job_db_connect() as _job_init_conn:
    pass


@contextmanager
def _operation_db_connect():
    conn = sqlite3.connect(OPERATION_DB_PATH, timeout=10, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operation_records (
                operation_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL UNIQUE,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                recovery_status TEXT NOT NULL DEFAULT 'not_needed',
                snapshot BLOB,
                reissue_result BLOB,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_records)")}
        if "reissue_result" not in columns:
            conn.execute("ALTER TABLE operation_records ADD COLUMN reissue_result BLOB")
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


with _operation_db_connect() as _operation_init_conn:
    pass
try:
    os.chmod(OPERATION_DB_PATH, 0o600)
except OSError:
    pass

threading.Thread(target=cleanup_api_keys, daemon=True).start()


def _persist_job(job_id: str, job: dict):
    payload = json.dumps(job, ensure_ascii=False, separators=(",", ":"))
    created_at = float(job.get("created_at", time.time()))
    updated_at = float(job.get("updated_at", created_at))
    with _job_db_connect() as conn:
        conn.execute(
            """INSERT INTO background_jobs(job_id,status,payload,created_at,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status=excluded.status,payload=excluded.payload,
                 created_at=excluded.created_at,updated_at=excluded.updated_at""",
            (job_id, str(job.get("status", "pending")), payload, created_at, updated_at),
        )


def _load_job(job_id: str):
    with _job_db_connect() as conn:
        row = conn.execute(
            "SELECT payload FROM background_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if not row:
        return None
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def _create_job(job_id: str, job: dict):
    with jobs_lock:
        jobs[job_id] = job
    _persist_job(job_id, job)


def _create_operation(operation_id: str, job_id: str, operation_type: str, now: float):
    with _operation_db_connect() as conn:
        conn.execute(
            """INSERT INTO operation_records(
                   operation_id,job_id,operation_type,status,error,recovery_status,
                   snapshot,reissue_result,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (operation_id, job_id, operation_type, "pending", None, "not_needed", None, None, now, now),
        )


def _update_operation(operation_id: str, **changes):
    allowed = {"status", "error", "recovery_status", "snapshot", "reissue_result", "updated_at"}
    values = {key: value for key, value in changes.items() if key in allowed}
    if not values:
        return
    values.setdefault("updated_at", time.time())
    assignments = ",".join(f"{key}=?" for key in values)
    with _operation_db_connect() as conn:
        conn.execute(
            f"UPDATE operation_records SET {assignments} WHERE operation_id=?",
            (*values.values(), operation_id),
        )


def _load_operation(operation_id: str):
    with _operation_db_connect() as conn:
        row = conn.execute(
            """SELECT operation_id,job_id,operation_type,status,error,recovery_status,
                      snapshot,reissue_result,created_at,updated_at
               FROM operation_records WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
    if not row:
        return None
    keys = ("operation_id", "job_id", "operation_type", "status", "error",
            "recovery_status", "snapshot", "reissue_result", "created_at", "updated_at")
    record = dict(zip(keys, row))
    if time.time() >= float(record["created_at"]) + OPERATION_RETENTION:
        with _operation_db_connect() as conn:
            conn.execute("DELETE FROM operation_records WHERE operation_id=?", (operation_id,))
        return None
    return record


def _close_operation(
    operation_id: str,
    *,
    status: str,
    recovery_status: str,
    error: str | None = None,
    issued_codes: tuple[str, str] | None = None,
):
    """Close an operation without making an already-issued ID disappear.

    Closed records contain no save snapshot. When a code was already issued, an
    encrypted copy is retained for the admin panel until the normal 48-hour
    cleanup. Keeping the row distinguishes an expired/unknown ID from an
    operation that never reached the recoverable stage.
    """
    if not operation_id:
        return
    encrypted_codes = None
    if issued_codes:
        try:
            encrypted_codes = _encrypt_snapshot(
                json.dumps(
                    {"tc": issued_codes[0], "ac": issued_codes[1]},
                    separators=(",", ":"),
                ).encode(),
                operation_id,
            )
        except Exception as exc:
            # Do not discard an existing save snapshot when the fallback code
            # itself cannot be stored. The admin panel can still reissue it.
            combined = _safe_error_message(
                f"{error or ''}\n発行済みコードの保管に失敗: {exc}"
            )
            _update_operation(
                operation_id,
                status="error",
                error=combined,
                recovery_status="reissue_failed",
            )
            return
    _update_operation(
        operation_id,
        status=status,
        error=_safe_error_message(error) if error else None,
        recovery_status=recovery_status,
        snapshot=None,
        reissue_result=encrypted_codes,
    )


def _operation_public(record: dict):
    status_labels = {
        "pending": "受付済み",
        "running": "処理中",
        "done": "完了",
        "error": "エラー",
    }
    recovery_labels = {
        "not_needed": "復旧不要",
        "not_available": "セーブ未取得のため再発行不可",
        "snapshot_saved": "管理者による再発行が可能",
        "reissue_failed": "再発行待ち",
        "issuing": "再発行中",
        "client_code_issued": "引き継ぎコード発行済み",
        "admin_reissued": "引き継ぎコード再発行済み",
    }
    return {
        "operation_id": record["operation_id"],
        "operation_type": record["operation_type"],
        "status": record["status"],
        "error": record.get("error"),
        "recovery_status": record.get("recovery_status", "not_needed"),
        "status_label": status_labels.get(record["status"], record["status"]),
        "recovery_status_label": recovery_labels.get(
            record.get("recovery_status", "not_needed"),
            record.get("recovery_status", "not_needed"),
        ),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "can_reissue": bool(
            record.get("snapshot")
            and record.get("status") == "error"
            and record.get("recovery_status") in ("snapshot_saved", "reissue_failed")
        ),
        "expires_at": record["created_at"] + OPERATION_RETENTION,
    }


def _snapshot_cipher_key() -> bytes:
    # 専用鍵を優先。未設定時は複数ワーカーで同じFLASK_SECRET_KEYから導出する。
    source = ADMIN_SNAPSHOT_KEY or os.getenv("FLASK_SECRET_KEY", "")
    if not source:
        raise RuntimeError("ADMIN_SNAPSHOT_KEY または FLASK_SECRET_KEY が必要です")
    return hashlib.sha256((source + "|autocat-admin-snapshot-v1").encode()).digest()


def _encrypt_snapshot(data: bytes, operation_id: str) -> bytes:
    key = _snapshot_cipher_key()
    nonce = secrets.token_bytes(12)
    aad = ("autocat-admin-snapshot-v2|" + operation_id).encode()
    return b"ACS2" + nonce + AESGCM(key).encrypt(nonce, data, aad)


def _decrypt_snapshot(blob: bytes, operation_id: str) -> bytes:
    if not blob or not blob.startswith(b"ACS2") or len(blob) < 32:
        raise ValueError("保存データが不正です")
    key = _snapshot_cipher_key()
    nonce, ciphertext = blob[4:16], blob[16:]
    aad = ("autocat-admin-snapshot-v2|" + operation_id).encode()
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:
        raise ValueError("保存データの検証に失敗しました") from exc


def _save_operation_snapshot(operation_id: str, save_file):
    raw = save_file.to_data().to_bytes()
    snapshot = _encrypt_snapshot(gzip.compress(raw, compresslevel=6), operation_id)
    _update_operation(operation_id, snapshot=snapshot, recovery_status="snapshot_saved")


def _generate_operation_id() -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(30))


def _safe_operation_id(data: dict) -> str:
    value = str(data.get("operation_id", "")).strip()
    if not value:
        return _generate_operation_id()
    return value if OPERATION_ID_RE.fullmatch(value) else ""


def _safe_error_message(value) -> str:
    text = str(value or "処理に失敗しました。")[:2000]
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1***:***@", text)
    text = re.sub(r"(?i)(authorization|token|secret|password)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    return text


def _update_job(job_id: str, changes: dict):
    """Update the local cache and durable shared job record together."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            job = _load_job(job_id)
            if job is None:
                return
            jobs[job_id] = job
        job.update(changes)
        snapshot = copy.deepcopy(job)
    _persist_job(job_id, snapshot)
    _settle_job_quota(snapshot)


def _settle_job_quota(job: dict | None) -> None:
    """無料回数とVIP体験1回を、成功時確定・失敗時返却する。"""
    if not isinstance(job, dict):
        return
    status = job.get("status")
    if status not in {"done", "error"}:
        return
    success = status == "done"
    reservation_id = job.get("quota_reservation_id")
    if reservation_id:
        try:
            free_usage_manager.settle(str(reservation_id), success=success)
            _process_invitation_vip_trials()
        except Exception as exc:
            # ジョブ結果は失わせず、次回の状態確認でも冪等に再試行する。
            print(f"[ERROR] free quota settlement {reservation_id}: {type(exc).__name__}")
    trial_reservation_id = job.get("trial_vip_reservation_id")
    if trial_reservation_id:
        try:
            account_store.settle_vip_job(str(trial_reservation_id), success=success)
        except Exception as exc:
            print(f"[ERROR] VIP trial settlement {trial_reservation_id}: {type(exc).__name__}")


def get_usage_count() -> int:
    with usage_count_lock:
        row = _usage_db_conn.execute("SELECT count FROM usage_count WHERE id = 1").fetchone()
        return row[0] if row else 0


def increment_usage_count() -> int:
    with usage_count_lock:
        _usage_db_conn.execute("UPDATE usage_count SET count = count + 1 WHERE id = 1")
        _usage_db_conn.commit()
        row = _usage_db_conn.execute("SELECT count FROM usage_count WHERE id = 1").fetchone()
        return row[0] if row else 0


def count_inflight_jobs() -> int:
    with _job_db_connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE status IN ('pending','running')"
        ).fetchone()
    return int(row[0]) if row else 0


def cleanup_jobs():
    while True:
        time.sleep(30)
        now = time.time()
        with _job_db_connect() as conn:
            rows = conn.execute(
                "SELECT job_id,payload,updated_at FROM background_jobs "
                "WHERE status IN ('pending','running')"
            ).fetchall()
        for jid, raw_payload, stored_updated_at in rows:
            try:
                job = json.loads(raw_payload)
            except (TypeError, ValueError):
                continue
            created = float(job.get("created_at", now))
            last_activity = float(job.get("updated_at") or job.get("started_at") or created)
            absolute_timeout = (
                CLONE_JOB_ABSOLUTE_TIMEOUT
                if job.get("operation_type") == "clone"
                else JOB_ABSOLUTE_TIMEOUT
            )
            inactivity_timeout = (
                CLONE_JOB_TIMEOUT
                if job.get("operation_type") == "clone"
                else JOB_TIMEOUT
            )
            if now - last_activity > inactivity_timeout or now - created > absolute_timeout:
                job.update({"status": "error", "updated_at": now})
                if job.get("transfer_received"):
                    job["error"] = (
                        "引き継ぎ取得後に処理がタイムアウトしました。"
                        "同じ操作を再実行せず、管理者へ確認してください。"
                    )
                else:
                    job["error"] = "処理がタイムアウトしました。もう一度実行してください。"
                # 読み取り後にワーカーが進捗更新した場合はタイムアウトで上書きしない。
                with _job_db_connect() as conn:
                    cur = conn.execute(
                        "UPDATE background_jobs SET status=?,payload=?,updated_at=? "
                        "WHERE job_id=? AND updated_at=?",
                        ("error", json.dumps(job, ensure_ascii=False, separators=(",", ":")),
                         now, jid, stored_updated_at),
                    )
                if cur.rowcount:
                    with jobs_lock:
                        jobs[jid] = job
                    _settle_job_quota(job)
                    operation_id = job.get("operation_id")
                    if operation_id:
                        if job.get("return_pending"):
                            job["admin_recovery_required"] = True
                            _update_job(jid, {
                                "admin_recovery_required": True,
                                "return_pending": False,
                            })
                            _update_operation(
                                operation_id, status="error", error=job["error"],
                                recovery_status="reissue_failed",
                            )
                        elif job.get("recovery_transfer_code"):
                            # A usable return code wins over a stale timeout flag.
                            # Clear the flag and retain a closed record so history
                            # never points at an immediately deleted operation.
                            job["admin_recovery_required"] = False
                            _update_job(jid, {"admin_recovery_required": False})
                            _close_operation(
                                operation_id,
                                status="error",
                                recovery_status="client_code_issued",
                                error=job["error"],
                                issued_codes=(
                                    job["recovery_transfer_code"],
                                    job.get("recovery_auth_code") or "",
                                ),
                            )
                        else:
                            job["admin_recovery_required"] = True
                            _update_job(jid, {"admin_recovery_required": True})
                            operation = _load_operation(operation_id)
                            recovery_state = (
                                operation.get("recovery_status", "reissue_failed")
                                if operation and operation.get("snapshot") else "reissue_failed"
                            )
                            _update_operation(
                                operation_id, status="error", error=job["error"],
                                recovery_status=recovery_state,
                            )

        cutoff = now - JOBS_TTL
        with _job_db_connect() as conn:
            conn.execute(
                "DELETE FROM background_jobs WHERE status IN ('done','error') AND created_at < ?",
                (cutoff,),
            )
            keep_ids = {
                row[0] for row in conn.execute(
                    "SELECT job_id FROM background_jobs ORDER BY created_at DESC LIMIT ?", (JOBS_MAX,)
                ).fetchall()
            }
            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(
                    f"DELETE FROM background_jobs WHERE job_id NOT IN ({placeholders})",
                    tuple(keep_ids),
                )
        with jobs_lock:
            for jid in list(jobs):
                if jid not in keep_ids:
                    jobs.pop(jid, None)
        # 返却コード発行まで失敗した操作だけを残し、作成から48時間で削除。
        with _operation_db_connect() as conn:
            conn.execute(
                "DELETE FROM operation_records WHERE created_at < ?",
                (now - OPERATION_RETENTION,),
            )


threading.Thread(target=cleanup_jobs, daemon=True).start()


def register_job_to_session(job_id: str):
    # FlaskのCookieセッションを肥大化させるとブラウザがCookieを破棄し、
    # 以後の/api/jobが全て403になる。新規UIはjob tokenを使い、ここは
    # 旧UI互換用として直近20件だけ保持する。
    existing = [value for value in session.get("job_ids", []) if isinstance(value, str)]
    session["job_ids"] = (existing + [job_id])[-20:]


def _new_job_access_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def session_owns_job(job_id: str, job: dict | None = None) -> bool:
    # 署名Cookie内のjob_idsは複数タブの同時レスポンスで片方が欠落し得る。
    # ジョブごとのCapability tokenを優先し、旧ジョブだけ従来方式へ戻す。
    if job is None:
        with jobs_lock:
            job = copy.deepcopy(jobs.get(job_id))
        if job is None:
            job = _load_job(job_id)
    expected = str((job or {}).get("job_access_hash") or "")
    supplied = request.headers.get("X-Job-Token", "")
    if expected and supplied:
        actual = hashlib.sha256(supplied.encode()).hexdigest()
        return secrets.compare_digest(actual, expected)
    return job_id in session.get("job_ids", [])


# =====================
# Discordログイン（チャット・管理者）/ サイトアカウント（VIP）
# =====================
def current_user():
    """Discordログイン中のチャット用ユーザー。VIP判定には使用しない。"""
    return session.get("discord_user")


def current_site_user():
    """サイトアカウント。VIP権限はACCOUNT/account.dbの契約状態から毎回取得する。"""
    user = account_store.get_account(session.get("site_account_id"))
    if user and user.get("status") == "active":
        return user
    session.pop("site_account_id", None)
    return None


def site_vip_confirmed() -> bool:
    user = current_site_user()
    return bool(user and user.get("is_vip"))


def is_admin_user() -> bool:
    user = current_user()
    configured = ADMIN_USER
    if not re.fullmatch(r"\d{15,22}", configured) or not user or not user.get("id"):
        return False
    return secrets.compare_digest(str(user["id"]), configured)


def admin_api_required(view):
    @_ft.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin_user():
            return jsonify({"error": "forbidden"}), 403
        return view(*args, **kwargs)
    return wrapped


def _admin_csrf_token() -> str:
    token = session.get("admin_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["admin_csrf_token"] = token
    return token


def _valid_admin_csrf() -> bool:
    expected = session.get("admin_csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


@app.route("/auth/login")
@limiter.limit("20 per minute")
def auth_login():
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        print("[ERROR] Discord OAuth設定が不足しています")
        return redirect("/?login_error=config")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    oauth_next = request.args.get("next", "/chat")
    allowed_next = {"/", "/chat", "/chat/admin", "/admin/panel", "/admin/vip"}
    session["oauth_next"] = oauth_next if oauth_next in allowed_next else "/chat"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
    }
    return redirect(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")


@app.route("/auth/callback")
@limiter.limit("20 per minute")
def auth_callback():
    state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", "")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        print("[ERROR] Discord OAuth stateが一致しません")
        return redirect("/?login_error=state")

    oauth_error = request.args.get("error")
    if oauth_error:
        print(f"[ERROR] Discord OAuth denied: {oauth_error}")
        return redirect("/?login_error=denied")

    code = request.args.get("code")
    if not code:
        return redirect("/?login_error=code")

    try:
        token_res = requests.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
            proxies=PROXIES,
        )
        if token_res.status_code != 200:
            print(f"[ERROR] Discord token exchange: HTTP {token_res.status_code}")
            return redirect("/?login_error=token")

        access_token = token_res.json().get("access_token")
        if not access_token:
            print("[ERROR] Discord token exchange: access_tokenがありません")
            return redirect("/?login_error=token")

        user_res = requests.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
            proxies=PROXIES,
        )
        if user_res.status_code != 200:
            print(f"[ERROR] Discord user fetch: HTTP {user_res.status_code}")
            return redirect("/?login_error=user")

        u = user_res.json()
        discord_id = u["id"]

        session.permanent = True
        session["discord_user"] = {
            "id": discord_id,
            "username": u.get("username"),
            "avatar": u.get("avatar"),
            # DiscordロールはVIP判定に使用しない。チャット互換用に常にFalseを保持する。
            "is_vip": False,
        }
    except Exception as e:
        print(f"[ERROR] auth_callback: {e}")
        return redirect("/?login_error=network")

    return redirect(session.pop("oauth_next", "/chat"))


@app.route("/auth/logout")
@limiter.limit("20 per minute")
def auth_logout():
    session.pop("discord_user", None)
    return redirect("/chat" if request.args.get("next") == "/chat" else "/")


@app.route("/auth/status")
@limiter.limit("60 per minute")
def auth_status():
    """現在のブラウザでDiscordセッションが共有されているか確認する。"""
    user = current_user()
    user_id = str((user or {}).get("id") or "")
    chat_admin_ids = {
        value.strip() for value in os.getenv("CHAT_ADMIN_DISCORD_IDS", "").split(",")
        if value.strip().isdigit()
    }
    response = jsonify({
        "logged_in": bool(user_id),
        "discord_id": user_id or None,
        "username": (user or {}).get("username"),
        "is_admin": is_admin_user(),
        "is_chat_admin": user_id in chat_admin_ids,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


# =====================
# 入力バリデーション
# =====================
def validate_common_input(data):
    """selected / char_list / custom_playtime の型・サイズを検証。問題なければ None、あればエラーレスポンス。"""
    if not isinstance(data, dict):
        return jsonify({"error": "入力が不正です"}), 400
    selected = data.get("selected", {})
    if not isinstance(selected, dict):
        return jsonify({"error": "selected が不正です"}), 400
    for v in selected.values():
        if not isinstance(v, list) or len(v) > 100:
            return jsonify({"error": "selected が不正です"}), 400
    char_list = data.get("char_list", [])
    if not isinstance(char_list, list) or len(char_list) > MAX_CHAR_LIST:
        return jsonify({"error": "char_list が不正です"}), 400
    character_settings = data.get("character_settings", [])
    if not isinstance(character_settings, list) or len(character_settings) > MAX_CHARACTER_SETTINGS:
        return jsonify({"error": "character_settings が不正です"}), 400
    for setting in character_settings:
        if not isinstance(setting, dict):
            return jsonify({"error": "character_settings が不正です"}), 400
        talents = setting.get("talents", {})
        if not isinstance(talents, dict) or len(talents) > 32:
            return jsonify({"error": "キャラ本能設定が不正です"}), 400

    ototo_settings = data.get("ototo_settings")
    if ototo_settings is not None:
        if not isinstance(ototo_settings, dict) or len(ototo_settings) > 64:
            return jsonify({"error": "オトート詳細設定が不正です"}), 400
        for cannon_id, setting in ototo_settings.items():
            if not str(cannon_id).isdigit() or not isinstance(setting, dict):
                return jsonify({"error": "オトート詳細設定が不正です"}), 400
            levels = setting.get("levels", {})
            if not isinstance(levels, dict) or len(levels) > 3:
                return jsonify({"error": "オトート詳細設定が不正です"}), 400
            if any(not str(part_id).isdigit() for part_id in levels):
                return jsonify({"error": "オトート詳細設定が不正です"}), 400

    vip_items = data.get("vip_items", {})
    if not isinstance(vip_items, dict) or len(vip_items) > len(VIP_ITEM_GROUPS):
        return jsonify({"error": "vip_items が不正です"}), 400
    for group_key, values in vip_items.items():
        spec = VIP_ITEM_GROUPS.get(group_key)
        if spec is None or not isinstance(values, dict) or len(values) > spec["length"]:
            return jsonify({"error": "vip_items が不正です"}), 400

    if not validate_legend_stages_shape(data):
        return jsonify({"error": "legend_stages が不正です"}), 400
    if not validate_detailed_stage_settings_shape(data):
        return jsonify({"error": "ステージ詳細指定が不正です"}), 400
    if not validate_labyrinth_character_settings_shape(data):
        return jsonify({"error": "地底迷宮のキャラ指定が不正です"}), 400
    if not validate_lineup_settings_shape(data):
        return jsonify({"error": "編成キャラ指定が不正です"}), 400
    if not validate_score_settings_shape(data):
        return jsonify({"error": "道場・未来編スコア指定が不正です"}), 400

    vip_facilities = data.get("vip_facilities", {})
    if not isinstance(vip_facilities, dict) or len(vip_facilities) > len(FACILITY_NAMES):
        return jsonify({"error": "vip_facilities が不正です"}), 400
    vip_talent_orbs = data.get("vip_talent_orbs", {})
    if not isinstance(vip_talent_orbs, dict) or len(vip_talent_orbs) > 1000:
        return jsonify({"error": "vip_talent_orbs が不正です"}), 400

    # プレイ時間(絶対値指定): 値が入っている場合は「時間:分」形式のみ許可
    playtime_raw = data.get("custom_playtime")
    if playtime_raw not in (None, ""):
        if validate_playtime_str(str(playtime_raw)) is None:
            return jsonify({"error": "プレイ時間は「時間:分」の形式で入力してください（例: 1200:30）"}), 400

    return None


def safe_count(data, maximum=MAX_COUNT):
    maximum = max(1, min(MAX_COUNT, int(maximum)))
    try:
        return max(1, min(maximum, int(data.get("count", 1))))
    except (TypeError, ValueError):
        return 1


def safe_character_settings(data):
    """名前選択UIから届いたキャラ・形態・レベル設定を安全な範囲へ丸める。IDは画面表示と同じ1始まり。"""
    out = []
    seen = set()
    try:
        form_counts = {
            character["id"]: character["form_count"]
            for character in get_character_metadata().get("characters", [])
            if character.get("selectable", True)
        }
    except Exception:
        form_counts = {}
    for raw in data.get("character_settings", [])[:MAX_CHARACTER_SETTINGS]:
        try:
            cat_id = max(1, min(9999, int(raw.get("id"))))
            if form_counts and cat_id not in form_counts:
                continue
            max_form = form_counts.get(cat_id, 4)
            form = max(1, min(max_form, int(raw.get("form", 1))))
            base_level = max(1, min(60, int(raw.get("base_level", 60))))
            plus_level = max(0, min(90, int(raw.get("plus_level", 0))))
            level_mode = raw.get("level_mode", "individual")
            if level_mode not in {"same", "individual", "random"}:
                level_mode = "individual"
            base_min = max(1, min(60, int(raw.get("base_level_min", 1))))
            base_max = max(1, min(60, int(raw.get("base_level_max", 60))))
            plus_min = max(0, min(90, int(raw.get("plus_level_min", 0))))
            plus_max = max(0, min(90, int(raw.get("plus_level_max", 90))))
        except (TypeError, ValueError, AttributeError):
            continue
        base_min, base_max = sorted((base_min, base_max))
        plus_min, plus_max = sorted((plus_min, plus_max))
        if cat_id in seen:
            continue
        seen.add(cat_id)
        out.append({
            "id": cat_id,
            "form": form,
            "base_level": base_level,
            "plus_level": plus_level,
            "level_mode": level_mode,
            "base_level_min": base_min,
            "base_level_max": base_max,
            "plus_level_min": plus_min,
            "plus_level_max": plus_max,
            "talent_action": (
                raw.get("talent_action")
                if raw.get("talent_action") in {"set", "disable_selected", "disable_all"}
                else "set"
            ),
            # 0は無効化。自然上限では丸めず、限界突破指定を保持する。
            "talents": {
                str(max(1, min(99999, int(talent_id)))): max(0, min(32767, int(level)))
                for talent_id, level in (raw.get("talents") or {}).items()
                if str(talent_id).lstrip("-").isdigit()
                and str(level).lstrip("-").isdigit()
            } if isinstance(raw.get("talents"), dict) else {},
        })
    return out


def safe_ototo_settings(data):
    """画面の表示レベルを、最新版で実在する城・部品・上限へ丸める。"""
    raw_settings = data.get("ototo_settings")
    if not isinstance(raw_settings, dict):
        return None
    try:
        cannons = get_ototo_metadata().get("cannons", [])
    except Exception:
        cannons = _bundled_ototo_metadata()["cannons"]
    definitions = {
        int(cannon["id"]): {
            int(part["id"]): (
                int(part.get("min_level", 0)),
                int(part.get("max_level", 0)),
            )
            for part in cannon.get("parts", [])
        }
        for cannon in cannons
    }
    output = {}
    for raw_cannon_id, raw_setting in list(raw_settings.items())[:64]:
        try:
            cannon_id = int(raw_cannon_id)
        except (TypeError, ValueError):
            continue
        if cannon_id not in definitions or not isinstance(raw_setting, dict):
            continue
        raw_levels = raw_setting.get("levels", {})
        if not isinstance(raw_levels, dict):
            continue
        levels = {}
        for raw_part_id, raw_level in list(raw_levels.items())[:3]:
            try:
                part_id = int(raw_part_id)
                minimum, maximum = definitions[cannon_id][part_id]
                level = max(minimum, min(maximum, int(raw_level)))
            except (TypeError, ValueError, KeyError):
                continue
            levels[str(part_id)] = level
        if levels:
            output[str(cannon_id)] = {"levels": levels}
    return output or None


# アカウント種別 → セーブファイル名のマッピング（ホワイトリスト方式。任意の文字列をパスに使わせない）
ACCOUNT_TYPE_FILES = {
    "new": "Nyanko_new",
    "beginner": "Nyanko_beginner",
    "intermediate": "Nyanko_Intermediate",
    "advanced": "Nyanko_advanced",
}


def safe_account_type(data):
    """account_type を検証済みのキーとして返す。不正・未指定なら 'new'（初期垢）。"""
    raw = str(data.get("account_type", "new")).strip()
    return raw if raw in ACCOUNT_TYPE_FILES else "new"


def safe_custom_amounts(data):
    out = {}
    for k in CUSTOM_KEYS:
        try:
            val = int(data.get(k, 0))
        except (TypeError, ValueError):
            val = 0
        # 極端な負値・過大値を丸める
        out[k] = max(0, min(val, 2_000_000_000))
    return out


# bcsfe 3.6.0 / JP 15.5.1。lengthは入力上限で、実際の有効数は
# 最新Gatyaitembuy metadataとセーブ配列長の両方で適用時に確認する。
VIP_ITEM_GROUPS = {
    "battle_items": {"length": 64, "max": 9999},
    "catseyes": {"length": 256, "max": 9999},
    "catamins": {"length": 256, "max": 9999},
    "base_materials": {"length": 256, "max": 9999},
    "catfruit": {"length": 512, "max": 998},
    "event_tickets": {"length": 512, "max": 9999},
    "labyrinth_medals": {"length": 4, "max": 32767},
}

VIP_ITEM_LABELS = {
    "battle_items": ["スピードアップ", "トレジャーレーダー", "ネコボン", "ニャンピュータ", "おかめはちもく", "スニャイパー"],
    "catseyes": ["キャッツアイ【EX】", "キャッツアイ【レア】", "キャッツアイ【激レア】", "キャッツアイ【超激レア】", "キャッツアイ【伝説】", "キャッツアイ【闇】"],
    "catamins": ["ネコビタンA", "ネコビタンB", "ネコビタンC"],
    "base_materials": [
        "レンガ", "羽根", "備長炭", "鋼の歯車", "黄金", "宇宙石", "謎の骨", "アンモナイト",
        "レンガZ", "羽根Z", "備長炭Z", "鋼の歯車Z", "黄金Z", "宇宙石Z", "謎の骨Z", "アンモナイトZ",
    ],
    "catfruit": [
        "紫マタタビの種", "赤マタタビの種", "青マタタビの種", "緑マタタビの種", "黄マタタビの種",
        "紫マタタビ", "赤マタタビ", "青マタタビ", "緑マタタビ", "黄マタタビ", "虹マタタビ",
        "古代マタタビの種", "古代マタタビ", "虹マタタビの種", "金マタタビ", "悪マタタビの種",
        "悪マタタビ", "金マタタビの種", "紫獣石", "紅獣石", "蒼獣石", "翠獣石", "黄獣石",
        "紫獣結晶", "紅獣結晶", "蒼獣結晶", "翠獣結晶", "黄獣結晶", "虹獣石",
    ],
    "labyrinth_medals": ["ブロンズ勲章", "シルバー勲章", "ゴールド勲章", "プラチナ勲章"],
}

VIP_ITEM_SELECTED_VALUES = {
    "battle_items": {"battle_items", "custom_battle_items"},
    "catseyes": {"catseyes", "custom_catseyes"},
    "catamins": {"catamins", "custom_catamins"},
    "base_materials": {"base_materials", "custom_base_materials"},
    "catfruit": {"matatabi", "custom_matatabi"},
    "event_tickets": {"event_tickets", "custom_event_tickets"},
    "labyrinth_medals": {"labyrinth_medals"},
}


def safe_vip_items(data):
    """VIP個別アイテムを {group: {index: amount}} に正規化する。"""
    raw_groups = data.get("vip_items", {})
    if not isinstance(raw_groups, dict):
        return {}
    result = {}
    try:
        dynamic_groups = get_legend_metadata().get("vip_item_groups", {})
    except Exception as e:
        print(f"[WARN] vip item metadata fallback: {e}")
        dynamic_groups = {}
    for group_key, spec in VIP_ITEM_GROUPS.items():
        raw_values = raw_groups.get(group_key, {})
        if not isinstance(raw_values, dict):
            continue
        values = {}
        for raw_index, raw_amount in raw_values.items():
            try:
                index = int(raw_index)
                amount = int(raw_amount)
            except (TypeError, ValueError):
                continue
            labels = dynamic_groups.get(group_key) or VIP_ITEM_LABELS.get(group_key, [])
            valid_length = min(spec["length"], len(labels)) if labels else spec["length"]
            if 0 <= index < valid_length:
                values[index] = max(0, min(amount, spec["max"]))
        # 空dictも保持する。これは「親項目は選択済みだが個別項目は全解除」を表す。
        if group_key in raw_groups:
            result[group_key] = values
    if "event_tickets" in raw_groups:
        raw_values = raw_groups.get("event_tickets", {})
        values = {}
        try:
            valid_ids = {item["id"] for item in get_legend_metadata().get("event_tickets", [])}
        except Exception as e:
            print(f"[ERROR] event ticket metadata: {e}")
            valid_ids = set()
        if isinstance(raw_values, dict):
            for ticket_id, raw_amount in raw_values.items():
                try:
                    amount = int(raw_amount)
                except (TypeError, ValueError):
                    continue
                if ticket_id in valid_ids:
                    values[ticket_id] = max(0, min(amount, 9999))
        result["event_tickets"] = values
    return result


def apply_vip_items(save, vip_items):
    """bcsfeの保存配列に、選択された種類だけを書き込む。"""
    logs = []
    dynamic_labels = {}
    if vip_items:
        try:
            dynamic_labels = get_legend_metadata().get("vip_item_groups", {})
        except Exception:
            dynamic_labels = {}
    targets = {
        "battle_items": getattr(getattr(save, "battle_items", None), "items", []),
        "catseyes": getattr(save, "catseyes", []),
        "catamins": getattr(save, "catamins", []),
        "base_materials": getattr(getattr(getattr(save, "ototo", None), "base_materials", None), "materials", []),
        "catfruit": getattr(save, "catfruit", []),
        "labyrinth_medals": getattr(save, "labyrinth_medals", []),
    }
    for group_key, values in (vip_items or {}).items():
        if group_key == "event_tickets":
            arrays = {
                1: getattr(save, "event_capsules", []),
                8: getattr(save, "lucky_tickets", []),
                10: getattr(save, "event_capsules_2", []),
            }
            for ticket_id, amount in values.items():
                try:
                    category, index = map(int, ticket_id.split(":"))
                    target = arrays.get(category, [])
                    if 0 <= index < len(target):
                        target[index] = amount
                        logs.append(f"イベントチケット[{ticket_id}]({amount})")
                except (TypeError, ValueError, IndexError) as e:
                    print(f"[ERROR] event ticket {ticket_id}: {e}")
            continue
        target = targets.get(group_key)
        labels = dynamic_labels.get(group_key) or VIP_ITEM_LABELS.get(group_key, [])
        if target is None:
            continue
        if group_key == "labyrinth_medals" and len(target) < 4:
            target.extend([0] * (4 - len(target)))
        for index, amount in values.items():
            if not 0 <= index < len(target):
                continue
            try:
                entry = target[index]
                if hasattr(entry, "amount"):
                    entry.amount = amount
                else:
                    target[index] = amount
                label = labels[index] if index < len(labels) else f"{group_key}[{index}]"
                logs.append(f"{label}({amount})")
            except Exception as e:
                print(f"[ERROR] vip_items {group_key}[{index}]: {e}")
    return logs


ERROR_CAT_IDS = [156, 183, 286, 321, 340, 354, 433, 434, 466, 493, 498, 499, 500, 501,
                 741, 742, 743, 744, 745, 746, 789, 674]


def update_web_data():
    global ERROR_CAT_IDS
    while True:
        try:
            r = requests.get(
                "https://battlecats-db.com/unit/r_all.html",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
                proxies=PROXIES,
            )
            r.encoding = r.apparent_encoding
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                no_list = []
                table = soup.find("table")
                if table:
                    for tr in table.find_all("tr"):
                        cells = tr.find_all("td")
                        if len(cells) >= 10:
                            no_text = cells[0].get_text(strip=True)
                            if no_text.isdigit():
                                no_list.append(int(no_text))
                if no_list:
                    ERROR_CAT_IDS = sorted(
                        list(set(range(1, max(no_list) + 1)) - set(no_list))
                        + [674]
                    )
        except Exception as e:
            print(f"[ERROR] update_web_data: {e}")
        time.sleep(60 * 60 * 24)


if os.getenv("DISABLE_BACKGROUND_UPDATER", "0") != "1":
    _updater_thread = threading.Thread(target=update_web_data, daemon=True)
    _updater_thread.start()


CUSTOM_KEYS = [
    "custom_catfood", "custom_xp", "custom_np",
    "custom_normal_tickets", "custom_rare_tickets",
    "custom_platinum_tickets", "custom_legend_tickets",
    "custom_leadership", "custom_battle_items", "custom_catamins",
    "custom_catseyes", "custom_base_materials", "custom_matatabi",
    "custom_talent_orbs", "custom_base_upgrades", "custom_event_tickets",
]


# メインステージの章ラベル（表示順=ポジション0〜8に対応。VIP限定の章選択機能で使用）
MAIN_STORY_CHAPTER_LABELS = [
    "日本編1章", "日本編2章", "日本編3章",
    "未来編1章", "未来編2章", "未来編3章",
    "宇宙編1章", "宇宙編2章", "宇宙編3章",
]

# 表示上のポジション(0〜8) → save.story.chapters の実インデックスへの変換テーブル。
# ゾンビ編と同様に実データ上ではインデックス3が欠番のため、日本編0,1,2 → 未来編4,5,6 → 宇宙編7,8,9 とスキップする。
MAIN_STORY_REAL_INDEX = [0, 1, 2, 4, 5, 6, 7, 8, 9]


# レジェンド系の表示名はbcsfeのゲームデータから取得する。セーブ配列の種類と
# Map_Name / StageName のコードを同じ表で管理し、画面と適用処理のずれを防ぐ。
LEGEND_SERIES = {
    "legend": {"label": "レジェンドストーリー", "code": "N", "base_index": 0},
    "true_legend": {"label": "真レジェンドストーリー", "code": "NA", "base_index": 13000},
    "zero_legend": {"label": "零レジェンドストーリー", "code": "ND", "base_index": 34000},
}
LEGEND_SELECTED_VALUES = {
    "legend": "legend_clear",
    "true_legend": "true_legend_clear",
    "zero_legend": "zero_legend_clear",
}
EVENT_STAGE_FAMILIES = {
    "event": {"label": "イベントステージ", "code": "S", "base_index": 1000, "kind": "event", "group": 1},
    "collab": {"label": "コラボステージ", "code": "C", "base_index": 2000, "kind": "event", "group": 2},
    "dojo_ranking": {"label": "ランキングの間", "code": "R", "base_index": 6000, "kind": "event", "group": 3},
    "tower": {"label": "にゃんこ塔・異界にゃんこ塔", "code": "V", "base_index": 7000, "kind": "tower"},
    "catamin": {"label": "ネコビタンステージ", "code": "B", "base_index": 14000, "kind": "catamin_stages"},
    "legend_quest": {"label": "レジェンドクエスト", "code": "D", "base_index": 16000, "kind": "legend_quest"},
    "gauntlets": {"label": "強襲ステージ（各月・夏休み系を含む）", "code": "A", "base_index": 24000, "kind": "gauntlets"},
    "enigma": {"label": "発掘・地図ステージ", "code": "H", "base_index": 25000, "kind": "enigma_clears"},
    "collab_gauntlets": {"label": "コラボ強襲", "code": "CA", "base_index": 27000, "kind": "collab_gauntlets"},
    "behemoth": {"label": "超獣討伐ステージ", "code": "Q", "base_index": 31000, "kind": "behemoth_culling"},
    "labyrinth": {
        "label": "地底迷宮",
        "code": "L",
        "base_index": 33000,
        "kind": "labyrinth",
        "stage_file": "StageName_L_ja.csv",
        "uses_clear_count": False,
    },
    "colosseum": {"label": "異次元コロシアム", "code": "SR", "base_index": 36000, "kind": "event", "group": 4},
    "catclaw": {"label": "にゃんこ道検定", "code": "G", "base_index": 37000, "kind": "dojo_chapters", "stage_file": "StageName_G_ja.csv"},
}
MAX_STAGE_CLEAR_COUNT = 32767
MAX_DETAILED_STAGE_SELECTIONS = 50000
MAX_LABYRINTH_CHARACTERS = 10000
LABYRINTH_CHARACTER_ACTIONS = {
    "unlock_selected", "unlock_all", "seal_selected", "seal_all",
    "release_lineup_all",
}
_legend_metadata_cache = None
_legend_metadata_cached_at = 0.0
_legend_metadata_lock = threading.Lock()
LEGEND_METADATA_TTL = 15 * 60
_character_metadata_cache = None
_character_metadata_cached_at = 0.0
_character_metadata_lock = threading.Lock()
CHARACTER_METADATA_TTL = 15 * 60
_ototo_metadata_cache = None
_ototo_metadata_cached_at = 0.0
_ototo_metadata_lock = threading.Lock()
OTOTO_METADATA_TTL = 15 * 60

# GatyaData_Option_SetR.tsv の seriesID に対応する表示名。未登録の新規系列は
# ID付きの名称で表示し、データ更新で画面全体が使えなくならないようにする。
GACHA_SERIES_NAMES = {
    0: "伝説のネコルガ族", 1: "超激ダイナマイツ", 2: "戦国武神バサラーズ",
    3: "電脳学園ギャラクシーギャルズ", 4: "超破壊大帝ドラゴンエンペラーズ",
    5: "レッドバスターズ", 6: "超古代勇者ウルトラソウルズ",
    7: "逆襲の英雄ダークヒーローズ", 8: "ハロウィンガチャ",
    9: "クリスマスギャルズ", 11: "「ゆるドラシル」コラボガチャ",
    13: "「メルクストーリア」コラボガチャ", 14: "「生きろ！マンボウ！」コラボガチャ",
    15: "「消滅都市」コラボガチャ", 16: "新年ガチャ",
    17: "「ケリ姫スイーツ」コラボガチャ", 18: "究極降臨ギガントゼウス",
    19: "超ネコ祭", 21: "プラチナガチャ", 22: "エアバスターズ",
    23: "「魔法少女まどか☆マギカ」コラボガチャ",
    24: "革命軍隊アイアンウォーズ", 26: "イースターカーニバル",
    27: "極ネコ祭", 28: "絶命美少女ギャルズモンスターズ",
    32: "メタルバスターズ", 33: "大精霊エレメンタルピクシーズ",
    34: "劇場版「Fate/stay night [Heaven's Feel]」コラボガチャ",
    35: "超選抜祭", 37: "「エヴァンゲリオン」コラボガチャ",
    38: "「ビックリマン」コラボガチャ", 39: "極選抜祭", 42: "超極ネコ祭",
    43: "「初音ミク」コラボガチャ", 44: "「エヴァンゲリオン」コラボガチャ 2nd",
    45: "波動バスターズ", 46: "レジェンドガチャ", 47: "超国王祭",
    48: "バレンタインギャルズ", 49: "「らんま1/2」コラボガチャ",
    50: "女王祭", 52: "ホワイトデーガチャ", 53: "ジューンブライドガチャ",
    54: "「ストリートファイター6」コラボガチャ BLUE TEAM",
    55: "「ストリートファイター6」コラボガチャ RED TEAM",
    56: "超生命体バスターズ", 57: "熱血！大運動会 赤組",
    58: "熱血！大運動会 白組", 59: "バスターズ祭",
    60: "「メタルスラッグアタック」コラボガチャ",
    63: "「るろうに剣心 -明治剣客浪漫譚-」コラボガチャ",
    64: "サマーガールズ サンシャイン", 65: "サマーガールズ ブルーオーシャン",
    66: "ウルトラ4セレクション", 67: "ミラクル4セレクション",
    68: "エクセレント4セレクション", 69: "1億DL記念ガチャ",
    70: "DL記念選抜ガチャ", 71: "アウトレットガチャ",
    72: "「範馬刃牙」コラボガチャ", 73: "「ソニック・ザ・ヘッジホッグ」コラボガチャ",
    74: "アニメ「鬼滅の刃」コラボガチャ", 75: "熱血！大運動会 黒組",
}

# JP 15.6.0 の有効なガチャ系列と所属キャラID。最新版取得に失敗した時も
# コラボ・ガチャ絞り込みを継続できるよう、gzip+base64で同梱する。
BUNDLED_CHARACTER_GROUPS_B64 = (
    "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"
)
BUNDLED_CHARACTER_RARITIES_B64 = (
    "H4sIAGTjcmoC/41UW24EMQi7UD/W4Jxm1ftfo22GgGFmpQpNdpXwMhjer68p+Ld4CPdpv3L9cgv2Tcm5YXyeGhyWFucRxH29+U0scvoU3SLTSwfhC5GHjygHIQNXeTh5Hn0X7YrO7ZNxc060KL4jI2+qfmg+jw5GJtaq4g21Z41Ko3K0h9p2HN7yZNSh7BkaLjn48GvZLQ5UHvVgZqBeTaJSusnoFm//GN6eBZLTsTvecKuqD0skT/BBa3INYeNS956hCW9XyKW30tvfC/Zn+5atkgjt82qtUxc7VuIsbhbzLaJxzJbv2CYzSLGjTOaFazUeVkc9cbvMLG598rYhLlmJxts9G77DnGIHm38TbhTrajoh/H8lo3X6XVhauUKmhdnluak80SN77y2nQt1RQmJRtgNlglbUCFGvJdPUN4wJqjkXaG/etKxNXrd22Uqfas5gxxJvMze2/fYknhNY20e10ZDg+wcx0WfLzwYAAA=="
)
# JP 15.5.0 nyankoPictureBookData.csvの実装形態数。Unit_Explanationには
# 未実装形態用として直前の名前を複製した行があるため、名前の行数では判定しない。
# ID456（モモコ）だけは提供された15.5.1実機の進化前後セーブ差分で第3形態を確認済み。
BUNDLED_CHARACTER_FORM_COUNTS_B64 = (
    "H4sIAOdwfGoC/51T2w0DIQxbqD9x2Kbq/mtUpT0gsQOnKkLikdjOg6c//jF0O/lwzLVvw4NxWl+tRKqY4z0W/GknrZP75P+xFk4q4uuFRR8GG2T+rHsqwzab+vVOt5w4kaqTkVEyr/Ft9JK7A4mjum6hw0i+IDbWkpUxFs9hjHc6z/7m7NuYEK4IivlTWuOEcMWjsqgyKgFhqUgXNUXJq+qbK6xnD6W53PnW26kv6i/lieafAjFbLqu/5mHy97jQpqcOqbKZMffM+s5+67rD1mxhtAUlRxtF4QayFffx3Y6odiuPGsVfb+qOjK3PBgAA"
)
# unitbuy.csvのforce_true_form_levelから生成した、自然な第三形態取得状態。
# 0=レベル到達で自動進化、2=素材/報酬等で取得。JP 15.5.0・871体。
BUNDLED_CHARACTER_TRUE_FORM_STATES_B64 = (
    "H4sIAJh1fGoC/4s20EGHRqNwFI7CUTiC4GipNyxgLAD9s0vDzwYAAA=="
)
BUNDLED_TALENT_DATA_B64 = "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"
BUNDLED_CHARACTER_DATA_B64 = (
    "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"
    "TLAzHjQZ0TQTEYLP1cM3aJBRhyMoOGJVd7KgvW8QwD/sJEaIwr9LRoCzE/uK5DluDhGEDUYTXbzowaczWYBTBq+1URpiwXePxlae"
    "5+/iCYeO+sDKUHj26AHBpktHWOE5t7wUHG3CwQmTbV1ir60uJAWCRj3aEHxK0SXf2muOC3NdoDe1W3pLAnDFDrJay+2Jgllf//nf"
    "/7ffNs8Xk0BoadJrXHx+Uu97PJhZ1WcZC4l0/TYBeLuJCF60q5n/sen7lPRkcuAUpTmqY9gzWTe+7iNOXJK7ir+Mt9ioAZhCI4Zm"
    "8edOokFCKN5bHqriqcp6i/rHEXMrJuYIBE07/agnsSRGIK4ZMRJ71BhwbYiZXgx6Sp6jJ+YG+7kd6+GJ6yj8u2QEeLJfykMu3Bpk"
    "nidsHKvCDqITqlkIu+adY1J9l4oD524J7FfdJibaE0c/aOh0/Ig3ptfVqBJmJuz7VDsclCMeQhHsb96DRh9ZBNcHe9P8BVIViA5N"
    "uiYRKDQnAXZDK1W8TYwdABSzxuiVe6qnbf8ZgpMFBsV16rZQScCCGaSzEp9/MC/lOkHznIYCp36Xk1m73HYG2IJ3Kg3So3XAvceJ"
    "h9XPzYLv+1BgyeZ7uD6nvAeNTX0bCQTgndF+xQfGKUWrtjbl77uw4GrtPc3ll/B6J5a37EkLs06YcHGDyW60xs84i2fY1l+fKDd3"
    "z8mjH84T/BdZKOPwfw2EgrJTiEX8+DfC0h5y6qMP4YDNsUAu+j5ErwEP+myjH4vmlvrLLgr0b0b+7RI8ZztYLChBOzzeVPK1CWry"
    "gbCnPfJ08gga1XUioDVlT6vWT3QwYabPbj71mUIwNOPsOSVPAS+/4kb8OWFgBA+aE82Hl/rqGZ+Ij7WHqly763hyfgXTCrn8aPiH"
    "H3NK0qrlKsgdyfzk2uUX5ksk1BgyMOiEnBYePZBsmjcus3LtPVAcCB68pdzd8Avd1IrrTa9pBIJ2NGxfn2aCgdFwfRpiBi68x45A"
    "0NQk1N2v5fdrOfXwRT2xLcR/myko8HPP4Pa7eJbld2HB1ckr6XydpHbtYcHVyezkfLKkdu1hwbXPS/jG3zGh0ZsoMLPSlfmy8RZv"
    "o5B10rE1enOGIL7tP4ptYwp1dTjWR28KxdlHsUkzqqvDTe0XH6Bagm8fBa4Jef702v3gp1TVWFl54DS4lZT271ZnAJvRa2xXh3WI"
    "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"
    "mhrgsDjTHJk1L/Vv/vf/Ayvdy29ODAEA"
)
FACILITY_NAMES = [
    "にゃんこ砲攻撃力", "にゃんこ砲射程", "にゃんこ砲チャージ",
    "働きネコ仕事効率", "働きネコお財布", "お城体力",
    "研究力", "会計力", "勉強力", "統率力",
]


def _clean_stage_names(values):
    names = []
    for value in values or []:
        name = str(value).strip()
        if not name or name == "＠":
            break
        names.append(name)
    return names


def _latest_jp_game_data_getter():
    """BCSFEの配布一覧を数値版で比較し、JPの実在する最新版を返す。"""
    cc = core.CountryCode.from_code("jp")
    parsed_versions = list(core.GameDataGetter.get_downloaded_versions_region(cc))
    try:
        repo_metadata = core.GameDataGetter.get_metadata(show_alt=False) or {}
        versions = list(core.GameDataGetter.get_versions(repo_metadata).get(cc.get_code(), {}))
    except Exception as e:
        print(f"[WARN] game data version list: {e}")
        versions = []
    for version in versions:
        try:
            parsed_versions.append(core.GameVersion.from_string(version))
        except (TypeError, ValueError):
            continue
    if parsed_versions:
        latest = max(parsed_versions, key=lambda version: version.game_version)
        return core.GameDataGetter(cc, latest, do_print=False)
    # ネットワークもローカルデータもない時に、BCSFEのCLI用確認プロンプトを
    # Webワーカー上で開かないための空getter。各機能はNoneを受けて安全に失敗する。
    getter = object.__new__(core.GameDataGetter)
    getter.print = False
    getter.lang = core.core_data.config.get_str(core.ConfigKey.LOCALE)
    getter.real_cc = cc
    getter.cc = cc.get_cc_lang()
    getter.gv = core.GameVersion(TARGET_GAME_VERSION_NUMBER)
    getter.version = None
    getter.all_versions = None
    getter.url = None
    getter.filepath = None
    return getter


def _character_groups_from_series(series_members):
    """seriesIDごとのキャラ集合を画面用の安定した一覧へ変換する。"""
    groups = []
    for raw_series_id, raw_cat_ids in series_members.items():
        try:
            series_id = int(raw_series_id)
        except (TypeError, ValueError):
            continue
        cat_ids = sorted({
            max(1, min(9999, int(cat_id)))
            for cat_id in raw_cat_ids
            if str(cat_id).lstrip("-").isdigit() and 1 <= int(cat_id) <= 9999
        })
        if not cat_ids:
            continue
        name = GACHA_SERIES_NAMES.get(series_id, f"ガチャシリーズ #{series_id}")
        groups.append({
            "id": f"series_{series_id}",
            "series_id": series_id,
            "name": name,
            "kind": "collab" if "コラボ" in name else "gacha",
            "cat_ids": cat_ids,
        })
    return sorted(groups, key=lambda item: (item["kind"] != "collab", item["series_id"]))


def _bundled_character_groups():
    try:
        payload = json.loads(gzip.decompress(base64.b64decode(BUNDLED_CHARACTER_GROUPS_B64)))
    except (ValueError, OSError, gzip.BadGzipFile):
        return []
    return _character_groups_from_series(payload if isinstance(payload, dict) else {})


def _download_character_groups(getter):
    """最新版の有効ガチャから、コラボ・ガチャ系列ごとの所属キャラを作る。"""
    set_data = getter.download("DataLocal", "GatyaDataSetR1.csv")
    option_data = getter.download("DataLocal", "GatyaData_Option_SetR.tsv")
    if set_data is None or option_data is None:
        return _bundled_character_groups()

    sets = []
    for row in core.CSV(set_data, remove_empty=False).lines:
        members = []
        for field in row:
            cat_id = field.to_int()
            if cat_id < 0:
                break
            members.append(cat_id + 1)  # ゲームデータは0始まり、画面は1始まり。
        sets.append(members)

    series_members = {}
    lines = option_data.to_str().replace("\r\n", "\n").split("\n")
    for line in lines[1:]:
        columns = line.split("\t")
        if len(columns) < 6:
            continue
        try:
            set_id = int(columns[0])
            banner_enabled = int(columns[1])
            series_id = int(columns[5])
        except ValueError:
            continue
        if banner_enabled != 1 or not (0 <= set_id < len(sets)):
            continue
        series_members.setdefault(series_id, set()).update(sets[set_id])
    groups = _character_groups_from_series(series_members)
    return groups or _bundled_character_groups()


def _bundled_character_metadata():
    """通信失敗時に使う、同梱済みJPキャラ情報。"""
    try:
        payload = json.loads(gzip.decompress(base64.b64decode(BUNDLED_CHARACTER_DATA_B64)))
        rarities = json.loads(gzip.decompress(base64.b64decode(BUNDLED_CHARACTER_RARITIES_B64)))
        form_counts = json.loads(gzip.decompress(base64.b64decode(BUNDLED_CHARACTER_FORM_COUNTS_B64)))
        true_form_states = json.loads(gzip.decompress(base64.b64decode(BUNDLED_CHARACTER_TRUE_FORM_STATES_B64)))
        bundled_talents = json.loads(gzip.decompress(base64.b64decode(BUNDLED_TALENT_DATA_B64)))
    except (ValueError, OSError, gzip.BadGzipFile) as e:
        raise RuntimeError("内蔵キャラ名データを読み込めません") from e

    rarity_names = ["基本キャラ", "EX", "レア", "激レア", "超激レア", "伝説レア"]
    characters = []
    for display_id, raw_names in payload.get("characters", []):
        display_id = int(display_id)
        names = [str(name).strip() for name in raw_names if str(name).strip()]
        if not names:
            continue
        rarity = rarities[display_id - 1] if display_id - 1 < len(rarities) else -1
        form_count = form_counts[display_id - 1] if display_id - 1 < len(form_counts) else 1
        # ユーザー提供のJP 15.5.1実機セーブで実装を確認した公開データ差分。
        # 自然な第三形態取得状態はunitbuy由来の0/2を別metadataで保持する。
        if display_id == 456:
            form_count = max(form_count, 3)
        form_count = max(1, min(4, int(form_count)))
        names = names[:form_count]
        while len(names) < form_count:
            names.append(names[-1] if names else f"No.{display_id} 第{len(names) + 1}形態")
        selectable = display_id not in ERROR_CAT_IDS and not all(
            re.fullmatch(r"\d+[-_]\d+", name) for name in names
        )
        characters.append({
            "id": display_id,
            "names": names,
            "form_count": form_count,
            "selectable": selectable,
            "true_form_state": (
                int(true_form_states[display_id - 1])
                if display_id - 1 < len(true_form_states) else 2
            ),
            "rarity": rarity,
            "rarity_name": rarity_names[rarity] if 0 <= rarity < len(rarity_names) else "不明",
            "talents": bundled_talents.get(str(display_id), []),
        })
    return {
        "data_version": str(payload.get("version", "bundled-jp")),
        "source": "bundled",
        "rarity_names": rarity_names,
        "characters": characters,
        "groups": _bundled_character_groups(),
    }


def _download_character_metadata():
    """最新BCSFE JPデータから名前・レアリティ・実装形態数を組み立てる。"""
    cc = core.CountryCode.from_code("jp")
    getter = _latest_jp_game_data_getter()
    picture_data = getter.download("DataLocal", "nyankoPictureBookData.csv")
    unit_buy_data = getter.download("DataLocal", "unitbuy.csv")
    skill_data = getter.download("DataLocal", "SkillAcquisition.csv")
    skill_name_data = getter.download("resLocal", "SkillDescriptions.csv")
    if picture_data is None or unit_buy_data is None:
        raise RuntimeError("最新JPキャラ定義を取得できません")

    picture_rows = core.CSV(picture_data).lines
    unit_buy_rows = core.CSV(unit_buy_data).lines
    talent_names = {}
    if skill_name_data is not None:
        for row in core.CSV(
            skill_name_data,
            core.Delimeter.from_country_code_res(cc),
            remove_empty=False,
        ).lines[1:]:
            if len(row) >= 2:
                talent_names[row[0].to_int()] = row[1].to_str().split("<br>", 1)[0].strip()
    talents_by_cat = {}
    if skill_data is not None:
        for row in core.CSV(skill_data, remove_empty=False).lines[1:]:
            if len(row) < 16:
                continue
            cat_id = row[0].to_int()
            talents = []
            for slot, offset in enumerate(range(2, len(row) - 13, 14)):
                ability_id = row[offset].to_int()
                max_level = row[offset + 1].to_int() or 1
                text_id = row[offset + 10].to_int()
                is_ultra = bool(row[offset + 13].to_int())
                if ability_id <= 0 or text_id <= 0:
                    continue
                talents.append({
                    "id": ability_id,
                    "name": talent_names.get(text_id) or f"本能 {ability_id}",
                    "max_level": max(1, max_level),
                    "category": "超本能" if is_ultra else "本能",
                    "ultra": is_ultra,
                    "slot": slot,
                })
            if talents:
                talents_by_cat[cat_id] = talents
    rarity_names = ["基本キャラ", "EX", "レア", "激レア", "超激レア", "伝説レア"]
    characters = []
    for cat_id, picture_row in enumerate(picture_rows):
        if len(picture_row) < 3:
            continue
        form_count = max(1, min(4, picture_row[2].to_int()))
        rarity = unit_buy_rows[cat_id][13].to_int() if cat_id < len(unit_buy_rows) and len(unit_buy_rows[cat_id]) > 13 else -1
        name_data = getter.download("resLocal", f"Unit_Explanation{cat_id + 1}_ja.csv")
        names = []
        if name_data is not None:
            name_rows = core.CSV(
                name_data,
                core.Delimeter.from_country_code_res(cc),
                remove_empty=False,
            ).lines
            names = [row[0].to_str().strip() for row in name_rows if len(row)]
        # 15.5.1実機の進化前後セーブ差分で、モモコ(ID456)の第3形態を確認済み。
        # Unit_Explanationの未実装用重複行を一般化すると大量の誤表示になるため、
        # 公開picture bookとの差分補正は実測できたキャラだけに限定する。
        if cat_id + 1 == 456:
            form_count = max(form_count, 3)
        names = [name for name in names[:form_count] if name]
        while len(names) < form_count:
            names.append(names[-1] if names else f"No.{cat_id + 1} 第{len(names) + 1}形態")
        selectable = cat_id + 1 not in ERROR_CAT_IDS and not all(
            re.fullmatch(r"\d+[-_]\d+", name) for name in names
        )
        characters.append({
            "id": cat_id + 1,
            "names": names,
            "form_count": form_count,
            "selectable": selectable,
            "true_form_state": (
                0 if cat_id < len(unit_buy_rows)
                and len(unit_buy_rows[cat_id]) > 20
                and unit_buy_rows[cat_id][20].to_int() >= 0
                else 2
            ),
            "rarity": rarity,
            "rarity_name": rarity_names[rarity] if 0 <= rarity < len(rarity_names) else "不明",
            "talents": talents_by_cat.get(cat_id, []),
        })

    if not characters:
        raise RuntimeError("最新JPキャラ名データが空です")
    return {
        "data_version": getter.version or "latest-jp",
        "source": "bcsfe",
        "rarity_names": rarity_names,
        "characters": characters,
        "groups": _download_character_groups(getter),
    }


def get_character_metadata():
    """BCSFEの最新JPキャラ情報を返し、通信失敗時だけ同梱版へ切り替える。"""
    global _character_metadata_cache, _character_metadata_cached_at
    if _character_metadata_cache is not None and time.time() - _character_metadata_cached_at < CHARACTER_METADATA_TTL:
        return _character_metadata_cache
    with _character_metadata_lock:
        if _character_metadata_cache is not None and time.time() - _character_metadata_cached_at < CHARACTER_METADATA_TTL:
            return _character_metadata_cache
        try:
            metadata = _download_character_metadata()
        except Exception as e:
            print(f"[WARN] latest character metadata fallback: {e}")
            metadata = _bundled_character_metadata()
        if not metadata.get("characters"):
            raise RuntimeError("キャラ名データを取得できません")
        _character_metadata_cache = metadata
        _character_metadata_cached_at = time.time()
        return _character_metadata_cache


def _bundled_ototo_metadata():
    names = [
        "にゃんこ城強化", "スロウ砲", "鉄壁砲", "かみなり砲",
        "水鉄砲", "エンジェル砲", "キャノンブレイク砲", "呪い砲",
    ]
    output = []
    for cannon_id, name in enumerate(names):
        if cannon_id == 0:
            parts = [{"id": 0, "name": "城体力", "min_level": 1, "max_level": 30}]
        else:
            parts = [
                {"id": 0, "name": "主砲", "min_level": 1, "max_level": 30},
                {"id": 1, "name": "土台", "min_level": 0, "max_level": 20},
                {"id": 2, "name": "装飾", "min_level": 0, "max_level": 20},
            ]
        output.append({"id": cannon_id, "name": name, "parts": parts})
    return {"data_version": "bundled-jp-15.6.0", "source": "bundled", "cannons": output}


def _download_ototo_metadata():
    cc = core.CountryCode.from_code("jp")
    getter = _latest_jp_game_data_getter()
    description_data = getter.download("resLocal", "CastleRecipeDescriptions.csv")
    unlock_data = getter.download("DataLocal", "CastleRecipeUnlock.csv")
    if description_data is None or unlock_data is None:
        raise RuntimeError("オトート定義を取得できません")

    max_levels = {}
    for row in core.CSV(unlock_data, remove_empty=False).lines:
        if len(row) < 5:
            continue
        cannon_id, part_id, level = row[0].to_int(), row[1].to_int(), row[4].to_int()
        if cannon_id < 0 or part_id not in {0, 1, 2} or level < 0:
            continue
        max_levels[(cannon_id, part_id)] = max(max_levels.get((cannon_id, part_id), 0), level)

    cannons = []
    rows = core.CSV(
        description_data,
        delimiter=core.Delimeter.from_country_code_res(cc),
        remove_empty=False,
    ).lines
    for row in rows:
        if len(row) < 9:
            continue
        cannon_id = row[0].to_int()
        if cannon_id < 0:
            continue
        name = row[1].to_str().strip() or row[6].to_str().strip() or f"城強化 #{cannon_id}"
        part_ids = [part_id for part_id in (0, 1, 2) if (cannon_id, part_id) in max_levels]
        if not part_ids:
            continue
        part_names = {
            0: "城体力" if cannon_id == 0 else "主砲",
            1: row[7].to_str().strip() or "土台",
            2: row[8].to_str().strip() or "装飾",
        }
        parts = [{
            "id": part_id,
            "name": part_names[part_id],
            "min_level": 1 if part_id == 0 else 0,
            "max_level": max_levels[(cannon_id, part_id)],
        } for part_id in part_ids]
        cannons.append({"id": cannon_id, "name": name, "parts": parts})
    if not cannons:
        raise RuntimeError("オトート定義が空です")
    return {
        "data_version": getter.version or "latest-jp",
        "source": "bcsfe",
        "cannons": cannons,
    }


def get_ototo_metadata():
    global _ototo_metadata_cache, _ototo_metadata_cached_at
    if _ototo_metadata_cache is not None and time.time() - _ototo_metadata_cached_at < OTOTO_METADATA_TTL:
        return _ototo_metadata_cache
    with _ototo_metadata_lock:
        if _ototo_metadata_cache is not None and time.time() - _ototo_metadata_cached_at < OTOTO_METADATA_TTL:
            return _ototo_metadata_cache
        try:
            metadata = _download_ototo_metadata()
        except Exception as e:
            print(f"[WARN] latest ototo metadata fallback: {e}")
            metadata = _bundled_ototo_metadata()
        _ototo_metadata_cache = metadata
        _ototo_metadata_cached_at = time.time()
        return _ototo_metadata_cache


def get_legend_metadata():
    """bcsfeの最新JPゲームデータから章名・ステージ名・実装済み冠数を返す。"""
    global _legend_metadata_cache, _legend_metadata_cached_at
    if _legend_metadata_cache is not None and time.time() - _legend_metadata_cached_at < LEGEND_METADATA_TTL:
        return _legend_metadata_cache
    with _legend_metadata_lock:
        if _legend_metadata_cache is not None and time.time() - _legend_metadata_cached_at < LEGEND_METADATA_TTL:
            return _legend_metadata_cache

        cc = core.CountryCode.from_code("jp")
        # 文字列順では15.10と15.9を誤判定し得るため、配布版を数値比較する。
        getter = _latest_jp_game_data_getter()
        map_name_data = getter.download("resLocal", "Map_Name.csv")
        map_option_data = getter.download("DataLocal", "Map_option.csv")
        if map_name_data is None or map_option_data is None:
            raise RuntimeError("最新JPゲームデータを取得できません")
        map_name_csv = core.CSV(map_name_data, core.Delimeter.from_country_code_res(cc))
        all_map_names = {
            row[0].to_int(): row[1].to_str().strip()
            for row in map_name_csv if len(row) >= 2
        }
        map_option = core.MapOption.from_csv(core.CSV(map_option_data))
        # 異次元コロシアムは全章のMap_Nameが同名なので、特殊ルール名を章名へ足す。
        special_rule_names = {}
        special_rules_data = getter.download("DataLocal", "SpecialRulesMap.json")
        localizable_data = getter.download("resLocal", "localizable.tsv")
        if special_rules_data is not None and localizable_data is not None:
            localizable_names = {
                row[0].to_str().strip(): row[1].to_str().strip()
                for row in core.CSV(localizable_data, "\t") if len(row) >= 2
            }
            raw_rule_maps = core.JsonFile.from_data(special_rules_data).as_object().get("MapID", {})
            for raw_map_id, rule in raw_rule_maps.items():
                try:
                    full_map_id = int(raw_map_id)
                except (TypeError, ValueError):
                    continue
                label = str(rule.get("RuleNameLabel", "")) if isinstance(rule, dict) else ""
                if label and localizable_names.get(label):
                    special_rule_names[full_map_id] = localizable_names[label]
        metadata = {}
        for series_key, spec in LEGEND_SERIES.items():
            stage_data = getter.download(
                "resLocal", f"StageName_R{spec['code']}_ja.csv"
            )
            if stage_data is None:
                raise RuntimeError(f"{series_key}のステージ名を取得できません")
            stage_csv = core.CSV(stage_data, core.Delimeter.from_country_code_res(cc))
            maps = []
            for map_id, row in enumerate(stage_csv):
                stage_names = _clean_stage_names(row.to_str_list())
                map_name = all_map_names.get(spec["base_index"] + map_id)
                if not stage_names or not map_name:
                    continue
                option = map_option.get_map(spec["base_index"] + map_id) if map_option else None
                crown_count = max(1, min(4, int(option.crown_count if option else 4)))
                maps.append({
                    "id": map_id,
                    "name": map_name,
                    "stages": stage_names,
                    "crowns": crown_count,
                })
            metadata[series_key] = {"label": spec["label"], "maps": maps}

        # ゾンビ襲来はメイン3編×各3章。セーブ内のステージ順に並べ替えて返す。
        story_stage_names = {}
        for chapter_type in range(3):
            stage_data = getter.download("resLocal", f"StageName{chapter_type}_ja.csv")
            if stage_data is None:
                raise RuntimeError(f"ゾンビ編{chapter_type}のステージ名を取得できません")
            stage_csv = core.CSV(stage_data, core.Delimeter.from_country_code_res(cc))
            source_names = [row[0].to_str().strip() for row in stage_csv if len(row)]
            story_stage_names[chapter_type] = [
                source_names[stage_id if stage_id >= 46 else 45 - stage_id]
                for stage_id in range(min(48, len(source_names)))
            ]
        metadata["zombie"] = {
            "label": "ゾンビ襲来",
            "chapters": [
                {
                    "id": position,
                    "real_id": MAIN_STORY_REAL_INDEX[position],
                    "name": MAIN_STORY_CHAPTER_LABELS[position],
                    "stages": story_stage_names[position // 3],
                }
                for position in range(len(MAIN_STORY_CHAPTER_LABELS))
            ],
        }
        metadata["main_story"] = {
            "label": "メインステージ",
            "chapters": [
                {
                    "id": position,
                    "real_id": MAIN_STORY_REAL_INDEX[position],
                    "name": MAIN_STORY_CHAPTER_LABELS[position],
                    "stages": story_stage_names[position // 3],
                }
                for position in range(len(MAIN_STORY_CHAPTER_LABELS))
            ],
        }

        # bcsfeの各ステージ編集機能と同じ文字コード・ベースIDを使う。
        # 通常イベントだけでなく、塔・強襲・超獣・地図・コラボもまとめて返す。
        event_families = {}
        for family_key, family_spec in EVENT_STAGE_FAMILIES.items():
            stage_file = family_spec.get(
                "stage_file", f"StageName_R{family_spec['code']}_ja.csv"
            )
            stage_data = getter.download(
                "resLocal", stage_file
            )
            if stage_data is None:
                continue
            stage_csv = core.CSV(stage_data, core.Delimeter.from_country_code_res(cc))
            maps = []
            for map_id, row in enumerate(stage_csv):
                stage_names = _clean_stage_names(row.to_str_list())
                full_map_id = family_spec["base_index"] + map_id
                map_name = all_map_names.get(full_map_id)
                if not stage_names or not map_name:
                    continue
                if family_key == "colosseum" and special_rule_names.get(full_map_id):
                    map_name = f"{map_name}【{special_rule_names[full_map_id]}】"
                option = map_option.get_map(full_map_id) if map_option else None
                crown_count = max(1, min(4, int(option.crown_count if option else 1)))
                maps.append({
                    "id": map_id,
                    "name": map_name,
                    "stages": stage_names,
                    "crowns": crown_count,
                })
            event_families[family_key] = {
                "label": family_spec["label"],
                "maps": maps,
                "uses_clear_count": family_spec.get("uses_clear_count", True),
            }
        metadata["event_stage_families"] = event_families

        aku_stage_data = getter.download("resLocal", "StageName_DM_ja.csv")
        if aku_stage_data is None:
            raise RuntimeError("魔界編のステージ名を取得できません")
        aku_stage_csv = core.CSV(aku_stage_data, core.Delimeter.from_country_code_res(cc))
        aku_stage_names = _clean_stage_names([
            value.to_str() for row in aku_stage_csv for value in row
        ])
        metadata["aku"] = {
            "label": "魔界編",
            "chapters": [{"id": 0, "name": "魔界編", "stages": aku_stage_names}],
        }
        aku_ex_stage_data = getter.download("resLocal", "StageName_RE_ja.csv")
        if aku_ex_stage_data is None:
            raise RuntimeError("魔界編EXのステージ名を取得できません")
        aku_ex_stage_csv = core.CSV(aku_ex_stage_data, core.Delimeter.from_country_code_res(cc))
        aku_ex_names = _clean_stage_names(aku_ex_stage_csv[42].to_str_list())
        metadata["aku_ex"] = {
            "label": "魔界編EX",
            "chapters": [{
                "id": 0,
                "name": all_map_names.get(4042, "富士山EX"),
                "stages": aku_ex_names or ["富士山"],
            }],
        }

        metadata["data_version"] = getter.version

        ability_data = getter.download("DataLocal", "AbilityData.csv")
        if ability_data is None:
            raise RuntimeError("施設データを取得できません")
        ability_csv = core.CSV(ability_data)
        metadata["facilities"] = [
            {
                "id": facility_id,
                "name": FACILITY_NAMES[facility_id],
                "max_base": ability_csv[facility_id][2].to_int(),
                "max_plus": ability_csv[facility_id][3].to_int(),
                "max_level": ability_csv[facility_id][2].to_int() + ability_csv[facility_id][3].to_int(),
            }
            for facility_id in range(min(len(FACILITY_NAMES), len(ability_csv)))
        ]

        equipment_data = getter.download("DataLocal", "equipmentlist.json")
        grade_data = getter.download("DataLocal", "equipmentgrade.csv")
        attribute_data = getter.download("resLocal", "attribute_explonation.tsv")
        effect_data = getter.download("resLocal", "equipment_explonation.tsv")
        if any(value is None for value in (equipment_data, grade_data, attribute_data, effect_data)):
            raise RuntimeError("本能玉データを取得できません")
        raw_orbs = core.JsonFile.from_data(equipment_data).as_object().get("ID", [])
        grade_csv = core.CSV(grade_data)
        attribute_csv = core.CSV(attribute_data, "\t")
        effect_csv = core.CSV(effect_data, "\t")
        orb_groups = {}
        for orb_id, raw_orb in enumerate(raw_orbs):
            grade_id = int(raw_orb.get("gradeID", 0))
            effect_id = int(raw_orb.get("content", 0))
            target_id = raw_orb.get("attribute")
            target_id = int(target_id) if target_id is not None else None
            key = (target_id, effect_id)
            if key not in orb_groups:
                target = ""
                if target_id is not None and 0 <= target_id < len(attribute_csv):
                    row = attribute_csv[target_id]
                    target = (row[1].to_str() if len(row) > 1 else row[0].to_str()).strip()
                effect = effect_csv[effect_id][0].to_str().split("%@", 1)[0].strip()
                orb_groups[key] = {
                    "name": f"{target} {effect}".strip(),
                    "target_id": target_id,
                    "effect_id": effect_id,
                    "ranks": [],
                }
            rank = grade_csv[grade_id][3].to_str().strip()
            orb_groups[key]["ranks"].append({"id": orb_id, "rank": rank, "grade_id": grade_id})
        metadata["talent_orbs"] = list(orb_groups.values())

        ticket_buy_data = getter.download("DataLocal", "Gatyaitembuy.csv")
        ticket_name_data = getter.download("resLocal", "GatyaitemName.csv")
        if ticket_buy_data is None or ticket_name_data is None:
            raise RuntimeError("イベントチケットデータを取得できません")
        ticket_buy_csv = core.CSV(ticket_buy_data)
        ticket_name_csv = core.CSV(ticket_name_data, core.Delimeter.from_country_code_res(cc))
        ticket_category_labels = {1: "イベント", 8: "福引", 10: "福引2"}
        event_tickets = []
        for item_id, row in enumerate(ticket_buy_csv.lines[1:]):
            if len(row) < 8:
                continue
            category = row[6].to_int()
            index = row[7].to_int()
            if category not in ticket_category_labels or index < 0:
                continue
            name = ticket_name_csv[item_id][0].to_str().strip() if item_id < len(ticket_name_csv) else ""
            comment = row[12].to_str().strip() if len(row) > 12 else ""
            purpose = comment or name or "名称不明チケット"
            if purpose == name:
                label = f"{name}（{ticket_category_labels[category]}保存枠{index + 1}）"
            else:
                label = f"{purpose}（{name or '名称不明チケット'}・{ticket_category_labels[category]}保存枠{index + 1}）"
            event_tickets.append({
                "id": f"{category}:{index}",
                "item_id": item_id,
                "category": category,
                "index": index,
                "name": name or "名称不明チケット",
                "purpose": purpose,
                "label": label,
            })
        # Gatyaitembuy.csvには過去イベントの保存枠も残り続ける。
        # 同名チケットはitem_idが最も新しい枠だけを採用し、最新版追加時に自動更新する。
        latest_event_tickets = {}
        for ticket in event_tickets:
            previous = latest_event_tickets.get(ticket["name"])
            priority = ("ID統一用" in ticket["purpose"], ticket["item_id"])
            previous_priority = (
                "ID統一用" in previous["purpose"], previous["item_id"]
            ) if previous else (False, -1)
            if priority > previous_priority:
                latest_event_tickets[ticket["name"]] = ticket
        event_tickets = sorted(latest_event_tickets.values(), key=lambda item: item["item_id"])
        for ticket in event_tickets:
            if "ID統一用" in ticket["purpose"]:
                ticket["label"] = f"{ticket['name']}（最新共通枠）"
            elif ticket["purpose"] != ticket["name"]:
                ticket["label"] = f"{ticket['purpose']}（{ticket['name']}・最新枠）"
            else:
                ticket["label"] = f"{ticket['name']}（最新枠）"
        metadata["event_tickets"] = event_tickets

        # 既存のVIP個別アイテムもGatyaitembuyの保存インデックスから生成する。
        # 新しいマタタビ等が追加された場合、HTMLの固定配列を更新せず追従できる。
        category_by_group = {
            "battle_items": 3,
            "catfruit": 4,
            "catseyes": 5,
            "catamins": 6,
            "base_materials": 7,
        }
        vip_item_groups = {}
        for group_key, category in category_by_group.items():
            labels_by_index = {}
            for item_id, row in enumerate(ticket_buy_csv.lines[1:]):
                if len(row) < 8 or row[6].to_int() != category:
                    continue
                item_index = row[7].to_int()
                if item_index < 0:
                    continue
                item_name = ticket_name_csv[item_id][0].to_str().strip() if item_id < len(ticket_name_csv) else ""
                labels_by_index[item_index] = item_name or f"{group_key}[{item_index}]"
            if labels_by_index:
                max_index = max(labels_by_index)
                fallback_labels = VIP_ITEM_LABELS.get(group_key, [])
                vip_item_groups[group_key] = [
                    labels_by_index.get(
                        item_index,
                        fallback_labels[item_index] if item_index < len(fallback_labels) else f"{group_key}[{item_index}]",
                    )
                    for item_index in range(max_index + 1)
                ]
        metadata["vip_item_groups"] = vip_item_groups
        _legend_metadata_cache = metadata
        _legend_metadata_cached_at = time.time()
        return metadata


LATEST_STAGE_CONTAINER_MINIMUMS = {
    "gauntlets": 82,
    "collab_gauntlets": 28,
    "zero_legend": 34,
}
LATEST_TALENT_ORB_COUNT = 310


def _character_form_counts():
    return {
        int(character["id"]): int(character["form_count"])
        for character in get_character_metadata().get("characters", [])
    }


def _character_true_form_states():
    return {
        int(character["id"]): max(0, min(2, int(character.get("true_form_state", 2))))
        for character in get_character_metadata().get("characters", [])
    }


def _latest_talent_orb_ids():
    try:
        ids = [
            int(rank["id"])
            for group in get_legend_metadata().get("talent_orbs", [])
            for rank in group.get("ranks", [])
        ]
        if ids:
            return sorted(set(ids))
    except Exception as e:
        print(f"[WARN] talent orb metadata fallback: {e}")
    return list(range(LATEST_TALENT_ORB_COUNT))


def _ensure_gauntlet_map(container, map_id):
    """BCSFEのGauntletChaptersへ未実装時の空マップを追加する。"""
    if map_id < 0 or not getattr(container, "chapters", None):
        return False
    first = container.chapters[0]
    if not first.chapters:
        return False
    total_stars = len(first.chapters)
    total_stages = len(first.chapters[0].stages)
    while len(container.chapters) <= map_id:
        chapter = type(first).init(total_stages, total_stars)
        for star_chapter in chapter.chapters:
            star_chapter.total_stages = total_stages
        container.chapters.append(chapter)
        if hasattr(container, "unknown"):
            container.unknown.append(0)
    return True


def _ensure_standard_chapter_map(container, map_id):
    """Chapters系（ネコビタン）へ最新版で増えたマップ枠を非破壊で追加する。"""
    chapters = getattr(container, "chapters", None)
    if map_id < 0 or not chapters:
        return False
    first = chapters[0]
    if not first.chapters:
        return False
    total_stars = len(first.chapters)
    total_stages = len(first.chapters[0].stages)
    while len(chapters) <= map_id:
        chapter = type(first).init(total_stages, total_stars)
        for star_chapter in chapter.chapters:
            star_chapter.total_stages = total_stages
        chapters.append(chapter)
    return True


def _ensure_latest_stage_slots(save, metadata=None):
    """15.5.1の既存ステージ機能に必要な保存枠を進行状態を保って補完する。"""
    targets = dict(LATEST_STAGE_CONTAINER_MINIMUMS)
    if metadata:
        for family_key, attribute in (
            ("gauntlets", "gauntlets"),
            ("collab_gauntlets", "collab_gauntlets"),
            ("behemoth", "behemoth_culling"),
            ("enigma", "enigma_clears"),
        ):
            maps = metadata.get("event_stage_families", {}).get(family_key, {}).get("maps", [])
            if maps:
                targets[attribute] = max(targets.get(attribute, 0), max(item["id"] for item in maps) + 1)
        zero_maps = metadata.get("zero_legend", {}).get("maps", [])
        if zero_maps:
            targets["zero_legend"] = max(targets["zero_legend"], max(item["id"] for item in zero_maps) + 1)

    for attribute in ("gauntlets", "collab_gauntlets", "behemoth_culling", "enigma_clears"):
        target_length = targets.get(attribute, 0)
        container = getattr(save, attribute, None)
        if container is not None and target_length:
            _ensure_gauntlet_map(container, target_length - 1)
    if metadata:
        catamin_maps = metadata.get("event_stage_families", {}).get("catamin", {}).get("maps", [])
        if catamin_maps:
            previous_length = len(save.catamin_stages.chapters.chapters)
            _ensure_standard_chapter_map(
                save.catamin_stages.chapters,
                max(item["id"] for item in catamin_maps),
            )
            added = len(save.catamin_stages.chapters.chapters) - previous_length
            if added > 0:
                save.catamin_stages.unknown.extend([0] * added)
        catclaw_maps = metadata.get("event_stage_families", {}).get("catclaw", {}).get("maps", [])
        if catclaw_maps and getattr(save, "dojo_chapters", None) is not None:
            save.dojo_chapters.create(max(item["id"] for item in catclaw_maps))
    if targets.get("zero_legend"):
        _ensure_zero_map(save, targets["zero_legend"] - 1)


def ensure_latest_save_schema(save):
    """旧15.4テンプレートを15.5.1互換へ非破壊で補完する。"""
    save.set_gv(core.GameVersion(TARGET_GAME_VERSION_NUMBER))
    # BCSFE 3.6.0で15.5.0から追加された保存フィールド。
    if not hasattr(save, "ub39"):
        save.ub39 = False

    metadata = get_character_metadata()
    characters = metadata.get("characters", [])
    target_cat_count = max((int(character["id"]) for character in characters), default=len(save.cats.cats))
    while len(save.cats.cats) < target_cat_count:
        save.cats.cats.append(core.Cat.init(len(save.cats.cats)))

    legend_metadata = None
    try:
        legend_metadata = get_legend_metadata()
    except Exception as e:
        print(f"[WARN] latest stage schema fallback: {e}")
    _ensure_latest_stage_slots(save, legend_metadata)

    # 長さ付き保存配列は、最新の既存アイテム数までゼロで拡張する。
    item_groups = (legend_metadata or {}).get("vip_item_groups", {})
    for attribute, group_key in (("catfruit", "catfruit"), ("catseyes", "catseyes"), ("catamins", "catamins")):
        target = len(item_groups.get(group_key, []))
        values = getattr(save, attribute, None)
        if isinstance(values, list) and target > len(values):
            values.extend([0] * (target - len(values)))
    materials = getattr(getattr(getattr(save, "ototo", None), "base_materials", None), "materials", None)
    target_materials = len(item_groups.get("base_materials", []))
    if isinstance(materials, list) and materials and target_materials > len(materials):
        material_type = type(materials[0])
        materials.extend(material_type.init() for _ in range(target_materials - len(materials)))

    ticket_arrays = {
        1: ("event_capsules", "event_capsules_counter"),
        8: ("lucky_tickets",),
        10: ("event_capsules_2",),
    }
    for category, attributes in ticket_arrays.items():
        category_tickets = [
            ticket for ticket in (legend_metadata or {}).get("event_tickets", [])
            if int(ticket.get("category", -1)) == category
        ]
        required_length = max((int(ticket["index"]) + 1 for ticket in category_tickets), default=0)
        for attribute in attributes:
            values = getattr(save, attribute, None)
            if isinstance(values, list) and required_length > len(values):
                values.extend([0] * (required_length - len(values)))
    return save


def _set_cat_form(save, cat, form_count, requested_form=None, true_form_state=None):
    """実装済み形態数を越えず、実機の自然な形態フラグで設定する。"""
    total_forms = max(1, min(4, int(form_count)))
    form = max(1, min(int(requested_form or total_forms), total_forms))
    if not cat.unlocked:
        cat.unlock(save)

    # BCSFEのset_form()/true_form()は、2形態しかないキャラにも
    # unlocked_forms=1以上を付ける経路がある。これは実機セーブの
    # 「2形態キャラ=current_form 1 / unlocked_forms 0」と一致しないため、
    # 形態数と選択形態から3フィールドを必ず一組で検証する。
    old_current = max(0, int(cat.current_form))
    old_unlocked = max(0, int(cat.unlocked_forms))
    old_fourth = int(cat.fourth_form)
    cat.current_form = form - 1
    if total_forms == 1:
        cat.unlocked_forms = 0
        cat.fourth_form = 0
    elif total_forms == 2:
        cat.unlocked_forms = 0
        cat.fourth_form = 0
    elif form == 4:
        cat.unlocked_forms = max(0, min(2, int(true_form_state if true_form_state is not None else 2)))
        cat.fourth_form = 2
    elif form == 3:
        cat.unlocked_forms = max(0, min(2, int(true_form_state if true_form_state is not None else 2)))
        cat.fourth_form = 0 if total_forms == 3 else max(0, min(old_fourth, 2))
    elif total_forms == 3:
        cat.unlocked_forms = min(old_unlocked, 3)
        cat.fourth_form = 0
    elif old_fourth == 2 or old_current == 3:
        cat.unlocked_forms = min(old_unlocked, 3)
        cat.fourth_form = 2
    else:
        cat.unlocked_forms = min(old_unlocked, 3)
        cat.fourth_form = max(0, min(old_fourth, 1))
    return form


def _set_cat_latest_form(save, cat, form_counts=None, true_form_states=None):
    counts = form_counts or _character_form_counts()
    total_forms = counts.get(cat.id + 1)
    if total_forms is None:
        # 配布metadataより新しいキャラを1形態と決めつけて壊さない。
        return None
    states = true_form_states or _character_true_form_states()
    return _set_cat_form(save, cat, total_forms, true_form_state=states.get(cat.id + 1, 2))


def _normalise_cat_form_flags(save, form_counts=None):
    """既存セーブに残った未実装形態・余分な進化権を安全に補正する。

    現在形態を可能な限り保ちつつ、実装形態数を越える値だけを縮める。
    第3/第4形態を既に解放済みで現在は下位形態を使用している状態は保持する。
    """
    counts = form_counts or _character_form_counts()
    repaired = 0
    for cat in save.cats.cats:
        if cat.id + 1 not in counts:
            # 新版で追加された未知キャラは、metadataが追いつくまで現状を保つ。
            continue
        total_forms = max(1, min(4, int(counts[cat.id + 1])))
        before = (
            int(cat.current_form),
            int(cat.unlocked_forms),
            int(cat.fourth_form),
        )

        current_form = max(0, min(int(cat.current_form), total_forms - 1))
        unlocked_forms = max(0, int(cat.unlocked_forms))
        fourth_form = int(cat.fourth_form)

        if total_forms == 1:
            current_form = unlocked_forms = fourth_form = 0
        elif total_forms == 2:
            # 2形態キャラの第2形態は進化権を使用しない。
            unlocked_forms = 0
            fourth_form = 0
        elif total_forms == 3:
            # 第三形態のunlocked_formsは取得経路により0/1/2/3があり得る。
            # 推測で引き上げず、未実装の第4形態フラグだけを除去する。
            unlocked_forms = min(unlocked_forms, 3)
            fourth_form = 0
        else:
            # fourth_form=2 または実際に第4形態を使用中なら解放済み。
            has_fourth = fourth_form == 2 or current_form == 3
            if has_fourth:
                unlocked_forms = min(unlocked_forms, 3)
                fourth_form = 2
            else:
                current_form = min(current_form, 2)
                unlocked_forms = min(unlocked_forms, 3)
                fourth_form = max(0, min(fourth_form, 1))

        after = (current_form, unlocked_forms, fourth_form)
        if after != before:
            cat.current_form, cat.unlocked_forms, cat.fourth_form = after
            repaired += 1
    return repaired


def _talent_definitions_by_cat():
    """画面と適用処理が同じ最新JP本能定義を使うための索引。"""
    return {
        int(character["id"]): {
            int(talent["id"]): talent for talent in character.get("talents", [])
        }
        for character in get_character_metadata().get("characters", [])
        if character.get("talents")
    }


def _set_cat_talent_level(cat, ability_id, level):
    if cat.talents is None:
        cat.talents = []
    talent = next((item for item in cat.talents if item.id == ability_id), None)
    if talent is None:
        cat.talents.append(Talent(ability_id, level))
    else:
        talent.level = level


def _apply_character_talents(cat, display_id, setting, talent_definitions):
    """指定キャラの実装済み本能だけを設定。自然上限を越える値も保持する。"""
    definitions = talent_definitions.get(display_id, {})
    if not definitions:
        return 0
    action = setting.get("talent_action", "set")
    if action == "disable_all":
        if cat.talents is None:
            return 0
        for talent in cat.talents:
            talent.level = 0
        return len(cat.talents)
    levels = setting.get("talents", {})
    applied = 0
    for talent_id, requested_level in levels.items():
        ability_id = int(talent_id)
        if ability_id not in definitions:
            continue
        level = 0 if action == "disable_selected" else max(0, min(32767, int(requested_level)))
        _set_cat_talent_level(cat, ability_id, level)
        applied += 1
    return applied


def _latest_rank_gift_thresholds():
    """最新JP rankGift.csvを数値版で読み、(保存index, 必要UR)を返す。"""
    getter = _latest_jp_game_data_getter()
    data = getter.download("DataLocal", "rankGift.csv")
    if data is None:
        raise RuntimeError("ユーザーランク報酬定義を取得できません")
    return [
        (index, row[0].to_int())
        for index, row in enumerate(core.CSV(data).lines)
        if len(row)
    ]


def _set_all_user_rank_rewards(save, claimed):
    """現在到達済みUR報酬を一括変更し、未来報酬は未受取のままにする。"""
    thresholds = _latest_rank_gift_thresholds()
    required = max((index + 1 for index, _ in thresholds), default=0)
    while len(save.user_rank_rewards.rewards) < required:
        save.user_rank_rewards.rewards.append(Reward.init())
    user_rank = save.calculate_user_rank()
    original = [reward.claimed for reward in save.user_rank_rewards.rewards]
    for reward in save.user_rank_rewards.rewards:
        reward.claimed = False
    for index, threshold in thresholds:
        if claimed and threshold <= user_rank:
            save.user_rank_rewards.rewards[index].claimed = True
    changed = sum(
        before != reward.claimed
        for before, reward in zip(original, save.user_rank_rewards.rewards)
    )
    return changed, sum(threshold <= user_rank for _, threshold in thresholds)


def _set_all_catguide_rewards(save, claimed):
    """受取化は所持キャラ、未受取化は全保存枠を対象にする。"""
    changed = 0
    targets = [cat for cat in save.cats.cats if cat.unlocked] if claimed else save.cats.cats
    for cat in targets:
        if cat.catguide_collected != bool(claimed):
            cat.catguide_collected = bool(claimed)
            changed += 1
    return changed, len(targets)


def _complete_all_defined_missions(save):
    """最新JP定義のメイン・スペシャル・週・月ミッションを全て達成状態へする。"""
    getter = _latest_jp_game_data_getter()
    data = getter.download("DataLocal", "Mission_Condition.csv")
    if data is None:
        raise RuntimeError("ミッション定義を取得できません")
    # 配信終了済みだがセーブに残っているミッションも取りこぼさない。
    seen = set(save.missions.clear_states)
    for mission_id in seen:
        save.missions.clear_states[mission_id] = 2
    for row in core.CSV(data).lines:
        if len(row) < 4:
            continue
        if not row[0].to_str().strip().lstrip("-").isdigit():
            continue
        mission_id = row[0].to_int()
        mission_type = row[1].to_int()
        seen.add(mission_id)
        save.missions.clear_states[mission_id] = 2
        save.missions.requirements[mission_id] = max(0, row[3].to_int())
        # この辞書は週次だけでなく、現在表示済みの期間ミッションIDも保持する。
        if mission_type in {1, 2, 3}:
            save.missions.weekly_missions[mission_id] = True
    return len(seen)


def validate_legend_stages_shape(data):
    """legend_stagesの構造と総選択数を軽量検証する。"""
    raw = data.get("legend_stages", {})
    if not isinstance(raw, dict) or any(key not in LEGEND_SERIES for key in raw):
        return False
    count = 0
    for maps in raw.values():
        if not isinstance(maps, dict) or len(maps) > 100:
            return False
        for stars in maps.values():
            if not isinstance(stars, dict) or len(stars) > 4:
                return False
            for stages in stars.values():
                if not isinstance(stages, list) or len(stages) > 12:
                    return False
                count += len(stages)
                if count > MAX_LEGEND_SELECTIONS:
                    return False
    return True


def validate_detailed_stage_settings_shape(data):
    """メイン・イベントのステージ別クリア回数のネストと総件数を検証する。"""
    total = 0
    main_raw = data.get("main_story_stages")
    if main_raw is not None:
        if not isinstance(main_raw, dict) or len(main_raw) > len(MAIN_STORY_REAL_INDEX):
            return False
        for stages in main_raw.values():
            if not isinstance(stages, dict) or len(stages) > 48:
                return False
            total += len(stages)

    event_raw = data.get("event_stage_settings")
    if event_raw is not None:
        if not isinstance(event_raw, dict) or any(key not in EVENT_STAGE_FAMILIES for key in event_raw):
            return False
        for family_key, maps in event_raw.items():
            if not isinstance(maps, dict) or len(maps) > 1000:
                return False
            for stars in maps.values():
                if not isinstance(stars, dict) or len(stars) > 4:
                    return False
                for stages in stars.values():
                    max_stages = 256 if family_key == "labyrinth" else 64
                    if not isinstance(stages, dict) or len(stages) > max_stages:
                        return False
                    total += len(stages)
    return total <= MAX_DETAILED_STAGE_SELECTIONS


def validate_labyrinth_character_settings_shape(data):
    """地底迷宮の封印・解除指定の型と件数だけを先に検証する。"""
    raw = data.get("labyrinth_characters")
    if raw is None:
        return True
    if not isinstance(raw, dict) or raw.get("action") not in LABYRINTH_CHARACTER_ACTIONS:
        return False
    ids = raw.get("ids", [])
    if not isinstance(ids, list) or len(ids) > MAX_LABYRINTH_CHARACTERS:
        return False
    try:
        return all(1 <= int(cat_id) <= 9999 for cat_id in ids)
    except (TypeError, ValueError):
        return False


def validate_lineup_settings_shape(data):
    """VIP編成設定の型を検証する。キャラ番号は画面と同じ1始まり、空枠はNone。"""
    raw = data.get("lineup_settings")
    if raw is None:
        return True
    if not isinstance(raw, dict):
        return False
    cats = raw.get("cats")
    forms = raw.get("forms")
    if not isinstance(cats, list) or len(cats) != 10 or not isinstance(forms, list) or len(forms) != 10:
        return False
    try:
        lineup_number = int(raw.get("lineup", 1))
        if not 1 <= lineup_number <= 20:
            return False
        return (
            all(cat_id is None or 1 <= int(cat_id) <= 9999 for cat_id in cats)
            and all(form is None or 1 <= int(form) <= 4 for form in forms)
        )
    except (TypeError, ValueError):
        return False


def validate_score_settings_shape(data):
    """VIP限定の道場・未来編スコア指定の型と件数を検証する。"""
    dojo = data.get("dojo_score_settings")
    if dojo is not None:
        if not isinstance(dojo, dict) or set(dojo) - {"hall_of_initiates"}:
            return False
        try:
            if not all(0 <= int(value) <= 2_147_483_647 for value in dojo.values()):
                return False
        except (TypeError, ValueError):
            return False

    future = data.get("future_score_settings")
    if future is not None:
        if not isinstance(future, dict) or len(future) > 3:
            return False
        try:
            if any(int(position) not in {3, 4, 5} for position in future):
                return False
            if not all(0 <= int(value) <= 9999 for value in future.values()):
                return False
        except (TypeError, ValueError):
            return False
    return True


def safe_dojo_score_settings(data):
    """BCSFEで保存可能な常設道場（入門の間）のスコアだけを返す。"""
    raw = data.get("dojo_score_settings")
    if not isinstance(raw, dict) or "hall_of_initiates" not in raw:
        return None
    try:
        score = max(0, min(2_147_483_647, int(raw["hall_of_initiates"])))
    except (TypeError, ValueError):
        return None
    return {"hall_of_initiates": score}


def safe_future_score_settings(data):
    """画面上の未来編1～3章（位置3～5）を0～9999へ正規化する。"""
    raw = data.get("future_score_settings")
    if not isinstance(raw, dict):
        return None
    output = {}
    for raw_position, raw_score in list(raw.items())[:3]:
        try:
            position = int(raw_position)
            score = max(0, min(9999, int(raw_score)))
        except (TypeError, ValueError):
            continue
        if position in {3, 4, 5}:
            output[position] = score
    return output or None


def safe_lineup_settings(data):
    """編成番号と10枠を、最新版キャラmetadataに照合して正規化する。"""
    raw = data.get("lineup_settings")
    if not isinstance(raw, dict):
        return None
    try:
        characters = get_character_metadata().get("characters", [])
        form_counts = {
            int(character["id"]): int(character.get("form_count", 1))
            for character in characters if character.get("selectable", True)
        }
        valid_ids = set(form_counts)
    except Exception:
        valid_ids = set()
        form_counts = {}
    try:
        lineup_number = max(1, min(20, int(raw.get("lineup", 1))))
    except (TypeError, ValueError):
        lineup_number = 1
    cats = []
    for raw_id in raw.get("cats", [])[:10]:
        if raw_id is None:
            cats.append(None)
            continue
        try:
            cat_id = int(raw_id)
        except (TypeError, ValueError):
            cats.append(None)
            continue
        cats.append(cat_id if (not valid_ids or cat_id in valid_ids) else None)
    cats.extend([None] * (10 - len(cats)))
    forms = []
    for index, raw_form in enumerate(raw.get("forms", [])[:10]):
        cat_id = cats[index] if index < len(cats) else None
        if raw_form is None:
            forms.append(None)
            continue
        try:
            requested = int(raw_form)
        except (TypeError, ValueError):
            requested = 1
        forms.append(max(1, min(requested, form_counts.get(cat_id, 4))))
    forms.extend([None] * (10 - len(forms)))
    # 形態はキャラ単位の保存値。同じキャラで混在した場合は高い形態を優先する。
    final_forms = {}
    for cat_id, form in zip(cats, forms):
        if cat_id is not None and form is not None:
            final_forms[cat_id] = max(final_forms.get(cat_id, 1), form)
    forms = [final_forms.get(cat_id) for cat_id in cats]
    return {"lineup": lineup_number, "cats": cats, "forms": forms}


def safe_legend_stages(data):
    """章→星→ステージの選択をbcsfeメタデータに照合して正規化する。"""
    raw = data.get("legend_stages", {})
    if not isinstance(raw, dict):
        return {}
    try:
        metadata = get_legend_metadata()
    except Exception as e:
        print(f"[ERROR] legend metadata: {e}")
        return {}

    result = {}
    for series_key, raw_maps in raw.items():
        if series_key not in LEGEND_SERIES or not isinstance(raw_maps, dict):
            continue
        map_specs = {m["id"]: m for m in metadata.get(series_key, {}).get("maps", [])}
        clean_maps = {}
        for raw_map_id, raw_stars in raw_maps.items():
            try:
                map_id = int(raw_map_id)
            except (TypeError, ValueError):
                continue
            map_spec = map_specs.get(map_id)
            if map_spec is None or not isinstance(raw_stars, dict):
                continue
            clean_stars = {}
            for raw_star, raw_stages in raw_stars.items():
                try:
                    star = int(raw_star)
                except (TypeError, ValueError):
                    continue
                if not 0 <= star < map_spec["crowns"] or not isinstance(raw_stages, list):
                    continue
                stages = sorted({
                    int(stage) for stage in raw_stages
                    if str(stage).lstrip("-").isdigit() and 0 <= int(stage) < len(map_spec["stages"])
                })
                if stages:
                    clean_stars[star] = stages
            if clean_stars:
                clean_maps[map_id] = clean_stars
        # 空dictも「親は選択済みだが詳細は全解除」として保持する。
        result[series_key] = clean_maps
    return result


def full_legend_selection(series_key):
    """指定シリーズの実装済み章・冠・ステージをすべて選択した形にする。"""
    metadata = get_legend_metadata().get(series_key, {})
    maps = {}
    for map_spec in metadata.get("maps", []):
        maps[map_spec["id"]] = {
            star: list(range(len(map_spec["stages"])))
            for star in range(map_spec["crowns"])
        }
    return {series_key: maps}


def safe_vip_facilities(data):
    raw = data.get("vip_facilities", {})
    if not isinstance(raw, dict):
        return {}
    try:
        specs = {item["id"]: item for item in get_legend_metadata().get("facilities", [])}
    except Exception as e:
        print(f"[ERROR] facility metadata: {e}")
        return {}
    result = {}
    for raw_id, raw_level in raw.items():
        try:
            facility_id, level = int(raw_id), int(raw_level)
        except (TypeError, ValueError):
            continue
        spec = specs.get(facility_id)
        if spec is not None:
            # VIPの「施設 指定」は通常の+上限を超える値も許可する。
            # 基礎Lvは適用時に正規上限へ収め、残りをすべて+値へ回す。
            result[facility_id] = max(1, level)
    return result


def safe_vip_talent_orbs(data):
    raw = data.get("vip_talent_orbs", {})
    if not isinstance(raw, dict):
        return {}
    try:
        valid_ids = {
            rank["id"]
            for group in get_legend_metadata().get("talent_orbs", [])
            for rank in group.get("ranks", [])
        }
    except Exception as e:
        print(f"[ERROR] talent orb metadata: {e}")
        return {}
    result = {}
    for raw_id, raw_amount in raw.items():
        try:
            orb_id, amount = int(raw_id), int(raw_amount)
        except (TypeError, ValueError):
            continue
        if orb_id in valid_ids:
            result[orb_id] = max(0, min(amount, 998))
    return result


def apply_vip_facilities(save, facilities):
    logs = []
    if facilities is None:
        return logs
    try:
        specs = {item["id"]: item for item in get_legend_metadata().get("facilities", [])}
        valid_skills = save.special_skills.get_valid_skills()
        for facility_id, level in facilities.items():
            if facility_id >= len(valid_skills) or facility_id not in specs:
                continue
            spec = specs[facility_id]
            base_level = min(level, spec["max_base"])
            plus_level = max(level - spec["max_base"], 0)
            valid_skills[facility_id].upgrade.base = max(base_level - 1, 0)
            valid_skills[facility_id].upgrade.plus = plus_level
            # にゃんこ砲攻撃力にはセーブ内部のミラー枠がある。
            if facility_id == 0 and len(save.special_skills.skills) > 1:
                save.special_skills.skills[1].upgrade.base = max(base_level - 1, 0)
                save.special_skills.skills[1].upgrade.plus = plus_level
            logs.append(f"{spec['name']}(Lv.{level})")
    except Exception as e:
        print(f"[ERROR] vip facilities: {e}")
    return logs


def apply_vip_talent_orbs(save, talent_orbs):
    logs = []
    for orb_id, amount in (talent_orbs or {}).items():
        try:
            save.talent_orbs.set_orb(orb_id, amount)
            logs.append(f"本能玉ID{orb_id}({amount})")
        except Exception as e:
            print(f"[ERROR] vip talent orb {orb_id}: {e}")
    if logs:
        return [f"本能玉({len(logs)}種類)"]
    return []


def _ensure_zero_map(save, map_id):
    """古いテンプレートにも最新の零レジェンド章スロットを安全に追加する。"""
    chapters = save.zero_legends.chapters
    if not chapters:
        return False
    while len(chapters) <= map_id:
        chapters.append(copy.deepcopy(chapters[0]))
        for chapter in chapters[-1].chapters:
            chapter.selected_stage = 0
            chapter.clear_progress = 0
            chapter.unlock_state = 0
            if hasattr(chapter, "chapter_unlock_state"):
                chapter.chapter_unlock_state = 0
            for stage in chapter.stages:
                stage.clear_times = 0
    return True


def _sync_idi_clear(save):
    """古代研究所★4クリア時にイディ関連の取得状態を矛盾なく揃える。"""
    try:
        idi = save.cats.get_cat_by_id(568)
        if idi:
            idi.unlock(save)
        elif len(save.unit_drops) > 202:
            save.unit_drops[202] = 1
    except Exception as e:
        print(f"[ERROR] idi unlock: {e}")
    try:
        save.medals.add_medal(86)  # 【古代生命体撃破】
    except Exception as e:
        print(f"[ERROR] idi medal: {e}")
    try:
        # RE_026「太古の力」の報酬取得状態。配列があるバージョンだけ同期する。
        reward = save.item_reward_stages.sub_chapters[26].sub_chapters[0].stages[0]
        reward.claimed = True
    except (AttributeError, IndexError):
        pass


def apply_legend_stages(save, selections):
    """選択された章・冠・ステージだけをクリアする。"""
    logs = []
    for series_key, maps in (selections or {}).items():
        applied = 0
        for map_id, stars in maps.items():
            for star, stage_ids in stars.items():
                for stage_id in stage_ids:
                    try:
                        if series_key == "legend":
                            chapter = save.event_stages.chapters[0].chapters[map_id].chapters[star]
                            if stage_id >= len(chapter.stages):
                                continue
                            chapter.chapter_unlock_state = 3
                            chapter.clear_progress = max(chapter.clear_progress, stage_id + 1)
                            chapter.stages[stage_id].clear_stage(1, ensure_cleared_only=True)
                        elif series_key == "true_legend":
                            chapter = save.uncanny.chapters.chapters[map_id].chapters[star]
                            if stage_id >= len(chapter.stages):
                                continue
                            chapter.chapter_unlock_state = 3
                            chapter.clear_progress = max(chapter.clear_progress, stage_id + 1)
                            chapter.stages[stage_id].clear_stage(1, ensure_cleared_only=True)
                        elif series_key == "zero_legend":
                            if not _ensure_zero_map(save, map_id):
                                continue
                            chapter = save.zero_legends.chapters[map_id].chapters[star]
                            if stage_id >= len(chapter.stages):
                                continue
                            chapter.unlock_state = 3
                            chapter.chapter_unlock_state = 3
                            chapter.clear_progress = max(chapter.clear_progress, stage_id + 1)
                            chapter.stages[stage_id].clear_stage(1, ensure_cleared_only=True)
                        else:
                            continue
                        applied += 1
                    except (AttributeError, IndexError) as e:
                        print(f"[ERROR] {series_key}[{map_id}][{star}][{stage_id}]: {e}")

        if applied:
            label = LEGEND_SERIES[series_key]["label"]
            logs.append(f"{label}({applied}ステージ冠)")

    # レジェンド最終章「古代研究所」★4「太古の力」を選んだ時だけ同期する。
    idi_stages = selections.get("legend", {}).get(48, {}).get(3, []) if selections else []
    if 0 in idi_stages:
        _sync_idi_clear(save)
        logs.append("イディ取得・撃破済み")
    return logs


def safe_main_story_chapters(data):
    """main_story_chapters を検証済みの整数リスト(表示ポジション0〜8)として返す。不正・未指定なら None(=全章)。"""
    raw = data.get("main_story_chapters")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    try:
        indices = sorted({int(v) for v in raw if 0 <= int(v) <= 8})
    except (TypeError, ValueError):
        return None
    return indices if indices else None


def _safe_clear_count(value):
    try:
        return max(1, min(MAX_STAGE_CLEAR_COUNT, int(value)))
    except (TypeError, ValueError):
        return 1


def safe_main_story_stages(data):
    """章→ステージ→クリア回数。キーが存在する空辞書も親の全クリアを抑止する。"""
    if "main_story_stages" not in data:
        return None
    raw = data.get("main_story_stages")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for raw_position, raw_stages in raw.items():
        try:
            position = int(raw_position)
        except (TypeError, ValueError):
            continue
        if not 0 <= position < len(MAIN_STORY_REAL_INDEX) or not isinstance(raw_stages, dict):
            continue
        stages = {}
        for raw_stage, raw_count in raw_stages.items():
            try:
                stage = int(raw_stage)
            except (TypeError, ValueError):
                continue
            if 0 <= stage < 48:
                stages[stage] = _safe_clear_count(raw_count)
        result[position] = stages
    return result


def safe_event_stage_settings(data):
    """イベント種別→マップ→冠→ステージ→クリア回数を安全な整数へ整形する。"""
    labyrinth_characters = safe_labyrinth_character_settings(data)
    if "event_stage_settings" not in data and labyrinth_characters is None:
        return None
    raw = data.get("event_stage_settings")
    if not isinstance(raw, dict):
        raw = {}
    result = {}
    for family_key, raw_maps in raw.items():
        if family_key not in EVENT_STAGE_FAMILIES or not isinstance(raw_maps, dict):
            continue
        maps = {}
        for raw_map, raw_stars in raw_maps.items():
            try:
                map_id = int(raw_map)
            except (TypeError, ValueError):
                continue
            if not 0 <= map_id < 1000 or not isinstance(raw_stars, dict):
                continue
            stars = {}
            for raw_star, raw_stages in raw_stars.items():
                try:
                    star = int(raw_star)
                except (TypeError, ValueError):
                    continue
                if not 0 <= star < 4 or not isinstance(raw_stages, dict):
                    continue
                stages = {}
                for raw_stage, raw_count in raw_stages.items():
                    try:
                        stage = int(raw_stage)
                    except (TypeError, ValueError):
                        continue
                    max_stages = 256 if family_key == "labyrinth" else 64
                    if 0 <= stage < max_stages:
                        stages[stage] = _safe_clear_count(raw_count)
                if stages:
                    stars[star] = stages
            if stars:
                maps[map_id] = stars
        result[family_key] = maps
    if labyrinth_characters is not None:
        result["_labyrinth_characters"] = labyrinth_characters
    return result


def safe_labyrinth_character_settings(data):
    """画面表示と同じ1始まりのキャラ番号を重複なしで正規化する。"""
    raw = data.get("labyrinth_characters")
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in LABYRINTH_CHARACTER_ACTIONS:
        return None
    ids = []
    seen = set()
    for raw_id in raw.get("ids", [])[:MAX_LABYRINTH_CHARACTERS]:
        try:
            display_id = max(1, min(9999, int(raw_id)))
        except (TypeError, ValueError):
            continue
        if display_id in seen:
            continue
        seen.add(display_id)
        ids.append(display_id)
    if action.endswith("_selected") and not ids:
        return None
    return {"action": action, "ids": ids}


def safe_special_stages(data):
    """ゾンビ襲来・魔界編の詳細選択を検証する。空選択も親処理を抑止するため保持する。"""
    raw = data.get("special_stages")
    if not isinstance(raw, dict):
        return None
    result = {}
    if "zombie" in raw:
        zombie_raw = raw.get("zombie")
        zombie = {}
        if isinstance(zombie_raw, dict):
            for raw_chapter, raw_stages in zombie_raw.items():
                try:
                    chapter = int(raw_chapter)
                except (TypeError, ValueError):
                    continue
                if not 0 <= chapter < len(MAIN_STORY_REAL_INDEX) or not isinstance(raw_stages, list):
                    continue
                try:
                    zombie[chapter] = sorted({int(stage) for stage in raw_stages if 0 <= int(stage) < 48})
                except (TypeError, ValueError):
                    zombie[chapter] = []
        result["zombie"] = zombie
    if "aku" in raw:
        aku_raw = raw.get("aku")
        if isinstance(aku_raw, list):
            try:
                result["aku"] = sorted({int(stage) for stage in aku_raw if 0 <= int(stage) < 64})
            except (TypeError, ValueError):
                result["aku"] = []
        else:
            result["aku"] = []
    if "aku_ex" in raw:
        aku_ex_raw = raw.get("aku_ex")
        if isinstance(aku_ex_raw, list):
            try:
                result["aku_ex"] = sorted({int(stage) for stage in aku_ex_raw if int(stage) == 0})
            except (TypeError, ValueError):
                result["aku_ex"] = []
        else:
            result["aku_ex"] = []
    return result


def _sync_jagando_clear(save):
    """富士山EX（破壊神ジャガンドー）のクリア・報酬・キャラ取得を同期する。"""
    try:
        save.event_stages.clear_stage(
            1, 42, 0, 0, clear_amount=1, ensure_cleared_only=True
        )
    except (AttributeError, IndexError) as e:
        print(f"[ERROR] jagando stage: {e}")
    try:
        jagando_jr = save.cats.get_cat_by_id(622)
        if jagando_jr:
            jagando_jr.unlock(save)
    except Exception as e:
        print(f"[ERROR] jagando jr unlock: {e}")
    try:
        reward = save.item_reward_stages.sub_chapters[42].sub_chapters[0].stages[0]
        reward.claimed = True
    except (AttributeError, IndexError):
        pass


def apply_main_story_stages(save, selections):
    """選択されたメインステージだけを指定回数クリアし、お宝を最高にする。"""
    logs = []
    if selections is None:
        return logs
    applied = 0
    for position, stages in selections.items():
        if not 0 <= position < len(MAIN_STORY_REAL_INDEX):
            continue
        real_id = MAIN_STORY_REAL_INDEX[position]
        if real_id >= len(save.story.chapters):
            continue
        chapter = save.story.chapters[real_id]
        for stage_id, clear_count in stages.items():
            if stage_id >= min(48, len(chapter.stages)):
                continue
            try:
                chapter.clear_stage(stage_id, clear_count)
                chapter.stages[stage_id].treasure = 3
                applied += 1
            except (AttributeError, IndexError) as e:
                print(f"[ERROR] main_story_stages[{position}][{stage_id}]: {e}")
    if applied:
        logs.append(f"メイン詳細({applied}ステージ)")
    return logs


def apply_dojo_score_settings(save, settings):
    """常設道場の保存領域へスコアを設定する（BCSFEの道場編集と同じ0:0）。"""
    if not settings:
        return []
    try:
        score = int(settings["hall_of_initiates"])
        save.dojo.chapters.get_stage(0, 0).score = score
        return [f"道場・入門の間スコア({score})"]
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        print(f"[ERROR] dojo score: {e}")
        return []


def apply_future_score_settings(save, settings):
    """未来編の各章48ステージへ採点スコアを設定する。"""
    if not settings:
        return []
    logs = []
    for position, score in settings.items():
        if position not in {3, 4, 5}:
            continue
        real_id = MAIN_STORY_REAL_INDEX[position]
        try:
            chapter = save.story.chapters[real_id]
            stages = chapter.get_valid_treasure_stages()
            for stage in stages:
                stage.itf_timed_score = int(score)
            logs.append(f"{MAIN_STORY_CHAPTER_LABELS[position]}採点スコア({score}・{len(stages)}ステージ)")
        except (AttributeError, IndexError, TypeError, ValueError) as e:
            print(f"[ERROR] future score chapter {position}: {e}")
    return logs


def _labyrinth_value(save, names, default=None):
    """BCSFEが地底迷宮へ正式名を付けた後も、現行の未命名フィールドへ後方互換する。"""
    owners = [getattr(save, "labyrinth", None), save]
    for owner in owners:
        if owner is None:
            continue
        for name in names:
            if hasattr(owner, name):
                return getattr(owner, name)
    return default


def _set_labyrinth_value(save, names, value):
    owners = [getattr(save, "labyrinth", None), save]
    changed = False
    for owner in owners:
        if owner is None:
            continue
        for name in names:
            if hasattr(owner, name):
                setattr(owner, name, value.copy() if isinstance(value, list) else value)
                changed = True
    return changed


def _labyrinth_unlocked_cat_ids(save):
    return {
        int(cat.id) for cat in getattr(getattr(save, "cats", None), "cats", [])
        if bool(getattr(cat, "unlocked", False))
    }


def _sync_labyrinth_remaining(save, trapped=None, active=None):
    trapped = set(trapped if trapped is not None else _labyrinth_value(
        save, ("trapped_cats", "sealed_cats", "unavailable_cats", "ushl3"), []
    ))
    active = set(active if active is not None else _labyrinth_value(
        save, ("active_cats", "current_cats", "surviving_cats", "ushl4"), []
    ))
    unlocked = _labyrinth_unlocked_cat_ids(save)
    remaining = max(0, len(unlocked - trapped - active))
    _set_labyrinth_value(save, ("available_cat_count", "remaining_cat_count", "ush6"), remaining)
    _set_labyrinth_value(save, ("available_cat_count_2", "remaining_cat_count_2", "ush8"), remaining)
    return remaining


def _remove_labyrinth_lineup_cats(save, cat_ids):
    """地底迷宮専用編成（現行セーブではスロット20）から封印対象だけを外す。"""
    if not cat_ids:
        return
    try:
        slots = save.lineups.slots[20].slots
    except (AttributeError, IndexError):
        return
    encoded_ids = {cat_id + 2 for cat_id in cat_ids}
    for slot in slots:
        if getattr(slot, "cat_id", -1) in encoded_ids:
            slot.cat_id = -1


def apply_lineup_settings(save, settings):
    """通常編成1〜20の指定された10枠を書き換える。同一キャラの重複も保持する。"""
    if not settings:
        return []
    try:
        lineup_index = int(settings.get("lineup", 1)) - 1
        lineups = getattr(getattr(save, "lineups", None), "slots", [])
        # 現行セーブの21番目は地底迷宮専用。通常編成としては1〜20だけを扱う。
        if not 0 <= lineup_index < min(20, len(lineups)):
            return ["編成設定エラー(指定編成が存在しません)"]
        slots = lineups[lineup_index].slots
        cat_ids = list(settings.get("cats", []))[:10]
        cat_ids.extend([None] * (10 - len(cat_ids)))
        forms = list(settings.get("forms", []))[:10]
        forms.extend([None] * (10 - len(forms)))
        form_counts = _character_form_counts()
        true_form_states = _character_true_form_states()
        applied = 0
        for slot_index, display_id in enumerate(cat_ids):
            if slot_index >= len(slots):
                break
            if display_id is None:
                slots[slot_index].cat_id = -1
                continue
            display_id = int(display_id)
            cat = save.cats.get_cat_by_id(display_id - 1)
            if cat is None:
                slots[slot_index].cat_id = -1
                continue
            # 編成だけを設定する操作で外部ゲームデータ取得を発生させない。
            # 未解放キャラが選ばれた場合も編成が無効化されないよう必要フラグを立てる。
            cat.unlocked = 1
            cat.gatya_seen = 1
            if forms[slot_index] is not None:
                _set_cat_form(
                    save, cat, form_counts.get(display_id, 1), forms[slot_index],
                    true_form_states.get(display_id, 2),
                )
            # 編成内の保存値は、画面表示の1始まりキャラ番号に+1した値。
            slots[slot_index].cat_id = display_id + 1
            applied += 1
        save.lineups.selected_slot = lineup_index
        save.unlock_equip_menu()
        return [f"編成{lineup_index + 1}設定({applied}体・空枠{10 - applied})"]
    except Exception as e:
        print(f"[ERROR] lineup settings: {e}")
        return ["編成設定エラー"]


def apply_labyrinth_characters(save, settings):
    """地底迷宮の封印キャラを、最新キャラ数に追従しながら封印・解除する。"""
    if not settings:
        return []
    action = settings.get("action")
    if action not in LABYRINTH_CHARACTER_ACTIONS:
        return []

    trapped_names = ("trapped_cats", "sealed_cats", "unavailable_cats", "ushl3")
    active_names = ("active_cats", "current_cats", "surviving_cats", "ushl4")
    trapped = set(_labyrinth_value(save, trapped_names, []))
    active_order = list(dict.fromkeys(_labyrinth_value(save, active_names, [])))
    active = set(active_order)
    unlocked = _labyrinth_unlocked_cat_ids(save)
    selected = {
        display_id - 1 for display_id in settings.get("ids", [])
        if display_id >= 1 and display_id - 1 in unlocked
    }

    changed_ids = set()
    if action == "unlock_selected":
        changed_ids = trapped & selected
        trapped -= selected
    elif action == "unlock_all":
        changed_ids = set(trapped)
        trapped.clear()
    elif action == "seal_selected":
        changed_ids = selected - trapped
        trapped |= selected
        removed_active = active & selected
        if removed_active:
            active_order = [cat_id for cat_id in active_order if cat_id not in removed_active]
            active -= removed_active
            _remove_labyrinth_lineup_cats(save, removed_active)
    elif action == "seal_all":
        changed_ids = unlocked - trapped
        trapped = set(unlocked)
        _remove_labyrinth_lineup_cats(save, active)
        active_order = []
        active.clear()
    elif action == "release_lineup_all":
        changed_ids = set(active)
        _remove_labyrinth_lineup_cats(save, active)
        active_order = []
        active.clear()

    # 同じIDが封印中と現編成へ同時に入る状態は作らない。
    active_order = [cat_id for cat_id in active_order if cat_id not in trapped]
    active = set(active_order)
    trapped &= unlocked
    _set_labyrinth_value(save, trapped_names, sorted(trapped))
    _set_labyrinth_value(save, active_names, active_order)
    remaining = _sync_labyrinth_remaining(save, trapped, active)

    if hasattr(save, "ub25"):
        save.ub25 = True
    labels = {
        "unlock_selected": "指定封印解除",
        "unlock_all": "全封印解除",
        "seal_selected": "指定キャラ封印",
        "seal_all": "全キャラ封印",
        "release_lineup_all": "編成中キャラ全解除",
    }
    return [f"地底迷宮 {labels[action]}({len(changed_ids)}体・残り{remaining}体)"]


def apply_labyrinth_stages(save, maps):
    """選択された最深階までを連続クリアとして同期する。"""
    selected_floors = set()
    for map_id, stars in (maps or {}).items():
        if map_id != 0:
            continue
        for star, stages in stars.items():
            if star == 0:
                selected_floors.update(int(stage_id) for stage_id in stages)
    if not selected_floors:
        return []

    floor_order = _labyrinth_value(save, ("floor_order", "stage_order", "ushl2"), [])
    max_floors = len(floor_order) or 100
    requested = min(max_floors, max(selected_floors) + 1)
    current = int(_labyrinth_value(save, ("current_floor", "cleared_floor", "ush4"), 0) or 0)
    target = max(current, requested)

    stage_ids = list(getattr(save, "stage_ids_10s", []))
    known = set(stage_ids)
    for floor_index in range(target):
        stage_key = 33000000 + floor_index * 10
        if stage_key not in known:
            stage_ids.append(stage_key)
            known.add(stage_key)
    save.stage_ids_10s = stage_ids

    _set_labyrinth_value(save, ("current_floor", "cleared_floor", "ush4"), target)
    _set_labyrinth_value(save, ("highest_floor", "max_cleared_floor", "ush5"), target)
    _set_labyrinth_value(save, ("display_floor", "progress_floor", "ush7"), target)
    _set_labyrinth_value(save, ("remaining_floor_index", "uby10"), max(-128, min(127, max_floors - 1 - target)))
    if hasattr(save, "ui17"):
        save.ui17 = 33000
    if hasattr(save, "ub24"):
        save.ub24 = True
    if hasattr(save, "ub25"):
        save.ub25 = True
    remaining = _sync_labyrinth_remaining(save)
    return [f"地底迷宮 地底{target}層までクリア(残り{remaining}体)"]


def apply_event_stage_settings(save, selections):
    """イベント・塔・強襲・超獣などをステージ別の指定回数でクリアする。"""
    logs = []
    if selections is None:
        return logs
    labyrinth_characters = selections.get("_labyrinth_characters")
    for family_key, maps in selections.items():
        if family_key == "_labyrinth_characters":
            continue
        spec = EVENT_STAGE_FAMILIES.get(family_key)
        if not spec:
            continue
        if spec["kind"] == "labyrinth":
            logs.extend(apply_labyrinth_stages(save, maps))
            continue
        applied = 0
        for map_id, stars in maps.items():
            for star, stages in stars.items():
                for stage_id, clear_count in stages.items():
                    try:
                        if spec["kind"] == "event":
                            save.event_stages.clear_stage(
                                spec["group"], map_id, star, stage_id,
                                clear_amount=clear_count,
                            )
                        elif spec["kind"] == "tower":
                            save.tower.chapters.clear_stage(
                                map_id, star, stage_id, clear_count
                            )
                        elif spec["kind"] == "catamin_stages":
                            chapters = save.catamin_stages.chapters
                            _ensure_standard_chapter_map(chapters, map_id)
                            chapters.clear_stage(
                                map_id, star, stage_id, clear_count
                            )
                            # ネコビタンステージは通常のステージ別回数に加えて、
                            # マップ単位の表示回数も別dictへ保存される。
                            completion_key = spec["base_index"] + map_id
                            save.event_stages.chapter_completion_count[completion_key] = max(
                                clear_count,
                                save.event_stages.chapter_completion_count.get(completion_key, 0),
                            )
                        elif spec["kind"] == "dojo_chapters":
                            save.dojo_chapters.create(map_id)
                            chapter = save.dojo_chapters.chapters[map_id].chapters[star]
                            chapter.clear_progress = max(chapter.clear_progress, stage_id + 1)
                            chapter.stages[stage_id].clear_times = clear_count
                            chapter.unlock_state = 3
                        else:
                            container = getattr(save, spec["kind"])
                            _ensure_gauntlet_map(container, map_id)
                            container.clear_stage(map_id, star, stage_id, clear_count)
                        applied += 1
                    except (AttributeError, IndexError, TypeError) as e:
                        print(f"[ERROR] event_stage_settings[{family_key}][{map_id}][{star}][{stage_id}]: {e}")
        if applied:
            logs.append(f"{spec['label']}({applied}ステージ)")
    logs.extend(apply_labyrinth_characters(save, labyrinth_characters))
    return logs


def apply_special_stages(save, selections):
    """選択されたゾンビ襲来・魔界編ステージだけをクリアする。"""
    logs = []
    if selections is None:
        return logs

    if "zombie" in selections:
        applied = 0
        zombie = selections.get("zombie", {})
        for position, stage_ids in zombie.items():
            if not 0 <= position < len(MAIN_STORY_REAL_INDEX):
                continue
            chapter_key = MAIN_STORY_REAL_INDEX[position]
            if chapter_key not in save.outbreaks.chapters:
                save.outbreaks.chapters[chapter_key] = ZombieChapter(chapter_key, {})
            chapter = save.outbreaks.chapters[chapter_key]
            current_chapter = save.outbreaks.current_outbreaks.get(chapter_key)
            for stage_id in stage_ids:
                chapter.outbreaks[stage_id] = ZombieOutbreak(True)
                if current_chapter is not None:
                    current_chapter.outbreaks.pop(stage_id, None)
                applied += 1
            if current_chapter is not None and not current_chapter.outbreaks:
                save.outbreaks.current_outbreaks.pop(chapter_key, None)
        if applied:
            logs.append(f"ゾンビ({applied}ステージ)")

    if "aku" in selections:
        applied_ids = set()
        stage_ids = selections.get("aku", [])
        if hasattr(save, "aku") and save.aku and save.aku.chapters:
            for chapters_stars in save.aku.chapters:
                for chapter in chapters_stars.chapters:
                    valid_ids = [stage_id for stage_id in stage_ids if stage_id < len(chapter.stages)]
                    for stage_id in valid_ids:
                        chapter.stages[stage_id].clear_times = 1
                        applied_ids.add(stage_id)
                    if valid_ids:
                        chapter.current_stage = max(chapter.current_stage, max(valid_ids))
        if applied_ids:
            logs.append(f"魔界編({len(applied_ids)}ステージ)")
    if "aku_ex" in selections and 0 in selections.get("aku_ex", []):
        _sync_jagando_clear(save)
        logs.append("富士山EX(破壊神ジャガンドー)")
    return logs


def apply_ototo_settings(save, ototo_settings):
    """個別指定された城だけを更新する。part 0はセーブ上だけ0始まり。"""
    if not ototo_settings:
        return []
    applied = []
    name_by_id = {
        int(item["id"]): item.get("name", f"城強化 #{item['id']}")
        for item in get_ototo_metadata().get("cannons", [])
    }
    for raw_cannon_id, setting in ototo_settings.items():
        try:
            cannon_id = int(raw_cannon_id)
            cannon = save.ototo.cannons.cannons.get(cannon_id)
            if cannon is None:
                cannon = core.game.gamoto.ototo.Cannon(0, [])
                save.ototo.cannons.cannons[cannon_id] = cannon
            required_size = 1 if cannon_id == 0 else 3
            levels = list(cannon.levels)
            while len(levels) < required_size:
                levels.append(0)
            changed_parts = []
            for raw_part_id, raw_level in setting.get("levels", {}).items():
                part_id = int(raw_part_id)
                display_level = int(raw_level)
                if not (0 <= part_id < required_size):
                    continue
                levels[part_id] = display_level - 1 if part_id == 0 else display_level
                changed_parts.append(display_level)
            if not changed_parts:
                continue
            cannon.levels = levels[:required_size]
            if cannon_id != 0:
                cannon.development = 3
            applied.append(f"{name_by_id.get(cannon_id, f'城強化 #{cannon_id}')}個別設定")
        except Exception as e:
            print(f"[ERROR] ototo detail {raw_cannon_id}: {e}")
    return applied


def apply_daiko_segment(save, selected_items: list, char_list: list, custom_amounts: dict, custom_playtime: str = "", main_story_chapters=None, character_settings=None) -> list:
    applied_logs = []
    save.show_ban_message = False

    def amt(key):
        return int(custom_amounts.get(key, 0))

    if "catfood" in selected_items:
        save.catfood = 58000; applied_logs.append("猫缶(58000)")
    elif "custom_catfood" in selected_items:
        save.catfood = amt("custom_catfood"); applied_logs.append(f"猫缶({amt('custom_catfood')})")

    if "xp" in selected_items:
        save.xp = 99999999; applied_logs.append("XP(99999999)")
    elif "custom_xp" in selected_items:
        save.xp = amt("custom_xp"); applied_logs.append(f"XP({amt('custom_xp')})")

    if "np" in selected_items:
        save.np = 9999; applied_logs.append("NP(9999)")
    elif "custom_np" in selected_items:
        save.np = amt("custom_np"); applied_logs.append(f"NP({amt('custom_np')})")

    if "normal_tickets" in selected_items:
        save.normal_tickets = 999; applied_logs.append("銀チケ(999)")
    elif "custom_normal_tickets" in selected_items:
        save.normal_tickets = amt("custom_normal_tickets"); applied_logs.append(f"銀チケ({amt('custom_normal_tickets')})")

    if "rare_tickets" in selected_items:
        save.rare_tickets = 999; applied_logs.append("金チケ(999)")
    elif "custom_rare_tickets" in selected_items:
        save.rare_tickets = amt("custom_rare_tickets"); applied_logs.append(f"金チケ({amt('custom_rare_tickets')})")

    if "platinum_tickets" in selected_items:
        save.platinum_tickets = 20; applied_logs.append("プラチナ(20)")
    elif "custom_platinum_tickets" in selected_items:
        save.platinum_tickets = amt("custom_platinum_tickets"); applied_logs.append(f"プラチナ({amt('custom_platinum_tickets')})")

    if "legend_tickets" in selected_items:
        save.legend_tickets = 10; applied_logs.append("レジェンドチケ(10)")
    elif "custom_legend_tickets" in selected_items:
        save.legend_tickets = amt("custom_legend_tickets"); applied_logs.append(f"レジェンドチケ({amt('custom_legend_tickets')})")

    if "leadership" in selected_items:
        save.leadership = 999; applied_logs.append("リーダーシップ(999)")
    elif "custom_leadership" in selected_items:
        save.leadership = amt("custom_leadership"); applied_logs.append(f"リーダーシップ({amt('custom_leadership')})")

    if "catseyes" in selected_items:
        try: save.catseyes = [999] * len(save.catseyes); applied_logs.append("キャッツアイ(999)")
        except Exception as e: print(f"[ERROR] catseyes: {e}")
    elif "custom_catseyes" in selected_items:
        try: save.catseyes = [amt("custom_catseyes")] * len(save.catseyes); applied_logs.append(f"キャッツアイ({amt('custom_catseyes')})")
        except Exception as e: print(f"[ERROR] custom_catseyes: {e}")

    if "catamins" in selected_items:
        try: save.catamins = [999] * len(save.catamins); applied_logs.append("ネコビタン(999)")
        except Exception as e: print(f"[ERROR] catamins: {e}")
    elif "custom_catamins" in selected_items:
        try: save.catamins = [amt("custom_catamins")] * len(save.catamins); applied_logs.append(f"ネコビタン({amt('custom_catamins')})")
        except Exception as e: print(f"[ERROR] custom_catamins: {e}")

    if "base_materials" in selected_items:
        try:
            if hasattr(save, 'ototo') and hasattr(save.ototo, 'base_materials'):
                for material in save.ototo.base_materials.materials: material.amount = 999
                applied_logs.append("城素材(999)")
        except Exception as e: print(f"[ERROR] base_materials: {e}")
    elif "custom_base_materials" in selected_items:
        try:
            if hasattr(save, 'ototo') and hasattr(save.ototo, 'base_materials'):
                for material in save.ototo.base_materials.materials: material.amount = amt("custom_base_materials")
                applied_logs.append(f"城素材({amt('custom_base_materials')})")
        except Exception as e: print(f"[ERROR] custom_base_materials: {e}")

    if "matatabi" in selected_items:
        try: save.catfruit = [998] * len(save.catfruit); applied_logs.append("マタタビ(998)")
        except Exception as e: print(f"[ERROR] matatabi: {e}")
    elif "custom_matatabi" in selected_items:
        try: save.catfruit = [amt("custom_matatabi")] * len(save.catfruit); applied_logs.append(f"マタタビ({amt('custom_matatabi')})")
        except Exception as e: print(f"[ERROR] custom_matatabi: {e}")

    if "talent_orbs" in selected_items:
        try:
            orb_ids = _latest_talent_orb_ids()
            for orb_id in orb_ids:
                save.talent_orbs.set_orb(orb_id, 998)
            applied_logs.append(f"本能玉({len(orb_ids)}種類・998)")
        except Exception as e: print(f"[ERROR] talent_orbs: {e}")
    elif "custom_talent_orbs" in selected_items:
        try:
            orb_ids = _latest_talent_orb_ids()
            for orb_id in orb_ids:
                save.talent_orbs.set_orb(orb_id, amt("custom_talent_orbs"))
            applied_logs.append(f"本能玉({len(orb_ids)}種類・{amt('custom_talent_orbs')})")
        except Exception as e: print(f"[ERROR] custom_talent_orbs: {e}")

    if "battle_items" in selected_items:
        try:
            for b_item in save.battle_items.items: b_item.amount = 9999
            applied_logs.append("バトルアイテム(9999)")
        except Exception as e: print(f"[ERROR] battle_items: {e}")
    elif "custom_battle_items" in selected_items:
        try:
            for b_item in save.battle_items.items: b_item.amount = amt("custom_battle_items")
            applied_logs.append(f"バトルアイテム({amt('custom_battle_items')})")
        except Exception as e: print(f"[ERROR] custom_battle_items: {e}")

    if "main_story_clear" in selected_items:
        try:
            chapters = save.story.chapters
            if main_story_chapters is None:
                target_indices = list(range(len(chapters)))
                selected_positions = list(range(len(MAIN_STORY_CHAPTER_LABELS)))
            else:
                selected_positions = [p for p in main_story_chapters if p < len(MAIN_STORY_REAL_INDEX)]
                target_indices = [MAIN_STORY_REAL_INDEX[p] for p in selected_positions if MAIN_STORY_REAL_INDEX[p] < len(chapters)]
            for idx in target_indices:
                chapter = chapters[idx]
                chapter.clear_chapter()
                for i in range(len(chapter.stages)):
                    if i < 48: chapter.stages[i].treasure = 3
            if main_story_chapters is None or len(selected_positions) == len(MAIN_STORY_CHAPTER_LABELS):
                applied_logs.append("メインクリア")
            else:
                names = [MAIN_STORY_CHAPTER_LABELS[p] for p in selected_positions if p < len(MAIN_STORY_CHAPTER_LABELS)]
                applied_logs.append(f"メインクリア({'/'.join(names)})")
        except Exception as e: print(f"[ERROR] main_story_clear: {e}")

    if "zombie_clear" in selected_items:
        try:
            for chapter_key in [0, 1, 2, 4, 5, 6, 7, 8, 9]:
                if chapter_key not in save.outbreaks.chapters:
                    save.outbreaks.chapters[chapter_key] = ZombieChapter(chapter_key, {})
                chapter = save.outbreaks.chapters[chapter_key]
                for stage_id in range(48):
                    chapter.outbreaks[stage_id] = ZombieOutbreak(True)
            save.outbreaks.current_outbreaks = {}
            applied_logs.append("ゾンビ")
        except Exception as e:
            print(f"[ERROR] zombie_clear: {e}")

    if "aku_clear" in selected_items:
        try:
            if hasattr(save, 'aku') and save.aku and save.aku.chapters:
                for chapters_stars in save.aku.chapters:
                    for chapter in chapters_stars.chapters:
                        if chapter.stages and len(chapter.stages) > 0:
                            chapter.current_stage = len(chapter.stages) - 1
                            for stage in chapter.stages:
                                stage.clear_times = 1
                _sync_jagando_clear(save)
            applied_logs.append("魔界編")
        except Exception as e: print(f"[ERROR] aku_clear: {e}")

    if "legend_clear" in selected_items:
        try: applied_logs.extend(apply_legend_stages(save, full_legend_selection("legend")))
        except Exception as e: print(f"[ERROR] legend_clear: {e}")

    if "true_legend_clear" in selected_items:
        try:
            applied_logs.extend(apply_legend_stages(save, full_legend_selection("true_legend")))
        except Exception as e: print(f"[ERROR] true_legend_clear: {e}")

    if "zero_legend_clear" in selected_items:
        try:
            applied_logs.extend(apply_legend_stages(save, full_legend_selection("zero_legend")))
        except Exception as e: print(f"[ERROR] zero_legend_clear: {e}")

    if "event_clear" in selected_items:
        try:
            for group_id in range(len(save.event_stages.chapters)): save.event_stages.clear_group(group_id)
            applied_logs.append("イベント")
        except Exception as e: print(f"[ERROR] event_clear: {e}")

    if "gamatoto_max" in selected_items:
        try:
            g_levels = core.core_data.get_gamatoto_levels(save)
            save.gamatoto.xp = g_levels.get_xp_from_level(g_levels.get_max_level())
            applied_logs.append("ガマトト")
        except Exception as e: print(f"[ERROR] gamatoto_max: {e}")

    if "gamatoto_helpers" in selected_items:
        try:
            new_helpers = []
            for i in range(129, 139): new_helpers.append(core.game.gamoto.gamatoto.Helper(i))
            save.gamatoto.helpers.helpers = new_helpers
            applied_logs.append("助手")
        except Exception as e: print(f"[ERROR] gamatoto_helpers: {e}")

    if "ototo_max" in selected_items:
        try:
            for cannon_id, cannon in save.ototo.cannons.cannons.items():
                cannon.development = 3
                if cannon_id == 0: cannon.levels = [29]
                else: cannon.levels = [29, 20, 20]
            applied_logs.append("オトート")
        except Exception as e: print(f"[ERROR] ototo_max: {e}")

    if "cat_shrine_max" in selected_items:
        try:
            save.cat_shrine.xp_offering = 100000000
            applied_logs.append("神社")
        except Exception as e: print(f"[ERROR] cat_shrine_max: {e}")

    if "gold_membership" in selected_items:
        try:
            save.officer_pass.gold_pass.get_gold_pass(114514, 365, save)
            applied_logs.append("ゴールド")
        except Exception as e: print(f"[ERROR] gold_membership: {e}")

    if "slots_max" in selected_items:
        try: save.lineups.unlocked_slots = 19; applied_logs.append("スロット")
        except Exception as e: print(f"[ERROR] slots_max: {e}")

    if "medals_all" in selected_items:
        try:
            medal_names = core.core_data.get_medal_names(save)
            if medal_names.medal_names:
                for i in range(len(medal_names.medal_names)): save.medals.add_medal(i)
            applied_logs.append("メダル")
        except Exception as e: print(f"[ERROR] medals_all: {e}")

    if "skip_tutorial" in selected_items:
        try: save.story.clear_tutorial(save); applied_logs.append("チュートリアル")
        except Exception as e: print(f"[ERROR] skip_tutorial: {e}")

    if "hide_rank_up" in selected_items:
        try:
            save.unlock_popups_11 = [1] * 3
            if hasattr(save, 'max_rank_up_sale'): save.max_rank_up_sale()
            else: save.rank_up_sale_value = 0x7FFFFFFF
            user_rank = save.calculate_user_rank()
            rank_gifts = save.user_rank_rewards.read_rank_gifts(save)
            if rank_gifts and rank_gifts.rank_gift:
                for rank_gift in rank_gifts.rank_gift:
                    if rank_gift.threshold <= user_rank:
                        save.user_rank_rewards.set_claimed(rank_gift.index, False)
            applied_logs.append("ポップアップ非表示")
        except Exception as e: print(f"[ERROR] hide_rank_up: {e}")

    if "user_rank_rewards_claimed" in selected_items:
        try:
            changed, eligible = _set_all_user_rank_rewards(save, True)
            applied_logs.append(f"UR報酬全受取({eligible}件・変更{changed}件)")
        except Exception as e:
            print(f"[ERROR] user_rank_rewards_claimed: {e}")

    if "user_rank_rewards_unclaimed" in selected_items:
        try:
            changed, eligible = _set_all_user_rank_rewards(save, False)
            applied_logs.append(f"UR報酬全未受取(変更{changed}件)")
        except Exception as e:
            print(f"[ERROR] user_rank_rewards_unclaimed: {e}")

    if "catguide_rewards_claimed" in selected_items:
        try:
            changed, targets = _set_all_catguide_rewards(save, True)
            applied_logs.append(f"猫図鑑報酬全受取({targets}体・変更{changed}体)")
        except Exception as e:
            print(f"[ERROR] catguide_rewards_claimed: {e}")

    if "catguide_rewards_unclaimed" in selected_items:
        try:
            changed, targets = _set_all_catguide_rewards(save, False)
            applied_logs.append(f"猫図鑑報酬全未受取(変更{changed}体)")
        except Exception as e:
            print(f"[ERROR] catguide_rewards_unclaimed: {e}")

    if "cat_scratcher_reset" in selected_items:
        try:
            # 実機の未実施データでも開催日(start_times)は残る。
            # BCSFE reset()はそれまで消すため、結果と完了フラグだけ戻す。
            save.cat_scratcher.completed = {}
            save.cat_scratcher.values = {}
            applied_logs.append("スクラッチ未実施化")
        except Exception as e:
            print(f"[ERROR] cat_scratcher_reset: {e}")

    if "unlock_enemy_guide" in selected_items:
        try:
            enemy_dict = core.game.battle.enemy.EnemyDictionary(save)
            valid_enemies = enemy_dict.get_valid_enemies()
            if valid_enemies:
                for enemy_id in valid_enemies:
                    save.enemy_guide[enemy_id] = 1
                applied_logs.append("敵図鑑全解放")
        except Exception as e: print(f"[ERROR] unlock_enemy_guide: {e}")

    if "add_playtime_720h" in selected_items:
        try:
            current_play_time = PlayTime(save.officer_pass.play_time)
            added_time = PlayTime.from_hours(720)
            new_play_time = current_play_time + added_time
            save.officer_pass.play_time = new_play_time.frames
            applied_logs.append("プレイ時間+720h")
        except Exception as e: print(f"[ERROR] add_playtime_720h: {e}")
    elif "custom_playtime" in selected_items:
        try:
            parsed = validate_playtime_str(str(custom_playtime))
            if parsed:
                hours, minutes = parsed
                save.officer_pass.play_time = PlayTime.from_hours_mins_secs(hours, minutes, 0).frames
                applied_logs.append(f"プレイ時間({hours}:{minutes:02d})")
            else:
                print("[ERROR] custom_playtime: 不正な形式のためスキップ")
        except Exception as e: print(f"[ERROR] custom_playtime: {e}")

    if "all_missions_clear" in selected_items:
        try:
            completed = _complete_all_defined_missions(save)
            applied_logs.append(f"全ミッションクリア({completed}件・全種類)")
        except Exception as e:
            print(f"[ERROR] all_missions_clear: {e}")
            traceback.print_exc()

    if "base_upgrades_max" in selected_items:
        try:
            for i in range(len(save.special_skills.skills)):
                if i == 2: save.special_skills.skills[i].upgrade.base = 9; save.special_skills.skills[i].upgrade.plus = 0
                else: save.special_skills.skills[i].upgrade.base = 19; save.special_skills.skills[i].upgrade.plus = 10
            applied_logs.append("全施設Max")
        except Exception as e: print(f"[ERROR] base_upgrades_max: {e}")
    elif "custom_base_upgrades" in selected_items:
        try:
            v = amt("custom_base_upgrades")
            for i in range(len(save.special_skills.skills)):
                if i == 2:
                    save.special_skills.skills[i].upgrade.base = max(v - 1, 0)
                    save.special_skills.skills[i].upgrade.plus = 0
                else:
                    base_level = min(max(v - 1, 0), 19)
                    plus_level = max(v - (base_level + 1), 0)
                    save.special_skills.skills[i].upgrade.base = base_level
                    save.special_skills.skills[i].upgrade.plus = plus_level
            applied_logs.append(f"全施設({v})")
        except Exception as e: print(f"[ERROR] custom_base_upgrades: {e}")

    if "unlock_all_cats" in selected_items:
        try:
            for cat in save.cats.cats: cat.unlock(save)
            applied_logs.append("全解放")
        except Exception as e: print(f"[ERROR] unlock_all_cats: {e}")

    if char_list:
        char_applied = []
        for char_val in char_list:
            try:
                cat_id = int(char_val) - 1
                cat = save.cats.get_cat_by_id(cat_id)
                if not cat: continue
                if "unlock_specific" in selected_items: cat.unlock(save); char_applied.append(f"解放({char_val})")
                if "remove_specific" in selected_items: cat.remove(reset=True, save_file=save); char_applied.append(f"削除({char_val})")
            except: pass
        if char_applied: applied_logs.extend(char_applied)

    # 名前選択UIの指定キャラ。従来の1始まりID入力とは独立して併用できる。
    if character_settings:
        char_applied = []
        form_counts = _character_form_counts()
        true_form_states = _character_true_form_states()
        talent_definitions = _talent_definitions_by_cat() if "talents_specific" in selected_items else {}
        for setting in character_settings:
            try:
                display_id = int(setting["id"])
                cat = save.cats.get_cat_by_id(display_id - 1)
                if not cat:
                    continue
                if "unlock_specific" in selected_items:
                    cat.unlock(save)
                    char_applied.append(f"解放({display_id})")
                if "remove_specific" in selected_items:
                    cat.remove(reset=True, save_file=save)
                    char_applied.append(f"削除({display_id})")
                    continue
                if "level_specific" in selected_items:
                    cat.unlock(save)
                    if setting.get("level_mode") == "random":
                        base_min = int(setting.get("base_level_min", 1))
                        base_max = int(setting.get("base_level_max", 60))
                        plus_min = int(setting.get("plus_level_min", 0))
                        plus_max = int(setting.get("plus_level_max", 90))
                        base_level = base_min + secrets.randbelow(base_max - base_min + 1)
                        plus_level = plus_min + secrets.randbelow(plus_max - plus_min + 1)
                    else:
                        base_level = int(setting["base_level"])
                        plus_level = int(setting["plus_level"])
                    cat.upgrade.base = max(0, base_level - 1)
                    cat.upgrade.plus = max(0, plus_level)
                    char_applied.append(
                        f"Lv.{base_level}+{plus_level}({display_id})"
                    )
                if "form_specific" in selected_items:
                    form = _set_cat_form(
                        save,
                        cat,
                        form_counts.get(display_id, 1),
                        int(setting["form"]),
                        true_form_states.get(display_id, 2),
                    )
                    char_applied.append(f"第{form}形態({display_id})")
                if "max_specific" in selected_items:
                    cat.unlock(save)
                    cat.upgrade.base = 59
                    cat.upgrade.plus = 90
                    char_applied.append(f"Lv.Max+({display_id})")
                if "max_specific_2" in selected_items:
                    cat.unlock(save)
                    cat.upgrade.base = 59
                    char_applied.append(f"Lv.Max({display_id})")
                if "true_form_specific" in selected_items:
                    form = _set_cat_latest_form(save, cat, form_counts, true_form_states)
                    if form is None:
                        char_applied.append(f"最終形態スキップ・形態定義未取得({display_id})")
                    else:
                        char_applied.append(f"最終形態・第{form}形態({display_id})")
                if "talents_specific" in selected_items:
                    if not cat.unlocked:
                        char_applied.append(f"本能スキップ・未解放({display_id})")
                    else:
                        applied = _apply_character_talents(
                            cat, display_id, setting, talent_definitions
                        )
                        if applied:
                            action_label = {
                                "set": "本能設定",
                                "disable_selected": "選択本能無効",
                                "disable_all": "全本能無効",
                            }.get(setting.get("talent_action"), "本能設定")
                            char_applied.append(f"{action_label}({display_id}: {applied}個)")
            except Exception as e:
                print(f"[ERROR] character_settings: {e}")
        if char_applied:
            applied_logs.extend(char_applied)

    if "max_all_cats_plus" in selected_items:
        try:
            for cat in save.cats.cats:
                if cat.unlocked:
                    cat.upgrade.base = 59; cat.upgrade.plus = 90
            applied_logs.append("全Lv.Max+")
        except Exception as e: print(f"[ERROR] max_all_cats_plus: {e}")

    if "max_all_cats" in selected_items:
        try:
            for cat in save.cats.cats:
                if cat.unlocked:
                    cat.upgrade.base = 59
            applied_logs.append("全Lv.Max")
        except Exception as e: print(f"[ERROR] max_all_cats: {e}")

    if "true_form_all" in selected_items:
        try:
            unlocked_cats = [cat for cat in save.cats.cats if cat.unlocked]
            form_counts = _character_form_counts()
            true_form_states = _character_true_form_states()
            for cat in unlocked_cats:
                _set_cat_latest_form(save, cat, form_counts, true_form_states)
            skipped = sum(1 for cat in unlocked_cats if cat.id + 1 not in form_counts)
            applied_logs.append(
                f"全最大形態(形態定義未取得{skipped}体を除外)" if skipped else "全最大形態"
            )
        except Exception as e: print(f"[ERROR] true_form_all: {e}")

    if "talents_all" in selected_items:
        try:
            talent_data = core.TalentData.from_game_data(save)
            if talent_data:
                for cat in save.cats.cats:
                    if not cat.unlocked: continue
                    cat_skill = talent_data.get_cat_skill(cat.id)
                    if cat_skill:
                        if cat.talents is None:
                            cat.talents = []
                        for skill in cat_skill.skills:
                            if skill.ability_id <= 0 or skill.text_id <= 0:
                                continue
                            max_lv = skill.max_lv if skill.max_lv != 0 else 1
                            existing_talent = next(
                                (t for t in cat.talents if t.id == skill.ability_id), None
                            )
                            if existing_talent:
                                existing_talent.level = max_lv
                            else:
                                cat.talents.append(Talent(skill.ability_id, max_lv))
                applied_logs.append("全本能")
            else:
                applied_logs.append("エラー: 本能データロード失敗")
        except Exception as e:
            print(f"[ERROR] talents_all: {e}")

    if "talents_disable_all" in selected_items:
        try:
            disabled = 0
            for cat in save.cats.cats:
                if cat.talents is None:
                    continue
                for talent in cat.talents:
                    if talent.level:
                        talent.level = 0
                        disabled += 1
            applied_logs.append(f"全キャラ本能無効({disabled}個)")
        except Exception as e:
            print(f"[ERROR] talents_disable_all: {e}")

    if char_list:
        char_applied = []
        for char_val in char_list:
            try:
                cat_id = int(char_val) - 1
                cat = save.cats.get_cat_by_id(cat_id)
                if not cat or not cat.unlocked: continue
                if "max_specific" in selected_items: cat.upgrade.base = 59; cat.upgrade.plus = 90; char_applied.append(f"Lv.Max+({char_val})")
                if "max_specific_2" in selected_items: cat.upgrade.base = 59; char_applied.append(f"Lv.Max({char_val})")
                if "true_form_specific" in selected_items:
                    form = _set_cat_latest_form(save, cat)
                    if form is None:
                        char_applied.append(f"最終形態スキップ・形態定義未取得({char_val})")
                    else:
                        char_applied.append(f"最終形態・第{form}形態({char_val})")
            except: pass
        if char_applied: applied_logs.extend(char_applied)

    if "remove_error_cats" in selected_items:
        try:
            removed_count = 0
            for err_id in ERROR_CAT_IDS:
                cat = save.cats.get_cat_by_id(err_id - 1)
                if cat: cat.remove(reset=True, save_file=save); removed_count += 1
            if removed_count > 0:
                applied_logs.append(f"エラーキャラ削除({removed_count}体)")
        except Exception as e: print(f"[ERROR] remove_error_cats: {e}")

    return applied_logs


def _apply_all_segments_unlocked(save_file, selected, char_list, custom_amounts, custom_playtime="", main_story_chapters=None, vip_items=None, legend_stages=None, vip_facilities=None, vip_talent_orbs=None, special_stages=None, character_settings=None, main_story_stages=None, event_stage_settings=None, lineup_settings=None, ototo_settings=None, dojo_score_settings=None, future_score_settings=None):
    final_logs = apply_vip_items(save_file, vip_items)
    final_logs.extend(apply_legend_stages(save_file, legend_stages))
    final_logs.extend(apply_vip_facilities(save_file, vip_facilities))
    final_logs.extend(apply_vip_talent_orbs(save_file, vip_talent_orbs))
    final_logs.extend(apply_special_stages(save_file, special_stages))
    final_logs.extend(apply_main_story_stages(save_file, main_story_stages))
    final_logs.extend(apply_event_stage_settings(save_file, event_stage_settings))
    final_logs.extend(apply_lineup_settings(save_file, lineup_settings))
    final_logs.extend(apply_ototo_settings(save_file, ototo_settings))
    final_logs.extend(apply_dojo_score_settings(save_file, dojo_score_settings))
    final_logs.extend(apply_future_score_settings(save_file, future_score_settings))
    s1 = selected.get("s1", [])
    s2 = selected.get("s2", [])
    s3 = selected.get("s3", [])
    s4 = selected.get("s4", [])
    for i, segment in enumerate([s1, s2, s3, s4], 1):
        if not segment and (i != 3 or not char_list): continue
        items = []
        for item in segment:
            if item == "all_stage_clear":
                items.extend(["main_story_clear", "zombie_clear", "aku_clear",
                               "legend_clear", "true_legend_clear", "zero_legend_clear", "event_clear"])
            else:
                items.append(item)
        # VIP個別指定がある親項目は、従来の「全配列を一括更新」処理から除外する。
        if vip_items:
            granular_values = set().union(*(
                VIP_ITEM_SELECTED_VALUES.get(group_key, set()) for group_key in vip_items
            ))
            items = [item for item in items if item not in granular_values]
        # 詳細選択が送られたシリーズは従来の全クリア処理を抑止する。
        if legend_stages is not None:
            granular_legend_values = {
                LEGEND_SELECTED_VALUES[key] for key in legend_stages if key in LEGEND_SELECTED_VALUES
            }
            items = [item for item in items if item not in granular_legend_values]
        if special_stages is not None:
            granular_special_values = {
                "zombie": "zombie_clear", "aku": "aku_clear", "aku_ex": "aku_clear"
            }
            suppressed = {granular_special_values[key] for key in special_stages if key in granular_special_values}
            items = [item for item in items if item not in suppressed]
        if main_story_stages is not None:
            items = [item for item in items if item != "main_story_clear"]
        if event_stage_settings is not None:
            items = [item for item in items if item != "event_clear"]
        if vip_facilities is not None:
            items = [item for item in items if item not in {"base_upgrades_max", "custom_base_upgrades"}]
        if vip_talent_orbs is not None:
            items = [item for item in items if item not in {"talent_orbs", "custom_talent_orbs"}]
        if ototo_settings is not None:
            items = [item for item in items if item not in {"ototo_max", "ototo_detailed"}]
        final_logs.extend(apply_daiko_segment(
            save_file,
            list(dict.fromkeys(items)),
            char_list if i == 3 else [],
            custom_amounts,
            custom_playtime,
            main_story_chapters,
            character_settings if i == 3 else None,
        ))
    # 形態を扱う操作時だけ既存の不可能な状態も修復する。通常のアイテム・
    # ステージ操作で、配布metadataより新しい実機形態を触らないための制限。
    form_actions = {"form_specific", "true_form_specific", "true_form_all"}
    should_repair_forms = any(form_actions.intersection(segment) for segment in (s1, s2, s3, s4))
    if should_repair_forms or lineup_settings is not None:
        repaired_forms = _normalise_cat_form_flags(save_file)
        if repaired_forms:
            final_logs.append(f"形態・進化権自動修正({repaired_forms}体)")
    return final_logs


def _apply_all_segments(save_file, selected, char_list, custom_amounts, custom_playtime="", main_story_chapters=None, vip_items=None, legend_stages=None, vip_facilities=None, vip_talent_orbs=None, special_stages=None, character_settings=None, main_story_stages=None, event_stage_settings=None, lineup_settings=None, ototo_settings=None, dojo_score_settings=None, future_score_settings=None):
    """BCSFEのsave依存グローバルキャッシュを別ジョブと混在させず適用する。"""
    with BCSFE_EDIT_LOCK:
        ensure_latest_save_schema(save_file)
        # BCSFE 3.6.0はgetterやchara_drop等をプロセス全体でキャッシュする。
        # すべて同じ最新版へ固定し、saveを保持する派生キャッシュはジョブごとに破棄する。
        core.core_data.game_data_getter = _latest_jp_game_data_getter()
        for cache_name in (
            "gatya_item_names", "gatya_item_buy", "chara_drop", "gamatoto_levels",
            "gamatoto_members_name", "localizable", "abilty_data", "enemy_names",
            "rank_gift_descriptions", "rank_gifts", "treasure_text", "cat_shrine_levels",
            "medal_names", "mission_names", "mission_conditions",
        ):
            if hasattr(core.core_data, cache_name):
                setattr(core.core_data, cache_name, None)
        return _apply_all_segments_unlocked(
            save_file, selected, char_list, custom_amounts, custom_playtime,
            main_story_chapters, vip_items, legend_stages, vip_facilities,
            vip_talent_orbs, special_stages, character_settings,
            main_story_stages, event_stage_settings, lineup_settings, ototo_settings,
            dojo_score_settings, future_score_settings,
        )


def run_job_daiko(job_id, operation_id, transfer_code, auth_code, selected, char_list, custom_amounts, custom_playtime="", main_story_chapters=None, vip_items=None, legend_stages=None, vip_facilities=None, vip_talent_orbs=None, special_stages=None, character_settings=None, main_story_stages=None, event_stage_settings=None, lineup_settings=None, ototo_settings=None, dojo_score_settings=None, future_score_settings=None, access_tier="free"):
    def update(d):
        d["updated_at"] = time.time()
        _update_job(job_id, d)
    handler = None
    checkpoint_codes = None
    final_issue_started = False
    try:
        update({"status": "running", "started_at": time.time(), "log": "サーバーに接続中..."})
        _update_operation(operation_id, status="running", error=None)

        cc = core.CountryCode.from_code("jp")
        gv = core.GameVersion(TARGET_GAME_VERSION_NUMBER)

        char_list = [c.strip() for c in char_list if str(c).strip().isdigit()]

        handler, result = ServerHandler.from_codes(
            transfer_code, auth_code, cc, gv, save_backup=False
        )
        if handler is None:
            update({"status": "error", "error": "エラー: コード無効。"})
            _close_operation(
                operation_id,
                status="error",
                recovery_status="not_available",
                error="コード無効のため、セーブデータは取得されていません。",
            )
            return

        update({
            "transfer_received": True,
            "log": "引き継ぎ取得済み・復旧用コードを保存中...",
        })
        _save_operation_snapshot(operation_id, handler.save_file)

        # 入力コードはfrom_codes成功時点で消費される。編集・保存中に
        # プロセスが落ちてもアカウントを回収できるよう、変更前データで
        # 復旧用コードを先に発行し、SQLiteのジョブ記録へ即時保存する。
        checkpoint_codes = handler.get_codes(tries=2)
        if not checkpoint_codes:
            update({
                "status": "error",
                "transfer_received": True,
                "admin_recovery_required": True,
                "error": (
                    "引き継ぎ取得後、作業前の復旧用コードを発行できませんでした。"
                    "同じ操作を再実行せず管理者へ連絡してください。"
                ),
            })
            _update_operation(
                operation_id, status="error",
                error="引き継ぎ取得後、作業前の復旧用コードを発行できませんでした。",
                recovery_status="reissue_failed",
            )
            return
        update({
            "recovery_transfer_code": checkpoint_codes[0],
            "recovery_auth_code": checkpoint_codes[1],
            "admin_recovery_required": False,
            "log": "復旧用コード保存済み・データを適用中...",
        })

        final_logs = _apply_all_segments(handler.save_file, selected, char_list, custom_amounts, custom_playtime, main_story_chapters, vip_items, legend_stages, vip_facilities, vip_talent_orbs, special_stages, character_settings, main_story_stages, event_stage_settings, lineup_settings, ototo_settings, dojo_score_settings, future_score_settings)
        # 最終発行だけ失敗した時は、編集済みデータから管理者が再発行できるよう更新。
        _save_operation_snapshot(operation_id, handler.save_file)

        update({"log": "サーバーに保存中..."})
        final_issue_started = True
        codes = handler.get_codes()
        if codes:
            t, a = codes
            total_count = increment_usage_count()
            update({
                "status": "done", "transfer_code": t, "auth_code": a,
                "applied": final_logs, "recovery_transfer_code": None,
                "recovery_auth_code": None, "admin_recovery_required": False,
            })
            _close_operation(
                operation_id, status="done", recovery_status="not_needed"
            )
            send_usage_log(access_tier, final_logs, total_count, USAGE_DB_PATH)
        else:
            # 最終POSTはサーバー側だけ成功して応答が失われた可能性がある。
            # 直後に自動再発行すると既知のコードまで無効化し得るため、最新の
            # 編集済みスナップショットを残して管理パネルから再発行する。
            error_text = (
                "引き継ぎ取得後の保存通信を確認できませんでした。"
                "同じ操作を再実行せず、管理パネルから引き継ぎコードを再発行してください。"
            )
            update({
                "status": "error",
                "error": error_text,
                "transfer_received": True,
                "admin_recovery_required": True,
            })
            _update_operation(
                operation_id,
                status="error",
                error=error_text,
                recovery_status="reissue_failed",
            )

    except Exception as e:
        payload = {"status": "error", "error": _safe_error_message(e)}
        # 最終発行を始める前ならチェックポイントコードは確実に既知。
        # 発行開始後は応答喪失の可能性があるため、再送せずsnapshotを残す。
        if checkpoint_codes and not final_issue_started:
            payload.update({
                "recovery_transfer_code": checkpoint_codes[0],
                "recovery_auth_code": checkpoint_codes[1],
                "admin_recovery_required": False,
                "error": "処理中にエラーが発生しました。引き継ぎコードを確認してください。",
            })
        elif handler is not None:
            payload.update({
                "transfer_received": True,
                "admin_recovery_required": True,
                "error": (
                    "引き継ぎ取得後にエラーが発生しました。"
                    "同じ操作を再実行せず、管理パネルから引き継ぎコードを再発行してください。"
                ),
            })
        if checkpoint_codes and not final_issue_started:
            payload["admin_recovery_required"] = False
            _close_operation(
                operation_id,
                status="error",
                recovery_status="client_code_issued",
                error=payload["error"],
                issued_codes=checkpoint_codes,
            )
        elif handler is None:
            payload["admin_recovery_required"] = False
            _close_operation(
                operation_id,
                status="error",
                recovery_status="not_available",
                error=payload["error"],
            )
        else:
            payload["admin_recovery_required"] = True
            _update_operation(
                operation_id, status="error", error=payload["error"],
                recovery_status="reissue_failed",
            )
        update(payload)
        print(traceback.format_exc())


def run_job_create(job_id, selected, char_list, custom_amounts, count=1, custom_playtime="", account_type_key="new", main_story_chapters=None, vip_items=None, legend_stages=None, vip_facilities=None, vip_talent_orbs=None, special_stages=None, character_settings=None, main_story_stages=None, event_stage_settings=None, lineup_settings=None, ototo_settings=None, dojo_score_settings=None, future_score_settings=None, access_tier="free"):
    def update(d):
        d["updated_at"] = time.time()
        _update_job(job_id, d)
    try:
        update({"status": "running", "started_at": time.time(), "log": "アカウントを作成中..."})

        cc = core.CountryCode.from_code("jp")
        gv = core.GameVersion(TARGET_GAME_VERSION_NUMBER)

        char_list = [c.strip() for c in char_list if str(c).strip().isdigit()]

        filename = ACCOUNT_TYPE_FILES.get(account_type_key, "Nyanko_new")
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if not os.path.exists(path):
            update({"status": "error", "error": f"エラー: {filename} が見つかりません。"})
            return

        count = max(1, min(MAX_COUNT, int(count)))
        accounts = []
        final_logs = []

        for i in range(count):
            update({"log": f"アカウントを作成中... ({i+1}/{count})"})
            save = core.SaveFile(core.Data.from_file(core.Path(path)), cc=cc)
            ensure_latest_save_schema(save)
            handler = ServerHandler(save)
            if not handler.create_new_account():
                update({"status": "error", "error": f"エラー: アカウント作成失敗 ({i+1}個目)。"})
                return

            if i == 0:
                update({"log": "データを適用中..."})
                final_logs = _apply_all_segments(handler.save_file, selected, char_list, custom_amounts, custom_playtime, main_story_chapters, vip_items, legend_stages, vip_facilities, vip_talent_orbs, special_stages, character_settings, main_story_stages, event_stage_settings, lineup_settings, ototo_settings, dojo_score_settings, future_score_settings)
            else:
                _apply_all_segments(handler.save_file, selected, char_list, custom_amounts, custom_playtime, main_story_chapters, vip_items, legend_stages, vip_facilities, vip_talent_orbs, special_stages, character_settings, main_story_stages, event_stage_settings, lineup_settings, ototo_settings, dojo_score_settings, future_score_settings)

            codes = handler.get_codes()
            if codes:
                accounts.append({"tc": codes[0], "ac": codes[1]})
            else:
                update({"status": "error", "error": f"エラー: 保存失敗 ({i+1}個目)。"})
                return

        total_count = increment_usage_count()
        update({
            "status": "done",
            "accounts": accounts,
            "transfer_code": accounts[0]["tc"],
            "auth_code": accounts[0]["ac"],
            "applied": final_logs,
        })
        send_usage_log(
            access_tier,
            [f"新規アカウント作成 ×{len(accounts)}", *final_logs],
            total_count,
            USAGE_DB_PATH,
        )

    except Exception as e:
        update({"status": "error", "error": str(e)})
        print(traceback.format_exc())


class SafeCloneServerHandler(ServerHandler):
    """Create at most one copy account and snapshot it as soon as its ID exists."""

    def __init__(self, save_file, snapshot_callback=None, print=True):
        super().__init__(save_file, print=print)
        self.snapshot_callback = snapshot_callback
        self.account_assigned = False

    def get_password(self, tries=0):
        # BCSFE's default fallback calls create_new_account() again when account
        # setup communication fails. A clone must never silently abandon the
        # first issued account ID and create another one.
        password = self.get_stored_password()
        if password is not None:
            return password
        password = self.refresh_password()
        if password is not None:
            return password
        return self.get_password_new()

    def create_new_account(self, tries=1):
        new_inquiry_code = self.get_new_inquiry_code()
        if new_inquiry_code is None:
            return False

        self.save_file.inquiry_code = new_inquiry_code
        self.account_assigned = True
        self.remove_stored_auth_token()
        self.remove_stored_save_key_data()
        self.remove_stored_password()
        fail_text = "EXPECT_THIS_TO_FAIL"
        start_count = (40 - len(fail_text)) // 2
        end_count = 40 - len(fail_text) - start_count
        self.save_file.password_refresh_token = (
            "_" * start_count + fail_text + "_" * end_count
        )

        # ID発行直後に永続化。以降の認証通信が失敗しても管理パネルから
        # 同じコピーアカウントの再発行を試せる。
        if self.snapshot_callback is not None:
            self.snapshot_callback(self.save_file)

        password = self.get_password()
        auth_token = self.get_auth_token()
        save_key_data = self.get_save_key()
        self.update_managed_items()
        self.save_file.show_ban_message = False
        return password is not None and auth_token is not None and save_key_data is not None


def run_job_clone(job_id, operation_id, transfer_code, auth_code, count=1, access_tier="free"):
    def update(d):
        d["updated_at"] = time.time()
        _update_job(job_id, d)
    handler = None
    orig_codes = None
    copies = []
    active_copy_handler = None
    copy_recovery_pending = False
    try:
        update({"status": "running", "started_at": time.time(), "log": "サーバーに接続中..."})
        _update_operation(operation_id, status="running", error=None)

        cc = core.CountryCode.from_code("jp")
        gv = core.GameVersion(TARGET_GAME_VERSION_NUMBER)

        handler, result = ServerHandler.from_codes(
            transfer_code, auth_code, cc, gv, save_backup=False
        )
        if handler is None:
            update({"status": "error", "error": "エラー: コード無効。"})
            _close_operation(
                operation_id,
                status="error",
                recovery_status="not_available",
                error="コード無効のため、セーブデータは取得されていません。",
            )
            return

        update({"transfer_received": True, "log": "引き継ぎ取得済み・元アカウントを保存中..."})
        _save_operation_snapshot(operation_id, handler.save_file)

        update({"log": "セーブデータを取得中..."})
        save_data = handler.save_file.data

        # 最終転送POSTの応答が失われた場合、直後の再送は最初に発行された
        # コードを無効化し得る。BCSFE内部の段階別試行だけに限定する。
        orig_codes = handler.get_codes()
        if not orig_codes:
            error_text = (
                "引き継ぎ取得後、元アカウントの復旧コードを発行できませんでした。"
                "同じ操作を連続実行せず管理者へ連絡してください。"
            )
            update({
                "status": "error",
                "error": error_text,
                "transfer_received": True,
                "admin_recovery_required": True,
            })
            _update_operation(operation_id, status="error", error=error_text, recovery_status="reissue_failed")
            return

        # コピー作成中に後続処理が失敗しても、ここで確定した
        # 元アカウントの最新コードをエラー画面から回収できるよう保持する。
        update({
            "recovery_transfer_code": orig_codes[0],
            "recovery_auth_code": orig_codes[1],
            "admin_recovery_required": False,
            "log": "元アカウント保護済み・コピーを作成中...",
        })
        count = max(1, min(MAX_COUNT, int(count)))

        for i in range(count):
            copy_recovery_pending = True
            update({
                "log": f"コピーアカウントを作成中... ({i+1}/{count})",
                "return_pending": True,
            })
            copy_save = core.SaveFile(core.Data(save_data), cc=cc)
            ensure_latest_save_schema(copy_save)
            active_copy_handler = SafeCloneServerHandler(
                copy_save,
                snapshot_callback=lambda save: _save_operation_snapshot(operation_id, save),
            )
            if not active_copy_handler.create_new_account():
                error_text = f"エラー: コピーアカウント作成失敗 ({i+1}個目)。"
                if active_copy_handler.account_assigned:
                    # 新しいIDは既に発行済み。snapshotを破棄せず、同じIDの
                    # データから管理パネルで再発行できる状態にする。
                    update({
                        "status": "error", "error": error_text,
                        "admin_recovery_required": True,
                        "return_pending": False,
                    })
                    _update_operation(
                        operation_id,
                        status="error",
                        recovery_status="reissue_failed",
                        error=error_text,
                    )
                else:
                    copy_recovery_pending = False
                    update({
                        "status": "error", "error": error_text,
                        "admin_recovery_required": False,
                        "return_pending": False,
                    })
                    _close_operation(
                        operation_id,
                        status="error",
                        recovery_status="client_code_issued",
                        error=error_text,
                        issued_codes=orig_codes,
                    )
                return

            update({"log": f"コピーアカウントを保存中... ({i+1}/{count})"})
            _save_operation_snapshot(operation_id, active_copy_handler.save_file)
            copy_codes = active_copy_handler.get_codes()
            if not copy_codes:
                update({
                    "status": "error",
                    "error": f"エラー: コピーアカウントの保存失敗 ({i+1}個目)。",
                    "admin_recovery_required": True,
                    "return_pending": False,
                })
                _update_operation(
                    operation_id, status="error", recovery_status="reissue_failed",
                    error=f"コピーアカウントの保存失敗 ({i+1}個目)。",
                )
                return
            copies.append({"tc": copy_codes[0], "ac": copy_codes[1]})
            copy_recovery_pending = False
            update({"return_pending": False, "copies": copy.deepcopy(copies)})
            # このコピーは返却済みなので、次のコピー作成中は元アカウントを復旧対象に戻す。
            _save_operation_snapshot(operation_id, handler.save_file)
            active_copy_handler = None

        total_count = increment_usage_count()
        update({
            "status": "done",
            "orig_transfer_code": orig_codes[0],
            "orig_auth_code": orig_codes[1],
            "copies": copies,
            "copy_transfer_code": copies[0]["tc"],
            "copy_auth_code": copies[0]["ac"],
            "admin_recovery_required": False,
        })
        _close_operation(
            operation_id, status="done", recovery_status="not_needed"
        )
        send_usage_log(
            access_tier,
            [f"アカウント複製 ×{len(copies)}"],
            total_count,
            USAGE_DB_PATH,
        )

    except Exception as e:
        payload = {"status": "error", "error": _safe_error_message(e)}
        if orig_codes:
            payload.update({
                "recovery_transfer_code": orig_codes[0],
                "recovery_auth_code": orig_codes[1],
                "copies": copy.deepcopy(copies),
            })
        if handler is not None:
            payload.update({
                "transfer_received": True,
                "error": (
                    "引き継ぎ取得後に複製エラーが発生しました。"
                    "同じ操作を再実行せず、表示されたコードまたは管理パネルを使用してください。"
                ),
            })

        # コピーID発行後・コード返却前なら、そのコピーsnapshotを必ず保持。
        # 元アカウントの既知コードを例外処理で再発行して無効化しない。
        if copy_recovery_pending and active_copy_handler is not None:
            try:
                if active_copy_handler.account_assigned:
                    _save_operation_snapshot(operation_id, active_copy_handler.save_file)
            except Exception as snapshot_error:
                print(f"[ERROR] clone snapshot: {snapshot_error}")
            payload["admin_recovery_required"] = True
            payload["return_pending"] = False
            _update_operation(
                operation_id,
                status="error",
                error=payload["error"],
                recovery_status="reissue_failed",
            )
        elif orig_codes:
            payload["admin_recovery_required"] = False
            _close_operation(
                operation_id,
                status="error",
                recovery_status="client_code_issued",
                error=payload["error"],
                issued_codes=orig_codes,
            )
        elif handler is not None:
            payload["admin_recovery_required"] = True
            _update_operation(
                operation_id, status="error", error=payload["error"],
                recovery_status="reissue_failed",
            )
        else:
            payload["admin_recovery_required"] = False
            _close_operation(
                operation_id,
                status="error",
                recovery_status="not_available",
                error=payload["error"],
            )
        update(payload)
        print(traceback.format_exc())


class RecoveryServerHandler(ServerHandler):
    """Admin recovery must never silently turn the save into a new account."""
    def create_new_account(self, tries=3):
        return False


def run_admin_reissue(operation_id: str):
    record = _load_operation(operation_id)
    if not record or not record.get("snapshot"):
        _update_operation(
            operation_id, status="error", recovery_status="reissue_failed",
            error="復旧用データが見つかりません。",
        )
        return
    try:
        raw = gzip.decompress(_decrypt_snapshot(record["snapshot"], operation_id))
        cc = core.CountryCode.from_code("jp")
        with BCSFE_EDIT_LOCK:
            save_file = core.SaveFile(core.Data(raw), cc=cc)
            inquiry_before = str(save_file.inquiry_code)
            handler = RecoveryServerHandler(save_file, print=False)
            codes = handler.get_codes(tries=2)
            inquiry_after = str(handler.save_file.inquiry_code)
        if inquiry_after != inquiry_before:
            raise RuntimeError("復旧中にアカウントIDが変化したため中断しました。")
        if not codes:
            raise RuntimeError("引き継ぎコードを発行できませんでした。")
        job = _load_job(record["job_id"])
        if job:
            _update_job(record["job_id"], {
                "recovery_transfer_code": codes[0],
                "recovery_auth_code": codes[1],
                "updated_at": time.time(),
            })
        _update_operation(
            operation_id, status="done", error=None,
            recovery_status="admin_reissued",
            # 成功後は二重発行を防ぐためsnapshotを破棄。
            snapshot=None,
            reissue_result=_encrypt_snapshot(
                json.dumps({"tc": codes[0], "ac": codes[1]}, separators=(",", ":")).encode(),
                operation_id,
            ),
        )
    except Exception as exc:
        # スナップショットは残し、通信復旧後の再試行を可能にする。
        original_error = str(record.get("error") or "").strip()
        reissue_error = _safe_error_message(exc)
        combined_error = f"{original_error}\n管理者再発行: {reissue_error}".strip()[:2000]
        _update_operation(operation_id, recovery_status="reissue_failed", error=combined_error)
        print(f"[ERROR] admin reissue {operation_id}: {exc}")


# =====================
# APIエンドポイント
# =====================
def _reserve_free_quota(data: dict, job_id: str):
    """無料版の実行1回分を原子的に予約する。"""
    try:
        free_usage_manager.reserve(
            real_ip(),
            data.get("fingerprint", ""),
            job_id,
            allow_month_end_unlimited=bool(current_site_user()),
        )
        return job_id, None
    except QuotaExhausted as exc:
        return None, (jsonify({"error": str(exc), "code": "quota_exhausted"}), 402)
    except IdentityConflict as exc:
        return None, (jsonify({"error": str(exc), "code": "identity_conflict"}), 409)
    except FreeUsageError as exc:
        return None, (jsonify({"error": str(exc), "code": "identity_invalid"}), 400)
    except sqlite3.Error:
        return None, (jsonify({"error": "利用回数を確認できませんでした。少し待ってから再実行してください。"}), 503)


def _reserve_site_vip_job(job_id: str):
    """有料VIPを再確認し、無料体験なら実行1回を原子的に予約する。"""
    user = current_site_user()
    if not user or not user.get("is_vip"):
        return None, (jsonify({"error": "VIP利用権を確認できませんでした。"}), 403)
    try:
        result = account_store.reserve_vip_job(user["id"], job_id)
        return result.get("reservation_id"), None
    except AccountPermissionError as exc:
        return None, (jsonify({"error": str(exc), "code": "vip_trial_unavailable"}), 409)
    except sqlite3.Error:
        return None, (jsonify({"error": "VIP利用権を確認できませんでした。少し待ってから再実行してください。"}), 503)


def _refund_unattached_quota(reservation_id: str | None) -> None:
    """ジョブ保存前の失敗で、紐付けられなかった予約を返却する。"""
    if not reservation_id:
        return
    try:
        free_usage_manager.settle(reservation_id, success=False)
    except Exception as exc:
        print(f"[ERROR] free quota refund {reservation_id}: {type(exc).__name__}")


def _refund_unattached_vip_trial(reservation_id: str | None) -> None:
    """ジョブ保存前の失敗で、予約したVIP体験1回を返却する。"""
    if not reservation_id:
        return
    try:
        account_store.settle_vip_job(reservation_id, success=False)
    except Exception as exc:
        print(f"[ERROR] VIP trial refund {reservation_id}: {type(exc).__name__}")


def _discard_unstarted_job(job_id: str) -> None:
    """ワーカー投入前に失敗したジョブのメモリ・永続レコードを片付ける。"""
    with jobs_lock:
        jobs.pop(job_id, None)
    try:
        with _job_db_connect() as conn:
            conn.execute("DELETE FROM background_jobs WHERE job_id=?", (job_id,))
    except sqlite3.Error:
        pass


@app.route("/api/run_daiko", methods=["POST"])
@limiter.limit("30 per minute")
def api_run_daiko():
    data = request.get_json(silent=True)
    err = validate_common_input(data)
    if err:
        return err
    err = validate_transfer_auth_codes(data)
    if err:
        return err
    if not validate_and_consume_api_key(data.get("api_key", "")):
        return jsonify({"error": "無効なAPIキー"}), 403
    if count_inflight_jobs() >= MAX_INFLIGHT_JOBS:
        return jsonify({"error": "混雑しています。少し待ってからお試しください。"}), 503

    custom_amounts = safe_custom_amounts(data)
    custom_playtime = safe_custom_playtime(data)
    character_settings = safe_character_settings(data)
    selected_payload = copy.deepcopy(data.get("selected", {}))

    # VIP権限はサイトアカウントの契約状態から判定する。
    is_vip_confirmed = site_vip_confirmed()
    if is_vip_confirmed:
        main_story_chapters = safe_main_story_chapters(data)
        main_story_stages = safe_main_story_stages(data)
        event_stage_settings = safe_event_stage_settings(data)
        vip_items = safe_vip_items(data)
        legend_stages = safe_legend_stages(data) if "legend_stages" in data else None
        vip_facilities = safe_vip_facilities(data) if "vip_facilities" in data else None
        vip_talent_orbs = safe_vip_talent_orbs(data) if "vip_talent_orbs" in data else None
        special_stages = safe_special_stages(data) if "special_stages" in data else None
        lineup_settings = safe_lineup_settings(data) if "lineup_settings" in data else None
        ototo_settings = safe_ototo_settings(data) if "ototo_settings" in data else None
        dojo_score_settings = safe_dojo_score_settings(data) if "dojo_score_settings" in data else None
        future_score_settings = safe_future_score_settings(data) if "future_score_settings" in data else None
    else:
        main_story_chapters = None
        main_story_stages = None
        event_stage_settings = None
        vip_items = {}
        legend_stages = None
        vip_facilities = None
        vip_talent_orbs = None
        special_stages = None
        lineup_settings = None
        ototo_settings = None
        dojo_score_settings = None
        future_score_settings = None
        selected_payload["s3"] = [
            value for value in selected_payload.get("s3", [])
            if value not in {"talents_specific", "talents_disable_all", "lineup_custom"}
        ]
        selected_payload["s2"] = [
            value for value in selected_payload.get("s2", [])
            if value not in VIP_ONLY_SYSTEM_ACTIONS
        ]

    # 外部metadataを使う入力整理が完了してからジョブIDを発行する。
    # 開始レスポンス前に通信が切れても、見えない孤立ジョブを実行しない。
    job_id = str(uuid.uuid4())
    job_token, job_access_hash = _new_job_access_token()
    operation_id = _safe_operation_id(data)
    if not operation_id:
        return jsonify({"error": "受付IDは30桁の英数字です"}), 400
    quota_reservation_id = None
    trial_vip_reservation_id = None
    if is_vip_confirmed:
        trial_vip_reservation_id, vip_error = _reserve_site_vip_job(job_id)
        if vip_error:
            return vip_error
    else:
        quota_reservation_id, quota_error = _reserve_free_quota(data, job_id)
        if quota_error:
            return quota_error
    now = time.time()
    try:
        _create_operation(operation_id, job_id, "daiko", now)
    except sqlite3.IntegrityError:
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        return jsonify({"error": "その受付IDは既に登録されています"}), 409
    except sqlite3.Error:
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        return jsonify({"error": "受付情報を保存できませんでした。"}), 503
    try:
        _create_job(job_id, {"status": "pending", "log": "待機中...",
                             "operation_id": operation_id, "operation_type": "daiko",
                             "job_access_hash": job_access_hash,
                             "quota_reservation_id": quota_reservation_id,
                             "trial_vip_reservation_id": trial_vip_reservation_id,
                             "error": None, "transfer_code": None, "auth_code": None,
                             "applied": [], "created_at": now, "updated_at": now,
                             "transfer_received": False})
    except Exception:
        _discard_unstarted_job(job_id)
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        _close_operation(
            operation_id, status="error", recovery_status="not_available",
            error="ジョブ情報の保存に失敗しました。",
        )
        return jsonify({"error": "処理を開始できませんでした"}), 503
    register_job_to_session(job_id)
    try:
        executor.submit(
            run_job_daiko,
            job_id, operation_id, str(data.get("transfer_code", "")).strip(), str(data.get("auth_code", "")).strip(),
            selected_payload, data.get("char_list", []), custom_amounts, custom_playtime, main_story_chapters, vip_items, legend_stages, vip_facilities, vip_talent_orbs, special_stages, character_settings, main_story_stages, event_stage_settings, lineup_settings, ototo_settings, dojo_score_settings, future_score_settings,
            "VIP" if is_vip_confirmed else "free",
        )
    except Exception as exc:
        error_text = _safe_error_message(exc)
        _update_job(job_id, {"status": "error", "error": error_text, "updated_at": time.time()})
        _close_operation(
            operation_id,
            status="error",
            recovery_status="not_available",
            error=error_text,
        )
        return jsonify({"error": "処理を開始できませんでした"}), 503
    return jsonify({"job_id": job_id, "job_token": job_token, "operation_id": operation_id})


@app.route("/api/run_create", methods=["POST"])
@limiter.limit("30 per minute")
def api_run_create():
    data = request.get_json(silent=True)
    err = validate_common_input(data)
    if err:
        return err
    if not validate_and_consume_api_key(data.get("api_key", "")):
        return jsonify({"error": "無効なAPIキー"}), 403
    if count_inflight_jobs() >= MAX_INFLIGHT_JOBS:
        return jsonify({"error": "混雑しています。少し待ってからお試しください。"}), 503

    custom_amounts = safe_custom_amounts(data)
    custom_playtime = safe_custom_playtime(data)
    character_settings = safe_character_settings(data)
    selected_payload = copy.deepcopy(data.get("selected", {}))
    # アカウント種別選択・詳細指定はサイトVIP契約者のみ許可。
    is_vip_confirmed = site_vip_confirmed()
    if is_vip_confirmed:
        count = safe_count(data)
        account_type_key = safe_account_type(data)
        main_story_chapters = safe_main_story_chapters(data)
        main_story_stages = safe_main_story_stages(data)
        event_stage_settings = safe_event_stage_settings(data)
        vip_items = safe_vip_items(data)
        legend_stages = safe_legend_stages(data) if "legend_stages" in data else None
        vip_facilities = safe_vip_facilities(data) if "vip_facilities" in data else None
        vip_talent_orbs = safe_vip_talent_orbs(data) if "vip_talent_orbs" in data else None
        special_stages = safe_special_stages(data) if "special_stages" in data else None
        lineup_settings = safe_lineup_settings(data) if "lineup_settings" in data else None
        ototo_settings = safe_ototo_settings(data) if "ototo_settings" in data else None
        dojo_score_settings = safe_dojo_score_settings(data) if "dojo_score_settings" in data else None
        future_score_settings = safe_future_score_settings(data) if "future_score_settings" in data else None
    else:
        count = safe_count(data, 2)
        account_type_key = "new"
        main_story_chapters = None
        main_story_stages = None
        event_stage_settings = None
        vip_items = {}
        legend_stages = None
        vip_facilities = None
        vip_talent_orbs = None
        special_stages = None
        lineup_settings = None
        ototo_settings = None
        dojo_score_settings = None
        future_score_settings = None
        selected_payload["s3"] = [
            value for value in selected_payload.get("s3", [])
            if value not in {"talents_specific", "talents_disable_all", "lineup_custom"}
        ]
        selected_payload["s2"] = [
            value for value in selected_payload.get("s2", [])
            if value not in VIP_ONLY_SYSTEM_ACTIONS
        ]

    job_id = str(uuid.uuid4())
    job_token, job_access_hash = _new_job_access_token()
    quota_reservation_id = None
    trial_vip_reservation_id = None
    if is_vip_confirmed:
        trial_vip_reservation_id, vip_error = _reserve_site_vip_job(job_id)
        if vip_error:
            return vip_error
    else:
        quota_reservation_id, quota_error = _reserve_free_quota(data, job_id)
        if quota_error:
            return quota_error
    now = time.time()
    try:
        _create_job(job_id, {"status": "pending", "log": "待機中...",
                             "job_access_hash": job_access_hash,
                             "quota_reservation_id": quota_reservation_id,
                             "trial_vip_reservation_id": trial_vip_reservation_id,
                             "error": None, "transfer_code": None, "auth_code": None,
                             "applied": [], "accounts": [], "created_at": now, "updated_at": now})
    except Exception:
        _discard_unstarted_job(job_id)
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        return jsonify({"error": "処理を開始できませんでした"}), 503
    register_job_to_session(job_id)
    try:
        executor.submit(
            run_job_create,
            job_id, selected_payload,
            data.get("char_list", []), custom_amounts, count, custom_playtime, account_type_key, main_story_chapters, vip_items, legend_stages, vip_facilities, vip_talent_orbs, special_stages, character_settings, main_story_stages, event_stage_settings, lineup_settings, ototo_settings, dojo_score_settings, future_score_settings,
            "VIP" if is_vip_confirmed else "free",
        )
    except Exception as exc:
        error_text = _safe_error_message(exc)
        _update_job(job_id, {"status": "error", "error": error_text, "updated_at": time.time()})
        return jsonify({"error": "処理を開始できませんでした"}), 503
    return jsonify({"job_id": job_id, "job_token": job_token})


@app.route("/api/run_clone", methods=["POST"])
@limiter.limit("30 per minute")
def api_run_clone():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "入力が不正です"}), 400
    err = validate_transfer_auth_codes(data)
    if err:
        return err
    if not validate_and_consume_api_key(data.get("api_key", "")):
        return jsonify({"error": "無効なAPIキー"}), 403
    if count_inflight_jobs() >= MAX_INFLIGHT_JOBS:
        return jsonify({"error": "混雑しています。少し待ってからお試しください。"}), 503

    is_vip_confirmed = site_vip_confirmed()

    job_id = str(uuid.uuid4())
    job_token, job_access_hash = _new_job_access_token()
    operation_id = _safe_operation_id(data)
    if not operation_id:
        return jsonify({"error": "受付IDは30桁の英数字です"}), 400
    quota_reservation_id = None
    trial_vip_reservation_id = None
    if is_vip_confirmed:
        trial_vip_reservation_id, vip_error = _reserve_site_vip_job(job_id)
        if vip_error:
            return vip_error
    else:
        quota_reservation_id, quota_error = _reserve_free_quota(data, job_id)
        if quota_error:
            return quota_error
    now = time.time()
    try:
        _create_operation(operation_id, job_id, "clone", now)
    except sqlite3.IntegrityError:
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        return jsonify({"error": "その受付IDは既に登録されています"}), 409
    except sqlite3.Error:
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        return jsonify({"error": "受付情報を保存できませんでした。"}), 503
    try:
        _create_job(job_id, {"status": "pending", "log": "待機中...",
                             "operation_id": operation_id, "operation_type": "clone",
                             "job_access_hash": job_access_hash,
                             "quota_reservation_id": quota_reservation_id,
                             "trial_vip_reservation_id": trial_vip_reservation_id,
                             "error": None,
                             "orig_transfer_code": None, "orig_auth_code": None,
                             "copy_transfer_code": None, "copy_auth_code": None,
                             "copies": [], "created_at": now, "updated_at": now,
                             "transfer_received": False})
    except Exception:
        _discard_unstarted_job(job_id)
        _refund_unattached_quota(quota_reservation_id)
        _refund_unattached_vip_trial(trial_vip_reservation_id)
        _close_operation(
            operation_id, status="error", recovery_status="not_available",
            error="ジョブ情報の保存に失敗しました。",
        )
        return jsonify({"error": "処理を開始できませんでした"}), 503
    register_job_to_session(job_id)
    count = safe_count(data, MAX_COUNT if is_vip_confirmed else 2)
    try:
        executor.submit(
            run_job_clone,
            job_id, operation_id, str(data.get("transfer_code", "")).strip(), str(data.get("auth_code", "")).strip(), count,
            "VIP" if is_vip_confirmed else "free",
        )
    except Exception as exc:
        error_text = _safe_error_message(exc)
        _update_job(job_id, {"status": "error", "error": error_text, "updated_at": time.time()})
        _close_operation(
            operation_id,
            status="error",
            recovery_status="not_available",
            error=error_text,
        )
        return jsonify({"error": "処理を開始できませんでした"}), 503
    return jsonify({"job_id": job_id, "job_token": job_token, "operation_id": operation_id})


@app.route("/api/job/<job_id>")
@limiter.limit("120 per minute")
def api_job_status(job_id):
    with jobs_lock:
        job = copy.deepcopy(jobs.get(job_id))
    if job is None:
        job = _load_job(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if not session_owns_job(job_id, job):
        return jsonify({"error": "unauthorized"}), 403
    _settle_job_quota(job)
    job.pop("job_access_hash", None)
    job.pop("quota_reservation_id", None)
    job.pop("trial_vip_reservation_id", None)
    return jsonify(job)


@app.route("/api/get_key", methods=["POST"])
@limiter.limit("10 per minute")
def api_get_key():
    key = generate_api_key()
    return jsonify({"api_key": key})


@app.route("/api/usage_count")
@limiter.limit("60 per minute")
def api_usage_count():
    return jsonify({"count": get_usage_count()})


@app.route("/api/free/status", methods=["POST"])
@limiter.limit("60 per minute")
def api_free_status():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "入力が不正です"}), 400
    site_account = current_site_user()
    try:
        _process_invitation_vip_trials()
        result = free_usage_manager.status(
            real_ip(), data.get("fingerprint", ""),
            allow_month_end_unlimited=bool(site_account),
        )
    except IdentityConflict as exc:
        return jsonify({"error": str(exc), "code": "identity_conflict"}), 409
    except FreeUsageError as exc:
        return jsonify({"error": str(exc), "code": "identity_invalid"}), 400
    except sqlite3.Error:
        return jsonify({"error": "利用回数を確認できませんでした。"}), 503
    site_account = current_site_user()
    result["trial"] = {
        "logged_in": bool(site_account),
        "is_vip": bool(site_account and site_account.get("is_vip")),
        "claimed": bool(site_account and site_account.get("trial_vip_claimed_at")),
        "offer_active": bool(site_account and site_account.get("trial_offer_active")),
        "offer_expires_at": site_account.get("trial_offer_expires_at") if site_account else None,
        "uses_remaining": int(site_account.get("trial_vip_uses_remaining") or 0) if site_account else 0,
        "vip_expires_at": site_account.get("vip_expires_at") if site_account else None,
    }
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/free/invitation", methods=["POST"])
@limiter.limit("10 per minute")
def api_free_invitation():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "入力が不正です"}), 400
    try:
        result = free_usage_manager.invitation_link(
            real_ip(), data.get("fingerprint", ""), PUBLIC_BASE_URL,
            allow_month_end_unlimited=bool(current_site_user()),
        )
    except IdentityConflict as exc:
        return jsonify({"error": str(exc), "code": "identity_conflict"}), 409
    except FreeUsageError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.Error:
        return jsonify({"error": "招待リンクを発行できませんでした。"}), 503
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/free/invitation/redeem", methods=["POST"])
@limiter.limit("10 per minute")
def api_free_invitation_redeem():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "入力が不正です"}), 400
    try:
        result = free_usage_manager.redeem_invitation(
            data.get("code", ""), real_ip(), data.get("fingerprint", ""),
            allow_month_end_unlimited=bool(current_site_user()),
        )
    except IdentityConflict as exc:
        return jsonify({"error": str(exc), "code": "identity_conflict"}), 409
    except InvitationError as exc:
        return jsonify({"error": str(exc), "code": "invitation_rejected"}), 409
    except FreeUsageError as exc:
        return jsonify({"error": str(exc)}), 400
    except sqlite3.Error:
        return jsonify({"error": "招待報酬を処理できませんでした。"}), 503
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/admin/operations/<operation_id>")
@limiter.limit("60 per minute")
@admin_api_required
def api_admin_operation(operation_id):
    if not OPERATION_ID_RE.fullmatch(operation_id):
        return jsonify({"error": "受付IDの形式が不正です"}), 400
    record = _load_operation(operation_id)
    if not record:
        return jsonify({"error": "not found"}), 404
    result = _operation_public(record)
    if record.get("recovery_status") in {"admin_reissued", "client_code_issued"}:
        try:
            codes = json.loads(
                _decrypt_snapshot(record.get("reissue_result"), operation_id).decode()
            )
            result["recovery_transfer_code"] = codes.get("tc")
            result["recovery_auth_code"] = codes.get("ac")
        except Exception:
            result["recovery_transfer_code"] = None
            result["recovery_auth_code"] = None
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/admin/operations/<operation_id>/reissue", methods=["POST"])
@limiter.limit("5 per minute")
@admin_api_required
def api_admin_operation_reissue(operation_id):
    if not request.is_json:
        return jsonify({"error": "JSONリクエストが必要です"}), 415
    if not _valid_admin_csrf():
        return jsonify({"error": "CSRF検証に失敗しました"}), 403
    if not OPERATION_ID_RE.fullmatch(operation_id):
        return jsonify({"error": "受付IDの形式が不正です"}), 400
    with _operation_db_connect() as conn:
        cur = conn.execute(
            """UPDATE operation_records SET recovery_status='issuing',updated_at=?
               WHERE operation_id=? AND snapshot IS NOT NULL
                 AND recovery_status IN ('snapshot_saved','reissue_failed')
                 AND status='error' AND created_at>=?""",
            (time.time(), operation_id, time.time() - OPERATION_RETENTION),
        )
    if cur.rowcount != 1:
        return jsonify({"error": "再発行できない状態です"}), 409
    executor.submit(run_admin_reissue, operation_id)
    return jsonify({"status": "issuing"}), 202


@app.route("/api/char_dict")
@limiter.limit("30 per minute")
def api_char_dict():
    try:
        char_dict = {}
        for character in get_character_metadata().get("characters", []):
            if not character.get("selectable", True):
                continue
            display_id = int(character["id"])
            number = str(display_id).zfill(3)
            for name in character.get("names", []):
                if name:
                    char_dict[str(name)] = number
        return jsonify(char_dict)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vip_legend_metadata")
@limiter.limit("20 per minute")
def api_vip_legend_metadata():
    if not site_vip_confirmed():
        return jsonify({"error": "VIP限定機能です"}), 403
    try:
        return jsonify(get_legend_metadata())
    except Exception as e:
        print(f"[ERROR] vip legend metadata: {e}")
        return jsonify({"error": "ステージ名データを読み込めませんでした"}), 500


@app.route("/api/character_metadata")
@limiter.limit("10 per minute")
def api_character_metadata():
    try:
        return jsonify(get_character_metadata())
    except Exception as e:
        print(f"[ERROR] character metadata: {e}")
        return jsonify({"error": "キャラ名データを読み込めませんでした"}), 500


@app.route("/api/vip_ototo_metadata")
@limiter.limit("20 per minute")
def api_vip_ototo_metadata():
    if not site_vip_confirmed():
        return jsonify({"error": "VIP限定機能です"}), 403
    try:
        return jsonify(get_ototo_metadata())
    except Exception as e:
        print(f"[ERROR] vip ototo metadata: {e}")
        return jsonify({"error": "オトート定義を読み込めませんでした"}), 500




@app.after_request
def inject_infra_credit(response):
    """HTMLレスポンスの </body> 直前にクレジットフッターを自動挿入する（テンプレート編集不要）。"""
    try:
        credit_html = globals().get("INFRA_CREDIT_HTML", "")
        if (
            credit_html
            and response.status_code == 200
            and response.content_type
            and response.content_type.startswith("text/html")
            and not response.direct_passthrough
        ):
            body = response.get_data(as_text=True)
            if "</body>" in body and 'id="infra-credit"' not in body:
                response.set_data(body.replace("</body>", credit_html + "\n</body>", 1))
    except Exception as e:
        print(f"[ERROR] inject_infra_credit: {e}")
    return response


# =====================
# ページルート（/ と /vip）
# =====================
@app.route("/")
@limiter.limit("5 per second")
def index():
    user = current_site_user()
    if user and user.get("is_vip"):
        return redirect("/vip")
    response = app.make_response(render_template(
        "index.html", user=user, discord_user=current_user(), discord_is_admin=is_admin_user()
    ))
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return response


@app.route("/admin/panel")
@limiter.limit("20 per minute")
def admin_panel():
    if not current_user():
        return redirect("/auth/login?next=/admin/panel")
    if not is_admin_user():
        return app.make_response(("このDiscordアカウントは管理者に登録されていません。", 403))
    operation_id = request.args.get("id", "").strip()
    record = None
    search_error = None
    if operation_id:
        if not OPERATION_ID_RE.fullmatch(operation_id):
            search_error = "IDは30桁の英数字で入力してください。"
        else:
            loaded = _load_operation(operation_id)
            if loaded:
                record = _operation_public(loaded)
            else:
                search_error = "そのIDは存在しません。"
    response = app.make_response(render_template(
        "admin_panel.html", user=current_user(), operation=record,
        operation_id=operation_id, search_error=search_error,
        csrf_token=_admin_csrf_token(),
        vip_overview=account_store.admin_overview(),
    ))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route("/vip")
@limiter.limit("5 per second")
def vip_page():
    user = current_site_user()
    if not user or not user.get("is_vip"):
        return redirect("/account/login?next=/vip")
    response = app.make_response(render_template(
        "vip.html", user=user, discord_user=current_user(), discord_is_admin=is_admin_user()
    ))
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Cookie"
    return response


@app.route("/vip-guide")
@limiter.limit("10 per minute")
def vip_guide_page():
    """サイトアカウント方式の公開VIP案内・購入導線ページ。"""
    user = current_site_user()
    response = app.make_response(render_template(
        "vip_guide.html",
        user=user,
        price_label=VIP_PRICE_LABEL,
        plan_label=VIP_PLAN_LABEL,
        plans=account_store.vip_plans(),
        vip_stats=account_store.public_vip_stats(),
    ))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


register_access_guard(app, account_store, real_ip=real_ip, limiter=limiter)
register_account_routes(
    app,
    account_store,
    current_discord_user=current_user,
    is_admin_user=is_admin_user,
    real_ip=real_ip,
    limiter=limiter,
    static_dir=os.path.join(BASE_DIR, "static"),
    free_identity_resolver=free_usage_manager.identity_account_id,
)
register_chat_routes(
    app,
    chat_store,
    dm_store,
    current_user=current_user,
    real_ip=real_ip,
    static_dir=os.path.join(BASE_DIR, "static"),
)
register_dm_routes(
    app,
    chat_store,
    dm_store,
    current_user=current_user,
    real_ip=real_ip,
)
register_moderation_routes(
    app,
    chat_store,
    current_user=current_user,
    real_ip=real_ip,
)


if __name__ == "__main__":
    print("=" * 50)
    print("にゃんこ代行ツール - Web版")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=1142, threaded=True)
