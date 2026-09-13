"""Flask routes for site accounts, VIP purchase DMs, and VIP administration."""

from __future__ import annotations

import json
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from flask import Blueprint, jsonify, make_response, redirect, render_template, request, send_from_directory, session

from ACCOUNT.account_store import (
    AccountError,
    AccountExternalServiceError,
    AccountPermissionError,
    AccountRateLimitError,
    AccountStore,
)


_push_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-push")


def _csrf_token() -> str:
    token = session.get("account_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["account_csrf_token"] = token
    return token


def _valid_csrf() -> bool:
    expected = str(session.get("account_csrf_token") or "")
    supplied = str(request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _safe_next(raw: object) -> str:
    value = str(raw or "").strip()
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.fragment:
        return "/account"
    if parts.path in {"/", "/account", "/vip", "/vip-guide"} and not parts.query:
        return parts.path
    if parts.path == "/vip/purchase":
        query = parse_qs(parts.query, keep_blank_values=False)
        if set(query) - {"plan"}:
            return "/vip/purchase"
        plan = str((query.get("plan") or [""])[0])
        return f"/vip/purchase?{urlencode({'plan': plan})}" if plan in {"30", "60", "90"} else "/vip/purchase"
    return "/account"


def _push_one(store: AccountStore, subscription: dict, payload: dict) -> None:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return
    private_key = os.getenv("CHAT_VAPID_PRIVATE_KEY", "").strip()
    subject = os.getenv("CHAT_VAPID_SUBJECT", "mailto:admin@autocat.jp").strip()
    if not private_key:
        return
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=private_key,
            vapid_claims={"sub": subject},
            ttl=86400,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            store.remove_subscription_endpoint(subscription.get("endpoint", ""))
        else:
            print(f"[ACCOUNT PUSH] failed: HTTP {status or 'unknown'}")
    except Exception as exc:
        print(f"[ACCOUNT PUSH] failed: {type(exc).__name__}")


def _fanout(store: AccountStore, owner_key: str, body: str, url: str, tag: str) -> None:
    payload = {
        "title": "AUTOCAT JP",
        "body": str(body or "VIP購入DMに新着があります。")[:140],
        "url": url,
        "tag": tag,
    }
    for subscription in store.subscriptions(owner_key):
        _push_one(store, subscription, payload)


def register_account_routes(
    app,
    store: AccountStore,
    current_discord_user: Callable[[], dict | None],
    is_admin_user: Callable[[], bool],
    real_ip: Callable[[], str],
    limiter,
    static_dir: str,
    free_identity_resolver: Callable[[str, object], int] | None = None,
) -> None:
    bp = Blueprint("autocat_accounts", __name__)
    static_path = Path(static_dir).resolve()

    def current_account() -> dict | None:
        account = store.get_account(session.get("site_account_id"))
        if account and account.get("status") == "active":
            return account
        session.pop("site_account_id", None)
        return None

    def require_account() -> dict:
        account = current_account()
        if not account:
            raise AccountPermissionError("サイトアカウントへログインしてください。")
        return account

    def require_admin() -> str:
        if not is_admin_user():
            raise AccountPermissionError("管理者のみ利用できます。")
        return str((current_discord_user() or {}).get("id") or "admin")

    def bind_invitation_identity(account: dict, fingerprint: object) -> dict:
        if not free_identity_resolver or not fingerprint:
            return account
        try:
            free_identity_id = free_identity_resolver(real_ip(), fingerprint)
            return store.bind_free_identity(int(account["id"]), free_identity_id)
        except Exception as exc:
            # Account registration/login must remain available if the optional
            # trial binding cannot be completed. A failed binding grants nothing.
            print(f"[ACCOUNT TRIAL] identity binding failed: {type(exc).__name__}")
            return account

    def error_response(exc: Exception):
        if isinstance(exc, AccountPermissionError):
            return jsonify({"error": str(exc)}), 403
        if isinstance(exc, AccountRateLimitError):
            return jsonify({"error": str(exc)}), 429
        if isinstance(exc, AccountExternalServiceError):
            return jsonify({"error": str(exc)}), 503
        if isinstance(exc, AccountError):
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, (TypeError, ValueError)):
            return jsonify({"error": "入力が不正です。"}), 400
        print(f"[ACCOUNT] unexpected error: {type(exc).__name__}")
        return jsonify({"error": "処理を完了できませんでした。"}), 503

    def page(template: str, **values):
        response = make_response(render_template(template, csrf_token=_csrf_token(), **values))
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @bp.get("/account/login")
    @limiter.limit("20 per minute")
    def account_login_page():
        if current_account():
            return redirect(_safe_next(request.args.get("next")))
        return page("account_auth.html", mode="login", next_url=_safe_next(request.args.get("next")))

    @bp.get("/account/register")
    @limiter.limit("10 per minute")
    def account_register_page():
        if current_account():
            return redirect(_safe_next(request.args.get("next")))
        return page("account_auth.html", mode="register", next_url=_safe_next(request.args.get("next")))

    @bp.post("/api/account/register")
    @limiter.limit("5 per hour")
    def account_register():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = store.create_account(
                real_ip(), data.get("fingerprint"), data.get("username"), data.get("password")
            )
            account = bind_invitation_identity(account, data.get("fingerprint"))
            session.permanent = True
            session["site_account_id"] = int(account["id"])
            return jsonify({"account": account, "redirect": _safe_next(data.get("next"))}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/account/login")
    @limiter.limit("10 per minute")
    def account_login():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = store.authenticate(data.get("username"), data.get("password"))
            account = bind_invitation_identity(account, data.get("fingerprint"))
            session.permanent = True
            session["site_account_id"] = int(account["id"])
            return jsonify({"account": account, "redirect": _safe_next(data.get("next"))})
        except Exception as exc:
            return error_response(exc)

    @bp.route("/account/logout", methods=["GET", "POST"])
    @limiter.limit("30 per minute")
    def account_logout():
        if request.method == "POST" and not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        session.pop("site_account_id", None)
        return redirect("/")

    @bp.get("/account")
    @limiter.limit("30 per minute")
    def account_page():
        account = current_account()
        if not account:
            return redirect("/account/login?next=/account")
        return page("account_dashboard.html", account=account, tickets=store.list_user_tickets(int(account["id"])))

    @bp.get("/vip/purchase")
    @limiter.limit("30 per minute")
    def purchase_page():
        try:
            selected_plan = store.plan_quote(request.args.get("plan", 30))
        except AccountError:
            selected_plan = store.plan_quote(30)
        account = current_account()
        if not account:
            next_url = f"/vip/purchase?plan={selected_plan['days']}"
            return redirect(f"/account/login?{urlencode({'next': next_url})}")
        tickets = store.list_user_tickets(int(account["id"]))
        selected_key = str(request.args.get("key") or (tickets[0]["purchase_key"] if tickets else ""))
        return page(
            "vip_purchase.html",
            account=account,
            tickets=tickets,
            selected_key=selected_key,
            plans=store.vip_plans(),
            selected_plan=selected_plan,
            vapid_public_key=os.getenv("CHAT_VAPID_PUBLIC_KEY", "").strip(),
        )

    @bp.post("/api/vip/purchase/confirm")
    @limiter.limit("5 per hour")
    def purchase_confirm():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = require_account()
            ticket, created, message = store.create_purchase_ticket(
                int(account["id"]), data.get("duration_days")
            )
            if created and message:
                _push_executor.submit(
                    _fanout,
                    store,
                    "admin",
                    f"{account['username']} からVIP {ticket['requested_days']}日・{ticket['price_yen']:,}円の購入申請が届きました。ID: {account['public_id']}",
                    f"/admin/vip?ticket={ticket['purchase_key']}",
                    f"vip-purchase-{ticket['purchase_key']}",
                )
            return jsonify({"ticket": ticket, "created": created}), 201 if created else 200
        except Exception as exc:
            return error_response(exc)

    @bp.get("/api/vip/purchase/state")
    @limiter.limit("120 per minute")
    def purchase_state():
        try:
            account = require_account()
            tickets = store.list_user_tickets(int(account["id"]))
            key = str(request.args.get("key") or (tickets[0]["purchase_key"] if tickets else ""))
            ticket = store.ticket_state(key, account_id=int(account["id"])) if key else None
            return jsonify({"account": account, "tickets": tickets, "ticket": ticket, "plans": store.vip_plans()})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/vip/purchase/messages")
    @limiter.limit("30 per minute")
    def purchase_message():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = require_account()
            message, _ = store.send_purchase_message(
                data.get("purchase_key"), data.get("content"), "user", account["username"], int(account["id"])
            )
            key = str(data.get("purchase_key") or "").strip().upper()
            _push_executor.submit(
                _fanout, store, "admin", f"{account['username']}: {message['content']}",
                f"/admin/vip?ticket={key}", f"vip-purchase-{key}",
            )
            return jsonify({"message": message}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/vip/purchase/payment-reported")
    @limiter.limit("10 per minute")
    def purchase_payment_reported():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = require_account()
            result = store.report_purchase_payment(data.get("purchase_key"), int(account["id"]))
            if result["changed"]:
                key = result["purchase_key"]
                _push_executor.submit(
                    _fanout,
                    store,
                    "admin",
                    f"{account['username']} から支払い完了の連絡が届きました。",
                    f"/admin/vip?ticket={key}",
                    f"vip-payment-{key}",
                )
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/vip/purchase/paypay-link")
    @limiter.limit("10 per minute")
    def purchase_paypay_link():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = require_account()
            result = store.submit_paypay_link(
                data.get("purchase_key"),
                data.get("payment_link"),
                int(account["id"]),
                account["username"],
            )
            if result["changed"]:
                key = result["purchase_key"]
                _push_executor.submit(
                    _fanout,
                    store,
                    "admin",
                    f"{account['username']} からPayPay送金リンクが届きました。",
                    f"/admin/vip?ticket={key}",
                    f"vip-paypay-{key}",
                )
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/vip/purchase/close")
    @limiter.limit("10 per minute")
    def purchase_close():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = require_account()
            result = store.close_purchase_ticket(
                data.get("purchase_key"), "user", int(account["id"])
            )
            if result["changed"]:
                key = result["purchase_key"]
                _push_executor.submit(
                    _fanout,
                    store,
                    "admin",
                    f"{account['username']} がVIP購入チケットを削除済みに移動しました。",
                    "/admin/vip",
                    f"vip-purchase-{key}",
                )
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.get("/account-sw.js")
    def account_service_worker():
        response = make_response(send_from_directory(static_path, "account-sw.js"))
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @bp.post("/api/account/push/subscribe")
    @limiter.limit("20 per minute")
    def push_subscribe():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        if not os.getenv("CHAT_VAPID_PUBLIC_KEY", "").strip() or not os.getenv("CHAT_VAPID_PRIVATE_KEY", "").strip():
            return jsonify({"error": "通知機能が設定されていません。"}), 503
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            if data.get("admin"):
                require_admin()
                owner_key = "admin"
            else:
                owner_key = f"account:{int(require_account()['id'])}"
            store.save_subscription(owner_key, data.get("subscription"))
            return jsonify({"subscribed": True})
        except Exception as exc:
            return error_response(exc)

    @bp.delete("/api/account/push/subscribe")
    @limiter.limit("20 per minute")
    def push_unsubscribe():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            if data.get("admin"):
                require_admin()
                owner_key = "admin"
            else:
                owner_key = f"account:{int(require_account()['id'])}"
            store.remove_subscription(owner_key, data.get("endpoint"))
            return jsonify({"subscribed": False})
        except Exception as exc:
            return error_response(exc)

    @bp.get("/admin/vip")
    @limiter.limit("60 per minute")
    def vip_admin_page():
        if not current_discord_user():
            return redirect("/auth/login?next=/admin/vip")
        try:
            require_admin()
        except AccountPermissionError:
            return make_response("このDiscordアカウントは管理者に登録されていません。", 403)
        return page(
            "vip_admin.html",
            user=current_discord_user(),
            selected_key=str(request.args.get("ticket") or "").strip().upper(),
            plans=store.vip_plans(),
            payment_guide=os.getenv(
                "VIP_PAYMENT_GUIDE",
                "お支払い方法をご案内します。送金先と注意事項をご確認のうえ、お支払い後に「支払いが完了しました」を押してください。",
            ).strip(),
            vapid_public_key=os.getenv("CHAT_VAPID_PUBLIC_KEY", "").strip(),
        )

    @bp.get("/api/admin/vip/state")
    @limiter.limit("120 per minute")
    def vip_admin_state():
        try:
            require_admin()
            return jsonify(store.admin_overview(request.args.get("ticket", "")))
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/vip/messages")
    @limiter.limit("60 per minute")
    def vip_admin_message():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            admin_id = require_admin()
            message, account_id = store.send_purchase_message(
                data.get("purchase_key"), data.get("content"), "admin", "管理者"
            )
            key = str(data.get("purchase_key") or "").strip().upper()
            _push_executor.submit(
                _fanout, store, f"account:{account_id}", f"管理者: {message['content']}",
                f"/vip/purchase?key={key}", f"vip-purchase-{key}",
            )
            return jsonify({"message": message, "admin_id": admin_id}), 201
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/vip/close")
    @limiter.limit("30 per minute")
    def vip_admin_close():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            require_admin()
            result = store.close_purchase_ticket(data.get("purchase_key"), "admin")
            if result["changed"]:
                key = result["purchase_key"]
                _push_executor.submit(
                    _fanout,
                    store,
                    f"account:{result['account_id']}",
                    "管理者がVIP購入チケットを削除済みに移動しました。",
                    "/vip/purchase",
                    f"vip-purchase-{key}",
                )
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/vip/ticket-status")
    @limiter.limit("60 per minute")
    def vip_admin_ticket_status():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            admin_id = require_admin()
            result = store.set_ticket_status(data.get("purchase_key"), data.get("status"), admin_id)
            key = result["purchase_key"]
            _push_executor.submit(
                _fanout, store, f"account:{result['account_id']}", "VIP購入申請の状態が更新されました。",
                f"/vip/purchase?key={key}", f"vip-purchase-{key}",
            )
            return jsonify(result)
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/vip/grant")
    @limiter.limit("30 per minute")
    def vip_admin_grant():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            admin_id = require_admin()
            account = store.grant_vip(
                data.get("public_id"), data.get("duration_days"), admin_id, data.get("purchase_key", "")
            )
            _push_executor.submit(
                _fanout, store, f"account:{account['id']}", "VIPが付与されました。VIPページをご利用いただけます。",
                "/account", f"vip-granted-{account['public_id']}",
            )
            return jsonify({"account": account})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/vip/revoke")
    @limiter.limit("20 per minute")
    def vip_admin_revoke():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = store.revoke_vip(data.get("public_id"), require_admin())
            _push_executor.submit(
                _fanout, store, f"account:{account['id']}", "VIP契約状態が更新されました。",
                "/account", f"vip-revoked-{account['public_id']}",
            )
            return jsonify({"account": account})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/accounts/manage")
    @limiter.limit("30 per minute")
    def account_admin_manage():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            account = store.manage_account(data.get("public_id"), data.get("action"), require_admin())
            action = str(data.get("action") or "")
            messages = {
                "suspend": "サイトアカウントが一時停止されました。",
                "activate": "サイトアカウントが再開されました。",
                "ban": "サイトアカウントがBANされました。",
                "unban": "サイトアカウントのBANが解除されました。",
                "gban": "サイト全体へのアクセスがGBANされました。",
                "ungban": "サイト全体のGBANが解除されました。",
            }
            _push_executor.submit(
                _fanout, store, f"account:{account['id']}", messages.get(action, "アカウント状態が更新されました。"),
                "/account", f"account-managed-{account['public_id']}-{action}",
            )
            return jsonify({"account": account})
        except Exception as exc:
            return error_response(exc)

    @bp.post("/api/admin/accounts/update")
    @limiter.limit("30 per minute")
    def account_admin_update():
        if not _valid_csrf():
            return jsonify({"error": "ページを再読み込みしてください。"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "入力が不正です。"}), 400
        try:
            admin_id = require_admin()
            action = str(data.get("action") or "").strip()
            if action == "rename":
                account = store.rename_account(data.get("public_id"), data.get("username"), admin_id)
                body = "サイトアカウントのユーザー名が管理者によって変更されました。"
            else:
                raise AccountError("アカウント編集操作が不正です。")
            _push_executor.submit(
                _fanout, store, f"account:{account['id']}", body,
                "/account", f"account-updated-{account['public_id']}-{action}",
            )
            return jsonify({"account": account})
        except Exception as exc:
            return error_response(exc)

    app.register_blueprint(bp)
