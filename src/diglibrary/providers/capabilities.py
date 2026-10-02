"""Reusable generic provider capability declarations."""

from diglibrary.providers.models import ProviderCapability


class ProviderCapabilities:
    """Purpose: provide common capability values without closing the extension set.

    Responsibilities: expose reusable, immutable capability constants for future
    provider metadata. Boundaries: it contains no behavior, service knowledge, or
    capability-selection policy. Dependencies: ``ProviderCapability`` only.
    Collaborators: provider implementations, registry validation, and manager
    dispatch. Constraints: callers may construct additional valid capabilities;
    this namespace must never become a restrictive enumeration.
    """

    SEARCH_ALBUM = ProviderCapability("search_album")
    SEARCH_TRACK = ProviderCapability("search_track")
    SEARCH_PLAYLIST = ProviderCapability("search_playlist")
    RESOLVE_SPOTIFY_URL = ProviderCapability("resolve_spotify_url")
    DOWNLOAD_ALBUM = ProviderCapability("download_album")
    DOWNLOAD_TRACK = ProviderCapability("download_track")
    FLAC = ProviderCapability("flac")
    MP3 = ProviderCapability("mp3")
    METADATA = ProviderCapability("metadata")
