"""The benchmark that ships, checked against the application it reaches into.

`benchmark/` is published code: three scripts that fabricate a paired corpus on
somebody else's machine, read it with this application's own analyzer, and score
this application's own rule over what they read. Nothing tested any of it: no
file under `tests/` so much as named the folder, and every way it can break is a
way that is invisible here.

It breaks by *reaching*. `measure.py` names `FfmpegQualityAnalyzer` and seven
properties of a spectral profile; `score.py` names `track_is_transcoded` and
calls it with four arguments in one order; a hypothesis script names a window
table. A rename on this side leaves both scripts importable and the suite green,
because nothing here imports them — and fails on the machine of the one person
who followed the README.

And it breaks by *drifting*. `measure.py` writes eight tab-separated fields and
`score.py` unpacks four of them **by position**, with no header between them to
disagree with. Swap two columns and nothing raises: every row still parses,
every arm still scores, and the rule is handed a decay where a ceiling belongs.
The table that comes out is wrong in silence, which is the one failure a
benchmark must not have.

So there are three questions here, and they are independent: does every name the
benchmark reaches still exist, does what one script writes mean what the next
one reads, and does the recipe still fabricate every arm the scorer expects.
"""

import ast
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from diglibrary.quality.models import BandEnergy, SpectralProfile

PROJECT = Path(__file__).resolve().parent.parent.parent
BENCHMARK = PROJECT / "benchmark"

# The probe frequencies the analyzer reads, so a profile built here has the
# shape of one that came off a file rather than a shape invented for a test.
PROBE_FREQUENCIES = (13_000, 14_000, 15_000, 16_000, 17_500, 19_000, 20_000, 21_500)

# What `score.py` calls each position of the row `measure.py` writes. The first
# field is the file name and the three left out are read by nothing, which is
# why they are the ones a reorder would hide behind.
WHAT_EACH_POSITION_HOLDS = {
    1: "cutoff_hertz",
    3: "floor_from_hertz",
    5: "decay_db",
    6: "ceiling_db",
}


def _load(name: str) -> ModuleType:
    """Import one benchmark script under a name of its own.

    `benchmark/` is not a package and its scripts are called `measure` and
    `score`, which are names anything could take. They are loaded by path and
    registered as `benchmark_measure` and friends, so nothing in the suite can
    end up importing one of them by accident later.
    """
    spec = importlib.util.spec_from_file_location(f"benchmark_{name}", BENCHMARK / f"{name}.py")
    assert spec and spec.loader, f"benchmark/{name}.py is not there"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _profile(levels: tuple[float, ...]) -> SpectralProfile:
    """One spectral profile, built from the real bands rather than described."""
    bands = zip(PROBE_FREQUENCIES, levels, strict=True)
    return SpectralProfile(bands=tuple(BandEnergy(hertz, db) for hertz, db in bands))


# A master that runs out of treble the way records do, and a file walled at
# 16 kHz with nothing above it. They are here to be told apart: the rule says
# False for the first and True for the second, so a round trip that reads the
# wrong columns stops agreeing with it.
HONEST = _profile((-30.0, -33.0, -36.0, -39.0, -44.0, -50.0, -56.0, -62.0))
WALLED = _profile((-30.0, -32.0, -34.0, -36.0, -120.0, -120.0, -120.0, -120.0))


class _Analysis:
    def __init__(self, spectral: SpectralProfile) -> None:
        self.spectral = spectral


class _StandInAnalyzer:
    """Answers with one profile, so the row can be produced without a file."""

    answer = HONEST

    def __init__(self, *_: object) -> None:
        pass

    def analyze(self, _: Path) -> _Analysis:
        return _Analysis(self.answer)


def _row_for(profile: SpectralProfile, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The line `measure.py` would write for one profile, split into fields."""
    measure = _load("measure")
    monkeypatch.setattr(_StandInAnalyzer, "answer", profile)
    monkeypatch.setattr(measure, "FfmpegQualityAnalyzer", _StandInAnalyzer)
    monkeypatch.setattr(measure, "SubprocessCommandRunner", lambda *_: None)
    return measure.measure_one(Path("000-original.flac"), "ffmpeg").split("\t")


def test_every_name_the_benchmark_reaches_into_the_application_exists() -> None:
    """Every `from diglibrary… import …` in the published scripts still resolves.

    Written over whatever is in the folder rather than over the three scripts the
    README documents, so a script added later is covered by having been added.
    """
    crossings: list[tuple[Path, str, str]] = []
    for source in sorted(BENCHMARK.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("diglibrary"):
                crossings.extend((source, node.module or "", alias.name) for alias in node.names)

    # A check that found nothing to check looks exactly like a check that passed.
    assert crossings, "no crossing into the application was found under benchmark/"

    # A script states the same import twice — once for an installed package and
    # once for a checkout — so a missing name is reported by where it is reached
    # from and not once per branch of the fallback.
    missing = {
        f"{source.relative_to(PROJECT)}: {module_name}.{name}"
        for source, module_name, name in crossings
        if not hasattr(importlib.import_module(module_name), name)
    }
    assert not missing, "the benchmark reaches names the application no longer has: " + "; ".join(
        sorted(missing)
    )


def test_the_three_scripts_name_the_same_arms() -> None:
    """The scorer's arms and the measurer's arms are one set, not two lists."""
    measure, score = _load("measure"), _load("score")
    assert set(measure.ARMS) == {arm for arm, _ in score.ARMS}
    assert len(score.ARMS) == len({arm for arm, _ in score.ARMS}), "an arm is listed twice"


def test_what_measure_writes_is_what_score_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each position the scorer unpacks holds the quantity it calls it by.

    Asserted against the profile's own properties, so a column that moves is
    named in the failure rather than showing up as a verdict that drifted.
    """
    row = _row_for(WALLED, monkeypatch)
    assert len(row) == 8, f"the row is {len(row)} fields, and the scorer reads eight"
    assert row[0] == "000-original.flac"

    for position, attribute in WHAT_EACH_POSITION_HOLDS.items():
        value = getattr(WALLED, attribute)
        expected = str(value) if isinstance(value, int) or value is None else f"{value:.2f}"
        assert (
            row[position] == expected
        ), f"field {position} should hold {attribute} ({expected}) and holds {row[position]!r}"


def test_the_scorer_convicts_the_row_the_measurer_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row written by one script and scored by the other keeps its verdict.

    The two profiles disagree on purpose: a scorer reading the wrong columns can
    still answer, and what it stops doing is agreeing with the rule.
    """
    score = _load("score")
    verdicts = {}
    for name, profile in (("honest", HONEST), ("walled", WALLED)):
        row = _row_for(profile, monkeypatch)
        tsv = tmp_path / f"eng-{name}.tsv"
        tsv.write_text("\t".join(row) + "\n", encoding="utf-8")

        convicted, measured, errors = score.score_arm(tsv)
        assert (measured, errors) == (1, 0), f"{name}: the scorer could not read the row it got"

        # Reached through the scorer, which is how the published script reaches
        # it: the question is whether those two agree, not whether this file can
        # import the rule for itself.
        expected = score.track_is_transcoded(
            profile.cutoff_hertz, profile.decay_db, profile.ceiling_db, profile.floor_from_hertz
        )
        assert convicted == int(expected), f"{name}: the scorer disagrees with the shipped rule"
        verdicts[name] = convicted

    assert (
        verdicts["honest"] != verdicts["walled"]
    ), "both profiles scored alike; nothing was told apart"


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("lame")),
    reason="the recipe needs ffmpeg and lame, which the README asks for and this machine lacks",
)
def test_the_recipe_fabricates_exactly_the_arms_the_scorer_expects(tmp_path: Path) -> None:
    """The three commands of the README, run in order over two seconds of audio.

    The source is fabricated here from a seeded generator, so the corpus is this
    machine's and nobody's library, and it is short because what is asserted is
    the machinery and never the verdicts: two seconds of noise says nothing about
    a rule, and a test that read a verdict off one would be scoring the ruler
    against a toy.
    """
    measure, score = _load("measure"), _load("score")
    source = tmp_path / "source.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=d=2:c=white:r=44100:a=0.3:seed=1",
            "-ac",
            "1",
            "-sample_fmt",
            "s16",
            "-y",
            str(source),
        ],
        check=True,
    )
    listing = tmp_path / "sources.txt"
    listing.write_text(f"{source}\n", encoding="utf-8")
    arms, results = tmp_path / "arms", tmp_path / "results"

    for command in (
        [
            sys.executable,
            str(BENCHMARK / "build_arms.py"),
            "--from-list",
            str(listing),
            "--outdir",
            str(arms),
        ],
        [
            sys.executable,
            str(BENCHMARK / "measure.py"),
            "--arms",
            str(arms),
            "--results",
            str(results),
        ],
        [sys.executable, str(BENCHMARK / "score.py"), "--results", str(results)],
    ):
        done = subprocess.run(command, capture_output=True, encoding="utf-8", text=True)
        assert done.returncode == 0, f"{Path(command[1]).name} refused: {done.stderr.strip()}"

    fabricated = {path.name.split("-", 1)[1].removesuffix(".flac") for path in arms.glob("*.flac")}
    assert fabricated == set(measure.ARMS), (
        "the recipe and the measurer disagree about which arms exist: "
        f"only fabricated {sorted(fabricated - set(measure.ARMS))}, "
        f"only expected {sorted(set(measure.ARMS) - fabricated)}"
    )

    for arm in measure.ARMS:
        rows = (results / f"eng-{arm}.tsv").read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1, f"{arm}: one source should make one row, and made {len(rows)}"
        assert "\tERR\t" not in rows[0], f"{arm}: the analyzer could not read what the recipe made"
        _, measured, errors = score.score_arm(results / f"eng-{arm}.tsv")
        assert (measured, errors) == (1, 0), f"{arm}: the scorer could not read the row"
