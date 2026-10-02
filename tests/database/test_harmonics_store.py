"""Key and tempo survive the round trip, and never answer for another recording."""

import logging
from pathlib import Path

from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore, StoredHarmonics


def _store(tmp_path: Path) -> LibraryStore:
    logger = logging.getLogger("test")
    database = Database(tmp_path / "diglibrary.sqlite3", logger)
    database.initialize()
    return LibraryStore(database, logger)


def _measurement(audio_key: str, **overrides) -> StoredHarmonics:
    fields = {
        "audio_key": audio_key,
        "content_signature": "shared-shape",
        "tonic": "A",
        "mode": "minor",
        "key_confidence": 0.81,
        "bpm": 120.0,
        "bpm_confidence": 0.9,
        "bpm_alternative": None,
        "key_margin": 0.07,
        "runner_up_tonic": "C",
        "runner_up_mode": "major",
    }
    fields.update(overrides)
    return StoredHarmonics(**fields)


def test_what_was_measured_is_what_comes_back(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record_harmonics([_measurement("audio-1", bpm_alternative=60.0)])

    found = store.harmonics_for(["audio-1"])["audio-1"]
    assert found.key_name == "A minor"
    assert found.key_confidence == 0.81
    assert found.bpm == 120.0
    assert found.bpm_alternative == 60.0
    # Written by one statement and read by another: a column added to the
    # insert and not to the select comes back None for ever, and nothing raises.
    assert found.key_margin == 0.07
    assert (found.runner_up_tonic, found.runner_up_mode) == ("C", "major")


def test_measuring_again_carries_the_margin_with_the_key(tmp_path: Path) -> None:
    """The upsert states the column list a third time, after insert and select.

    A second measurement replacing the key and leaving the old margin beside it
    is a row describing two different readings — and it reads as working, since
    both columns hold a number.
    """
    store = _store(tmp_path)

    store.record_harmonics([_measurement("audio-1", key_margin=0.01)])
    store.record_harmonics(
        [_measurement("audio-1", tonic="C", mode="major", key_margin=0.22, runner_up_tonic="G")]
    )

    found = store.harmonics_for(["audio-1"])["audio-1"]
    assert found.key_name == "C major"
    assert found.key_margin == 0.22
    assert found.runner_up_tonic == "G"


def test_a_key_measured_before_the_margin_existed_keeps_answering(tmp_path: Path) -> None:
    """NULL is *measured before this column*, and such a row is not broken.

    It is the shape of `wall_low_hertz` and of `floor_hertz`: the reading stands,
    and only the screen's word about how decided it was is withheld.
    """
    store = _store(tmp_path)

    store.record_harmonics(
        [_measurement("audio-1", key_margin=None, runner_up_tonic=None, runner_up_mode=None)]
    )

    found = store.harmonics_for(["audio-1"])["audio-1"]
    assert found.key_name == "A minor"
    assert found.key_margin is None


def test_two_recordings_of_one_shape_keep_their_own_keys(tmp_path: Path) -> None:
    """A `content_signature` describes a shape, not a recording.

    Two different songs of the same exact length in the same format share one.
    Storing by signature would make one of these two answer for the other — a
    screen confidently wrong about a file nobody touched.
    """
    store = _store(tmp_path)

    store.record_harmonics(
        [
            _measurement("audio-1", tonic="A", mode="minor", bpm=92.0),
            _measurement("audio-2", tonic="F", mode="major", bpm=140.0),
        ]
    )

    found = store.harmonics_for(["audio-1", "audio-2"])
    assert found["audio-1"].key_name == "A minor" and found["audio-1"].bpm == 92.0
    assert found["audio-2"].key_name == "F major" and found["audio-2"].bpm == 140.0


def test_measuring_again_replaces_rather_than_multiplies(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record_harmonics([_measurement("audio-1", tonic="A", mode="minor")])
    store.record_harmonics([_measurement("audio-1", tonic="C", mode="major")])

    found = store.harmonics_for(["audio-1"])
    assert len(found) == 1
    assert found["audio-1"].key_name == "C major"


def test_half_an_answer_is_stored_as_half_an_answer(tmp_path: Path) -> None:
    """A track can have a readable key and no settled tempo. `None` and never a
    zero, because `0 BPM` on a screen states something the audio never said."""
    store = _store(tmp_path)

    store.record_harmonics([_measurement("audio-1", bpm=None, bpm_confidence=None)])

    found = store.harmonics_for(["audio-1"])["audio-1"]
    assert found.key_name == "A minor"
    assert found.bpm is None


def test_a_recording_never_measured_is_absent_and_not_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.harmonics_for(["never-measured"]) == {}


def test_asking_about_nothing_touches_no_database(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.harmonics_for([]) == {}
    store.record_harmonics([])
