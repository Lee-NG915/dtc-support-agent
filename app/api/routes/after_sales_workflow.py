"""Authenticated internal after-sales workflow APIs.

No route here creates or queries an E-pig ticket. Approval only persists an
``approved`` decision and a ``prepared`` operation for the later worker slice.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies import get_container, get_required_staff
from app.connectors.errors import (
    ConnectorError,
    ConnectorGone,
    ConnectorNotFound,
    ConnectorUnavailable,
)
from app.connectors.models import RequestType
from app.core.exceptions import (
    AfterSalesDisabledError,
    AttachmentExpiredError,
    ConnectorContractError,
    ConnectorUnavailableError,
    ForbiddenMarketError,
    ForbiddenRoleError,
    InvalidRequestError,
    NotFoundError,
)
from app.schemas.after_sales import (
    AFTER_SALES_MARKETS,
    READ_ROLES,
    StaffActor,
)
from app.schemas.after_sales_workflow import (
    APPROVE_REJECT_ROLES,
    ApproveRequest,
    CaseDetail,
    CaseListResponse,
    CaseState,
    DraftCreateRequest,
    EventListResponse,
    MaterialCreateRequest,
    RejectRequest,
    SubmitRequest,
    SyncResponse,
)
from app.services.after_sales.service import AfterSalesWorkflow
from app.services.container import AppContainer

router = APIRouter(prefix="/after-sales", tags=["after-sales-workflow"])

_APPROVAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")


def get_after_sales_workflow(
    container: AppContainer = Depends(get_container),
) -> AfterSalesWorkflow:
    workflow = container.after_sales_workflow
    if workflow is None:
        if not container.settings.after_sales_enabled:
            raise AfterSalesDisabledError()
        raise ConnectorUnavailableError()
    return workflow


@router.get("/{market}/cases", response_model=CaseListResponse)
async def list_cases(
    market: str,
    status: CaseState | None = Query(default=None),
    request_type: RequestType | None = Query(default=None),
    updated_from: datetime | None = Query(default=None),
    updated_to: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseListResponse:
    _authorize_read(actor, market)
    return await workflow.list_cases(
        market,
        status=status,
        request_type=request_type,
        updated_from=updated_from,
        updated_to=updated_to,
        limit=limit,
        offset=offset,
    )


@router.post("/{market}/cases/sync", response_model=SyncResponse)
async def sync_cases(
    market: str,
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> SyncResponse:
    _authorize_support(actor, market)
    try:
        return await workflow.sync(actor, market)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.get("/{market}/cases/{case_id}", response_model=CaseDetail)
async def get_case(
    market: str,
    case_id: int = Path(ge=1),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_read(actor, market)
    return await workflow.get_case_detail(market, case_id)


@router.get("/{market}/cases/{case_id}/events", response_model=EventListResponse)
async def get_case_events(
    market: str,
    case_id: int = Path(ge=1),
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> EventListResponse:
    _authorize_read(actor, market)
    return await workflow.list_events(market, case_id, after_seq=after_seq, limit=limit)


@router.post("/{market}/cases/{case_id}/materials", response_model=CaseDetail)
async def add_material(
    market: str,
    request: MaterialCreateRequest,
    case_id: int = Path(ge=1),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_support(actor, market)
    return await workflow.add_material(actor, market, case_id, request)


@router.post("/{market}/cases/{case_id}/drafts", response_model=CaseDetail)
async def create_draft(
    market: str,
    request: DraftCreateRequest,
    case_id: int = Path(ge=1),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_support(actor, market)
    try:
        return await workflow.create_draft(actor, market, case_id, request)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.post(
    "/{market}/cases/{case_id}/drafts/{version}/submit",
    response_model=CaseDetail,
)
async def submit_draft(
    market: str,
    request: SubmitRequest,
    case_id: int = Path(ge=1),
    version: int = Path(ge=1),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_support(actor, market)
    try:
        return await workflow.submit_draft(actor, market, case_id, version, request)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.post(
    "/{market}/cases/{case_id}/approvals/{approval_id}/approve",
    response_model=CaseDetail,
)
async def approve(
    market: str,
    request: ApproveRequest,
    case_id: int = Path(ge=1),
    approval_id: str = Path(),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_approver(actor, market)
    _validate_approval_id(approval_id)
    try:
        return await workflow.approve(actor, market, case_id, approval_id, request)
    except ConnectorError as exc:
        _raise_connector_error(exc)


@router.post(
    "/{market}/cases/{case_id}/approvals/{approval_id}/reject",
    response_model=CaseDetail,
)
async def reject(
    market: str,
    request: RejectRequest,
    case_id: int = Path(ge=1),
    approval_id: str = Path(),
    actor: StaffActor = Depends(get_required_staff),
    workflow: AfterSalesWorkflow = Depends(get_after_sales_workflow),
) -> CaseDetail:
    _authorize_approver(actor, market)
    _validate_approval_id(approval_id)
    return await workflow.reject(actor, market, case_id, approval_id, request)


# ------------------------------------------------------------------ helpers


def _authorize_market(actor: StaffActor, market: str) -> None:
    if market not in AFTER_SALES_MARKETS:
        raise InvalidRequestError("invalid market")
    if not actor.may_read_market(market):
        raise ForbiddenMarketError()


def _authorize_read(actor: StaffActor, market: str) -> None:
    _authorize_market(actor, market)
    if not actor.has_role(*READ_ROLES):
        raise ForbiddenRoleError()


def _authorize_support(actor: StaffActor, market: str) -> None:
    _authorize_market(actor, market)
    if not actor.has_role("support"):
        raise ForbiddenRoleError()


def _authorize_approver(actor: StaffActor, market: str) -> None:
    _authorize_market(actor, market)
    if not actor.has_role(*APPROVE_REJECT_ROLES):
        raise ForbiddenRoleError()


def _validate_approval_id(value: str) -> None:
    if not _APPROVAL_ID_RE.fullmatch(value):
        raise InvalidRequestError("invalid approval_id")


def _raise_connector_error(exc: ConnectorError) -> NoReturn:
    if isinstance(exc, ConnectorNotFound):
        raise NotFoundError() from None
    if isinstance(exc, ConnectorGone):
        raise AttachmentExpiredError() from None
    if isinstance(exc, ConnectorUnavailable):
        raise ConnectorUnavailableError() from None
    raise ConnectorContractError() from None
