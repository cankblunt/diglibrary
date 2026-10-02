"""The net under a background thread that dies, and whether it holds."""

import logging
import threading

from diglibrary.ui.app import _report_thread_deaths


class _Recorder(logging.Handler):
    """Keep every record that reached a handler, and re-raise what logging swallows.

    `logging` reports a failure inside a handler by printing to stderr and
    carrying on, which is exactly the silence this whole mechanism exists to
    end. `raiseExceptions` is left on and the records are kept, so a report that
    cannot be written fails the test instead of disappearing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record."""
        self.records.append(record)


def test_a_thread_that_dies_is_actually_written_down() -> None:
    """The hook that reports a dead thread must itself be able to write.

    A background thread that raises leaves the progress bar on its last count
    forever, and an application launched from the Dock has no stderr anyone
    reads — so the hook is the only way the question "why did it stop?" has an
    answer.

    `logging` refuses to let an `extra` field overwrite a `LogRecord`
    attribute: a report passing `extra={"thread": ...}` raises
    `KeyError: "Attempt to overwrite 'thread' in LogRecord"` inside the hook,
    before anything is written. This drives the hook, so a report that cannot
    be written fails here.
    """
    logger = logging.getLogger("test.thread.deaths")
    recorder = _Recorder()
    logger.addHandler(recorder)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    was = threading.excepthook
    try:
        _report_thread_deaths(logger)
        hook = threading.excepthook
        try:
            raise ValueError("the worker gave up")
        except ValueError as error:
            hook(
                threading.ExceptHookArgs(
                    (type(error), error, error.__traceback__, threading.current_thread())
                )
            )
    finally:
        threading.excepthook = was
        logger.removeHandler(recorder)

    assert recorder.records, "a thread that died has to leave a record, or nothing does"
    written = recorder.records[0]
    assert written.operation == "thread.unhandled"  # type: ignore[attr-defined]
    assert written.exc_info is not None, "and it has to carry the traceback that explains it"
