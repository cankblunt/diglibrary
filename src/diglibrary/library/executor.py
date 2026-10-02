"""Applying a change plan to disk, and undoing it from the trail it leaves."""

import errno
import logging
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from diglibrary.library.artwork import ArtworkStore, ImageFacts, extension_for
from diglibrary.library.audio import AUDIO_EXTENSIONS
from diglibrary.library.containers import count_audio_below, names_the_folder
from diglibrary.library.planner import ChangeOperation, ChangePlan, OperationKind
from diglibrary.library.tags import TagStore
from diglibrary.library.xattrs import carry_across, movable_names

_IMAGE_KINDS = frozenset({OperationKind.WRITE_IMAGE, OperationKind.EMBED_IMAGE})


class ExecutionState(StrEnum):
    """The outcome of applying or reverting one plan."""

    APPLIED = "applied"
    FAILED = "failed"
    REVERTED = "reverted"


class ExecutionError(RuntimeError):
    """Purpose: report that a plan could not be applied or reverted.

    Responsibilities: name what went wrong without hiding what already changed.
    Boundaries: it never rolls anything back on its own — the caller decides,
    from the recorded trail, whether to revert. Dependencies: built-in exception
    behavior. Collaborators: ``ChangeExecutor``. Constraints: it is raised only
    for a refusal before any write; a failure partway through is reported as a
    result, because the trail matters more than the exception.
    """


class StalePlanError(ExecutionError):
    """Purpose: report that a plan describes a folder the disk no longer has.

    Responsibilities: separate "this plan is out of date" from every other
    refusal, so the window can say so and offer the one thing that fixes it —
    a fresh scan. Boundaries: it carries no remedy of its own and never
    re-plans. Dependencies: none. Collaborators: ``ChangeExecutor`` and
    ``LibraryApi``. Constraints: it is a subclass of ``ExecutionError`` so that
    every existing handler still catches it, and a more specific one may treat
    it differently.
    """


@dataclass(frozen=True, slots=True)
class AppliedOperation:
    """Purpose: record one operation that actually happened, with what it replaced.

    Responsibilities: pair the planned operation with the state captured at the
    moment of writing. Boundaries: it performs nothing. Dependencies:
    ``ChangeOperation``. Collaborators: ``ExecutionResult`` and reversal.
    Constraints: ``before_state`` here is authoritative — it is read immediately
    before the write, not at planning time, because the disk may have changed in
    between and a reversal must restore what was really there.
    ``created_directories`` records the folders this operation had to create, so
    that undoing it can remove exactly those and nothing else.
    """

    operation: ChangeOperation
    before_state: Mapping[str, object]
    created_directories: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Purpose: report what a plan did, including a partial run.

    Responsibilities: carry the plan, the operations that took effect, the
    resulting state, and any failure. Boundaries: it decides nothing about what
    to do next. Dependencies: ``AppliedOperation``. Collaborators: the executor,
    the review interface, and the database. Constraints: a failed run still
    lists everything that succeeded, because those are exactly the operations a
    reversal has to undo.
    """

    plan: ChangePlan
    state: ExecutionState
    applied: tuple[AppliedOperation, ...] = ()
    failure: str | None = None
    reverted: tuple[AppliedOperation, ...] = field(default_factory=tuple)

    @property
    def is_complete(self) -> bool:
        """Return whether every planned operation took effect."""
        return self.state is ExecutionState.APPLIED and len(self.applied) == len(
            self.plan.operations
        )


class ChangeExecutor:
    """Purpose: carry out a plan, and be able to undo exactly what it did.

    Responsibilities: rehearse the whole plan, apply operations in order,
    capture each previous state at the moment of writing, stop at the first
    failure, and reverse a recorded trail. Boundaries: it plans nothing and
    decides nothing about confidence. Dependencies: an injected tag store, and
    an artwork store and backup directory when a plan carries images.
    Collaborators: the planner and the application workflow. Constraints: it
    refuses to overwrite an existing file rather than resolving the conflict,
    and a failure leaves the trail intact so the caller can revert deliberately
    rather than have a rollback happen implicitly. It deletes exactly two
    things, both only while undoing what it itself did: a directory it created
    and left empty, and an image file it wrote whose content is still
    byte-for-byte its own.
    """

    def __init__(
        self,
        tag_store: TagStore,
        logger: logging.Logger,
        artwork_store: ArtworkStore | None = None,
        backup_directory: Path | None = None,
    ) -> None:
        """Create an executor from an injected tag store, artwork store, and logger."""
        self._tag_store = tag_store
        self._logger = logger
        self._artwork_store = artwork_store
        self._backup_directory = backup_directory

    def apply(
        self, plan: ChangePlan, witness: Callable[[AppliedOperation], None] | None = None
    ) -> ExecutionResult:
        """Apply every operation in order, stopping at the first failure.

        ``witness`` is told about each operation the moment it takes effect, so
        that something outside this class can write the trail down as it is
        made. Every failure the executor can *see* is already covered — it
        returns the trail with the failure — but a process killed mid-apply
        returns nothing, and the operations already on disk would then have no
        row to revert from. The exposure is one album and seconds wide, and it
        is closed rather than left unlikely.

        The callback is how this stays true to its boundaries: the executor is
        storage-free on purpose, and `library/` may not import `database/`. A
        witness that raises is logged and does not stop the run — the disk is
        already changed, and the end-of-run record is still to come.
        """
        if not plan.is_applicable:
            raise ExecutionError(
                "This plan is blocked and must not be applied: " + " ".join(plan.blockers)
            )
        if any(operation.kind in _IMAGE_KINDS for operation in plan.operations):
            # Checked before the first write rather than partway through, so a
            # misassembled executor never leaves an album half organized.
            self._artwork_requirements()
        # The rehearsal runs before the container lock on purpose: a plan the
        # disk can no longer carry out is the more fundamental fact, and
        # measuring a shelf inside a half-applied album reports a shelving
        # problem that is not there.
        self._refuse_a_plan_that_cannot_finish(plan)
        self._refuse_a_container_rename(plan)
        applied: list[AppliedOperation] = []
        for operation in plan.operations:
            try:
                entry = self._perform(operation)
                applied.append(entry)
                self._tell(witness, entry)
            except Exception as error:
                self._logger.error(
                    "Change operation failed; earlier operations remain applied.",
                    extra={"operation": "library.apply.failure"},
                )
                return ExecutionResult(
                    plan=plan,
                    state=ExecutionState.FAILED,
                    applied=tuple(applied),
                    failure=str(error),
                )
        self._logger.info(
            "Change plan applied.",
            extra={"operation": "library.apply", "operations": len(applied)},
        )
        return ExecutionResult(plan=plan, state=ExecutionState.APPLIED, applied=tuple(applied))

    def _tell(
        self, witness: Callable[[AppliedOperation], None] | None, entry: AppliedOperation
    ) -> None:
        """Report one applied operation, without letting the report break the run."""
        if witness is None:
            return
        try:
            witness(entry)
        except Exception:
            # Losing the running record is bad; abandoning a half-written album
            # because the record could not be written is worse.
            self._logger.exception(
                "An applied operation could not be recorded as it happened.",
                extra={"operation": "library.apply.witness_failure"},
            )

    def revert(self, result: ExecutionResult) -> ExecutionResult:
        """Undo a recorded trail, most recent operation first.

        A reversal that stopped is carried in `failure`, beside what came back —
        the result already has a place for it, and the alternative is a caller
        being told the reversal ended without being told it did not finish.

        Asked as *was there one*, not as *is it worth reading*: an exception
        whose text is empty is still a stop, and `stopped or result.failure`
        would have handed back the apply's own failure — or nothing at all — as
        though the walk had finished.
        """
        undone, stopped = self.revert_trail(result.applied)
        return ExecutionResult(
            plan=result.plan,
            state=ExecutionState.REVERTED,
            applied=(),
            failure=stopped if stopped is not None else result.failure,
            reverted=undone,
        )

    def revert_trail(
        self,
        applied: tuple[AppliedOperation, ...],
        undone_witness: Callable[[AppliedOperation], None] | None = None,
    ) -> tuple[tuple[AppliedOperation, ...], str | None]:
        """Undo a trail of applied operations, most recent first.

        Returns what came back and, when it stopped, what stopped it.

        The trail is all a reversal needs, which is what lets the interface
        revert a plan recorded in the database long after the run that applied
        it has ended.

        ``undone_witness`` is told about each operation as it comes back, for
        the same reason the apply has one: what is on the disk and what the
        database says about it must not be allowed to drift apart at the moment
        the run stops.

        **A failure partway is reported and not raised**, which is the rule
        ``ExecutionError`` states: it is raised only for a refusal before any
        write, because the trail matters more than the exception. Raising in
        the middle would hand everything already back on the disk to nobody,
        and no recorded path could follow it. The rehearsal below makes a stop
        partway rare, and rare is not the same as handled.
        """
        self._refuse_a_revert_that_cannot_finish(applied)
        undone: list[AppliedOperation] = []
        for entry in reversed(applied):
            try:
                self._undo(entry)
            except Exception as error:
                self._logger.error(
                    "Reversal stopped partway; what came back is reported.",
                    extra={"operation": "library.revert.failure", "undone": len(undone)},
                )
                return tuple(undone), str(error)
            undone.append(entry)
            self._tell_it_came_back(undone_witness, entry)
        self._logger.info(
            "Change plan reverted.",
            extra={"operation": "library.revert", "operations": len(undone)},
        )
        return tuple(undone), None

    def _tell_it_came_back(
        self, witness: Callable[[AppliedOperation], None] | None, entry: AppliedOperation
    ) -> None:
        """Report one undone operation, without letting the report break the reversal."""
        if witness is None:
            return
        try:
            witness(entry)
        except Exception:
            self._logger.exception(
                "An undone operation could not be recorded as it happened.",
                extra={"operation": "library.revert.witness_failure"},
            )

    def _refuse_a_revert_that_cannot_finish(self, trail: tuple[AppliedOperation, ...]) -> None:
        """Rehearse the whole reversal against the disk before the first undo.

        The reversal is the promise that makes an apply safe to offer, so it
        is rehearsed the same way the apply is. The outcome is written down
        only once the walk *returns*, so a reversal that stops halfway leaves
        the plan reading ``applied`` with every ``reverted_at`` empty: the disk
        is half-way back and the database says nothing happened. In a real
        library most stale trails break in the middle of the walk rather than
        on the first undo, which is the case a rehearsal exists for.

        A reversal is all or nothing for the same reason an apply is, so this
        walks the trail backwards against a model of the disk *as the reversal
        reshapes it* and names everything wrong at once. Refusing costs nothing
        and leaves the album exactly as it stands.
        """
        disk = _DiskAsPlanned()
        problems: list[str] = []
        lost: list[Path] = []
        for entry in reversed(trail):
            problems.extend(self._undo_problems(entry, disk, lost))
        problems = [*_lost_sentences(lost), *problems]
        if not problems:
            return
        self._logger.error(
            "Refusing a reversal that could not be carried through to its end.",
            extra={"operation": "library.revert.stale_trail", "problems": problems},
        )
        raise StalePlanError(
            " ".join(problems) + " Nothing was undone. Scan the folder the album is in now, "
            "so this library knows where it lives."
        )

    def _undo_problems(
        self, entry: AppliedOperation, disk: "_DiskAsPlanned", lost: list[Path]
    ) -> tuple[str, ...]:
        """Say what would stop one operation coming back, recording what it moves.

        A path the disk no longer holds goes to ``lost`` instead, so that it is
        named once however many operations reach it.
        """
        operation = entry.operation
        if operation.kind in (OperationKind.RENAME_FILE, OperationKind.RENAME_FOLDER):
            current = Path(str(operation.after_state["path"]))
            original = Path(str(entry.before_state["path"]))
            held = disk.holder(current)
            occupant = disk.holder(original)
            where = disk.origin(current)
            disk.moved(current, original)
            if held is None:
                lost.append(where)
                return ()
            if occupant is not None and not _is_same_entry(held, occupant):
                return (
                    f"{original} already exists, so {current.name} cannot be "
                    "put back under that name.",
                )
            return ()
        if operation.kind is OperationKind.WRITE_IMAGE:
            # The undo takes the image away and copies the replaced one back —
            # and `copy` creates the folders on the way, so a target whose album
            # folder is gone is written into a path nothing else will ever read.
            # It is the one undo here that does not raise, which is exactly why
            # it needs asking about.
            if disk.holder(operation.target_path.parent) is None:
                lost.append(disk.origin(operation.target_path))
            return ()
        # Only an icon this application drew is taken away, so an entry that
        # never drew one asks nothing of the disk.
        icon = operation.kind is OperationKind.EMBED_IMAGE and operation.after_state.get("icon")
        if icon and not entry.before_state.get("drawn"):
            return ()
        if disk.holder(operation.target_path) is None:
            lost.append(disk.origin(operation.target_path))
        return ()

    def _refuse_a_plan_that_cannot_finish(self, plan: ChangePlan) -> None:
        """Rehearse every operation against the disk before the first one is written.

        A plan is all or nothing to the album it describes. A late operation
        failing leaves an album with its tags written, files moved into disc
        folders, and a folder that never got its name — a state nothing can
        read back, and one the next scan refuses for a shelving problem that is
        not there. A destination that cannot be taken must therefore refuse the
        whole plan while refusing costs nothing.

        The rehearsal is run against a model of the disk *as the plan reshapes
        it*, not against the disk as it stands, because a plan legitimately
        renames a disc folder before moving files into it: asking the real disk
        whether `CD1/01 Song.flac` is free would refuse a correct plan, and
        asking it whether `CD1/01 Song.flac` is there to be moved would refuse
        one that has not run yet.

        A problem found does not stop the rehearsal: the move is recorded as if
        it had worked and the walk continues, so one refusal names everything
        wrong with the plan instead of surrendering one problem per attempt.
        """
        disk = _DiskAsPlanned()
        problems: list[str] = []
        lost: list[Path] = []
        for operation in plan.operations:
            if operation.kind is OperationKind.WRITE_TAGS:
                if disk.holder(operation.target_path) is None:
                    lost.append(disk.origin(operation.target_path))
                continue
            if operation.kind in _IMAGE_KINDS:
                problems.extend(self._image_problems(operation, disk, lost))
                continue
            source = operation.target_path
            destination = Path(str(operation.after_state["path"]))
            held = disk.holder(source)
            occupant = disk.holder(destination)
            if held is None:
                lost.append(disk.origin(source))
            elif occupant is not None and not _is_same_entry(held, occupant):
                problems.append(
                    f"{destination} already exists, so {source.name} cannot take that name."
                )
            disk.moved(source, destination)
        gone = bool(lost)
        problems = [*_lost_sentences(lost), *problems]
        if not problems:
            return
        self._logger.error(
            "Refusing a plan that could not be carried through to its end.",
            extra={
                "operation": "library.apply.stale_plan" if gone else "library.apply.refused",
                "problems": problems,
            },
        )
        if gone:
            raise StalePlanError(
                " ".join(problems) + " Scan the folder again to plan it from what is there now."
            )
        raise ExecutionError(" ".join(problems))

    def _image_problems(
        self, operation: ChangeOperation, disk: "_DiskAsPlanned", lost: list[Path]
    ) -> tuple[str, ...]:
        """Say what would stop one image operation, adding to ``lost`` what the disk lost.

        A staged image that is gone is not a stale plan: no scan puts it back,
        the artwork has to be fetched again, so it is reported without that
        advice.
        """
        store, _ = self._artwork_requirements()
        # A repair stages nothing: the picture it writes back is the one already
        # inside the file, so the only thing that can be missing is the file.
        staged = operation.after_state.get("source")
        if staged is not None:
            source = Path(str(staged))
            if not source.is_file():
                return (f"The cover image staged for this album is gone: {source}.",)
        target = operation.target_path
        occupant = disk.holder(target)
        if operation.kind is OperationKind.EMBED_IMAGE:
            if occupant is None:
                lost.append(disk.origin(target))
            return ()
        if occupant is not None and store.describe(occupant) is None:
            return (f"Refusing to overwrite something that is not a readable image: {target}.",)
        disk.wrote(target)
        return ()

    def _refuse_a_container_rename(self, plan: ChangePlan) -> None:
        """Refuse to give an album's name to a folder that mostly holds other albums.

        The scanner already declines to call such a folder an album.
        This is the second and independent lock: it reads the disk at the moment
        of writing instead of trusting the scan, so the day the first rule is
        wrong — a stale scan, a folder that grew since, a plan replayed later —
        the rename still does not happen. A disc subfolder belongs to the unit
        and counts as its own, which is why a multi-disc album passes here.
        """
        own_paths = {file.path for file in plan.unit.audio_files}
        for operation in plan.operations:
            if operation.kind is not OperationKind.RENAME_FOLDER:
                continue
            folder = operation.target_path
            if not folder.is_dir():
                # The plan describes a folder that is no longer there — applied
                # already, reverted, or moved by hand since the scan. That is a
                # stale plan, not a container, and saying "it mostly holds other
                # albums" about a folder that does not exist names a cause that
                # was never measured.
                self._logger.error(
                    "Refusing to apply a plan whose folder is no longer on disk.",
                    extra={"operation": "library.apply.stale_plan", "folder": str(folder)},
                )
                raise StalePlanError(
                    f"{folder.name} is no longer on disk, so this plan is out of date. "
                    "Scan the folder again to plan it from what is there now."
                )
            own = sum(1 for path in own_paths if folder in path.parents)
            own_below = sum(
                1 for path in own_paths if path.parent != folder and folder in path.parents
            )
            foreign = count_audio_below(folder, AUDIO_EXTENSIONS) - own_below
            if names_the_folder(own, foreign):
                continue
            self._logger.error(
                "Refusing to rename a folder that mostly holds other albums.",
                extra={
                    "operation": "library.apply.container_refused",
                    "own_tracks": own,
                    "foreign_tracks": foreign,
                },
            )
            raise ExecutionError(
                f"Refusing to rename {folder}: this album is {own} of the "
                f"{own + foreign} audio files it holds, so the folder is not its own."
            )

    def _perform(self, operation: ChangeOperation) -> AppliedOperation:
        if operation.kind is OperationKind.WRITE_IMAGE:
            return self._write_image(operation)
        if operation.kind is OperationKind.EMBED_IMAGE:
            return self._embed_image(operation)
        if operation.kind is OperationKind.WRITE_TAGS:
            before = self._tag_store.read(operation.target_path)
            desired = {
                name: tuple(values)
                for name, values in dict(operation.after_state["tags"]).items()  # type: ignore[arg-type]
            }
            self._tag_store.write(operation.target_path, desired)
            return AppliedOperation(
                operation=operation,
                before_state={"tags": {name: list(values) for name, values in before.items()}},
            )
        source = operation.target_path
        destination = Path(str(operation.after_state["path"]))
        created = _rename(source, destination)
        return AppliedOperation(
            operation=operation,
            before_state={"path": str(source)},
            created_directories=created,
        )

    def _write_image(self, operation: ChangeOperation) -> AppliedOperation:
        """Put one cover image in place, backing up whatever it replaces."""
        store, backup_directory = self._artwork_requirements()
        source = Path(str(operation.after_state["source"]))
        if not source.is_file():
            raise ExecutionError(f"The staged image is gone: {source}.")
        target = operation.target_path
        before: dict[str, object] = {}
        replaced = store.describe(target)
        if replaced is not None:
            destination = _backup_path(backup_directory, replaced)
            store.move(target, destination)
            before = {"path": str(target), "backup": str(destination), "digest": replaced.digest}
        elif target.exists():
            raise ExecutionError(
                f"Refusing to overwrite something that is not a readable image: {target}."
            )
        try:
            created = store.copy(source, target)
        except Exception:
            # Put back what was moved aside. An operation that raises never
            # reaches `applied`, so nothing records it and nothing will ever
            # undo it — the cover would sit in the backup folder under a name
            # that is its own digest, with no trail saying which album it came
            # out of, and the album would be left with none. The rule the
            # project states is that every write is preceded by a reversible
            # backup; a backup nothing can find is not one.
            if before:
                store.move(Path(str(before["backup"])), target)
            raise
        return AppliedOperation(
            operation=operation, before_state=before, created_directories=created
        )

    def _embed_image(self, operation: ChangeOperation) -> AppliedOperation:
        """Embed the front cover in one track, backing up the picture it replaces."""
        store, backup_directory = self._artwork_requirements()
        if operation.after_state.get("icon"):
            return self._draw_finder_icon(operation, store)
        repairing = bool(operation.after_state.get("repair"))
        source = None if repairing else Path(str(operation.after_state["source"]))
        if source is not None and not source.is_file():
            raise ExecutionError(f"The staged image is gone: {source}.")
        target = operation.target_path
        before: dict[str, object] = {}
        replaced = store.describe_embedded(target)
        if replaced is not None:
            destination = _backup_path(backup_directory, replaced)
            store.extract(target, destination)
            before = {"backup": str(destination), "digest": replaced.digest}
        if repairing:
            # The picture the file already holds, sent back through the writer
            # so that the block states what the image *is*. Nothing new enters
            # the file: the bytes taken out for the backup a line above are the
            # very bytes going back in. A file whose picture disappeared between
            # planning and applying is refused rather than emptied.
            if replaced is None:
                raise ExecutionError(f"There is no picture left to repair in {target.name}.")
            source = Path(str(before["backup"]))
        assert source is not None
        store.embed(target, source)
        return AppliedOperation(operation=operation, before_state=before)

    def _draw_finder_icon(
        self, operation: ChangeOperation, store: ArtworkStore
    ) -> AppliedOperation:
        """Give one file the small icon the Finder draws, made from its own picture.

        No backup, because nothing is replaced: this is only ever planned for a
        file that has no icon of its own, and the undo is to take away exactly
        what was given. `drawn` records whether it was, so a reversal never
        strips an icon that arrived from somewhere else.
        """
        target = operation.target_path
        if not target.exists():
            raise ExecutionError(_vanished(target))
        # A picture that disappeared between planning and applying is not a
        # failure worth stopping an apply for — the icon is the adornment on the
        # adornment. It is recorded as not drawn, and the undo leaves it alone.
        drawn = store.draw_in_the_finder(target)
        return AppliedOperation(operation=operation, before_state={"drawn": drawn})

    def _undo(self, entry: AppliedOperation) -> None:
        if entry.operation.kind is OperationKind.WRITE_TAGS:
            restored = {
                name: tuple(values)
                for name, values in dict(entry.before_state["tags"]).items()  # type: ignore[arg-type]
            }
            self._tag_store.write(entry.operation.target_path, restored)
            return
        if entry.operation.kind is OperationKind.WRITE_IMAGE:
            self._undo_write_image(entry)
            return
        if entry.operation.kind is OperationKind.EMBED_IMAGE:
            self._undo_embed_image(entry)
            return
        current = Path(str(entry.operation.after_state["path"]))
        original = Path(str(entry.before_state["path"]))
        _rename(current, original)
        _remove_created_directories(entry.created_directories)

    def _undo_write_image(self, entry: AppliedOperation) -> None:
        store, _ = self._artwork_requirements()
        target = entry.operation.target_path
        store.remove(target, str(entry.operation.after_state["digest"]))
        backup = entry.before_state.get("backup")
        if backup is not None and not target.exists():
            # Copied back, never moved. A backup is named by its own content, so
            # two discs of one album carrying the same cover keep one file
            # between them — and moving it back would give it to whichever disc
            # is undone first and leave the second with a `FileNotFoundError`
            # and no cover at all. Copying restores both, and because the name *is* the
            # digest, the bytes each disc gets back are provably the bytes it
            # had. The backup outliving the revert is what the folder's own
            # ceiling is for (`enforce_folder_limit`).
            store.copy(Path(str(backup)), target)
        _remove_created_directories(entry.created_directories)

    def _undo_embed_image(self, entry: AppliedOperation) -> None:
        store, _ = self._artwork_requirements()
        if entry.operation.after_state.get("icon"):
            if entry.before_state.get("drawn"):
                store.stop_drawing_in_the_finder(entry.operation.target_path)
            return
        backup = entry.before_state.get("backup")
        if backup is None:
            store.clear(entry.operation.target_path)
            return
        store.embed(entry.operation.target_path, Path(str(backup)))

    def _artwork_requirements(self) -> tuple[ArtworkStore, Path]:
        if self._artwork_store is None or self._backup_directory is None:
            raise ExecutionError(
                "Cover art cannot be written without an artwork store and a backup directory."
            )
        return self._artwork_store, self._backup_directory


@dataclass(slots=True)
class _DiskAsPlanned:
    """Purpose: answer where a path lives on disk once the plan so far has run.

    Responsibilities: translate a path in the plan's world back to the entry
    that really holds it, and record the moves and writes already rehearsed.
    Boundaries: it touches nothing — it only reads existence. Dependencies:
    none. Collaborators: the executor's rehearsal. Constraints: a path is
    resolved by undoing the recorded moves in reverse, so a folder renamed
    early carries everything below it; a path whose folder moved away and was
    not refilled is held by nothing. Images are recorded as plain creations
    because a plan always writes them last, after every rename that could move
    them.
    """

    _moves: list[tuple[Path, Path]] = field(default_factory=list)
    _written: set[Path] = field(default_factory=set)

    def holder(self, path: Path) -> Path | None:
        """Return the disk entry that would hold ``path`` now, or None if nothing does."""
        if path in self._written:
            return path
        current = self._traced(path)
        return current if current is not None and current.exists() else None

    def origin(self, path: Path) -> Path:
        """Return the path the disk had to hold for the plan so far to reach ``path``.

        What a refusal names. A plan renames `Artist - Song.flac` to
        `08. Song.flac` and then embeds a picture in the second name, so
        when the first is gone the picture's target is the same absence and not
        a file that ever existed to go missing.
        """
        return self._traced(path) or path

    def _traced(self, path: Path) -> Path | None:
        """Undo the recorded moves over ``path``, or None if the plan moved it away."""
        current = path
        for source, destination in reversed(self._moves):
            if _is_within(current, destination):
                current = _rebased(current, destination, source)
            elif _is_within(current, source):
                # Whatever was here moved away, and no later move — already
                # walked, since this runs in reverse — put anything back.
                return None
        return current

    def moved(self, source: Path, destination: Path) -> None:
        """Record that the plan moves ``source`` onto ``destination``."""
        self._moves.append((source, destination))

    def wrote(self, path: Path) -> None:
        """Record that the plan creates a file at ``path``."""
        self._written.add(path)


def _is_within(path: Path, folder: Path) -> bool:
    """Report whether ``path`` is ``folder`` itself or sits below it."""
    return path == folder or folder in path.parents


def _rebased(path: Path, old_root: Path, new_root: Path) -> Path:
    """Return ``path`` as it reads under ``new_root`` instead of ``old_root``."""
    return new_root if path == old_root else new_root / path.relative_to(old_root)


def _vanished(path: Path) -> str:
    """Say that the disk no longer holds something the plan describes."""
    return f"{path.name} is no longer on disk, so this plan is out of date."


_NAMED_WHEN_MANY = 3
"""How many lost paths a refusal names before it counts the rest."""


def _lost_sentences(lost: Sequence[Path]) -> tuple[str, ...]:
    """Say once which paths the disk no longer holds, each named once.

    A rehearsal meets one missing file once per operation that reaches it —
    its tags, its rename, the picture embedded under its new name — and a
    sentence for each would refuse one album in dozens of sentences. A path
    below a folder that is itself gone is that folder's absence, so it is not
    counted again.
    """
    unique = list(dict.fromkeys(lost))
    kept = [path for path in unique if not any(other in path.parents for other in unique)]
    if not kept:
        return ()
    if len(kept) == 1:
        return (_vanished(kept[0]),)
    names = [path.name for path in kept[:_NAMED_WHEN_MANY]]
    rest = len(kept) - len(names)
    listed = (
        f"{', '.join(names)} and {rest} more"
        if rest
        else f"{', '.join(names[:-1])} and {names[-1]}"
    )
    return (
        f"{len(kept)} of the paths this plan names are no longer on disk: {listed}. "
        "This plan is out of date.",
    )


def rerooted_trail(
    trail: tuple[AppliedOperation, ...], was: Path, now: Path
) -> tuple[AppliedOperation, ...]:
    """Read a recorded trail as it reads with the album where it lives today.

    A trail names absolute paths, and filing an album away — moving the
    finished folder from where it was organized into the music library — is an
    ordinary last step. Unless the trail follows it, it goes on naming the
    folder the apply renamed, and the album loses its way back at the moment it
    is filed.

    Two rules, because two different things can have happened and both are the
    user's doing. The folder may carry a new **name** — the trail's final folder is read as
    the one the album has now — and it may sit in a new **place**, which is what
    the second rule follows, so the spelling the folder had *before* the apply
    renamed it moves along with it.

    What the reversal then undoes is what this application did, and only that.
    The album is put back under its old name **where it was filed**, never
    moved back to the folder it was organized in: the name was this
    application's doing and the filing was the user's.

    Paths outside the album are left exactly as written — a replaced picture in
    the backup folder and a staged image in the cache are named from where they
    really are, and neither moved.
    """
    if was == now:
        return trail

    def follow(path: Path) -> Path:
        if _is_within(path, was):
            return _rebased(path, was, now)
        if _is_within(path, was.parent):
            return _rebased(path, was.parent, now.parent)
        return path

    def followed(state: Mapping[str, object]) -> Mapping[str, object]:
        if "path" not in state:
            return state
        return {**state, "path": str(follow(Path(str(state["path"]))))}

    return tuple(
        replace(
            entry,
            operation=replace(
                entry.operation,
                target_path=follow(entry.operation.target_path),
                after_state=followed(entry.operation.after_state),
                before_state=followed(entry.operation.before_state),
            ),
            before_state=followed(entry.before_state),
            created_directories=tuple(follow(path) for path in entry.created_directories),
        )
        for entry in trail
    )


def _backup_path(backup_directory: Path, facts: ImageFacts) -> Path:
    """Name a replaced image by its own content, so identical images collapse onto one file."""
    return backup_directory / f"{facts.digest[:16]}{extension_for(facts.media_type)}"


def _move_across_volumes(source: Path, destination: Path) -> None:
    """Copy ``source`` onto another volume with every attribute, then remove it.

    `shutil.move` copies with `copy2`, which carries no extended attribute on
    macOS (see `library.xattrs`), so an album filed onto another disk would
    arrive without the Finder tags on its folder and without the custom icons
    on its files. A folder carrying Finder tags survives `rename` whole and
    comes out of such a copy with no tag at all. The source is removed only
    after every path in the copy carries what its original carried.
    """

    def copy(one: str, other: str) -> str:
        shutil.copy2(one, other)
        carry_across(Path(one), Path(other))
        return other

    if not source.is_dir():
        copy(str(source), str(destination))
    else:
        shutil.copytree(source, destination, symlinks=True, copy_function=copy)
        for folder in (source, *(path for path in source.rglob("*") if path.is_dir())):
            carry_across(folder, destination / folder.relative_to(source))
    originals = (source, *source.rglob("*")) if source.is_dir() else (source,)
    lost = [
        path
        for path in originals
        if not path.is_symlink()
        and not movable_names(path) <= movable_names(destination / path.relative_to(source))
    ]
    if lost:
        raise ExecutionError(
            f"Copying {source} to another volume lost the Finder attributes of {len(lost)} "
            f"path(s); the original was left in place beside the copy at {destination}."
        )
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()


def _rename(source: Path, destination: Path) -> tuple[Path, ...]:
    """Move one path to another, refusing to overwrite anything that exists.

    Returns the directories that had to be created, so that undoing this move
    can remove exactly those — the only deletion this project performs.
    """
    if not source.exists():
        raise ExecutionError(f"Nothing to move at {source}.")
    if destination.exists() and not _is_same_entry(source, destination):
        raise ExecutionError(f"Refusing to overwrite an existing path: {destination}.")
    missing: list[Path] = []
    candidate = destination.parent
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        try:
            source.rename(destination)
        except OSError as error:
            # Filing an album into a library folder can cross volumes — a
            # large collection often lives on an external drive — and
            # `rename` cannot (EXDEV). The move then becomes
            # copy-then-delete, which is not atomic: it is the one case here
            # where an interruption can leave both copies, and it is still what
            # a person means by "send it there".
            if error.errno != errno.EXDEV:
                raise
            _move_across_volumes(source, destination)
    except Exception:
        # The directories this call made are only reported on the way out, and
        # a move that raises never gets there — so they would be left standing
        # in the library with nothing in them and nothing recording that this
        # project put them there.
        _remove_created_directories(tuple(missing))
        raise
    return tuple(missing)


def _remove_created_directories(directories: tuple[Path, ...]) -> None:
    """Remove directories this project created, and only while they are empty."""
    for directory in directories:
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            # A directory that is no longer empty belongs to the user now.
            continue


def _is_same_entry(source: Path, destination: Path) -> bool:
    """Report whether two paths are the same entry, as on a case-insensitive disk.

    Renaming ``a.flac`` to ``A.flac`` on macOS finds an existing destination that
    is in fact the source, and refusing that would block a legitimate correction.
    """
    try:
        return os.path.samefile(source, destination)
    except OSError:
        return False
