"""Cover Art Archive access: which images a release has, and their bytes."""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

from diglibrary.metadata.authentication import EnvironmentCredentials, musicbrainz_headers
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import MetadataRequestError, RetryPolicy
from diglibrary.metadata.transport import HttpTransport, JsonHttpClient

BASE_URL = "https://coverartarchive.org"

ARCHIVE_NOTICE = (
    "Cover art comes from the Cover Art Archive, where all images are "
    "copyrighted by their respective rights holders and are provided for archival "
    "purposes, to be used at your own risk."
)
"""The notice this project surfaces wherever cover art is enabled."""

_NOT_FOUND = 404

IMAGE_HOSTS = ("coverartarchive.org", "archive.org")
"""Whose images this client will fetch, as suffixes so subdomains are included.

The archive publishes its manifests under `coverartarchive.org` and serves the
files themselves from the Internet Archive's own hosts (`ia800207.us.archive.org`
and its siblings), which is why the second entry is here and why the match is on
the registrable name rather than on one literal host."""


def archive_address(url: str) -> str | None:
    """Return this address as an HTTPS one belonging to the archive, or ``None``.

    The archive publishes many of its image and thumbnail addresses as
    `http://`, so refusing anything that is not already HTTPS would refuse a
    large share of the covers it has.

    Upgraded rather than accepted, because the two questions are separate. The
    *host* is the security property: it is what stops an address inside somebody
    else's JSON from pointing at the local disk or the local network, and it is
    checked strictly. The *scheme* is transport, the archive answers on HTTPS
    for the same paths, and a cover that arrives over cleartext is one anybody
    on the wire can swap for another — which this application then embeds into
    every track of the album.

    Anything that is not already one of those two schemes is refused outright:
    `file:` and `data:` do not get upgraded into something allowed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").casefold()
    # Matched on the dotted boundary rather than by `endswith` alone: a host
    # named `coverartarchive.org.attacker.example` ends with nothing this
    # allows, but `evilcoverartarchive.org` ends with the string.
    if not any(host == known or host.endswith(f".{known}") for known in IMAGE_HOSTS):
        return None
    return urlunparse(parsed._replace(scheme="https"))


@dataclass(frozen=True, slots=True)
class CoverArtEntry:
    """Purpose: describe one image the archive holds for a release.

    Responsibilities: carry the original image's address, the sizes the archive
    has already rendered, and what the image depicts. Boundaries: it holds no
    image data and expresses no preference — which size to take is a write
    policy, decided elsewhere. Dependencies: none. Collaborators:
    ``CoverArtArchiveClient`` and the artwork service. Constraints: the
    thumbnail keys are the archive's own ("250", "500", "1200"), kept verbatim
    rather than translated into an enumeration this project would have to keep
    in step with the service.
    """

    url: str
    is_front: bool = False
    is_back: bool = False
    thumbnails: Mapping[str, str] = field(default_factory=dict)


class CoverArtArchiveClient:
    """Purpose: read what the Cover Art Archive publishes for a MusicBrainz release.

    Responsibilities: list the images of a release or of its release group, and
    fetch the bytes of one image. Boundaries: it chooses no image and no size,
    writes nothing, and knows nothing about albums on disk. Dependencies: the
    injected JSON client for manifests and the injected transport for image
    bytes — a JSON cache cannot hold a picture, and caching megabytes that are
    about to be written into the library anyway would store them twice.
    Collaborators: the artwork service. Constraints: a release with no art
    answers 404, which is an ordinary answer here and not a failure; every other
    status is.
    """

    def __init__(
        self,
        http_client: JsonHttpClient,
        transport: HttpTransport,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy,
        credentials: EnvironmentCredentials,
        contact_environment_variable: str,
        timeout_seconds: float,
        logger: logging.Logger,
    ) -> None:
        """Create an archive client without resolving environment values until request time."""
        self._http_client = http_client
        self._transport = transport
        self._rate_limiter = rate_limiter
        self._retry_policy = retry_policy
        self._credentials = credentials
        self._contact_environment_variable = contact_environment_variable
        self._timeout_seconds = timeout_seconds
        self._logger = logger

    def release_images(self, release_id: str) -> tuple[CoverArtEntry, ...]:
        """Return the images the archive holds for one release, which may be none."""
        return self._images(f"{BASE_URL}/release/{release_id}")

    def release_group_images(self, group_id: str) -> tuple[CoverArtEntry, ...]:
        """Return the images of the release the group considers representative."""
        return self._images(f"{BASE_URL}/release-group/{group_id}")

    def download(self, url: str) -> bytes:
        """Return the bytes of one image, refusing an address that is not the archive's.

        The address comes out of the archive's own manifest, which is to say
        out of somebody else's JSON — so it is data, and it does not get to
        choose which host this application fetches from. Without this, an
        answer naming ``http://192.168.1.1/…`` makes the app a probe of the
        user's own network, and whatever it fetches is written into the album
        folder and embedded into every track.
        """
        address = archive_address(url)
        if address is None:
            raise MetadataRequestError("The cover art address is not the archive's.")

        def request() -> bytes:
            self._rate_limiter.acquire()
            response = self._transport.get(address, self._headers("image/*"), self._timeout_seconds)
            if response.status < 200 or response.status >= 300:
                retryable = response.status == 429 or response.status >= 500
                raise MetadataRequestError(
                    f"The cover art archive returned HTTP {response.status}.",
                    retryable=retryable,
                    status=response.status,
                )
            return response.body

        self._logger.info("Cover art download.", extra={"operation": "artwork.download"})
        return self._retry_policy.execute(request)

    def _images(self, url: str) -> tuple[CoverArtEntry, ...]:
        try:
            payload = self._http_client.get(url, self._headers("application/json"))
        except MetadataRequestError as error:
            if error.status == _NOT_FOUND:
                return ()
            raise
        return _entries(payload)

    def _headers(self, accept: str) -> Mapping[str, str]:
        contact = self._credentials.required(self._contact_environment_variable)
        return {**musicbrainz_headers(contact, None), "Accept": accept}


def _entries(payload: Mapping[str, Any]) -> tuple[CoverArtEntry, ...]:
    images = payload.get("images")
    if not isinstance(images, list):
        return ()
    entries: list[CoverArtEntry] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("image")
        if not isinstance(url, str) or not url:
            continue
        # Every shape is proved before it is walked. `types` answered as a
        # number and `thumbnails` answered as a list are both an exception out
        # of a parser, and the shape of an answer is the answerer's choice.
        raw_types = image.get("types")
        types = (
            {value.casefold() for value in raw_types if isinstance(value, str)}
            if isinstance(raw_types, list)
            else set()
        )
        raw_thumbnails = image.get("thumbnails")
        entries.append(
            CoverArtEntry(
                url=url,
                is_front=image.get("front") is True or "front" in types,
                is_back=image.get("back") is True or "back" in types,
                thumbnails={
                    key: value
                    for key, value in (
                        raw_thumbnails.items() if isinstance(raw_thumbnails, dict) else ()
                    )
                    if isinstance(key, str) and isinstance(value, str)
                },
            )
        )
    return tuple(entries)
