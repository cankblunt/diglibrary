"""Injectable standard-library HTTP transport for metadata clients."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, OpenerDirector, Request, build_opener

from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import MetadataRequestError, RetryPolicy

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
"""The most one answer may weigh, which is a ceiling and not an expectation.

The largest thing this application ever asks for over HTTP is a cover, and the
Cover Art Archive's own originals top out well below this. Without a ceiling
the size of the answer is chosen by whoever answers: a body that never ends is
read into memory until the machine gives up, and for a picture it is then
written to disk once per track of the album."""


class CredentialError(RuntimeError):
    """Raised when required metadata credentials are unavailable."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """An HTTP response represented independently of a concrete HTTP library."""

    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def seconds_to_wait(self) -> float | None:
        """Return the delay this response asked for, when it asked for one.

        Only the delta-seconds form is read. The HTTP-date form is legal and
        rare, and parsing a date against a clock this project does not control
        would be guessing at a number the same server will send as an integer
        the next time.
        """
        for name, value in self.headers.items():
            if name.lower() != "retry-after":
                continue
            try:
                return max(0.0, float(value.strip()))
            except ValueError:
                return None
        return None


class HttpTransport(Protocol):
    """Perform one HTTP GET operation without metadata-provider knowledge."""

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Return an HTTP response for the supplied request values."""


def _opener() -> OpenerDirector:
    """Build an opener that can speak HTTPS and nothing else.

    ``urlopen`` uses a default opener carrying handlers for ``file:``, ``ftp:``
    and ``data:``. Every address this application fetches comes out of somebody
    else's JSON — a Cover Art Archive manifest names the image's URL — so an
    answer naming a ``file:`` address is an answer that reads the user's own
    disk and hands the bytes back to be written into an album. The default
    opener does return the file's contents for such an address.

    Plain ``http:`` is left out too. Every service this project talks to is
    HTTPS, so the only thing a cleartext address could be is a downgrade.
    """
    return build_opener(HTTPSHandler())


class UrllibTransport:
    """HTTP transport implemented only with Python's standard library.

    Constraints: HTTPS only, and an answer is read up to a ceiling — both are
    about who chooses. An address that arrives inside a response must not be
    able to choose the *scheme*, and a server must not be able to choose how
    much memory this application spends.
    """

    def __init__(self, max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        """Create a transport with a ceiling on how large one answer may be."""
        self._max_response_bytes = max_response_bytes
        self._opener = _opener()

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Perform a GET request and preserve HTTP status failures for retry policy handling."""
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=response.status,
                    body=self._read(response),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            with error:
                return HttpResponse(
                    status=error.code, body=self._read(error), headers=dict(error.headers.items())
                )
        except ValueError as error:
            # `build_opener` raises this for a scheme it has no handler for,
            # which is the refusal above arriving as an ordinary failed lookup.
            raise MetadataRequestError(f"Unsupported address: {error}") from error
        except URLError as error:
            raise MetadataRequestError(f"Network error: {error.reason}", retryable=True) from error

    def _read(self, response: object) -> bytes:
        """Return the body, refusing one that runs past the ceiling.

        One byte past is read on purpose: `Content-Length` is the sender's
        claim about itself, and the only way to know the body is too large is
        to find something where the end should be.
        """
        body = response.read(self._max_response_bytes + 1)  # type: ignore[attr-defined]
        if len(body) > self._max_response_bytes:
            raise MetadataRequestError(
                f"The answer was larger than the {self._max_response_bytes} bytes allowed."
            )
        return body


class JsonHttpClient:
    """Fetch JSON through cache, rate-limit, retry, and logging infrastructure."""

    def __init__(
        self,
        source_name: str,
        transport: HttpTransport,
        cache: JsonMetadataCache,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        timeout_seconds: float,
        logger: logging.Logger,
    ) -> None:
        """Create a source-scoped JSON client from injected infrastructure services."""
        self._source_name = source_name
        self._transport = transport
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._timeout_seconds = timeout_seconds
        self._logger = logger

    def get(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        """Return a cached or fetched JSON object for a source API request."""
        cache_key = f"{self._source_name}:{url}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._logger.info("Metadata cache hit.", extra={"operation": "metadata.cache.hit"})
            return cached

        def request() -> Mapping[str, Any]:
            self._rate_limiter.acquire()
            response = self._transport.get(url, headers, self._timeout_seconds)
            if response.status < 200 or response.status >= 300:
                retryable = response.status == 429 or response.status >= 500
                raise MetadataRequestError(
                    f"{self._source_name} returned HTTP {response.status}.",
                    retryable=retryable,
                    status=response.status,
                    retry_after_seconds=response.seconds_to_wait(),
                )
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MetadataRequestError("Metadata API returned invalid JSON.") from error
            if not isinstance(payload, dict):
                raise MetadataRequestError("Metadata API returned a non-object JSON payload.")
            return payload

        self._logger.info("Metadata API request.", extra={"operation": "metadata.request"})
        payload = self._retry_policy.execute(request)
        self._cache.set(cache_key, payload)
        return payload
