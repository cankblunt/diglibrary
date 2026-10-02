"""Unit tests for choosing, downloading, and staging cover art."""

import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from diglibrary.application.artwork import ArtworkPolicy, ArtworkService
from diglibrary.library.artwork import FilesystemArtworkStore, ImageKind, describe_bytes
from diglibrary.metadata.coverart import CoverArtEntry

FRONT = CoverArtEntry(
    url="https://archive/front-original.jpg",
    is_front=True,
    thumbnails={
        "250": "https://archive/front-250.jpg",
        "500": "https://archive/front-500.jpg",
        "1200": "https://archive/front-1200.jpg",
    },
)
BACK = CoverArtEntry(
    url="https://archive/back-original.jpg",
    is_back=True,
    thumbnails={"500": "https://archive/back-500.jpg", "1200": "https://archive/back-1200.jpg"},
)


class FakeArchive:
    """Answer manifests from fixed entries and downloads from a URL map."""

    def __init__(
        self,
        release: tuple[CoverArtEntry, ...] = (),
        group: tuple[CoverArtEntry, ...] = (),
        images: dict[str, bytes] | None = None,
    ) -> None:
        self._release = release
        self._group = group
        self._images = images or {}
        self.downloaded: list[str] = []
        self.manifests: list[str] = []

    def release_images(self, release_id: str) -> tuple[CoverArtEntry, ...]:
        """Return the configured release manifest."""
        self.manifests.append(f"release:{release_id}")
        return self._release

    def release_group_images(self, group_id: str) -> tuple[CoverArtEntry, ...]:
        """Return the configured release-group manifest."""
        self.manifests.append(f"group:{group_id}")
        return self._group

    def download(self, url: str) -> bytes:
        """Return the configured bytes, or fail the way a network does."""
        self.downloaded.append(url)
        if url not in self._images:
            raise ConnectionError(f"no route to {url}")
        return self._images[url]


def test_the_file_gets_1200_and_the_embedded_picture_gets_500(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The size split between file and embedded picture keeps a many-track album light."""
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": jpeg(1200, 1200),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )

    artwork = _service(archive, tmp_path).fetch("mb1")

    assert [image.facts.width for image in artwork.files] == [1200]
    assert artwork.embedded is not None and artwork.embedded.facts.width == 500
    assert artwork.embedded.kind is ImageKind.FRONT


def test_a_request_above_the_largest_thumbnail_takes_the_original(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Asking for more than the archive renders is served by the original scan."""
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-original.jpg": jpeg(3000, 3000),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )

    artwork = _service(archive, tmp_path, file_pixels=3000).fetch("mb1")

    assert [image.facts.width for image in artwork.files] == [3000]


def test_the_back_cover_is_fetched_only_when_it_is_wanted(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Front and back are fetched by default, and the setting turns the back off."""
    images = {
        "https://archive/front-1200.jpg": jpeg(1200, 1200),
        "https://archive/front-500.jpg": jpeg(500, 500),
        "https://archive/back-1200.jpg": jpeg(1200, 1200),
    }

    with_back = _service(FakeArchive((FRONT, BACK), images=images), tmp_path).fetch("mb1")
    without = _service(
        FakeArchive((FRONT, BACK), images=images), tmp_path, include_back_cover=False
    ).fetch("mb1")

    assert sorted(image.kind for image in with_back.files) == [ImageKind.BACK, ImageKind.FRONT]
    assert [image.kind for image in without.files] == [ImageKind.FRONT]


def test_the_release_group_answers_when_the_pressing_has_no_art(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The fallback: an album whose only scanned sleeve belongs to another pressing."""
    archive = FakeArchive(
        release=(),
        group=(FRONT,),
        images={
            "https://archive/front-1200.jpg": jpeg(1200, 1200),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )

    artwork = _service(archive, tmp_path).fetch("mb1", "rg1")

    assert archive.manifests == ["release:mb1", "group:rg1"]
    assert len(artwork.files) == 1


def test_the_fallback_can_be_turned_off(tmp_path: Path) -> None:
    """Fidelity to the exact pressing is one setting away."""
    archive = FakeArchive(release=(), group=(FRONT,))

    artwork = _service(archive, tmp_path, release_group_fallback=False).fetch("mb1", "rg1")

    assert artwork.is_empty
    assert archive.manifests == ["release:mb1"]


def test_a_failed_download_leaves_the_rest_of_the_artwork_intact(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Art never blocks organization, so a broken image is simply absent."""
    archive = FakeArchive(
        release=(FRONT, BACK),
        images={
            "https://archive/front-1200.jpg": jpeg(1200, 1200),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )

    artwork = _service(archive, tmp_path).fetch("mb1")

    assert [image.kind for image in artwork.files] == [ImageKind.FRONT]
    assert artwork.embedded is not None


def test_bytes_that_are_not_a_readable_image_are_discarded(tmp_path: Path) -> None:
    """Nothing unmeasurable is staged, because it could not be compared or reverted."""
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": b"<html>error</html>",
            "https://archive/front-500.jpg": b"<html>error</html>",
        },
    )

    assert _service(archive, tmp_path).fetch("mb1").is_empty


def test_staging_is_content_addressed(tmp_path: Path, jpeg: Callable[[int, int], bytes]) -> None:
    """Two albums sharing a cover stage it once, and a repeated run stages nothing new."""
    same = jpeg(1200, 1200)
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": same,
            "https://archive/front-500.jpg": same,
        },
    )
    service = _service(archive, tmp_path)

    first = service.fetch("mb1")
    second = service.fetch("mb1")

    assert first.files[0].path == second.files[0].path
    assert first.embedded is not None
    assert first.embedded.path == first.files[0].path, "identical bytes stage to one file"
    assert len(list((tmp_path / "staging").iterdir())) == 1


def test_a_disabled_policy_never_touches_the_archive(tmp_path: Path) -> None:
    """The whole step is switchable."""
    archive = FakeArchive(release=(FRONT,))

    assert _service(archive, tmp_path, enabled=False).fetch("mb1").is_empty
    assert archive.manifests == []


def test_downloads_run_in_parallel_when_the_policy_allows_it(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The number of parallel downloads is a setting; the result must not depend on it."""
    images = {
        "https://archive/front-1200.jpg": jpeg(1200, 1200),
        "https://archive/front-500.jpg": jpeg(500, 500),
        "https://archive/back-1200.jpg": jpeg(1100, 1100),
    }
    serial = _service(FakeArchive((FRONT, BACK), images=images), tmp_path, parallel=1).fetch("mb1")
    parallel = _service(
        FakeArchive((FRONT, BACK), images=images), tmp_path / "other", parallel=4
    ).fetch("mb1")

    assert [image.kind for image in serial.files] == [image.kind for image in parallel.files]
    assert [image.facts.digest for image in serial.files] == [
        image.facts.digest for image in parallel.files
    ]


def test_the_albums_own_picture_takes_the_folder_cover_when_it_is_larger(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A picture already in the files wins when it is larger than the archive's."""
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": jpeg(358, 358),
            "https://archive/front-500.jpg": jpeg(358, 358),
        },
    )
    tracks = _tracks(tmp_path, jpeg(640, 640))

    artwork = _service(archive, tmp_path).fetch("mb1", tracks=tracks)

    front = next(image for image in artwork.files if image.kind is ImageKind.FRONT)
    assert front.facts.width == 640
    assert artwork.embedded is not None
    assert artwork.embedded.facts.width == 358, "what goes into a track is decided separately"


def test_the_archive_keeps_the_folder_cover_when_it_is_the_better_picture(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """The rule is more pixels, not a preference for one source over the other."""
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": jpeg(1200, 1200),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )
    tracks = _tracks(tmp_path, jpeg(640, 640))

    artwork = _service(archive, tmp_path).fetch("mb1", tracks=tracks)

    assert artwork.files[0].facts.width == 1200


def test_an_album_the_archive_cannot_answer_for_still_uses_its_own_picture(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Zero pixels lose to any picture, so a folder still gets its cover."""
    tracks = _tracks(tmp_path, jpeg(640, 640))

    artwork = _service(FakeArchive(), tmp_path).fetch(None, tracks=tracks)

    assert [image.facts.width for image in artwork.files] == [640]
    assert artwork.embedded is None


def test_tracks_without_a_picture_add_nothing(tmp_path: Path) -> None:
    """An album with no art anywhere is still organized without art."""
    tracks = _tracks(tmp_path, None)

    assert _service(FakeArchive(), tmp_path).fetch(None, tracks=tracks).is_empty


def _tracks(root: Path, picture: bytes | None) -> tuple[Path, ...]:
    """Copy two real FLAC files, optionally with a picture already embedded."""
    fixtures = Path(__file__).parent.parent / "fixtures" / "audio"
    root.mkdir(parents=True, exist_ok=True)
    tracks = []
    for index, name in enumerate(("tone.flac", "tone-long.flac")):
        track = root / f"{index}.flac"
        shutil.copy(fixtures / name, track)
        tracks.append(track)
    if picture is not None:
        image = root / "embedded.jpg"
        image.write_bytes(picture)
        for track in tracks:
            FilesystemArtworkStore().embed(track, image)
    return tuple(tracks)


def _service(
    archive: FakeArchive,
    root: Path,
    file_pixels: int = 1200,
    include_back_cover: bool = True,
    release_group_fallback: bool = True,
    enabled: bool = True,
    parallel: int = 1,
    embed_pixels: int = 500,
) -> ArtworkService:
    return ArtworkService(
        archive,  # type: ignore[arg-type]
        root / "staging",
        ArtworkPolicy(
            enabled=enabled,
            include_back_cover=include_back_cover,
            file_pixels=file_pixels,
            embed_pixels=embed_pixels,
            parallel_downloads=parallel,
            release_group_fallback=release_group_fallback,
        ),
        logging.getLogger("test.artwork"),
        FilesystemArtworkStore(),
    )


def test_one_address_is_downloaded_once_however_many_uses_want_it(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """When both uses want the same thumbnail, it is downloaded once.

    With the embedded size equal to the file size, the folder's cover and the
    tracks' picture share one address. Two requests for one URL would be two
    downloads of the same bytes from someone else's server, on every album
    organized.
    """
    archive = FakeArchive(
        release=(FRONT,),
        images={"https://archive/front-1200.jpg": jpeg(1200, 1200)},
    )

    artwork = _service(archive, tmp_path, embed_pixels=1200).fetch("mb1")

    assert archive.downloaded.count("https://archive/front-1200.jpg") == 1
    assert [image.facts.width for image in artwork.files] == [1200]
    assert artwork.embedded is not None and artwork.embedded.facts.width == 1200
    assert artwork.embedded.kind is ImageKind.FRONT, "each use keeps its own name"


def test_the_same_cover_staged_by_several_threads_at_once(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Staging the same picture from several threads at once must not raise.

    Covers are fetched in parallel in one process and two of them are often
    the same picture. A temporary file named from the process id alone is one
    path for both threads: the first rename takes it and the second raises
    `No such file or directory`, which would fail an identification over a
    picture.
    """
    service = _service(FakeArchive(), tmp_path)
    data = jpeg(1200, 1200)
    facts = describe_bytes(data)
    assert facts is not None
    staged: list[Path] = []
    failures: list[Exception] = []

    def stage() -> None:
        try:
            staged.append(service._write(data, facts.digest, facts.media_type))
        except Exception as error:  # pragma: no cover - the failure is the bug
            failures.append(error)

    threads = [threading.Thread(target=stage) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"staging one picture twice must not raise: {failures}"
    assert len({path for path in staged}) == 1, "content-addressed: one digest, one file"
    assert staged[0].read_bytes() == data
    leftovers = [path.name for path in (tmp_path / "staging").iterdir() if path.suffix == ".tmp"]
    assert not leftovers, f"no temporary file may survive: {leftovers}"


def test_a_picture_already_in_the_folder_is_what_the_files_get(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """A sleeve in the folder is embedded when the files carry no picture.

    The Cover Art Archive is addressed by a MusicBrainz release, so an album
    identified by another source gets nothing from it. A picture already in
    the album's folder fills that absence.
    """
    folder = tmp_path / "VA - Lantern Hours"
    folder.mkdir()
    (folder / "capa.jpg").write_bytes(jpeg(1000, 1000))
    # A back cover is in the folder too, and is larger. It says what it is.
    (folder / "contracapa.jpg").write_bytes(jpeg(1400, 1400))
    service = _service(FakeArchive(release=(), images={}), tmp_path)

    artwork = service.fetch(None, None, (), folder=folder)

    assert artwork.embedded is not None
    assert artwork.embedded.kind is ImageKind.FRONT
    assert artwork.embedded.facts.pixels == 1000 * 1000, "the back cover is not the front"
    # Staged out of the folder, because images are written after every rename
    # has run and a path inside the renamed folder would be gone by then.
    assert artwork.embedded.path.parent == tmp_path / "staging"
    assert (folder / "capa.jpg").is_file(), "the file in the folder is left where it is"


def test_an_album_the_archive_answered_for_keeps_the_archive_picture(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """It fills an absence and never competes: the fetched embed stands."""
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(jpeg(1400, 1400))
    archive = FakeArchive(
        release=(FRONT,),
        images={
            "https://archive/front-1200.jpg": jpeg(1200, 1200),
            "https://archive/front-500.jpg": jpeg(500, 500),
        },
    )

    artwork = _service(archive, tmp_path).fetch("mb1", None, (), folder=folder)

    assert artwork.embedded is not None
    assert artwork.embedded.facts.width == 500, "the archive's own embed size stands"


def test_a_folder_with_no_picture_at_all_changes_nothing(tmp_path: Path) -> None:
    """An absence on both sides is not something to fill."""
    folder = tmp_path / "Album"
    folder.mkdir()
    service = _service(FakeArchive(release=(), images={}), tmp_path)

    assert service.fetch(None, None, (), folder=folder).embedded is None


def test_files_that_already_carry_a_picture_are_left_alone(
    tmp_path: Path, jpeg: Callable[[int, int], bytes]
) -> None:
    """Filling an absence, never competing, because of size.

    The archive is asked for a small picture to embed, which keeps a
    many-track album light; a sleeve in a folder is whatever size it is. With
    no resizer here, replacing pictures the files already carry with a large
    sleeve costs megabytes per track and gains nothing.
    """
    folder = tmp_path / "Album"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(jpeg(2000, 2000))
    track = folder / "01.flac"
    shutil.copy(Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "tone.flac", track)
    seed = folder / "seed.jpg"
    seed.write_bytes(jpeg(500, 500))
    FilesystemArtworkStore().embed(track, seed)
    service = _service(FakeArchive(release=(), images={}), tmp_path)

    assert service.fetch(None, None, (track,), folder=folder).embedded is None
