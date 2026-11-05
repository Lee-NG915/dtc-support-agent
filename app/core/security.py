"""Log redaction and PII masking. Never log secrets or raw identifiers."""

from __future__ import annotations

import hashlib
import re
import secrets

from app.core.constants import PII_MASK

STAFF_TOKEN_PREFIX = "dtc_staff_"
STAFF_TOKEN_BYTES = 32
MAX_STAFF_TOKEN_LENGTH = 256

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "cookie",
        "secret",
    }
)

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD = re.compile(r"\b(\d{6})\d{8,10}(\d{2}[\dXx])\b")
_BANK_CARD = re.compile(r"\b(\d{4})\d{8,11}(\d{4})\b")
_ORDER_ID = re.compile(r"\b(ORD)[-_]?(\d{4})\d+(\d{2})\b", re.IGNORECASE)


def generate_staff_token() -> str:
    """Create a high-entropy opaque bearer token for the internal pilot."""
    return f"{STAFF_TOKEN_PREFIX}{secrets.token_urlsafe(STAFF_TOKEN_BYTES)}"


def hash_staff_token(token: str) -> str:
    """Return the only representation of a staff token that may be persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_bearer_token(authorization: str | None) -> str | None:
    """Parse one RFC 6750 Bearer credential without accepting extra parts."""
    if not authorization:
        return None
    scheme, separator, credentials = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = credentials.strip()
    if not token or " " in token or len(token) > MAX_STAFF_TOKEN_LENGTH:
        return None
    return token


def redact_mapping(payload: dict[str, object]) -> dict[str, object]:
    redacted: dict[str, object] = {}
    for key, value in payload.items():
        if key.lower() in _SECRET_KEYS:
            redacted[key] = PII_MASK
        elif isinstance(value, str):
            redacted[key] = mask_pii(value)
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value)  # type: ignore[arg-type]
        else:
            redacted[key] = value
    return redacted


def mask_pii(text: str) -> str:
    text = _PHONE.sub(rf"\1{PII_MASK}\2", text)
    text = _ID_CARD.sub(rf"\1{PII_MASK}\2", text)
    text = _BANK_CARD.sub(rf"\1{PII_MASK}\2", text)
    text = _ORDER_ID.sub(rf"\1{PII_MASK}\3", text)
    return text
