"""Log redaction and PII masking. Never log secrets or raw identifiers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime

from app.core.constants import PII_MASK

STAFF_TOKEN_PREFIX = "dtc_staff_"
STAFF_TOKEN_BYTES = 32
MAX_STAFF_TOKEN_LENGTH = 256

# Stateless, short-lived demo sessions. The payload binds one existing staff
# identity to an expiry and a schema version; the signature is the only thing
# that makes it a credential. The shared demo password is never stored here.
DEMO_SESSION_PREFIX = "dtc_demo."
DEMO_SESSION_VERSION = 1
MAX_DEMO_SESSION_LENGTH = 512
MAX_DEMO_STAFF_ID_LENGTH = 64
_DEMO_PAYLOAD_KEYS = frozenset({"staff_id", "expires_at", "version"})

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


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _demo_signature(secret: str, encoded_payload: str) -> bytes:
    message = f"{DEMO_SESSION_PREFIX}{encoded_payload}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def create_demo_session_token(
    *,
    staff_id: str,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """Issue a signed demo session for one existing staff identity.

    The token contains only the staff id, an absolute expiry and the token
    schema version. Every authorization decision still re-reads the staff row.
    """
    if not staff_id or len(staff_id) > MAX_DEMO_STAFF_ID_LENGTH:
        raise ValueError("invalid demo staff id")
    if len(secret.strip()) < 32:
        raise ValueError("demo session secret must be at least 32 characters")
    issued_at = now or datetime.now(UTC)
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    payload = {
        "staff_id": staff_id,
        "expires_at": int(issued_at.timestamp()) + ttl_seconds,
        "version": DEMO_SESSION_VERSION,
    }
    encoded_payload = _base64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _demo_signature(secret, encoded_payload)
    return f"{DEMO_SESSION_PREFIX}{encoded_payload}.{_base64url_encode(signature)}"


def verify_demo_session_token(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> str | None:
    """Return the bound staff id only for an intact, current token.

    Signature, payload shape, version and expiry are all checked. Any edit to
    the payload or signature, an unknown format or a misconfigured secret fails
    closed. The caller must still load current roles and enabled/expiry state.
    """
    if not token or len(token) > MAX_DEMO_SESSION_LENGTH:
        return None
    if not token.startswith(DEMO_SESSION_PREFIX):
        return None
    if len(secret.strip()) < 32:
        return None
    parts = token[len(DEMO_SESSION_PREFIX) :].split(".")
    if len(parts) != 2:
        return None
    encoded_payload, encoded_signature = parts
    if not encoded_payload or not encoded_signature:
        return None
    try:
        signature = _base64url_decode(encoded_signature)
    except (binascii.Error, ValueError):
        return None
    # Reject non-canonical encodings: base64url's unused trailing bits could
    # otherwise let a different token string decode to the same bytes.
    if _base64url_encode(signature) != encoded_signature:
        return None
    expected = _demo_signature(secret, encoded_payload)
    if not hmac.compare_digest(signature, expected):
        return None
    # Only parse the payload after its signature has been authenticated.
    try:
        payload_bytes = _base64url_decode(encoded_payload)
    except (binascii.Error, ValueError):
        return None
    if _base64url_encode(payload_bytes) != encoded_payload:
        return None
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != _DEMO_PAYLOAD_KEYS:
        return None
    staff_id = payload.get("staff_id")
    expires_at = payload.get("expires_at")
    version = payload.get("version")
    if (
        not isinstance(staff_id, str)
        or not staff_id
        or len(staff_id) > MAX_DEMO_STAFF_ID_LENGTH
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or version != DEMO_SESSION_VERSION
    ):
        return None
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if expires_at <= int(current.timestamp()):
        return None
    return staff_id


def verify_demo_password(candidate: str, expected: str) -> bool:
    """Compare the shared demo password without length-dependent shortcuts."""
    if not expected:
        return False
    candidate_digest = hashlib.sha256(candidate.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(candidate_digest, expected_digest)


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
