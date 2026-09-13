"""Flask API routes for private direct messages."""

from __future__ import annotations

import os
import secrets
from typing import Callable

from flask import Blueprint, jsonify, request, session

from CHAT.chat_store import ChatError, ChatPermissionError, ChatRateLimitError, ChatStore
from CHAT.dm_store import DMStore


def _admin_ids() -> set[str]:
    return {
        item.strip()
        for item in os.getenv("CHAT_ADMIN_DISCORD_IDS", "").split(",")
        if item.strip().isdigit()
    }


def _valid_csrf() -> bool:
    expected = str(session.get("chat_csrf_token") or "")
    supplied = str(request.headers.get("X-CSRF-Token") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def register_dm_routes(
    app,
    store: ChatStore,
    dm_store: DMStore,
    current_user: Callable[[], dict | None],
    real_ip: Callable[[], str],
) -> None:
    bp = Blueprint("autocat_dm", __name__)
    admin_ids = _admin_ids()

    def actor_from(data: dict) -> dict:
        actor = store.resolve_actor(
            real_ip(), data.get("fingerprint", ""), current_user(), admin_ids
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
        print(f"[CHAT DM] unexpected error: {type(exc).__name__}")
        return jsonify({"error": "DMを処理できませんでした。"}), 503

    @bp.post("/api/chat/dm/state")
    def dm_state():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            result = dm_store.list_state(actor, data.get("target_token", ""))
            result["actor"] = {
                "id": actor["id"],
                "name": actor["name"],
                "avatar": actor["avatar"],
                "is_admin": actor["is_admin"],
                "discord": actor["discord"],
            }
            result["restriction"] = store.restriction_for_actor(actor)
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/dm/messages")
    def dm_send():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            store.ensure_actor_can_post(actor)
            message = dm_store.send_message(
                actor, data.get("target_token"), data.get("content")
            )
            return jsonify({"message": message}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.delete("/api/chat/dm/messages/<int:message_id>")
    def dm_delete(message_id: int):
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            actor = actor_from(data)
            return jsonify(dm_store.delete_message(message_id, actor))
        except Exception as exc:
            return error_response(exc)

    app.register_blueprint(bp)
