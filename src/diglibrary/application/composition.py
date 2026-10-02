"""The sole composition root for DigLibrary infrastructure."""

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from diglibrary.application.acoustic import AcousticIdentification
from diglibrary.application.api import (
    FolderPicker,
    ImagePicker,
    ImageUrls,
    LibraryApi,
    PathOpener,
    _Pipeline,
)
from diglibrary.application.artwork import ArtworkPolicy, ArtworkService
from diglibrary.application.bench import QualityBench
from diglibrary.application.contracts import MetadataSources
from diglibrary.application.identification import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    IdentificationWorkflow,
)
from diglibrary.application.quality import QualitySurvey
from diglibrary.config import credentials
from diglibrary.config.loader import load_configuration
from diglibrary.config.models import ApplicationConfig
from diglibrary.connectors.slskd.client import SlskdClient, UrllibSlskdTransport
from diglibrary.connectors.slskd.connector import SlskdConnector
from diglibrary.connectors.slskd.mapper import SlskdMapper
from diglibrary.database.connection import Database
from diglibrary.database.copies import take_copy
from diglibrary.database.library_store import LibraryStore
from diglibrary.harmonic.analyzer import FfmpegAudioDecoder, HarmonicAnalyzer
from diglibrary.library.artwork import ArtworkStore, FilesystemArtworkStore
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import ChangeExecutor
from diglibrary.library.fingerprint import (
    ChromaprintFingerprinter,
    SubprocessFingerprintRunner,
    find_fpcalc,
)
from diglibrary.library.matching import AlbumMatcher
from diglibrary.library.naming import NamingError, NamingPolicy
from diglibrary.library.planner import ChangePlanner
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.logging.setup import configure_logging
from diglibrary.metadata.acoustid import REQUESTS_PER_SECOND as ACOUSTID_PER_SECOND
from diglibrary.metadata.acoustid import AcoustIdClient
from diglibrary.metadata.authentication import EnvironmentCredentials
from diglibrary.metadata.cache import JsonMetadataCache
from diglibrary.metadata.coverart import CoverArtArchiveClient
from diglibrary.metadata.discogs import DiscogsClient
from diglibrary.metadata.itunes import ITunesVerifier
from diglibrary.metadata.musicbrainz import MusicBrainzClient
from diglibrary.metadata.rate_limit import RateLimiter
from diglibrary.metadata.retry import RetryPolicy
from diglibrary.metadata.service import MetadataService
from diglibrary.metadata.spotify import SpotifyPointer
from diglibrary.metadata.transport import JsonHttpClient, UrllibTransport
from diglibrary.providers.manager import ProviderManager
from diglibrary.providers.registry import ProviderRegistry
from diglibrary.providers.soulseek import SOULSEEK_PROVIDER_ID, SoulseekProvider
from diglibrary.providers.types import ProviderId
from diglibrary.quality.analysis import (
    DEEP_PROBE_FREQUENCIES,
    FfmpegQualityAnalyzer,
    SubprocessCommandRunner,
    find_ffmpeg,
)
from diglibrary.quality.framing import FfmpegPcmReader, FrameGridProbe
from diglibrary.quality.spectrogram import SpectrogramRenderer

SPECTROGRAM_CACHE = Path("~/.diglibrary/cache/spectrograms").expanduser()
"""Where the drawn spectrograms are kept.

Named here rather than in `config.toml` because it is not a choice anyone has
to revisit: the pictures are discardable, and the one thing that matters
about the folder is that it is outside the repository and outside any folder
macOS guards with TCC. `~/.diglibrary` is already where this app keeps state that
must be readable whether it was launched from the Dock or from a terminal.
"""


@dataclass(frozen=True, slots=True)
class Application:
    """Expose the fully assembled DigLibrary infrastructure dependency graph.

    Purpose:
        Makes composition-root-owned services available to application workflows.
    Responsibilities:
        Holds initialized configuration, database, logging, metadata, provider, and connector
        infrastructure without creating or coordinating additional dependencies.
    Architectural boundaries:
        Is a dependency container, not a workflow, Engine, provider, or business service.
    Dependencies:
        Depends only on infrastructure instances assembled by ``create_application``.
    Expected collaborators:
        Application-layer workflows consume this value; no lower layer should construct it.
    Constraints:
        Its frozen structure preserves a single composition root and no field has hidden setup.
    """

    config: ApplicationConfig
    database: Database
    logger: logging.Logger
    metadata: MetadataService
    providers: ProviderManager
    acquisition: SoulseekProvider | None
    slskd: SlskdConnector
    artwork: ArtworkService
    artwork_store: ArtworkStore
    naming: NamingPolicy


def naming_from_settings(
    base: NamingPolicy, settings: Mapping[str, object], logger: logging.Logger
) -> NamingPolicy:
    """Build the naming policy the window's settings ask for.

    Public and in one place so that the test suite's pipeline factory calls
    this function instead of keeping a copy of it: a copy is what a test would
    measure, and the suite could then agree with itself while the window did
    something else.

    A composed arrangement wins over the ready-made style. Each template is
    applied on its own, because the composer has two lines and only one of them
    may have been filled in.

    An invalid template is refused rather than raised. A template is validated
    where it is saved, so an invalid one here means a file edited by hand or
    written by an older version; falling back to the style with a line in the
    log keeps a typo in a setting from being a window that will not open.
    """
    naming = base
    if "naming_style" in settings:
        naming = NamingPolicy.from_style(
            str(settings["naming_style"]),
            include_edition=bool(settings.get("include_edition", naming.include_edition)),
        )
    elif "include_edition" in settings:
        naming = replace(naming, include_edition=bool(settings["include_edition"]))
    for key in ("folder_template", "track_template"):
        composed = str(settings.get(key) or "").strip()
        if not composed:
            continue
        try:
            naming = replace(naming, **{key: composed})
        except NamingError:
            logger.warning(
                "A composed name arrangement was not usable and the style was kept.",
                extra={"operation": "settings.naming.refused", "which": key},
            )
    return naming


def create_application(config_path: Path) -> Application:
    """Compose and initialize DigLibrary infrastructure from a TOML file.

    Engine implementations are intentionally not created in Phase 1. Future
    application-layer workflows will receive their engine dependencies here.
    """
    config = load_configuration(config_path)
    logger = configure_logging(config.logging)
    # Before anything that reads a credential is built, and here because this is
    # the one place that builds anything. Under the icon the launcher
    # has already sourced the same file, so every name is in the environment
    # already and this moves none of them; from a terminal it is the only thing
    # that does. The count is all that is said — never a name's value.
    if brought := credentials.load():
        logger.info(
            "Keys were read from the environment file.",
            extra={"operation": "credentials.load", "keys": brought},
        )
    database = Database(config.database.path, logger)
    # A copy before the schema moves, and only when it is about to move. The
    # migrations are not all additive — two of them delete rows, one by a
    # `LIKE` heuristic — while `take_copy` runs at the *end* of the library
    # restore, much later. Without this an upgrade would run with no copy of
    # the state it is about to change: the only fallback would be the previous
    # session's, which is skipped whenever nothing changed and so may be
    # arbitrarily old. Free in the ordinary case, because `pending_migrations`
    # is a single query and answers False on every launch but the one after an
    # update.
    if database.pending_migrations():
        logger.info(
            "The schema is about to move, so the database is copied first.",
            extra={"operation": "database.copy.before_migration"},
        )
        take_copy(config.database.path, config.database.copies_directory, logger)
    database.initialize()
    metadata = _create_metadata_service(config, logger)
    artwork_store = FilesystemArtworkStore()
    slskd = SlskdConnector(
        config.slskd,
        SlskdClient(config.slskd, UrllibSlskdTransport(), logger, os.environ.get),
        SlskdMapper(),
        logger,
    )
    providers, acquisition = _compose_providers(config, slskd, logger)
    logger.info("Application initialized.", extra={"operation": "application.initialize"})
    return Application(
        config=config,
        database=database,
        logger=logger,
        metadata=metadata,
        providers=providers,
        acquisition=acquisition,
        slskd=slskd,
        artwork=_create_artwork_service(config, logger, artwork_store),
        artwork_store=artwork_store,
        naming=config.naming,
    )


def create_api(
    application: Application,
    folder_picker: FolderPicker | None = None,
    image_picker: ImagePicker | None = None,
    path_opener: PathOpener | None = None,
    icon_maker: Callable[[], dict[str, object]] | None = None,
    keep_awake: Callable[[bool], None] | None = None,
    images: ImageUrls | None = None,
) -> LibraryApi:
    """Assemble the window's application API over an initialized Application.

    The pipeline factory rebuilds the pieces that depend on the window's own
    settings — naming style, artwork, threshold — while everything
    infrastructural stays what ``create_application`` composed.
    """
    config = application.config
    logger = application.logger
    store = LibraryStore(application.database, logger)
    tag_store = MutagenTagStore()
    artwork_store = application.artwork_store
    # One question asked once. Four places want ffmpeg, and four independent
    # lookups are four answers that only happen to agree: the window could
    # report that audio can be measured from one of them while another had
    # given up. Resolved here and handed down, so the fact the screens are told
    # and the objects that do the work come from the same look at the disk.
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning(
            "ffmpeg was not found on PATH or in its usual locations; "
            "no audio can be measured on this machine.",
            extra={"operation": "composition.ffmpeg.absent"},
        )
    else:
        logger.info(
            "Audio will be measured with ffmpeg.",
            extra={"operation": "composition.ffmpeg.found", "executable": ffmpeg},
        )
    # Composed once rather than per pipeline: it holds a rate limiter, and a
    # fresh one per settings change would hand every scan its own budget. Once
    # *per state of the key*, though — it is composed from the environment, and
    # a key pasted into the window arrives after this line has run. Composing
    # it exactly once would leave `Connected services` saying connected while
    # the fingerprint path stayed absent until the application was restarted.
    composed: dict[bool, AcousticIdentification | None] = {}

    def acoustic_now() -> AcousticIdentification | None:
        has_key = bool(os.environ.get(credentials.ACOUSTID))
        if has_key not in composed:
            composed.clear()
            composed[has_key] = _create_acoustic_identification(
                config, logger, application.metadata, store
            )
        return composed[has_key]

    def pipeline(settings: dict[str, object]) -> _Pipeline:
        naming = naming_from_settings(config.naming, settings, logger)
        artwork = application.artwork
        if settings.get("artwork_enabled") is False:
            artwork = None
        threshold = float(settings.get("threshold", DEFAULT_CONFIDENCE_THRESHOLD))
        matcher = AlbumMatcher()
        planner = ChangePlanner(naming, tag_store, artwork_store)
        # The independent witness is always composed; it is not a setting.
        # What keeps it affordable is *when* it is asked — only about an album
        # that did not settle on its own, and on demand when one is opened —
        # because the service allows roughly twenty calls a minute and a
        # library holds thousands of folders.
        verifier = ITunesVerifier(logger)
        workflow = IdentificationWorkflow(
            metadata=application.metadata,
            matcher=matcher,
            planner=planner,
            tag_store=tag_store,
            logger=logger,
            threshold=threshold,
            artwork=artwork,
            verifier=verifier,
            acoustic=acoustic_now(),
        )
        executor = ChangeExecutor(tag_store, logger, artwork_store, config.artwork.backup_directory)
        scanner = LibraryScanner(MutagenAudioProbe(), logger)
        survey = _create_quality_survey(scanner, logger, store, ffmpeg)
        return _Pipeline(
            scanner=scanner,
            workflow=workflow,
            executor=executor,
            threshold=threshold,
            planner=planner,
            artwork=artwork,
            tag_store=tag_store,
            quality=survey,
            bench=QualityBench(
                scanner,
                store,
                logger,
                survey,
                _create_deep_analyzer(logger, ffmpeg),
                # So the bench can put one file to the service on request,
                # which is the only way the stored acoustic answers grow.
                acoustic=acoustic_now(),
            ),
        )

    return LibraryApi(
        acquisition=application.acquisition,
        pipeline_factory=pipeline,
        store=store,
        artwork_store=artwork_store,
        logger=logger,
        folder_picker=folder_picker,
        image_picker=image_picker,
        cover_cache=config.artwork.staging_directory / "covers",
        path_opener=path_opener,
        icon_maker=icon_maker,
        keep_awake=keep_awake,
        spectrograms=_create_spectrogram_renderer(logger, ffmpeg),
        # The two caches that are kept under a ceiling. The artwork budget
        # covers the staged pictures and the sleeves the window draws as one
        # number, because what is set in Settings has to be what the disk
        # holds.
        metadata_cache=JsonMetadataCache(
            config.metadata.cache_directory, config.metadata.cache_ttl_seconds
        ),
        artwork_cache=(
            config.artwork.staging_directory,
            config.artwork.staging_directory / "covers",
        ),
        backups=(config.artwork.backup_directory,),
        # The database and where its copies go. Not `backups` above — that is
        # the images a write replaced, and these are the database itself,
        # taken as the window opens and bounded by its own ceiling.
        database=config.database.path,
        database_copies=config.database.copies_directory,
        # Key and tempo, measured from the audio itself and never asked of a
        # catalogue. Absent when this machine has no ffmpeg, exactly like the
        # spectrograms: the screen says so rather than offering a gesture that
        # would quietly do nothing.
        harmonic_analyzer=_create_harmonic_analyzer(logger, ffmpeg),
        # Whether audio can be measured at all, which is one fact about this
        # machine and not one per screen. Quality and Mixing both ask it, and
        # neither works it out from whichever object it happens to hold.
        ffmpeg=ffmpeg is not None,
        # Where the window fetches a cover's bytes. Handed in by the interface,
        # which owns the server, because this layer never imports upward.
        images=images,
        # Composed with the transport alone and **without the JSON client**,
        # because that one writes every answer into `cache/metadata` and no
        # Spotify value may be stored.
        spotify=SpotifyPointer(
            UrllibTransport(),
            logger,
            config.metadata.request_timeout_seconds,
            EnvironmentCredentials(),
        ),
    )


def _create_artwork_service(
    config: ApplicationConfig, logger: logging.Logger, store: ArtworkStore
) -> ArtworkService:
    """Assemble the cover-art service over the Cover Art Archive.

    The archive states it has no rate limiting rules, so the limiter here is
    politeness while several images are in flight at once, not an obligation.
    The manifests are JSON and go through the shared cache; the images do not,
    because a cache would hold a second copy of bytes that are about to be
    written into the library anyway.
    """
    metadata_config = config.metadata
    http_client = JsonHttpClient(
        "coverartarchive",
        UrllibTransport(),
        JsonMetadataCache(metadata_config.cache_directory, metadata_config.cache_ttl_seconds),
        RateLimiter(config.artwork.parallel_downloads * 2),
        RetryPolicy(metadata_config.max_attempts, metadata_config.backoff_seconds),
        metadata_config.request_timeout_seconds,
        logger,
    )
    client = CoverArtArchiveClient(
        http_client,
        UrllibTransport(),
        RateLimiter(config.artwork.parallel_downloads * 2),
        RetryPolicy(metadata_config.max_attempts, metadata_config.backoff_seconds),
        EnvironmentCredentials(),
        metadata_config.musicbrainz.contact_environment_variable,
        metadata_config.request_timeout_seconds,
        logger,
    )
    return ArtworkService(
        client,
        config.artwork.staging_directory,
        ArtworkPolicy(
            enabled=config.artwork.enabled,
            include_back_cover=config.artwork.include_back_cover,
            file_pixels=config.artwork.file_pixels,
            embed_pixels=config.artwork.embed_pixels,
            parallel_downloads=config.artwork.parallel_downloads,
            release_group_fallback=config.artwork.release_group_fallback,
        ),
        logger,
        store,
    )


def _compose_providers(
    config: ApplicationConfig,
    slskd: SlskdConnector,
    logger: logging.Logger,
) -> tuple[ProviderManager, SoulseekProvider | None]:
    """Register the acquisition providers this build ships, as configured.

    The Soulseek provider is handed the connector through its contracts alone,
    which is what lets it be tested without slskd and replaced without touching
    it. Whether it is enabled is the configuration's decision, not this
    function's: an identifier absent from ``providers.enabled`` is registered
    and left disabled, so the window can still show that it exists and say why
    it is quiet.
    """
    enabled = SOULSEEK_PROVIDER_ID in config.providers.enabled
    soulseek = SoulseekProvider(
        slskd,
        slskd,
        logger,
        enabled=enabled,
        priority=_configured_priority(config, SOULSEEK_PROVIDER_ID),
    )
    registry = ProviderRegistry()
    registry.register(soulseek)
    return ProviderManager(registry, logger), (soulseek if enabled else None)


def _configured_priority(config: ApplicationConfig, identifier: ProviderId) -> int:
    """Return where this provider sits in the configured order of preference."""
    order = config.providers.priority
    return order.index(identifier) if identifier in order else 10


def _create_acoustic_identification(
    config: ApplicationConfig,
    logger: logging.Logger,
    metadata: MetadataService,
    store: object,
) -> AcousticIdentification | None:
    """Compose the acoustic path, or nothing when the machine cannot run it.

    Two things have to be true and neither is this project's to install: fpcalc
    on disk, and the user's own AcoustID key in the environment. Absent either,
    identification runs without it — acoustic identification never blocks
    organizing, and a feature that is off should be off rather than failing per
    album.
    """
    executable = find_fpcalc()
    if executable is None:
        logger.info(
            "Acoustic identification is off: fpcalc is not installed.",
            extra={"operation": "acoustic.unavailable"},
        )
        return None
    metadata_config = config.metadata
    http = JsonHttpClient(
        "acoustid",
        UrllibTransport(),
        JsonMetadataCache(metadata_config.cache_directory, metadata_config.cache_ttl_seconds),
        RateLimiter(ACOUSTID_PER_SECOND),
        RetryPolicy(metadata_config.max_attempts, metadata_config.backoff_seconds),
        metadata_config.request_timeout_seconds,
        logger,
    )
    client = AcoustIdClient(http, EnvironmentCredentials(), logger)
    if not client.is_configured:
        logger.info(
            "Acoustic identification is off: no key is configured.",
            extra={"operation": "acoustic.unconfigured"},
        )
        return None
    return AcousticIdentification(
        ChromaprintFingerprinter(SubprocessFingerprintRunner(executable), logger),
        client,
        metadata,
        logger,
        store=store,  # type: ignore[arg-type]
    )


def _create_metadata_service(config: ApplicationConfig, logger: logging.Logger) -> MetadataService:
    metadata_config = config.metadata
    cache = JsonMetadataCache(metadata_config.cache_directory, metadata_config.cache_ttl_seconds)
    retry_policy = RetryPolicy(metadata_config.max_attempts, metadata_config.backoff_seconds)
    credentials = EnvironmentCredentials()
    discogs_http = JsonHttpClient(
        str(MetadataSources.DISCOGS),
        UrllibTransport(),
        cache,
        RateLimiter(metadata_config.discogs.requests_per_minute / 60),
        retry_policy,
        metadata_config.request_timeout_seconds,
        logger,
    )
    musicbrainz_http = JsonHttpClient(
        str(MetadataSources.MUSICBRAINZ),
        UrllibTransport(),
        cache,
        RateLimiter(metadata_config.musicbrainz.requests_per_second),
        retry_policy,
        metadata_config.request_timeout_seconds,
        logger,
    )
    # The sequence is the precedence order. Adding a source is one entry in it.
    return MetadataService(
        (
            (
                MetadataSources.DISCOGS,
                DiscogsClient(
                    discogs_http, credentials, metadata_config.discogs.token_environment_variable
                ),
            ),
            (
                MetadataSources.MUSICBRAINZ,
                MusicBrainzClient(
                    musicbrainz_http,
                    credentials,
                    metadata_config.musicbrainz.contact_environment_variable,
                    metadata_config.musicbrainz.access_token_environment_variable,
                ),
            ),
        )
    )


def _create_quality_survey(
    scanner: LibraryScanner,
    logger: logging.Logger,
    store: LibraryStore,
    executable: str | None,
) -> QualitySurvey | None:
    """Assemble the quality survey, or report its absence honestly.

    ffmpeg is an external binary rather than a Python dependency, so it can be
    missing on a machine that otherwise runs everything. When it is, the survey
    is ``None`` and the window says so, instead of measuring nothing and
    reporting every album as fine.

    The binary is handed in rather than looked for here: this is one of four
    places that wanted it, and four independent answers are four chances for the
    screen to be told something the objects do not agree with.
    """
    if executable is None:
        return None
    # The frame grid is part of the ordinary pass: it is the only measurement
    # that reaches a 320 kbps transcode stored as FLAC, and asking for it later
    # would mean a library whose answer depends on when each album was scanned.
    # It runs on lossless containers only, and costs a few seconds of one core
    # per such file.
    analyzer = FfmpegQualityAnalyzer(
        SubprocessCommandRunner(executable),
        logger,
        frame_grid=FrameGridProbe(FfmpegPcmReader(executable, logger=logger)),
    )
    return QualitySurvey(scanner, analyzer, logger, store=store)


def _create_deep_analyzer(
    logger: logging.Logger, executable: str | None
) -> FfmpegQualityAnalyzer | None:
    """Assemble the analyzer used when one album is asked about in depth.

    The same class as the survey's with two things changed: thirteen rungs
    reaching down to 14 kHz instead of five, and no limit on how much of the
    file is read. It costs about twenty-one ffmpeg passes per track, which is
    why it is never what a library-sized pass uses.

    A long timeout, because this one reads whole files: the survey's two minutes
    is generous for sixty seconds of audio and is not for a twenty-minute side.
    """
    if executable is None:
        return None
    return FfmpegQualityAnalyzer(
        SubprocessCommandRunner(executable, timeout_seconds=900.0),
        logger,
        frequencies=DEEP_PROBE_FREQUENCIES,
        seconds=None,
        # The same sweep the survey runs, over the same eight seconds: what
        # makes the deep pass deep is spectral resolution, and the grid does
        # not get sharper with more audio — it is already unambiguous, and a
        # whole-file sweep would cost minutes per track to say the same thing.
        frame_grid=FrameGridProbe(
            FfmpegPcmReader(executable, timeout_seconds=900.0, logger=logger)
        ),
    )


def _create_harmonic_analyzer(
    logger: logging.Logger, executable: str | None
) -> HarmonicAnalyzer | None:
    """Assemble the key and tempo analyzer, or report its absence.

    The same ffmpeg and the same rule as the spectrograms: without the binary
    nothing can be decoded, so there is no measurement to offer and
    the window is told rather than shown a gesture that does nothing.
    """
    if executable is None:
        return None
    return HarmonicAnalyzer(FfmpegAudioDecoder(executable), logger)


def _create_spectrogram_renderer(
    logger: logging.Logger, executable: str | None
) -> SpectrogramRenderer | None:
    """Assemble the spectrogram renderer, or report its absence.

    The same ffmpeg and the same rule as the survey: without the binary there
    is no picture, and the window says so rather than showing an empty frame.

    The cache lives in `~/.diglibrary`, outside the repository and outside any
    folder macOS protects with TCC, where a read can fail silently. It is
    discardable by design: deleting it costs the time to draw again and nothing
    else.
    """
    if executable is None:
        return None
    return SpectrogramRenderer(
        SubprocessCommandRunner(executable),
        SPECTROGRAM_CACHE,
        logger,
    )
