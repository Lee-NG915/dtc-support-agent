"""Runtime behavior for scoped policy brands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PolicyProfile:
    allowed_markets: tuple[str, ...] = ("global",)
    requires_category: bool = False
    default_as_of_today: bool = False
    filter_dates: bool = False
    force_retrieval: bool = False
    policy_only: bool = False
    stateless_history: bool = False
    block_generic_policy_tools: bool = False
    temperature: float = 0.2
    demo_disclosure: str = ""


_UPSTREAM = PolicyProfile()
_KOALA = PolicyProfile(
    allowed_markets=("au", "us", "sg"),
    requires_category=True,
    default_as_of_today=True,
    filter_dates=True,
    force_retrieval=True,
    policy_only=True,
    stateless_history=True,
    block_generic_policy_tools=True,
    temperature=0.0,
)
_EPIG = PolicyProfile(
    allowed_markets=("au", "us", "sg"),
    requires_category=True,
    default_as_of_today=True,
    filter_dates=True,
    force_retrieval=True,
    policy_only=True,
    stateless_history=True,
    block_generic_policy_tools=True,
    temperature=0.0,
    demo_disclosure=(
        "说明：以上内容仅基于 E-pig 内部自编演示政策，非官方或生产批准条款，" "不用于真实订单裁决。"
    ),
)

_PROFILES = {
    "upstream_demo": _UPSTREAM,
    "koala": _KOALA,
    "epig": _EPIG,
}
MANAGED_POLICY_PREFIXES = ("koala/", "epig/")


def get_policy_profile(brand: str) -> PolicyProfile:
    return _PROFILES.get(brand, _UPSTREAM)
