"""Storing canonical metadata as JSON, and reading it back unchanged."""

import json
from datetime import date
from typing import Any

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSourceId,
    ReleaseMetadata,
    TrackMetadata,
)


def serialize_release(release: ReleaseMetadata) -> str:
    """Return one release as JSON, complete enough to rebuild it exactly.

    The whole release is kept, not a summary, so that an improved matcher can be
    re-run later without asking a rate-limited service the same question again.
    """
    return json.dumps(_release_payload(release), ensure_ascii=False, sort_keys=True)


class UnreadableReleaseError(ValueError):
    """Raised when stored JSON is not a release this build can rebuild."""


def deserialize_release(payload: str) -> ReleaseMetadata:
    """Rebuild a release from stored JSON, or refuse a payload of the wrong shape.

    What is stored came from somebody else's API, through a serializer that may
    not be this one — a row written by an older build, by a provider whose
    shape has since moved, or by a hand editing the file. A payload that is a
    list rather than an object, or whose `artists` is a bare string, must not
    raise `TypeError` out of the store: that would not skip one release, it
    would end the restore or the identification pass that asked for it.

    Refused as :class:`UnreadableReleaseError` so the caller can pass over one row.
    """
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise UnreadableReleaseError("A stored release is not readable JSON.") from error
    if not isinstance(data, dict):
        raise UnreadableReleaseError("A stored release is not a JSON object.")
    try:
        return _release_from(data)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise UnreadableReleaseError("A stored release is not shaped like one.") from error


def _release_from(data: dict[str, Any]) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSourceId(data["source"]),
        source_release_id=data["source_release_id"],
        title=data["title"],
        artists=tuple(_artist(item) for item in data.get("artists", ())),
        tracks=tuple(_track(item) for item in data.get("tracks", ())),
        released_on=_date(data.get("released_on")),
        original_released_on=_date(data.get("original_released_on")),
        country=data.get("country"),
        barcode=data.get("barcode"),
        labels=tuple(data.get("labels", ())),
        catalog_numbers=tuple(data.get("catalog_numbers", ())),
        genres=tuple(data.get("genres", ())),
        styles=tuple(data.get("styles", ())),
        formats=tuple(data.get("formats", ())),
        release_group_id=data.get("release_group_id"),
        external_ids=_external_ids(data.get("external_ids", {})),
    )


def _release_payload(release: ReleaseMetadata) -> dict[str, Any]:
    return {
        "source": str(release.source),
        "source_release_id": release.source_release_id,
        "title": release.title,
        "artists": [_artist_payload(artist) for artist in release.artists],
        "tracks": [_track_payload(track) for track in release.tracks],
        "released_on": release.released_on.isoformat() if release.released_on else None,
        "original_released_on": (
            release.original_released_on.isoformat() if release.original_released_on else None
        ),
        "country": release.country,
        "barcode": release.barcode,
        "labels": list(release.labels),
        "catalog_numbers": list(release.catalog_numbers),
        "genres": list(release.genres),
        "styles": list(release.styles),
        "formats": list(release.formats),
        "release_group_id": release.release_group_id,
        "external_ids": {str(key): value for key, value in release.external_ids.items()},
    }


def _artist_payload(artist: ArtistMetadata) -> dict[str, Any]:
    # The separator has to travel with the artist, or a restored release joins
    # its artists with `&` again: a field the payload does not carry is a rule
    # that cannot fire on anything read back from the cache.
    return {
        "name": artist.name,
        "sort_name": artist.sort_name,
        "external_ids": {str(key): value for key, value in artist.external_ids.items()},
        "credited_as": artist.credited_as,
        "joined_by": artist.joined_by,
    }


def _track_payload(track: TrackMetadata) -> dict[str, Any]:
    return {
        "title": track.title,
        "position": track.position,
        "medium_number": track.medium_number,
        "position_on_medium": track.position_on_medium,
        # What the source printed, which is a different thing from this
        # application's own count of the tracks. Without it every release read
        # back from the cache has it as None, and the medley fold cannot fire
        # on any album restored from the cache.
        "published_position": track.published_position,
        "artists": [_artist_payload(artist) for artist in track.artists],
        "duration_ms": track.duration_ms,
        "isrcs": list(track.isrcs),
        "external_ids": {str(key): value for key, value in track.external_ids.items()},
    }


def _artist(data: dict[str, Any]) -> ArtistMetadata:
    return ArtistMetadata(
        name=data["name"],
        sort_name=data.get("sort_name"),
        external_ids=_external_ids(data.get("external_ids", {})),
        credited_as=data.get("credited_as"),
        joined_by=data.get("joined_by"),
    )


def _track(data: dict[str, Any]) -> TrackMetadata:
    return TrackMetadata(
        title=data["title"],
        position=data.get("position"),
        medium_number=data.get("medium_number"),
        position_on_medium=data.get("position_on_medium"),
        # Absent from payloads written by older builds, and absent reads as
        # None, which is how those releases already behave.
        published_position=data.get("published_position"),
        artists=tuple(_artist(item) for item in data.get("artists", ())),
        duration_ms=data.get("duration_ms"),
        isrcs=tuple(data.get("isrcs", ())),
        external_ids=_external_ids(data.get("external_ids", {})),
    )


def _external_ids(data: dict[str, str]) -> dict[MetadataSourceId, str]:
    return {MetadataSourceId(key): value for key, value in data.items()}


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
