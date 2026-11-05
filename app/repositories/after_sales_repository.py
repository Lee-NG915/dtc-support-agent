"""Persistence helpers for the persistent after-sales workflow.

Business transactions and revision checks live in the workflow service; this
module keeps SQLAlchemy queries small and testable. No query here trusts a
request-scoped market or case id without both being supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DatabaseError
from app.core.time import utcnow
from app.repositories.models import (
    AfterSalesApprovalRow,
    AfterSalesCaseRow,
    AfterSalesDraftRow,
    AfterSalesEventRow,
    AfterSalesMaterialRow,
    AfterSalesOperationRow,
    AfterSalesSyncCursorRow,
)


class AfterSalesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ cases

    async def get_case(self, market: str, case_id: int) -> AfterSalesCaseRow | None:
        return await self._session.scalar(
            select(AfterSalesCaseRow).where(
                AfterSalesCaseRow.market == market,
                AfterSalesCaseRow.case_id == case_id,
            )
        )

    async def get_case_by_request(self, market: str, request_id: str) -> AfterSalesCaseRow | None:
        return await self._session.scalar(
            select(AfterSalesCaseRow).where(
                AfterSalesCaseRow.market == market,
                AfterSalesCaseRow.request_id == request_id,
            )
        )

    async def cas_case(
        self,
        *,
        market: str,
        case_id: int,
        expected_revision: int,
        values: dict[str, object],
    ) -> bool:
        """Apply one conditional update. False means another writer won."""
        values = {
            **values,
            "revision": expected_revision + 1,
            "updated_at": utcnow(),
        }
        try:
            result = await self._session.execute(
                update(AfterSalesCaseRow)
                .where(
                    AfterSalesCaseRow.market == market,
                    AfterSalesCaseRow.case_id == case_id,
                    AfterSalesCaseRow.revision == expected_revision,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to update the case") from exc
        return getattr(result, "rowcount", 0) == 1

    # ----------------------------------------------------------------- cursor

    async def get_cursor(self, market: str) -> AfterSalesSyncCursorRow | None:
        return await self._session.get(AfterSalesSyncCursorRow, market)

    async def advance_cursor(
        self,
        *,
        market: str,
        expected_revision: int,
        cursor: str | None,
        high_watermark: str,
        last_success_at: datetime,
    ) -> bool:
        """Atomically advance one market cursor from a known revision.

        The predicate is part of the UPDATE, so PostgreSQL decides the winner
        instead of a Python read-check-write sequence. False means another
        sync committed first; the caller must roll back this whole batch.
        """
        try:
            result = await self._session.execute(
                update(AfterSalesSyncCursorRow)
                .where(
                    AfterSalesSyncCursorRow.market == market,
                    AfterSalesSyncCursorRow.revision == expected_revision,
                )
                .values(
                    cursor=cursor,
                    high_watermark=high_watermark,
                    revision=expected_revision + 1,
                    last_success_at=last_success_at,
                    updated_at=last_success_at,
                )
                .execution_options(synchronize_session=False)
            )
        except SQLAlchemyError as exc:
            raise DatabaseError("failed to advance the sync cursor") from exc
        return getattr(result, "rowcount", 0) == 1

    # -------------------------------------------------------------- materials

    async def list_materials(self, case_id: int) -> list[AfterSalesMaterialRow]:
        result = await self._session.scalars(
            select(AfterSalesMaterialRow)
            .where(AfterSalesMaterialRow.case_id == case_id)
            .order_by(AfterSalesMaterialRow.created_at, AfterSalesMaterialRow.material_id)
        )
        return list(result)

    # ----------------------------------------------------------------- drafts

    async def list_drafts(self, case_id: int) -> list[AfterSalesDraftRow]:
        result = await self._session.scalars(
            select(AfterSalesDraftRow)
            .where(AfterSalesDraftRow.case_id == case_id)
            .order_by(AfterSalesDraftRow.version)
        )
        return list(result)

    async def get_draft_by_id(self, draft_id: str) -> AfterSalesDraftRow | None:
        return await self._session.get(AfterSalesDraftRow, draft_id)

    async def get_draft(self, case_id: int, version: int) -> AfterSalesDraftRow | None:
        return await self._session.scalar(
            select(AfterSalesDraftRow).where(
                AfterSalesDraftRow.case_id == case_id,
                AfterSalesDraftRow.version == version,
            )
        )

    async def next_draft_version(self, case_id: int) -> int:
        current = await self._session.scalar(
            select(func.max(AfterSalesDraftRow.version)).where(
                AfterSalesDraftRow.case_id == case_id
            )
        )
        return int(current or 0) + 1

    # -------------------------------------------------------------- approvals

    async def get_approval(self, case_id: int, approval_id: str) -> AfterSalesApprovalRow | None:
        return await self._session.scalar(
            select(AfterSalesApprovalRow).where(
                AfterSalesApprovalRow.case_id == case_id,
                AfterSalesApprovalRow.approval_id == approval_id,
            )
        )

    async def list_approvals(self, case_id: int) -> list[AfterSalesApprovalRow]:
        result = await self._session.scalars(
            select(AfterSalesApprovalRow)
            .where(AfterSalesApprovalRow.case_id == case_id)
            .order_by(AfterSalesApprovalRow.created_at)
        )
        return list(result)

    async def latest_pending_approval(self, case_id: int) -> AfterSalesApprovalRow | None:
        return await self._session.scalar(
            select(AfterSalesApprovalRow)
            .where(
                AfterSalesApprovalRow.case_id == case_id,
                AfterSalesApprovalRow.status == "pending",
            )
            .order_by(AfterSalesApprovalRow.created_at.desc())
            .limit(1)
        )

    async def latest_unresolved_approval(self, case_id: int) -> AfterSalesApprovalRow | None:
        """Pending or approved context that a local content change can retire."""
        return await self._session.scalar(
            select(AfterSalesApprovalRow)
            .where(
                AfterSalesApprovalRow.case_id == case_id,
                AfterSalesApprovalRow.status.in_(("pending", "approved")),
            )
            .order_by(AfterSalesApprovalRow.created_at.desc())
            .limit(1)
        )

    # ------------------------------------------------------------- operations

    async def list_operations(self, case_id: int) -> list[AfterSalesOperationRow]:
        result = await self._session.scalars(
            select(AfterSalesOperationRow)
            .where(AfterSalesOperationRow.case_id == case_id)
            .order_by(AfterSalesOperationRow.created_at)
        )
        return list(result)

    async def get_operation_by_approval(self, approval_id: str) -> AfterSalesOperationRow | None:
        return await self._session.scalar(
            select(AfterSalesOperationRow).where(AfterSalesOperationRow.approval_id == approval_id)
        )

    async def active_operation(self, case_id: int) -> AfterSalesOperationRow | None:
        from app.repositories.models import ACTIVE_OPERATION_STATUSES

        return await self._session.scalar(
            select(AfterSalesOperationRow)
            .where(
                AfterSalesOperationRow.case_id == case_id,
                AfterSalesOperationRow.status.in_(ACTIVE_OPERATION_STATUSES),
            )
            .order_by(AfterSalesOperationRow.created_at.desc())
            .limit(1)
        )

    async def prepared_operation(self, case_id: int) -> AfterSalesOperationRow | None:
        return await self._session.scalar(
            select(AfterSalesOperationRow)
            .where(
                AfterSalesOperationRow.case_id == case_id,
                AfterSalesOperationRow.status == "prepared",
                AfterSalesOperationRow.dispatched_at.is_(None),
            )
            .order_by(AfterSalesOperationRow.created_at.desc())
            .limit(1)
        )

    # ----------------------------------------------------------------- events

    async def append_event(
        self,
        *,
        case_id: int,
        event_type: str,
        actor_staff_id: str | None = None,
        old_state: str | None = None,
        new_state: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        reason: str | None = None,
    ) -> AfterSalesEventRow:
        current = await self._session.scalar(
            select(func.max(AfterSalesEventRow.seq)).where(AfterSalesEventRow.case_id == case_id)
        )
        event = AfterSalesEventRow(
            case_id=case_id,
            seq=int(current or 0) + 1,
            event_type=event_type,
            actor_staff_id=actor_staff_id,
            old_state=old_state,
            new_state=new_state,
            entity_type=entity_type,
            entity_id=entity_id,
            reason=reason,
            created_at=utcnow(),
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_events(
        self, case_id: int, *, after_seq: int, limit: int
    ) -> Sequence[AfterSalesEventRow]:
        result = await self._session.scalars(
            select(AfterSalesEventRow)
            .where(
                AfterSalesEventRow.case_id == case_id,
                AfterSalesEventRow.seq > after_seq,
            )
            .order_by(AfterSalesEventRow.seq)
            .limit(limit + 1)
        )
        return list(result)

    async def pending_case_ids(self, case_ids: Sequence[int]) -> set[int]:
        if not case_ids:
            return set()
        result = await self._session.scalars(
            select(AfterSalesApprovalRow.case_id).where(
                AfterSalesApprovalRow.case_id.in_(case_ids),
                AfterSalesApprovalRow.status == "pending",
            )
        )
        return set(result)

    async def prepared_case_ids(self, case_ids: Sequence[int]) -> set[int]:
        if not case_ids:
            return set()
        result = await self._session.scalars(
            select(AfterSalesOperationRow.case_id).where(
                AfterSalesOperationRow.case_id.in_(case_ids),
                AfterSalesOperationRow.status == "prepared",
                AfterSalesOperationRow.dispatched_at.is_(None),
            )
        )
        return set(result)

    async def update_approval(
        self,
        approval: AfterSalesApprovalRow,
        *,
        status: str,
        decided_by_staff_id: str | None,
        reason: str | None,
        now: datetime,
    ) -> None:
        approval.status = status
        approval.decided_by_staff_id = decided_by_staff_id
        approval.decision_reason = reason
        approval.decided_at = now
        await self._session.flush()

    async def update_draft_status(
        self, draft: AfterSalesDraftRow, status: str, *, now: datetime
    ) -> None:
        draft.status = status
        if status in {"pending_approval", "approved", "rejected", "invalidated", "superseded"}:
            draft.decided_at = now if status != "pending_approval" else draft.decided_at
        if status == "pending_approval":
            draft.submitted_at = now
        await self._session.flush()

    async def fail_prepared_operation(
        self,
        operation: AfterSalesOperationRow,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        operation.status = "failed"
        operation.failure_reason = reason
        operation.updated_at = now
        await self._session.flush()
