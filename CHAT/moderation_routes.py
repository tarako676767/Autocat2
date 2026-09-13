"""Administrator panel routes for chat bans and timeouts."""

from __future__ import annotations

import os
import secrets
from typing import Callable

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, session

from CHAT.chat_store import ChatError, ChatPermissionError, ChatStore


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


def register_moderation_routes(
    app,
    store: ChatStore,
    current_user: Callable[[], dict | None],
    real_ip: Callable[[], str],
) -> None:
    bp = Blueprint("autocat_chat_moderation", __name__)
    admin_ids = _admin_ids()

    def actor() -> dict:
        user = current_user()
        return store.resolve_actor(real_ip(), "", user, admin_ids)

    def require_admin() -> dict:
        current = actor()
        if not current["is_admin"]:
            raise ChatPermissionError("管理者のみ利用できます。")
        return current

    def error_response(exc: Exception):
        if isinstance(exc, ChatPermissionError):
            return jsonify({"error": str(exc)}), 403
        if isinstance(exc, ChatError):
            return jsonify({"error": str(exc)}), 400
        print(f"[CHAT ADMIN] unexpected error: {type(exc).__name__}")
        return jsonify({"error": "管理操作を処理できませんでした。"}), 503

    @bp.get("/chat/admin")
    def moderation_page():
        user = current_user()
        if not user:
            return redirect("/auth/login?next=/chat/admin")
        user_id = str((user or {}).get("id") or "")
        if user_id not in admin_ids:
            return make_response("このDiscordアカウントはチャット管理者に登録されていません。", 403)
        response = make_response(render_template(
            "chat_admin.html", user=user, csrf_token=_csrf_token()
        ))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @bp.post("/api/chat/admin/state")
    def moderation_state():
        try:
            current = require_admin()
            return jsonify({"restrictions": store.list_restrictions(current)})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/admin/unban-code")
    def moderation_unban_code():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            return jsonify(store.unban_by_code(require_admin(), data.get("code")))
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/chat/admin/clear-restriction")
    def moderation_clear():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            return jsonify(store.clear_restriction(require_admin(), data.get("token")))
        except Exception as exc:
            return error_response(exc)

    app.register_blueprint(bp)
