"""What a migration is allowed to do to a library that already exists.

A migration runs **by itself**, on a user's own database, the first time the
application is opened after the code changed. Nobody approves it, nothing on
screen mentions it, and it is the one path by which a contributor's change
reaches data that already exists. Not every migration can be additive: some
rebuild a table, some delete rows, some run an `UPDATE`.

So the rule is not "never"; it is **declared, or refused**. Every migration that
touches what is already there is named below with why it is allowed, and a new one
that does so fails this test until the reason is written down.

`take_copy` in `composition` is the other half — a compressed copy is written
before the schema moves, so a migration that turns out to be wrong is recoverable
rather than final.
"""

import re

from diglibrary.database.migrations import MIGRATIONS

_DESTRUCTIVE = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+COLUMN|DROP\s+INDEX|RENAME\s+TO|RENAME\s+COLUMN"
    r"|DELETE\s+FROM|TRUNCATE|UPDATE\s+)",
    re.IGNORECASE,
)
"""Statements that can change or remove something already on disk.

`ALTER TABLE ... ADD COLUMN` and `CREATE` are absent on purpose: they add, and
adding is what a migration is supposed to do.
"""

DECLARED: dict[int, str] = {
    1: (
        "The stub schema's five empty tables. No released code ever inserted a "
        "row into them, so dropping them is provably lossless."
    ),
    10: (
        "Widening a column by rebuilding the table — SQLite cannot widen in "
        "place. Rows are copied into the new table before the old one is "
        "dropped, and `test_widening_the_correction_field_keeps_the_corrections` "
        "holds that."
    ),
    11: (
        "Re-keying fingerprints onto the audio they came from, by rebuild. "
        "`test_rekeying_the_fingerprint_carries_it_onto_the_audio` holds it."
    ),
    12: (
        "Adding manual pairing to the corrections table, by rebuild. "
        "`test_widening_the_field_a_third_time_keeps_every_correction` holds it."
    ),
    26: (
        "Dropping the enumerated CHECK on `manual_corrections.field`, by "
        "rebuild — an enumerated list refuses a correction the code is "
        "written to record. Every row is copied by name first, `release_key` "
        "included, and `test_opening_the_field_keeps_every_correction_and_its_"
        "release` holds it. Nothing is lost: what goes is a second copy of a "
        "vocabulary the application owns."
    ),
    13: (
        "Re-keying measurements onto `audio_key`, by rebuild. "
        "`test_naming_the_audio_a_measurement_came_from_keeps_every_old_one` "
        "holds it."
    ),
    15: (
        "Deletes the per-album quality overrides, which became per-file: an "
        "album-wide verdict cannot be split into files it was never measured "
        "against, so keeping it would have applied one file's answer to a whole "
        "record."
    ),
    16: (
        "Deletes measurements filed against a `content_signature` shared by "
        "provably different recordings. "
        "They describe audio nobody can identify, and a measurement attached to "
        "the wrong recording is worse than no measurement."
    ),
    23: (
        "Fills a new `year` column from the release rows already stored. It "
        "writes only the column added by the same migration."
    ),
    25: (
        "Fills the new `unit_signature` column on downloads already collected, "
        "by matching the folder's own leaf name, and only when exactly one "
        "album carries it. It writes only the column added by the same "
        "migration, and writes `''` — unchanged — wherever the answer is not "
        "certain. `test_the_backfill_anchors_only_what_one_album_answers_for` "
        "holds both halves."
    ),
    35: (
        "Drops the saved playlists, because the player and the lists were taken "
        "out of the application and nothing reads them. Losing them is "
        "the decision, not a side effect; the copy written before the schema "
        "moves still holds them. `test_the_playlists_go_and_nothing_beside_them_"
        "does` holds that only those two tables leave."
    ),
}
"""Every migration allowed to touch data that already exists, and why.

Adding an entry here is the deliberate act. Nothing enforces that the reason is a
good one — a person does — but nothing lets one arrive unwritten either.
"""


def test_no_migration_touches_existing_data_without_saying_so() -> None:
    """A new destructive migration fails here until it is declared.

    Written as the complement: the test does not list what is forbidden, it lists
    what has been allowed, so a statement nobody has thought of yet is refused
    rather than missed. A migration is the worst place to miss one: it runs on
    a library before any window is drawn.
    """
    undeclared: dict[int, list[str]] = {}
    for migration in MIGRATIONS:
        if migration.version in DECLARED:
            continue
        hits = [
            " ".join(statement.split())[:80]
            for statement in migration.statements
            if _DESTRUCTIVE.search(statement)
        ]
        if hits:
            undeclared[migration.version] = hits

    assert undeclared == {}, (
        "These migrations change or remove data that already exists and are not "
        f"declared in DECLARED: {undeclared}. If that is intended, add an entry "
        "saying what is lost and why keeping it would be worse — and check that "
        "a copy is taken before the schema moves."
    )


def test_the_declared_list_does_not_outlive_what_it_describes() -> None:
    """An exception that no longer exists is a permission nobody revoked.

    The list is the interesting artefact here, so it has to keep being true: a
    version that was renumbered or a migration that was rewritten to be additive
    must drop out of it, or the next destructive statement in that slot inherits
    an approval written for something else.
    """
    versions = {migration.version for migration in MIGRATIONS}
    unknown = sorted(set(DECLARED) - versions)
    assert unknown == [], f"DECLARED names migrations that do not exist: {unknown}"

    by_version = {migration.version: migration for migration in MIGRATIONS}
    stale = sorted(
        version
        for version in DECLARED
        if not any(_DESTRUCTIVE.search(s) for s in by_version[version].statements)
    )
    assert stale == [], (
        f"These migrations are declared as touching existing data and no longer "
        f"do: {stale}. Remove them, so the permission does not outlive the reason."
    )
