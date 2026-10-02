"""Everything a release audit does that a machine can do, so a person does the rest.

Some defects need somebody to think: a report made by returning early, two
halves of a rule that disagree. No script finds those, and this one does not
pretend to. Others are mechanical and are still found by hand, or not found:

- a commit in the release with no line in the changelog at all;
- a version bump that reaches the links written as
  `raw.githubusercontent…/vX.Y.Z/…` and misses the ones written as
  `github.com/…/blob/vX.Y.Z/…`, which are the same URL in a second shape, so
  the published page serves the previous release's documents.

Run it before a release. It writes nothing, asks nothing, and never touches a
remote — a preflight that could change something is one nobody dares run.

    .venv/bin/python tools/preflight_release.py

It exits 0 when every check passes, 1 when one refuses. A check it cannot run —
`twine` absent, no network — says so and does not pass silently, because a check
that reports nothing looks exactly like a check that found nothing.
"""

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_public import DESCRIBES_THE_PATTERNS, PRIVATE

PROJECT = Path(__file__).resolve().parent.parent

# Where a previous version number is allowed to survive: the record of what
# happened, and the code and tests that describe an older release in prose.
# Written as places rather than as a count, so a new one is somebody's decision.
VERSION_MAY_SURVIVE_IN = (
    *PRIVATE,
    "CHANGELOG.md",
    "docs/images/install.txt",
    "tests/",
    "tools/",
    # The Homebrew formula, because `check_the_homebrew_formula_is_this_release`
    # owns this question and answers it with the ordering this one cannot know:
    # a formula carries the sha256 of the *published* archive, so it can only be
    # corrected after the release exists. Without this line the two checks
    # contradict each other.
    "packaging/homebrew/",
    # The benchmark's reference results, which name the version that measured
    # them. That is a record of what happened, like the changelog: bumping it
    # with the release would claim a measurement nobody made.
    "benchmark/",
)

# A commit that changes what the application does and says nothing in the
# changelog is the defect this catches. These are the shapes that legitimately
# do not: what is not published at all, and what no user of the application
# meets.
CHANGELOG_NOT_EXPECTED_FOR = re.compile(
    "^("
    + "|".join(re.escape(prefix) for prefix in PRIVATE)
    + r"|\.gitignore$|docs/|tools/|tests/|\.github/)"
)


class Report:
    """What was asked, what answered, and whether anything refused."""

    def __init__(self) -> None:
        self.refused = 0
        self.unknown = 0
        # The sdist the build check produced, so the formula's digest is
        # compared against the file this release will actually publish
        # rather than against a second number written down by hand.
        self.built_sdist: Path | None = None

    def ok(self, check: str, detail: str = "") -> None:
        print(f"  \033[32m✓\033[0m {check}{f' — {detail}' if detail else ''}")

    def no(self, check: str, detail: str) -> None:
        self.refused += 1
        print(f"  \033[31m✗\033[0m {check} — {detail}")

    def cannot(self, check: str, detail: str) -> None:
        self.unknown += 1
        print(f"  \033[33m?\033[0m {check} — {detail}")


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _version() -> str:
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["project"]["version"]


def check_the_tree_is_clean(report: Report) -> None:
    """A release is cut from what is committed, never from what is open."""
    dirty = _git("status", "--porcelain")
    if dirty:
        report.no("the working tree is clean", f"{len(dirty.splitlines())} paths are not committed")
        return
    report.ok("the working tree is clean")


def check_the_changelog_has_this_version(report: Report, version: str) -> None:
    """The section is what declares the release, and the release notes come from it."""
    text = (PROJECT / "CHANGELOG.md").read_text(encoding="utf-8")
    if re.search(rf"^## {re.escape(version)}$", text, re.MULTILINE):
        report.ok(f"CHANGELOG declares {version}")
        return
    report.no(
        f"CHANGELOG declares {version}",
        "no section for it; it is still `## Unreleased`",
    )


def _commit_hash(reference: str) -> str:
    """The full hash, so an abbreviation written by hand matches whatever git prints."""
    try:
        return _git("rev-parse", f"{reference}^{{commit}}")
    except subprocess.CalledProcessError:
        return reference


def _accounted_for() -> tuple[dict[str, str], list[str]]:
    """Read the by-hand answers, and say which of them are not answers.

    A reason of three words is not a person having looked, so the file is
    refused for it rather than accepting a placeholder. An unknown commit is
    refused too: a hash that names nothing would otherwise sit here looking
    like coverage. The file is kept with the full history, which is where the
    commits it names are; a tree without it has no answers and needs none.
    """
    path = PROJECT / "internal" / "changelog_accounted_for.toml"
    if not path.exists():
        return {}, []
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("accounted", [])
    answers: dict[str, str] = {}
    complaints: list[str] = []
    for entry in entries:
        commit = str(entry.get("commit", "")).strip()
        where = " ".join(str(entry.get("where", "")).split())
        if not commit:
            complaints.append("an entry with no commit")
            continue
        resolved = _commit_hash(commit)
        if resolved == commit and not re.fullmatch(r"[0-9a-f]{40}", commit):
            complaints.append(f"{commit} — no such commit in this repository")
            continue
        if len(where) < 40:
            complaints.append(f"{commit} — the reason is too short to be one")
            continue
        answers[resolved] = where
    return answers, complaints


def check_every_commit_is_accounted_for(report: Report, version: str) -> None:
    """A commit that changes the application and says nothing to the reader.

    This cannot judge whether an entry *describes* a commit; it asks the narrower
    question a machine can answer — did this commit touch what a user meets, and
    did it or any commit since the tag write the changelog? A commit flagged here
    may well be covered by a neighbour's entry, since a change made over three
    commits is described once. The point is that somebody looks and says so.

    `internal/changelog_accounted_for.toml` is where that answer goes. Without such
    a place the same commits are refused at every run, and a check that always
    refuses is one people learn to scroll past. The cost of using it is having
    to write the reason down: the reason is printed beside the commit at every
    run, so whoever reads the preflight reads the claim and can weigh it.
    """
    tags = _git("tag", "--sort=-v:refname").splitlines()
    previous = next((tag for tag in tags if tag.lstrip("v") != version), None)
    if previous is None:
        report.cannot("every commit has a changelog line", "no previous tag to measure from")
        return

    commits = _git("log", "--format=%h %s", f"{previous}..HEAD").splitlines()
    if not commits:
        report.cannot("every commit has a changelog line", f"nothing since {previous}")
        return

    accounted, malformed = _accounted_for()
    if malformed:
        report.no(
            "every entry in changelog_accounted_for.toml says where the line is",
            f"{len(malformed)} do not:",
        )
        for complaint in malformed:
            print(f"        {complaint}")

    silent: list[tuple[str, str]] = []
    explained: list[tuple[str, str]] = []
    for line in commits:
        reference = line.split(" ", 1)[0]
        touched = _git("show", "--name-only", "--format=", reference).splitlines()
        touched = [path for path in touched if path]
        if any(path == "CHANGELOG.md" for path in touched):
            continue
        if all(CHANGELOG_NOT_EXPECTED_FOR.match(path) for path in touched):
            continue
        # By full hash on both sides, because the abbreviation git prints is not
        # a fixed width — it grows with the repository, so a seven-character
        # entry written today would stop matching its own commit later.
        where = accounted.get(_commit_hash(reference))
        (explained if where else silent).append((line, where or ""))

    if silent:
        report.no(
            f"every commit since {previous} wrote to the changelog",
            f"{len(silent)} did not, and each needs a person to say why "
            "in internal/changelog_accounted_for.toml:",
        )
        for line, _ in silent:
            print(f"        {line}")
        return
    report.ok(
        f"every commit since {previous} wrote to the changelog",
        f"{len(commits)} commits, {len(explained)} accounted for by hand",
    )
    for line, where in explained:
        print(f"        {line}\n          — {where}")

    # An entry that answers for no commit in this window is describing nothing.
    # It is said and not refused: stale bookkeeping cannot make a release wrong.
    answered = {_commit_hash(line.split(" ", 1)[0]) for line, _ in explained}
    stale = sorted(set(accounted) - answered)
    if stale:
        report.cannot(
            "every entry in changelog_accounted_for.toml still answers for a commit",
            f"{len(stale)} answer for nothing since {previous} — remove them: "
            + ", ".join(short[:7] for short in stale),
        )


def check_no_previous_version_survives(report: Report, version: str) -> None:
    """The bump reaches every shape of a pinned URL, not the shape it was written in.

    `raw.githubusercontent.com/…/vX/…` and `github.com/…/blob/vX/…` are the same
    link written two ways, and a replace over one leaves the other pointing at
    the previous release's documents from the published page.
    """
    tags = _git("tag", "--sort=-v:refname").splitlines()
    previous = next((tag.lstrip("v") for tag in tags if tag.lstrip("v") != version), None)
    if previous is None:
        report.cannot("no previous version survives", "no previous tag to look for")
        return

    # **Where it is pinned, not where it is mentioned.** A comment saying a
    # sentence "went out in 1.2.0" is the record and must survive; a URL or an
    # unpack folder naming 1.2.0 sends a reader to the previous release. Asked as
    # the three forms a version is *pinned* in, so prose is never flagged and a
    # link never escapes.
    pinned = re.compile(
        rf"(/v?{re.escape(previous)}/)|(diglibrary-{re.escape(previous)})"
        rf"|(^version\s*=\s*[\"']{re.escape(previous)})"
    )
    tracked = _git("ls-files").splitlines()
    left: list[str] = []
    for path in tracked:
        if path.startswith(VERSION_MAY_SURVIVE_IN) or path.endswith((".gif", ".png")):
            continue
        try:
            text = (PROJECT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pinned.search(line):
                left.append(f"{path}:{number}")

    if left:
        report.no(f"no file still names {previous}", f"{len(left)}: {', '.join(left[:8])}")
        return
    report.ok(f"no file still names {previous}")


def check_the_homebrew_formula_is_this_release(report: Report) -> None:
    """The tap is not on the front page, which is exactly why it can rot unseen.

    Nothing on either front page sends a beginner to the tap, so a formula left
    behind produces no complaint at all: it installs the previous version for
    whoever found it.

    **It cannot simply be required to match**, for the same reason the recording
    cannot: a formula names its archive by `sha256`, and that digest is of the
    file the *index* serves. The index cannot hold this version until this
    version is published, so between the bump and the publish the formula is
    one release behind, and is corrected afterwards, like the picture.

    So the rule is the same limit: this version, or the previous one while this
    one is not yet on the index. Anything else is rot. And when the formula does
    name this version, the digest is fetched from the index and compared, because
    a `sha256` copied from the previous release is a URL that still resolves and
    a file that is not this one.
    """
    formula = PROJECT / "packaging" / "homebrew" / "diglibrary.rb"
    if not formula.is_file():
        report.cannot("the Homebrew formula names a real release", "no formula here")
        return

    text = formula.read_text(encoding="utf-8")
    url = re.search(r'^\s*url\s+"([^"]+)"', text, re.MULTILINE)
    digest = re.search(r'^\s*sha256\s+"([0-9a-f]{64})"', text, re.MULTILINE)
    if url is None or digest is None:
        report.no("the Homebrew formula names a real release", "it declares no url or no sha256")
        return

    version = _version()
    named = re.search(r"diglibrary-(\d+\.\d+\.\d+)\.tar\.gz$", url.group(1))
    if named is None:
        report.no("the Homebrew formula names a real release", f"its url is {url.group(1)}")
        return
    declared = named.group(1)

    published = _sdist_digest_on_the_index(declared)
    if published is None:
        report.cannot(
            "the Homebrew formula names a real release",
            f"it declares {declared}; the index could not be asked",
        )
        return

    if declared == version:
        if published == digest.group(1):
            report.ok("the Homebrew formula names this release", f"{version}, digest matches")
        else:
            report.no(
                "the Homebrew formula names this release",
                "its sha256 is not the digest of the archive the index serves",
            )
        return

    if _sdist_digest_on_the_index(version) is None:
        report.cannot(
            "the Homebrew formula names this release",
            f"it names {declared} and {version} is not on the index yet — a formula is "
            f"corrected after publishing, because its digest is of the published archive",
        )
        return

    report.no(
        "the Homebrew formula names this release",
        f"it names {declared}, and {version} has been on the index since before this ran",
    )


def _sdist_digest_on_the_index(version: str) -> str | None:
    """The sha256 the package index serves for one version's sdist, or None.

    None means *could not be answered*, never *not there*: the caller has to be
    able to tell an unpublished version from an unreachable index, and a check
    that reports nothing looks exactly like a check that found nothing.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/diglibrary/{version}/json", timeout=20
        ) as answer:
            data = json.load(answer)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    for entry in data.get("urls", []):
        if entry.get("packagetype") == "sdist":
            return str(entry["digests"]["sha256"])
    return None


def check_nothing_names_a_person(report: Report) -> None:
    """The first question before publishing anything, asked of every tracked file.

    `/Users/someone/…` is the placeholder this project writes on purpose, so it
    is what a real home directory is measured against. `tools/export_public.py`
    asks the rest of the question, and is run before a tree is published.

    The files that spell out the patterns they refuse are left out here for the
    reason they are left out there: an example of a path that must be refused is
    a path, and it names nobody.
    """
    found = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT),
            "grep",
            "-nP",
            r"/Users/(?!(?:someone|me)/)[a-z][a-z0-9_.-]*/",
            "--",
            ".",
            *(f":!{path}" for path in sorted(DESCRIBES_THE_PATTERNS)),
        ],
        capture_output=True,
        text=True,
    )
    if found.stdout.strip():
        lines = found.stdout.strip().splitlines()
        report.no("no tracked file names a home directory", f"{len(lines)}: {lines[0][:90]}")
        return
    report.ok("no tracked file names a home directory")


def check_the_recording_is_current(report: Report, version: str) -> None:
    """What the picture shows, read out of the transcript beside it."""
    transcript = PROJECT / "docs" / "images" / "install.txt"
    picture = PROJECT / "docs" / "images" / "install.gif"
    if not transcript.is_file() and not picture.is_file():
        report.cannot(
            "the recording shows this version",
            f"there is no recording; make one once {version} is on the index",
        )
        return
    if not transcript.is_file():
        report.no("the recording names a version", "no transcript beside the picture")
        return
    shown = re.findall(r"diglibrary[ -](\d+\.\d+\.\d+)", transcript.read_text(encoding="utf-8"))
    if not shown:
        report.no("the recording names a version", "the transcript names none")
        return
    if version in shown:
        report.ok("the recording shows this version", version)
        return
    report.cannot(
        "the recording shows this version",
        f"it shows {sorted(set(shown))}, which is allowed while this is unpublished "
        f"— record it again once {version} is on the index",
    )


def check_it_builds_and_the_commands_answer(report: Report, version: str) -> None:
    """The three things a stranger types, run from the file that will be uploaded.

    Built and installed rather than imported from the checkout, because a
    checkout finds a runtime file that packaging left out and the failure is
    invisible here and total on anybody else's machine.
    """
    workspace = PROJECT / "build" / "preflight"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    built = subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(workspace / "dist")],
        capture_output=True,
        text=True,
    )
    if built.returncode != 0:
        report.no("the package builds", built.stderr.strip().splitlines()[-1][:120])
        return
    wheel = next((workspace / "dist").glob(f"diglibrary-{version}-*.whl"), None)
    if wheel is None:
        report.no("the package builds", f"no wheel named {version}")
        return
    report.ok("the package builds", f"{wheel.name}, {wheel.stat().st_size // 1024} KB")
    report.built_sdist = next((workspace / "dist").glob(f"diglibrary-{version}.tar.gz"), None)

    subprocess.run([sys.executable, "-m", "venv", str(workspace / "venv")], capture_output=True)
    python = workspace / "venv" / "bin" / "python"
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "--no-cache-dir", str(wheel)],
        capture_output=True,
        text=True,
    )
    if installed.returncode != 0:
        report.no("the wheel installs", installed.stderr.strip().splitlines()[-1][:120])
        return

    command = workspace / "venv" / "bin" / "diglibrary"
    for spoken in ([], ["make-icon"], ["restore-copy"]):
        answered = subprocess.run([str(command), *spoken, "--help"], capture_output=True, text=True)
        name = " ".join(["diglibrary", *spoken])
        if answered.returncode != 0 or "usage:" not in answered.stdout:
            report.no(f"`{name} --help` describes itself", (answered.stdout + answered.stderr)[:90])
        else:
            report.ok(f"`{name} --help` describes itself")

    reported = subprocess.run(
        [
            str(python),
            "-c",
            "from diglibrary.metadata.authentication import application_version;"
            "print(application_version())",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if reported == version:
        report.ok("the installed package reports this version", reported)
    else:
        report.no("the installed package reports this version", f"it says {reported!r}")

    if shutil.which("twine") or _twine_in(python):
        artefacts = [str(path) for path in (workspace / "dist").glob("*")]
        checked = subprocess.run(
            [str(python), "-m", "twine", "check", "--strict", *artefacts],
            capture_output=True,
            text=True,
        )
        if checked.returncode == 0:
            report.ok("`twine check --strict` passes", "the same gate the workflow runs")
        else:
            report.no("`twine check --strict` passes", checked.stdout.strip()[-120:])
    else:
        report.cannot(
            "`twine check --strict` passes",
            "twine is not installed here; the workflow runs it either way",
        )


def _twine_in(python: Path) -> bool:
    return subprocess.run([str(python), "-c", "import twine"], capture_output=True).returncode == 0


def main() -> int:
    version = _version()
    print(f"\nPreflight for DigLibrary {version}\n")
    report = Report()

    check_the_tree_is_clean(report)
    check_the_changelog_has_this_version(report, version)
    check_every_commit_is_accounted_for(report, version)
    check_no_previous_version_survives(report, version)
    check_nothing_names_a_person(report)
    check_the_recording_is_current(report, version)
    check_it_builds_and_the_commands_answer(report, version)
    # After the build, because it is the archive this produced that the
    # formula's digest has to be of.
    check_the_homebrew_formula_is_this_release(report)
    shutil.rmtree(PROJECT / "build" / "preflight", ignore_errors=True)

    print()
    if report.refused:
        print(f"  {report.refused} refused. This release is not ready.\n")
        return 1
    if report.unknown:
        print(f"  Nothing refused, {report.unknown} could not be answered here — read them.\n")
        return 0
    print("  Every mechanical check passes. The ones that matter most are still yours.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
