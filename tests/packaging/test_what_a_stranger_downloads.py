"""What the download looks like to somebody who has never seen this project.

The ZIP from a release is the product here — there is no installer — so the
first thing a stranger meets is a folder listing. A root full of internal
documents and folders that ship empty does not say which file is for the person
who downloaded it, and the two pages written for that person get lost in it.
"""

import re
import sys
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

from export_public import is_private  # noqa: E402

# Every way a URL can name a file *of this repository at some revision*. Written
# as a shape rather than as a list of the links in the page today, so a form
# nobody has used yet — `tree/`, `raw/` — is checked the first time it appears.
# `…/issues` and `…/releases/latest` match none of these on purpose: they name no
# revision, so there is nothing about them that could go stale.
PINNED_TARGET = re.compile(
    r"https://raw\.githubusercontent\.com/cankblunt/diglibrary/(?P<ref>[^/]+)/(?P<path>[^)\s]+)"
    r"|https://github\.com/cankblunt/diglibrary/(?:blob|tree|raw)/(?P<ref2>[^/]+)/(?P<path2>[^)\s]+)"
)

# The files a person is meant to open, and the two the packaging needs. Written
# as *what may be there* rather than as a count, so adding one is a decision
# somebody makes here on purpose.
ALLOWED_AT_THE_ROOT = {
    "README.md",
    "LEIAME.md",
    "CHANGELOG.md",
    "LICENSE",
    "pyproject.toml",
    ".gitignore",
}


def _version_in_pyproject() -> str:
    """The one version this download claims to be, read where packaging reads it."""
    config = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["project"]["version"]


def _pinned_targets(page: str) -> list[tuple[str, str]]:
    """Every `(revision, path)` the page names in this repository."""
    text = (PROJECT / page).read_text(encoding="utf-8")
    return [
        (found["ref"] or found["ref2"], found["path"] or found["path2"])
        for found in PINNED_TARGET.finditer(text)
    ]


def _tracked() -> list[str]:
    """Every path that is published, read from git's own index file list."""
    from subprocess import run

    listing = run(["git", "ls-files"], cwd=PROJECT, capture_output=True, text=True, check=True)
    return [path for path in listing.stdout.splitlines() if not is_private(path)]


def test_the_root_of_the_download_holds_only_what_a_person_should_open() -> None:
    """A root full of internal documents is a root that answers nobody."""
    at_the_root = {path for path in _tracked() if "/" not in path}

    assert at_the_root <= ALLOWED_AT_THE_ROOT, (
        "these are at the root of everybody's download and are not written for "
        f"the person downloading it: {sorted(at_the_root - ALLOWED_AT_THE_ROOT)}"
    )


def test_no_empty_folder_travels_in_the_download() -> None:
    """A folder kept in the repository by a `.gitkeep` ships empty.

    An installation never uses such folders: the first run puts its database,
    logs, caches and backups in `~/.diglibrary`, and the application makes what
    it needs when it needs it.
    """
    markers = [path for path in _tracked() if Path(path).name == ".gitkeep"]

    assert markers == [], f"these folders ship empty and mean nothing to anyone: {sorted(markers)}"


def test_both_front_pages_exist_and_point_at_each_other() -> None:
    """Each front page names the other in its opening lines.

    A translation nobody can find from the page they landed on is a translation
    that does not exist.
    """
    english = (PROJECT / "README.md").read_text(encoding="utf-8")
    portuguese = (PROJECT / "LEIAME.md").read_text(encoding="utf-8")

    assert "LEIAME.md" in english[:400], "the English page does not offer the Portuguese one"
    assert "README.md" in portuguese[:400], "the Portuguese page does not offer the English one"


def test_every_link_and_picture_on_the_front_pages_points_at_something_here() -> None:
    """A broken link on the page somebody lands on is worse than no page.

    Both pages carry the same walkthrough and the same three illustrations, so a
    file moved without them is a step somebody cannot follow.
    """
    missing: list[str] = []
    for page in ("README.md", "LEIAME.md"):
        text = (PROJECT / page).read_text(encoding="utf-8")
        targets = re.findall(r"\]\(([^)#]+)\)", text) + re.findall(r'src="([^"]+)"', text)
        for target in targets:
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (PROJECT / target).exists():
                missing.append(f"{page} → {target}")

    assert missing == [], f"these point at nothing: {missing}"


def test_the_walkthrough_is_numbered_and_says_what_each_step_costs() -> None:
    """The steps are the same eight on both pages.

    Guarded because a translation drifts one edit at a time, and a person
    following the Portuguese page must not be following a shorter version of the
    instructions.
    """
    steps = {}
    for page in ("README.md", "LEIAME.md"):
        text = (PROJECT / page).read_text(encoding="utf-8")
        steps[page] = re.findall(r"^### (\d)\. ", text, flags=re.MULTILINE)

    assert steps["README.md"] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
    ], f"the English walkthrough is no longer eight numbered steps: {steps['README.md']}"
    assert steps["README.md"] == steps["LEIAME.md"], (
        "the two pages no longer walk through the same steps: "
        f"{steps['README.md']} against {steps['LEIAME.md']}"
    )


def test_the_english_page_names_nothing_by_a_relative_path() -> None:
    """`readme = "README.md"` in `pyproject.toml`, so this page *is* the PyPI page.

    A relative target resolves against the repository it is rendered in, and PyPI
    is not that repository: there, every relative picture is a broken image and
    every relative document a dead link.

    `LEIAME.md` is exempt and keeps its relative targets: it is rendered on GitHub
    and nowhere else.
    """
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)]+)\)", text) + re.findall(r'src="([^"]+)"', text)

    relative = [
        target
        for target in targets
        if not target.startswith(("http://", "https://", "mailto:", "#"))
    ]

    assert relative == [], (
        "these are relative, and the PyPI page cannot resolve them — pin them to "
        f"the release tag: {relative}"
    )


def test_every_target_the_english_page_pins_names_this_version_and_exists_here() -> None:
    """A published PyPI page is immutable and the branch it points at is not.

    Pinning to `main` would have the page of an old version showing a screenshot
    of a newer one — a screen that version never had. So each target names the
    release tag, and this is the line of the release process that has to move
    with the version.

    The second half covers what
    `test_every_link_and_picture_on_the_front_pages_points_at_something_here`
    cannot: it skips anything beginning with `http`, so without this a typo in a
    pinned path would be caught by nothing until somebody opened the page.
    """
    expected = f"v{_version_in_pyproject()}"

    stale = [f"{ref}/{path}" for ref, path in _pinned_targets("README.md") if ref != expected]
    assert stale == [], (
        f"these name a revision that is not this release's tag {expected!r}, so the "
        f"published page would show somebody else's version: {stale}"
    )

    missing = [path for _, path in _pinned_targets("README.md") if not (PROJECT / path).exists()]
    assert missing == [], f"these are pinned and point at nothing in this repository: {missing}"


def test_the_folder_the_download_unpacks_into_is_named_after_this_version() -> None:
    """Step 4 is a `cd` into a folder named after the version, on both pages.

    GitHub names the unpacked folder `diglibrary-<tag without the v>`, so a
    version bump that leaves this line behind sends every reader to a folder that
    is not there — and step 4 is the one step whose failure looks like the
    download itself went wrong.

    The illustrations are read too: the version is written on both pages and in
    `terminal.svg`, and a rule applied only to the places somebody remembered
    misses the next one. Every text artefact of the walkthrough is read, so a
    new place is caught the day it appears.

    `install.gif` names no folder of any version: what it shows is
    `pipx install diglibrary`. It does name a version, in what the install
    printed back, and that is read out of the transcript beside it by
    `test_the_recording_names_this_version_or_the_one_before_it` — under a
    different rule, because a real install can only show a version the index
    already has.
    """
    version = _version_in_pyproject()
    readable = ["README.md", "LEIAME.md"] + [
        str(picture.relative_to(PROJECT))
        for picture in sorted((PROJECT / "docs/images").glob("*.svg"))
    ]

    wrong: list[str] = []
    for artefact in readable:
        text = (PROJECT / artefact).read_text(encoding="utf-8")
        for named in re.findall(r"diglibrary-(\d+\.\d+\.\d+)", text):
            if named != version:
                wrong.append(f"{artefact} → diglibrary-{named}")

    assert wrong == [], (
        f"the walkthrough tells people to open a folder this release does not "
        f"unpack into, which is {f'diglibrary-{version}'!r}: {wrong}"
    )


def test_every_command_the_front_pages_tell_people_to_run_exists() -> None:
    """A page that names a file nobody ships is a step that cannot be taken.

    A command is not a link, and the link test never looks at one: a script that
    moved into the package would leave the pages naming a path that is gone.

    Two shapes are checked, and each is the whole shape rather than the instances
    that are there today: a script named under `tools/` has to be a file, and a
    word given to this application's own command has to be one it answers to.

    Read out of the fenced `bash` blocks rather than out of the prose: the page
    says that a step "put `diglibrary` on your path", and a bare word-match reads
    `diglibrary on` as a subcommand called `on`. What is checked is what a reader
    is told to type.
    """
    from diglibrary.__main__ import COMMANDS

    broken: list[str] = []
    for page in ("README.md", "LEIAME.md"):
        text = (PROJECT / page).read_text(encoding="utf-8")
        typed = "\n".join(re.findall(r"^```bash\n(.*?)^```", text, flags=re.MULTILINE | re.DOTALL))

        for script in re.findall(r"tools/([a-z_]+\.py)", text):
            if not (PROJECT / "tools" / script).is_file():
                broken.append(f"{page} → tools/{script} is not there")

        # Both the bare command an installation has on its path and the
        # `.venv/bin/` form the source walkthrough needs, since it never
        # activates the virtualenv.
        asked = re.findall(r"^(?:\S*bin/)?diglibrary ([a-z][a-z-]*)", typed, flags=re.MULTILINE)
        for word in asked:
            if word not in COMMANDS:
                broken.append(f"{page} → `diglibrary {word}` is not a command")

    assert broken == [], f"the walkthrough asks for things that do not exist: {broken}"


def test_both_front_pages_offer_the_one_line_install() -> None:
    """Both pages give the install by name before the install from source.

    `readme = "README.md"`, so this file is the project page on the index:
    somebody standing on the page where one line installs the application must
    not be walked through eight steps of Terminal instead.

    A published page is immutable, so this is guarded rather than remembered: the
    cost of noticing it late is a version number.
    """
    missing: list[str] = []
    for page in ("README.md", "LEIAME.md"):
        typed = "\n".join(
            re.findall(
                r"^```bash\n(.*?)^```",
                (PROJECT / page).read_text(encoding="utf-8"),
                flags=re.MULTILINE | re.DOTALL,
            )
        )
        if "pipx install diglibrary" not in typed:
            missing.append(page)

    assert missing == [], (
        "these pages do not offer the one-line install, and one of them is what a "
        f"package index renders: {missing}"
    )


def test_the_recording_shows_the_route_the_pages_give_and_no_path_of_anybody_s() -> None:
    """The picture that plays is the one artefact of the walkthrough nothing read.

    A recording is pixels, so it can go on showing a route the pages no longer
    give while every other illustration is checked against the version.

    That is answered by `docs/images/install.txt`, which
    `tools/record_install.py` writes from the same run that draws the frames. Two
    things are asked of it, and neither is about how the picture looks:

    - every line it shows somebody typing is a line both front pages tell them to
      type, so the day the route changes the recording is refused rather than
      quietly kept;
    - it names no path on anybody's machine. The run happens in a throwaway pipx
      home and pipx says so in its own output; a picture published to an index is
      immutable, and the first question before publishing anything is whether it
      names a person or a machine.
    """
    picture = PROJECT / "docs" / "images" / "install.gif"
    transcript = PROJECT / "docs" / "images" / "install.txt"
    if _there_is_no_recording():
        return
    assert picture.is_file(), "the recording the front page shows is not here"
    assert transcript.is_file(), (
        "the recording has no transcript beside it, so nothing here can read what it shows: "
        "run tools/record_install.py --apply"
    )

    text = transcript.read_text(encoding="utf-8")
    shown = re.findall(r"^~ % (.+)$", text, flags=re.MULTILINE)
    assert shown, f"{transcript.name} shows nobody typing anything"

    typed_on_the_pages = {
        line.strip()
        for page in ("README.md", "LEIAME.md")
        for block in re.findall(
            r"^```bash\n(.*?)^```",
            (PROJECT / page).read_text(encoding="utf-8"),
            flags=re.MULTILINE | re.DOTALL,
        )
        for line in block.splitlines()
    }
    unasked = [command for command in shown if command not in typed_on_the_pages]
    assert unasked == [], (
        "the recording shows somebody typing something neither front page asks for, "
        f"which is a picture of a route nobody is given: {unasked}"
    )

    named = [
        line
        for line in text.splitlines()
        if re.search(r"/Users/|/private/|/var/folders/|/home/[a-z]", line)
    ]
    assert named == [], f"the recording names a path on somebody's machine: {named}"


def _there_is_no_recording() -> bool:
    """Whether the install has no recording at all, which is a state and not a gap.

    A recording is a real install from the package index, so a version that is
    not on the index yet cannot have one. With neither the picture nor its
    transcript here, what is asked is that no page shows a picture that is not
    there; with either of them here, every check below applies.
    """
    images = PROJECT / "docs" / "images"
    if (images / "install.gif").exists() or (images / "install.txt").exists():
        return False
    pages = ("README.md", "LEIAME.md", "docs/index.html", "docs/verify.html", "docs/lossless.html")
    showing = [
        page for page in pages if "install.gif" in (PROJECT / page).read_text(encoding="utf-8")
    ]
    assert showing == [], f"these pages show a recording that is not here: {showing}"
    return True


def _released_versions() -> list[str]:
    """Every version the changelog has a section for, newest first.

    A release is declared here by writing its section, so this is what says which
    versions could have been on the package index when a recording was made.
    """
    text = (PROJECT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## (\d+\.\d+\.\d+)", text, flags=re.MULTILINE)


def test_the_recording_names_this_version_or_the_one_before_it() -> None:
    """An install prints the version it installed, and that is what goes stale.

    The transcript says what somebody is shown typing; the version is in what
    the install printed, and this reads that.

    It cannot simply be required to match this release. The recording is a real
    install from the package index, so it can only ever show a version the index
    already holds, and the commit that bumps the version necessarily comes before
    the publish: at a release the picture is exactly one version behind. Two
    behind is a stale recording, and that is where this refuses.

    Read over every form this package is named in rather than over one
    installer's sentence — `pipx` prints `diglibrary 1.2.0` and `pip` prints
    `diglibrary-1.2.0`, and a wording nobody has met yet would otherwise leave
    this finding nothing and passing, which is a recording of nothing looking
    exactly like proof.
    """
    transcript = PROJECT / "docs" / "images" / "install.txt"
    if _there_is_no_recording():
        return
    text = transcript.read_text(encoding="utf-8")

    shown = re.findall(r"diglibrary[ -](\d+\.\d+\.\d+)", text)
    assert shown, (
        "the transcript names no version of this package, so either the recording "
        "stopped showing one or the wording changed and this guard has gone blind: "
        f"{transcript.name}"
    )

    version = _version_in_pyproject()
    previous = next((released for released in _released_versions() if released != version), None)
    allowed = {version} | ({previous} if previous is not None else set())

    stale = sorted({named for named in shown if named not in allowed})
    assert stale == [], (
        f"the recording shows an install of {stale} and this release is {version}, which is "
        f"more than the one version behind a recording may honestly be: record it again with "
        f"`.venv/bin/python tools/record_install.py --apply` once {version} is on the index"
    )


def test_a_stranger_on_another_system_is_told_rather_than_left_with_a_dead_window() -> None:
    """`pip install` cannot refuse by platform, so the first word has to.

    Both front pages say that Windows and Linux are not supported yet, and
    `pip install diglibrary` succeeds everywhere, because a pure-Python wheel
    carries no platform to refuse by. Without a refusal, what somebody on
    Windows meets is a window that does not open, with nothing raised and
    nothing said.

    Three things are asked of the sentence. It refuses **before** the command
    line is read, because a refusal that depends on which word was typed is two
    refusals. It is the **only** place that states the rule, because a rule
    stated in two places is corrected in one of them. And it names the system
    back to the reader rather than guessing.
    """
    source = (PROJECT / "src" / "diglibrary" / "__main__.py").read_text(encoding="utf-8")

    refusals = re.findall(r'sys\.platform\s*!=\s*"darwin"', source)
    assert len(refusals) == 1, (
        "the rule *this is not macOS* is stated in more than one place here, which is "
        f"where one of them stops being corrected: {len(refusals)} found"
    )

    gate = source.index('sys.platform != "darwin"')
    reads_the_words = source.index("sys.argv[1] in COMMANDS")
    assert gate < reads_the_words, (
        "the refusal comes after the command line is read, so which word somebody typed "
        "decides what they are told"
    )

    from diglibrary.__main__ import NOT_MACOS

    spoken = NOT_MACOS.format(system="Windows")
    assert "Windows" in spoken, "the sentence never names the system it is refusing"
    for promise in ("Windows and Linux are intended", "not promised", "github.com/cankblunt"):
        assert promise in spoken, f"the refusal drops what it owes the reader: {promise!r}"
    assert (
        "Nothing was changed" in spoken
    ), "a refusal about somebody's music library must say that it touched nothing"
