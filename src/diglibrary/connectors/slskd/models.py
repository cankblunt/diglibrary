"""Compatibility aliases for connector-neutral models used by ``SlskdConnector``.

slskd payload objects are deliberately confined to ``client`` and ``mapper``.
The aliases keep the slskd-named import surface while making every value returned
by the connector a model from the generic connector contract.
"""

from diglibrary.connectors.models import (
    ConnectorHealth as SlskdHealth,
)
from diglibrary.connectors.models import (
    ConnectorHealthState as SlskdHealthState,
)
from diglibrary.connectors.models import (
    SearchAvailability as SlskdAvailability,
)
from diglibrary.connectors.models import (
    SearchFile as SlskdFile,
)
from diglibrary.connectors.models import (
    SearchKind as SlskdSearchKind,
)
from diglibrary.connectors.models import (
    SearchQueueState as SlskdQueueState,
)
from diglibrary.connectors.models import (
    SearchRequest as SlskdSearchRequest,
)
from diglibrary.connectors.models import (
    SearchResponse as SlskdSearchResponse,
)
from diglibrary.connectors.models import (
    SearchSource as SlskdPeer,
)

__all__ = [
    "SlskdAvailability",
    "SlskdFile",
    "SlskdHealth",
    "SlskdHealthState",
    "SlskdPeer",
    "SlskdQueueState",
    "SlskdSearchKind",
    "SlskdSearchRequest",
    "SlskdSearchResponse",
]
