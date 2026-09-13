"""Site-wide IP/fingerprint GBAN enforcement."""

from __future__ import annotations

import secrets
from typing import Callable
from urllib.parse import urlsplit

from flask import jsonify, make_response, redirect, render_template, request, session

from ACCOUNT.account_store import AccountError, AccountStore


GBAN_REDIRECT_URL = "https://discord.gg/zgngnSfbUE"
_SESSION_FINGERPRINT = "access_fingerprint"
_SESSION_NONCE = "access_check_nonce"


def _safe_next(raw: object) -> str:
    value = str(raw or "").strip()
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment or not parts.path.startswith("/"):
        return "/"
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _redirect_to_blacklist() :
    response = redirect(GBAN_REDIRECT_URL, code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def register_access_guard(app, store: AccountStore, real_ip: Callable[[], str], limiter) -> None:
    """Require a signed fingerprint session and redirect every GBAN match."""

    @app.post("/api/access/identify")
    @limiter.limit("20 per minute")
    def access_identify():
        try:
            if store.check_global_ban(real_ip())["blocked"]:
                session.pop(_SESSION_FINGERPRINT, None)
                return _redirect_to_blacklist()
        except AccountError:
            pass
        expected = str(session.get(_SESSION_NONCE) or "")
        supplied = str(request.form.get("nonce") or "")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({"error": "アクセス確認の有効期限が切れました。再読み込みしてください。"}), 403
        fingerprint = str(request.form.get("fingerprint") or "").strip().lower()
        try:
            result = store.check_global_ban(real_ip(), fingerprint)
        except AccountError as exc:
            return jsonify({"error": str(exc)}), 400
        session.pop(_SESSION_NONCE, None)
        if result["blocked"]:
            session.pop(_SESSION_FINGERPRINT, None)
            return _redirect_to_blacklist()
        session[_SESSION_FINGERPRINT] = fingerprint
        session.permanent = True
        return redirect(_safe_next(request.form.get("next")), code=303)

    @app.before_request
    def enforce_global_ban():
        path = request.path
        # Only the fingerprint collector can run before verification.
        # User-Agent, method, route type, and static-file paths never exempt GBAN.
        if path == "/api/access/identify":
            return None

        fingerprint = str(session.get(_SESSION_FINGERPRINT) or "").strip().lower()
        try:
            result = store.check_global_ban(real_ip(), fingerprint or None)
        except AccountError:
            result = {"blocked": False}
            fingerprint = ""
        if result["blocked"]:
            session.pop(_SESSION_FINGERPRINT, None)
            return _redirect_to_blacklist()
        if fingerprint:
            return None

        if request.method in {"GET", "HEAD"} and request.accept_mimetypes.accept_html:
            nonce = secrets.token_urlsafe(32)
            session[_SESSION_NONCE] = nonce
            response = make_response(render_template(
                "access_check.html",
                nonce=nonce,
                next_url=_safe_next(request.full_path.rstrip("?")),
            ))
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
            return response

        response = jsonify({
            "error": "アクセス確認が必要です。最初にブラウザでサイトを開いてください。",
            "verification_url": "/",
        })
        response.status_code = 428
        response.headers["Cache-Control"] = "no-store"
        return response
