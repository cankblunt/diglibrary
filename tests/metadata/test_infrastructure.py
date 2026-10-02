"""Unit tests for metadata cache, authentication, pacing, and retry infrastructure."""

from collections.abc import Mapping
from email.utils import formatdate

import pytest

from diglibrary.metadata.authentication import (
    EnvironmentCredentials,
    application_version,
    discogs_headers,
    musicbrainz_headers,
)
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import (
    MAXIMUM_RETRY_AFTER_SECONDS,
    MetadataRequestError,
    RetryPolicy,
)
from diglibrary.metadata.transport import CredentialError, HttpResponse


def test_cache_returns_entry_until_its_ttl_expires(tmp_path) -> None:
    """The filesystem cache does not return stale provider payloads."""
    now = [100.0]
    cache = JsonMetadataCache(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    cache.set("release", {"id": "1"})

    assert cache.get("release") == {"id": "1"}

    now[0] = 111.0
    assert cache.get("release") is None


def test_an_expired_entry_is_removed_as_it_is_found(tmp_path) -> None:
    """Nothing else ever deletes one, and a large library leaves thousands."""
    now = [100.0]
    cache = JsonMetadataCache(tmp_path, ttl_seconds=10, clock=lambda: now[0])
    cache.set("release", {"id": "1"})
    now[0] = 111.0

    assert cache.get("release") is None
    assert list(tmp_path.iterdir()) == []


def test_a_write_leaves_no_temporary_file_behind(tmp_path) -> None:
    """The temporary is named for the write, not the key.

    A scan and a search can ask the same source for the same release at the same
    moment; two writers sharing one temporary path interleave their bytes into
    it before either replaces the entry.
    """
    cache = JsonMetadataCache(tmp_path, ttl_seconds=10, clock=lambda: 0.0)

    cache.set("release", {"id": "1"})
    cache.set("release", {"id": "2"})

    assert cache.get("release") == {"id": "2"}
    assert [path.suffix for path in tmp_path.iterdir()] == [".json"]


def test_rate_limiter_spaces_requests() -> None:
    """The limiter reserves the next request slot before allowing a second call."""
    now = [0.0]
    delays: list[float] = []

    def sleep(delay: float) -> None:
        delays.append(delay)
        now[0] += delay

    limiter = RateLimiter(2.0, monotonic=lambda: now[0], sleep=sleep)
    limiter.acquire()
    limiter.acquire()

    assert delays == [0.5]


def test_retry_policy_retries_only_transient_request_errors() -> None:
    """Retry count and linear delay are deterministic under injected time."""
    attempts = [0]
    delays: list[float] = []

    def operation() -> str:
        attempts[0] += 1
        if attempts[0] < 3:
            raise MetadataRequestError("unavailable", retryable=True)
        return "complete"

    result = RetryPolicy(3, 0.25, sleep=delays.append).execute(operation)

    assert result == "complete"
    assert delays == [0.25, 0.5]


def test_a_server_that_says_how_long_to_wait_is_obeyed() -> None:
    """Half a second is not what a 429 means, and this scan makes 429 routine."""
    attempts = [0]
    delays: list[float] = []

    def operation() -> str:
        attempts[0] += 1
        if attempts[0] == 1:
            raise MetadataRequestError(
                "rate limited", retryable=True, status=429, retry_after_seconds=12.0
            )
        return "complete"

    assert RetryPolicy(3, 0.25, sleep=delays.append).execute(operation) == "complete"
    assert delays == [12.0]


def test_a_wait_a_server_asks_for_is_capped() -> None:
    """Honouring the header is the protocol; freezing a library-sized scan is not."""
    delays: list[float] = []

    def operation() -> str:
        raise MetadataRequestError(
            "rate limited", retryable=True, status=429, retry_after_seconds=8_000.0
        )

    with pytest.raises(MetadataRequestError):
        RetryPolicy(2, 0.25, sleep=delays.append).execute(operation)

    assert delays == [MAXIMUM_RETRY_AFTER_SECONDS]


def test_a_response_that_asks_for_a_date_rather_than_seconds_is_ignored() -> None:
    """The HTTP-date form is legal, rare, and not worth guessing a clock over."""
    dated = HttpResponse(
        status=429, body=b"", headers={"Retry-After": formatdate(1_000_000_000, usegmt=True)}
    )
    numeric = HttpResponse(status=429, body=b"", headers={"retry-after": " 30 "})

    assert dated.seconds_to_wait() is None
    assert numeric.seconds_to_wait() == 30.0


def test_retry_policy_does_not_retry_permanent_errors() -> None:
    """Permanent request failures leave the policy on the first attempt."""
    with pytest.raises(MetadataRequestError, match="invalid"):
        RetryPolicy(3, 1.0, sleep=lambda _: None).execute(
            lambda: (_ for _ in ()).throw(MetadataRequestError("invalid"))
        )


def test_credentials_and_headers_never_require_secrets_in_configuration() -> None:
    """Authentication values resolve lazily and are represented only in headers."""
    values: Mapping[str, str] = {"TOKEN": "secret", "CONTACT": "ops@example.com"}
    credentials = EnvironmentCredentials(values.get)

    assert credentials.required("TOKEN") == "secret"
    assert credentials.optional("MISSING") is None
    assert discogs_headers("secret")["Authorization"] == "Discogs token=secret"
    assert musicbrainz_headers("ops@example.com", "bearer")["Authorization"] == "Bearer bearer"

    with pytest.raises(CredentialError, match="MISSING"):
        credentials.required("MISSING")


def test_the_user_agent_carries_the_installed_version_and_nothing_hardcodes_one() -> None:
    """The version lives in pyproject.toml alone; both header builders read it installed."""
    installed = application_version()

    assert installed != "0", "the package under test is expected to be installed"
    assert discogs_headers("secret")["User-Agent"] == f"DigLibrary/{installed}"
    assert (
        musicbrainz_headers("ops@example.com", None)["User-Agent"]
        == f"DigLibrary/{installed} (ops@example.com)"
    )
