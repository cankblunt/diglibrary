"""Reading a Spotify link as words, and never as anything else.

A pasted Spotify link is resolved to **words**: an artist and an album, handed
straight to a Soulseek search or to a Discogs/MusicBrainz search, and then
dropped.

**Nothing here is ever stored.** Not in `metadata_releases`, not in the metadata
cache on disk, not in a tag, not in a file name that came from Spotify's own
characters. That is why this module talks to `HttpTransport` directly instead of
going through `JsonHttpClient` like every other source: that client writes every
answer into `cache/metadata`, and Spotify's Developer Terms limit local caching
to "the temporary caching of metadata and cover art" and say "Do not store
Spotify Content indefinitely". A pointer that cached would break those terms,
silently, on its first use.

So this is a pointer and not a source: it never votes on an identification, it
never becomes a candidate, and no value it returns is written anywhere. What it
produces is a query the user could have typed themselves.
"""

import json
import logging
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.transport import HttpTransport

SPOTIFY_HOST = "open.spotify.com"
ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
API_ROOT = "https://api.spotify.com/v1"
OEMBED_URL = "https://open.spotify.com/oembed"

_WEB_LINK = re.compile(
    # An `/intl-xx/` segment sits between the host and the kind on links
    # copied from a localized client.
    r"^/(?:intl-[a-z]{2}/)?(album|track|playlist)/([A-Za-z0-9]+)",
)
_URI_LINK = re.compile(r"^spotify:(album|track|playlist):([A-Za-z0-9]+)$")


class SpotifyError(RuntimeError):
    """Purpose: report that a Spotify link could not be turned into words.

    Responsibilities: carry a sentence a person can act on. Boundaries: it never
    describes a failure of identification — nothing here identifies anything.
    Dependencies: none. Collaborators: ``SpotifyPointer`` and the window.
    Constraints: the message is shown verbatim, so it says what to do next.
    """


@dataclass(frozen=True, slots=True)
class SpotifyLink:
    """One thing a pasted Spotify address points at."""

    kind: str
    identifier: str

    @property
    def address(self) -> str:
        """The canonical web address for this link, which is what oEmbed wants."""
        return f"https://{SPOTIFY_HOST}/{self.kind}/{self.identifier}"


@dataclass(frozen=True, slots=True)
class PointedWords:
    """The words a link resolved to — an album to look for, and who made it."""

    album: str
    artist: str | None = None
    exact: bool = True
    """Whether these words came from the Web API rather than from oEmbed.

    An inexact answer is an album title with nobody's name beside it, and the
    screen says so: a search for an album title alone is a different promise
    from a search for the artist and the title together.
    """

    def as_query(self) -> str:
        """Join the words the way a person would type them into a search."""
        return f"{self.artist} {self.album}".strip() if self.artist else self.album


class TokenGranter(Protocol):
    """Exchange a client id and secret for a Spotify access token."""

    def token(self, client_id: str, client_secret: str, timeout_seconds: float) -> str:
        """Return a bearer token for the client-credentials flow."""


class UrllibTokenGranter:
    """Purpose: perform the one POST this module needs, and nothing else.

    Responsibilities: the client-credentials exchange. Boundaries: it holds no
    token between calls and knows nothing about albums. Dependencies: urllib.
    Collaborators: ``SpotifyPointer``. Constraints: separate from
    ``HttpTransport`` because that protocol is a GET and widening it for one
    caller would put a POST in front of every source in the project.
    """

    def token(self, client_id: str, client_secret: str, timeout_seconds: float) -> str:
        """Return a bearer token, raising ``SpotifyError`` when refused."""
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            ACCOUNTS_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise SpotifyError(f"Spotify refused the key: {error}") from error
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise SpotifyError("Spotify returned no access token for that key.")
        return token


def parse_spotify_link(text: str) -> SpotifyLink | None:
    """Read what a pasted Spotify address points at, without asking anyone.

    Accepts both shapes a client copies — the web address and the `spotify:`
    URI — and ignores the `?si=` tracking tail, which is on every link the share
    button produces.
    """
    candidate = text.strip()
    if not candidate:
        return None
    uri = _URI_LINK.match(candidate)
    if uri is not None:
        return SpotifyLink(kind=uri.group(1), identifier=uri.group(2))
    try:
        parsed = urllib.parse.urlparse(candidate)
    except ValueError:
        return None
    if parsed.hostname != SPOTIFY_HOST:
        return None
    match = _WEB_LINK.match(parsed.path)
    return SpotifyLink(kind=match.group(1), identifier=match.group(2)) if match else None


class SpotifyPointer:
    """Purpose: turn a Spotify link into the words to search for elsewhere.

    Responsibilities: resolve an album link to its title and artist, and a track
    link to the album that contains it. Boundaries: it stores nothing, caches
    nothing, and returns no identifier, duration, popularity or artwork — only
    words. Dependencies: an HTTP transport and, optionally, the user's own
    client credentials. Collaborators: the acquisition search and the album
    dialog. Constraints: without credentials it has only the public oEmbed
    endpoint, which names an album but never its artist and never a track's
    album.
    """

    def __init__(
        self,
        transport: HttpTransport,
        logger: logging.Logger,
        timeout_seconds: float = 10.0,
        credentials: EnvironmentCredentials | None = None,
        client_id_variable: str = "DIGLIBRARY_SPOTIFY_CLIENT_ID",
        client_secret_variable: str = "DIGLIBRARY_SPOTIFY_CLIENT_SECRET",
        granter: TokenGranter | None = None,
    ) -> None:
        """Create a pointer that uses the user's own key when one is set."""
        self._transport = transport
        self._logger = logger
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials
        self._client_id_variable = client_id_variable
        self._client_secret_variable = client_secret_variable
        self._granter = granter or UrllibTokenGranter()

    @property
    def has_key(self) -> bool:
        """Whether the user's own Spotify credentials are available right now."""
        return self._key() is not None

    def words_for(self, link: SpotifyLink) -> PointedWords:
        """Return the album words this link points at.

        A playlist is refused here on purpose: turning one into a queue of
        albums is its own gesture with its own screen, and answering a playlist
        with its first album would be this application deciding which of its
        records was meant.
        """
        if link.kind == "playlist":
            raise SpotifyError(
                "That is a playlist. Paste an album or a track link — a playlist "
                "is a list of records, and choosing one of them is yours to do."
            )
        key = self._key()
        if key is not None:
            return self._from_web_api(link, key)
        if link.kind == "track":
            # The oEmbed answer for a track carries the track's own title and
            # nothing else — not the artist, and not the album it belongs to —
            # so a track link cannot be read at all without the Web API.
            raise SpotifyError(
                "A track link needs your own Spotify key to find the album it is on. "
                "Paste the album's link instead."
            )
        return self._from_oembed(link)

    # --- internals ---------------------------------------------------------

    def _key(self) -> tuple[str, str] | None:
        if self._credentials is None:
            return None
        client_id = self._credentials.optional(self._client_id_variable)
        client_secret = self._credentials.optional(self._client_secret_variable)
        return (client_id, client_secret) if client_id and client_secret else None

    def _from_web_api(self, link: SpotifyLink, key: tuple[str, str]) -> PointedWords:
        token = self._granter.token(key[0], key[1], self._timeout_seconds)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        payload = self._json(f"{API_ROOT}/{link.kind}s/{link.identifier}", headers)
        album = payload.get("album") if link.kind == "track" else payload
        if not isinstance(album, Mapping):
            raise SpotifyError("Spotify answered without an album.")
        title = album.get("name")
        if not isinstance(title, str) or not title.strip():
            raise SpotifyError("Spotify answered without an album name.")
        return PointedWords(album=title.strip(), artist=_first_artist(album), exact=True)

    def _from_oembed(self, link: SpotifyLink) -> PointedWords:
        address = urllib.parse.quote(link.address, safe=":/")
        payload = self._json(f"{OEMBED_URL}?url={address}", {"Accept": "application/json"})
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SpotifyError("Spotify did not name that album.")
        # No artist: the public endpoint does not publish one. Said out loud
        # rather than guessed at, because a search without the artist is a
        # different search and the screen has to be able to say which it ran.
        return PointedWords(album=title.strip(), artist=None, exact=False)

    def _json(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]:
        try:
            response = self._transport.get(url, headers, self._timeout_seconds)
        except Exception as error:
            raise SpotifyError(f"Spotify could not be reached: {error}") from error
        if response.status == 404:
            raise SpotifyError("Spotify has nothing at that link.")
        if response.status < 200 or response.status >= 300:
            raise SpotifyError(f"Spotify answered HTTP {response.status}.")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except Exception as error:
            raise SpotifyError(f"Spotify's answer could not be read: {error}") from error
        if not isinstance(payload, Mapping):
            raise SpotifyError("Spotify's answer was not what this app expected.")
        return payload


def _first_artist(album: Mapping[str, Any]) -> str | None:
    """Return the album's credited artists, joined the plain way.

    Deliberately not `written_credit`: that renders what a *catalogue* published
    and this is not a catalogue. These words exist to be typed into a search box
    and then forgotten.
    """
    artists = album.get("artists")
    if not isinstance(artists, list):
        return None
    names = [
        str(artist["name"]).strip()
        for artist in artists
        if isinstance(artist, Mapping) and str(artist.get("name", "")).strip()
    ]
    return " ".join(names[:2]) or None
