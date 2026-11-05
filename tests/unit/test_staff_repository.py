from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.security import (
    generate_staff_token,
    hash_staff_token,
    parse_bearer_token,
)
from app.core.time import utcnow
from app.repositories.db import init_schema
from app.repositories.staff_repository import StaffRepository


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
