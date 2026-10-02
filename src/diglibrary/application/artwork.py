"""Fetching the cover art an identified release should carry, and staging it."""

import itertools
import logging
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from diglibrary.library.artwork import (
    Artwork,
    ArtworkStore,
    ImageFacts,
    ImageKind,
    StagedImage,
    describe_bytes,
    extension_for,
)
from diglibrary.metadata.coverart import CoverArtArchiveClient, CoverArtEntry

DEFAULT_FILE_PIXELS = 1200
"""The size written into the album folder."""

DEFAULT_EMBED_PIXELS = 500
"""The size embedded in every track, which keeps a twenty-track album light.

Kept at 500 rather than 1200 for a reason beyond weight. Rips commonly carry
an embedded picture of around 600 pixels, so a 1200-pixel candidate would
replace the picture in most files, and each replacement leaves the small cover
the Finder draws pointing at the picture that *was* there (`library.xattrs`),
which nothing offers to redraw. At 500 the candidate is rarely larger than what
a file already carries, and the rule that an embedded picture is replaced only
by a larger one refuses the replacement.

The folder's own `cover.jpg` is unaffected and stays at `DEFAULT_FILE_PIXELS`,
which is where a large image is worth its bytes.
"""

DEFAULT_PARALLEL_DOWNLOADS = 4
"""How many images are fetched at once; the archive declares no rate limit."""

_THUMBNAIL_SIZES = (250, 500, 1200)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
NOT_THE_FRONT = ("back", "contracapa", "verso", "encarte", "booklet", "inside", "tray", "obi")
"""Words that say a picture in the folder is not the album's face.

The front goes by many names — `folder.jpg`, `albumartsmall.jpg`, `cover.jpg`,
`front.jpg` and others — so choosing by name would miss most of them. What is
reliably legible is the opposite: `back.jpg` and `cd.jpg` say what they are. So
the rule reads the exclusions and takes the largest of what is left, which also
settles `albumartsmall` against its `_large` twin without knowing either name.
"""


@dataclass(frozen=True, slots=True)
class ArtworkPolicy:
    """Purpose: hold the configured choices about which images to fetch and how big.

    Responsibilities: carry the switchable parts of the artwork rules. Boundaries: it
    performs nothing and reads nothing. Dependencies: none. Collaborators:
    ``ArtworkService`` and configuration loading. Constraints: a request larger
    than the archive's biggest thumbnail is served by the original scan, so
    raising ``file_pixels`` never fails — it only costs more bytes.
    """

    enabled: bool = True
    include_back_cover: bool = True
    file_pixels: int = DEFAULT_FILE_PIXELS
    embed_pixels: int = DEFAULT_EMBED_PIXELS
    parallel_downloads: int = DEFAULT_PARALLEL_DOWNLOADS
    release_group_fallback: bool = True


@dataclass(frozen=True, slots=True)
class _Request:
    """One image to fetch: which cover it is, at which address, for which use."""

    kind: ImageKind
    url: str
    is_embedded: bool


class ArtworkService:
    """Purpose: turn a resolved MusicBrainz release into images staged on disk.

    Responsibilities: ask the archive what exists, choose the size each use
    needs, download them, and stage the bytes under their own digest.
    Boundaries: it never writes into the user's library, and never decides
    whether an image is worth writing — that comparison is the planner's.
    Dependencies: the archive client only. Collaborators: the identification
    workflow and the change planner. Constraints: nothing here raises for a
    missing or unreachable picture, because art must never block organization;
    and staging is content-addressed, so two albums sharing a cover stage it
    once and a repeated run stages nothing new.
    """

    def __init__(
        self,
        client: CoverArtArchiveClient,
        staging_directory: Path,
        policy: ArtworkPolicy,
        logger: logging.Logger,
        store: ArtworkStore | None = None,
    ) -> None:
        """Create the service from an archive client, a store, and the configured policy."""
        self._client = client
        self._staging_directory = staging_directory
        self._policy = policy
        self._logger = logger
        self._store = store
        # Names the temporary files a staging write uses. `count` is atomic
        # under the GIL, which is the whole requirement: two threads must never
        # be handed the same number.
        self._stamps = itertools.count()

    @property
    def enabled(self) -> bool:
        """Report whether cover art is switched on at all.

        Asked by the caller so that *off* and *nothing was found* stop being the
        same empty answer: the planner repairs a picture block that misstates
        its image without any fetched image, and it must not do that when
        artwork has been turned off.
        """
        return bool(self._policy.enabled)

    def fetch(
        self,
        release_id: str | None = None,
        release_group_id: str | None = None,
        tracks: Sequence[Path] = (),
        folder: Path | None = None,
    ) -> Artwork:
        """Return the staged images for one release, or an empty result.

        ``tracks`` are the album's own files, whose embedded picture competes
        for the folder cover — which is why an album that nothing addresses in
        the archive is still worth asking about. ``folder`` is
        where the album lives, and carries the other direction of the same
        idea: a picture already sitting there is what the tracks get when the
        archive has nothing to give them.
        """
        if not self._policy.enabled:
            return Artwork()
        artwork = self._with_local_front(self._from_archive(release_id, release_group_id), tracks)
        return self._with_folder_embed(artwork, folder, tracks)

    def local_only(self, tracks: Sequence[Path], folder: Path | None) -> Artwork:
        """Return the artwork that comes from the disk alone, with no network.

        ``fetch`` is three steps: the archive, then the album's own picture
        offered as the folder cover, then a picture already in the folder
        offered to the tracks. Only the first needs the network. An album
        planned offline — the restore re-plans every album carrying a
        correction, and must not wait on the archive to open the window — still
        gets the two local steps through this method; otherwise an album whose
        files all carry a cover and whose folder holds no image would never be
        planned a `write_image`.
        """
        if not self._policy.enabled:
            return Artwork()
        local = self._with_local_front(Artwork(), tracks)
        return self._with_folder_embed(local, folder, tracks)

    def extract_local_front(self, tracks: Sequence[Path]) -> StagedImage | None:
        """Stage the largest picture embedded in these files, without any network.

        This is the extraction run for a coverless album that nothing has
        identified yet: the bytes are the user's own, and staging them is
        read-only — whether they are written anywhere is the planner's and the
        batch gesture's decision.
        """
        if not self._policy.enabled or not tracks or self._store is None:
            return None
        local = self._largest_embedded(tracks)
        if local is None:
            return None
        facts, track = local
        return self._stage_embedded(track, facts)

    def stage_chosen(self, source: Path) -> StagedImage | None:
        """Stage an image the user pointed at, or ``None`` when it is not one.

        Read the same way every other picture here is read — the bytes decide
        what it is, not the extension — so a file that is not an image,
        or one this application cannot describe, simply does not become a cover
        rather than becoming a broken one.
        """
        if not self.enabled:
            return None
        try:
            facts = self._store.describe(source)
        except Exception:
            self._logger.exception(
                "A chosen cover could not be read.",
                extra={"operation": "artwork.chosen.unreadable"},
            )
            return None
        if facts is None:
            return None
        return self._stage_file(source, facts)

    def _from_archive(self, release_id: str | None, release_group_id: str | None) -> Artwork:
        if not release_id:
            return Artwork()
        entries = self._entries(release_id, release_group_id)
        if not entries:
            self._logger.info(
                "No cover art is published for this release.",
                extra={"operation": "artwork.absent"},
            )
            return Artwork()
        staged = self._download(self._requests(entries))
        files = tuple(
            image for request, image in staged if image is not None and not request.is_embedded
        )
        embedded = next(
            (image for request, image in staged if image is not None and request.is_embedded),
            None,
        )
        return Artwork(files=files, embedded=embedded)

    def _with_local_front(self, artwork: Artwork, tracks: Sequence[Path]) -> Artwork:
        """Let the album's own embedded picture take the folder cover when it is larger.

        The comparison runs one way only: what goes *into* a track is decided
        by the planner's own embed comparison, never by this.
        """
        if not tracks or self._store is None:
            return artwork
        local = self._largest_embedded(tracks)
        if local is None:
            return artwork
        facts, track = local
        front = next((image for image in artwork.files if image.kind is ImageKind.FRONT), None)
        if front is not None and front.facts.pixels >= facts.pixels:
            return artwork
        staged = self._stage_embedded(track, facts)
        if staged is None:
            return artwork
        # Two different reasons, and the log names the one that applies. With
        # no front from the archive there was no comparison, and saying the
        # album's picture was larger would read as the archive having been
        # consulted. The restore plans offline, so that is the ordinary case.
        self._logger.info(
            (
                "The album's own picture is larger than the archive's cover."
                if front is not None
                else "No cover was fetched, so the album's own picture takes the folder cover."
            ),
            extra={
                "operation": "artwork.local_front",
                "compared": front is not None,
                "pixels": facts.pixels,
            },
        )
        others = tuple(image for image in artwork.files if image.kind is not ImageKind.FRONT)
        return replace(artwork, files=(staged, *others))

    def _with_folder_embed(
        self, artwork: Artwork, folder: Path | None, tracks: Sequence[Path] = ()
    ) -> Artwork:
        """Let a picture already in the folder be what the tracks get.

        The other direction of the local-picture rule: an album identified on
        a source the Cover Art Archive cannot be addressed by gets nothing from
        the archive, so a sleeve sitting in the folder is the only picture its
        files can be given.

        Nothing is invented and nothing is fetched: the bytes are already on
        the disk. It only ever fills an absence — an album the archive answered
        for is untouched, and so is one whose files already carry a picture.

        The second refusal has a reason: the archive is asked for a 500-pixel
        picture to embed because that keeps a twenty-track album light, while a
        sleeve in a folder is whatever size it is, often several times larger.
        There is no resizer here, so replacing good embedded covers with it
        would cost megabytes per album and gain nothing.
        """
        if artwork.embedded is not None or folder is None or self._store is None:
            return artwork
        if any(self._embedded_facts(track) is not None for track in tracks):
            return artwork
        chosen = self._front_in_folder(folder)
        if chosen is None:
            return artwork
        facts, path = chosen
        staged = self._stage_file(path, facts)
        if staged is None:
            return artwork
        self._logger.info(
            "The folder's own picture will be written into the files.",
            extra={"operation": "artwork.folder_embed"},
        )
        return replace(artwork, embedded=staged)

    def _embedded_facts(self, track: Path) -> ImageFacts | None:
        """Return what this file already carries, treating an unreadable one as empty."""
        assert self._store is not None
        try:
            return self._store.describe_embedded(track)
        except Exception:
            return None

    def front_in_folder(self, folder: Path) -> tuple[ImageFacts, Path] | None:
        """The album's face as it sits in its own folder, for anything that draws it.

        Public because the window's cards need exactly this answer. The first
        image in alphabetical order is `back.jpg` in every folder that has one,
        and the front goes by many names, so the *exclusions* are what read
        reliably — which is the rule below, and the only one.
        """
        return self._front_in_folder(folder)

    def _front_in_folder(self, folder: Path) -> tuple[ImageFacts, Path] | None:
        """Return the largest picture in this folder that does not say it is not the front."""
        assert self._store is not None
        best: tuple[ImageFacts, Path] | None = None
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return None
        for entry in entries:
            if entry.suffix.lower() not in IMAGE_SUFFIXES or not entry.is_file():
                continue
            name = entry.stem.casefold()
            if any(word in name for word in NOT_THE_FRONT) or name == "cd":
                continue
            try:
                facts = self._store.describe(entry)
            except Exception:
                continue
            if facts is not None and (best is None or facts.pixels > best[0].pixels):
                best = (facts, entry)
        return best

    def _stage_file(self, source: Path, facts: ImageFacts) -> StagedImage | None:
        """Copy a picture out of the album folder, so a rename cannot strand it.

        Images are written last, after every rename has run, so a source path
        inside the folder being renamed would be gone by the time it was read.
        """
        assert self._store is not None
        self._staging_directory.mkdir(parents=True, exist_ok=True)
        destination = self._staging_directory / f"{facts.digest}{extension_for(facts.media_type)}"
        try:
            if not destination.exists():
                self._store.copy(source, destination)
        except Exception:
            self._logger.warning(
                "A picture in the album folder could not be staged.",
                extra={"operation": "artwork.folder_embed.failure"},
            )
            return None
        return StagedImage(kind=ImageKind.FRONT, path=destination, facts=facts)

    def _largest_embedded(self, tracks: Sequence[Path]) -> tuple[ImageFacts, Path] | None:
        assert self._store is not None
        best: tuple[ImageFacts, Path] | None = None
        for track in tracks:
            try:
                facts = self._store.describe_embedded(track)
            except Exception:
                continue
            if facts is not None and (best is None or facts.pixels > best[0].pixels):
                best = (facts, track)
        return best

    def _stage_embedded(self, track: Path, facts: ImageFacts) -> StagedImage | None:
        assert self._store is not None
        self._staging_directory.mkdir(parents=True, exist_ok=True)
        destination = self._staging_directory / f"{facts.digest}{extension_for(facts.media_type)}"
        try:
            if not destination.exists():
                self._store.extract(track, destination)
        except Exception:
            self._logger.warning(
                "The album's own picture could not be staged.",
                extra={"operation": "artwork.local_front.failure"},
            )
            return None
        return StagedImage(kind=ImageKind.FRONT, path=destination, facts=facts)

    def _entries(self, release_id: str, release_group_id: str | None) -> tuple[CoverArtEntry, ...]:
        entries = self._safely(lambda: self._client.release_images(release_id))
        if entries or not self._policy.release_group_fallback or not release_group_id:
            return entries
        # The pressing in hand has no picture, but the album does: the release
        # group points at the edition MusicBrainz considers representative.
        return self._safely(lambda: self._client.release_group_images(release_group_id))

    def _requests(self, entries: tuple[CoverArtEntry, ...]) -> tuple[_Request, ...]:
        front = next((entry for entry in entries if entry.is_front), entries[0])
        requests = [
            _Request(ImageKind.FRONT, _address(front, self._policy.file_pixels), False),
            _Request(ImageKind.FRONT, _address(front, self._policy.embed_pixels), True),
        ]
        back = next((entry for entry in entries if entry.is_back), None)
        if self._policy.include_back_cover and back is not None:
            requests.append(
                _Request(ImageKind.BACK, _address(back, self._policy.file_pixels), False)
            )
        return tuple(requests)

    def _download(
        self, requests: tuple[_Request, ...]
    ) -> tuple[tuple[_Request, StagedImage | None], ...]:
        """Fetch every requested image, in parallel unless the policy says otherwise.

        One address is fetched once, however many uses want it. The folder's
        cover and the tracks' picture are two requests for two sizes, and when
        the policy sets both sizes to the same thumbnail they name one address,
        which would otherwise be downloaded twice for every album organized.
        """
        wanted = list(dict.fromkeys(request.url for request in requests))
        by_url = {url: _Request(ImageKind.FRONT, url, False) for url in wanted}
        if self._policy.parallel_downloads <= 1:
            staged = {url: self._stage(by_url[url]) for url in wanted}
        else:
            with ThreadPoolExecutor(max_workers=self._policy.parallel_downloads) as pool:
                fetched = pool.map(self._stage, [by_url[url] for url in wanted])
                staged = dict(zip(wanted, fetched, strict=True))
        return tuple(
            (request, _restaged(staged[request.url], request.kind)) for request in requests
        )

    def _stage(self, request: _Request) -> StagedImage | None:
        try:
            data = self._client.download(request.url)
        except Exception:
            self._logger.warning(
                "A cover image could not be downloaded.",
                extra={"operation": "artwork.download.failure"},
            )
            return None
        facts = describe_bytes(data)
        if facts is None:
            self._logger.warning(
                "A downloaded cover image was not a readable JPEG or PNG.",
                extra={"operation": "artwork.unreadable"},
            )
            return None
        try:
            path = self._write(data, facts.digest, facts.media_type)
        except OSError:
            # A cover is an adornment; an identification is the work. Staging
            # one must never be what stops an album from being identified.
            self._logger.warning(
                "A cover image could not be staged.",
                extra={"operation": "artwork.stage.failure", "digest": facts.digest},
            )
            return None
        return StagedImage(kind=request.kind, path=path, facts=facts)

    def _write(self, data: bytes, digest: str, media_type: str) -> Path:
        """Stage one image under its own digest, atomically."""
        self._staging_directory.mkdir(parents=True, exist_ok=True)
        destination = self._staging_directory / f"{digest}{extension_for(media_type)}"
        if destination.exists():
            return destination
        # The temporary name must be unique to this call, not to this process:
        # covers are fetched four at a time in one process, two of them are
        # often the same picture, and a name built from the pid alone has both
        # threads writing one file and the loser renaming what is already gone.
        # Deterministic on purpose — a counter and the thread, never a random
        # name.
        stamp = f"{os.getpid()}.{threading.get_ident()}.{next(self._stamps)}"
        temporary = destination.with_name(f"{destination.name}.{stamp}.tmp")
        temporary.write_bytes(data)
        try:
            os.replace(temporary, destination)
        except OSError:
            # Another thread staged the same digest first. Content-addressed
            # means their file and this one hold the same bytes, so the work is
            # done; only the leftover is ours to clear.
            temporary.unlink(missing_ok=True)
            if not destination.exists():
                raise
        return destination

    def _safely(
        self, operation: Callable[[], tuple[CoverArtEntry, ...]]
    ) -> tuple[CoverArtEntry, ...]:
        try:
            return operation()
        except Exception:
            self._logger.warning(
                "The cover art archive could not be consulted.",
                extra={"operation": "artwork.archive.failure"},
            )
            return ()


def _restaged(image: StagedImage | None, kind: ImageKind) -> StagedImage | None:
    """Return one staged image under the kind that asked for it.

    A download is addressed by URL and nothing else, so one fetch can answer
    both the folder's cover and the tracks' picture. What each use calls it is
    the caller's, not the downloader's.
    """
    if image is None or image.kind is kind:
        return image
    return replace(image, kind=kind)


def _address(entry: CoverArtEntry, requested_pixels: int) -> str:
    """Return the archive address that best serves a requested size.

    The archive renders thumbnails at fixed sizes, so the largest one that does
    not exceed the request is taken. A request beyond the largest thumbnail is
    answered by the original scan, and a request below the smallest still gets
    the smallest rather than nothing.
    """
    available = [size for size in _THUMBNAIL_SIZES if str(size) in entry.thumbnails]
    if not available or requested_pixels > max(available):
        return entry.url
    usable = [size for size in available if size <= requested_pixels] or [min(available)]
    return entry.thumbnails[str(max(usable))]
