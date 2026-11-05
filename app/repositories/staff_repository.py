"""Staff credential persistence and per-request authentication."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DatabaseError
from app.core.security import MAX_STAFF_TOKEN_LENGTH, hash_staff_token
from app.core.time import as_utc, utcnow
from app.repositories.models import AfterSalesStaffRow
from app.schemas.after_sales import (
    AfterSalesMarket,
    StaffActor,
    StaffRole,
    parse_staff_markets,
    parse_staff_roles,
)


class StaffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authenticate(self, token: str | None) -> StaffActor | None:
        """Resolve a bearer token against current database state.

        Disabled, expired and revoked credentials fail closed. Nothing is
        cached, so a database update applies to the next request immediately.
        """
        if not token or len(token) > MAX_STAFF_TOKEN_LENGTH:
            return None
        try:
            result = await self._session.execute(
                select(AfterSalesStaffRow).where(
                    AfterSalesStaffRow.token_sha256 == hash_staff_token(token)
                )
            )
            row = result.scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to authenticate staff") from exc
        if row is None or not row.enabled or as_utc(row.expires_at) <= utcnow():
            return None
        return StaffActor(
            staff_id=row.staff_id,
            display_name=row.display_name,
            roles=parse_staff_roles(row.roles),
            allowed_markets=parse_staff_markets(row.allowed_markets),
        )

    async def create(
        self,
        *,
        staff_id: str,
        display_name: str,
        token: str,
        roles: Iterable[StaffRole],
        allowed_markets: Iterable[AfterSalesMarket],
        expires_at: datetime,
        enabled: bool = True,
    ) -> AfterSalesStaffRow:
        """Persist a staff row. The plaintext token is never stored."""
        row = AfterSalesStaffRow(
            staff_id=staff_id,
            display_name=display_name,
            token_sha256=hash_staff_token(token),
            enabled=enabled,
            expires_at=expires_at,
            roles=",".join(sorted(set(roles))),
            allowed_markets=",".join(sorted(set(allowed_markets))),
        )
        self._session.add(row)
        await self._session.flush()
        return row
