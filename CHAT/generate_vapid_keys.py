"""Generate one VAPID key pair for the chat Web Push configuration."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


private_key = ec.generate_private_key(ec.SECP256R1())
private_value = private_key.private_numbers().private_value.to_bytes(32, "big")
public_value = private_key.public_key().public_bytes(
    Encoding.X962, PublicFormat.UncompressedPoint
)

print(f"CHAT_VAPID_PUBLIC_KEY={b64url(public_value)}")
print(f"CHAT_VAPID_PRIVATE_KEY={b64url(private_value)}")

