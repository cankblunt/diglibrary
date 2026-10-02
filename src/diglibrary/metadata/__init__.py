"""Metadata Engine infrastructure and canonical metadata models."""

from diglibrary.metadata.discogs import DiscogsClient
from diglibrary.metadata.models import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSearchResult,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.metadata.musicbrainz import MusicBrainzClient
from diglibrary.metadata.service import MetadataService

__all__ = [
    "ArtistMetadata",
    "DiscogsClient",
    "MetadataQuery",
    "MetadataSearchResult",
    "MetadataService",
    "MetadataSourceId",
    "MetadataSources",
    "MusicBrainzClient",
    "ReleaseMetadata",
    "TrackMetadata",
]
