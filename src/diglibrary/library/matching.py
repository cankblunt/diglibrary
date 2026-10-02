"""Deciding which release an album unit is, with an explanation for the answer."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from diglibrary.application.contracts import (
    MetadataSourceId,
    ReleaseMetadata,
    TrackMetadata,
    written_credit,
)
from diglibrary.library.models import AlbumUnit, AudioFileFacts
from diglibrary.metadata.normalization import fold_accents, normalize_text

DURATION_TOLERANCE_MS = 6_000
"""How far a track may differ and still count as the same track when scoring.

Measured against a real library rather than guessed. Catalogue durations are
typed by hand, often from a sleeve rather than from the audio, so they drift by
seconds: under a three-second rule one track a little over three seconds out
blocks an album whose every other track matched. Six seconds absorbs that and
stays far below the gap between tracks.
"""

ORDERED_TOLERANCE_MS = 15_000
"""How far a track may differ when the file order already corroborates the match.

Pairing and judging are different questions. When the files are already in the
release's order and the count agrees, the sequence itself is the evidence, and a
single sleeve time typed loosely should not throw the album out. This window is
only ever used with that corroboration, never to pair files freely.
"""

_UNVERIFIABLE_CEILING = 0.55
"""The most confidence a candidate can earn without per-track evidence.

Deliberately below any sensible automatic threshold: when a source gives no
track lengths and the titles cannot be compared either, the match is a guess
from text, and a guess is reviewed, never applied.
"""

_TITLE_VETO_CEILING = 0.15
"""The most confidence a candidate keeps when the track titles barely agree.

Both sides published names and almost none of them met: that is not this album,
and what its lengths happened to do is beside the point. An unrelated record
whose lengths coincide on a handful of tracks is the case this exists for. The
veto lifts when durations agree on half the tracks or more, which is the
untitled-files and foreign-title case.
"""

_TITLE_VETO_SHARE = 0.25
"""How much of an album's names must meet before the veto lets a candidate through.

Zero is the wrong edge. A compilation may legitimately carry one track by the
album's artist, and with a veto that fires only when no name agrees, that single
track lets the compilation through and the whole album is offered somebody
else's names.

A quarter was measured on a real library rather than chosen: wrong candidates
of that kind agree on a small share of the names, genuine identifications agree
on a much larger one, and a quarter sits inside the gap between the two.
"""

_VETO_DURATION_RESCUE = 0.5
"""How much duration agreement excuses a candidate whose titles all disagree."""

_DURATION_LIFT = 0.5
"""How far duration agreement may close the gap between the text score and 1.0.

Duration corroborates; it never convicts. Agreement lifts a candidate toward
certainty in proportion to the share of lengths that matched, and disagreement
is absent from the arithmetic: by design it costs nothing.
"""

_EDITION_PENALTY = 0.25
"""What a candidate loses when one side names an edition the other does not.

The guard that text-first scoring keeps against its own risk: text matches an
acoustic set and its studio original equally well, and duration does not
separate them once disagreement costs nothing. A marker on one side and not the
other is a disagreement in the text itself, so it is answered in the text.

Both directions are read, and the track titles as well as the album title. A
remixes compilation carries an album's songs under the album's names with
`(… Remix)` on each one while its own title says nothing, so reading the album
title alone misses it; and the marker may be on the catalogue's side only.
"""

_EDITION_MARKERS = (
    "ao vivo",
    "au vivo",
    "en vivo",
    "live",
    "unplugged",
    "acustico",
    "in concert",
    "mtv",
    "remix",
    "rmx",
)

_EDITION_MARKER = re.compile(r"\b(?:" + "|".join(_EDITION_MARKERS) + ")")
"""The markers, anchored at the start of a word and open at the end.

Anchored because this reads every track title rather than one album title,
and `live` inside *Delivery* or *Alive* would be an edition marker on nearly any
record. Open at the end so a plural such as `remixes` is the same word as its
singular.
"""

_EDITION_SENTENCES = {
    "local": "your files name an edition this release does not",
    "release": "this release names an edition your files do not",
    "each": "your files and this release name different editions",
}
"""What to say about an edition disagreement, by the side the marker is on.

Three sentences because the side is measured and there is no honest default: a
map that ends in one sentence for whatever it was not asked about tells a user
that their folder names a live edition when the marker is the catalogue's word
on one of the release's own tracks.
"""

_DIVERGENCE_PENALTY = 0.2
"""How much confidence a candidate loses when another source contradicts it."""

_VARIOUS_NAMES = frozenset({"various", "various artists", "va", "v.a.", "v/a"})
"""Credits that name no artist, so comparing them to one measures nothing."""

TITLE_MATCH_THRESHOLD = 0.8
"""How similar two titles must be to count as the same track when scoring.

Titles are typed by hand on both sides, so exact equality is too strict —
"Na-Na (Means I Need You)" and "Na Na Means I Need You" are one song. Below
this, agreement is noise: short titles collide too easily.
"""

TITLE_RESCUE_THRESHOLD = 0.85
"""How similar a title must be to pair a file no duration could place.

Higher than the scoring threshold because a rescue overrides the duration
evidence entirely: it exists for the sleeve-typo case, where the catalogue says
2:23 and the audio lasts 3:24 but the file is named exactly after the track.
A rescue is also required to be unique — one file, one track, no runner-up —
because renaming a file onto the wrong track is the worst mistake this project
can make.
"""

_DECORATED_STEM_THRESHOLD = 0.90
"""How closely two titles must agree once their decoration is removed.

Above both the scoring bar and the rescue bar, deliberately: a decoration hides
a suffix, not a different song, so what remains has to be nearly the same words
before the removal is allowed to count for anything.
"""

_TRAILING_BRACKET = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")
"""One trailing parenthetical or bracketed suffix: "(Bonus Track)", "[Live]"."""

_TRAILING_DASH_EDITION = re.compile(
    r"\s*[-\u2013\u2014]\s*(?:"
    r"b[ôo]nus(?:\s+track)?|bonus(?:\s+track)?|faixa\s+b[ôo]nus|"
    r"remix|remaster\w*|remasteriza\w*|ao\s+vivo|live|en\s+vivo|"
    r"radio\s+edit|edit|version|vers[\u00e3a]o|instrumental|single|ac\u00fastico|acustico"
    r")\s*$",
    re.IGNORECASE,
)
"""A dash followed by a known edition word — never arbitrary text."""

_TITLE_MARGIN = 0.05
"""How clearly a title must beat the runner-up before it settles a tie."""


@dataclass(frozen=True, slots=True)
class MatchSignals:
    """Purpose: record what the matcher actually observed, so a score can be audited.

    Responsibilities: carry the comparison counts and similarities behind one
    confidence value. Boundaries: it applies no weight and reaches no verdict.
    Dependencies: none. Collaborators: ``MatchCandidate`` and the review
    interface. Constraints: these are observations. Any value a verification
    source contributed appears here only as agreement or disagreement, never as
    the data compared, because that data may not be retained.
    """

    local_track_count: int
    release_track_count: int
    durations_compared: int
    durations_agreeing: int
    text_similarity: float
    titles_compared: int = 0
    titles_agreeing: int = 0
    verification_agreed: bool | None = None
    contradicted_by: tuple[MetadataSourceId, ...] = ()
    artist_similarity: float | None = None
    album_similarity: float | None = None
    year_agreement: float | None = None
    titles_in_order: bool = False
    edition_marker_side: str = ""
    """Which side names an edition the other says nothing about: ``"local"``,
    ``"release"``, ``"each"`` when both name a different one, or ``""`` for the
    ordinary case where nobody disagrees.

    The side, not a flag, because the sentence a person reads has to say where
    the word is: a flag can only produce one sentence, and that sentence blames
    the folder for a marker the catalogue published. A screen never names a
    cause it did not measure.
    """

    @property
    def edition_mismatch(self) -> bool:
        """Whether one side names an edition the other says nothing about."""
        return bool(self.edition_marker_side)

    @property
    def duration_score(self) -> float | None:
        """Return the share of tracks whose length matches, or ``None`` without evidence."""
        if self.durations_compared == 0:
            return None
        widest = max(self.local_track_count, self.release_track_count)
        return self.durations_agreeing / widest if widest else 0.0

    @property
    def title_score(self) -> float | None:
        """Return the share of tracks whose title matches, or ``None`` without evidence."""
        if self.titles_compared == 0:
            return None
        widest = max(self.local_track_count, self.release_track_count)
        return self.titles_agreeing / widest if widest else 0.0

    @property
    def track_count_agrees(self) -> bool:
        """Return whether both sides hold the same number of tracks."""
        return self.local_track_count == self.release_track_count


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """Purpose: pair one candidate release with a confidence and a readable reason.

    Responsibilities: carry the release, its score, the observations behind it,
    and a sentence a person can judge. Boundaries: it decides nothing — whether
    a candidate is applied or reviewed is a threshold decision made elsewhere.
    Dependencies: ``ReleaseMetadata`` and ``MatchSignals``. Collaborators: the
    identification store and the review interface. Constraints: the explanation
    is stored with the identification, so it must stay meaningful long after the
    comparison that produced it.
    """

    release: ReleaseMetadata
    confidence: float
    explanation: str
    signals: MatchSignals

    @property
    def vetoed_by_the_names(self) -> bool:
        """Whether the names refused this candidate outright.

        Asked of the candidate rather than recomputed by the caller, and
        answered by the same function the score itself uses: the query ladder
        walks on while every answer a rung brought back is vetoed, and a second
        statement of *what vetoed means* is how the two would drift apart.
        """
        return titles_veto(self.signals)


def titles_veto(signals: "MatchSignals") -> bool:
    """Whether both sides published names and almost none of them met.

    **The veto, in one place.** Whatever the lengths did by chance, a candidate
    the names refuse is not this album — unless durations agree broadly, which
    is the foreign-titles and untitled-files case, where the names measure
    nothing and the audio still speaks.

    Read as a share: at zero, one true track by the album's artist on an
    unrelated compilation is enough to let that compilation through. Two
    callers ask this — the score, and the query ladder deciding whether a rung
    answered anything — and they must never be able to disagree about what a
    refused candidate is.
    """
    duration = signals.duration_score
    return (
        signals.titles_compared > 0
        and signals.titles_agreeing / signals.titles_compared < _TITLE_VETO_SHARE
        and (duration is None or duration < _VETO_DURATION_RESCUE)
    )


class AlbumMatcher:
    """Purpose: rank candidate releases for one album unit the way a person would.

    Responsibilities: compare artist, album title, track titles and their order,
    year and track count, then let duration agreement corroborate the result,
    and produce an explained confidence per candidate. Boundaries: it performs no
    I/O, queries no source, and never decides to write — it is a pure function
    over what it is handed, which is what makes every scoring rule testable
    offline. Dependencies: canonical metadata models only. Collaborators: the
    identification workflow and the review interface. Constraints: text decides
    and duration only confirms; a candidate whose every track title
    disagrees is vetoed however well its lengths happened to line up.
    """

    def __init__(
        self,
        duration_tolerance_ms: int = DURATION_TOLERANCE_MS,
        ordered_tolerance_ms: int = ORDERED_TOLERANCE_MS,
        text_weight: float = 0.85,
        track_count_weight: float = 0.15,
        album_weight: float = 0.35,
        artist_weight: float = 0.25,
        title_weight: float = 0.30,
        year_weight: float = 0.10,
    ) -> None:
        """Create a matcher with the project's default signal weights."""
        self._tolerance = duration_tolerance_ms
        self._ordered_tolerance = ordered_tolerance_ms
        self._text_weight = text_weight
        self._track_count_weight = track_count_weight
        self._album_weight = album_weight
        self._artist_weight = artist_weight
        self._title_weight = title_weight
        self._year_weight = year_weight

    @property
    def duration_tolerance_ms(self) -> int:
        """Return the tolerance this matcher scores with.

        Alignment must use the same value: if scoring and pairing disagreed
        about what counts as the same track, a confident score could rest on a
        pairing the planner would refuse, or the reverse.
        """
        return self._tolerance

    @property
    def ordered_tolerance_ms(self) -> int:
        """Return the wider tolerance used only when the file order corroborates."""
        return self._ordered_tolerance

    def rank(
        self,
        unit: AlbumUnit,
        releases: tuple[ReleaseMetadata, ...],
        search_hint: str | None = None,
        local_titles: tuple[str, ...] | None = None,
        hint_artist: str | None = None,
        hint_album: str | None = None,
        hint_year: int | None = None,
    ) -> tuple[MatchCandidate, ...]:
        """Return every candidate scored and ordered, most confident first.

        The hints and ``local_titles`` are the disk's own reading of the album —
        hints, never truth — and they are the evidence the score is built
        from: artist, album, track titles and their order, year, and track
        count. Durations only corroborate what the text found.
        """
        hint = search_hint if search_hint is not None else unit.folder_path.name
        candidates = [
            self._score(unit, release, hint, local_titles, hint_artist, hint_album, hint_year)
            for release in releases
        ]
        return tuple(sorted(candidates, key=lambda candidate: _ordering(candidate, hint_year)))

    def _score(
        self,
        unit: AlbumUnit,
        release: ReleaseMetadata,
        hint: str,
        local_titles: tuple[str, ...] | None,
        hint_artist: str | None = None,
        hint_album: str | None = None,
        hint_year: int | None = None,
    ) -> MatchCandidate:
        local_durations = unit.track_durations_ms
        release_durations = tuple(
            track.duration_ms for track in release.tracks if track.duration_ms is not None
        )
        comparable = (
            local_durations is not None
            and len(release_durations) == len(release.tracks)
            and bool(release_durations)
        )
        agreeing = (
            self._agreeing_durations(local_durations or (), release_durations) if comparable else 0
        )
        titles_compared = 0
        titles_agreeing = 0
        in_order = False
        if local_titles and release.tracks:
            # An edition label one side stamped on every track and the other did
            # not. Removed before comparing, on both sides, because a tail every
            # track carries distinguishes none of them.
            compared_titles = _local_titles_for_release(local_titles, release.tracks)
            compared_tracks = _release_tracks_for_local(release.tracks, local_titles)
            titles_compared = len(compared_titles)
            titles_agreeing = _agreeing_titles(
                compared_titles, compared_tracks, album_credit(release)
            )
            in_order = _titles_in_order(compared_titles, compared_tracks, album_credit(release))
        signals = MatchSignals(
            local_track_count=unit.track_count,
            release_track_count=len(release.tracks),
            durations_compared=len(release_durations) if comparable else 0,
            durations_agreeing=agreeing,
            text_similarity=_similarity(hint, _release_text(release)),
            titles_compared=titles_compared,
            titles_agreeing=titles_agreeing,
            artist_similarity=_artist_similarity(hint_artist, release),
            album_similarity=(_similarity(hint_album, release.title) if hint_album else None),
            year_agreement=_year_agreement(hint_year, release),
            titles_in_order=in_order,
            edition_marker_side=_edition_marker_side(hint, hint_album, release, local_titles),
        )
        return MatchCandidate(
            release=release,
            confidence=self._confidence(signals),
            explanation=explain_evidence(signals),
            signals=signals,
        )

    def _agreeing_durations(
        self, local_durations: tuple[int, ...], release_durations: tuple[int, ...]
    ) -> int:
        """Count tracks whose length matches, ignoring the order they appear in.

        Both sequences are sorted before comparison because the order of files in
        a folder is part of the naming this project does not trust.
        """
        remaining = sorted(release_durations)
        agreeing = 0
        for duration in sorted(local_durations):
            for index, candidate in enumerate(remaining):
                if abs(candidate - duration) <= self._tolerance:
                    remaining.pop(index)
                    agreeing += 1
                    break
        return agreeing

    def _confidence(self, signals: MatchSignals) -> float:
        """Score a candidate the way a person reads a record.

        Text decides: artist, album title, track titles and their order, year,
        and track count compose the score. Duration agreement can only lift it
        toward 1.0; disagreement costs nothing, by design. Two hard edges guard
        the rule: names that barely agree are a veto, and no per-track title
        evidence at all keeps the unverifiable ceiling, because a match made of
        album-level text alone is still a guess.

        The edition penalty is subtracted here, once, for every candidate the
        veto let through. Written on one of the three readings instead, it
        misses the two that lift a score by duration, and a disagreement about
        the edition then costs nothing exactly where the lengths are doing all
        the work — while the explanation still announces the disagreement.

        The veto is the one terminal exit and stays outside: it is not a reading
        of the evidence but a refusal to read it, and its value is a ceiling
        rather than a score for anything to be subtracted from.
        """
        if titles_veto(signals):
            return round(min(_TITLE_VETO_CEILING, signals.text_similarity / 4), 4)
        confidence = self._read_the_evidence(signals)
        if signals.edition_mismatch:
            confidence -= _EDITION_PENALTY
        return round(max(0.0, min(1.0, confidence)), 4)

    def _read_the_evidence(self, signals: MatchSignals) -> float:
        """Compose text, track count and duration into one unrounded reading.

        Three readings, chosen by what the evidence actually is, and none of
        them applies a penalty or clamps a range: that belongs to the single
        exit in ``_confidence``, which is the whole point of this function
        existing.
        """
        track_count_score = 1.0 if signals.track_count_agrees else 0.0
        duration = signals.duration_score

        # Files named aaa.flac against tracks the catalogue names in full: when
        # the names met barely or not at all and the lengths agree broadly, the
        # local names are noise, not counter-evidence, and are left out of the
        # reading — the same judgement the veto's own escape hatch makes, and
        # read on the same share, so the two halves cannot part company.
        titles_are_noise = (
            signals.titles_compared > 0
            and signals.titles_agreeing / signals.titles_compared < _TITLE_VETO_SHARE
            and duration is not None
            and duration >= _VETO_DURATION_RESCUE
        )
        text_score = self._text_score(signals, ignore_titles=titles_are_noise)
        base = self._text_weight * text_score + self._track_count_weight * track_count_score
        if titles_are_noise:
            return base + (1.0 - base) * duration
        if signals.title_score is None:
            # No per-track evidence on the text side: album-level text is a
            # guess, and only duration agreement can raise it above the
            # ceiling a guess deserves. With no names to read, the lengths are
            # the only reading there is, so here — and only here — duration
            # keeps its full authority to confirm.
            if duration is None or duration <= 0:
                return min(_UNVERIFIABLE_CEILING, base)
            return base + (1.0 - base) * duration

        confidence = base
        if duration is not None and duration > 0:
            confidence += (1.0 - confidence) * _DURATION_LIFT * duration
        return confidence

    def _text_score(self, signals: MatchSignals, ignore_titles: bool = False) -> float:
        """Compose the text evidence into one number, renormalizing what is absent.

        A missing observation — no artist hint, no year on either side — leaves
        its weight out of the denominator instead of counting as zero, so an
        album is not punished for the folder's silence.
        """
        parts: list[tuple[float, float]] = []
        if signals.album_similarity is not None:
            parts.append((self._album_weight, signals.album_similarity))
        if signals.artist_similarity is not None:
            parts.append((self._artist_weight, signals.artist_similarity))
        if signals.title_score is not None and not ignore_titles:
            ordered_bonus = 0.1 if signals.titles_in_order else 0.0
            parts.append((self._title_weight, min(1.0, signals.title_score + ordered_bonus)))
        if signals.year_agreement is not None:
            parts.append((self._year_weight, signals.year_agreement))
        if not parts:
            return signals.text_similarity
        total_weight = sum(weight for weight, _ in parts)
        return sum(weight * value for weight, value in parts) / total_weight


def reconcile(
    primary: MatchCandidate | None,
    others: tuple[tuple[MetadataSourceId, MatchCandidate], ...],
) -> MatchCandidate | None:
    """Lower a winning candidate's confidence when another source contradicts it.

    The primary source's answer is kept, as configured, but a contradiction is
    never silent: it costs confidence, which is what pushes a disputed album
    below the threshold and into review with both versions shown.

    Only an evidence-backed answer can contradict. A source whose best
    candidate is a text guess — no durations compared, no titles agreeing —
    did not find the album; that is an absence, not a dispute, and punishing a
    duration-proven match for it sends correctly identified albums to review.
    """
    if primary is None:
        return None
    contradicting = tuple(
        source
        for source, candidate in others
        if _is_evidence_backed(candidate) and not _agrees(primary.release, candidate.release)
    )
    if not contradicting:
        return primary
    penalty = _DIVERGENCE_PENALTY * len(contradicting)
    signals = MatchSignals(
        local_track_count=primary.signals.local_track_count,
        release_track_count=primary.signals.release_track_count,
        durations_compared=primary.signals.durations_compared,
        durations_agreeing=primary.signals.durations_agreeing,
        text_similarity=primary.signals.text_similarity,
        titles_compared=primary.signals.titles_compared,
        titles_agreeing=primary.signals.titles_agreeing,
        verification_agreed=primary.signals.verification_agreed,
        contradicted_by=contradicting,
        artist_similarity=primary.signals.artist_similarity,
        album_similarity=primary.signals.album_similarity,
        year_agreement=primary.signals.year_agreement,
        titles_in_order=primary.signals.titles_in_order,
        edition_marker_side=primary.signals.edition_marker_side,
    )
    disputed = ", ".join(str(source) for source in contradicting)
    return MatchCandidate(
        release=primary.release,
        confidence=round(max(0.0, primary.confidence - penalty), 4),
        explanation=f"{primary.explanation} Contradicted by {disputed}.",
        signals=signals,
    )


def _is_evidence_backed(candidate: MatchCandidate) -> bool:
    """Report whether a candidate rests on per-track evidence rather than text."""
    return candidate.signals.durations_compared > 0 or candidate.signals.titles_agreeing > 0


def _agrees(primary: ReleaseMetadata, other: ReleaseMetadata) -> bool:
    """Report whether two sources are describing the same album.

    Only the title decides. Release years deliberately do not: two catalogues
    routinely surface different pressings of one album, and a reissue dated
    decades after the original is not a dispute about what the album is. The
    album's own year is settled from the master anyway.
    """
    return _similarity(primary.title, other.title) >= 0.85


def _release_text(release: ReleaseMetadata) -> str:
    artists = " ".join(artist.name for artist in release.artists)
    return f"{artists} {release.title}".strip()


_CONJUNCTIONS = frozenset({"e", "and", "y", "&", "+"})
"""The words that join two names, in the languages a library is commonly written in.

A duo written with a word on the folder and with `&` on the sleeve is the same
duo, and comparing the two spellings whole lowers the artist similarity of an
album that is plainly the right one. They are folded to one symbol for
comparison only; nothing written to disk passes through here.
"""

_WORD = re.compile(r"\w+|&|\+", re.UNICODE)
"""One word, in any script. `\\w` on purpose: a title in a script written without
spaces yields no word at all under an ASCII-only pattern, so several different
tracks reduce to the same empty word list and look like one song.
"""


def _words(text: str) -> tuple[str, ...]:
    """The words of a name, sorted, accent-blind, with conjunctions folded."""
    folded = fold_accents(normalize_text(text) or "").casefold()
    return tuple(sorted("&" if word in _CONJUNCTIONS else word for word in _WORD.findall(folded)))


def _same_words(left: str, right: str) -> bool:
    """Whether two names hold exactly the same words, in whatever order.

    Deliberately narrow: the *same* words, every one of them, and never an empty
    list. It says "these are one name written two ways", which is the honest
    reading of `Alpha Beta Gamma` against `Beta Gamma; Alpha` — the words are
    identical and only the order and a semicolon differ. Measured on real
    releases, two different tracks of one release did not share a whole word
    list, so this does not make one track look like its neighbour.
    """
    words = _words(left)
    return bool(words) and words == _words(right)


def _similarity(left: str, right: str) -> float:
    """Compare two names as a person reads them: accent-blind and case-blind.

    A name typed with its accent and the same name typed without it are one
    name spelled by two typists; compared raw they are merely similar, which is
    enough to lose an album its own artist match.

    Word order is not spelling, so the same words in another order score as the
    same name. The sequence comparison below cannot see that: it scores the
    same words in another order well under any bar and leaves a file loose that
    is plainly the track.
    """
    first = fold_accents(normalize_text(left) or "").casefold()
    second = fold_accents(normalize_text(right) or "").casefold()
    if not first or not second:
        return 0.0
    if _same_words(left, right):
        return 1.0
    return round(SequenceMatcher(None, first, second).ratio(), 4)


def _artist_similarity(hint_artist: str | None, release: ReleaseMetadata) -> float | None:
    """Compare the hinted artist with the release's credit, when both name someone.

    A compilation credited to Various on either side yields no observation:
    "Various" is a convention, not a name, and comparing it to one measures
    nothing.
    """
    if not hint_artist or not release.artists:
        return None
    credited = written_credit(release.artists)
    if (
        hint_artist.strip().casefold() in _VARIOUS_NAMES
        or credited.strip().casefold() in _VARIOUS_NAMES
    ):
        return None
    return _similarity(hint_artist, credited)


def _year_agreement(hint_year: int | None, release: ReleaseMetadata) -> float | None:
    """Score how the hinted year sits against the pressing's and the master's.

    A person accepts either reading: the folder may carry the master's year
    while the matched pressing is a reissue, and that is agreement, not doubt.
    Within a year counts as near-agreement, because catalogues and sleeves
    disagree about releases made at the turn of a year.
    """
    if hint_year is None:
        return None
    known = [
        released.year
        for released in (release.released_on, release.original_released_on)
        if released is not None
    ]
    if not known:
        return None
    distance = min(abs(year - hint_year) for year in known)
    if distance == 0:
        return 1.0
    if distance <= 1:
        return 0.8
    return 0.0


def _titles_in_order(
    local_titles: tuple[str, ...],
    release_tracks: tuple[TrackMetadata, ...],
    credit: str = "",
) -> bool:
    """Report whether the matching titles appear in the same order on both sides.

    Order is part of how a person reads a tracklist. Only the pairs that
    actually agree are consulted, so two bonus tracks at the end do not
    disqualify an otherwise ordered album.
    """
    if not local_titles or not release_tracks:
        return False
    contested = album_titles(release_tracks)
    matched_positions: list[int] = []
    used: set[int] = set()
    for local in local_titles:
        best_index: int | None = None
        best_similarity = 0.0
        for index, track in enumerate(release_tracks):
            if index in used:
                continue
            # Credit-aware like every other comparison. Reading the bare title
            # would leave the order signal blind on a compilation whose tags
            # spell `Artist - Title`.
            similarity = track_title_similarity(local, track, credit, contested)
            if similarity > best_similarity:
                best_similarity = similarity
                best_index = index
        if best_index is not None and best_similarity >= TITLE_MATCH_THRESHOLD:
            used.add(best_index)
            matched_positions.append(best_index)
    if len(matched_positions) < 2:
        return False
    return matched_positions == sorted(matched_positions)


def _edition_marker_side(
    hint: str,
    hint_album: str | None,
    release: ReleaseMetadata,
    local_titles: tuple[str, ...] | None = None,
) -> str:
    """Report which side names an edition the other says nothing about.

    Text matches an acoustic set and its studio original equally well, so a
    marker on one side and not the other is read as a disagreement in the text
    itself.

    Symmetric, and over the track titles as well. Read only from the folder
    against the release's title, it cannot see a remixes compilation: its own
    title names no edition, the marker sits on every one of its tracks, and
    most of its names agree with the original album because a remix album is
    made of that album's own song names.
    """
    local = _edition_markers(f"{hint} {hint_album or ''} " + " ".join(local_titles or ()))
    remote = _edition_markers(
        release.title + " " + " ".join(track.title or "" for track in release.tracks)
    )
    if not local ^ remote:
        return ""
    if local - remote and remote - local:
        return "each"
    return "local" if local - remote else "release"


def _edition_markers(text: str) -> frozenset[str]:
    """Which edition words this text uses, folded and anchored at a word start."""
    return frozenset(_EDITION_MARKER.findall(fold_accents(text).casefold()))


def title_similarity(local: str, release: str) -> float:
    """Compare two track titles, seeing past a decorated suffix.

    A title ending in a dash and an edition word and the same title ending in
    `(Bonus Track)` are one track written two ways, and comparing them whole
    scores below every bar, so an obvious pair becomes two orphans.

    The stem comparison only counts when a *recognized* decoration was actually
    removed and what remains agrees at ``_DECORATED_STEM_THRESHOLD``. The higher
    of the two similarities wins, so nothing that already matched can score
    lower for this.
    """
    raw = _similarity(local, release)
    local_stem, release_stem = _undecorated(local), _undecorated(release)
    if local_stem == local and release_stem == release:
        return raw
    stem = _similarity(local_stem, release_stem)
    return max(raw, stem) if stem >= _DECORATED_STEM_THRESHOLD else raw


def album_credit(release: ReleaseMetadata) -> str:
    """The release's own artists, as a tag would write them before a title."""
    return written_credit(release.artists)


def written_forms(track: TrackMetadata, credit: str = "") -> tuple[str, ...]:
    """Every way a file's own tag may spell this track's name.

    A `title` tag does not always hold the title alone. On some compilations
    every tag reads `Artist - Title` while the `artist` tag holds the label, so
    the performer exists nowhere else on the file, and it is the catalogue that
    publishes the bare title.

    ``credit`` is the album's own artists, used only when the track publishes
    none of its own.

    A guest is written before the title or after it, and both are the same
    record. A catalogue puts every artist in front — `First & Second & Third -
    Title` — while a file may lead with one and hang the others off the end,
    `First - Title Feat. Second & Third`. Compared with the two forms above,
    that scores under the title bar and far under the rescue bar, so the track
    is reported as having no file while the file sits unclaimed in the folder.

    So the guests are also written after the title, in the two places a name is
    read from: the file's name, which leads with one artist, and the file's
    `title` tag, which leads with none.

    Both are built from the source's own artist list rather than from any word:
    the first artist, the title, then the rest joined as the source joins them.
    `Feat.` is never written and never looked for — the separator could be
    anything, and the resemblance does not depend on it.

    Measured on a real library, the two added forms move no file's best match
    to another track and give no file a rival above the scoring bar.
    """
    written = written_credit(track.artists) or credit
    if not written:
        return (track.title,)
    forms = (track.title, f"{written} - {track.title}")
    lead = written_credit(track.artists[:1])
    guests = written_credit(track.artists[1:])
    if lead and guests:
        forms += (f"{lead} - {track.title} {guests}", f"{track.title} {guests}")
    return forms


def contested_stems(tracks: tuple[TrackMetadata, ...]) -> frozenset[str]:
    """The undecorated titles that more than one track of this release wears.

    A decoration is normally noise — the same song written `(Bonus Track)` here
    and `- Bônus` there — which is why ``title_similarity`` sees past it. On a
    release that publishes both a song and its instrumental, the decoration is
    the *only* thing that tells the two apart, and seeing past it makes them one
    name: both files score the same against both tracks, so nothing can claim
    either track and the whole album is blocked as ambiguous.

    Asked of the release rather than of a pair, because that is where the fact
    lives: `Song (instrumental)` is only indistinguishable while `Song` sits
    beside it on the same tracklist.
    """
    counted = Counter(_undecorated(track.title).casefold() for track in tracks)
    return frozenset(stem for stem, count in counted.items() if count > 1)


@dataclass(frozen=True, slots=True)
class AlbumTitles:
    """What a release's whole tracklist says about comparing one title to it.

    Two facts, and both are about the album rather than about any track, which
    is why they are read once and carried rather than worked out at each
    comparison: the stems more than one track wears (``contested_stems``), and
    the title each track is left with once the phrase every one of them carries
    is taken off (``_without_the_words_every_track_carries``).

    The second exists because pruning one side only is wrong in one case. The
    local titles have their shared phrase removed, which is right when the
    phrase is only on the files — every file reads `<song> - <edition>` and
    the catalogue names the songs plainly. When both sides carry it, which is
    what every album arranged from its own tags does because there the release
    is the tags, a file is compared by what remains of its name against the
    track's whole title and scores under the bar a veto needs. No title then
    vetoes anything, and a positional pairing over identical durations stands
    even when it gives the files each other's names.

    It answers ``in`` as a bare set of contested stems would, so a comparison
    that only asks whether a stem is contested needs nothing else.
    """

    contested: frozenset[str] = frozenset()
    pruned: Mapping[str, str] = field(default_factory=dict)

    def __contains__(self, stem: object) -> bool:
        """Answer as a bare set of contested stems would."""
        return stem in self.contested


NO_ALBUM_TITLES = AlbumTitles()
"""No tracklist to read these facts from, which is what a bare comparison has."""


def album_titles(tracks: tuple[TrackMetadata, ...]) -> AlbumTitles:
    """Read both album-wide facts a title comparison needs, in one pass.

    One function rather than two calls beside each other: a caller that took the
    contested stems and forgot the pruning would apply the rule by half, and
    every comparison in this module reaches the one or the other.
    """
    pruned = _without_the_words_every_track_carries(tuple(track.title for track in tracks))
    return AlbumTitles(
        contested=contested_stems(tracks),
        pruned={
            track.title: title
            for track, title in zip(tracks, pruned, strict=True)
            if title != track.title
        },
    )


def track_title_similarity(
    local: str,
    track: TrackMetadata,
    credit: str = "",
    contested: AlbumTitles = NO_ALBUM_TITLES,
) -> float:
    """Compare a local title against every way this track's name may be written.

    A local title spelled `Artist - Title (Decoration)` against a catalogue
    title spelled `Title (Decoration)` scores just under the rescue bar when
    compared bare, although the title itself differs by a capital letter at
    most; the file is then reported missing while it sits in the folder. With
    the credit folded in, the two compare as equal.

    Nothing is guessed. This never asks where a local title stops being an
    artist — it asks whether the local title reads as this track's own credit
    followed by this track's own name, and both halves come from the source. An
    album whose tracks carry no credit and whose release names no artist
    compares on the bare title alone.

    ``contested`` is what the whole tracklist says (see ``AlbumTitles``): the
    stems this release publishes twice, and the shortened form of this track's
    own name. The shortened form is a third written form rather than a
    substitute, because the phrase is shared only when both sides carry it.

    For a contested track the whole title is compared and the decoration
    counts, because it is what distinguishes them; everywhere else the
    decoration is seen past, as ``title_similarity`` does. On an album with a
    vocal and an instrumental take of one song this turns a four-way tie into
    two clear preferences, and every track pairs the right way round.
    """
    forms = written_forms(track, credit)
    # The same title with the phrase every track of this release carries taken
    # off — the form a local title that has already been pruned can be compared
    # with. Added to the forms rather than replacing them, because the phrase is
    # shared only when both sides carry it, and the unpruned form is what an
    # ordinary catalogue answers to.
    shortened = contested.pruned.get(track.title)
    if shortened:
        forms = (*forms, shortened)
    if _undecorated(track.title).casefold() in contested:
        return max(_similarity(local, form) for form in forms)
    return max(title_similarity(local, form) for form in forms)


def _undecorated(title: str) -> str:
    """Strip the trailing decorations a catalogue and a folder spell differently."""
    stem = title
    for _ in range(2):
        reduced = _TRAILING_DASH_EDITION.sub("", _TRAILING_BRACKET.sub("", stem)).strip()
        if reduced == stem or not reduced:
            break
        stem = reduced
    return stem


_TAIL_JOINERS = "-\u2013\u2014\u00b7:;,/|(["
"""What a shop puts between a name and the edition it stamped after it.

The dashes are written as escapes because three different characters look like
one on the page — hyphen, en dash, em dash — and a file carries whichever the
shop happened to type.
"""

SHARED_TAIL_MINIMUM_TITLES = 3
"""How many titles must carry a tail before it is read as an edition label.

Two titles ending alike is a coincidence a two-track single can produce; a
whole album doing it is a label the shop stamped on every line.
"""

SHARED_TAIL_MINIMUM_LETTERS = 4
"""How much of a tail must be word before it is worth removing.

Guards against trimming punctuation, a closing bracket, or a stray digit, none
of which is an edition and all of which are shared by accident.
"""


def _shared_title_tail(titles: Sequence[str]) -> str:
    """Return the ending every one of these titles shares, when it names none of them.

    A digital shop writes the edition into each track — `Song - New Mix 2015` —
    while a catalogue publishes the name alone. Compared whole, every title of
    such an album scores under the title bar, and the shorter the real name the
    worse it does, because a tail of fixed length weighs more against a short
    name than against a long one. No track pairs.

    Written by the complement, never by a list. Nothing here knows the words
    *remaster*, *live* or *mix*: a set spelled out by enumeration misses the
    next edition, which is spelled in a language nobody listed. What it knows
    is that a tail every track carries distinguishes none of them, so it cannot
    be part of any of their names.

    Empty unless the tail begins at a word boundary and leaves every title with
    something of its own: a tail that would blank a track, or leave the whole
    album saying one word, is not an edition and is refused.
    """
    if len(titles) < SHARED_TAIL_MINIMUM_TITLES or any(not title.strip() for title in titles):
        return ""
    folded = [title.casefold() for title in titles]
    shortest = min(len(title) for title in folded)
    length = 0
    while length < shortest and len({title[-(length + 1)] for title in folded}) == 1:
        length += 1
    while length:
        tail = titles[0][-length:]
        # A boundary, so `... Mix 2015` never becomes `...x 2015`, which would
        # leave a fragment of a word attached to a name it does not belong to.
        if tail[:1].isspace() or (len(titles[0]) > length and titles[0][-length - 1].isspace()):
            trimmed = [_without_tail(title, length) for title in titles]
            if (
                sum(character.isalpha() for character in tail) >= SHARED_TAIL_MINIMUM_LETTERS
                and all(trimmed)
                and len(set(trimmed)) > 1
                and all(map(_brackets_balanced, trimmed))
            ):
                return tail
        length -= 1
    return ""


def _brackets_balanced(title: str) -> bool:
    """Report whether every bracket this title opens, it also closes.

    The guard that a word boundary alone does not give. When every track ends
    `(<somebody> remix)`, the longest ending they all share is `remix)` — it
    begins after a space and it has five letters, and trimming it leaves
    `Song (Somebody`, a name with a bracket hanging open. A tail that cuts a
    structure in half is a fragment and not an edition, and the unclosed bracket
    is what says so without anything having to know the word *remix*.
    """
    depth = 0
    closing = {")": "(", "]": "[", "}": "{"}
    opening = set(closing.values())
    for character in title:
        if character in opening:
            depth += 1
        elif character in closing:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _without_tail(title: str, length: int) -> str:
    """Return the title without its shared ending, and without what joined it on.

    The separator goes with the tail. `Song - New Mix 2015` is `Song` and not
    `Song -`, because a dash left dangling is a character the catalogue never
    wrote either.
    """
    return title[: len(title) - length].strip().rstrip(_TAIL_JOINERS).strip()


def _local_titles_for_release(
    local_titles: tuple[str, ...], release_tracks: Sequence[TrackMetadata]
) -> tuple[str, ...]:
    """Return the local titles as this release would have written them.

    The tail is removed only when **this** release does not share it. Both sides
    stamped with the same edition is not the problem this solves — an album of
    `... (Live)` matched against a catalogue that also says `(Live)` pairs as it
    stands, and stripping one side of it would break that. The removal applies
    only where the two disagree.
    """
    tail = _shared_title_tail(local_titles)
    if not tail:
        return local_titles
    if tail.casefold() == _shared_title_tail([track.title for track in release_tracks]).casefold():
        return local_titles
    return tuple(_without_tail(title, len(tail)) for title in local_titles)


def _release_tracks_for_local(
    release_tracks: tuple[TrackMetadata, ...], local_titles: Sequence[str]
) -> tuple[TrackMetadata, ...]:
    """Return the release's tracks as the disk would have written them.

    The mirror of ``_local_titles_for_release``: a catalogue that stamps
    `(2011 Remaster)` on every line faces a folder that does not, and the
    failure is identical with the sides swapped. Only the title is
    rewritten; position, artists and credit are the release's own and are not
    this function's business.
    """
    tail = _shared_title_tail([track.title for track in release_tracks])
    if not tail:
        return release_tracks
    if tail.casefold() == _shared_title_tail(local_titles).casefold():
        return release_tracks
    return tuple(
        replace(track, title=_without_tail(track.title, len(tail))) for track in release_tracks
    )


def _agreeing_titles(
    local_titles: tuple[str, ...],
    release_tracks: tuple[TrackMetadata, ...],
    credit: str = "",
) -> int:
    """Count local titles that uniquely claim a release title.

    Greedy over the best similarity first, consuming both sides, so a release
    with two near-identical titles cannot be counted twice.
    """
    contested = album_titles(release_tracks)
    pairs = sorted(
        (
            (
                track_title_similarity(local, track, credit, contested),
                local_index,
                release_index,
            )
            for local_index, local in enumerate(local_titles)
            for release_index, track in enumerate(release_tracks)
        ),
        reverse=True,
    )
    taken_local: set[int] = set()
    taken_release: set[int] = set()
    agreeing = 0
    for similarity, local_index, release_index in pairs:
        if similarity < TITLE_MATCH_THRESHOLD:
            break
        if local_index in taken_local or release_index in taken_release:
            continue
        taken_local.add(local_index)
        taken_release.add(release_index)
        agreeing += 1
    return agreeing


CONCLUSIVE_SIMILARITY = 0.99
"""Above this, a similarity has nothing left to say and stops being printed.

`artist similarity 1.00` is *the names are the same*, which the sentence around
it already implies; `0.42` is the only kind of number here that explains how a
wrong record was arrived at, and that one is kept. Written as a threshold
rather than as `!= 1.00` because 0.997 is the same statement.
"""


def explain_evidence(signals: MatchSignals, said_elsewhere: frozenset[str] = frozenset()) -> str:
    """Narrate the evidence in the order it decided: text, then audio.

    ``said_elsewhere`` names the facts the caller's own screen already states, and
    is how this sentence says each thing once without there being two sentences to
    keep in agreement: it is computed once, here, and the answer goes in the
    payload. Recognised keys are ``titles``, ``track_count`` and ``durations``.
    The album dialog omits all three — the source panel gives the titles beside
    a link to check them against, the tags line gives the counts as
    `tracks 12 → 13` while a blocker gives them *naming* the track with no file,
    and the proof line under it is the lengths. Every other caller omits
    nothing, because nothing around it says these: a below-threshold reason and
    a candidate row are read on their own.

    A conclusive similarity is dropped for everybody, because `artist similarity
    1.00` is the word *yes* written as a number on any screen at all.
    """
    parts: list[str] = []
    if signals.titles_compared > 0 and "titles" not in said_elsewhere:
        ordered = ", in order" if signals.titles_in_order else ""
        parts.append(
            f"{signals.titles_agreeing} of {signals.titles_compared} track titles"
            f" matched{ordered}"
        )
    if signals.artist_similarity is not None and signals.artist_similarity < CONCLUSIVE_SIMILARITY:
        parts.append(f"artist similarity {signals.artist_similarity:.2f}")
    if signals.album_similarity is not None and signals.album_similarity < CONCLUSIVE_SIMILARITY:
        parts.append(f"album title similarity {signals.album_similarity:.2f}")
    if signals.year_agreement is not None:
        parts.append("year agrees" if signals.year_agreement >= 0.8 else "year disagrees")
    if "track_count" not in said_elsewhere:
        parts.append(
            f"track count matches at {signals.local_track_count}"
            if signals.track_count_agrees
            else f"track count differs ({signals.local_track_count} local, "
            f"{signals.release_track_count} on the release)"
        )
    if "durations" not in said_elsewhere:
        if signals.durations_compared > 0:
            parts.append(
                f"{signals.durations_agreeing} of {signals.durations_compared} track lengths "
                f"agreed within {DURATION_TOLERANCE_MS // 1000}s"
            )
        else:
            parts.append("no track lengths published by the source")
    if not signals.titles_compared and signals.artist_similarity is None:
        parts.append(f"name similarity {signals.text_similarity:.2f}")
    if signals.edition_marker_side:
        # A side this map has no sentence for says only what was measured — that
        # the two disagree — rather than picking one of the three and naming a
        # direction nobody read. Indexing would raise instead, and bringing an
        # identification down over the wording of one clause is the worse of the
        # two failures.
        parts.append(
            _EDITION_SENTENCES.get(
                signals.edition_marker_side,
                "your files and this release disagree about the edition",
            )
        )
    if not parts:
        # Everything this evidence had was stated by the screen asking for it,
        # which is an ordinary case for a clean album once the dialog's own
        # surfaces are subtracted. An empty string is the honest answer, and
        # the caller draws nothing.
        return ""
    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


@dataclass(frozen=True, slots=True)
class TrackAssignment:
    """Purpose: bind one local file to the release track it is believed to be.

    Responsibilities: carry the pairing, the disc and track numbers that follow
    from it, and how far the durations differed. Boundaries: it renames nothing
    and writes nothing. Dependencies: local facts and canonical track metadata.
    Collaborators: the change planner. Constraints: the numbers come from the
    release, never from the file's current name.
    """

    file: AudioFileFacts
    track: TrackMetadata
    disc_number: int
    track_number: int
    duration_delta_ms: int | None
    # Offered, not settled: this file fitted more than one track and nothing
    # could prove which. The pairing is still the best one available — nearest
    # name, then nearest length — but it is a suggestion, and nothing is ever
    # written from it until the user says so. `suggested` is a separate word
    # on purpose: `contested` already means *a title stem the tracklist prints
    # twice*, which is a fact about the release rather than about this pairing.
    suggested: bool = False
    # What binds this file to this track, in one word: `hand` when the user
    # said so, `name` when the file's own title agrees with the track's, and
    # `length` when nothing but the clock does. Read at the end of `align_tracks`
    # from the pair itself rather than from the rule that made it, because the
    # question the screen asks is not *how was this chosen* but *what evidence is
    # there* — a positional pairing whose names all agree is name evidence.
    #
    # The screen needs it because rows paired by nothing but a length within
    # the tolerance would otherwise look exactly like rows paired by name.
    paired_by: str = "length"


@dataclass(frozen=True, slots=True)
class TrackAlignment:
    """Purpose: report which local file is which track, and how sure that is.

    Responsibilities: hold the assignments, whatever stayed unmatched, and
    whether the pairing was forced. Boundaries: it makes no decision about
    writing. Dependencies: ``TrackAssignment``. Collaborators: the change
    planner and the review interface. Constraints: an alignment that is not
    complete and unambiguous must never be applied automatically — renaming a
    file to the wrong track is the most damaging mistake this project can make.
    """

    assignments: tuple[TrackAssignment, ...]
    method: str
    unmatched_files: tuple[AudioFileFacts, ...] = ()
    unmatched_tracks: tuple[TrackMetadata, ...] = ()
    is_ambiguous: bool = False

    @property
    def settled(self) -> tuple[TrackAssignment, ...]:
        """The pairings nothing is in doubt about, which are the only ones written."""
        return tuple(one for one in self.assignments if not one.suggested)

    @property
    def suggested(self) -> tuple[TrackAssignment, ...]:
        """The pairings offered among rivals nothing could tell apart."""
        return tuple(one for one in self.assignments if one.suggested)

    @property
    def is_usable(self) -> bool:
        """Return whether every file and every track paired up unambiguously."""
        return (
            not self.unmatched_files
            and not self.unmatched_tracks
            and not self.is_ambiguous
            and bool(self.assignments)
        )

    @property
    def is_partial(self) -> bool:
        """Return whether some files paired beyond doubt and others did not.

        This is what a user may apply by hand: real pairings exist, none of
        *them* is a guess, and what is left out is left out rather than written
        on a guess.

        A doubt about some tracks does not block the others. When two tracks
        share a title and sit a few seconds apart, the files nothing is unsure
        about are still planned: what is certain is applied, what is doubtful
        is left exactly as it was, and the album stays in review until the
        doubt is settled.
        """
        return bool(self.settled) and bool(
            self.unmatched_files or self.unmatched_tracks or self.suggested
        )


def align_tracks(
    unit: AlbumUnit,
    release: ReleaseMetadata,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
    ordered_tolerance_ms: int = ORDERED_TOLERANCE_MS,
    local_titles: tuple[str, ...] | None = None,
    pinned: Mapping[str, int] | None = None,
    published: ReleaseMetadata | None = None,
) -> TrackAlignment:
    """Pair each local file with its release track, and say what binds each pair.

    Positional pairing is tried first, in the file order on disk: when every
    pair already agrees on duration, the existing order is corroborated by the
    audio and is the safest reading, so it may use the wider window. Only when
    that fails does pairing fall back to nearest duration, which has no such
    corroboration and therefore uses the strict window.

    ``local_titles``, one per file, resolve what durations alone cannot: two
    release tracks of the same published length are told apart by name instead
    of blocking the album, and a file no duration could place is rescued when
    exactly one leftover track carries its title — the sleeve-typo case, where
    the catalogue's minutes are wrong and the name is right. Without titles the
    stricter, duration-only behavior stands.

    ``pinned`` maps a file's content signature to the release position the user
    says it holds. Those pairs are made first and are never scored: a pairing
    made by hand is a statement about the files, and no measurement here
    outranks it. Everything else is aligned by the ladder above, among the
    files and tracks the pinned pairs left over.

    ``published`` is the same release as the source published it, given when
    ``release`` carries names the user typed over the source's. A typed name is
    what will be written, and it is evidence only where a file answers to it
    (see ``_as_the_files_name_them``). The alignment returned names the tracks
    of ``release``, whichever of the two was compared.
    """
    heard = _as_the_files_name_them(release, published, local_titles)
    alignment = _what_binds_each_pair(
        _aligned(unit, heard, tolerance_ms, ordered_tolerance_ms, local_titles, pinned),
        unit,
        heard,
        local_titles,
        pinned,
    )
    return alignment if heard is release else _naming_the_tracks_of(release, alignment)


def _as_the_files_name_them(
    release: ReleaseMetadata,
    published: ReleaseMetadata | None,
    local_titles: tuple[str, ...] | None,
) -> ReleaseMetadata:
    """Return the release with each corrected track under the name the files agree with.

    A name typed over the source's replaces it in what is written. Compared in
    its place as well, it turns a correction into evidence against the file it
    was typed for: the file still carries the source's name, answers to no
    track, and the track is reported as having no file. And compared never, it
    fails the other way, because an album applied with a correction holds files
    named by it, which the source's name no longer matches.

    So a corrected track is compared under whichever of its two names the files
    in this folder agree with more, and the typed one where they agree equally:
    it is the user's statement, and two titles typed across each other are how
    a user says which file is which. A track nothing was typed over is not
    touched, so an album without corrections is aligned exactly as before.

    Decided per track and from the whole folder, never per pair: a name is a
    property of the track, and choosing it pair by pair would let one track
    answer to both names at once and tie with its neighbour.
    """
    if published is None or not local_titles:
        return release
    spoken = tuple(title for title in _without_the_words_every_track_carries(local_titles) if title)
    as_published = {track.position: track for track in published.tracks}
    if (
        not spoken
        or None in as_published
        or any(track.position is None for track in release.tracks)
    ):
        return release
    typed_names, typed_credit = album_titles(release.tracks), album_credit(release)
    source_names, source_credit = album_titles(published.tracks), album_credit(published)

    def agreement(track: TrackMetadata, credit: str, names: AlbumTitles) -> float:
        return max(track_title_similarity(title, track, credit, names) for title in spoken)

    tracks = tuple(
        (
            source
            if (source := as_published.get(track.position)) is not None
            and _names_differ(track, source)
            and agreement(source, source_credit, source_names)
            > agreement(track, typed_credit, typed_names)
            else track
        )
        for track in release.tracks
    )
    if all(now is before for now, before in zip(tracks, release.tracks, strict=True)):
        return release
    return replace(release, tracks=tracks)


def _names_differ(track: TrackMetadata, source: TrackMetadata) -> bool:
    """Whether anything a file's name is compared with was typed over this track."""
    return (
        track.title != source.title
        or track.artists != source.artists
        or track.artist_credit != source.artist_credit
    )


def _naming_the_tracks_of(release: ReleaseMetadata, alignment: TrackAlignment) -> TrackAlignment:
    """Return the alignment carrying this release's own tracks, matched by position.

    The pairing may have been found under the source's names, and what is
    planned and drawn from it is the release with the typed ones.
    """
    named = {track.position: track for track in release.tracks}
    return replace(
        alignment,
        assignments=tuple(
            replace(one, track=named.get(one.track.position, one.track))
            for one in alignment.assignments
        ),
        unmatched_tracks=tuple(
            named.get(track.position, track) for track in alignment.unmatched_tracks
        ),
    )


def _aligned(
    unit: AlbumUnit,
    release: ReleaseMetadata,
    tolerance_ms: int,
    ordered_tolerance_ms: int,
    local_titles: tuple[str, ...] | None,
    pinned: Mapping[str, int] | None,
) -> TrackAlignment:
    """Walk the ladder and return the pairing, with nothing said about evidence."""
    files = unit.audio_files
    tracks = release.tracks
    if not files or not tracks:
        return TrackAlignment((), "none", files, tracks, is_ambiguous=bool(files or tracks))
    if pinned:
        return _aligned_by_hand(
            unit, release, tolerance_ms, ordered_tolerance_ms, local_titles, pinned
        )

    credit = album_credit(release)
    positional = _positional_alignment(
        files,
        tracks,
        max(tolerance_ms, ordered_tolerance_ms),
        local_titles,
        credit,
    )
    if positional is not None:
        return positional
    # `credit` above is the album's own artists, for the tracks that publish
    # none of their own: a tag that spells `Artist - Title` is compared against
    # the same shape rather than against a bare title it cannot match.
    named = _titled_alignment(files, tracks, local_titles, credit, tolerance_ms)
    if named is not None:
        return named
    titled = _titled_positional_alignment(files, tracks, local_titles, credit)
    if titled is not None:
        return titled
    return _duration_alignment(files, tracks, tolerance_ms, local_titles, credit)


def _aligned_by_hand(
    unit: AlbumUnit,
    release: ReleaseMetadata,
    tolerance_ms: int,
    ordered_tolerance_ms: int,
    local_titles: tuple[str, ...] | None,
    pinned: Mapping[str, int],
) -> TrackAlignment:
    """Honour the pairings made by hand, and align what is left over around them.

    A pairing made by hand is a fact about the files, not a candidate to be
    scored, so it is never compared, never contested and never rescued away.
    What it does is remove one file and one track from the question: the ladder
    then runs on the remainder, which is why pairing the one file a duplicate
    title had deadlocked can also settle the tracks around it.

    Two things are deliberately tolerated rather than raised. A signature that
    names no file here — the folder changed since the pairing was made — and a
    position the release does not have simply do not pair, because a stored
    pairing outliving its file must not stop an album from being read at all.
    The window is what reports them, since it is the only place they can be
    acted on.
    """
    tracks_by_position = {track.position: track for track in release.tracks}
    spoken = _without_the_words_every_track_carries(local_titles or ())
    titles = dict(zip(unit.audio_files, spoken, strict=True)) if local_titles else {}
    assignments: list[TrackAssignment] = []
    # The files a pinned pairing took, held by identity and not by signature.
    # A pairing names an audio, and a folder may hold that audio twice: one
    # recording, byte for byte, on a release that publishes the same length
    # for two of its tracks. Held by signature, the second file would leave
    # the table with the first — neither paired nor loose — and the track it
    # fits would have nothing to be offered. One position per signature is
    # still what `pinned` can say; the file it did not reach stays on the
    # table for the ladder.
    spoken_files: set[int] = set()
    spoken_tracks: set[int] = set()
    for file in unit.audio_files:
        position = pinned.get(file.content_signature)
        if position is None or position in spoken_tracks:
            continue
        track = tracks_by_position.get(position)
        if track is None:
            continue
        assignments.append(_assign(file, track, _delta(file, track)))
        spoken_files.add(id(file))
        spoken_tracks.add(position)

    remaining_files = tuple(file for file in unit.audio_files if id(file) not in spoken_files)
    remaining_tracks = tuple(
        track for track in release.tracks if track.position not in spoken_tracks
    )
    if not remaining_files or not remaining_tracks:
        # Nothing left to decide. This is stated rather than delegated, because
        # `align_tracks` reads an empty side as an album it cannot read at all
        # and calls it ambiguous — which would block the very apply the pinned
        # pairings just made possible.
        rest = TrackAlignment((), "none", remaining_files, remaining_tracks)
    else:
        rest = align_tracks(
            replace(unit, audio_files=remaining_files),
            replace(release, tracks=remaining_tracks),
            tolerance_ms=tolerance_ms,
            ordered_tolerance_ms=ordered_tolerance_ms,
            local_titles=(
                tuple(titles.get(file, "") for file in remaining_files) if titles else None
            ),
        )
    together = sorted(
        [*assignments, *rest.assignments], key=lambda assignment: assignment.track.position
    )
    return TrackAlignment(
        assignments=tuple(together),
        method="by hand" if not rest.assignments else f"by hand+{rest.method}",
        unmatched_files=rest.unmatched_files,
        unmatched_tracks=rest.unmatched_tracks,
        is_ambiguous=rest.is_ambiguous,
    )


def _positional_alignment(
    files: tuple[AudioFileFacts, ...],
    tracks: tuple[TrackMetadata, ...],
    tolerance_ms: int,
    local_titles: tuple[str, ...] | None = None,
    credit: str = "",
) -> TrackAlignment | None:
    """Pair in the order they already sit in, when every length agrees.

    A name still outranks the order: text decides and duration only
    corroborates, in every pairing path. The sequence is evidence, and it is
    weaker evidence than a file that says in so many words which track it is:
    if any file's own title clearly names a *different* track of this release,
    the order is not corroboration, it is a coincidence of lengths, and the
    named paths below get their turn instead.
    """
    if len(files) != len(tracks):
        return None
    spoken = _without_the_words_every_track_carries(local_titles or ())
    contested = album_titles(tracks) if local_titles else NO_ALBUM_TITLES
    interchangeable = _pairs_a_length_reads_the_same_either_way(files, tracks)
    assignments: list[TrackAssignment] = []
    unproven = False
    for index, (file, track) in enumerate(zip(files, tracks, strict=True)):
        delta = _delta(file, track)
        if delta is None or delta > tolerance_ms:
            return None
        if spoken and _title_names_another(spoken[index], track, list(tracks), credit, contested):
            return None
        named = bool(spoken) and (
            track_title_similarity(spoken[index], track, credit, contested) >= TITLE_MATCH_THRESHOLD
        )
        # A length that fits two tracks holds neither of them. The order on
        # disk is corroborated by the audio only where an exchange would cost
        # something; where it reads exactly as well, what decided was the order
        # the folder happened to be read in. The row is offered rather than
        # settled, and an ambiguous alignment is what every guard downstream
        # already refuses to apply on its own.
        offered = index in interchangeable and not named
        if offered:
            unproven = True
        # Marked on the row, not only on the album: `suggested` is what names the
        # files in the refusal and draws the `?` beside them, and a blocked
        # album that cannot say which rows blocked it is the count without the
        # table.
        assignments.append(_assign(file, track, delta, suggested=offered))
    return TrackAlignment(tuple(assignments), "positional", is_ambiguous=unproven)


def _pairs_a_length_reads_the_same_either_way(
    files: tuple[AudioFileFacts, ...], tracks: tuple[TrackMetadata, ...]
) -> frozenset[int]:
    """Which positions another file could take without the durations noticing.

    An album whose files pair at 200, 300, 800 and 3200 ms, where any swap
    lands seven to nine seconds out, is corroborated by the audio and earns the
    wider window. An album of four files and four tracks of one length is not:
    every swap reads exactly as well as the order does — nothing is
    corroborated, and the answer is the order the folder happened to be read
    in.

    So the question is not whether two lengths are close. It is whether some
    exchange is **as good as** the reading on offer: that is the one case where
    the reading cannot claim the audio chose it.
    """
    fragile: set[int] = set()
    for index, (file, track) in enumerate(zip(files, tracks, strict=True)):
        here = _delta(file, track)
        if here is None:
            continue
        for other, (swapped_file, swapped_track) in enumerate(zip(files, tracks, strict=True)):
            if other == index:
                continue
            mine = _delta(file, swapped_track)
            theirs = _delta(swapped_file, track)
            there = _delta(swapped_file, swapped_track)
            if mine is None or theirs is None or there is None:
                continue
            if max(mine, theirs) <= max(here, there):
                fragile.update((index, other))
    return frozenset(fragile)


def _titled_alignment(
    files: tuple[AudioFileFacts, ...],
    tracks: tuple[TrackMetadata, ...],
    local_titles: tuple[str, ...] | None,
    credit: str = "",
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> TrackAlignment | None:
    """Pair every file with the track its own title unequivocally names.

    The title decides and the duration confirms. If a name were consulted only
    when two published lengths compete for one file, a single length in range
    would be taken on trust: a catalogue whose minutes are wrong for one track
    then moves that file onto its neighbour's name, orphans the neighbour, and
    reports the right track as missing.

    This runs before duration pairing and demands everything: every file names a
    track above the threshold, with a clear margin over its runner-up, and the
    pairing covers every file and every track. Anything less falls through to the
    duration path, whose own guard refuses a pairing the title contradicts.
    Lengths are still recorded on every assignment, so a disagreement is reported
    as evidence rather than silently deciding the album.
    """
    if not local_titles or len(local_titles) != len(files) or len(files) != len(tracks):
        return None
    contested = album_titles(tracks)
    chosen: dict[int, int] = {}
    for index, title in enumerate(local_titles):
        if not title.strip():
            return None
        scored = sorted(
            (
                (track_title_similarity(title, track, credit, contested), position)
                for position, track in enumerate(tracks)
            ),
            reverse=True,
        )
        best_similarity, best_position = scored[0]
        if best_similarity < TITLE_MATCH_THRESHOLD:
            return None
        if len(scored) > 1 and scored[1][0] > best_similarity - _TITLE_MARGIN:
            return None
        chosen[index] = best_position
    if len(set(chosen.values())) != len(tracks):
        return None
    # Every file named a track, so nothing is loose — the only swap possible
    # here is between two named pairs that refute each other's lengths and
    # agree crosswise. The same rule as the duration path's, applied where this
    # path makes its pairs, so that both paths implement it.
    named = [(files[index], tracks[position]) for index, position in sorted(chosen.items())]
    kept, swapped = _swaps_two_lengths_settle(
        named, (), (), dict(zip(files, local_titles, strict=True)), tolerance_ms
    )
    read_at = {id(file): index for index, file in enumerate(files)}
    pairs = sorted([*kept, *swapped], key=lambda pair: read_at[id(pair[0])])
    return TrackAlignment(
        tuple(_assign(file, track, _delta(file, track)) for file, track in pairs),
        "titled",
    )


def _titled_positional_alignment(
    files: tuple[AudioFileFacts, ...],
    tracks: tuple[TrackMetadata, ...],
    local_titles: tuple[str, ...] | None,
    credit: str = "",
) -> TrackAlignment | None:
    """Pair files with tracks in order, on the strength of every title agreeing.

    Order plus name is exactly what a person reads off a sleeve. This runs
    only when the release publishes no lengths at all — a published length
    keeps its authority over pairing — and it demands agreement on *every* pair,
    because a single doubtful name in an otherwise ordered album is exactly the
    file that must not be renamed by momentum.
    """
    if not local_titles or len(files) != len(tracks) or len(local_titles) != len(files):
        return None
    if any(track.duration_ms is not None for track in tracks):
        return None
    contested = album_titles(tracks)
    assignments: list[TrackAssignment] = []
    for file, title, track in zip(files, local_titles, tracks, strict=True):
        if track_title_similarity(title, track, credit, contested) < TITLE_MATCH_THRESHOLD:
            return None
        assignments.append(_assign(file, track, None))
    return TrackAlignment(tuple(assignments), "titled-positional")


def _duration_alignment(
    files: tuple[AudioFileFacts, ...],
    tracks: tuple[TrackMetadata, ...],
    tolerance_ms: int,
    local_titles: tuple[str, ...] | None = None,
    credit: str = "",
) -> TrackAlignment:
    titles = (
        dict(zip(files, _without_the_words_every_track_carries(local_titles), strict=True))
        if local_titles
        else {}
    )
    contested = album_titles(tracks)
    remaining = list(tracks)
    assignments: list[TrackAssignment] = []
    unmatched_files: list[AudioFileFacts] = []
    used_titles = False
    # A contested choice and the rivals it was made against. Whether it was
    # really a choice cannot be known while the loop runs: a rival still on the
    # table here may be claimed, by its own name, by a file read later.
    #
    # Named apart from the ``contested`` stems above, which is a fact about the
    # release rather than about this loop. Sharing the name would overwrite the
    # stems with an empty list one line after they are computed.
    contested_choices: list[tuple[TrackMetadata, ...]] = []
    # The files whose pairing was picked out of rivals rather than proved. Held
    # by identity because two files of one album can be equal in every field
    # this compares.
    suggested_files: set[int] = set()

    # The names go first, before any clock is read. The guard in the loop below
    # refuses a pairing whose file names a *different* track, but it asks that
    # of the tracks still on the table, so it is answered by the order the
    # files happen to be read in: a file read early can take, on length alone,
    # the track a later file is named after, and by the time that later file is
    # read the track it names is gone and nothing contradicts pairing it with
    # another.
    #
    # Taken here, the pairs a name settles cost the loop below nothing and can
    # not be taken by a length read earlier.
    named = [
        (file, track)
        for file, track in _pairs_the_names_settle(files, tracks, titles, credit, contested)
        if track in remaining
    ]
    # Two lengths overrule one name. A name can settle a pair that the clock
    # refutes by most of a minute: a file named after a song is the remix, and
    # the file of the song's own length sits beside it named after its remixer.
    # Where the file fits exactly one loose track and exactly one loose file
    # fits the track it was named to, the name is not corroborated: it is
    # contradicted twice, and the pair of lengths that agree is what gets
    # taken.
    named_files = {id(file) for file, _ in named}
    named_tracks = {id(track) for _, track in named}
    kept, swapped = _swaps_two_lengths_settle(
        named,
        [file for file in files if id(file) not in named_files],
        [track for track in remaining if id(track) not in named_tracks],
        titles,
        tolerance_ms,
    )
    for file, track in [*kept, *swapped]:
        remaining.remove(track)
        assignments.append(_assign(file, track, _delta(file, track)))
    used_titles = bool(named)
    decided_before_the_loop = {id(assignment.file) for assignment in assignments}
    # The order files were read in, so the assignments can be given back in it.
    # Taking the named pairs first puts them at the head of the list otherwise,
    # and this function answers in the order of the folder.
    read_at = {id(file): index for index, file in enumerate(files)}

    for file in files:
        if id(file) in decided_before_the_loop:
            continue
        within = [
            track
            for track in remaining
            if (delta := _delta(file, track)) is not None and delta <= tolerance_ms
        ]
        if not within:
            unmatched_files.append(file)
            continue
        best: TrackMetadata | None = None
        if len(within) > 1:
            best = _settled_by_title(titles.get(file), within, credit, contested)
            if best is not None:
                used_titles = True
        if best is None and len(within) > 1:
            # The number the file already carries, before anything is guessed.
            # It is the strongest evidence available here: two tracks may be
            # titled alike and sit seconds apart, but if the file says `01` and
            # exactly one of the rivals is position 1, that is not a tie at all.
            best = _settled_by_number(file, within, titles.get(file))
        if best is None:
            best = _nearest_of(titles.get(file), within, file)
            if len(within) > 1:
                contested_choices.append(tuple(track for track in within if track is not best))
                suggested_files.add(id(file))
        if _title_names_another(titles.get(file), best, remaining, credit, contested):
            # The length fits and the name says otherwise, so the length is
            # wrong: this is the published-minutes error, and taking the pairing
            # would write a neighbour's name onto this audio *and* orphan the
            # file that track belongs to. Leaving both loose costs a review; the
            # rescue below usually pairs them by name anyway.
            unmatched_files.append(file)
            continue
        remaining.remove(best)
        assignments.append(_assign(file, best, _delta(file, best)))

    if titles:
        rescued, unmatched_files, remaining = _rescue_by_title(
            unmatched_files, remaining, titles, credit, contested
        )
        if rescued:
            used_titles = True
            assignments.extend(rescued)

    # A tie is only a tie while the other candidate is still available — but
    # only a NAME may take it off the table (text decides, length confirms).
    # A rival freed by elimination is freed by the very guess in question, and
    # two coin flips do not settle each other: two takes of one song, both
    # titled alike, stay ambiguous.
    spoken_for = {
        id(assignment.track)
        for assignment in assignments
        if _names_this_track_alone(
            titles.get(assignment.file), assignment.track, tracks, credit, contested
        )
    }
    ambiguous = any(id(rival) not in spoken_for for rivals in contested_choices for rival in rivals)

    # Which pairings are offered rather than settled. Both sides of a doubt are
    # marked, not just the file that happened to be read first, for the reason
    # given above: a rival freed by elimination is freed by the very guess in
    # question. Marking only the chooser would let the second file of a true
    # tie look certain merely because the first had already taken the other
    # track, and its name would be written on a coin flip.
    disputed = {id(rival) for rivals in contested_choices for rival in rivals if ambiguous}
    settled_by_name = {
        id(assignment.track) for assignment in assignments if id(assignment.track) in spoken_for
    }
    offered = {
        index
        for index, assignment in enumerate(assignments)
        if ambiguous
        and (id(assignment.file) in suggested_files or id(assignment.track) in disputed)
        and id(assignment.track) not in settled_by_name
    }
    assignments = [
        replace(assignment, suggested=True) if index in offered else assignment
        for index, assignment in enumerate(assignments)
    ]
    assignments.sort(key=lambda assignment: read_at.get(id(assignment.file), 0))

    return TrackAlignment(
        assignments=tuple(assignments),
        method="duration+title" if used_titles else "duration",
        unmatched_files=tuple(unmatched_files),
        unmatched_tracks=tuple(remaining),
        is_ambiguous=ambiguous,
    )


def _ordering(candidate: MatchCandidate, hint_year: int | None) -> tuple[object, ...]:
    """Order candidates so that two equally confident pressings settle the same way.

    The code is deterministic; the *source's* ordering of equally confident
    results is not, so without a tie-break the winner could change between
    scans. The year written on the folder wins first, because a folder named
    for a pressing names the pressing it holds, and what the user wrote
    outranks the catalogue. Failing that, the earliest pressing — the album
    rather than a reissue. Failing that, the release id, which says nothing
    about the record but says the same thing forever.
    """
    release = candidate.release
    pressing = release.released_on.year if release.released_on else None
    written_year_agrees = hint_year is not None and pressing == hint_year
    return (
        -candidate.confidence,
        not written_year_agrees,
        pressing if pressing is not None else 9999,
        release.source_release_id,
    )


def _what_binds_each_pair(
    alignment: TrackAlignment,
    unit: AlbumUnit,
    release: ReleaseMetadata,
    local_titles: tuple[str, ...] | None,
    pinned: Mapping[str, int] | None,
) -> TrackAlignment:
    """Say of every pair whether a name, the user's hand, or only a length holds it.

    Read from the pair rather than from the rule that made it, and in one place
    rather than at each `_assign` call: what the screen asks of a row is what
    evidence there is for it, and a pairing made in the file order whose names
    all agree has name evidence — the rule that chose it is not the answer to
    that question.
    """
    if not alignment.assignments:
        return alignment
    credit = album_credit(release)
    contested = album_titles(release.tracks)
    spoken = _without_the_words_every_track_carries(local_titles or ())
    titles = (
        dict(zip(unit.audio_files, spoken, strict=True))
        if local_titles and len(local_titles) == len(unit.audio_files)
        else {}
    )
    by_hand = dict(pinned or {})

    def binding(assignment: TrackAssignment) -> str:
        # A pinned pairing is about one position: a second file of the same
        # audio, paired by the ladder to another track, was not paired by hand.
        if by_hand.get(assignment.file.content_signature) == assignment.track.position:
            return "hand"
        title = titles.get(assignment.file)
        if title and (
            track_title_similarity(title, assignment.track, credit, contested)
            >= TITLE_MATCH_THRESHOLD
        ):
            return "name"
        return "length"

    return replace(
        alignment,
        assignments=tuple(
            replace(assignment, paired_by=binding(assignment))
            for assignment in alignment.assignments
        ),
    )


def _pairs_the_names_settle(
    files: tuple[AudioFileFacts, ...],
    tracks: tuple[TrackMetadata, ...],
    titles: Mapping[AudioFileFacts, str],
    credit: str,
    contested: AlbumTitles,
) -> tuple[tuple[AudioFileFacts, TrackMetadata], ...]:
    """Return the pairs the names decide on their own, before any length is read.

    A pair is settled only when it is unambiguous from **both** sides: this file
    names this track above the matching threshold and with a clear margin over
    its runner-up, and no other file names that same track as well. Anything less
    is left to the ladder — a name that two files claim decides nothing, and this
    must never be the place a doubtful pairing is made silently.

    A title the tracklist prints twice is excluded by `contested`, which is what
    keeps two takes of one song out of here.
    """
    if not titles:
        return ()
    claims: dict[int, list[tuple[float, AudioFileFacts]]] = {}
    for file in files:
        title = titles.get(file)
        if not title or not title.strip():
            continue
        scored = sorted(
            (
                (track_title_similarity(title, track, credit, contested), index)
                for index, track in enumerate(tracks)
            ),
            reverse=True,
        )
        if not scored:
            continue
        best, index = scored[0]
        if best < TITLE_MATCH_THRESHOLD:
            continue
        if len(scored) > 1 and scored[1][0] > best - _TITLE_MARGIN:
            continue
        claims.setdefault(index, []).append((best, file))
    return tuple(
        (claimants[0][1], tracks[index])
        for index, claimants in sorted(claims.items())
        if len(claimants) == 1
    )


def _swaps_two_lengths_settle(
    named: Sequence[tuple[AudioFileFacts, TrackMetadata]],
    loose_files: Sequence[AudioFileFacts],
    loose_tracks: Sequence[TrackMetadata],
    titles: Mapping[AudioFileFacts, str],
    tolerance_ms: int,
) -> tuple[list[tuple[AudioFileFacts, TrackMetadata]], list[tuple[AudioFileFacts, TrackMetadata]]]:
    """Withdraw a name-settled pair that two lengths refute, and return the swap.

    Text decides and duration corroborates, and that rule stands here untouched
    for a name that is unequivocal: where the catalogue's minutes are wrong and
    the name is right, a file far from the published length still keeps the
    track it is named after. What is withdrawn is one narrow shape, proved from
    every side: the file fits **exactly one** loose track within the window,
    **exactly one** loose file fits the track the name had claimed, and **all
    four wear one title** — the claimed track's own stem begins the loose
    file's name and the other track's name. That is one song in two versions,
    with the source and the catalogue calling the plain one and the decorated
    one the other way round; a name that two files wear was never unequivocal,
    and the two lengths that agree crosswise are what tell the versions apart,
    the same reading a contested stem is given.

    A loose item is one no name settled, or one half of another refuted pair.
    A half taken out of another pair brings its partner along — the two pairs
    are re-made crosswise — or it is not taken: dissolving a pair to rescue a
    third is how a swap turns into a chain nobody can read.

    Returns the pairs that stand and the pairs the swap made, in that order.
    """

    def within(file: AudioFileFacts, track: TrackMetadata) -> bool:
        delta = _delta(file, track)
        return delta is not None and delta <= tolerance_ms

    def wears(title: str, stem: str) -> bool:
        spoken, stem = title.casefold().strip(), stem.casefold().strip()
        if not stem or not spoken.startswith(stem):
            return False
        rest = spoken[len(stem) :].lstrip()
        return rest == "" or rest[0] in _TAIL_JOINERS

    refuted = [
        (file, track)
        for file, track in named
        if (delta := _delta(file, track)) is not None and delta > tolerance_ms
    ]
    if not refuted:
        return list(named), []
    partner_file = {id(track): file for file, track in refuted}
    partner_track = {id(file): track for file, track in refuted}
    pool_files = [*loose_files, *(file for file, _ in refuted)]
    pool_tracks = [*loose_tracks, *(track for _, track in refuted)]
    taken: set[int] = set()
    swapped: list[tuple[AudioFileFacts, TrackMetadata]] = []
    for file, track in refuted:
        if id(file) in taken or id(track) in taken:
            continue
        other_tracks = [
            candidate
            for candidate in pool_tracks
            if candidate is not track and id(candidate) not in taken and within(file, candidate)
        ]
        other_files = [
            candidate
            for candidate in pool_files
            if candidate is not file and id(candidate) not in taken and within(candidate, track)
        ]
        if len(other_tracks) != 1 or len(other_files) != 1:
            continue
        other_track, other_file = other_tracks[0], other_files[0]
        if partner_file.get(id(other_track), other_file) is not other_file:
            continue
        if partner_track.get(id(other_file), other_track) is not other_track:
            continue
        stem = _undecorated(track.title)
        if not (
            wears(titles.get(file, ""), stem)
            and wears(titles.get(other_file, ""), stem)
            and wears(other_track.title, stem)
        ):
            continue
        swapped.extend(((file, other_track), (other_file, track)))
        taken.update((id(file), id(track), id(other_file), id(other_track)))
    kept = [(file, track) for file, track in named if id(file) not in taken]
    return kept, swapped


def _title_names_another(
    title: str | None,
    chosen: TrackMetadata,
    remaining: list[TrackMetadata],
    credit: str = "",
    contested: AlbumTitles = NO_ALBUM_TITLES,
) -> bool:
    """Report whether a file's own title clearly names some track other than ``chosen``.

    The bar is the rescue threshold rather than the scoring one, because this
    overrules a published length: a vague resemblance is not enough to refuse a
    pairing the audio supports. A file whose title says nothing recognisable —
    ``Track 03`` — names nothing and so vetoes nothing.
    """
    if not title:
        return False
    if track_title_similarity(title, chosen, credit, contested) >= TITLE_MATCH_THRESHOLD:
        return False
    return any(
        track is not chosen
        and track_title_similarity(title, track, credit, contested) >= TITLE_RESCUE_THRESHOLD
        for track in remaining
    )


_SEPARATORS = " -\u2013\u2014\u00b7|/,:;"
"""What a shared phrase hangs from, in the forms file names commonly use."""


def _without_the_words_every_track_carries(titles: Sequence[str]) -> tuple[str, ...]:
    """Drop the phrase every one of these titles begins or ends with.

    A phrase carried by every track distinguishes nothing. When every file's
    title reads `<song> - <edition phrase>` and the release names its tracks
    plainly, each title scores under the title bar, nothing is spoken for by
    name, and the album falls back to duration alone — which can pair files
    with their neighbours' tracks wherever two lengths sit a few seconds apart.

    Derived from the album rather than from a list of words like "remaster" or
    "live": a word list is a set written by enumerating the members somebody
    already knew, and the next pressing says it in a language nobody listed.

    Never applied when it would leave a title empty — an album whose tracks
    really do share their whole name is the one that must stay ambiguous.
    """
    words = [title.split() for title in titles]
    if len(words) < 3 or any(not part for part in words):
        return tuple(titles)

    def shared(at_end: bool) -> int:
        count = 0
        while True:
            step = count + 1
            if any(len(part) <= step for part in words):
                return count
            piece = words[0][-step] if at_end else words[0][step - 1]
            here = (part[-step] if at_end else part[step - 1] for part in words)
            if not all(_same_words(word, piece) for word in here):
                return count
            count = step

    tail, head = shared(at_end=True), shared(at_end=False)
    if not tail and not head:
        return tuple(titles)
    # The separator the shared phrase hung from goes with it: `Song -` is not
    # a title, and the dash left dangling costs the comparison as much as the
    # words did.
    return tuple(
        " ".join(part[head : len(part) - tail]).strip(_SEPARATORS) or title
        for part, title in zip(words, titles, strict=True)
    )


def _names_this_track_alone(
    title: str | None,
    track: TrackMetadata,
    tracks: tuple[TrackMetadata, ...],
    credit: str = "",
    contested: AlbumTitles = NO_ALBUM_TITLES,
) -> bool:
    """Report whether this file's title names this track and no other on the release.

    Uniqueness is asked of the whole release rather than of one file's shortlist:
    a name that fits two tracks names neither, and it is precisely the album with
    two takes of one song that must stay ambiguous.
    """
    if not title:
        return False
    mine = track_title_similarity(title, track, credit, contested)
    if mine < TITLE_MATCH_THRESHOLD:
        return False
    return all(
        track_title_similarity(title, other, credit, contested) <= mine - _TITLE_MARGIN
        for other in tracks
        if other is not track
    )


def _settled_by_title(
    title: str | None,
    within: list[TrackMetadata],
    credit: str = "",
    contested: AlbumTitles = NO_ALBUM_TITLES,
) -> TrackMetadata | None:
    """Pick the one track a file's own title clearly names, or admit a tie.

    Used only among tracks whose durations already fit: the title selects
    between otherwise equal claims, it never overrides the audio.
    """
    if title is None:
        return None
    scored = sorted(
        (
            (track_title_similarity(title, track, credit, contested), index)
            for index, track in enumerate(within)
        ),
        reverse=True,
    )
    best_similarity, best_index = scored[0]
    if best_similarity < TITLE_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and scored[1][0] > best_similarity - _TITLE_MARGIN:
        return None
    return within[best_index]


def _rescue_by_title(
    orphan_files: list[AudioFileFacts],
    orphan_tracks: list[TrackMetadata],
    titles: dict[AudioFileFacts, str],
    credit: str = "",
    contested: AlbumTitles = NO_ALBUM_TITLES,
) -> tuple[list[TrackAssignment], list[AudioFileFacts], list[TrackMetadata]]:
    """Pair leftover files with leftover tracks by name, when the name is unequivocal.

    This is the sleeve-typo escape: the pairing ignores duration entirely, so
    it demands a high similarity and a clear margin over every competing pair.
    Anything short of that stays an orphan and the album stays in review.
    """
    rescued: list[TrackAssignment] = []
    files = list(orphan_files)
    tracks = list(orphan_tracks)
    progressed = True
    while progressed and files and tracks:
        progressed = False
        pairs = sorted(
            (
                (
                    track_title_similarity(titles.get(file, ""), track, credit, contested),
                    file_index,
                    track_index,
                )
                for file_index, file in enumerate(files)
                for track_index, track in enumerate(tracks)
            ),
            reverse=True,
        )
        for similarity, file_index, track_index in pairs:
            # A track with a published length was orphaned because that length
            # disagreed, so overriding it demands the higher bar. A track with
            # no length at all has nothing to override — the title is the only
            # evidence there is, and the scoring threshold applies.
            floor = (
                TITLE_RESCUE_THRESHOLD
                if tracks[track_index].duration_ms is not None
                else TITLE_MATCH_THRESHOLD
            )
            if similarity < floor:
                continue
            # Named apart from the ``contested`` stems above, which is a fact
            # about the release: this one asks whether some *other* pairing
            # claims this file or this track just as strongly.
            has_a_rival = any(
                other_similarity > similarity - _TITLE_MARGIN
                for other_similarity, other_file, other_track in pairs
                if (other_file, other_track) != (file_index, track_index)
                and (other_file == file_index or other_track == track_index)
            )
            if has_a_rival:
                # This pair stays unresolved, but a clear pair further down
                # must not be lost with it.
                continue
            file = files.pop(file_index)
            track = tracks.pop(track_index)
            rescued.append(_assign(file, track, _delta(file, track)))
            progressed = True
            break
    return rescued, files, tracks


def _assign(
    file: AudioFileFacts,
    track: TrackMetadata,
    delta: int | None,
    suggested: bool = False,
) -> TrackAssignment:
    return TrackAssignment(
        file=file,
        track=track,
        disc_number=track.medium_number or file.disc_hint or 1,
        track_number=track.position_on_medium or track.position or 0,
        duration_delta_ms=delta,
        suggested=suggested,
    )


_STANDALONE_NUMBER = re.compile(r"(?:^|[\s\-_.\[(])(\d{1,3})(?=[\s\-_.\])]|$)")
"""Every standalone one-to-three digit group in a name, wherever it sits.

Not anchored to the front, because a file may be named `Artist - Album - 01 -
Title.flac`, with the number in the middle; a front-anchored pattern passes a
test written with `01 - …` and finds nothing in such a name. Which of the
numbers found is the track number is not decided here: `_settled_by_number`
asks the *release* that, which is what keeps the 40 of `Club 40 Years` from
answering."""


def _numbered(file: AudioFileFacts, title: str | None) -> set[int]:
    """Return the track number this file already carries, if any.

    Read from the stripped title first. In a file named `Artist - Album - 01 -
    Title.flac` the number sits in the middle, behind an artist and an album,
    so a rule that reads the front of the file name finds nothing. What
    `_without_the_words_every_track_carries` hands back has already had the
    words every track repeats taken off it, which leaves `01 - Title` — the
    number at the front, where it can be read.
    """
    found: set[int] = set()
    for text in (title, file.path.name):
        if text:
            found.update(int(one) for one in _STANDALONE_NUMBER.findall(text))
    return found


def _settled_by_number(
    file: AudioFileFacts, within: list[TrackMetadata], title: str | None
) -> TrackMetadata | None:
    """Pick the one rival whose position is the number this file already carries.

    The strongest evidence this function has: two tracks may be titled alike
    and sit seconds apart, but if the file says `01` and exactly one of the
    rivals is position 1, there was never a tie.

    Only among rivals a duration already admitted, which is what keeps it
    honest. A file name's number was written by whoever numbered the files
    before and may belong to another edition entirely — so it is allowed to
    choose between candidates the audio has already vouched for, and never to
    reach past them. Exactly one must match; two matches, or none, and it
    declines rather than picks.
    """
    numbers = _numbered(file, title)
    if not numbers:
        return None
    # The *release* decides which of the numbers found is the track number, by
    # being the only one a rival answers to. A title reading `Club 40 Years`
    # offers a 40 that no rival here is, and it simply never matches.
    hits = [
        track for track in within if numbers & {track.position, track.position_on_medium} - {None}
    ]
    if len(hits) != 1:
        return None
    # The name has to agree first. A file is called `01.flac` because it is
    # the first file in the folder, not because anybody checked it is track one.
    # A file named `01.flac` whose own title reads `Something Else Entirely`
    # may sit between two tracks it matches neither of, and letting its number
    # choose turns the folder's ordering into evidence and renames on a coin
    # flip. So the number only ever separates rivals the *title* already admits
    # this file could be.
    if title is None:
        return None
    # Compared against what follows the number, not against the whole line.
    # In `Album - 01 - Title` the album and the number in front drag the
    # similarity with a track titled `Title` under the bar, and the guard above
    # would refuse the very album this function exists to settle.
    spoken = _after_the_number(title, hits[0])
    return hits[0] if title_similarity(spoken, hits[0].title) >= TITLE_MATCH_THRESHOLD else None


def _after_the_number(title: str, track: TrackMetadata) -> str:
    """Return what a local title says after the number that named this track.

    The number is a label on the name, not part of it: what has to agree with
    the catalogue is the words beside it.
    """
    for number in {track.position, track.position_on_medium} - {None}:
        for found in _STANDALONE_NUMBER.finditer(title):
            if int(found.group(1)) != number:
                continue
            tail = title[found.end() :].lstrip(" -_.)]")
            if tail:
                return tail
    return title


def _nearest_of(
    title: str | None, within: list[TrackMetadata], file: AudioFileFacts
) -> TrackMetadata:
    """Pick the likeliest of several tracks a file fits, for offering only.

    Reached when the durations all fit and the *name* could not settle it, so
    what is returned is a suggestion and its caller marks it as one.

    Name first, then length, which is the order every pairing follows. The
    similarity here is deliberately the one that reads the whole written title
    rather than `track_title_similarity`, which sees past decorations on a
    contested stem — and the decoration is the entire question at this point.
    A release printing `Song` and `Song (instrumental)` gives a file called
    `Song (instrumental)` a plain reason to prefer the second, where the
    undecorated comparison makes them the same word four ways over.

    Falls back to the nearest length when there is no local title to read.
    """

    def rank(track: TrackMetadata) -> tuple[float, int]:
        nearness = -title_similarity(title, track.title) if title else 0.0
        return (nearness, _delta(file, track) or 0)

    return min(within, key=rank)


def _delta(file: AudioFileFacts, track: TrackMetadata) -> int | None:
    if file.properties is None or track.duration_ms is None:
        return None
    return abs(file.properties.duration_ms - track.duration_ms)
