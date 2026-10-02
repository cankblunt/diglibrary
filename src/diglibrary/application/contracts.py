"""Application-layer data contracts shared across architectural boundaries."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from diglibrary.identity import validate_identifier
from diglibrary.providers.types import ProviderId


@dataclass(frozen=True, slots=True)
class ResolutionRequest:
    """Unresolved user input accepted by the Resolution Engine."""

    value: str


@dataclass(frozen=True, slots=True)
class ResolvedRequest:
    """Canonicalized music request supplied to providers by the application layer."""

    value: str


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """An unranked candidate returned by a provider to the application layer."""

    provider: ProviderId
    identifier: str
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, order=True)
class MetadataSourceId:
    """Purpose: identify one trusted metadata source by a stable, portable name.

    Responsibilities: validate and transport a metadata-source identifier.
    Boundaries: it does not rank sources, fetch data, or enumerate the sources
    this project ships. Dependencies: the shared identifier validation only.
    Collaborators: ``ReleaseMetadata``, ``MetadataService``, and source clients.
    Constraints: the set of sources is open — precedence is configured as an
    ordered sequence, so adding a source never requires changing a type.
    """

    value: str

    def __post_init__(self) -> None:
        """Reject identifiers that are not portable across configuration and storage."""
        validate_identifier(self.value, "metadata source")

    def __str__(self) -> str:
        """Return the identifier as written in configuration, logs, and storage."""
        return self.value


class MetadataSources:
    """Purpose: name the trusted metadata sources this project ships.

    Responsibilities: expose reusable identifier constants in the default
    precedence order. Boundaries: it holds no behavior, no client, and no
    ranking policy, and it is explicitly not a closed enumeration — callers may
    construct any other valid ``MetadataSourceId``. Dependencies:
    ``MetadataSourceId`` only. Collaborators: source clients and the composition
    root. Constraints: precedence lives in the ordered sequence given to
    ``MetadataService``, never in this namespace.
    """

    DISCOGS = MetadataSourceId("discogs")
    MUSICBRAINZ = MetadataSourceId("musicbrainz")
    BANDCAMP = MetadataSourceId("bandcamp")
    # The one source that is not a catalogue and never leaves this machine: the
    # files' own tags, read as they are. It is here beside the others
    # because everything downstream — matching, aligning, naming, the panel that
    # says where each source stands — asks a release which source it came from,
    # and a release with no source would have to be special-cased in every one
    # of those places.
    TAGS = MetadataSourceId("tags")


@dataclass(frozen=True, slots=True)
class ArtistMetadata:
    """Canonical, provider-independent artist metadata."""

    name: str
    sort_name: str | None = None
    external_ids: Mapping[MetadataSourceId, str] = field(default_factory=dict)
    # How this artist was credited *on this release*, when the sleeve says
    # something other than the catalogue's canonical name — Discogs calls it
    # `anv`. Carried and not yet used to name anything, because it cuts both
    # ways: a sleeve can restore punctuation or an accent the canonical name
    # lacks, and it can equally drop an accent the canonical name has. Which
    # spelling wins is a choice for the user, not for this field.
    credited_as: str | None = None
    # The words the source prints *between* this artist and the next one:
    # `&`, `,`, `Feat.`, `Presents`, `Convida`. Kept on the artist it follows
    # because that is the only place it means anything, and empty on the last.
    joined_by: str | None = None


def written_credit(artists: Sequence[ArtistMetadata]) -> str:
    """Join a source's artists the way the source itself joins them.

    One function, so that every place that writes a credit joins it the same
    way. A hard-coded `" & "` is not what catalogues publish: `&` is only one
    of the separators in use, beside `,`, `Feat.`, `Featuring`, `Ft.`, `+`,
    `Presents`, `With` and `Vs.`, and replacing them all with `&` changes the
    credit the sleeve prints.

    A separator that begins with a comma is written tight against the name
    before it; everything else stands between spaces. One rule, no table of
    special cases.

    `" & "` remains the fallback, for every source that publishes artists
    without saying how it joins them.
    """
    written = ""
    previous: ArtistMetadata | None = None
    for artist in artists:
        name = artist.name.strip()
        if not name:
            continue
        if previous is None:
            written = name
            previous = artist
            continue
        separator = (previous.joined_by or "").strip()
        previous = artist
        if not separator:
            written += f" & {name}"
        elif separator.startswith(","):
            written += f"{separator} {name}"
        else:
            written += f" {separator} {name}"
    return written


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Canonical track metadata belonging to a release."""

    title: str
    position: int | None = None
    medium_number: int | None = None
    position_on_medium: int | None = None
    artists: tuple[ArtistMetadata, ...] = ()
    # How the credit is *written*, when that is not simply the artists joined.
    # A file name is one string and a tag holds several, and the two are not the
    # same fact: "First Artist, Second Artist" names the file, while the tag
    # gets two values a player can group by. Discogs models this as a `join`
    # between artists and MusicBrainz calls it an artist credit; here it is only
    # ever set by a correction typed by the user, because a source's own credit
    # is already carried by `artists`.
    artist_credit: str | None = None
    # Exactly what the source printed for this track's place — `A5.1`, `B2`,
    # `1.01`. `position` above is this application's own count, and counting is
    # what destroyed the fact that mattered: `A5.1` and `A5.2` are two songs
    # inside ONE track, and flattened to 5 and 6 they became two tracks, one of
    # which no file could ever hold. Kept verbatim, and read only by
    # the rule that decides whether two entries are one recording.
    published_position: str | None = None
    duration_ms: int | None = None
    isrcs: tuple[str, ...] = ()
    external_ids: Mapping[MetadataSourceId, str] = field(default_factory=dict)
    # Whether the user added this track because the local copy holds it and the
    # catalogue never listed it. It travels *on the track* rather than in a set
    # kept beside the release, because the release is handed around and
    # replanned, and a companion set is a second copy that parts from the first:
    # growing an already-grown release would otherwise insert the track twice.
    # It is also the only way a screen can say which row was added without
    # recomputing the answer.
    added_by_user: bool = False


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    """Canonical album or release metadata returned by one metadata source."""

    source: MetadataSourceId
    source_release_id: str
    title: str
    artists: tuple[ArtistMetadata, ...]
    tracks: tuple[TrackMetadata, ...] = ()
    released_on: date | None = None
    original_released_on: date | None = None
    country: str | None = None
    barcode: str | None = None
    labels: tuple[str, ...] = ()
    catalog_numbers: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    release_group_id: str | None = None
    external_ids: Mapping[MetadataSourceId, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetadataQuery:
    """Provider-independent criteria for release metadata discovery.

    ``text`` is a free-text query used when the structured fields found nothing:
    each client maps it onto its own general-search parameter, unconstrained by
    field, which is the widest net a catalogue offers.
    """

    artist: str | None = None
    album: str | None = None
    track: str | None = None
    barcode: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataSearchResult:
    """Unranked releases returned by one source for a metadata query."""

    source: MetadataSourceId
    releases: Sequence[ReleaseMetadata]
