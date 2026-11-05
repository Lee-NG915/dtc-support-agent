"""Business connector selection. Epig configuration failures never fall back."""

from __future__ import annotations

from app.config.settings import Settings
from app.connectors.base import BusinessConnector
from app.connectors.epig import EpigConnector
from app.connectors.errors import ConnectorConfigurationError
from app.connectors.fake import FakeConnector


def build_business_connector(settings: Settings) -> BusinessConnector:
    if settings.business_connector == "fake":
        return FakeConnector()
    if settings.business_connector == "epig":
        return EpigConnector(settings)
    raise ConnectorConfigurationError(
        f"unsupported BUSINESS_CONNECTOR: {settings.business_connector}"
    )
