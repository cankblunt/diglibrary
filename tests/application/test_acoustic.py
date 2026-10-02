"""Unit tests for identifying an album by its audio."""

import logging
from datetime import datetime
from pathlib import Path

from diglibrary.application.acoustic import AcousticIdentification
from diglibrary.application.contracts import MetadataSources, ReleaseMetadata
from diglibrary.library.fingerprint import AudioFingerprint
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.metadata.acoustid import AcousticMatch


class _Fingerprinter:
    """Return one fingerprint per path, and remember what it was asked for."""

    def __init__(self, refuse: set[str] | None = None) -> None:
        self.refuse = refuse or set()
        self.asked: list[str] = []

    def fingerprint(self, path: Path) -> AudioFingerprint | None:
        self.asked.append(Path(path).name)
        if Path(path).name in self.refuse:
            return None
        return AudioFingerprint(fingerprint=f"fp-{Path(path).name}", duration_seconds=200)


class _AcoustId:
    """Answer each fingerprint with the release groups configured for that file."""

    def __init__(self, groups: dict[str, tuple[str, ...]], configured: bool = True) -> None:
        self._groups = groups
        self._configured = configured
        self.lookups = 0

    @property
    def is_configured(self) -> bool:
        return self._configured

    def lookup(self, fingerprint: AudioFingerprint) -> tuple[AcousticMatch, ...]:
        self.lookups += 1
        answer = self._groups.get(fingerprint.fingerprint.removeprefix("fp-"))
        if answer is None:
            return ()
        return (AcousticMatch(score=0.98, recording_ids=("rec",), release_group_ids=answer),)


class _Metadata:
    """Return one pressing per browsed group, and count the browses."""

    def __init__(self) -> None:
        self.browsed: list[str] = []

    def releases_in_group(self, source: object, group_id: str) -> tuple[ReleaseMetadata, ...]:
        self.browsed.append(group_id)
        return (
            ReleaseMetadata(
                source=MetadataSources.MUSICBRAINZ,
                source_release_id=f"release-of-{group_id}",
                title="Third Lantern",
                artists=(),
                tracks=(),
            ),
        )


class _Store:
    """A fingerprint store keyed by the signature and the audio together.

    Keyed by the pair because a signature is a declared shape, and two
    recordings can share one.
    """

    def __init__(self) -> None:
        self.saved: dict[tuple[str, str | None], AudioFingerprint] = {}
        self.answers: dict[tuple[str, str | None], tuple[str | None, float | None]] = {}

    def fingerprint_for(
        self, content_signature: str, audio_key: str | None = None
    ) -> AudioFingerprint | None:
        return self.saved.get((content_signature, audio_key))

    def remember_fingerprint(
        self, content_signature: str, audio_key: str | None, value: AudioFingerprint
    ) -> None:
        self.saved[(content_signature, audio_key)] = value

    def remember_lookup(
        self,
        content_signature: str,
        audio_key: str | None,
        recording_id: str | None,
        score: float | None,
    ) -> None:
        self.answers[(content_signature, audio_key)] = (recording_id, score)


def _named_audio(path: Path) -> str:
    """Name the audio of a file that is not on disk, so the pair is the real pair."""
    return f"audio-{path.name}"


def _logger() -> logging.Logger:
    return logging.getLogger("test.acoustic")


def _unit(count: int = 9) -> AlbumUnit:
    files = tuple(
        AudioFileFacts(
            path=Path(f"/music/album/{index:02d}.flac"),
            content_signature=f"sig-{index:02d}",
            file_size_bytes=1,
            modified_at=datetime(2001, 2, 3),
        )
        for index in range(1, count + 1)
    )
    return AlbumUnit(folder_path=Path("/music/album"), unit_signature="unit", audio_files=files)


def _built(acoustid: _AcoustId, **kwargs: object) -> tuple[_Fingerprinter, _Metadata, object]:
    fingerprinter = kwargs.pop("fingerprinter", None) or _Fingerprinter()
    metadata = _Metadata()
    kwargs.setdefault("audio_key_of", _named_audio)
    identification = AcousticIdentification(
        fingerprinter,  # type: ignore[arg-type]
        acoustid,  # type: ignore[arg-type]
        metadata,  # type: ignore[arg-type]
        _logger(),
        **kwargs,  # type: ignore[arg-type]
    )
    return fingerprinter, metadata, identification


def test_the_group_the_sampled_tracks_agree_on_is_the_album() -> None:
    """One track names several groups; the group all sampled tracks share is the album.

    A single recording appears on many releases, so agreement between the
    sampled tracks is what selects one.
    """
    acoustid = _AcoustId(
        {
            "01.flac": ("wrong-a", "right"),
            "05.flac": ("right", "wrong-b"),
            "09.flac": ("right",),
        }
    )
    _, metadata, identification = _built(acoustid)

    releases = identification.releases_for(_unit())

    assert metadata.browsed == ["right"]
    assert [release.source_release_id for release in releases] == ["release-of-right"]


def test_the_sample_is_spread_across_the_album_and_costs_three_lookups() -> None:
    """A compilation's first tracks are the ones likeliest to be on other compilations."""
    acoustid = _AcoustId({name: ("right",) for name in ("01.flac", "05.flac", "09.flac")})
    fingerprinter, _, identification = _built(acoustid)

    identification.releases_for(_unit(9))

    assert fingerprinter.asked == ["01.flac", "05.flac", "09.flac"]
    assert acoustid.lookups == 3


def test_audio_that_disagrees_with_itself_names_no_album() -> None:
    """Three tracks naming three different records is what a mixed folder looks like.

    Browsing an arbitrary one of them would be the app guessing where its own
    evidence refused to.
    """
    acoustid = _AcoustId({"01.flac": ("one",), "05.flac": ("two",), "09.flac": ("three",)})
    _, metadata, identification = _built(acoustid)

    assert identification.releases_for(_unit()) == ()
    assert metadata.browsed == []


def test_a_single_track_that_answers_alone_is_still_an_answer() -> None:
    """An album of one or two files cannot corroborate, and is not thereby wrong."""
    acoustid = _AcoustId({"01.flac": ("only",)})
    _, metadata, identification = _built(acoustid)

    identification.releases_for(_unit(1))

    assert metadata.browsed == ["only"]


def test_a_file_that_cannot_be_fingerprinted_does_not_stop_the_others() -> None:
    """A damaged track is not an unidentifiable album."""
    acoustid = _AcoustId({"05.flac": ("right",), "09.flac": ("right",)})
    fingerprinter = _Fingerprinter(refuse={"01.flac"})
    _, metadata, identification = _built(acoustid, fingerprinter=fingerprinter)

    identification.releases_for(_unit())

    assert metadata.browsed == ["right"]


def test_without_a_key_nothing_is_fingerprinted_at_all() -> None:
    """The expensive half must not run when the answer cannot be asked for."""
    acoustid = _AcoustId({}, configured=False)
    fingerprinter, metadata, identification = _built(acoustid)

    assert identification.is_available is False
    assert identification.releases_for(_unit()) == ()
    assert fingerprinter.asked == []
    assert metadata.browsed == []


def test_a_known_fingerprint_is_not_computed_twice() -> None:
    """Decoding audio is the expensive operation; the store exists to pay once."""
    acoustid = _AcoustId({name: ("right",) for name in ("01.flac", "05.flac", "09.flac")})
    store = _Store()
    fingerprinter, _, identification = _built(acoustid, store=store)

    identification.releases_for(_unit())
    first_pass = list(fingerprinter.asked)
    identification.releases_for(_unit())

    assert first_pass == ["01.flac", "05.flac", "09.flac"]
    assert fingerprinter.asked == first_pass
    # Keyed by the audio, so the same recording under a new name is free,
    # which is what organizing produces.
    assert sorted(store.saved) == [
        ("sig-01", "audio-01.flac"),
        ("sig-05", "audio-05.flac"),
        ("sig-09", "audio-09.flac"),
    ]


def test_only_the_best_groups_are_browsed_because_each_one_is_a_request() -> None:
    """A track commonly names many groups; browsing every one is a request each."""
    shared = tuple(f"group-{index}" for index in range(6))
    acoustid = _AcoustId({name: shared for name in ("01.flac", "05.flac", "09.flac")})
    _, metadata, identification = _built(acoustid, group_limit=2)

    identification.releases_for(_unit())

    assert len(metadata.browsed) == 2


def test_being_asked_and_knowing_nothing_is_said_out_loud(caplog) -> None:
    """Silence from the service is a state, not the absence of one.

    A service can answer every sampled track with nothing, or hold a
    fingerprint with no recording behind it. Without a log record, that run is
    indistinguishable from one that never asked.
    """
    acoustid = _AcoustId({})
    _, metadata, identification = _built(acoustid)

    with caplog.at_level(logging.INFO, logger="test.acoustic"):
        assert identification.releases_for(_unit()) == ()

    assert metadata.browsed == []
    assert any(record.__dict__.get("operation") == "acoustic.unknown" for record in caplog.records)


def test_what_the_service_answered_is_written_down_either_way() -> None:
    """The answer to a lookup is stored beside the fingerprint it was drawn from.

    A lookup costs a request against a rate-limited service, so its answer is
    worth as much as the fingerprint. Both outcomes are recorded: "asked, and
    the service does not know this recording" is a fact about the file, and
    without it no screen can tell it from "never asked".
    """
    store = _Store()
    unit = _unit(count=2)
    # Only the first file's fingerprint is one this service knows.
    acoustid = _AcoustId({"01.flac": ("group-1",)})
    identification = AcousticIdentification(
        _Fingerprinter(),  # type: ignore[arg-type]
        acoustid,  # type: ignore[arg-type]
        _Metadata(),  # type: ignore[arg-type]
        _logger(),
        store=store,  # type: ignore[arg-type]
        audio_key_of=_named_audio,
    )
    asked = [
        (file.content_signature, _named_audio(file.path))
        for file in identification._sample(unit.audio_files)
    ]

    identification.releases_for(unit)

    named = [signature for signature, answer in store.answers.items() if answer[0] is not None]
    blank = [signature for signature, answer in store.answers.items() if answer[0] is None]
    assert set(store.answers) == set(asked), "every file it put to the service is written down"
    assert named and store.answers[named[0]] == (
        "rec",
        0.98,
    ), "what it named, and how sure it was, has to survive the request that bought it"
    assert blank, (
        "and so must the answer that named nothing, or no screen can tell "
        "'never asked' from 'asked, and unknown'"
    )
