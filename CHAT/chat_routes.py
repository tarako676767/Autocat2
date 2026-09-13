"""Flask routes for AUTOCAT JP public and administrator chat rooms."""

from __future__ import annotations

import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from flask import Blueprint, jsonify, make_response, render_template, request, send_from_directory, session

from CHAT.chat_store import (
    ChatError,
    ChatPermissionError,
    ChatRateLimitError,
    ChatStore,
)
from CHAT.dm_store import DMStore
from DISCORD.chat_bridge import send_admin_chat


_notify_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chat-notify")


def _admin_ids() -> set[str]:
    return {
        item.strip()
        for item in os.getenv("CHAT_ADMIN_DISCORD_IDS", "").split(",")
        if item.strip().isdigit()
    }


def _csrf_token() -> str:
    token = session.get("chat_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["chat_csrf_token"] = token
    return token


def _valid_csrf() -> bool:
    expected = str(session.get("chat_csrf_token") or "")
    supplied = str(request.headers.get("X-CSRF-Token") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _push_one(store: ChatStore, subscription: dict, message: dict) -> None:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return
    private_key = os.getenv("CHAT_VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("CHAT_VAPID_SUBJECT", "mailto:admin@autocat.jp").strip()
    if not private_key:
        return
    payload = json.dumps({
        "title": "AUTOCAT JP",
        "body": str(message.get("content") or "")[:140],
        "url": f"/chat?room=admin&message={int(message['id'])}",
        "tag": f"autocat-admin-{int(message['id'])}",
    }, ensure_ascii=False)
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": subject},
            ttl=86400,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            store.remove_subscription(subscription.get("endpoint", ""))
        else:
            print(f"[CHAT PUSH] failed: HTTP {status or 'unknown'}")
    except Exception as exc:
        print(f"[CHAT PUSH] failed: {type(exc).__name__}")


def _fanout_admin_message(store: ChatStore, message: dict) -> None:
    message_id = int(message["id"])
    if not store.claim_admin_fanout(message_id):
        print(f"[CHAT FANOUT] duplicate skipped: message_id={message_id}")
        return
    notify = bool(message.get("notify", message.get("notify_push", True)))
    discord_sent = False
    try:
        discord_sent = send_admin_chat(
            message.get("author_name", "管理者"),
            message.get("content", ""),
            notify=notify,
        )
        if notify:
            for subscription in store.subscriptions():
                _push_one(store, subscription, message)
    finally:
        store.complete_admin_fanout(message_id, discord_sent, notify)


def register_chat_routes(
    app,
    store: ChatStore,
    dm_store: DMStore,
    current_user: Callable[[], dict | None],
    real_ip: Callable[[], str],
    static_dir: str,
) -> None:
    bp = Blueprint("autocat_chat", __name__)
    admin_ids = _admin_ids()
    static_path = Path(static_dir).resolve()

    def actor_from(data: dict) -> dict:
        actor = store.resolve_actor(
            real_ip(),
            data.get("fingerprint", ""),
            current_user(),
            admin_ids,
        )
        dm_store.register_actor(actor)
        return actor

    def error_response(exc: Exception):
        if isinstance(exc, ChatPermissionError):
            return jsonify({"error": str(exc)}), 403
        if isinstance(exc, ChatRateLimitError):
            return jsonify({"error": str(exc)}), 429
        if isinstance(exc, ChatError):
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, ValueError):
            return jsonify({"error": "入力が不正です。"}), 400
        print(f"[CHAT] unexpected error: {type(exc).__name__}")
        return jsonify({"error": "チャットを処理できませんでした。"}), 503

    @bp.get("/chat")
    def chat_page():
        user = current_user()
        user_id = str((user or {}).get("id") or "")
        response = make_response(render_template(
            "chat.html",
            user=user,
            is_chat_admin=user_id in admin_ids,
            csrf_token=_csrf_token(),
            vapid_public_key=os.getenv("CHAT_VAPID_PUBLIC_KEY", "").strip(),
        ))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @bp.get("/chat-sw.js")
    def chat_service_worker():
        response = make_response(send_from_directory(static_path, "chat-sw.js"))
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/chat"
        return response

    @bp.get("/chat-assets/<path:filename>")
    def chat_asset(filename: str):
        if filename not in {"chat.js", "chat-admin.js"}:
            return jsonify({"error": "Not found"}), 404
        response = make_response(send_from_directory(static_path, filename))
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @bp.post("/api/chat/state")
    def chat_state():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            messages = store.list_messages(
                data.get("room", "free"), actor, int(data.get("after_id", 0) or 0)
            )
            return jsonify({
                "actor": {
                    "id": actor["id"],
                    "name": actor["name"],
                    "avatar": actor["avatar"],
                    "is_admin": actor["is_admin"],
                    "discord": actor["discord"],
                },
                "restriction": store.restriction_for_actor(actor),
                "messages": messages,
                "day": store._today(),
            })
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/messages")
    def chat_post_message():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            room = data.get("room", "free")
            notify = data.get("notify", data.get("notify_push", True))
            if room == "admin" and not isinstance(notify, bool):
                return jsonify({"error": "通知設定が不正です。"}), 400
            actor = actor_from(data)
            message = store.create_message(room, actor, data.get("content"))
            if message["room"] == "admin":
                message["notify"] = notify
                _notify_executor.submit(_fanout_admin_message, store, dict(message))
            return jsonify({"message": message}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.delete("/api/chat/messages/<int:message_id>")
    def chat_delete_message(message_id: int):
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            return jsonify(store.delete_message(message_id, actor))
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/messages/<int:message_id>/moderation")
    def chat_moderate_author(message_id: int):
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            return jsonify(store.set_author_restriction(
                message_id,
                actor,
                data.get("action"),
                data.get("duration_seconds"),
            ))
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/push/subscribe")
    def chat_push_subscribe():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        if not (
            os.getenv("CHAT_VAPID_PUBLIC_KEY", "").strip()
            and os.getenv("CHAT_VAPID_PRIVATE_KEY", "").strip()
        ):
            return jsonify({"error": "通知機能が設定されていません。"}), 503
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            store.save_subscription(actor, data.get("subscription"))
            return jsonify({"subscribed": True})
        except Exception as exc:
            return error_response(exc)

    app.register_blueprint(bp)
