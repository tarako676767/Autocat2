"""Send privacy-safe usage summaries to a configured Discord channel."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

import requests


DISCORD_API = "https://discord.com/api/v10"
_send_lock = threading.Lock()
_warning_lock = threading.Lock()
_warned_missing_config = False


def _description_text(items: str | Iterable[object]) -> str:
    if isinstance(items, str):
        values = [items]
    else:
        try:
            values = [str(value).strip() for value in items]
        except TypeError:
            values = [str(items)]
    lines = [f"・{value}" for value in values if value]
    return ("\n".join(lines) or "・処理完了")[:4000]


def _ensure_counter(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS discord_log_state (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               success_count INTEGER NOT NULL DEFAULT 0
           )"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO discord_log_state(id, success_count) VALUES(1, 0)"
    )


def _post_message(token: str, channel_id: str, payload: dict) -> bool:
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "autocat-discord-log/1.0",
    }
    for attempt in range(2):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=(5, 15),
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            print(f"[DISCORD LOG] network failure: {type(exc).__name__}")
            if attempt == 0:
                time.sleep(1)
                continue
            return False

        if response.status_code in (200, 201, 204):
            return True
        if response.status_code == 429 and attempt == 0:
            try:
                retry_after = float(response.json().get("retry_after", 1))
            except (TypeError, ValueError, AttributeError):
                retry_after = 1
            time.sleep(max(0.25, min(5.0, retry_after)))
            continue
        if 500 <= response.status_code < 600 and attempt == 0:
            time.sleep(1)
            continue
        print(f"[DISCORD LOG] HTTP {response.status_code}")
        return False
    return False


def send_usage_log(
    tier: str,
    description: str | Iterable[object],
    total_count: int,
    usage_db_path: str | os.PathLike[str],
) -> bool:
    """Send one successful site use and increment the Discord-only counter.

    Site completion must never depend on Discord. All errors are contained and
    False is returned. The Discord-only count advances only after Discord has
    accepted the message.
    """
    global _warned_missing_config

    # サイトのログイン/VIP判定用Botとは認証情報を完全に分離する。
    token = os.getenv("DISCORD_LOG_BOT_TOKEN", "").strip()
    channel_id = os.getenv("DISCORD_LOG_CHANNEL_ID", "").strip()
    if not token or not channel_id.isdigit():
        with _warning_lock:
            if not _warned_missing_config:
                print("[DISCORD LOG] disabled: DISCORD_LOG_BOT_TOKEN or channel ID is missing")
                _warned_missing_config = True
        return False

    title = "VIP" if str(tier).upper() == "VIP" else "free"
    db_path = Path(usage_db_path)
    try:
        with _send_lock:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path, timeout=45)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                _ensure_counter(conn)
                row = conn.execute(
                    "SELECT success_count FROM discord_log_state WHERE id=1"
                ).fetchone()
                next_count = int(row[0] if row else 0) + 1
                payload = {
                    "allowed_mentions": {"parse": []},
                    "embeds": [{
                        "title": title,
                        "description": _description_text(description),
                        "color": 0xF5C542 if title == "VIP" else 0x7A8491,
                        "fields": [
                            {"name": "実績数", "value": str(next_count), "inline": True},
                            {"name": "全実績数", "value": str(max(0, int(total_count))), "inline": True},
                        ],
                    }],
                }
                if not _post_message(token, channel_id, payload):
                    conn.rollback()
                    return False
                conn.execute(
                    "UPDATE discord_log_state SET success_count=? WHERE id=1",
                    (next_count,),
                )
                conn.commit()
                return True
            finally:
                conn.close()
    except Exception as exc:
        print(f"[DISCORD LOG] failed: {type(exc).__name__}")
        return False