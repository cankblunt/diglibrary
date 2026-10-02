"""External-service connector boundary for reusable integration infrastructure."""

from diglibrary.connectors.contracts import SearchConnector, TransferConnector
from diglibrary.connectors.models import (
    ConnectorHealth,
    ConnectorHealthState,
    SearchAvailability,
    SearchFile,
    SearchKind,
    SearchQueueState,
    SearchRequest,
    SearchResponse,
    SearchSource,
    Transfer,
    TransferFile,
    TransferRequest,
    TransferState,
)

__all__ = [
    "ConnectorHealth",
    "ConnectorHealthState",
    "SearchAvailability",
    "SearchConnector",
    "SearchFile",
    "SearchKind",
    "SearchQueueState",
    "SearchRequest",
    "SearchResponse",
    "SearchSource",
    "Transfer",
    "TransferConnector",
    "TransferFile",
    "TransferRequest",
    "TransferState",
]
