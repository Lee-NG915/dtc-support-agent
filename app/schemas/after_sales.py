"""Public after-sales identity types shared by auth, routes and connectors."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

StaffRole = Literal["support", "approver", "auditor", "operator"]
AfterSalesMarket = Literal["au", "sg", "us"]

STAFF_ROLES: frozenset[StaffRole] = frozenset({"support", "approver", "auditor", "operator"})
READ_ROLES: frozenset[StaffRole] = frozenset({"support", "approver", "auditor"})
AFTER_SALES_MARKETS: frozenset[AfterSalesMarket] = frozenset({"au", "sg", "us"})

OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$"
_OPAQUE_ID_RE = re.compile(OPAQUE_ID_PATTERN)


class StaffActor(BaseModel):
    """Server-authenticated staff identity used for authorization decisions."""

    model_config = ConfigDict(frozen=True)

    staff_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    roles: frozenset[StaffRole] = frozenset()
    allowed_markets: frozenset[AfterSalesMarket] = frozenset()

    def has_role(self, *roles: StaffRole) -> bool:
        return bool(self.roles.intersection(roles))

    def may_read_market(self, market: str) -> bool:
        return market in self.allowed_markets


class StaffIdentityResponse(BaseModel):
    """Public identity only: no token, digest, expiry internals or credential data."""

    staff_id: str
    display_name: str
    roles: list[StaffRole]
    allowed_markets: list[AfterSalesMarket]


class LoginOptionsResponse(BaseModel):
    """Public login-mode discovery; demo accounts never carry credentials."""

    mode: Literal["demo_password", "staff_token"]
    accounts: list[StaffIdentityResponse] = Field(default_factory=list)


class DemoLoginRequest(BaseModel):
    staff_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class DemoLoginResponse(BaseModel):
    """A short-lived signed session, never the shared password itself."""

    token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    identity: StaffIdentityResponse


def parse_staff_roles(raw: str) -> frozenset[StaffRole]:
    return frozenset(
        value for value in (part.strip() for part in raw.split(",")) if value in STAFF_ROLES
    )


def parse_staff_markets(raw: str) -> frozenset[AfterSalesMarket]:
    return frozenset(
        value for value in (part.strip() for part in raw.split(",")) if value in AFTER_SALES_MARKETS
    )


def is_valid_market(market: str) -> bool:
    return market in AFTER_SALES_MARKETS


def is_valid_opaque_id(value: str) -> bool:
    return bool(_OPAQUE_ID_RE.fullmatch(value))
