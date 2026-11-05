"""Safe connector errors and source-contract error classification."""

from __future__ import annotations

from typing import Any

from app.connectors.models import ErrorResponse, RecoveryAction

ERROR_STATUSES: dict[str, set[int]] = {
    "VALIDATION_ERROR": {400},
    "UNSUPPORTED_CONTRACT_VERSION": {400},
    "INVALID_CURSOR": {400},
    "UNAUTHORIZED": {401},
    "FORBIDDEN_MARKET": {403},
    "NOT_FOUND": {404},
    "IDEMPOTENCY_CONFLICT": {409},
    "REQUEST_STATE_CONFLICT": {409},
    "ORDER_VERSION_CONFLICT": {409},
    "ATTACHMENT_EXPIRED": {410},
    "ATTACHMENT_TOO_LARGE": {413},
    "UNSUPPORTED_MEDIA_TYPE": {415},
    "RATE_LIMITED": {429},
    "INTERNAL_ERROR": {500},
    "UPSTREAM_UNAVAILABLE": {503},
}

SAFE_MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "the upstream rejected the request as invalid",
    "UNSUPPORTED_CONTRACT_VERSION": "the upstream does not support wire contract major 1",
    "INVALID_CURSOR": "the upstream rejected the ingestion cursor",
    "UNAUTHORIZED": "the service credential was rejected",
    "FORBIDDEN_MARKET": "the service credential is not authorized for this market",
    "NOT_FOUND": "the requested resource was not found",
    "IDEMPOTENCY_CONFLICT": "the idempotency key is already bound to different business fields",
    "REQUEST_STATE_CONFLICT": "the request state cannot accept this write",
    "ORDER_VERSION_CONFLICT": "the order version changed before the upstream write",
    "ATTACHMENT_EXPIRED": "the attachment has expired",
    "ATTACHMENT_TOO_LARGE": "the attachment exceeds the upstream size limit",
    "UNSUPPORTED_MEDIA_TYPE": "the attachment media type is unsupported",
    "RATE_LIMITED": "the upstream rate limit was exceeded",
    "INTERNAL_ERROR": "the upstream reported an internal failure",
    "UPSTREAM_UNAVAILABLE": "the upstream is unavailable",
}

CONFLICT_CODES = {
    "IDEMPOTENCY_CONFLICT",
    "REQUEST_STATE_CONFLICT",
    "ORDER_VERSION_CONFLICT",
}
VALIDATION_CODES = {
    "VALIDATION_ERROR",
    "UNSUPPORTED_CONTRACT_VERSION",
    "INVALID_CURSOR",
    "ATTACHMENT_TOO_LARGE",
    "UNSUPPORTED_MEDIA_TYPE",
}


class ConnectorError(Exception):
    """An error that is safe to classify without exposing the raw response."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        recovery_action: RecoveryAction,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
        outcome_unknown: bool = False,
        contract_violation: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.recovery_action = recovery_action
        self.http_status = http_status
        self.details = details or {}
        self.outcome_unknown = outcome_unknown
        self.contract_violation = contract_violation


class ConnectorNotFound(ConnectorError):
    def __init__(self, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code="NOT_FOUND",
            message=SAFE_MESSAGES["NOT_FOUND"],
            retryable=False,
            recovery_action="NONE",
            http_status=404,
            details=details,
        )


class ConnectorGone(ConnectorError):
    def __init__(self, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code="ATTACHMENT_EXPIRED",
            message=SAFE_MESSAGES["ATTACHMENT_EXPIRED"],
            retryable=False,
            recovery_action="NONE",
            http_status=410,
            details=details,
        )


class ConnectorPermissionError(ConnectorError):
    def __init__(self, code: str = "FORBIDDEN_MARKET", *, http_status: int = 403) -> None:
        super().__init__(
            code=code,
            message=SAFE_MESSAGES.get(code, "the service credential cannot perform this request"),
            retryable=False,
            recovery_action="NONE",
            http_status=http_status,
        )


class ConnectorConflict(ConnectorError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code=code,
            message=SAFE_MESSAGES.get(code, "the upstream rejected a conflicting write"),
            retryable=False,
            recovery_action="NONE",
            http_status=409,
            details=details,
        )


class ConnectorValidationError(ConnectorError):
    def __init__(
        self,
        code: str = "VALIDATION_ERROR",
        *,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(
            code=code,
            message=SAFE_MESSAGES.get(code, "the upstream rejected the request"),
            retryable=False,
            recovery_action="NONE",
            http_status=http_status,
            details=details,
        )


class ConnectorUnavailable(ConnectorError):
    def __init__(
        self,
        *,
        create_operation: bool = False,
        retry_after_seconds: int | None = None,
        code: str = "UPSTREAM_UNAVAILABLE",
    ) -> None:
        details = (
            {"retry_after_seconds": retry_after_seconds}
            if retry_after_seconds is not None
            else None
        )
        super().__init__(
            code=code,
            message=SAFE_MESSAGES.get(code, SAFE_MESSAGES["UPSTREAM_UNAVAILABLE"]),
            retryable=not create_operation,
            recovery_action="LOOKUP_FIRST" if create_operation else "RETRY_REQUEST",
            http_status=503,
            details=details,
            outcome_unknown=create_operation,
        )


class ConnectorRateLimited(ConnectorUnavailable):
    def __init__(self, retry_after_seconds: int | None = None, *, create_operation: bool) -> None:
        super().__init__(
            create_operation=create_operation,
            retry_after_seconds=retry_after_seconds,
        )
        self.code = "RATE_LIMITED"
        self.message = SAFE_MESSAGES["RATE_LIMITED"]
        self.http_status = 429
        if not create_operation:
            self.retryable = True
            self.recovery_action = "RETRY_REQUEST"


class ConnectorProtocolError(ConnectorError):
    """A malformed, version-skewed or unexpected upstream response."""

    def __init__(self) -> None:
        super().__init__(
            code="UPSTREAM_UNAVAILABLE",
            message="the upstream returned a malformed or unsupported response",
            retryable=False,
            recovery_action="NONE",
            http_status=502,
            contract_violation=True,
        )


class ConnectorConfigurationError(ValueError):
    """Fail-closed connector configuration error (never falls back to Fake)."""


def map_wire_error(
    status: int,
    response: ErrorResponse,
    *,
    create_operation: bool,
) -> ConnectorError:
    """Convert a parsed source-contract error into a safe classified error."""
    code = response.error.code
    if status not in ERROR_STATUSES.get(code, set()):
        raise ConnectorProtocolError()

    details = (
        response.error.details.model_dump(mode="json", exclude_none=True)
        if response.error.details is not None
        else None
    )
    if code == "NOT_FOUND":
        return ConnectorNotFound(details=details)
    if code == "ATTACHMENT_EXPIRED":
        return ConnectorGone(details=details)
    if code in {"UNAUTHORIZED", "FORBIDDEN_MARKET"}:
        return ConnectorPermissionError(code, http_status=status)
    if code in CONFLICT_CODES:
        return ConnectorConflict(code, details=details)
    if code in VALIDATION_CODES:
        return ConnectorValidationError(code, details=details, http_status=status)
    if code == "RATE_LIMITED":
        retry_after = response.error.details.retry_after_seconds if response.error.details else None
        return ConnectorRateLimited(retry_after, create_operation=create_operation)

    retry_after = response.error.details.retry_after_seconds if response.error.details else None
    return ConnectorUnavailable(
        create_operation=create_operation,
        retry_after_seconds=retry_after,
        code=code,
    )
