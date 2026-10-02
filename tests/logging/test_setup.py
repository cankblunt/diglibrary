"""Tests for structured logging."""

import json
import logging.handlers
from pathlib import Path

from diglibrary.config.models import LoggingConfig
from diglibrary.logging.setup import configure_logging


def test_configure_logging_writes_required_structured_fields(tmp_path) -> None:
    """Each log record includes the required structured audit fields."""
    logger = configure_logging(LoggingConfig("INFO", tmp_path, "application.jsonl"))
    logger.info("Ready.", extra={"operation": "test.ready"})

    record = json.loads((tmp_path / "application.jsonl").read_text(encoding="utf-8"))

    assert record["level"] == "INFO"
    assert record["module"] == "diglibrary"
    assert record["operation"] == "test.ready"
    assert record["message"] == "Ready."
    assert record["timestamp"]


def test_a_logged_exception_carries_its_cause(tmp_path) -> None:
    """A failure logged without its cause is a failure nobody can diagnose.

    A formatter that drops exc_info records only that a source failed, and
    never why.
    """
    logger = configure_logging(LoggingConfig("INFO", tmp_path, "log.jsonl"))
    try:
        raise ValueError("the catalogue said no")
    except ValueError:
        logger.exception("A metadata source could not be searched.", extra={"operation": "x"})

    record = json.loads((tmp_path / "log.jsonl").read_text().strip().splitlines()[-1])

    assert record["error"] == "ValueError: the catalogue said no"
    assert "the catalogue said no" in record["traceback"]
    assert "Traceback" in record["traceback"]


def test_the_fields_a_caller_attaches_are_kept(tmp_path) -> None:
    """A refusal that logs its arithmetic and drops it cannot be audited.

    A formatter that keeps only the fields it knows by name records a refusal
    as `own=None foreign=None`.
    """
    logger = configure_logging(LoggingConfig("INFO", tmp_path, "log.jsonl"))

    logger.error(
        "Refusing to rename a folder that mostly holds other albums.",
        extra={
            "operation": "library.apply.container_refused",
            "own_tracks": 3,
            "foreign_tracks": 91,
        },
    )

    record = json.loads((tmp_path / "log.jsonl").read_text().strip().splitlines()[-1])

    assert record["own_tracks"] == 3
    assert record["foreign_tracks"] == 91
    assert record["operation"] == "library.apply.container_refused"


def test_the_log_is_bounded_and_keeps_what_came_before_the_failure(tmp_path: Path) -> None:
    """A plain FileHandler appends for the life of the installation.

    This application writes a record per album, per request and per measurement,
    so an unbounded file grows without limit. The ceiling is the live file plus
    three generations, so a machine can never be filled and the records just
    before a failure survive the roll.
    """
    from diglibrary.logging.setup import LOG_FILE_BYTES, LOG_FILE_GENERATIONS

    logger = configure_logging(LoggingConfig("INFO", tmp_path, "diglibrary.jsonl"))
    handler = logger.handlers[0]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == LOG_FILE_BYTES
    assert handler.backupCount == LOG_FILE_GENERATIONS

    # It actually rolls, and it actually stops: written past the ceiling, the
    # directory holds the live file and no more than `backupCount` behind it.
    handler.maxBytes = 2048
    for index in range(400):
        logger.info("filling the log", extra={"operation": "test.fill", "index": index})
    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == [
        "diglibrary.jsonl",
        "diglibrary.jsonl.1",
        "diglibrary.jsonl.2",
        "diglibrary.jsonl.3",
    ]


def test_a_credential_that_reaches_a_record_is_scrubbed_before_it_is_written(tmp_path) -> None:
    """The net under the discipline, not a replacement for it.

    Every call site today keeps URLs and headers out of the log on purpose —
    AcoustID's key rides in a query string and `metadata/acoustid.py` says in a
    comment why the URL is omitted. A later call site may forget, and
    `diglibrary.jsonl` is what gets pasted into bug reports.
    """
    logger = configure_logging(LoggingConfig("INFO", tmp_path, "application.jsonl"))
    secret = "abcd1234SECRETKEY"

    logger.info(
        "A lookup failed.",
        extra={
            "operation": "test.leak",
            "url": f"https://api.acoustid.org/v2/lookup?client={secret}&fingerprint=AQAA",
        },
    )
    try:
        raise RuntimeError(f"HTTP 401 for https://example.test/x?api_key={secret}")
    except RuntimeError:
        logger.exception("And its cause.", extra={"operation": "test.leak.cause"})

    written = (tmp_path / "application.jsonl").read_text(encoding="utf-8")

    assert secret not in written, "no credential reaches the file, in any field"
    assert "[redacted]" in written
    assert "fingerprint=AQAA" in written, "and the rest of the record survives to be read"


def test_a_field_whose_name_merely_ends_in_key_is_left_alone(tmp_path) -> None:
    """`audio_key` is the identity of a recording and appears all over this
    log — scrubbing it would hide which audio a measurement was taken from."""
    logger = configure_logging(LoggingConfig("INFO", tmp_path, "application.jsonl"))

    logger.info("Measured.", extra={"operation": "test.keep", "audio_key": "flac:1234:5678"})

    record = json.loads((tmp_path / "application.jsonl").read_text(encoding="utf-8"))
    assert record["audio_key"] == "flac:1234:5678"
