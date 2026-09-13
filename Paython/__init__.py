from .validator import (
    AMOUNT_MISMATCH_ERROR,
    LINK_LOOKUP_ERROR,
    LINK_NOT_PENDING_ERROR,
    PaymentLinkInfo,
    PaythonAmountMismatchError,
    PaythonLinkLookupError,
    PaythonLinkNotPendingError,
    PaythonLinkValidator,
    PaythonValidationError,
    validate_payment_link,
)

__version__ = "2.4.1-local"
__url__ = "https://github.com/taka-4602/PayPaython-mobile"


def __getattr__(name: str):
    if name in {"PayPay", "PayPayError", "PayPayLoginError", "PayPayNetWorkError"}:
        from . import main

        return getattr(main, name)
    raise AttributeError(name)

__all__ = [
    "AMOUNT_MISMATCH_ERROR",
    "LINK_LOOKUP_ERROR",
    "LINK_NOT_PENDING_ERROR",
    "PayPay",
    "PayPayError",
    "PayPayLoginError",
    "PayPayNetWorkError",
    "PaymentLinkInfo",
    "PaythonAmountMismatchError",
    "PaythonLinkLookupError",
    "PaythonLinkNotPendingError",
    "PaythonLinkValidator",
    "PaythonValidationError",
    "validate_payment_link",
]
