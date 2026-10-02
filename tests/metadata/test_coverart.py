"""Unit tests for Cover Art Archive access, with the network faked."""

import json
import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.coverart import CoverArtArchiveClient
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import MetadataRequestError, RetryPolicy
from diglibrary.metadata.transport import HttpResponse, JsonHttpClient

# The archive capitalises its type names; the parser folds case.
BACK_TYPE = "back".capitalize()

MANIFEST = {
    "images": [
        {
            "image": "https://coverartarchive.org/release/mb1/1.jpg",
            "front": True,
            "back": False,
            "types": ["Front"],
            "thumbnails": {
                "250": "https://coverartarchive.org/release/mb1/1-250.jpg",
                "500": "https://coverartarchive.org/release/mb1/1-500.jpg",
                "1200": "https://coverartarchive.org/release/mb1/1-1200.jpg",
            },
        },
        {
            "image": "https://coverartarchive.org/release/mb1/2.jpg",
            "front": False,
            "types": [BACK_TYPE],
            "thumbnails": {},
        },
    ]
}


class FakeTransport:
    """Answer each request with a queued status and body."""

    def __init__(self, responses: list[HttpResponse]) -> None:
        self._responses = responses
        self.urls: list[str] = []
        self.headers: list[Mapping[str, str]] = []

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        """Record the request and return the next queued response."""
        self.urls.append(url)
        self.headers.append(headers)
        return self._responses.pop(0)


def test_a_manifest_becomes_entries_that_say_what_each_image_is(tmp_path: Path) -> None:
    """The archive's own thumbnail keys are kept verbatim; the choice is made elsewhere."""
    transport = FakeTransport([HttpResponse(200, json.dumps(MANIFEST).encode("utf-8"))])

    entries = _client(tmp_path, transport).release_images("mb1")

    assert len(entries) == 2
    assert entries[0].is_front and not entries[0].is_back
    assert entries[0].thumbnails["1200"].endswith("1-1200.jpg")
    assert entries[1].is_back
    assert transport.urls == ["https://coverartarchive.org/release/mb1"]


def test_a_release_without_art_answers_nothing_rather_than_failing(tmp_path: Path) -> None:
    """404 is the archive saying "no picture", which must never block organization."""
    transport = FakeTransport([HttpResponse(404, b"Not Found")])

    assert _client(tmp_path, transport).release_images("mb1") == ()


def test_an_archive_failure_is_not_mistaken_for_an_absent_cover(tmp_path: Path) -> None:
    """A service that is down must not look like an album that has no art."""
    transport = FakeTransport([HttpResponse(503, b"") for _ in range(3)])

    with pytest.raises(MetadataRequestError):
        _client(tmp_path, transport).release_images("mb1")


def test_the_release_group_is_asked_at_its_own_address(tmp_path: Path) -> None:
    """The release-group fallback is a different endpoint, not a different parser."""
    transport = FakeTransport([HttpResponse(200, json.dumps(MANIFEST).encode("utf-8"))])

    entries = _client(tmp_path, transport).release_group_images("rg1")

    assert entries[0].is_front
    assert transport.urls == ["https://coverartarchive.org/release-group/rg1"]


def test_an_image_is_downloaded_as_bytes_and_identifies_this_project(tmp_path: Path) -> None:
    """MusicBrainz asks for a contactable User-Agent, and images are never cached as JSON."""
    transport = FakeTransport([HttpResponse(200, b"\xff\xd8binary")])

    body = _client(tmp_path, transport).download("https://coverartarchive.org/release/mb1/1.jpg")

    assert body == b"\xff\xd8binary"
    assert "someone@example.com" in transport.headers[0]["User-Agent"]
    assert transport.headers[0]["Accept"] == "image/*"


@pytest.mark.parametrize(
    "address",
    [
        "file:///home/someone/Pictures/passport.jpg",
        "http://192.168.1.1/status",
        "https://192.168.1.1/cover.jpg",
        "ftp://coverartarchive.org/1.jpg",
        "https://evilcoverartarchive.org/1.jpg",
        "https://coverartarchive.org.attacker.example/1.jpg",
        "data:image/jpeg;base64,/9j/",
    ],
)
def test_an_image_address_that_is_not_the_archive_s_is_never_fetched(
    tmp_path: Path, address: str
) -> None:
    """The address arrives inside somebody else's JSON, so it is data.

    A manifest naming `file:///…` reads the user's own disk and the bytes are
    then written into the album folder and embedded into every track; one
    naming an address on the local network makes this application a probe of it.
    Neither is a picture, and the refusal happens before the request.
    """
    transport = FakeTransport([HttpResponse(200, b"\xff\xd8secrets")])

    with pytest.raises(MetadataRequestError):
        _client(tmp_path, transport).download(address)
    assert transport.urls == []


def test_the_archive_publishes_cleartext_and_it_is_fetched_over_https(tmp_path: Path) -> None:
    """An address the archive publishes as `http://` is upgraded, not refused.

    The Cover Art Archive answers with `http://` addresses for a large share of
    its images and thumbnails, so a check that simply requires HTTPS refuses
    most of the covers the archive has. A test file whose addresses are all
    written `https` cannot see that, which is why this one is not.

    Upgraded rather than accepted: the host is the security property, and a
    cover fetched in cleartext is one anybody on the wire can swap for another —
    which this application then embeds into every track of the album.
    """
    transport = FakeTransport([HttpResponse(200, b"\xff\xd8binary")])
    published = "http://coverartarchive.org/release/0a1b2c3d/12345678901.jpg"

    body = _client(tmp_path, transport).download(published)

    assert body == b"\xff\xd8binary"
    assert transport.urls == [
        "https://coverartarchive.org/release/0a1b2c3d/12345678901.jpg"
    ], "the address the archive published is fetched, over https"


def test_the_archive_s_own_file_hosts_are_still_fetched(tmp_path: Path) -> None:
    """The manifests are one host and the files are another — refusing the
    second would refuse every cover the archive serves."""
    transport = FakeTransport([HttpResponse(200, b"\xff\xd8binary")])

    body = _client(tmp_path, transport).download("https://ia800207.us.archive.org/1.jpg")

    assert body == b"\xff\xd8binary"


@pytest.mark.parametrize(
    "image",
    [
        {"image": "https://coverartarchive.org/1.jpg", "thumbnails": ["250"]},
        {"image": "https://coverartarchive.org/1.jpg", "thumbnails": "250"},
        {"image": "https://coverartarchive.org/1.jpg", "types": 7},
        {"image": "https://coverartarchive.org/1.jpg", "types": None},
    ],
)
def test_a_manifest_of_the_wrong_shape_is_read_without_raising(
    tmp_path: Path, image: dict[str, object]
) -> None:
    """The shape of an answer is the answerer's choice, so it is proved and
    not assumed: `types` as a number or `thumbnails` as a list must not be an
    exception out of the parser, which would reach the screen as an album that
    silently never gets artwork again."""
    transport = FakeTransport([HttpResponse(200, json.dumps({"images": [image]}).encode("utf-8"))])

    entries = _client(tmp_path, transport).release_images("mb1")

    assert len(entries) == 1
    assert entries[0].thumbnails == {}


def _client(tmp_path: Path, transport: FakeTransport) -> CoverArtArchiveClient:
    json_client = JsonHttpClient(
        "coverartarchive",
        transport,
        JsonMetadataCache(tmp_path / "cache", ttl_seconds=60),
        RateLimiter(1_000_000, sleep=lambda _: None),
        RetryPolicy(3, 0.0, sleep=lambda _: None),
        1.0,
        logging.getLogger("test.coverart"),
    )
    return CoverArtArchiveClient(
        json_client,
        transport,
        RateLimiter(1_000_000, sleep=lambda _: None),
        RetryPolicy(3, 0.0, sleep=lambda _: None),
        EnvironmentCredentials({"CONTACT": "someone@example.com"}.get),
        "CONTACT",
        1.0,
        logging.getLogger("test.coverart"),
    )
