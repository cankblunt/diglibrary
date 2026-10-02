"""MusicBrainz metadata client and mapping into the canonical model."""

import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlencode

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.metadata.authentication import EnvironmentCredentials, musicbrainz_headers
from diglibrary.metadata.normalization import (
    normalize_identifier,
    normalize_text,
    parse_duration_ms,
    parse_partial_date,
    renumber_after_drop,
)
from diglibrary.metadata.transport import JsonHttpClient

_BASE_URL = "https://musicbrainz.org/ws/2"
_LUCENE_SPECIALS = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


PROJECT_CONTACT = "https://github.com/cankblunt/diglibrary"
"""How this application identifies itself to MusicBrainz when nobody overrides it.

Their published requirement is that the User-Agent carry enough for them to
reach *the application's maintainers* — the documented shape is the project's
website or the maintainer's email. Demanding an address from every person who
installs this, through `DIGLIBRARY_MUSICBRAINZ_CONTACT`, would be a barrier to
the first run and the wrong answer to what MusicBrainz asks: a user's address
does not let them reach the maintainers.

Not a credential, so nothing about the no-embedded-secrets rule touches it: it is
a return address, published on purpose, and it is what makes MusicBrainz work
with nothing configured at all.

The environment variable still wins where it is set — a fork, or anyone who
wants their own address on their own traffic.
"""


class MusicBrainzClient:
    """Read MusicBrainz releases and map each response into canonical metadata."""

    def __init__(
        self,
        http_client: JsonHttpClient,
        credentials: EnvironmentCredentials,
        contact_environment_variable: str,
        access_token_environment_variable: str,
    ) -> None:
        """Create a MusicBrainz client without resolving environment values until request time."""
        self._http_client = http_client
        self._credentials = credentials
        self._contact_environment_variable = contact_environment_variable
        self._access_token_environment_variable = access_token_environment_variable

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Search MusicBrainz releases without ranking or selecting the returned results."""
        if query.text:
            # The free-text fallback: bare terms against the default index,
            # with Lucene's own operators stripped so a title like
            # "Salt & Sand" is words, not syntax.
            lucene = _escape_lucene(query.text)
        else:
            terms = [
                f'artist:"{query.artist}"' if query.artist else None,
                f'release:"{query.album}"' if query.album else None,
                f'barcode:"{query.barcode}"' if query.barcode else None,
            ]
            lucene = " AND ".join(term for term in terms if term)
        payload = self._get("/release", {"query": lucene, "limit": "100"})
        releases = payload.get("releases", [])
        if not isinstance(releases, list):
            return ()
        return tuple(
            _map_musicbrainz_release(release) for release in releases if isinstance(release, dict)
        )

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Retrieve and map one full MusicBrainz release by MBID."""
        payload = self._get(
            f"/release/{release_id}",
            {"inc": "artists+recordings+labels+release-groups+genres"},
        )
        return _map_musicbrainz_release(payload)

    def first_release_date(self, group_id: str) -> date | None:
        """Return the date a MusicBrainz release group was first released."""
        payload = self._get(f"/release-group/{group_id}", {})
        return parse_partial_date(_string(payload, "first-release-date"))

    def releases_in_group(self, group_id: str) -> tuple[ReleaseMetadata, ...]:
        """Return every pressing of one release group, with its tracklist.

        This is what turns an acoustic answer into candidates: a fingerprint
        names a *recording*, and the release groups its recordings share name
        the album, but only a pressing has a tracklist to align files against.
        Every pressing comes back, in one request, and none is chosen here —
        which of them the files actually are is a question of durations, and
        that is the Decision Engine's.
        """
        payload = self._get(
            "/release",
            {"release-group": group_id, "inc": "recordings artist-credits", "limit": "100"},
        )
        releases = payload.get("releases", [])
        if not isinstance(releases, list):
            return ()
        return tuple(
            _map_musicbrainz_release(release) for release in releases if isinstance(release, dict)
        )

    def _get(self, path: str, parameters: Mapping[str, str]) -> Mapping[str, Any]:
        contact = self._credentials.optional(self._contact_environment_variable) or PROJECT_CONTACT
        token = self._credentials.optional(self._access_token_environment_variable)
        query = {**parameters, "fmt": "json"}
        return self._http_client.get(
            f"{_BASE_URL}{path}?{urlencode(query)}", musicbrainz_headers(contact, token)
        )


def _escape_lucene(text: str) -> str:
    """Reduce free text to bare search terms."""
    return " ".join(_LUCENE_SPECIALS.sub(" ", text).split())


def _map_musicbrainz_release(payload: Mapping[str, Any]) -> ReleaseMetadata:
    identifier = normalize_identifier(payload.get("id")) or ""
    release_group = payload.get("release-group")
    release_group_id = (
        normalize_identifier(release_group.get("id")) if isinstance(release_group, dict) else None
    )
    return ReleaseMetadata(
        source=MetadataSources.MUSICBRAINZ,
        source_release_id=identifier,
        title=normalize_text(_string(payload, "title")) or "",
        artists=_musicbrainz_artists(payload.get("artist-credit")),
        tracks=_musicbrainz_tracks(payload.get("media")),
        released_on=parse_partial_date(_string(payload, "date")),
        original_released_on=(
            parse_partial_date(_string(release_group, "first-release-date"))
            if isinstance(release_group, dict)
            else None
        ),
        country=normalize_text(_string(payload, "country")),
        barcode=normalize_text(_string(payload, "barcode")),
        labels=_musicbrainz_labels(payload.get("label-info")),
        catalog_numbers=_musicbrainz_catalog_numbers(payload.get("label-info")),
        genres=_mapping_names(payload.get("genres")),
        formats=_musicbrainz_formats(payload.get("media")),
        release_group_id=release_group_id,
        external_ids={MetadataSources.MUSICBRAINZ: identifier},
    )


def _musicbrainz_artists(value: object) -> tuple[ArtistMetadata, ...]:
    if not isinstance(value, list):
        return ()
    artists: list[ArtistMetadata] = []
    for credit in value:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist")
        if not isinstance(artist, dict):
            continue
        name = normalize_text(_string(artist, "name"))
        if name:
            identifier = normalize_identifier(artist.get("id"))
            artists.append(
                ArtistMetadata(
                    name=name,
                    sort_name=normalize_text(_string(artist, "sort-name")),
                    external_ids={MetadataSources.MUSICBRAINZ: identifier} if identifier else {},
                )
            )
    return tuple(artists)


def _musicbrainz_tracks(value: object) -> tuple[TrackMetadata, ...]:
    if not isinstance(value, list):
        return ()
    tracks: list[TrackMetadata] = []
    position = 0
    dropped = False
    for medium_index, medium in enumerate(value, start=1):
        if not isinstance(medium, dict) or not isinstance(medium.get("tracks"), list):
            continue
        medium_number = _positive_integer(medium.get("position")) or medium_index
        for track_index, track in enumerate(medium["tracks"], start=1):
            if not isinstance(track, dict):
                continue
            title = normalize_text(_string(track, "title"))
            if title and _is_data_track(title):
                # MusicBrainz lists an enhanced CD's data portion as a track of
                # its own, by convention titled `[data track]`. No rip can hold
                # it, so counting it would leave a track no file can match and
                # push every later track's number up by one.
                dropped = True
                continue
            position += 1
            if not title:
                continue
            recording = track.get("recording")
            recording_mapping = recording if isinstance(recording, dict) else {}
            isrcs = _string_values(recording_mapping.get("isrcs"))
            identifier = normalize_identifier(track.get("id"))
            tracks.append(
                TrackMetadata(
                    title=title,
                    position=position,
                    medium_number=medium_number,
                    position_on_medium=_positive_integer(track.get("position")) or track_index,
                    artists=_musicbrainz_artists(track.get("artist-credit")),
                    duration_ms=parse_duration_ms(track.get("length")),
                    isrcs=isrcs,
                    external_ids={MetadataSources.MUSICBRAINZ: identifier} if identifier else {},
                )
            )
    return renumber_after_drop(tuple(tracks)) if dropped else tuple(tracks)


_DATA_TRACK = re.compile(r"^\[\s*data\s+track\s*\]$", re.IGNORECASE)
"""MusicBrainz's own name for the data portion of an enhanced disc."""


def _is_data_track(title: str) -> bool:
    """Report whether this entry is the disc's data portion rather than audio.

    Matched on the catalogue's convention rather than on words in any one
    language: MusicBrainz writes exactly `[data track]`, and a real song is
    never titled that. Discogs marks the same thing differently — there the
    entry has no number and no length — so each source is read in its own
    terms.
    """
    return _DATA_TRACK.fullmatch(title.strip()) is not None


def _musicbrainz_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    labels: list[str] = []
    for item in value:
        label = item.get("label") if isinstance(item, dict) else None
        if isinstance(label, dict) and (name := normalize_text(_string(label, "name"))):
            labels.append(name)
    return tuple(labels)


def _musicbrainz_catalog_numbers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        number
        for item in value
        if isinstance(item, dict) and (number := normalize_text(_string(item, "catalog-number")))
    )


def _musicbrainz_formats(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        format_name
        for medium in value
        if isinstance(medium, dict) and (format_name := normalize_text(_string(medium, "format")))
    )


def _mapping_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        name
        for item in value
        if isinstance(item, dict) and (name := normalize_text(_string(item, "name")))
    )


def _string_values(value: object) -> tuple[str, ...]:
    return (
        tuple(normalized for item in value if (normalized := normalize_text(item)))
        if isinstance(value, list)
        else ()
    )


def _string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _positive_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
