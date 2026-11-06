from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import (
    DEMO_SESSION_PREFIX,
    MAX_DEMO_SESSION_LENGTH,
    create_demo_session_token,
    generate_staff_token,
    hash_staff_token,
    parse_bearer_token,
    verify_demo_password,
    verify_demo_session_token,
)
from app.core.time import utcnow
from app.repositories.db import init_schema
from app.repositories.staff_repository import StaffRepository

DEMO_SECRET = "demo-session-secret-" + ("s" * 20)


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_token_is_stored_only_as_sha256(session_factory) -> None:
    token = generate_staff_token()
    async with session_factory() as session:
        row = await StaffRepository(session).create(
            staff_id="staff_support_au",
            display_name="Synthetic Support",
            token=token,
            roles=("support",),
            allowed_markets=("au",),
            expires_at=utcnow() + timedelta(days=1),
        )
        assert row.token_sha256 == hash_staff_token(token)
        assert row.token_sha256 != token
        await session.commit()

    async with session_factory() as session:
        actor = await StaffRepository(session).authenticate(token)
        assert actor is not None
        assert actor.staff_id == "staff_support_au"
        assert actor.roles == frozenset({"support"})
        assert actor.allowed_markets == frozenset({"au"})

    async with session_factory() as session:
        assert await StaffRepository(session).authenticate("wrong-token") is None
        assert await StaffRepository(session).authenticate(token + "x") is None


@pytest.mark.asyncio
async def test_expired_and_disabled_credentials_are_rejected(session_factory) -> None:
    expired = generate_staff_token()
    disabled = generate_staff_token()
    async with session_factory() as session:
        repository = StaffRepository(session)
        await repository.create(
            staff_id="staff_expired",
            display_name="Expired",
            token=expired,
            roles=("support",),
            allowed_markets=("au",),
            expires_at=utcnow() - timedelta(seconds=1),
        )
        await repository.create(
            staff_id="staff_disabled",
            display_name="Disabled",
            token=disabled,
            roles=("support",),
            allowed_markets=("au",),
            expires_at=utcnow() + timedelta(days=1),
            enabled=False,
        )
        await session.commit()

    async with session_factory() as session:
        repository = StaffRepository(session)
        assert await repository.authenticate(expired) is None
        assert await repository.authenticate(disabled) is None


@pytest.mark.asyncio
async def test_list_available_excludes_disabled_and_expired_identities(session_factory) -> None:
    async with session_factory() as session:
        repository = StaffRepository(session)
        for staff_id, enabled, expires_at in (
            ("staff_active_b", True, utcnow() + timedelta(days=1)),
            ("staff_active_a", True, utcnow() + timedelta(days=1)),
            ("staff_disabled", False, utcnow() + timedelta(days=1)),
            ("staff_expired", True, utcnow() - timedelta(seconds=1)),
        ):
            await repository.create(
                staff_id=staff_id,
                display_name=staff_id,
                token=generate_staff_token(),
                roles=("support",),
                allowed_markets=("au",),
                expires_at=expires_at,
                enabled=enabled,
            )
        await session.commit()

    async with session_factory() as session:
        actors = await StaffRepository(session).list_available()
        assert [actor.staff_id for actor in actors] == ["staff_active_a", "staff_active_b"]
        assert all(actor.roles == frozenset({"support"}) for actor in actors)


def _encode_demo_payload(payload: dict[str, object]) -> str:
    return (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        .rstrip(b"=")
        .decode("ascii")
    )


def _signed_demo_payload(payload: dict[str, object], secret: str = DEMO_SECRET) -> str:
    encoded = _encode_demo_payload(payload)
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{DEMO_SESSION_PREFIX}{encoded}".encode(),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{DEMO_SESSION_PREFIX}{encoded}.{encoded_signature}"


def test_demo_session_round_trip_and_expiry_boundary() -> None:
    now = utcnow()
    token = create_demo_session_token(
        staff_id="staff_support_au",
        secret=DEMO_SECRET,
        ttl_seconds=60,
        now=now,
    )
    assert token.startswith(DEMO_SESSION_PREFIX)
    assert verify_demo_session_token(token, secret=DEMO_SECRET, now=now) == "staff_support_au"
    assert (
        verify_demo_session_token(
            token,
            secret=DEMO_SECRET,
            now=now + timedelta(seconds=59),
        )
        == "staff_support_au"
    )
    assert (
        verify_demo_session_token(
            token,
            secret=DEMO_SECRET,
            now=now + timedelta(seconds=60),
        )
        is None
    )


def test_demo_session_rejects_tampering_malformed_and_wrong_secret() -> None:
    now = utcnow()
    token = create_demo_session_token(
        staff_id="staff_support_au",
        secret=DEMO_SECRET,
        ttl_seconds=600,
        now=now,
    )
    prefix, encoded_payload, encoded_signature = token.split(".")
    original = {
        "staff_id": "staff_support_au",
        "expires_at": int(now.timestamp()) + 600,
        "version": 1,
    }
    assert verify_demo_session_token(token, secret=DEMO_SECRET, now=now) == "staff_support_au"

    def unsigned(value: dict[str, object]) -> str:
        return f"{prefix}.{_encode_demo_payload(value)}.{encoded_signature}"

    # Payload edits keep the old signature, so both must fail closed.
    assert (
        verify_demo_session_token(
            unsigned({**original, "staff_id": "staff_approver_au"}),
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert (
        verify_demo_session_token(
            unsigned({**original, "expires_at": int(now.timestamp()) + 9999}),
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert (
        verify_demo_session_token(
            token[:-1] + ("A" if token[-1] != "A" else "B"),
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert verify_demo_session_token(token, secret=DEMO_SECRET + "x", now=now) is None
    assert verify_demo_session_token(token, secret="too-short", now=now) is None
    assert verify_demo_session_token("demo-password-123", secret=DEMO_SECRET, now=now) is None
    assert (
        verify_demo_session_token(
            f"{prefix}.{encoded_payload}",
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert (
        verify_demo_session_token(
            f"{prefix}.{encoded_payload}.{encoded_signature}.extra",
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert (
        verify_demo_session_token(
            f"{prefix}.not-base64!.{encoded_signature}",
            secret=DEMO_SECRET,
            now=now,
        )
        is None
    )
    assert (
        verify_demo_session_token("x" * (MAX_DEMO_SESSION_LENGTH + 1), secret=DEMO_SECRET) is None
    )

    # A correctly signed but structurally invalid payload is still refused.
    for payload_override in (
        {**original, "version": 2},
        {**original, "extra": True},
        {**original, "staff_id": ""},
        {**original, "staff_id": "x" * 65},
        {**original, "expires_at": True},
        {**original, "expires_at": float(original["expires_at"])},
    ):
        assert (
            verify_demo_session_token(
                _signed_demo_payload(payload_override, secret=DEMO_SECRET),
                secret=DEMO_SECRET,
                now=now,
            )
            is None
        )


def test_demo_password_requires_the_exact_shared_value() -> None:
    assert verify_demo_password("demo-password-123", "demo-password-123")
    assert not verify_demo_password("demo-password-124", "demo-password-123")
    assert not verify_demo_password("", "demo-password-123")
    assert not verify_demo_password("anything", "")


@pytest.mark.asyncio
async def test_role_and_market_revocation_applies_to_the_next_request(session_factory) -> None:
    token = generate_staff_token()
    async with session_factory() as session:
        await StaffRepository(session).create(
            staff_id="staff_approver",
            display_name="Synthetic Approver",
            token=token,
            roles=("approver",),
            allowed_markets=("au", "sg"),
            expires_at=utcnow() + timedelta(days=1),
        )
        await session.commit()

    async with session_factory() as session:
        actor = await StaffRepository(session).authenticate(token)
        assert actor is not None
        assert actor.has_role("approver")
        assert actor.may_read_market("sg")

    async with session_factory() as session:
        from app.repositories.models import AfterSalesStaffRow

        row = await session.get(AfterSalesStaffRow, "staff_approver")
        assert row is not None
        row.roles = "auditor"
        row.allowed_markets = "us"
        await session.commit()

    async with session_factory() as session:
        actor = await StaffRepository(session).authenticate(token)
        assert actor is not None
        assert actor.roles == frozenset({"auditor"})
        assert actor.allowed_markets == frozenset({"us"})
        assert not actor.may_read_market("au")


def test_bearer_parser_rejects_malformed_headers() -> None:
    assert parse_bearer_token("Bearer dtc_staff_abc") == "dtc_staff_abc"
    assert parse_bearer_token("bearer dtc_staff_abc") == "dtc_staff_abc"
    assert parse_bearer_token(None) is None
    assert parse_bearer_token("") is None
    assert parse_bearer_token("Basic abc") is None
    assert parse_bearer_token("Bearer") is None
    assert parse_bearer_token("Bearer two tokens") is None
    assert parse_bearer_token("Bearer " + ("x" * 500)) is None
