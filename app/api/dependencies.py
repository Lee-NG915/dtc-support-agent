"""FastAPI dependency providers."""

from fastapi import Depends, Request

from app.core.exceptions import AfterSalesDisabledError, UnauthorizedError
from app.core.security import (
    DEMO_SESSION_PREFIX,
    parse_bearer_token,
    verify_demo_session_token,
)
from app.core.time import utcnow
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import StaffActor
from app.services.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


async def _authenticate_staff(request: Request, container: AppContainer) -> StaffActor | None:
    token = parse_bearer_token(request.headers.get("authorization"))
    if token is None:
        return None
    if token.startswith(DEMO_SESSION_PREFIX):
        settings = container.settings
        if not settings.after_sales_demo_login_active:
            return None
        staff_id = verify_demo_session_token(
            token,
            secret=settings.after_sales_demo_session_secret.get_secret_value(),
            now=utcnow(),
        )
        if staff_id is None:
            return None
        async with container.session_factory() as session:
            # Roles, markets, enabled and expiry are always current, so a
            # revocation or role change applies to the very next request.
            return await StaffRepository(session).get_current_actor(staff_id)
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
