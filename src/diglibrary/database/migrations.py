"""Ordered, additive schema migrations applied to the local SQLite database."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    """Purpose: carry one numbered, forward-only schema change.

    Responsibilities: pair a version number with the statements that bring the
    schema to that version. Boundaries: it does not open a connection, decide
    what is already applied, or execute anything. Dependencies: none.
    Collaborators: ``MIGRATIONS`` and ``Database.initialize``. Constraints: a
    published migration is never edited — a correction is always a new version,
    because an existing user's database has already run the old one.
    """

    version: int
    name: str
    statements: tuple[str, ...]


_LIBRARY_FOUNDATION = (
    # The stub schema shipped before v0.1.0 declared five tables with no columns
    # beyond an id, and no released code ever inserted a row into them: the
    # database package contained repository protocols only. Dropping them is
    # provably lossless.
    #
    # Not every later migration is additive: 10 to 13 rebuild a table, 15 and
    # 16 delete rows, 23 runs an `UPDATE`. The rule is enforced by
    # `tests/database/test_migration_safety.py`: each migration that touches
    # existing data is declared there with why it is allowed, and a new one
    # fails until what is lost has been written down.
    "DROP TABLE IF EXISTS downloads",
    "DROP TABLE IF EXISTS tracks",
    "DROP TABLE IF EXISTS albums",
    "DROP TABLE IF EXISTS providers",
    "DROP TABLE IF EXISTS reports",
    # --- What exists on disk -------------------------------------------------
    # An album unit is a folder as found, not a release: it is what the user
    # has, before anything is known about what it should be.
    """
    CREATE TABLE album_units (
        id INTEGER PRIMARY KEY,
        folder_path TEXT NOT NULL UNIQUE,
        unit_signature TEXT NOT NULL,
        track_count INTEGER NOT NULL,
        total_duration_ms INTEGER,
        state TEXT NOT NULL DEFAULT 'discovered'
            CHECK (state IN ('discovered', 'identified', 'needs_review', 'organized', 'skipped')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX index_album_units_signature ON album_units(unit_signature)",
    "CREATE INDEX index_album_units_state ON album_units(state)",
    # The content signature, not the path, is what makes work incremental: it
    # survives renaming and manual tag edits.
    """
    CREATE TABLE audio_files (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        content_signature TEXT NOT NULL,
        file_size_bytes INTEGER NOT NULL,
        modified_at TEXT NOT NULL,
        album_unit_id INTEGER REFERENCES album_units(id) ON DELETE SET NULL,
        container_format TEXT,
        codec TEXT,
        duration_ms INTEGER,
        sample_rate INTEGER,
        channels INTEGER,
        bit_depth INTEGER,
        bitrate INTEGER,
        track_position INTEGER,
        disc_position INTEGER,
        first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX index_audio_files_content_signature ON audio_files(content_signature)",
    "CREATE INDEX index_audio_files_album_unit ON audio_files(album_unit_id)",
    # --- Acoustic identity ---------------------------------------------------
    """
    CREATE TABLE acoustic_fingerprints (
        id INTEGER PRIMARY KEY,
        audio_file_id INTEGER NOT NULL UNIQUE
            REFERENCES audio_files(id) ON DELETE CASCADE,
        algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        external_recording_id TEXT,
        computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # --- What trusted sources say --------------------------------------------
    # The full source payload is kept so a later matcher improvement can be
    # re-run without asking a rate-limited service the same question again.
    """
    CREATE TABLE metadata_releases (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_release_id TEXT NOT NULL,
        title TEXT NOT NULL,
        primary_artist TEXT,
        released_on TEXT,
        track_count INTEGER,
        payload TEXT NOT NULL,
        fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (source, source_release_id)
    )
    """,
    # --- Which release a unit is, and how sure we are ------------------------
    # Every write records the source, the release, and the confidence that
    # justified it. A rejected identification is kept, not deleted, so the
    # same wrong answer is not proposed again.
    """
    CREATE TABLE identifications (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        metadata_release_id INTEGER REFERENCES metadata_releases(id) ON DELETE SET NULL,
        method TEXT NOT NULL
            CHECK (method IN ('acoustic', 'existing_tags', 'folder_name', 'external_link',
                              'text_search', 'user')),
        confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
        explanation TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'proposed'
            CHECK (state IN ('proposed', 'accepted', 'rejected', 'superseded')),
        decided_by TEXT CHECK (decided_by IN ('automatic', 'user')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        decided_at TEXT
    )
    """,
    "CREATE INDEX index_identifications_album_unit ON identifications(album_unit_id)",
    "CREATE INDEX index_identifications_state ON identifications(state)",
    # --- Every change is planned first, and stays reversible ------------------
    """
    CREATE TABLE change_plans (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        identification_id INTEGER REFERENCES identifications(id) ON DELETE SET NULL,
        state TEXT NOT NULL DEFAULT 'simulated'
            CHECK (state IN ('simulated', 'applied', 'reverted', 'failed', 'discarded')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        applied_at TEXT,
        reverted_at TEXT
    )
    """,
    "CREATE INDEX index_change_plans_album_unit ON change_plans(album_unit_id)",
    """
    CREATE TABLE change_operations (
        id INTEGER PRIMARY KEY,
        change_plan_id INTEGER NOT NULL REFERENCES change_plans(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('rename_folder', 'rename_file', 'write_tags', 'write_image',
                            'embed_image')),
        target_path TEXT NOT NULL,
        before_state TEXT,
        after_state TEXT NOT NULL,
        backup_path TEXT,
        applied_at TEXT,
        reverted_at TEXT,
        UNIQUE (change_plan_id, sequence)
    )
    """,
    # --- Settings, the one table carried over from the stub schema ------------
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


_MANUAL_CORRECTIONS = (
    # The user outranks the catalogue, and the record says so. A
    # correction stores the value it replaced, so a review can show that a
    # string came from a person rather than from a fetch, and a later
    # re-identification does not silently discard it.
    """
    CREATE TABLE manual_corrections (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        field TEXT NOT NULL CHECK (field IN ('folder_name', 'track_title')),
        track_position INTEGER,
        value TEXT NOT NULL,
        replaced TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (album_unit_id, field, track_position)
    )
    """,
)


_INTEGRITY_CERTIFICATE = (
    # Every apply leaves proof. The audio fingerprints taken before and after,
    # and the read-back comparison of every write, are stored with the plan
    # they certify.
    "ALTER TABLE change_plans ADD COLUMN run_id TEXT",
    "CREATE INDEX index_change_plans_run ON change_plans(run_id)",
    # The comparison results a verification source produced — this
    # application's own verdicts, never the source's data:
    # {"itunes": {...}, "cross_source": {...}}.
    "ALTER TABLE identifications ADD COLUMN verification TEXT",
    """
    CREATE TABLE apply_proofs (
        id INTEGER PRIMARY KEY,
        change_plan_id INTEGER NOT NULL REFERENCES change_plans(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('audio', 'writes')),
        ok INTEGER NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX index_apply_proofs_plan ON apply_proofs(change_plan_id)",
)


_MEASURED_QUALITY = (
    # What the audio itself turned out to be. Keyed by content signature
    # rather than by path, because the signature survives a retag and a
    # rename: an album that was measured is still measured after the organizer
    # renames its folder, and two copies of one recording are measured once.
    """
    CREATE TABLE track_quality (
        id INTEGER PRIMARY KEY,
        content_signature TEXT NOT NULL UNIQUE,
        encoding TEXT NOT NULL,
        effective_bitrate_kbps INTEGER,
        cutoff_hertz INTEGER,
        steepest_drop_db REAL,
        findings TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        measured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX index_track_quality_signature ON track_quality(content_signature)",
)


_SPECTRAL_CEILING = (
    # Slope alone mistakes a master whose energy simply descends for a
    # transcode. The rule also asks what is left above the fall, so a
    # replayed verdict needs the same number the fresh one had.
    "ALTER TABLE track_quality ADD COLUMN decay_db REAL",
    "ALTER TABLE track_quality ADD COLUMN ceiling_db REAL",
)


_QUALITY_OVERRIDES = (
    # The user's word about what the audio is, above the measurement — the
    # same standing that word has above the catalogue. Kept per album, because
    # a verdict is about an album and the same recording can sit in two of
    # them; `content_signature` empty means the whole album.
    """
    CREATE TABLE quality_overrides (
        id INTEGER PRIMARY KEY,
        unit_signature TEXT NOT NULL,
        content_signature TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL CHECK (verdict IN ('honest', 'transcoded')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (unit_signature, content_signature)
    )
    """,
    "CREATE INDEX index_quality_overrides_unit ON quality_overrides(unit_signature)",
)


_DOWNLOADS = (
    # What was fetched from the network, so History answers "where did this come
    # from" as well as "what did this app rename". The folder is kept as the
    # source spelled it, because that is the only name that means anything
    # back on the network; where it landed on disk is asked of the acquisition
    # service, which is the one that knows.
    """
    CREATE TABLE downloads (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        directory TEXT NOT NULL DEFAULT '',
        folder TEXT NOT NULL DEFAULT '',
        files INTEGER NOT NULL DEFAULT 0,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        whole_folder INTEGER NOT NULL DEFAULT 1,
        requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX index_downloads_requested ON downloads(requested_at)",
)


_COLLECTED_DOWNLOADS = (
    # A downloaded album joins the library by itself once it has finished
    # arriving, and stays there. `collected_at` is when this app read the
    # folder off the disk; `cleared_at` is the user dismissing the row, which
    # takes it off the screen and never touches a file.
    "ALTER TABLE downloads ADD COLUMN collected_at TEXT",
    "ALTER TABLE downloads ADD COLUMN cleared_at TEXT",
    "ALTER TABLE downloads ADD COLUMN landed_path TEXT NOT NULL DEFAULT ''",
)


_PERSISTENT_LIBRARY = (
    # Every album the Library has been shown stays across sessions, whatever
    # brought it in. `origin` is what the second filter row reads and the only
    # thing that still tells the two apart once both persist — `scanned` or
    # `downloaded`. Existing rows default to `scanned`; the downloads among
    # them are re-marked on the next restore, which reads the download history
    # anyway.
    "ALTER TABLE album_units ADD COLUMN origin TEXT NOT NULL DEFAULT 'scanned'",
    # The user taking the album off the shelf. Cleared is not deleted: the
    # files, the disk and the History are untouched, and a scan reaching the
    # folder again lifts it.
    "ALTER TABLE album_units ADD COLUMN cleared_at TEXT",
    "CREATE INDEX index_album_units_cleared ON album_units(cleared_at)",
    # One sweep is owed, and only to a database that already had rows when the
    # Library began persisting. SQLite cannot stat a path, so the work happens
    # on the next restore; what the migration can say is *that* it is owed.
    # Folders recorded by earlier builds may be long gone — temporary
    # directories, deleted test folders. They did not vanish from a shelf
    # anybody was shown, and drawn as missing they would bury the albums that
    # exist. A database created after this migration has no such rows and
    # never gets this setting.
    """
    INSERT INTO settings (key, value)
    SELECT 'library.sweep_pending', 'yes'
    WHERE EXISTS (SELECT 1 FROM album_units)
    """,
)


_TRACK_ARTIST_CORRECTION = (
    # A corrected track credit is a correction like the other two, and the
    # column that names the field refuses it — `CHECK (field IN
    # ('folder_name', 'track_title'))`. The insert fails with an
    # IntegrityError: a value the code writes that the column does not allow.
    #
    # SQLite cannot widen a CHECK in place, so the table is rebuilt, and it is
    # written the long way on purpose. Every column is copied by name,
    # including `id` and `created_at`,
    # so a standing correction keeps its identity and its date; the UNIQUE
    # constraint comes back with the table. Migration 2 is not edited, because
    # an existing database has already run it: this is what a correction to a
    # published migration looks like.
    """
    CREATE TABLE manual_corrections_widened (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        field TEXT NOT NULL
            CHECK (field IN ('folder_name', 'track_title', 'track_artist')),
        track_position INTEGER,
        value TEXT NOT NULL,
        replaced TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (album_unit_id, field, track_position)
    )
    """,
    """
    INSERT INTO manual_corrections_widened
        (id, album_unit_id, field, track_position, value, replaced, created_at)
    SELECT id, album_unit_id, field, track_position, value, replaced, created_at
    FROM manual_corrections
    """,
    "DROP TABLE manual_corrections",
    "ALTER TABLE manual_corrections_widened RENAME TO manual_corrections",
)


_FINGERPRINT_BY_AUDIO = (
    # The fingerprint was bound to `audio_files(id)`, and files are written
    # with `ON CONFLICT (path)`. Organizing renames every file of an album,
    # which inserts a *new* row with a new id, so the fingerprint stays
    # attached to the old one — the most expensive operation in the project,
    # discarded by the project succeeding. Identity is the audio, and
    # `track_quality` keys by `content_signature` for exactly this reason: it
    # is what survives a rename.
    #
    # Rebuilt the long way, like migration 10, because a published migration is
    # never edited. Rows are carried across by joining the file they were
    # computed from; the same recording can sit in two folders and therefore in
    # two `audio_files` rows, so the copy tolerates the collision — one
    # fingerprint of the same bytes is the same fingerprint. No released code
    # wrote a row here before this migration, so the join usually carries
    # nothing; it is written for a database that does hold rows.
    """
    CREATE TABLE acoustic_fingerprints_by_audio (
        id INTEGER PRIMARY KEY,
        content_signature TEXT NOT NULL UNIQUE,
        algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        external_recording_id TEXT,
        computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    INSERT OR IGNORE INTO acoustic_fingerprints_by_audio
        (id, content_signature, algorithm, fingerprint, duration_seconds,
         external_recording_id, computed_at)
    SELECT print.id, file.content_signature, print.algorithm, print.fingerprint,
           print.duration_seconds, print.external_recording_id, print.computed_at
    FROM acoustic_fingerprints AS print
    JOIN audio_files AS file ON file.id = print.audio_file_id
    """,
    "DROP TABLE acoustic_fingerprints",
    "ALTER TABLE acoustic_fingerprints_by_audio RENAME TO acoustic_fingerprints",
    "CREATE INDEX index_acoustic_fingerprints_signature "
    "ON acoustic_fingerprints(content_signature)",
)


_MANUAL_PAIRING = (
    # A file paired to a track by hand is a correction like the other three,
    # and the `field` column does not allow it: the value the code writes
    # would be refused at insert.
    #
    # Two things are new. The value is the file's `content_signature`, never its
    # path: organizing renames every file of an album, and a pairing bound to a
    # name would be discarded by the app succeeding. Migration 11 answers the
    # same defect the same way.
    #
    # And a pairing is only meaningful against the release it was made on, since
    # `track_position` numbers *that* tracklist: adopting another release makes
    # position 4 a different song. So the row records which release it was made
    # for, and a pairing whose release is no longer the album's is suspended
    # rather than applied or deleted — the user is asked whether to reuse it.
    # The column is NULL for every other field, which have no such dependency,
    # and NULL for every row migration 10 left behind.
    #
    # Rebuilt the long way, like migration 10: SQLite cannot widen a CHECK in
    # place, every column is copied by name so a standing correction keeps its
    # identity and its date, and migration 2 is not edited because an existing
    # database has already run it.
    """
    CREATE TABLE manual_corrections_paired (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        field TEXT NOT NULL
            CHECK (field IN ('folder_name', 'track_title', 'track_artist', 'track_file')),
        track_position INTEGER,
        value TEXT NOT NULL,
        replaced TEXT,
        release_key TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (album_unit_id, field, track_position)
    )
    """,
    """
    INSERT INTO manual_corrections_paired
        (id, album_unit_id, field, track_position, value, replaced, release_key, created_at)
    SELECT id, album_unit_id, field, track_position, value, replaced, NULL, created_at
    FROM manual_corrections
    """,
    "DROP TABLE manual_corrections",
    "ALTER TABLE manual_corrections_paired RENAME TO manual_corrections",
)


_QUALITY_BY_AUDIO_KEY = (
    # One measurement per `content_signature` is one too few, because the
    # signature does not identify a recording. It is the declared shape of a
    # stream — codec, sample count, sample rate, channels, bit depth — which is
    # what makes it survive a rename and a retag and what makes two *different*
    # songs of the same exact length collide. In a large real library a
    # substantial share of the files share a signature with a provably
    # different song, so a file that measures lossless can show the verdict of
    # another recording whose wall is real.
    #
    # So the row is keyed by the signature AND the audio key, which answers the
    # question the signature cannot. Nothing else is re-keyed by this
    # migration: the acoustic fingerprints, the manual pairings and the user's
    # own verdicts are untouched.
    #
    # `audio_key` is NULL on every row this migration copies, and a NULL row
    # stays exactly as valid as it was: nothing sweeps a library, and a fresh
    # measurement happens only where one is asked for. A read prefers the row
    # whose key matches the file in hand and falls back to the NULL row, so the
    # stored measurements keep answering and a precise row wins for the one
    # file it was measured from.
    #
    # SQLite cannot drop a column-level UNIQUE in place, so the table is rebuilt
    # the long way, like migrations 10, 11 and 12: every column copied by name,
    # `id` and `measured_at` included, so a measurement keeps its identity and
    # its date. Migration 4 is not edited, because an existing database has
    # already run it.
    """
    CREATE TABLE track_quality_by_audio (
        id INTEGER PRIMARY KEY,
        content_signature TEXT NOT NULL,
        audio_key TEXT,
        encoding TEXT NOT NULL,
        effective_bitrate_kbps INTEGER,
        cutoff_hertz INTEGER,
        steepest_drop_db REAL,
        findings TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        decay_db REAL,
        ceiling_db REAL,
        measured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    INSERT INTO track_quality_by_audio
        (id, content_signature, audio_key, encoding, effective_bitrate_kbps,
         cutoff_hertz, steepest_drop_db, findings, reason, decay_db, ceiling_db,
         measured_at)
    SELECT id, content_signature, NULL, encoding, effective_bitrate_kbps,
           cutoff_hertz, steepest_drop_db, findings, reason, decay_db, ceiling_db,
           measured_at
    FROM track_quality
    """,
    "DROP TABLE track_quality",
    "ALTER TABLE track_quality_by_audio RENAME TO track_quality",
    # `IFNULL` rather than the two columns plainly, because in SQLite NULL is
    # never equal to NULL: a plain UNIQUE (content_signature, audio_key) would
    # let a signature collect any number of legacy rows, which is the one thing
    # the old constraint did guarantee.
    "CREATE UNIQUE INDEX index_track_quality_audio "
    "ON track_quality(content_signature, IFNULL(audio_key, ''))",
    "CREATE INDEX index_track_quality_signature ON track_quality(content_signature)",
)


_QUALITY_BENCH = (
    # The Quality screen is a bench folders are loaded onto. Deliberately not
    # the Library's registry: a whole library of thousands of albums can be
    # put here, and `album_units` is the shelf of albums being organized, which
    # those would bury.
    #
    # A root is a folder the user pointed at. Removing one takes the folders
    # off the bench and touches no measurement and no file.
    """
    CREATE TABLE quality_bench_roots (
        id INTEGER PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        walked_at TEXT
    )
    """,
    # What the walk found. The verdict is NOT here on purpose: a stored
    # verdict is never trusted across a rule change, so what is kept is the
    # album's membership and the verdict is re-judged from the numbers on
    # every read. That is also what lets the screen open without walking a
    # disk, which takes tens of seconds for a large library.
    """
    CREATE TABLE quality_bench_albums (
        id INTEGER PRIMARY KEY,
        root_id INTEGER NOT NULL REFERENCES quality_bench_roots(id) ON DELETE CASCADE,
        folder_path TEXT NOT NULL,
        unit_signature TEXT NOT NULL,
        track_count INTEGER NOT NULL DEFAULT 0,
        seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (root_id, folder_path)
    )
    """,
    "CREATE INDEX index_quality_bench_albums_root ON quality_bench_albums(root_id)",
    # One row per file, because an album's verdict is the median of its tracks
    # and a median needs to know which tracks. `audio_key` is remembered here
    # once something has paid to read it, so the same file is never opened twice
    # to ask the same question.
    """
    CREATE TABLE quality_bench_files (
        id INTEGER PRIMARY KEY,
        album_id INTEGER NOT NULL REFERENCES quality_bench_albums(id) ON DELETE CASCADE,
        file_name TEXT NOT NULL,
        content_signature TEXT NOT NULL,
        audio_key TEXT,
        UNIQUE (album_id, file_name)
    )
    """,
    "CREATE INDEX index_quality_bench_files_album ON quality_bench_files(album_id)",
    "CREATE INDEX index_quality_bench_files_signature " "ON quality_bench_files(content_signature)",
)


_VERDICT_PER_FILE: tuple[str, ...] = (
    # A verdict is about a file, because only a file can have been lossy before
    # it arrived. No control creates an album-wide verdict: the flag exists
    # per file only.
    #
    # The album-wide words already stored have to survive, and survive
    # *withdrawable*. Left as they are they would be unreachable: with an album
    # marked honest no file reads as marked, so the file's own flag would
    # compute `clear`, delete a row that is not there, and change nothing — a
    # control that answers a click with silence.
    #
    # Expanding is exact rather than approximate. `_with_overrides` reads an
    # album verdict as "every file of this album, then the per-file words on
    # top", so one row per file with the same verdict decides identically. A
    # file that already carries a word of its own keeps it: `OR IGNORE` leaves
    # it alone, and the per-file word is the later and more specific one anyway.
    """
    INSERT OR IGNORE INTO quality_overrides (unit_signature, content_signature, verdict)
    SELECT override.unit_signature, file.content_signature, override.verdict
    FROM quality_overrides AS override
    JOIN album_units AS album ON album.unit_signature = override.unit_signature
    JOIN audio_files AS file ON file.album_unit_id = album.id
    WHERE override.content_signature = ''
    """,
    # Only the ones that were actually expanded. An album whose files this app
    # has never registered has nothing to expand onto, and dropping its row
    # would be discarding something the user said — so it stays, and
    # `_with_overrides` still honours it.
    """
    DELETE FROM quality_overrides
    WHERE content_signature = ''
      AND EXISTS (
        SELECT 1 FROM album_units AS album
        JOIN audio_files AS file ON file.album_unit_id = album.id
        WHERE album.unit_signature = quality_overrides.unit_signature
      )
    """,
)


_DISCARD_MISFILED_QUALITY: tuple[str, ...] = (
    # Rows filed under a file *name* instead of a content signature. An
    # earlier survey shared one analysis across albums holding the same
    # recording, then looked the signature up by the analysis's path — which
    # belongs to the other album — found nothing, and fell back to `path.name`.
    # The write side no longer does that; these are the rows it left.
    #
    # No such row is ever read: none joins `audio_files` or
    # `quality_bench_files`.
    #
    # The predicate is the extension. The obvious rule — a content signature is
    # a 64-character digest, so anything else is rubbish — is not an invariant
    # this schema enforces, and it is wrong about real data: a file name can
    # be exactly 64 characters long. A name ends in the extension of a file
    # this application reads; a digest cannot.
    #
    # Nothing is lost. Each duplicates a measurement that the album which
    # decoded the audio *first* had already written under the correct signature,
    # in the same run.
    """
    DELETE FROM track_quality
    WHERE lower(content_signature) LIKE '%.flac'
       OR lower(content_signature) LIKE '%.wav'
       OR lower(content_signature) LIKE '%.aiff'
       OR lower(content_signature) LIKE '%.aif'
       OR lower(content_signature) LIKE '%.m4a'
       OR lower(content_signature) LIKE '%.mp3'
    """,
)


_ACOUSTIC_ANSWERS: tuple[str, ...] = (
    # What the service answered. The fingerprint is kept — it is the expensive
    # part — and without these columns everything learned from it is discarded
    # the moment it has served the identification: the recording it named, how
    # sure the service was, and whether it answered at all.
    #
    # `external_recording_id` is declared in migration 1 and nothing wrote it
    # before this migration, so a NULL there proves nothing about what the
    # service said.
    #
    # `looked_up_at` is what tells "never asked" from "asked, and the service
    # does not know this record". For music outside the large catalogues the
    # second is the ordinary answer, and an absence that cannot be
    # distinguished from silence reads as an application that did not try.
    "ALTER TABLE acoustic_fingerprints ADD COLUMN recording_score REAL",
    "ALTER TABLE acoustic_fingerprints ADD COLUMN looked_up_at TEXT",
)


_WALL_BY_STEP: tuple[str, ...] = (
    # The rung below the wall, which is what a step-detected wall has and a
    # floor-detected one does not.
    #
    # This column is how the step rule applies to new measurements only. The
    # verdict is re-derived from stored numbers on every read, and
    # `stored_track_is_transcoded` deliberately asks the *current* rule of an
    # old row. Left alone, the step rule would re-judge a whole library the
    # moment it shipped, every row against a measurement never taken for it.
    #
    # So NULL means *measured without this column*, and a NULL row goes on
    # answering exactly as it did. The same shape as `audio_key` in
    # migration 13, for the same reason: a row must say which ruler measured it.
    # Additive, so nothing is rebuilt and nothing is lost.
    "ALTER TABLE track_quality ADD COLUMN wall_low_hertz INTEGER",
)


_PRINTS_BY_AUDIO_KEY: tuple[str, ...] = (
    # The collision migration 13 treats in `track_quality`, in the fingerprints.
    # `acoustic_fingerprints.content_signature` is UNIQUE, so one fingerprint —
    # and one MusicBrainz recording — answers for every file of that shape. The
    # signature is the declared shape of a stream and not a recording: two
    # different recordings of the same length, in the same format, carry one
    # signature, and the second file is handed the first one's identity with no
    # request spent to find out. Naming the audio costs a small fraction of
    # what computing a fingerprint afresh costs.
    #
    # A UNIQUE cannot be dropped in place and this project does not rebuild a
    # table to do it, so the fingerprints move to a table keyed by the audio, in
    # the exact shape migration 13 gave `track_quality`. `acoustic_fingerprints`
    # is left standing and stops being written: it is the record of what was
    # computed before the audio had a name.
    #
    # Every row copied keeps `audio_key` NULL, because nothing knows which of two
    # recordings it came from. A NULL row is never handed to a file that *can*
    # name its audio; it is *claimed* instead, the first time the same audio is
    # fingerprinted again and the fingerprint comes back identical. That is the
    # judge: the print itself says which file the row was always about, and the
    # lookup it paid a request for comes back with it. Nothing sweeps the
    # library — the repair happens on the albums that are worked on.
    """
    CREATE TABLE acoustic_prints (
        id INTEGER PRIMARY KEY,
        content_signature TEXT NOT NULL,
        audio_key TEXT,
        algorithm TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        duration_seconds INTEGER NOT NULL,
        external_recording_id TEXT,
        recording_score REAL,
        looked_up_at TEXT,
        computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # Spelled exactly as migration 13's, because that is how SQLite matches a
    # conflict target: a row that names its audio never overwrites the legacy
    # row of the same signature, and never overwrites another recording's.
    "CREATE UNIQUE INDEX index_acoustic_prints_audio "
    "ON acoustic_prints(content_signature, IFNULL(audio_key, ''))",
    "CREATE INDEX index_acoustic_prints_fingerprint ON acoustic_prints(fingerprint)",
    """
    INSERT INTO acoustic_prints
        (content_signature, audio_key, algorithm, fingerprint, duration_seconds,
         external_recording_id, recording_score, looked_up_at, computed_at)
    SELECT content_signature, NULL, algorithm, fingerprint, duration_seconds,
           external_recording_id, recording_score, looked_up_at, computed_at
    FROM acoustic_fingerprints
    """,
)


_WALKED_AUDIO_KEY: tuple[str, ...] = (
    # Every measurement names the audio it came from, from the scan on. Without
    # this column the key is written only where a file is *measured*
    # (`track_quality`) or where the bench asks for it, so the registry of
    # files itself has no way to say which audio a row is about.
    #
    # It is what a duplicate map is made of: a map drawn from the measured
    # files alone finds no repetition, and an empty answer reads exactly like
    # an engine that did not try.
    #
    # The cost: about 0.1 ms for a FLAC, which answers from the md5 it declares
    # of its own samples, and under 1 ms for everything else, which pays one
    # seek and 128 KB. The file is already open — the scan reads every header
    # to compute the content signature — so this is not a second pass over the
    # library.
    #
    # Additive, and NULL keeps its meaning from migration 13: this row cannot
    # name its audio, so nothing read from it is proof about a particular file.
    "ALTER TABLE audio_files ADD COLUMN audio_key TEXT",
    "CREATE INDEX index_audio_files_audio_key ON audio_files(audio_key)",
)


_DUPLICATE_DECISIONS: tuple[str, ...] = (
    # The user's word about two albums that hold some of the same audio, so the
    # map asks once and then stays quiet. The map itself moves nothing and
    # deletes nothing, and the screen says so: what to do with the files is
    # the user's decision, and this only stops the app asking again.
    #
    # Keyed by the two albums' content signatures rather than by their paths,
    # because organising renames folders and filing moves them, and a decision
    # that evaporated when the folders were tidied would be worse than none.
    # Ordered, so one pair is one row however the two are named.
    """
    CREATE TABLE duplicate_decisions (
        id INTEGER PRIMARY KEY,
        first_signature TEXT NOT NULL,
        second_signature TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK (verdict IN ('both', 'handled')),
        decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (first_signature, second_signature)
    )
    """,
)


_WORDS_BY_AUDIO_KEY: tuple[str, ...] = (
    # The last table keyed by the shape alone, and the one where being wrong
    # costs the most: it holds the user's word about the audio, which outranks
    # every measurement this application takes.
    #
    # `quality_overrides` is UNIQUE (unit_signature, content_signature), so one
    # album plus one signature is one word. Where two files of an album share a
    # signature that is exactly right when they are the same audio — a folder
    # holding one recording twice under two titles, byte for byte identical,
    # where marking one has to mark both. It is wrong when they are not: two
    # tracks of one album can share a signature with different audio, and a
    # word about one would land on the other. Both cases occur in real
    # libraries.
    #
    # A UNIQUE cannot be dropped in place and this project does not rebuild a
    # table to do it, so the words move to a table keyed by the audio as well,
    # in the shape migrations 13, 19 and 20 established. `quality_overrides` is
    # left standing and stops being written; every word copied keeps `audio_key`
    # NULL and goes on answering for its signature exactly as it does now.
    """
    CREATE TABLE quality_words (
        id INTEGER PRIMARY KEY,
        unit_signature TEXT NOT NULL,
        content_signature TEXT NOT NULL DEFAULT '',
        audio_key TEXT,
        verdict TEXT NOT NULL CHECK (verdict IN ('honest', 'transcoded')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX index_quality_words_audio ON quality_words"
    "(unit_signature, content_signature, IFNULL(audio_key, ''))",
    """
    INSERT INTO quality_words
        (unit_signature, content_signature, audio_key, verdict, created_at)
    SELECT unit_signature, content_signature, NULL, verdict, created_at
    FROM quality_overrides
    """,
)


_ALBUM_YEAR_IN_A_COLUMN: tuple[str, ...] = (
    # The year an album *is* from, beside the year this pressing came out.
    # `metadata_releases` has always kept `released_on` in a column and the
    # album's own year only inside the JSON payload — which was fine while the
    # only reader deserialized the whole release anyway.
    #
    # The Library draws itself from these columns, and that would put two
    # meanings of "year" on one screen: a card drawn from the column shows the
    # pressing (a reissue's year) and the same card, once its album has been
    # read back off the disk, shows the album's own — two different years
    # without anything having happened.
    #
    # Additive, and backfilled from the payloads already stored, so no release
    # has to be fetched again. NULL means the source published no original date,
    # which is what `released_on` alone already meant.
    "ALTER TABLE metadata_releases ADD COLUMN original_released_on TEXT",
    """
    UPDATE metadata_releases
       SET original_released_on = json_extract(payload, '$.original_released_on')
     WHERE original_released_on IS NULL
       AND json_valid(payload)
       AND json_extract(payload, '$.original_released_on') IS NOT NULL
    """,
)


_ALBUM_RATING: tuple[str, ...] = (
    # What the user thinks of the record, which is the one fact in this
    # database that no source can supply and no measurement can check.
    #
    # A column on `album_units` rather than a table of its own, because the row
    # is the right holder and it survives: `_upsert_unit` finds an album by its
    # folder first and by its audio second, so organizing it (which renames
    # the folder) keeps the row, and adding or removing files (which changes
    # the unit signature) keeps it too.
    #
    # NULL means unrated, and it is not zero: a record nobody has judged and a
    # record judged worthless are different facts, and only one of them
    # should draw anything on a card. The CHECK refuses anything else, because
    # a rating is only ever written from a control that offers five values.
    "ALTER TABLE album_units ADD COLUMN rating INTEGER "
    "CHECK (rating IS NULL OR rating BETWEEN 1 AND 5)",
)


_TRACK_HARMONICS = (
    # Key and tempo, measured from the audio. A new table rather than
    # columns on `track_quality`: that one answers "is this audio what it claims
    # to be", and this answers "what is it in" — two different questions, and
    # one of them can be absent while the other is not.
    #
    # **Keyed by `audio_key`, and NOT NULL.** `content_signature` identifies a
    # shape, and a substantial share of a real library shares one with a
    # provably different song: a key measured from one recording shown against
    # another is a screen that is confidently wrong. There is no legacy row
    # here to be gentle with — the table
    # is new — so the column that says *which audio* is required from the start,
    # and the signature is carried beside it only to find rows quickly.
    """
    CREATE TABLE IF NOT EXISTS track_harmonics (
        id INTEGER PRIMARY KEY,
        audio_key TEXT NOT NULL,
        content_signature TEXT NOT NULL,
        tonic TEXT,
        mode TEXT,
        key_confidence REAL,
        bpm REAL,
        bpm_confidence REAL,
        -- The other octave, when the audio does not settle between them. NULL
        -- is "not ambiguous", which is a different fact from "not measured" —
        -- that one is a NULL `bpm`.
        bpm_alternative REAL,
        measured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS index_track_harmonics_audio "
    "ON track_harmonics(audio_key)",
    "CREATE INDEX IF NOT EXISTS index_track_harmonics_signature "
    "ON track_harmonics(content_signature)",
)


# A download is tied to the album it became by what the album *is*, not by where
# it was standing. With `landed_path` as the only tie, the rename this
# application performs is followed, but an organised folder moved into the
# library by hand is not: no rename is recorded for that, and the History row
# stops showing the record it brought in — a screen that empties as the
# application is used correctly.
#
# The signature survives both, because it is derived from the audio's shape and
# never from the path (`library/signature.py`).
#
# **It is not unique, and that governs the whole design.** It describes a shape,
# and copies of the same record in several folders share one. So a signature
# that resolves to more than one album resolves to none, and the row stays as
# it is, rather than guessing which copy.
_DOWNLOAD_ANCHORED_BY_SIGNATURE = (
    # Empty means not anchored, which is every row until one is collected — and
    # every row already collected, until the backfill below reaches it.
    "ALTER TABLE downloads ADD COLUMN unit_signature TEXT NOT NULL DEFAULT ''",
    # The backfill, for downloads collected before this column existed. The only
    # thing linking those to an album is the folder's own name, because the
    # rename *was* followed while the folder stayed put — so `landed_path` holds
    # the organised name under the old parent, and the album holds the same name
    # under the new one.
    #
    # `replace(p, rtrim(p, replace(p, '/', '')), '')` is the leaf of a path in
    # SQLite, which has no such function: `replace(p, '/', '')` is the set of
    # characters that are not separators, `rtrim` with that set removes the leaf
    # and leaves the parent, and replacing the parent with nothing leaves the
    # leaf.
    #
    # `min(id)` with `HAVING count(*) = 1` is the exactly-one rule stated once:
    # the row exists only when a single album carries that name, so two albums
    # of the same name anchor nothing. A folder renamed by hand after it was
    # moved matches no name and stays blank, which is the honest answer for it.
    """
    UPDATE downloads
       SET unit_signature = COALESCE((
           SELECT min(units.unit_signature)
             FROM album_units AS units
            WHERE replace(units.folder_path,
                          rtrim(units.folder_path, replace(units.folder_path, '/', '')), '')
                = replace(downloads.landed_path,
                          rtrim(downloads.landed_path, replace(downloads.landed_path, '/', '')), '')
           HAVING count(*) = 1
       ), '')
     WHERE landed_path != '' AND collected_at IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS index_downloads_signature ON downloads(unit_signature)",
)


_CORRECTION_FIELDS_BY_COMPLEMENT = (
    # **A CHECK that enumerates the fields refuses every field added after
    # it.** Migrations 10 and 12 each widened the list by one member, and the
    # next kind of correction — a track added by the user — is refused at
    # insert exactly as those were.
    #
    # So the set is written as its complement: a correction's field is a
    # non-empty word, and which words exist is owned by the code that writes
    # them. The vocabulary is not a fact about the database — it is a fact
    # about the application, and a second copy of it here can only drift from
    # the first. Provider identifiers are handled the same way for the same
    # reason.
    #
    # What is *not* given up: the column still refuses NULL and refuses an empty
    # string, so a typo that produces nothing is still caught, and every other
    # constraint on the table is copied over unchanged.
    #
    # Rebuilt the long way, like migrations 10, 11, 12 and 13: SQLite cannot
    # drop a CHECK in place, every column is copied by name so a standing
    # correction keeps its identity and its date, and the earlier migrations are
    # not edited because an existing database has already run them.
    """
    CREATE TABLE manual_corrections_open (
        id INTEGER PRIMARY KEY,
        album_unit_id INTEGER NOT NULL REFERENCES album_units(id) ON DELETE CASCADE,
        field TEXT NOT NULL CHECK (length(trim(field)) > 0),
        track_position INTEGER,
        value TEXT NOT NULL,
        replaced TEXT,
        release_key TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (album_unit_id, field, track_position)
    )
    """,
    """
    INSERT INTO manual_corrections_open
        (id, album_unit_id, field, track_position, value, replaced, release_key, created_at)
    SELECT id, album_unit_id, field, track_position, value, replaced, release_key, created_at
    FROM manual_corrections
    """,
    "DROP TABLE manual_corrections",
    "ALTER TABLE manual_corrections_open RENAME TO manual_corrections",
)


_PLAYLISTS: tuple[str, ...] = (
    # A list the user built, which is neither a fact about an album nor a
    # measurement of one.
    #
    # **Keyed by `audio_key`, like everything that is a claim about one
    # recording.** `content_signature` identifies a shape that different songs
    # share, so a list keyed by it would quietly point at the wrong track; and
    # a path breaks the moment an album is filed away. The key survives both.
    #
    # The names are remembered beside the key, and they are **not** how a track
    # is found. They exist so a row whose file is gone can still say which track
    # it was: a list that answers a missing file with a blank line is a list that
    # cannot be repaired. When the file is there, what is on disk wins.
    """
    CREATE TABLE IF NOT EXISTS playlists (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # `place` is the order the user put them in, and it is the list's own
    # order — not the album's.
    """
    CREATE TABLE IF NOT EXISTS playlist_tracks (
        playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        place INTEGER NOT NULL,
        audio_key TEXT NOT NULL,
        remembered_track TEXT NOT NULL,
        remembered_artist TEXT NOT NULL DEFAULT '',
        remembered_album TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (playlist_id, place)
    )
    """,
    "CREATE INDEX IF NOT EXISTS playlist_tracks_by_audio ON playlist_tracks (audio_key)",
)


_PLAYLISTS_REMOVED: tuple[str, ...] = (
    # The player and the saved lists are not part of the application, and
    # nothing reads these two tables. Dropped rather than left behind: a
    # table no code reads is a list of recordings kept on
    # the user's disk for no purpose. The copy `take_copy` writes before the
    # schema moves still holds them.
    "DROP INDEX IF EXISTS playlist_tracks_by_audio",
    "DROP TABLE IF EXISTS playlist_tracks",
    "DROP TABLE IF EXISTS playlists",
)


_THE_FRAME_GRID_A_ROW_WAS_CAUGHT_BY: tuple[str, ...] = (
    # What the encoder's own framing said about this file, so that a row caught
    # by it stays caught when it is read again.
    #
    # Every stored row is re-judged from its numbers on every read, and a file
    # convicted by its frame grid has a spectrum that says lossless — that is
    # precisely why the measurement exists. A re-judgement with only the
    # spectral columns in front of it would absolve, on the next launch,
    # exactly what the scan had caught.
    #
    # Two columns rather than one: the rule has two halves — how far one
    # alignment of 576 stood above the rest, and whether all six readings named
    # it — and a single column could not tell a tall peak nobody corroborated
    # from a corroborated peak that is only noise.
    #
    # NULL means *measured without the frame grid*, and such a row keeps the
    # answer its own numbers give: the grid is part of every new measurement,
    # and no album already on the shelf is touched until it is measured again.
    "ALTER TABLE track_quality ADD COLUMN frame_grid_z REAL",
    "ALTER TABLE track_quality ADD COLUMN frame_grid_agrees INTEGER",
)


_FLOOR_FOR_THE_SECOND_PATH: tuple[str, ...] = (
    # Where the audio ends, kept so that BOTH ways of convicting can consult the
    # one ceiling this project has.
    #
    # `CUTOFF_TRUST_HZ` is 19 kHz precisely so that a record ending at 20 kHz —
    # where an honest converter's anti-aliasing filter lands — is never called
    # a fake. A ceiling that guards only the wall rule leaves the second path,
    # the fall-and-emptiness pair, convicting in exactly the band the first
    # path is calibrated to spare. Measured against the benchmark's masters
    # filtered at 20 kHz and never passed through an encoder: the wall path
    # convicts none of them and the unbounded second path convicts a
    # substantial share.
    #
    # NULL means *measured without this column*, and such a row keeps answering
    # from the two signs alone — the shape of `audio_key` in migration 13 and
    # of `wall_low_hertz` in 18, for the same reason: a row must say which
    # ruler measured it, and a floor cannot be inferred from a cutoff. What it
    # costs is that the reprieve reaches such an album only when that album is
    # measured again: on demand, never a sweep. Additive, so nothing is
    # rebuilt and nothing is lost.
    "ALTER TABLE track_quality ADD COLUMN floor_hertz INTEGER",
)


_KEY_MARGIN_AND_ITS_RUNNER_UP: tuple[str, ...] = (
    # How decided the key reading was, which the correlation does not say.
    # `measure_key` scores twenty-four keys; the distance from the best to the
    # second is what predicts whether the answer holds. Compared against a
    # reference key detector, agreement rises with the margin band by band,
    # while it does not rise in order with the correlation.
    #
    # NULL means *measured without this column*, and it is the only thing that
    # distinguishes a reading taken with the earlier key profile from one
    # taken with the current one. The screen says so rather than dressing an
    # old reading in a new word: there is one statement that writes a key
    # here, so a row with a key and no margin is old by construction and
    # cannot become old by accident.
    "ALTER TABLE track_harmonics ADD COLUMN key_margin REAL",
    # The key it beat. A close call is two different situations — with the
    # runner-up a neighbour on the wheel the reading usually still mixes, and
    # usually does not when it is elsewhere — and a DJ can act on the second
    # key where a number tells them only to distrust the first.
    "ALTER TABLE track_harmonics ADD COLUMN runner_up_tonic TEXT",
    "ALTER TABLE track_harmonics ADD COLUMN runner_up_mode TEXT",
)


_HOW_MANY_READINGS_NAMED_THE_ALIGNMENT: tuple[str, ...] = (
    # Migration 31 stores agreement as a flag, which answers *did all six
    # readings agree*. The rule asks *how many of the six named one alignment*,
    # and a flag cannot answer that: a row read back would be judged by a coarser
    # question than the one that measured it, which is the one thing a stored
    # measurement must never be.
    #
    # NULL means *measured while the question was still a flag*. Such a row is
    # judged by the flag it has, and gets the count when it is measured again —
    # the same posture migration 31 took towards the rows that came before it.
    "ALTER TABLE track_quality ADD COLUMN frame_grid_agreeing INTEGER",
)


_PHYSICAL_COLLECTION: tuple[str, ...] = (
    # Records on a shelf. Additive: no existing table or column is touched, so
    # the module can be taken out whole.
    #
    # A user's own locations and categories, each list starting empty: no list
    # of anybody's is shipped. Names are unique so a menu never offers two
    # entries that cannot be told apart.
    """
    CREATE TABLE IF NOT EXISTS collection_locations (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_categories (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # The link to a release is a column of its own, never a reuse of
    # `identifications`, which is about folders of audio. With no release, the
    # artist, album and year are the words a user typed. No column here may ever
    # describe money, which a test enforces.
    """
    CREATE TABLE IF NOT EXISTS physical_records (
        id INTEGER PRIMARY KEY,
        artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '',
        year INTEGER,
        release_row_id INTEGER REFERENCES metadata_releases(id),
        identified_by TEXT,
        identified_at TEXT,
        media_grade TEXT,
        sleeve_grade TEXT,
        location_id INTEGER REFERENCES collection_locations(id) ON DELETE SET NULL,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS physical_record_categories (
        record_id INTEGER NOT NULL REFERENCES physical_records(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES collection_categories(id) ON DELETE CASCADE,
        PRIMARY KEY (record_id, category_id)
    )
    """,
)


_WHAT_A_LIST_BRINGS: tuple[str, ...] = (
    # A list a user already keeps says more about a record than its artist and
    # title. These are that user's words about the pressing — never a
    # catalogue's, which stay in `metadata_releases` — and they are also what a
    # search for candidates asks by, since a label and a catalogue number name
    # one pressing on its sleeve.
    "ALTER TABLE physical_records ADD COLUMN label TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE physical_records ADD COLUMN catalog_number TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE physical_records ADD COLUMN edition TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE physical_records ADD COLUMN country TEXT NOT NULL DEFAULT ''",
    # When candidates were last asked for, and the catalogue's own words when it
    # refused. NULL for both is a record nobody has asked about.
    "ALTER TABLE physical_records ADD COLUMN candidates_asked_at TEXT",
    "ALTER TABLE physical_records ADD COLUMN candidates_error TEXT",
    # The pressings a search offered, kept until the user chooses one or says
    # none of them is theirs. Catalogue text only, as a search returns it; the
    # user's choice is what ties a record to a release, never this table.
    """
    CREATE TABLE IF NOT EXISTS physical_record_candidates (
        record_id INTEGER NOT NULL REFERENCES physical_records(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        source TEXT NOT NULL,
        release_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        artist TEXT NOT NULL DEFAULT '',
        year INTEGER,
        country TEXT NOT NULL DEFAULT '',
        formats TEXT NOT NULL DEFAULT '[]',
        labels TEXT NOT NULL DEFAULT '[]',
        catalog_numbers TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (record_id, position)
    )
    """,
)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="library_foundation", statements=_LIBRARY_FOUNDATION),
    Migration(version=2, name="manual_corrections", statements=_MANUAL_CORRECTIONS),
    Migration(version=3, name="integrity_certificate", statements=_INTEGRITY_CERTIFICATE),
    Migration(version=4, name="measured_quality", statements=_MEASURED_QUALITY),
    Migration(version=5, name="spectral_ceiling", statements=_SPECTRAL_CEILING),
    Migration(version=6, name="quality_overrides", statements=_QUALITY_OVERRIDES),
    Migration(version=7, name="downloads", statements=_DOWNLOADS),
    Migration(version=8, name="collected_downloads", statements=_COLLECTED_DOWNLOADS),
    Migration(version=9, name="persistent_library", statements=_PERSISTENT_LIBRARY),
    Migration(version=10, name="track_artist_correction", statements=_TRACK_ARTIST_CORRECTION),
    Migration(version=11, name="fingerprint_by_audio", statements=_FINGERPRINT_BY_AUDIO),
    Migration(version=12, name="manual_pairing", statements=_MANUAL_PAIRING),
    Migration(version=13, name="quality_by_audio_key", statements=_QUALITY_BY_AUDIO_KEY),
    Migration(version=14, name="quality_bench", statements=_QUALITY_BENCH),
    Migration(version=15, name="verdict_per_file", statements=_VERDICT_PER_FILE),
    Migration(
        version=16,
        name="discard_misfiled_quality",
        statements=_DISCARD_MISFILED_QUALITY,
    ),
    Migration(version=17, name="acoustic_answers", statements=_ACOUSTIC_ANSWERS),
    Migration(version=18, name="wall_by_step", statements=_WALL_BY_STEP),
    Migration(version=19, name="prints_by_audio_key", statements=_PRINTS_BY_AUDIO_KEY),
    Migration(version=20, name="walked_audio_key", statements=_WALKED_AUDIO_KEY),
    Migration(version=21, name="duplicate_decisions", statements=_DUPLICATE_DECISIONS),
    Migration(version=22, name="words_by_audio_key", statements=_WORDS_BY_AUDIO_KEY),
    Migration(version=23, name="album_year_in_a_column", statements=_ALBUM_YEAR_IN_A_COLUMN),
    Migration(version=24, name="track_harmonics", statements=_TRACK_HARMONICS),
    Migration(
        version=25,
        name="download_anchored_by_signature",
        statements=_DOWNLOAD_ANCHORED_BY_SIGNATURE,
    ),
    Migration(
        version=26,
        name="correction_fields_by_complement",
        statements=_CORRECTION_FIELDS_BY_COMPLEMENT,
    ),
    Migration(version=27, name="album_rating", statements=_ALBUM_RATING),
    Migration(version=28, name="playlists", statements=_PLAYLISTS),
    Migration(
        version=29,
        name="floor_for_the_second_path",
        statements=_FLOOR_FOR_THE_SECOND_PATH,
    ),
    Migration(
        version=30,
        name="key_margin_and_its_runner_up",
        statements=_KEY_MARGIN_AND_ITS_RUNNER_UP,
    ),
    Migration(
        version=31,
        name="the_frame_grid_a_row_was_caught_by",
        statements=_THE_FRAME_GRID_A_ROW_WAS_CAUGHT_BY,
    ),
    Migration(
        version=32,
        name="how_many_readings_named_the_alignment",
        statements=_HOW_MANY_READINGS_NAMED_THE_ALIGNMENT,
    ),
    Migration(version=33, name="physical_collection", statements=_PHYSICAL_COLLECTION),
    Migration(version=34, name="what_a_list_brings", statements=_WHAT_A_LIST_BRINGS),
    Migration(version=35, name="playlists_removed", statements=_PLAYLISTS_REMOVED),
)
