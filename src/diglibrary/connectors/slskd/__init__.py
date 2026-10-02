"""Typed, provider-independent connector for a running slskd service."""

from diglibrary.connectors.slskd.configuration import (
    SlskdAuthenticationMode,
    SlskdConfiguration,
)
from diglibrary.connectors.slskd.connector import SlskdConnector
from diglibrary.connectors.slskd.models import (
    SlskdHealth,
    SlskdSearchRequest,
    SlskdSearchResponse,
)

__all__ = [
    "SlskdAuthenticationMode",
    "SlskdConfiguration",
    "SlskdConnector",
    "SlskdHealth",
    "SlskdSearchRequest",
    "SlskdSearchResponse",
]
