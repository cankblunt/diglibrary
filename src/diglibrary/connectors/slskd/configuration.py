"""Immutable configuration values for the slskd connector."""

from dataclasses import dataclass
from enum import StrEnum


class SlskdAuthenticationMode(StrEnum):
    """Supported authentication mechanisms for slskd HTTP requests.

    Purpose:
        Identifies the configured credential mechanism without retaining a secret.
    Responsibilities:
        Constrains configuration and client header creation to documented slskd modes.
    Architectural boundaries:
        Does not read environment variables or communicate with slskd.
    Dependencies:
        Depends only on the Python standard library.
    Expected collaborators:
        ``SlskdConfiguration``, the configuration loader, and ``SlskdClient``.
    Constraints:
        API keys must be resolved at request time from an approved secret source.
    """

    NONE = "none"
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class SlskdConfiguration:
    """Validated non-secret settings used to communicate with one slskd instance.

    Purpose:
        Carries the complete connector configuration in an immutable value object.
    Responsibilities:
        Defines endpoint location, authentication mode, timeout, retry, and search limits.
    Architectural boundaries:
        Contains no credentials, HTTP behaviour, provider settings, or business policy.
    Dependencies:
        Depends only on ``SlskdAuthenticationMode``.
    Expected collaborators:
        The TOML loader, composition root, ``SlskdClient``, and ``SlskdConnector``.
    Constraints:
        Secrets are referenced only by environment-variable name and never stored here.
    """

    enabled: bool
    base_url: str
    timeout_seconds: float
    retry_limit: int
    authentication_mode: SlskdAuthenticationMode
    api_key_environment_variable: str
    api_base_path: str = "/api/v0"
    response_limit: int = 100
    search_settle_seconds: float = 90.0
    """The longest a search may run before it is stopped and read regardless.

    This is a backstop, not the ordinary wait: a search is normally ended as
    soon as the answers stop arriving (see ``search_quiet_seconds``). It exists
    for the search that never goes quiet, so a tab cannot sit open forever.
    """
    search_quiet_seconds: float = 3.0
    """How long the answers must stop arriving before the search is ended.

    slskd hands over a search's responses only once that search is over, and it
    is slow to call one over: a search can hold every respondent it will ever
    have within a few seconds and stay ``InProgress`` for many more. Ending it
    is what makes it readable, so ending it as soon as it goes quiet returns
    the result in seconds rather than after the service's own timeout.

    The cost of a shorter quiet period is the late respondent: a popular query
    goes on gathering a few more respondents long after most have answered.
    """
    search_poll_seconds: float = 1.0
    """How often to ask what the search is holding, and whether it has stopped."""
