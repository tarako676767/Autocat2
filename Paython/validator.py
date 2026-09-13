"""PayPay送金リンクをVIP購入申請と照合するための薄い検証層。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


AMOUNT_MISMATCH_ERROR = "金額不一致エラー"
LINK_NOT_PENDING_ERROR = "受け取り済またはキャンセルされています"
LINK_LOOKUP_ERROR = "送金リンクの状態を確認できませんでした"


class PaythonValidationError(Exception):
    """利用者へ安全に表示できるPaython検証エラー。"""


class PaythonAmountMismatchError(PaythonValidationError):
    pass


class PaythonLinkNotPendingError(PaythonValidationError):
    pass


class PaythonLinkLookupError(PaythonValidationError):
    pass


@dataclass(frozen=True)
class PaymentLinkInfo:
    amount: int
    status: str
    order_id: str


class PaythonLinkValidator:
    """添付されたPaythonの公開リンク検査をアプリ向けに正規化する。"""

    def __init__(self, client_factory: Callable[[], object] | None = None):
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client() -> object:
        from .main import PayPay

        return PayPay()

    def inspect(self, payment_link: str) -> PaymentLinkInfo:
        try:
            info = self._client_factory().link_check(payment_link, web_api=True)
            raw = info.raw if isinstance(getattr(info, "raw", None), dict) else {}
            payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
            raw_status = payload.get("orderStatus") or getattr(info, "status", "")
            status = str(raw_status or "").strip().upper()
            amount = int(getattr(info, "amount"))
            order_id = str(getattr(info, "order_id", "") or "")
            if not status or amount < 0:
                raise ValueError("invalid PayPay link response")
            return PaymentLinkInfo(amount=amount, status=status, order_id=order_id)
        except PaythonValidationError:
            raise
        except Exception as exc:
            raise PaythonLinkLookupError(LINK_LOOKUP_ERROR) from exc

    def validate(self, payment_link: str, expected_amount: int) -> PaymentLinkInfo:
        info = self.inspect(payment_link)
        if info.status != "PENDING":
            raise PaythonLinkNotPendingError(LINK_NOT_PENDING_ERROR)
        if info.amount != int(expected_amount):
            raise PaythonAmountMismatchError(AMOUNT_MISMATCH_ERROR)
        return info


def validate_payment_link(payment_link: str, expected_amount: int) -> PaymentLinkInfo:
    """既定クライアントでリンクの状態と金額を検証する。"""

    return PaythonLinkValidator().validate(payment_link, expected_amount)
