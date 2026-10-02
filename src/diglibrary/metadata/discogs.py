"""Discogs metadata client and mapping into the canonical model."""

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
from diglibrary.metadata.authentication import EnvironmentCredentials, discogs_headers
from diglibrary.metadata.normalization import (
    normalize_discogs_artist_name,
    normalize_identifier,
    normalize_text,
    parse_duration_ms,
    parse_partial_date,
    renumber_after_drop,
)
from diglibrary.metadata.transport import JsonHttpClient

_BASE_URL = "https://api.discogs.com"


class DiscogsClient:
    """Read Discogs releases and map each response into canonical metadata."""

    def __init__(
        self,
        http_client: JsonHttpClient,
        credentials: EnvironmentCredentials,
        token_environment_variable: str,
    ) -> None:
        """Create a Discogs client without resolving its token until request time."""
        self._http_client = http_client
        self._credentials = credentials
        self._token_environment_variable = token_environment_variable

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        """Search Discogs releases without ranking or selecting the returned results."""
        parameters = {"type": "release", "per_page": "100"}
        if query.text:
            # The free-text fallback: one general parameter, no field
            # constraints, for titles the structured search cannot see through.
            parameters["q"] = query.text
        if query.artist:
            parameters["artist"] = query.artist
        if query.album:
            parameters["release_title"] = query.album
        if query.barcode:
            parameters["barcode"] = query.barcode
        payload = self._get("/database/search", parameters)
        results = payload.get("results", [])
        if not isinstance(results, list):
            return ()
        return tuple(
            _map_discogs_search_result(result) for result in results if isinstance(result, dict)
        )

    def get_release(self, release_id: str) -> ReleaseMetadata:
        """Retrieve and map one full Discogs release by its provider identifier."""
        payload = self._get(f"/releases/{release_id}", {})
        return _map_discogs_release(payload)

    def main_release_id(self, group_id: str) -> str | None:
        """Return the release a Discogs master points at as its principal edition.

        A master is the album, not a pressing of it, and Discogs sends a search
        result straight to the master page — so a link a user copies from their
        browser is very often a master. Only a release carries a tracklist that
        files can be aligned against, so the master is followed to the edition
        Discogs itself considers the main one.
        """
        payload = self._get(f"/masters/{group_id}", {})
        main = payload.get("main_release")
        return str(main) if main else None

    def first_release_date(self, group_id: str) -> date | None:
        """Return the year a Discogs master was first released.

        A pressing carries its own year, so a CD reissue of a 1974 album says
        2006. The master is where the album's own year lives.
        """
        payload = self._get(f"/masters/{group_id}", {})
        year = payload.get("year")
        return parse_partial_date(str(year)) if year else None

    def _get(self, path: str, parameters: Mapping[str, str]) -> Mapping[str, Any]:
        token = self._credentials.required(self._token_environment_variable)
        url = f"{_BASE_URL}{path}?{urlencode(parameters)}" if parameters else f"{_BASE_URL}{path}"
        return self._http_client.get(url, discogs_headers(token))


def _map_discogs_search_result(payload: Mapping[str, Any]) -> ReleaseMetadata:
    identifier = normalize_identifier(payload.get("id")) or ""
    title = (
        normalize_text(payload.get("title") if isinstance(payload.get("title"), str) else None)
        or ""
    )
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title=title,
        artists=(),
        released_on=parse_partial_date(str(payload["year"])) if payload.get("year") else None,
        country=normalize_text(
            payload.get("country") if isinstance(payload.get("country"), str) else None
        ),
        formats=_string_values(payload.get("format")),
        # The label and the catalogue number are what tell two pressings of one
        # album apart, and a search answers with both: a list of label names and
        # one catalogue number.
        labels=_string_values(payload.get("label")),
        catalog_numbers=tuple(
            number for number in (normalize_text(_string(payload, "catno")),) if number
        ),
        external_ids={MetadataSources.DISCOGS: identifier},
    )


def _map_discogs_release(payload: Mapping[str, Any]) -> ReleaseMetadata:
    identifier = normalize_identifier(payload.get("id")) or ""
    labels = _mapping_values(payload.get("labels"), "name")
    catalog_numbers = _mapping_values(payload.get("labels"), "catno")
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id=identifier,
        title=normalize_text(_string(payload, "title")) or "",
        artists=_discogs_artists(payload.get("artists")),
        tracks=_discogs_tracks(payload.get("tracklist")),
        released_on=parse_partial_date(_string(payload, "released") or _string(payload, "year")),
        country=normalize_text(_string(payload, "country")),
        barcode=normalize_text(_string(payload, "barcode")),
        labels=labels,
        catalog_numbers=catalog_numbers,
        genres=_string_values(payload.get("genres")),
        styles=_string_values(payload.get("styles")),
        formats=_formats_with_descriptions(payload.get("formats")),
        release_group_id=normalize_identifier(payload.get("master_id")),
        external_ids={MetadataSources.DISCOGS: identifier},
    )


def _discogs_artists(value: object) -> tuple[ArtistMetadata, ...]:
    if not isinstance(value, list):
        return ()
    artists: list[ArtistMetadata] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = normalize_discogs_artist_name(_string(item, "name") or "")
        if name:
            identifier = normalize_identifier(item.get("id"))
            # `anv` is how this artist was credited on this particular sleeve and
            # `join` is what the sleeve prints before the next one. Without
            # them every collaboration would be written with `&` whatever the
            # record says.
            credited_as = normalize_discogs_artist_name(_string(item, "anv") or "")
            artists.append(
                ArtistMetadata(
                    name=name,
                    external_ids={MetadataSources.DISCOGS: identifier} if identifier else {},
                    credited_as=credited_as if credited_as and credited_as != name else None,
                    joined_by=normalize_text(_string(item, "join")),
                )
            )
    return tuple(artists)


_DISCOGS_MEDIUM_POSITION = re.compile(r"^\s*(\d{1,2})\s*[-.]\s*(\d{1,3})\s*$")


def _discogs_tracks(value: object) -> tuple[TrackMetadata, ...]:
    """Map a Discogs tracklist, skipping the entries that are not tracks.

    A Discogs tracklist interleaves headings and index entries with the real
    tracks. Counting those as tracks would inflate the release's track count and
    make it fail to match the folder it actually describes.
    """
    if not isinstance(value, list):
        return ()
    tracks: list[TrackMetadata] = []
    position = 0
    dropped = False
    for item in value:
        if not isinstance(item, dict):
            continue
        entry_type = _string(item, "type_")
        if entry_type is not None and entry_type != "track":
            continue
        title = normalize_text(_string(item, "title"))
        if not title:
            continue
        duration = _string(item, "duration")
        if _is_not_audio(_string(item, "position"), duration):
            dropped = True
            continue
        position += 1
        medium_number, position_on_medium = _discogs_medium(_string(item, "position"))
        tracks.append(
            TrackMetadata(
                title=title,
                position=position,
                medium_number=medium_number,
                position_on_medium=position_on_medium,
                published_position=_string(item, "position"),
                artists=_discogs_artists(item.get("artists")),
                duration_ms=parse_duration_ms(duration),
            )
        )
    return renumber_after_drop(tuple(tracks)) if dropped else tuple(tracks)


def _is_not_audio(position: str | None, duration: str | None) -> bool:
    """Report whether a tracklist entry describes something other than audio.

    An enhanced CD's data portion is listed as a track, with a position such
    as `Enhanced` and an empty duration. No rip of that disc can hold it, so
    kept as a track it has no file and pushes every later track's number up by
    one.

    A position with no digit and no duration is not enough to decide that: a
    7" single has sides `A` and `B` and often no lengths typed in, and dropping
    those leaves the release with no tracks at all. So the question is what the
    position *is*, not what it lacks. One or two letters is a side — `A`, `B`,
    `AA` — and a longer word with no digit and no length is what this skips:
    `Enhanced`, `Video`, `DVD`, `Data`.

    Punctuation is not part of the answer. Sides are also written `A.` and
    `B.`, so `isalpha()` on the whole position would reject them; the letters
    are counted and everything else is ignored.
    """
    if not position:
        return False
    if any(character.isdigit() for character in position):
        return False
    if duration:
        return False
    letters = [character for character in position if character.isalpha()]
    return not (0 < len(letters) <= 2)


def _discogs_medium(position: str | None) -> tuple[int | None, int | None]:
    """Read a disc and track number from a Discogs position string.

    Only the unambiguous ``disc-track`` form is interpreted. Vinyl positions
    such as ``A1`` describe a side rather than a disc, and a bare number carries
    no disc information, so both leave the medium unknown rather than guessed.
    """
    if not position:
        return None, None
    match = _DISCOGS_MEDIUM_POSITION.fullmatch(position)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _string_values(value: object) -> tuple[str, ...]:
    return (
        tuple(normalized for item in value if (normalized := normalize_text(item)))
        if isinstance(value, list)
        else ()
    )


def _formats_with_descriptions(value: object) -> tuple[str, ...]:
    """Return format names and their descriptions, both.

    "CD" alone cannot tell a plain reissue from a Deluxe Edition; Discogs
    carries that distinction in the descriptions ("Album, Deluxe Edition,
    Remastered"), and the edition rule in naming needs it.
    """
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = normalize_text(_string(item, "name"))
        if name and name not in found:
            found.append(name)
        descriptions = item.get("descriptions")
        if isinstance(descriptions, list):
            for description in descriptions:
                normalized = normalize_text(description)
                if normalized and normalized not in found:
                    found.append(normalized)
    return tuple(found)


def _mapping_values(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        normalized
        for item in value
        if isinstance(item, dict) and (normalized := normalize_text(_string(item, key)))
    )
