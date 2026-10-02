"""Standard-library structured logging configuration."""

import json
import logging
import logging.handlers
import re
from datetime import UTC, datetime

from diglibrary.config.models import LoggingConfig

_MEGABYTE = 1024 * 1024
LOG_FILE_BYTES = 8 * _MEGABYTE
LOG_FILE_GENERATIONS = 3
"""How much log is kept, at most: the live file plus three older ones, 32 MB.

A plain ``FileHandler`` appends for the life of the installation, and this
application writes a record per album, per request, per measurement, so the
file would grow without limit. A library scan is when the log matters most and
when it grows fastest, so the ceiling has to hold several runs rather than one:
eight megabytes is roughly a full survey of a library of a few thousand albums.

Rotating rather than truncating, because the interesting record is usually the
one just before the thing that went wrong.
"""

_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "operation",
    "message",
    "asctime",
    "taskName",
}
"""What a bare record already carries, so only the caller's own fields are added."""

_SECRET_NAMES = (
    "client_secret",
    "access_token",
    "refresh_token",
    "authorization",
    "api_key",
    "password",
    "apikey",
    "bearer",
    "secret",
    "token",
    "client",
    "auth",
    "key",
)
"""What a credential is called when it rides in a URL or a header.

Longest first, because the alternation is tried left to right and `api_key`
must be recognised as itself rather than as a `key` with something in front."""

_SECRET_IN_TEXT = re.compile(r"(?i)(?<![\w-])(" + "|".join(_SECRET_NAMES) + r")=([^&\s\"'\\]+)")
"""A credential riding in query-string or `name=value` form, wherever it appears.

Every call site keeps URLs and headers out of the log on purpose — this is the
net under that discipline, not a replacement for it. One call site that forgets
is enough, and this file is what gets pasted into bug reports.

The lookbehind is what keeps `audio_key=` readable: an underscore is a word
character, so a name with something in front of it is a *different* name and
only the ones spelled out above are treated as secrets. Redacting `audio_key`
would hide the field that says which audio a measurement was taken from."""


def _scrubbed(line: str) -> str:
    """Return this serialized record with any inline credential blanked."""
    return _SECRET_IN_TEXT.sub(r"\1=[redacted]", line)


class StructuredFormatter(logging.Formatter):
    """Render log records as newline-delimited JSON with required audit fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize one log record with timestamp, level, module, operation, and message.

        An exception is carried too. Without it ``logger.exception`` writes the
        sentence and discards the cause, and a logged failure says that
        something failed without saying what.

        The finished line is scrubbed of anything shaped like a credential,
        wherever in it that ended up — a field, a message, or a traceback.
        """
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "operation": getattr(record, "operation", "unspecified"),
            "message": record.getMessage(),
        }
        # Every other field the caller attached. A record whose message is
        # formatted from values it does not carry cannot be audited afterwards.
        payload.update(
            {
                name: value
                for name, value in record.__dict__.items()
                if name not in _STANDARD_FIELDS and not name.startswith("_")
            }
        )
        if record.exc_info and record.exc_info[0] is not None:
            error = record.exc_info[1]
            payload["error"] = f"{record.exc_info[0].__name__}: {error}"
            payload["traceback"] = self.formatException(record.exc_info)
        return _scrubbed(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def configure_logging(config: LoggingConfig) -> logging.Logger:
    """Configure and return the DigLibrary root logger.

    Reconfiguration replaces only handlers owned by the ``diglibrary`` logger,
    allowing tests and embedding applications to control other logging trees.
    """
    config.directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("diglibrary")
    logger.setLevel(config.level)
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        config.path,
        maxBytes=LOG_FILE_BYTES,
        backupCount=LOG_FILE_GENERATIONS,
        encoding="utf-8",
    )
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
    return logger
