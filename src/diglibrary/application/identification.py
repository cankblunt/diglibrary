"""The workflow that decides what an album is and what should change about it."""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path

from diglibrary.application.acoustic import AcousticIdentification
from diglibrary.application.artwork import ArtworkService
from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSourceId,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
    written_credit,
)
from diglibrary.library.artwork import Artwork
from diglibrary.library.containers import numbers_deny_one_album
from diglibrary.library.hints import (
    SearchHints,
    derive_hints,
    local_track_titles,
    numbered_tracks,
    query_ladder,
)
from diglibrary.library.matching import (
    AlbumMatcher,
    MatchCandidate,
    MatchSignals,
    TrackAlignment,
    align_tracks,
    reconcile,
)
from diglibrary.library.medleys import indexed_position, merge_indexed_tracks
from diglibrary.library.models import AlbumUnit
from diglibrary.library.planner import (
    ChangePlan,
    ChangePlanner,
    OperationKind,
    folder_name_is_free,
)
from diglibrary.library.spelling import a_whole_word_apart
from diglibrary.library.tagrelease import TagReading, read_tags
from diglibrary.library.tags import TagStore
from diglibrary.metadata.normalization import fold_accents
from diglibrary.metadata.service import MetadataService
from diglibrary.metadata.transport import CredentialError

DEFAULT_CONFIDENCE_THRESHOLD = 0.90
"""The factory default above which a plan may be applied unattended."""

DEFAULT_DETAIL_LIMIT = 5
"""How many search results per source are fetched in full to obtain durations."""

_CORRECTED_LADDER_LIMIT = 3
"""How many query forms a typed correction is worth.

The full ladder exists for an album nobody is watching. A typed correction is
waited on, and walking a dozen queries against a source that allows one request
a second turns a deliberate gesture into half a minute of nothing.
"""

_YEAR_TOLERANCE = 1
"""How far a catalogue year may sit from the folder's and still corroborate it."""


class Decision(StrEnum):
    """What the workflow concluded about one album."""

    AUTOMATIC = "automatic"
    REVIEW = "review"
    UNIDENTIFIED = "unidentified"


@dataclass(frozen=True, slots=True)
class IdentificationOutcome:
    """Purpose: report what one album is, how sure that is, and what would change.

    Responsibilities: pair the unit with the winning candidate, the resulting
    plan, and the decision the threshold implies. Boundaries: it applies
    nothing — a plan here has not touched the disk. Dependencies: matcher and
    planner results. Collaborators: the review interface and the persistence
    layer. Constraints: a plan is present only when the album was identified and
    the changes are not blocked; every other case carries its reason instead.
    """

    unit: AlbumUnit
    hints: SearchHints
    decision: Decision
    candidate: MatchCandidate | None = None
    plan: ChangePlan | None = None
    partial_plan: ChangePlan | None = None
    sources_consulted: tuple[MetadataSourceId, ...] = ()
    reason: str = ""
    verification: dict[str, object] | None = None
    alignment: TrackAlignment | None = None
    # What the audio said, when the names failed and it was asked. Empty when
    # it was never asked, which is the ordinary case: an album that settled on
    # its names does not reach the fingerprinter at all. The window needs the
    # value to tell "not asked" from "asked, and the service does not know this
    # record", which is a fact about the library worth reading.
    acoustic: str = ""
    # Where the values came from, when the proposal is an arrangement of the
    # album's own tags rather than an identification. An arrangement owes no
    # proof — it claims nothing about the world — but it owes this: what was
    # read, how unanimously, and which files said something else.
    provenance: TagReading | None = None
    artwork: Artwork | None = None
    """The art this plan was built from, carried so a re-plan need not re-fetch.

    Addressing the archive costs a MusicBrainz search and an archive request per
    album, against a source that allows about one a second. A re-plan
    that reaches for it again turns a settings change on a shelf of a hundred
    albums into minutes of a frozen window — and on a restored shelf, where
    nothing was fetched, into live requests every time. So what was fetched
    travels with the outcome, and an offline re-plan keeps the cover instead of
    silently dropping it from the plan.
    """

    @property
    def confidence(self) -> float:
        """Return the winning candidate's confidence, or zero when unidentified."""
        return self.candidate.confidence if self.candidate is not None else 0.0


def _why_unavailable(error: Exception) -> str:
    """Classify why a source could not be searched, in words safe to show.

    Three states, deliberately the same three the acquisition provider reports
    — no key, the key was refused, the service did not answer — because
    they are the three a person can act on, and because a window that describes
    one kind of failure in two vocabularies is a window that has to be learned
    twice.

    **The exception's own text is never carried out of here.** A failure raised
    while a request was being signed is the one place a key could reach a
    screen, and the rule that keeps tokens out of the log is the same rule
    (`config.credentials`). What travels is the class, not the message.
    """
    if isinstance(error, CredentialError):
        return "no_key"
    status = getattr(error, "status", None)
    if status in {401, 403}:
        return "refused"
    return "unreachable"


class IdentificationWorkflow:
    """Purpose: identify one album against the configured sources and plan its changes.

    Responsibilities: derive search hints, query sources in precedence order,
    fetch enough detail to compare durations, rank and reconcile candidates, and
    turn the winner into a change plan. Boundaries: it writes nothing, to disk
    or to the database, and it is the only place that coordinates the Metadata
    Engine with the Library Engine — the engines never call each other.
    Dependencies: injected metadata service, matcher, and planner. Collaborators:
    the application entry points and the review interface. Constraints: it stops
    at the first unambiguous answer, because MusicBrainz allows
    roughly one request a second and consulting everything by default would turn
    a library scan into hours spent confirming what the first source settled.
    """

    def __init__(
        self,
        metadata: MetadataService,
        matcher: AlbumMatcher,
        planner: ChangePlanner,
        tag_store: TagStore,
        logger: logging.Logger,
        threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        detail_limit: int = DEFAULT_DETAIL_LIMIT,
        consult_every_source: bool = False,
        artwork: ArtworkService | None = None,
        verifier: object | None = None,
        acoustic: AcousticIdentification | None = None,
    ) -> None:
        """Create the workflow from already-assembled engines and a threshold.

        ``verifier`` is the independent witness, present only when the
        per-scan switch is on: it corroborates or contests every
        identification against a catalogue neither written source feeds.
        """
        self._metadata = metadata
        self._matcher = matcher
        self._planner = planner
        self._tag_store = tag_store
        self._logger = logger
        self._threshold = threshold
        self._detail_limit = detail_limit
        self._consult_every_source = consult_every_source
        self._artwork = artwork
        self._verifier = verifier
        # Present only when fpcalc and the user's own key are both there:
        # absent, identification runs exactly as it does without the audio.
        self._acoustic = acoustic
        # Which sources went dark while this workflow was running, and why.
        # A source that raises is caught, logged and treated as having found
        # nothing, so the scan carries on with whatever is left. That is the
        # right behaviour, and without this record it is silent: a credential
        # missing from the file the application reads makes every search of
        # that source raise, and albums go on being identified by the sources
        # that answered — some of them wrongly — with only the log saying so.
        #
        # Kept as a reason per source, never as the exception's own text: an
        # exception raised while handling a key is the one place a key could
        # reach a screen, and the same rule that keeps tokens out of the log
        # keeps them out of here (`config.credentials`).
        self.sources_unavailable: dict[str, str] = {}
        # What the identification now running went without — reset per album,
        # folded into its outcome's verification as `missed`. The run-level dict
        # above keeps only the first reason per source (setdefault), so it
        # cannot say which albums a failure touched; without this, a source
        # that failed and a source never asked look identical in the dialog.
        self._missed_by_this_identify: dict[str, str] = {}

    @property
    def sources_answered(self) -> dict[str, int]:
        """How many times each source has answered, counted where it answers.

        A raise alone is not an outage: a source that answered several searches
        and timed out once was consulted. A source is reported as down only
        when it failed and answered nothing.

        The count belongs to the Metadata Engine because this class reaches it
        in several ways, and a count kept beside the search loop misses an
        answer that arrives through another of them — the acoustic path
        fetching releases after a search timed out, for example.
        """
        return self._metadata.answers

    def forget_answers(self) -> None:
        """Start a new run's count of answers. Called where a run begins."""
        self._metadata.forget_answers()

    def identify(
        self,
        unit: AlbumUnit,
        taken_names: frozenset[str] = frozenset(),
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
    ) -> IdentificationOutcome:
        """Identify one album unit and return what would change, without changing it.

        ``transcoded`` names the files the Quality Engine proved were lossy
        before their container, so the folder can be named for what the audio
        is rather than for what its extension claims.

        ``measured_rates`` is what those files' audio measured, and only where
        the measurement is provably that file's. It reaches no name: every
        lossy-origin track is `[Lossy]`, and this is carried for the planner
        rather than for the word.
        """
        self._missed_by_this_identify = {}
        hints = derive_hints(unit, self._tag_store)
        if not hints.is_usable:
            return IdentificationOutcome(
                unit=unit,
                hints=hints,
                decision=Decision.UNIDENTIFIED,
                reason="Nothing on disk suggests what this album is.",
            )
        titles = local_track_titles(unit, self._tag_store)
        # One witness, one album, one question — the album's own words.
        # Built here rather than per source so that two catalogues holding the
        # same medley cost one request between them, and so that the tracklist
        # question and the verification below read one answer.
        witness = _AskedWitness(unit, hints, titles, self._verifier, self._logger)

        best_by_source: list[tuple[MetadataSourceId, MatchCandidate, TrackAlignment]] = []
        consulted: list[MetadataSourceId] = []
        for source in self._metadata.source_order:
            consulted.append(source)
            candidate = self._best_from(source, unit, hints, titles=titles, witness=witness)
            if candidate is None:
                continue
            alignment = self._align(unit, candidate.release, titles)
            best_by_source.append((source, candidate, alignment))
            # Stopping early needs more than a confident score: a candidate
            # whose files cannot be paired is going to review anyway, and the
            # next source may hold a pressing that pairs cleanly.
            #
            # A candidate that would write a different word than one of the
            # files carries is not settled either. The first source can answer
            # with full confidence and every file paired while the next one
            # publishes the track title exactly as the file spells it. The
            # score is about the album; the disagreement is about a word, and
            # nothing in the score can see it.
            settled_here = (
                candidate.confidence >= self._threshold
                and alignment.is_usable
                and not _rewrites_a_user_word(alignment, unit, titles)
            )
            if not self._consult_every_source and settled_here:
                break

        # The audio is asked last and only when the names failed: no candidate at
        # all, none above the threshold, or a release whose tracks could not all
        # be paired. It is the same trigger the folder-name ladder uses — one
        # rule rather than two — and it is what keeps a clean library from
        # paying for it.
        acoustic = ""
        if self._acoustic is not None and not any(
            candidate.confidence >= self._threshold and alignment.is_usable
            for _, candidate, alignment in best_by_source
        ):
            if not self._acoustic.is_available:
                acoustic = "unavailable"
            else:
                heard = self._from_the_audio(unit, titles)
                acoustic = "unknown" if heard is None else "named"
                if heard is not None:
                    best_by_source.append(heard)
                    consulted.append(heard[0])

        if not best_by_source:
            return IdentificationOutcome(
                unit=unit,
                hints=hints,
                decision=Decision.UNIDENTIFIED,
                sources_consulted=tuple(consulted),
                reason="No source returned a release resembling this album.",
                acoustic=acoustic,
            )

        # The winner is the best-evidenced candidate, not the first source's:
        # precedence orders consultation and breaks ties, but a text guess from
        # a preferred source never outranks duration or title proof from a less
        # preferred one.
        #
        # A file left with no track outranks confidence. The two ways an
        # alignment can fail are not the same failure: a track with no file is
        # a song the folder does not hold, which is ordinary and harmless,
        # while a file with no track is music about to go unnamed.
        source, primary, alignment = max(
            best_by_source,
            key=lambda entry: (
                entry[2].is_usable,
                -len(entry[2].unmatched_files),
                entry[1].signals.durations_compared > 0 or entry[1].signals.titles_agreeing > 0,
                entry[1].confidence,
            ),
        )
        # Only an answer that could itself have won may contest the winner: a
        # below-threshold guess is that source failing to find the album, not
        # disputing it. A real dispute — two confident answers with different
        # titles — still costs confidence and forces review.
        challengers = tuple(
            (other_source, candidate)
            for other_source, candidate, _ in best_by_source
            if other_source != source and candidate.confidence >= self._threshold
        )
        settled = reconcile(primary, challengers)
        assert settled is not None
        # The witness is asked about an album that did not settle on its own,
        # which is where it earns its place. Asking about every album would be
        # honest and unaffordable: iTunes allows roughly twenty calls a minute,
        # so a library of a few thousand folders would spend hours of a scan
        # waiting on a witness that agrees.
        unsettled = not (settled.confidence >= self._threshold and alignment.is_usable)
        verification = self._verification(
            settled, best_by_source, source, witness, ask_witness=unsettled
        )
        # Stored on the album, not only on the run: this is what lets the
        # dialog say quietly what ITS identification went without, and it
        # survives a restart with the rest of the verification record.
        if self._missed_by_this_identify:
            verification = {**(verification or {}), "missed": dict(self._missed_by_this_identify)}
        settled = self._with_original_year(source, settled)
        known = {entry_source: candidate for entry_source, candidate, _ in best_by_source}
        return self._outcome(
            unit,
            hints,
            settled,
            alignment,
            tuple(consulted),
            known,
            titles,
            taken_names,
            verification,
            transcoded,
            measured_rates=measured_rates,
            acoustic=acoustic,
        )

    def alternatives(
        self,
        unit: AlbumUnit,
        artist: str | None = None,
        album: str | None = None,
        limit: int = 8,
    ) -> tuple[MatchCandidate, ...]:
        """Return ranked candidates from every source, for the review screen.

        With ``artist`` or ``album`` given, the user's corrected hints decide
        what is *searched for* — and nothing else. Scoring stays against what
        the album itself suggests: scoring results on their similarity to the
        searched words would let an unrelated record that repeats those words
        outrank the release the search was made to find.
        """
        own = derive_hints(unit, self._tag_store)
        searched = own
        if artist or album:
            searched = SearchHints(
                artist=artist or own.artist,
                album=album or own.album,
                year=own.year,
                text=" ".join(part for part in (artist or own.artist, album or own.album) if part),
            )
        titles = local_track_titles(unit, self._tag_store)
        ranked: list[MatchCandidate] = []
        seen: set[tuple[str, str]] = set()
        for source in self._metadata.source_order:
            for candidate in self._ranked_from(
                source,
                unit,
                searched,
                titles=titles,
                judge=own,
                ladder_limit=_CORRECTED_LADDER_LIMIT if searched is not own else None,
            ):
                key = (str(candidate.release.source), candidate.release.source_release_id)
                if key not in seen:
                    seen.add(key)
                    ranked.append(candidate)
        ranked.sort(key=lambda candidate: -candidate.confidence)
        return tuple(ranked[:limit])

    def detail(self, source: MetadataSourceId, release_id: str) -> MatchCandidate | None:
        """Return one release in full, with the album's own year resolved.

        Used by the review screen to expand a candidate being weighed. It
        scores nothing against an album — the candidate wrapper exists only to
        carry the release through the same shape everything else uses — so the
        confidence here is not a judgement and is not shown as one.
        """
        release = self._metadata.fetch_release(source, release_id)
        if release is None:
            return None
        candidate = MatchCandidate(
            release=release,
            confidence=0.0,
            explanation="",
            signals=MatchSignals(
                local_track_count=0,
                release_track_count=len(release.tracks),
                durations_compared=0,
                durations_agreeing=0,
                text_similarity=0.0,
            ),
        )
        return self._with_original_year(source, candidate)

    def resolve_group(self, source: MetadataSourceId, group_id: str) -> str | None:
        """Return the release an album-level link stands for."""
        return self._metadata.resolve_group(source, group_id)

    def adopt(
        self,
        unit: AlbumUnit,
        source: MetadataSourceId,
        release_id: str,
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
    ) -> IdentificationOutcome:
        """Build the outcome for the release the user chose, whatever its score.

        The user's choice is authoritative for *which* release; the plan is
        still computed honestly, so an unalignable choice arrives blocked and
        says why rather than being forced through.

        ``transcoded`` and ``measured_rates`` are what the audio measured, and
        they are carried for the reason ``refine`` carries them: choosing a
        pressing is a statement about the catalogue and none at all about the
        audio, so it must not undress the album.
        """
        release = self._metadata.fetch_release(source, release_id)
        if release is None:
            return IdentificationOutcome(
                unit=unit,
                hints=derive_hints(unit, self._tag_store),
                decision=Decision.UNIDENTIFIED,
                reason="The chosen release could not be fetched in full.",
            )
        return self.adopt_release(
            unit, source, release, transcoded=transcoded, measured_rates=measured_rates
        )

    def adopt_release(
        self,
        unit: AlbumUnit,
        source: MetadataSourceId,
        release: ReleaseMetadata,
        offline: bool = False,
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
    ) -> IdentificationOutcome:
        """Build the outcome for a release already in hand, without the lookup.

        This is what lets the Library be restored from the release cached in
        ``metadata_releases``: matching and planning are deterministic and
        offline, so a restored album is the outcome a scan would produce
        rather than a replica of one assembled from stored fragments.

        ``offline`` refuses **every** outbound call this method could otherwise
        make: the reissue's original year, the cover lookup, and the identity
        search behind the cover lookup. The last two live inside ``_outcome``
        rather than beside it. MusicBrainz allows roughly one request a second,
        so a restore that made them would delay the launch by seconds per
        album.

        A test of this must count the requests made. A source that raises
        proves nothing, because every one of these calls is wrapped in a
        handler that logs and carries on.

        ``transcoded`` and ``measured_rates`` are the same facts ``identify``
        and ``refine`` are given, and they are here for the same reason: this
        method is what a restore, a hand-picked release and a pasted link all
        plan through, and without them each would name an album already proven
        to be a transcode by its container.
        """
        hints = derive_hints(unit, self._tag_store)
        titles = local_track_titles(unit, self._tag_store)
        # Adopting by hand, restoring from cache and pasting a link all arrive
        # here, and every one of them plans against this copy of the album.
        release = self._as_this_copy_holds_it(unit, release)
        candidate = self._matcher.rank(
            unit,
            (release,),
            search_hint=hints.text,
            local_titles=titles,
            hint_artist=hints.artist,
            hint_album=hints.album,
            hint_year=hints.year,
        )[0]
        if not offline:
            candidate = self._with_original_year(source, candidate)
        alignment = self._align(unit, candidate.release, titles)
        return self._outcome(
            unit,
            hints,
            candidate,
            alignment,
            (source,),
            {},
            titles,
            offline=offline,
            transcoded=transcoded,
            measured_rates=measured_rates,
        )

    def name_from_tags(
        self,
        unit: AlbumUnit,
        taken_names: frozenset[str] = frozenset(),
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
    ) -> IdentificationOutcome | None:
        """Arrange one album by its own tags, with nothing asked of anyone.

        Arranging is not identifying. This claims nothing about which record the
        album is; it reads what the files already say and arranges it into the
        configured pattern. That is why **no matcher runs here and no
        confidence exists**: confidence measures a candidate against the disk,
        and this candidate *is* the disk — the number could only ever be an
        album agreeing with itself. What stands in evidence's place is
        provenance, carried on the outcome.

        Nothing is fetched, nothing is written, and ``None`` comes back when the
        files do not say enough — that album stays exactly as it arrived.

        It still goes through ``_outcome``, and that is the point: the guards
        that live there — a folder whose shape denies it is one album, a plan
        that cannot close, a name colliding with another album's — are exactly
        the ones an arrangement needs, and a second path beside them would be a
        path those guards never reach.

        **It renames — and gives the Finder icon, and does nothing else.** The
        icon is the one image gesture that fits the mode's own rule: made from
        the picture the file already carries, offered only to a file that draws
        none, reaching nobody. No tag is written and no cover is fetched or
        embedded. And **it never comes back automatic** — the answer is
        complete, the plan is ready, and reaching it is an explicit gesture:
        the album's own Approve, or `Arrange N by tags`. The batch that must
        not reach it is the *scan's* batch, because the two proposals are
        different claims.
        """
        reading = read_tags(unit, self._tag_store)
        if reading.release is None:
            return None
        hints = derive_hints(unit, self._tag_store)
        titles = local_track_titles(unit, self._tag_store)
        # A candidate built by hand, not ranked: the sentence is provenance, the
        # confidence is a constant the threshold ignores (the decision below is
        # always REVIEW), and the signals say only what was read.
        candidate = MatchCandidate(
            release=reading.release,
            confidence=1.0,
            explanation=_arranged_sentence(reading),
            signals=MatchSignals(
                local_track_count=len(unit.audio_files),
                release_track_count=len(reading.release.tracks),
                durations_compared=0,
                durations_agreeing=0,
                text_similarity=1.0,
            ),
        )
        outcome = self._outcome(
            unit,
            hints,
            candidate,
            self._align(unit, candidate.release, titles),
            (MetadataSources.TAGS,),
            {},
            titles,
            taken_names=taken_names,
            transcoded=transcoded,
            measured_rates=measured_rates,
            offline=True,
            renames_only=True,
        )
        outcome = replace(outcome, provenance=reading)
        contested = self._contested_name(unit, outcome.plan, taken_names)
        if not contested:
            # Proposed, never automatic — see the note above.
            if outcome.decision is Decision.AUTOMATIC:
                outcome = replace(outcome, decision=Decision.REVIEW)
            # An arrangement that can run says where its words came from; one
            # that cannot keeps the blocker, which is the more useful sentence.
            if outcome.plan is not None and outcome.plan.is_applicable:
                outcome = replace(outcome, reason=candidate.explanation)
            return outcome
        # The tag path invents no designator: a name collision means the album
        # needs a scan. The planner breaks a collision by naming the pressing
        # year, and it cannot here — a release built from tags has one year
        # and therefore no pressing to fall back on, so the two albums would
        # render one name and the second rename would land on the first. The
        # answer is not a cleverer name; it is that this album has a question the
        # tags cannot settle.
        return replace(
            outcome,
            decision=Decision.REVIEW,
            plan=None,
            partial_plan=None,
            reason=(
                f"Another album already claims the name “{contested}”. Two pressings "
                "of one record read the same in their tags, so this one is waiting "
                "for a scan to tell them apart."
            ),
        )

    def compare_with_tags(self, unit: AlbumUnit, release: ReleaseMetadata) -> dict[str, object]:
        """Say where a catalogue's answer stands against what the tags said.

        When a scan answers an album, the dialog reads the catalogue *against*
        what the files were saying — one line, always, because agreement is
        information that settles as much as a difference unsettles. This is
        the verification the tag mode could never provide for itself, arriving
        at the only moment it exists.

        Only identity is compared — artist, title, year — never spelling: an
        accent is not a different record, and the comparison folds the way the
        matcher's own rules do.

        **How many tracks the release has is not identity**, so it is not
        compared here. Beside `Year agrees`, a clause such as `tracks 12 → 13`
        explains nothing, and the same fact is already on screen in the
        blocker, which *names* the track the folder has no file for. This line
        is about whether the record is the same record; coverage is the plan's
        business.
        """
        reading = read_tags(unit, self._tag_store)
        theirs = reading.release
        if theirs is None:
            return {}
        diffs: list[str] = []
        mine_artist = written_credit(theirs.artists)
        its_artist = written_credit(release.artists)
        if _folded(mine_artist) != _folded(its_artist):
            diffs.append(f"artist “{mine_artist}” → “{its_artist}”")
        if _folded(theirs.title) != _folded(release.title):
            diffs.append(f"title “{theirs.title}” → “{release.title}”")
        its_year = release.original_released_on or release.released_on
        if theirs.released_on and its_year and theirs.released_on.year != its_year.year:
            diffs.append(f"year {theirs.released_on.year} → {its_year.year}")
        return {"agrees": not diffs, "diffs": diffs}

    def _contested_name(
        self, unit: AlbumUnit, plan: ChangePlan | None, taken_names: frozenset[str]
    ) -> str:
        """The folder name this plan wants and cannot have, or an empty string.

        Asked of the plan rather than rendered again: the two could disagree, and
        the one that matters is the name that would be written.
        """
        for operation in plan.operations if plan is not None else ():
            if operation.kind is not OperationKind.RENAME_FOLDER:
                continue
            if operation.target_path != unit.folder_path:
                continue
            destination = Path(str(operation.after_state["path"]))
            if folder_name_is_free(destination, unit.folder_path, taken_names):
                return ""
            return destination.name
        return ""

    @property
    def verifier(self) -> object | None:
        """The independent witness, so one album can be put to it on demand.

        Exposed rather than reached around: the window asks about the album
        just opened, and a second construction of this client would hand that
        request its own rate budget.
        """
        return self._verifier

    def refine(
        self,
        unit: AlbumUnit,
        candidate: MatchCandidate,
        folder_name: str | None = None,
        track_titles: dict[int, str] | None = None,
        track_artists: dict[int, str] | None = None,
        taken_names: frozenset[str] = frozenset(),
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
        track_files: Mapping[str, int] | None = None,
        offline: bool = False,
        artwork: Artwork | None = None,
    ) -> IdentificationOutcome:
        """Re-plan an identified album around the user's corrections.

        A corrected track title replaces the catalogue's for the tag and the
        file name, keyed by the track's position on the release. The candidate
        and its confidence are untouched: the user corrected a spelling, not
        the identification.

        A corrected track artist becomes two things at once: the credit written
        into the file name, verbatim, and the values the `artist` tag holds,
        split on the separators it was written with.

        **The corrections are planned with and not kept.** The candidate returned
        is the one given, carrying the release as the source published it, and
        the corrected copy exists only long enough to be planned from. Written
        back into the candidate, a correction could not be withdrawn: removing
        its row from the database would change nothing on screen, because the
        next re-plan would correct an already corrected release. What holds
        every standing correction is the database, and the caller passes them
        all in.

        ``track_files`` are pairings made by hand, each a file's content
        signature against the release position it is said to hold. They are
        given to the aligner rather than to the release, because what they
        correct is not a value the catalogue published — it is which file is
        which track.

        ``offline`` refuses every network call, for the same reason
        ``adopt_release`` has it: restoring the Library must not spend a
        catalogue's rate limit on answers this app already has.

        ``taken_names`` and ``transcoded`` are the same facts ``identify`` is
        given, and they must be carried through: re-planning without them would
        name a proven transcode by its container, so correcting one track title
        would drop the album's own verdict from its folder.
        """
        hints = derive_hints(unit, self._tag_store)
        titles = local_track_titles(unit, self._tag_store)
        release = candidate.release
        corrected = release
        if track_titles or track_artists:
            corrected = replace(
                release,
                tracks=tuple(
                    self._corrected_track(track, track_titles, track_artists)
                    for track in release.tracks
                ),
            )
        # The release as the source published it travels with the corrected one:
        # a typed name is what gets written, and it pairs a file only where a
        # file answers to it. Aligned against the typed names alone, typing a
        # title would take the file away from the track it was typed for.
        alignment = self._align(
            unit,
            corrected,
            titles,
            pinned=track_files,
            published=release if corrected is not release else None,
        )
        # ``offline`` guards the cover lookup as well: it addresses the Cover
        # Art Archive *through* MusicBrainz, two requests per album against a
        # source that allows about one a second. Restoring the Library re-plans
        # every album that carries a correction, so without this the restore
        # would be online for exactly those albums.
        # Art already in hand is used as it is. Passing it is how a caller
        # re-plans offline without dropping the cover from the plan, which is
        # what a settings change on a whole shelf has to be able to do.
        # `is_partial` belongs here for the same reason it belongs at the other
        # artwork gate (`_outcome`): an album that pairs all but one of its
        # files is an album whose cover is known, and refusing it art because
        # one file is a bonus leaves the folder with no `cover.jpg` and no
        # Finder icon. Written as *anything the planner will act on*, so the
        # next state to exist falls inside it rather than out.
        if artwork is None and (alignment.is_usable or alignment.is_partial):
            # Offline stops the *archive* being asked, which is what costs a
            # request. It does not mean refusing to read the local disk: the
            # album's own picture answering for the folder cover, and a sleeve
            # already in the folder answering for the tracks, are both local.
            artwork = (
                self._artwork_locally(unit)
                if offline
                else self._artwork_for(unit, hints, corrected, {}, titles)
            )
        # Titles the user corrected are their own words, and the naming rule
        # never reaches them; the planner is told which positions they hold.
        user_titles = frozenset(track_titles or ())
        plan = self._planner.plan(
            unit,
            corrected,
            alignment,
            artwork,
            folder_name=folder_name,
            taken_names=taken_names,
            transcoded=transcoded,
            measured_rates=measured_rates,
            user_titles=user_titles,
        )
        # An album paired by hand stays in review, however confident the
        # release is and however completely the plan closes. A hand pairing is
        # a decision taken one album at a time, and a batch that renamed the
        # album on the strength of it would apply a change nobody approved.
        # Apply on this album is one click away; nothing else reaches it.
        automatic = (
            candidate.confidence >= self._threshold and plan.is_applicable and not track_files
        )
        return IdentificationOutcome(
            unit=unit,
            hints=hints,
            decision=Decision.AUTOMATIC if automatic else Decision.REVIEW,
            candidate=candidate,
            plan=plan,
            partial_plan=self._partial_for(
                unit,
                corrected,
                alignment,
                artwork,
                plan,
                folder_name=folder_name,
                transcoded=transcoded,
                measured_rates=measured_rates,
                user_titles=user_titles,
            ),
            # Carried, not merely computed: the pairing is what the plan table
            # shows in its `now on disk` column, and an outcome without it
            # draws `no file here` beside every track of an album whose files
            # are paired.
            alignment=alignment,
            sources_consulted=(release.source,),
            reason=" ".join(plan.blockers) if plan.blockers else candidate.explanation,
            artwork=artwork,
        )

    def _corrected_track(
        self,
        track: TrackMetadata,
        titles: dict[int, str] | None,
        artists: dict[int, str] | None,
    ) -> TrackMetadata:
        """Put one track's corrections into it, each where it belongs."""
        title = (titles or {}).get(track.position, track.title)
        credit = (artists or {}).get(track.position)
        if credit is None:
            return replace(track, title=title)
        return replace(
            track,
            title=title,
            # The file name gets the credit exactly as written; the tag gets
            # it as values.
            artist_credit=credit,
            artists=split_artist_credit(credit),
        )

    def _from_the_audio(
        self, unit: AlbumUnit, titles: tuple[str, ...]
    ) -> tuple[MetadataSourceId, MatchCandidate, TrackAlignment] | None:
        """Return the audio's own best candidate, scored like every other one.

        The audio *contests* rather than concludes: a fingerprint proves which
        recording a file holds, which is not the same claim as which pressing
        an album is, and pressings are told apart by duration. So what the
        audio contributes is releases nothing else found, and they earn their
        confidence on the same evidence.
        """
        if self._acoustic is None:
            return None
        try:
            releases = self._acoustic.releases_for(unit)
        except Exception:
            self._logger.exception(
                "The audio could not be asked about this album.",
                extra={"operation": "identification.acoustic.failure"},
            )
            return None
        if not releases:
            return None
        ranked = self._matcher.rank(unit, releases, local_titles=titles)
        if not ranked:
            return None
        return (
            MetadataSources.MUSICBRAINZ,
            ranked[0],
            self._align(unit, ranked[0].release, titles),
        )

    def _align(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        titles: tuple[str, ...],
        pinned: Mapping[str, int] | None = None,
        published: ReleaseMetadata | None = None,
    ) -> TrackAlignment:
        return align_tracks(
            unit,
            release,
            tolerance_ms=self._matcher.duration_tolerance_ms,
            ordered_tolerance_ms=self._matcher.ordered_tolerance_ms,
            local_titles=titles,
            pinned=pinned,
            published=published,
        )

    def _with_original_year(
        self, source: MetadataSourceId, candidate: MatchCandidate
    ) -> MatchCandidate:
        """Attach the album's own release year to a candidate that is a reissue.

        Durations are what identify a release, and a reissue is often the only
        pressing whose durations a catalogue publishes. That makes the reissue
        the right match and the wrong year: the album is from 1974 even when the
        CD in hand is from 2006.
        """
        release = candidate.release
        if release.original_released_on is not None or release.release_group_id is None:
            return candidate
        try:
            original = self._metadata.fetch_first_release_date(source, release.release_group_id)
        except Exception:
            self._logger.warning(
                "The album's original release date could not be resolved.",
                extra={"operation": "identification.original_year.failure"},
            )
            return candidate
        if original is None:
            return candidate
        return replace(candidate, release=replace(release, original_released_on=original))

    def _partial_for(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        alignment: TrackAlignment,
        artwork: Artwork | None,
        plan: ChangePlan | None,
        *,
        folder_name: str | None = None,
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
        write_tags: bool = True,
        user_titles: frozenset[int] = frozenset(),
    ) -> ChangePlan | None:
        """Plan what did pair and leave the rest alone, or ``None``.

        One function because two paths produce outcomes — ``_outcome`` and
        ``refine``, the path an album takes when a file is paired by hand, a
        title or a credit is corrected, or another release is adopted — and
        both must offer the partial plan. With two implementations, the one
        that is forgotten disarms the partial approval after such a gesture,
        on an alignment that has not changed.

        Computing it here rather than on approval is what makes the gesture
        instant and costs no further fetch.
        """
        if plan is None or plan.is_applicable or not alignment.is_partial:
            return None
        partial = self._planner.plan(
            unit,
            release,
            alignment,
            artwork,
            folder_name=folder_name,
            allow_partial=True,
            transcoded=transcoded,
            measured_rates=measured_rates,
            write_tags=write_tags,
            user_titles=user_titles,
        )
        return partial if partial.is_applicable else None

    def _outcome(
        self,
        unit: AlbumUnit,
        hints: SearchHints,
        candidate: MatchCandidate,
        alignment: TrackAlignment,
        consulted: tuple[MetadataSourceId, ...],
        known: dict[MetadataSourceId, MatchCandidate],
        titles: tuple[str, ...] = (),
        taken_names: frozenset[str] = frozenset(),
        verification: dict[str, object] | None = None,
        transcoded: frozenset[str] = frozenset(),
        measured_rates: Mapping[str, int] | None = None,
        offline: bool = False,
        # The one path whose release came from the files themselves plans a
        # rename and nothing else: no tag is handed back to the file it was
        # read from, and no cover is fetched, written or embedded, because
        # neither is a rename. Stated here rather than at the two `plan` calls
        # below, so that both exits of this method honour it.
        renames_only: bool = False,
        acoustic: str = "",
        # Carried into every outcome: persistence decides `method='acoustic'`
        # from this field, and the window reads it to say whether the audio
        # was fingerprinted.
    ) -> IdentificationOutcome:
        # What the folder's own numbers prove about it, before anything is
        # planned for it. Read here rather than in the scanner, whose stated
        # boundary is the filesystem and no tag.
        denial = numbers_deny_one_album(
            numbered_tracks(unit, self._tag_store), len(unit.audio_files)
        )
        if unit.ambiguous_layout or denial:
            # Identified, never planned. A folder whose shape does not say which
            # album it is gets an answer to look at and no changes to approve:
            # the application asks rather than merging, splitting, or naming a
            # folder on a guess. Art is not fetched either, because nothing
            # here will be written.
            return IdentificationOutcome(
                acoustic=acoustic,
                unit=unit,
                hints=hints,
                decision=Decision.REVIEW,
                candidate=candidate,
                sources_consulted=consulted,
                reason=" ".join(
                    part for part in (unit.ambiguous_layout, denial, candidate.explanation) if part
                ),
                verification=verification,
                alignment=alignment,
            )
        # Art is fetched for an album headed for review as well as for one that
        # applies unattended: what the user approves has to be the whole plan.
        #
        # Except when restoring, which must reach nothing. The cover lookup,
        # and the MusicBrainz identity search behind it, cost two requests per
        # album against a source that allows about one a second, which a
        # restore of a whole shelf would pay at every launch.
        #
        # What offline refuses is the *request*. The local disk is still read:
        # the album's own picture may still take the folder cover, and a sleeve
        # already in the folder may still reach the tracks. Otherwise an
        # organized album whose files all carry a cover is restored over a
        # folder holding no image at all.
        #
        # The arrangement reads the local disk for a cover, and asks nobody.
        # Handed nothing, an album arranged by its own tags would end with the
        # Finder icons and no `cover.jpg` beside them, while the picture is in
        # the files, one local read away. It is the local half only: the
        # archive is addressed *through* MusicBrainz and arranging asks nobody
        # anything, so there is no release to address it by; and the planner
        # takes the cover from this and never the embed, because writing into
        # a file is what this mode promises not to do.
        if renames_only:
            artwork = self._artwork_locally(unit)
        elif not (alignment.is_usable or alignment.is_partial):
            artwork = None
        elif offline:
            artwork = self._artwork_locally(unit)
        else:
            artwork = self._artwork_for(unit, hints, candidate.release, known, titles)
        plan = self._planner.plan(
            unit,
            candidate.release,
            alignment,
            artwork,
            taken_names=taken_names,
            transcoded=transcoded,
            measured_rates=measured_rates,
            write_tags=not renames_only,
        )
        if not plan.is_applicable:
            return IdentificationOutcome(
                acoustic=acoustic,
                unit=unit,
                hints=hints,
                decision=Decision.REVIEW,
                candidate=candidate,
                plan=plan,
                partial_plan=self._partial_for(
                    unit,
                    candidate.release,
                    alignment,
                    artwork,
                    plan,
                    transcoded=transcoded,
                    measured_rates=measured_rates,
                    write_tags=not renames_only,
                ),
                sources_consulted=consulted,
                reason=" ".join(plan.blockers),
                verification=verification,
                alignment=alignment,
                artwork=artwork,
            )
        automatic = candidate.confidence >= self._threshold
        return IdentificationOutcome(
            acoustic=acoustic,
            unit=unit,
            hints=hints,
            decision=Decision.AUTOMATIC if automatic else Decision.REVIEW,
            candidate=candidate,
            plan=plan,
            sources_consulted=consulted,
            verification=verification,
            alignment=alignment,
            artwork=artwork,
            reason=(
                candidate.explanation
                if automatic
                else f"Confidence {candidate.confidence:.2f} is below the "
                f"{self._threshold:.2f} threshold. {candidate.explanation}"
            ),
        )

    def _verification(
        self,
        settled: MatchCandidate,
        best_by_source: list[tuple[MetadataSourceId, MatchCandidate, TrackAlignment]],
        winner: MetadataSourceId,
        witness: "_AskedWitness",
        ask_witness: bool = True,
    ) -> dict[str, object] | None:
        """Collect what independent witnesses say about this album.

        Two kinds of testimony, both stored as verdicts and never as data.
        Cross-source consensus is free: when the losing catalogue's own best
        answer names the same album, two databases that do not feed each other
        agree. The streaming witness costs a request against a service that
        allows about twenty a minute, so ``ask_witness`` decides whether this
        album is worth one.

        **It is asked about the album and not about the answer**, which is why
        the question is not built here: ``witness`` already holds it, in the
        album's own words, and hands back the same testimony however many
        callers read it. A testimony already bought during ranking is therefore
        kept even for an album that settled — the request has been made, and
        throwing the answer away to honour a budget spends nothing and loses
        everything the witness said.

        Every source consulted also records where it stands, by *name*: how many
        of the local track titles it publishes. A catalogue that names every
        local title is saying something about the record, while agreeing
        lengths can be coincidence, and the losing catalogue's position is
        worth seeing even when it lost.
        """
        verdicts: dict[str, object] = {}
        verdicts["sources"] = [
            {
                "source": str(other_source),
                "release_id": candidate.release.source_release_id,
                "title": candidate.release.title,
                "titles_agreeing": candidate.signals.titles_agreeing,
                "titles_compared": candidate.signals.titles_compared,
                "tracks": len(candidate.release.tracks),
                "local_tracks": candidate.signals.local_track_count,
                "winner": other_source == winner,
            }
            for other_source, candidate, _ in best_by_source
        ]
        for other_source, candidate, _ in best_by_source:
            if other_source == winner:
                continue
            same = _titles_agree(settled.release.title, candidate.release.title)
            verdicts["cross_source"] = {
                "witness": str(other_source),
                "title_agrees": same,
                "track_count_agrees": (
                    len(candidate.release.tracks) == len(settled.release.tracks)
                ),
            }
        if ask_witness or witness.was_asked:
            testimony = witness.testimony()
            if testimony is not None:
                verdicts["itunes"] = testimony
        return verdicts or None

    def _artwork_locally(self, unit: AlbumUnit) -> Artwork | None:
        """What the files and folder already hold, with nothing asked of anyone."""
        if self._artwork is None or not self._artwork.enabled:
            return None
        return self._artwork.local_only(
            tuple(file.path for file in unit.audio_files),
            None if unit.is_loose_track else unit.folder_path,
        )

    def _artwork_for(
        self,
        unit: AlbumUnit,
        hints: SearchHints,
        release: ReleaseMetadata,
        known: dict[MetadataSourceId, MatchCandidate],
        titles: tuple[str, ...] = (),
    ) -> Artwork | None:
        """Fetch the cover art of the identified album, when one can be addressed."""
        if self._artwork is None or not self._artwork.enabled:
            return None
        identity = self._musicbrainz_identity(unit, hints, release, known, titles)
        if identity is None:
            self._logger.info(
                "No MusicBrainz release matched this album, so the archive was not consulted.",
                extra={"operation": "artwork.unaddressable"},
            )
        release_id, group_id = identity if identity is not None else (None, None)
        # The album's own picture is still a candidate for the folder cover, even
        # when nothing addresses the archive.
        return self._artwork.fetch(
            release_id,
            group_id,
            tuple(file.path for file in unit.audio_files),
            folder=None if unit.is_loose_track else unit.folder_path,
        )

    def _musicbrainz_identity(
        self,
        unit: AlbumUnit,
        hints: SearchHints,
        release: ReleaseMetadata,
        known: dict[MetadataSourceId, MatchCandidate],
        titles: tuple[str, ...] = (),
    ) -> tuple[str, str | None] | None:
        """Resolve the MusicBrainz release that addresses this album's art.

        Nothing found here is ever written: the identified release stays the
        authority for every tag and every name. The barcode is tried first
        because it identifies a pressing exactly, and the fallback search is
        accepted only at the automatic threshold — the same durations, scored by
        the same matcher, that decided the identification itself.
        """
        if release.source == MetadataSources.MUSICBRAINZ:
            return release.source_release_id, release.release_group_id
        already_seen = known.get(MetadataSources.MUSICBRAINZ)
        if already_seen is not None and already_seen.confidence >= self._threshold:
            return already_seen.release.source_release_id, already_seen.release.release_group_id
        queries = [MetadataQuery(barcode=release.barcode)] if release.barcode else []
        queries.append(hints.to_query())
        for query in queries:
            candidate = self._best_from(
                MetadataSources.MUSICBRAINZ, unit, hints, query, titles=titles or None
            )
            if candidate is not None and candidate.confidence >= self._threshold:
                return candidate.release.source_release_id, candidate.release.release_group_id
        return None

    def _best_from(
        self,
        source: MetadataSourceId,
        unit: AlbumUnit,
        hints: SearchHints,
        query: MetadataQuery | None = None,
        titles: tuple[str, ...] | None = None,
        witness: "_AskedWitness | None" = None,
    ) -> MatchCandidate | None:
        """Return this source's best candidate, fetching detail where it is needed."""
        ranked = self._ranked_from(source, unit, hints, query=query, titles=titles, witness=witness)
        return ranked[0] if ranked else None

    def _ranked_from(
        self,
        source: MetadataSourceId,
        unit: AlbumUnit,
        hints: SearchHints,
        query: MetadataQuery | None = None,
        titles: tuple[str, ...] | None = None,
        judge: SearchHints | None = None,
        ladder_limit: int | None = None,
        witness: "_AskedWitness | None" = None,
    ) -> tuple[MatchCandidate, ...]:
        """Return this source's ranked candidates, fetching detail where it is needed.

        Without an explicit ``query``, the hint's query ladder is walked: each
        later, looser query runs only when the previous one **failed to
        convince**. ``judge`` scores the results against something other than
        what was searched for, which is what keeps a corrected search from
        grading its own answers. ``ladder_limit`` shortens the walk when the
        query came from a person: deliberate input deserves a fast answer more
        than it deserves an exhaustive one.

        **A rung that answers is not a rung that answered the question.** A
        loose rung can return dozens of rows that are all another record, while
        a later rung — the one that asks by a track's name — returns the album
        itself. So the walk ends on a candidate that survives the names' veto,
        and a rung that brings back only vetoed answers is a rung that answered
        nothing.

        The bar is the veto and deliberately not the automatic threshold: an
        album that merely needs review is the ordinary case, and walking every
        rung for each of those would put three more searches on nearly every
        album of a library scan.
        """
        queries = (query,) if query is not None else query_ladder(hints, titles or ())
        if ladder_limit is not None:
            queries = queries[:ladder_limit]
        gathered: list[MatchCandidate] = []
        for attempt in queries:
            try:
                summaries = self._metadata.search_source(source, attempt)
            except Exception as error:
                self._logger.exception(
                    "A metadata source could not be searched.",
                    # Which source, because this record is what a sentence on
                    # screen is built from, and a failure that names no source
                    # cannot be traced to the service it describes. The class of
                    # the failure is still all that travels — an exception raised
                    # while a request was being signed is the one place a key
                    # could reach a log.
                    extra={
                        "operation": "identification.search.failure",
                        "source": str(source),
                        "reason": _why_unavailable(error),
                    },
                )
                # Remembered, so that the run this belongs to can say a source
                # was missing from it. Returning empty here is still right —
                # one source being out is not a reason to abandon the album —
                # but it must stop being indistinguishable from a source that
                # answered and knew nothing.
                self.sources_unavailable.setdefault(str(source), _why_unavailable(error))
                self._missed_by_this_identify[str(source)] = _why_unavailable(error)
                return ()
            # The answer was counted by the Metadata Engine on the way back, as
            # every answer it gives is. Counting it here as well would keep one
            # rule in two places.
            if not summaries:
                continue

            # Search results are summaries without a tracklist, and durations are
            # the evidence this project decides on, so a few must be fetched in
            # full before anything can be scored. Choosing which few matters: a
            # catalogue returns dozens of pressings of the same album whose
            # titles are identical, so text similarity alone picks almost at
            # random. The year already known from the folder or the tags is
            # what separates them.
            shortlist = _shortlist(summaries, hints, self._detail_limit)
            detailed: list[ReleaseMetadata] = []
            for release in shortlist:
                if not release.tracks:
                    fetched = self._fetch(source, release.source_release_id)
                    if fetched is not None:
                        release = fetched
                detailed.append(self._as_this_copy_holds_it(unit, release, witness))
            scored_against = judge if judge is not None else hints
            ranked = self._matcher.rank(
                unit,
                tuple(detailed),
                search_hint=scored_against.text,
                local_titles=titles,
                hint_artist=scored_against.artist,
                hint_album=scored_against.album,
                hint_year=scored_against.year,
            )
            gathered.extend(ranked)
            if ranked and any(_convincing(candidate) for candidate in ranked):
                break
        if not gathered:
            return ()
        # Every rung's answers together, best first — a later rung's candidate
        # may beat an earlier one's, which is the whole point of having walked on.
        return tuple(sorted(gathered, key=lambda candidate: candidate.confidence, reverse=True))

    def _as_this_copy_holds_it(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        witness: "_AskedWitness | None" = None,
    ) -> ReleaseMetadata:
        """Fold entries the source indexed inside one track.

        Applied where a release meets the album it will be scored and planned
        against, because that is the only place the guard can be asked: the
        number of files is what says whether this copy holds a medley as one
        recording or as two, and the metadata mapper never sees a file.

        ``witness`` is the independent catalogue, asked *lazily and once*. The
        fold is decided during ranking, and it must not become a request per
        candidate. It does not, because the question is the album's own words
        rather than a candidate's: one answer is a fact about the album, so it
        serves every pressing of it, and here it is only ever asked when the
        shape has actually put a fold on the table.
        """
        if witness is None or not _might_fold(release):
            return merge_indexed_tracks(release, unit.track_count)
        return merge_indexed_tracks(release, unit.track_count, witness.tracks())

    def _fetch(self, source: MetadataSourceId, release_id: str) -> ReleaseMetadata | None:
        try:
            return self._metadata.fetch_release(source, release_id)
        except Exception:
            self._logger.warning(
                "A release could not be fetched in full.",
                extra={"operation": "identification.detail.failure"},
            )
            return None


def _rewrites_a_user_word(
    alignment: TrackAlignment, unit: AlbumUnit, titles: tuple[str, ...]
) -> bool:
    """Whether this pairing would write a word the file itself does not use.

    The one question a confident score cannot answer. Every signal the engine
    weighs is about the *album* — how many titles resembled, how many lengths
    agreed, whether the year fits — and all of them can be perfect while one
    title says a different word: the pairing is right, the recording is the
    same, and only the word is in dispute. Only this looks at words.

    Asked of the pairing rather than of the file order, because the two are not
    the same on any album where this matters. A file with no title in its tags
    contributes nothing rather than a disagreement — silence is not dissent.

    It does not decide anything and does not veto the candidate: it withholds
    the right to *stop asking*, so the next source gets its turn and the winner
    is still chosen on evidence afterwards.
    """
    by_path = dict(zip((file.path for file in unit.audio_files), titles, strict=False))
    return any(
        a_whole_word_apart(by_path.get(assignment.file.path), assignment.track.title)
        for assignment in alignment.assignments
    )


def _convincing(candidate: MatchCandidate) -> bool:
    """Whether this answer is worth ending a search on.

    A candidate the **names refuse** is not convincing, and that question is the
    matcher's own so it is asked there rather than restated here: a page of
    rows that are all another record is, for the purpose of stopping a search,
    nothing.

    A candidate **nobody could check** is not convincing either, and the veto
    can never say so — it needs names on both sides to compare. A release that
    publishes no tracklist at all is scored on album-level text alone and
    cannot be vetoed by a rule about names it does not have. **An answer with
    no per-track evidence is a guess, and a guess does not stop a search.**
    """
    if candidate.vetoed_by_the_names:
        return False
    signals = candidate.signals
    return signals.titles_compared > 0 or signals.durations_compared > 0


def _might_fold(release: ReleaseMetadata) -> bool:
    """Return whether this tracklist has entries indexed inside a track at all.

    Local, free, and the whole reason asking the witness stays affordable: few
    tracklists have such entries, and every other album never reaches the
    request.
    """
    positions = [track.published_position for track in release.tracks]
    return any(indexed_position(position) for position in positions)


class _AskedWitness:
    """Purpose: put one album to the witness, in the album's own words, once.

    Responsibilities: hold the question, ask it lazily, and memoize the whole
    testimony so that every caller reads one answer bought with one request.
    Boundaries: what leaves here is verdicts — booleans and counts this
    application computed — and never the catalogue data they were computed from.
    Dependencies: the witness the workflow was built with.
    Collaborators: ``_as_this_copy_holds_it``, which wants the count and nothing
    else, and ``_verification``, which stores the whole testimony.
    Constraints: **the question is the album's own words and never a
    candidate's**. A witness asked in a candidate's words is not independent:
    asked about a low-scoring guess, it can confirm a record that matches none
    of the files while the release that matches all of them goes unasked. The
    request is memoized including its failures — a witness that could not be
    reached is not asked again for the same album, because that would be one
    request per candidate and the cost is fixed at one per album.
    """

    def __init__(
        self,
        unit: AlbumUnit,
        hints: SearchHints,
        titles: tuple[str, ...],
        verifier: object | None,
        logger: logging.Logger,
    ) -> None:
        """Hold what is needed to ask, without asking."""
        self._unit = unit
        self._hints = hints
        self._titles = titles
        self._verifier = verifier
        self._logger = logger
        self._asked = False
        self._testimony: dict[str, object] | None = None

    @property
    def was_asked(self) -> bool:
        """Whether the request has already been made, so a caller spends nothing.

        This is what lets a settled album keep an answer it did not pay for: the
        track count is bought during ranking, and reading the rest of the same
        testimony afterwards costs no second request.
        """
        return self._asked

    def testimony(self) -> dict[str, object] | None:
        """Return every verdict this witness gave about the album, or ``None``."""
        if self._asked:
            return self._testimony
        self._asked = True
        if self._verifier is None:
            return None
        # The album's own words, which is what makes one answer serve every
        # pressing, and what lets the witness be asked about an album that
        # nothing identified or nothing convinced.
        asking = ReleaseMetadata(
            source=MetadataSourceId("itunes"),
            source_release_id="",
            title=self._hints.album or self._hints.text,
            artists=(ArtistMetadata(name=self._hints.artist),) if self._hints.artist else (),
        )
        try:
            testimony = self._verifier.verify(
                asking, self._unit.track_durations_ms or (), self._titles
            )
        except Exception as error:
            # Not *could not be reached*: a `429` is the witness answering.
            # The type and the text are both recorded so that the log can tell
            # the two apart.
            self._logger.warning(
                "The witness could not be asked about this album.",
                extra={
                    "operation": "identification.witness.failure",
                    "why": type(error).__name__,
                    "said": str(error),
                },
            )
            return None
        self._testimony = dict(testimony) if testimony else None
        self._logger.info(
            "The witness was asked about this album in its own words.",
            extra={
                "operation": "identification.witness",
                "tracks": (self._testimony or {}).get("track_count"),
                "files": self._unit.track_count,
            },
        )
        return self._testimony

    def tracks(self) -> int | None:
        """Return how many tracks the witness says this album has, or ``None``."""
        witness = (self.testimony() or {}).get("track_count")
        return witness if isinstance(witness, int) else None


def _titles_agree(left: str, right: str) -> bool:
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio() >= 0.85


def _shortlist(
    summaries: tuple[ReleaseMetadata, ...], hints: SearchHints, limit: int
) -> tuple[ReleaseMetadata, ...]:
    """Choose which search results are worth fetching in full.

    A summary carries a title, and often a year, and nothing else useful. When
    every candidate shares the title, the year is the only thing that tells one
    pressing from another, so it dominates here — and unlike the final score,
    being wrong is cheap: it costs one request, not a wrong rename.
    """
    return tuple(
        sorted(
            summaries,
            key=lambda release: (
                -_year_agreement(release, hints.year),
                -_title_agreement(release, hints.album),
                release.source_release_id,
            ),
        )[:limit]
    )


def _year_agreement(release: ReleaseMetadata, year: int | None) -> float:
    if year is None or release.released_on is None:
        return 0.0
    distance = abs(release.released_on.year - year)
    return 1.0 if distance == 0 else (0.5 if distance <= _YEAR_TOLERANCE else -1.0)


def _title_agreement(release: ReleaseMetadata, album: str | None) -> float:
    if not album:
        return 0.0
    return SequenceMatcher(None, release.title.casefold(), album.casefold()).ratio()


def split_artist_credit(credit: str) -> tuple[ArtistMetadata, ...]:
    """Split a written credit into the values an `artist` tag holds.

    Players and DJ software group by artist, and one value holding two names
    groups as neither of them. So `Ana Lume, Beto Lume` becomes two values.

    **It cannot tell a separator from a name that contains one.** `Salt, Stone &
    Ember` becomes three values, and no rule fixes that without knowing the band —
    guessing which commas are punctuation is exactly what this project refuses to
    do elsewhere. The written credit is kept whole in `artist_credit`, so the file
    name always carries the credit as written; only the tag is split, and only a
    correction can put it back.

    An empty piece is dropped rather than becoming a nameless artist, and a credit
    with no separator in it stays one value, untouched.
    """
    pieces = [piece.strip() for piece in _CREDIT_SEPARATORS.split(credit)]
    return tuple(ArtistMetadata(name=piece) for piece in pieces if piece)


_CREDIT_SEPARATORS = re.compile(r"\s*(?:,|&)\s*")


def _arranged_sentence(reading: TagReading) -> str:
    """One line saying where an arrangement's words came from, never how sure it is.

    Written for the place ``explanation`` goes — the dialog and the
    identification row — so it has to stand alone: the values, the agreement,
    and the word *arranged*, which is the claim's honest size.
    """
    facts = [reading.artist, reading.album, str(reading.year or "")]
    agreement = (
        f"all {reading.files} files agree"
        if not reading.dissent
        else f"{len(reading.dissent)} of {reading.files} files say otherwise"
    )
    numbers = "the tags number the tracks" if reading.numbered else "the disk order numbers them"
    return (
        f"Arranged from this album's own tags — {' · '.join(part for part in facts if part)}; "
        f"{agreement}; {numbers}. No source was asked."
    )


def _folded(text: str) -> str:
    """Identity comparison: accents, case and stray punctuation set aside."""
    folded = fold_accents(text).casefold()
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in folded).split())
