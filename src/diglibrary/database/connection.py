"""SQLite connection ownership and versioned schema migration."""

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from diglibrary.database.migrations import MIGRATIONS, Migration

_BUSY_TIMEOUT_SECONDS = 30.0
"""How long a writer waits for another writer before giving up.

SQLite's own default is five seconds. A quality survey holding the write lock
while a scan tries to record a unit is normal here, not exceptional.
"""


def _ask_for_wal(connection: sqlite3.Connection) -> None:
    """Put this database in WAL, and never let the asking be what stops the app.

    Changing the journal mode needs a brief exclusive lock, and **this is the
    one statement the busy timeout does not cover**: it is refused immediately
    rather than waited on. Several threads opening one new database is enough
    for one of them to fail right here with `database is locked`, which for a
    person is a double click that makes the application refuse to start.

    Swallowed on purpose, and it costs nothing to swallow: the mode is a
    property of the *file*, not of this connection, so a refusal means some
    other connection is setting it or has already set it. WAL is how readers and
    writers stop blocking each other; it is not what makes a write correct, and
    the transactions above answer for that.
    """
    try:
        connection.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        return


class MigrationError(RuntimeError):
    """Purpose: signal that the database schema cannot be brought up to date.

    Responsibilities: report an unusable or unexpected schema state without
    leaving a half-applied migration behind. Boundaries: it does not repair,
    downgrade, or delete anything. Dependencies: built-in exception behavior
    only. Collaborators: ``Database.initialize`` and application startup.
    Constraints: the database is left exactly as it was when this is raised —
    schema included — because the whole upgrade runs inside one explicit
    transaction. It has to be explicit: `sqlite3` opens one for `INSERT` and
    not for `CREATE TABLE`, so without it a half-applied migration leaves its
    new tables on the disk with nothing recording them.
    """


class Database:
    """Purpose: own SQLite connections and keep the schema at the current version.

    Responsibilities: open connections with the project's pragmas, apply pending
    migrations in order inside a transaction each, and record what was applied.
    Boundaries: it holds no domain knowledge, runs no query on a caller's
    behalf, and never downgrades a schema. Dependencies: ``MIGRATIONS`` and the
    standard library. Collaborators: the composition root and future
    repositories. Constraints: a database written by a newer DigLibrary is
    refused rather than silently used, because this build cannot know what its
    tables mean.
    """

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        """Create a database service configured for one SQLite database path."""
        self._path = path
        self._logger = logger

    @property
    def path(self) -> Path:
        """Return the configured SQLite database path."""
        return self._path

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection for one block of work, and close it when the block ends.

        The block is one transaction: it commits when it returns and rolls back
        when it raises, which is what ``sqlite3``'s own context manager does.
        What that manager does *not* do is close the connection — it only ends
        the transaction — so without the explicit close every call leaves an
        open handle behind for the garbage collector to find. A scan of a large
        library makes tens of thousands of these calls.

        The busy timeout is the other half. Three threads write here — a scan,
        a search, and the quality survey — and SQLite admits one writer at a
        time. Without a timeout the loser raises "database is locked"
        immediately; with it, it waits its turn.

        **``write=True`` for any block that will write, because otherwise the
        read it decides on is not in the same transaction as the write.** In
        `sqlite3`'s legacy mode a `SELECT` runs in autocommit — `in_transaction`
        is still False after one — and the implicit `BEGIN` arrives only with
        the `INSERT`. Almost every write here reads first: `_upsert_unit` looks
        the folder up to choose between an insert and an update. So two threads
        both read *not there*, both insert, and the second gets `IntegrityError`
        on the unique index. The block rolls back, and for a scan that is a
        batch of units and files gone, surfacing as albums that never appear.

        `BEGIN IMMEDIATE` puts the read and the write in one transaction with
        the write lock already held, so the second thread waits its turn, reads
        again, finds the row and updates it. Both succeed.

        Not the default, because it would serialize the reads too — and the
        reads are the tens of thousands. `tests/architecture/test_write_blocks.py`
        is what keeps the two in step.
        """
        connection = sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_SECONDS)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            _ask_for_wal(connection)
            if write:
                # After the pragmas: `journal_mode` cannot be set inside a
                # transaction, and would silently do nothing if it were.
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    def schema_version(self) -> int:
        """Return the highest applied migration version, or zero for a new database."""
        with self.connect() as connection:
            self._create_version_table(connection)
            return self._current_version(connection)

    def pending_migrations(self) -> bool:
        """Whether opening this database would move its schema.

        Asked before `initialize`, so a copy can be taken while the state a
        migration is about to change still exists. A database that is not there
        yet has nothing worth copying, and says so.
        """
        if not self._path.is_file():
            return False
        with self.connect() as connection:
            self._create_version_table(connection)
            return self._current_version(connection) < MIGRATIONS[-1].version

    def initialize(self) -> int:
        """Apply every pending migration in order and return the resulting version.

        The whole upgrade is one transaction, begun ``IMMEDIATE``, and both
        halves of that matter.

        **One transaction**, because `sqlite3` in its legacy mode opens one
        only for `INSERT`/`UPDATE`/`DELETE`: a `CREATE TABLE` runs in
        autocommit and is on the disk the instant it returns. So a migration
        that creates a table and then fills it from an older one — migrations
        13, 19 and 22 all do — would leave the table behind when the fill
        fails, while `schema_migrations` records nothing. The next launch
        re-runs it, `CREATE TABLE` answers *table already exists*, and the
        application cannot start until someone repairs the file by hand. A
        table survives an implicit rollback; inside an explicit transaction it
        does not.

        **IMMEDIATE**, because the write lock is taken before the version is
        read. Two instances launched together — a double click — would
        otherwise both read the same version and both apply the next
        migration, and the loser raises on the duplicate row. With the lock
        held the second one waits, then reads a version that is already
        current and has nothing to do.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            self._create_version_table(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._current_version(connection)
                latest = MIGRATIONS[-1].version
                if current > latest:
                    raise MigrationError(
                        f"Database schema version {current} is newer than this build supports "
                        f"({latest}). Upgrade DigLibrary instead of downgrading the database."
                    )
                for migration in MIGRATIONS:
                    if migration.version > current:
                        self._apply(connection, migration)
                        current = migration.version
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._logger.info(
            "Database schema is up to date.",
            extra={"operation": "database.initialize", "database_path": str(self._path)},
        )
        return current

    def _apply(self, connection: sqlite3.Connection, migration: Migration) -> None:
        """Run one migration inside the transaction ``initialize`` already opened.

        No transaction of its own, deliberately. `sqlite3`'s connection context
        manager is not reentrant — a nested ``with connection:`` commits the
        outer one on the way out — so a block of its own here would end the
        very transaction that protects the run.
        """
        try:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        except sqlite3.Error as error:
            raise MigrationError(
                f"Migration {migration.version} ({migration.name}) failed and was rolled back."
            ) from error
        self._logger.info(
            "Applied schema migration.",
            extra={"operation": "database.migrate", "database_path": str(self._path)},
        )

    @staticmethod
    def _create_version_table(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """)

    @staticmethod
    def _current_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0
