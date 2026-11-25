"""FastAPI dependency providers."""

from fastapi import Depends, Request
from fastapi.responses import Response

from app.core.exceptions import AfterSalesDisabledError, UnauthorizedError
from app.core.security import (
    DEMO_SESSION_PREFIX,
    parse_bearer_token,
    verify_consumer_session_token,
    verify_demo_session_token,
)
from app.core.time import utcnow
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import StaffActor
from app.services.container import AppContainer

CONSUMER_SESSION_COOKIE = "dtc_consumer_session"


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


class ConsumerActor:
    def __init__(self, github_id: str, login: str, demo_user_id: str | None) -> None:
        self.github_id = github_id
        self.login = login
        self.demo_user_id = demo_user_id

    @property
    def effective_user_id(self) -> str:
        return self.demo_user_id or f"gh_{self.github_id}"


def get_optional_consumer(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ConsumerActor | None:
    """Resolve the GitHub session cookie into a consumer actor when enabled."""
    if not container.settings.consumer_auth_enabled:
        return None
    token = request.cookies.get(CONSUMER_SESSION_COOKIE)
    if not token:
        return None
    verified = verify_consumer_session_token(
        token,
        secret=container.settings.consumer_session_secret.get_secret_value(),
        now=utcnow(),
    )
    if verified is None:
        return None
    github_id, login = verified
    from app.connectors.consumer import demo_user_for

    return ConsumerActor(
        github_id,
        login,
        demo_user_for(github_id, container.settings.consumer_demo_ids),
    )


def get_required_consumer(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ConsumerActor:
    if not container.settings.consumer_auth_enabled:
        raise AfterSalesDisabledError()
    actor = get_optional_consumer(request, container)
    if actor is None:
        raise UnauthorizedError("sign in with GitHub to view your data")
    return actor


def set_consumer_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        CONSUMER_SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_consumer_cookie(response: Response) -> None:
    response.delete_cookie(CONSUMER_SESSION_COOKIE, path="/")


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
