"""FastAPI dependency providers."""

from fastapi import Depends, Request

from app.core.exceptions import AfterSalesDisabledError, UnauthorizedError
from app.core.security import parse_bearer_token
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import StaffActor
from app.services.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


async def _authenticate_staff(request: Request, container: AppContainer) -> StaffActor | None:
    token = parse_bearer_token(request.headers.get("authorization"))
    if token is None:
        return None
    async with container.session_factory() as session:
        return await StaffRepository(session).authenticate(token)


async def get_optional_staff(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> StaffActor | None:
    """Require staff auth only when the after-sales feature is enabled."""
    if not container.settings.after_sales_enabled:
        return None
    actor = await _authenticate_staff(request, container)
    if actor is None:
        raise UnauthorizedError("invalid or expired staff credential")
    return actor


async def get_required_staff(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> StaffActor:
    if not container.settings.after_sales_enabled:
        raise AfterSalesDisabledError()
    actor = await _authenticate_staff(request, container)
    if actor is None:
        raise UnauthorizedError("invalid or expired staff credential")
    return actor
