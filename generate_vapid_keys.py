"""
Generate your own VAPID keys for push notifications.
Run this once: py generate_vapid_keys.py
Copy the output into your Render environment variables.
"""

import base64
from py_vapid import Vapid01 as Vapid
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


v = Vapid()
v.generate_keys()

public_raw = v.public_key.public_bytes(
    encoding=Encoding.X962,
    format=PublicFormat.UncompressedPoint,
)
private_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")

print("Copy these into your Render environment variables:\n")
print(f"VAPID_PUBLIC_KEY={b64url(public_raw)}")
print(f"VAPID_PRIVATE_KEY={b64url(private_raw)}")
print(f"VAPID_CLAIM_EMAIL=mailto:your-email@example.com")
print("\nAlso copy VAPID_PUBLIC_KEY into frontend/src/config.js")
