"""Forward administrator chat posts to one Discord channel."""

from __future__ import annotations

import os
import threading
import time

import requests


DISCORD_API = "https://discord.com/api/v10"
_send_lock = threading.Lock()


def build_admin_chat_payload(author: str, content: str, notify: bool = True) -> dict:
    payload = {
        "allowed_mentions": {"parse": ["everyone"] if notify else []},
        "embeds": [{
            "title": "AUTOCAT JP 管理者チャット",
            "description": str(content)[:4000],
            "color": 0x38BDF8,
            "footer": {"text": f"投稿者: {str(author)[:100]}"},
        }],
    }
    if notify:
        payload["content"] = "@everyone"
    return payload


def send_admin_chat(author: str, content: str, notify: bool = True) -> bool:
    token = os.getenv("CHAT_DISCORD_BOT_TOKEN", "").strip()
    channel_id = os.getenv("CHAT_DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel_id.isdigit():
        print("[CHAT DISCORD] disabled: token or channel is missing")
        return False
    payload = build_admin_chat_payload(author, content, bool(notify))
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "autocat-chat-bridge/1.0",
    }
    with _send_lock:
        for attempt in range(3):
            try:
                response = requests.post(
                    f"{DISCORD_API}/channels/{channel_id}/messages",
                    headers=headers,
                    json=payload,
                    timeout=(5, 15),
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt < 2:
                    time.sleep(1)
                    continue
                return False
            if response.status_code in (200, 201, 204):
                return True
            if response.status_code == 429 and attempt < 2:
                try:
                    delay = float(response.json().get("retry_after", 1))
                except (TypeError, ValueError, AttributeError):
                    delay = 1
                time.sleep(max(0.25, min(10, delay)))
                continue
            if 500 <= response.status_code < 600 and attempt < 2:
                time.sleep(1)
                continue
            print(f"[CHAT DISCORD] HTTP {response.status_code}")
            return False
    return False
