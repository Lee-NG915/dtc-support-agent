"""GitHub OAuth login for the consumer platform.

Authorization-code flow with a single-use, short-lived state held in memory.
Intended for the single-process demo deployment; a multi-instance deployment
must move the state store to shared storage (documented in the design doc).
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.api.dependencies import (
    clear_consumer_cookie,
    get_container,
    get_optional_consumer,
    set_consumer_cookie,
)
from app.core.exceptions import UnauthorizedError
from app.core.security import create_consumer_session_token
from app.core.time import utcnow
from app.services.container import AppContainer

router = APIRouter(prefix="/auth", tags=["consumer-auth"])

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_STATE_TTL_SECONDS = 600
_states: dict[str, float] = {}


def _new_state() -> str:
    now = time.time()
    for key, created in list(_states.items()):
        if now - created > _STATE_TTL_SECONDS:
            _states.pop(key, None)
    state = secrets.token_urlsafe(24)
    _states[state] = now
    return state


def _consume_state(state: str) -> bool:
    created = _states.pop(state, None)
    return created is not None and time.time() - created <= _STATE_TTL_SECONDS


def _require_oauth_config(container: AppContainer) -> tuple[str, str, str]:
    settings = container.settings
    if not settings.consumer_auth_enabled:
        raise UnauthorizedError("consumer sign-in is not enabled")
    client_id = settings.github_client_id.strip()
    client_secret = settings.github_client_secret.get_secret_value()
    redirect_url = settings.github_oauth_redirect_url.strip()
    if not client_id or not client_secret or not redirect_url:
        raise UnauthorizedError("GitHub sign-in is not configured")
    return client_id, client_secret, redirect_url


@router.get("/github/login")
async def github_login(container: AppContainer = Depends(get_container)) -> Response:
    client_id, _, redirect_url = _require_oauth_config(container)
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_url,
            "scope": "read:user",
            "state": _new_state(),
        }
    )
    return RedirectResponse(f"{_AUTHORIZE_URL}?{params}", status_code=302)


@router.get("/github/callback")
async def github_callback(
    code: str = "",
    state: str = "",
    container: AppContainer = Depends(get_container),
) -> Response:
    client_id, client_secret, redirect_url = _require_oauth_config(container)
    if not code or not state or not _consume_state(state):
        raise UnauthorizedError("invalid or expired OAuth state")
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_response = await client.post(
            _TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_url,
            },
        )
        if token_response.status_code != 200:
            raise UnauthorizedError("GitHub token exchange failed")
        token_payload = token_response.json()
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UnauthorizedError("GitHub token exchange failed")
        user_response = await client.get(
            _USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if user_response.status_code != 200:
            raise UnauthorizedError("failed to read GitHub profile")
        profile = user_response.json()

    github_id = profile.get("id")
    login = profile.get("login")
    if not isinstance(github_id, int) or not isinstance(login, str) or not login:
        raise UnauthorizedError("failed to read GitHub profile")
    settings = container.settings
    token = create_consumer_session_token(
        github_id=str(github_id),
        login=login,
        secret=settings.consumer_session_secret.get_secret_value(),
        ttl_seconds=settings.consumer_session_ttl_seconds,
        now=utcnow(),
    )
    response = RedirectResponse("/", status_code=302)
    set_consumer_cookie(response, token, settings.consumer_session_ttl_seconds)
    return response


@router.post("/logout")
async def logout(container: AppContainer = Depends(get_container)) -> Response:
    response = JSONResponse({"signed_out": True})
    clear_consumer_cookie(response)
    return response


@router.get("/me")
async def me(
    container: AppContainer = Depends(get_container),
    actor=Depends(get_optional_consumer),
) -> dict[str, object]:
    if not container.settings.consumer_auth_enabled:
        return {"auth_enabled": False, "signed_in": False}
    if actor is None:
        return {"auth_enabled": True, "signed_in": False}
    return {
        "auth_enabled": True,
        "signed_in": True,
        "github_id": actor.github_id,
        "login": actor.login,
        "demo_data_available": actor.demo_user_id is not None,
    }
