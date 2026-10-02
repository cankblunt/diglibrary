"""Photograph the window for the two front pages, over a library nobody owns.

The screenshots on `README.md`, `LEIAME.md` and `docs/index.html` are the window
that ships — the same `index.html`, `app.js`, `styles.css` and `strings.js` —
and they must show nothing of anybody's own library. So this builds one: a few
invented albums of short generated tones, tagged with invented names, identified
against an invented catalogue by the application's own API, some of them applied
and rated, and every track given an invented key and tempo. The window is served
from a temporary copy whose bridge forwards each call to that API over a local
port, and headless Chrome takes the pictures. No window opens on anybody's desk
and no network is reached.

    .venv/bin/python tools/record_screens.py            # rehearsal: writes to a temporary folder
    .venv/bin/python tools/record_screens.py --apply    # writes docs/images/screen-*.png

It never asks anything, and it refuses rather than guessing: without `ffmpeg`
or Chrome it says which one is missing and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mutagen.flac import FLAC

from diglibrary.application.api import LibraryApi, _Pipeline
from diglibrary.application.artwork import ArtworkPolicy, ArtworkService
from diglibrary.application.bench import QualityBench
from diglibrary.application.composition import naming_from_settings
from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataQuery,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.application.identification import IdentificationWorkflow
from diglibrary.database.connection import Database
from diglibrary.database.library_store import LibraryStore, StoredHarmonics
from diglibrary.library.artwork import FilesystemArtworkStore
from diglibrary.library.audio import MutagenAudioProbe
from diglibrary.library.executor import ChangeExecutor
from diglibrary.library.matching import AlbumMatcher
from diglibrary.library.naming import NamingPolicy
from diglibrary.library.planner import ChangePlanner
from diglibrary.library.scanner import LibraryScanner
from diglibrary.library.tags import MutagenTagStore
from diglibrary.metadata.service import MetadataService

PROJECT = Path(__file__).resolve().parent.parent
WEB = PROJECT / "src" / "diglibrary" / "ui" / "web"
IMAGES = PROJECT / "docs" / "images"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
SECONDS = 2

# Invented: artists, albums and titles made up for this picture. `known` is
# whether the invented catalogue answers for the album, and `apply` and `rating`
# are what is done to it afterwards, so the shelf shows every state a card has.
ALBUMS: tuple[dict[str, object], ...] = (
    {
        "artist": "Halden Street Orchestra",
        "album": "Quiet Almanac",
        "year": 1974,
        "tracks": ("Overture", "Tidewall", "Clear Lamps", "Southing", "Slow Cant", "Closing"),
        "known": True,
        "apply": True,
        "rating": 5,
    },
    {
        "artist": "The Velvet Harbour",
        "album": "Night Ferry",
        "year": 1981,
        "tracks": ("Dockside", "Lanterns", "Low Tide", "Crossing", "Fog Bell", "Home Port"),
        "known": True,
        "apply": True,
        "rating": 4,
    },
    {
        "artist": "Loose Gravel Trio",
        "album": "Glasswork Town",
        "year": 1969,
        "tracks": (
            "Market Steps",
            "Blue Casement",
            "Hillside",
            "Tramline",
            "Rooftop",
            "Small Hours",
        ),
        "known": True,
        "apply": False,
        "rating": None,
    },
    {
        "artist": "Ilse Corwen",
        "album": "Slow Signals",
        "year": 1995,
        "tracks": ("Static", "Relay", "Short Wave", "Antenna", "Quiet Hour", "Sign Off"),
        "known": True,
        "apply": True,
        "rating": 5,
    },
    {
        "artist": "Sandbar Combo",
        "album": "Cold Embers",
        "year": 1977,
        "tracks": ("Bonfire", "Ashfall", "Flint", "Porchlight", "Offshore", "Early Light"),
        "known": False,
        "apply": False,
        "rating": None,
    },
    {
        "artist": "Holloway Six",
        "album": "Paper Satellites",
        "year": 2003,
        "tracks": ("Launch", "Orbit", "Paper Moon", "Drift", "Re-entry", "Landing"),
        "known": False,
        "apply": False,
        "rating": None,
    },
)

# Invented readings, cycled over the tracks: tonic, mode, how sure, tempo, and
# the runner-up key and alternative tempo where the reading was close.
READINGS: tuple[tuple[str, str, float, float, float, str | None, float | None], ...] = (
    ("A", "minor", 0.82, 0.30, 118.0, None, None),
    ("C", "major", 0.61, 0.04, 96.0, "A", None),
    ("E", "minor", 0.77, 0.22, 124.0, None, None),
    ("G", "major", 0.55, 0.03, 88.0, "E", 176.0),
    ("D", "minor", 0.70, 0.12, 102.0, None, None),
    ("F", "major", 0.66, 0.08, 132.0, "D", None),
    ("B", "minor", 0.80, 0.27, 110.0, None, None),
    ("A#", "major", 0.58, 0.05, 92.0, "G", 184.0),
)


class InventedCatalogue:
    """Answer a search with the invented release whose title was asked for."""

    def __init__(self, releases: dict[str, ReleaseMetadata]) -> None:
        self._releases = releases

    def search_releases(self, query: MetadataQuery) -> tuple[ReleaseMetadata, ...]:
        wanted = (query.album or query.text or "").casefold()
        return tuple(
            release for release in self._releases.values() if release.title.casefold() in wanted
        )

    def get_release(self, release_id: str) -> ReleaseMetadata:
        return self._releases[release_id]


def _refuse(message: str) -> None:
    print(f"record_screens: {message}. Nothing was written.", file=sys.stderr)
    raise SystemExit(1)


def _build_library(root: Path) -> dict[str, ReleaseMetadata]:
    releases: dict[str, ReleaseMetadata] = {}
    for number, album in enumerate(ALBUMS, start=1):
        folder = root / f"{album['artist']} - {album['album']}"
        folder.mkdir(parents=True)
        titles = album["tracks"]
        for position, title in enumerate(titles, start=1):  # type: ignore[arg-type]
            path = folder / f"{position:02d} {title}.flac"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={200 + 40 * position + 7 * number}:duration={SECONDS}",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    str(path),
                ],
                check=True,
            )
            tags = FLAC(path)
            tags["artist"] = str(album["artist"])
            tags["albumartist"] = str(album["artist"])
            tags["album"] = str(album["album"])
            tags["title"] = str(title)
            tags["tracknumber"] = str(position)
            tags["date"] = str(album["year"])
            tags.save()
        if album["known"]:
            identifier = f"invented-{number}"
            releases[identifier] = ReleaseMetadata(
                source=MetadataSources.DISCOGS,
                source_release_id=identifier,
                title=str(album["album"]),
                artists=(ArtistMetadata(name=str(album["artist"])),),
                tracks=tuple(
                    TrackMetadata(
                        title=str(title),
                        position=position,
                        position_on_medium=position,
                        duration_ms=SECONDS * 1000,
                    )
                    for position, title in enumerate(titles, start=1)  # type: ignore[arg-type]
                ),
                released_on=date(int(album["year"]), 1, 1),  # type: ignore[call-overload]
            )
    return releases


def _api(work: Path, releases: dict[str, ReleaseMetadata]) -> LibraryApi:
    """The application's own API, composed the way the test suite composes it."""
    logger = logging.getLogger("record_screens")
    database = Database(work / "app" / "library.sqlite3", logger)
    database.initialize()
    store = LibraryStore(database, logger)
    tag_store = MutagenTagStore()
    artwork_store = FilesystemArtworkStore()
    catalogue = InventedCatalogue(releases)

    def pipeline(settings: dict[str, object]) -> _Pipeline:
        naming = naming_from_settings(NamingPolicy.from_style("spotiflac"), settings, logger)
        planner = ChangePlanner(naming, tag_store, artwork_store)
        artwork = ArtworkService(
            client=None,  # type: ignore[arg-type]
            staging_directory=work / "app" / "staging",
            policy=ArtworkPolicy(),
            logger=logger,
            store=artwork_store,
        )
        workflow = IdentificationWorkflow(
            metadata=MetadataService(((MetadataSources.DISCOGS, catalogue),)),  # type: ignore[arg-type]
            matcher=AlbumMatcher(duration_tolerance_ms=100, ordered_tolerance_ms=100),
            planner=planner,
            tag_store=tag_store,
            logger=logger,
            threshold=float(settings.get("threshold", 0.9)),
            artwork=artwork,
        )
        scanner = LibraryScanner(MutagenAudioProbe(), logger)
        return _Pipeline(
            scanner=scanner,
            workflow=workflow,
            executor=ChangeExecutor(tag_store, logger, artwork_store, work / "app" / "backup"),
            threshold=float(settings.get("threshold", 0.9)),
            planner=planner,
            artwork=artwork,
            tag_store=tag_store,
            bench=QualityBench(scanner, store, logger),
        )

    return LibraryApi(
        pipeline_factory=pipeline,
        store=store,
        artwork_store=artwork_store,
        logger=logger,
        cover_cache=work / "app" / "covers",
        # Checked in `main` before anything is built, so the window is told the
        # truth: this machine can measure.
        ffmpeg=True,
    )


def _wait(job: object, what: str) -> None:
    thread = getattr(job, "_job", None)
    if thread is not None:
        thread.join(timeout=120)
        if thread.is_alive():
            _refuse(f"{what} did not finish in two minutes")


def _stage(api: LibraryApi, library: Path) -> None:
    """Scan, apply and rate the invented albums, and give every track a reading."""
    # A new installation opens on the welcome dialog; the pictures are of the
    # window after it, so it is answered the way pressing its button answers it.
    api.welcomed()
    if not api.scan(str(library)).get("ok"):
        _refuse("the invented library could not be scanned")
    _wait(api, "the scan")
    by_name = {album["album"]: album for album in ALBUMS}
    cards = api.state()["albums"]
    for card in cards:  # type: ignore[union-attr]
        album = next((a for name, a in by_name.items() if str(name) in str(card["folder"])), None)
        if album is None:
            continue
        if album["apply"]:
            api.apply_automatic([card["unit_id"]])
            _wait(api, "an apply")
        if album["rating"]:
            api.rate_album(card["unit_id"], album["rating"])  # type: ignore[arg-type]
    readings = []
    files = [file for state in api._album_snapshot() for file in state.unit.audio_files]
    for index, file in enumerate(files):
        if not file.audio_key:
            continue
        tonic, mode, sure, margin, bpm, runner, alternative = READINGS[
            (index * 5 + index // 6) % len(READINGS)
        ]
        readings.append(
            StoredHarmonics(
                audio_key=file.audio_key,
                content_signature=file.content_signature,
                tonic=tonic,
                mode=mode,
                key_confidence=sure,
                bpm=bpm,
                bpm_confidence=0.8,
                bpm_alternative=alternative,
                key_margin=margin,
                runner_up_tonic=runner,
                runner_up_mode=mode if runner else None,
            )
        )
    api._store.record_harmonics(readings)


def _serve(api: LibraryApi, web: Path) -> ThreadingHTTPServer:
    names = sorted(
        name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))
    )
    bridge = (
        "<script>\n"
        f"const names = {json.dumps(names)};\n"
        "window.pywebview = { api: {} };\n"
        "for (const name of names) {\n"
        "  window.pywebview.api[name] = (...args) => fetch('/call', {method: 'POST',"
        " body: JSON.stringify({name, args})}).then((answer) => answer.json());\n"
        "}\n"
        "const tab = new URLSearchParams(location.search).get('tab');\n"
        'if (tab) setTimeout(() => document.querySelector(`[data-tab="${tab}"]`).click(), 1500);\n'
        "</script>\n"
        # Chrome takes the picture when the page has loaded, and an image that
        # arrives late is what holds that moment back until the tab has drawn.
        '<img src="/hold" alt="" style="position:absolute;width:0;height:0">\n'
    )
    index = web / "index.html"
    text = index.read_text(encoding="utf-8")
    marker = '<script type="module" src="app.js"></script>'
    if marker not in text:
        _refuse("index.html no longer loads app.js where this tool injects the bridge")
    index.write_text(text.replace(marker, bridge + marker), encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(web), **kwargs)  # type: ignore[misc]

        def log_message(self, *args: object) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/hold"):
                time.sleep(4)
                self.send_response(204)
                self.end_headers()
                return
            super().do_GET()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            call = json.loads(self.rfile.read(length) or b"{}")
            try:
                answer = getattr(api, call["name"])(*call.get("args", []))
            except Exception as error:  # a stand-in reports, it does not crash the page
                answer = {"ok": False, "error": str(error)}
            body = json.dumps(answer, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _shoot(url: str, target: Path, profile: Path, height: int) -> None:
    """Take one picture. Chrome writes the file and may stay running after it,
    so the file is what is waited for, and the browser is closed afterwards."""
    # Removed first: the file is the signal that Chrome has finished, and a
    # picture already there from the last run would answer before it began.
    target.unlink(missing_ok=True)
    browser = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size=1600,{height}",
            "--force-device-scale-factor=1.6",
            "--timeout=6000",
            f"--screenshot={target}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 90
        last = -1
        while time.monotonic() < deadline:
            size = target.stat().st_size if target.is_file() else 0
            if size and size == last:
                break
            last = size
            time.sleep(1)
        else:
            _refuse(f"Chrome did not write {target.name} in ninety seconds")
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            browser.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write into docs/images")
    arguments = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        _refuse("ffmpeg is not installed")
    if not CHROME.is_file():
        _refuse(f"Chrome is not at {CHROME}")
    logging.basicConfig(level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix="diglibrary-screens-") as scratch:
        work = Path(scratch)
        releases = _build_library(work / "library")
        api = _api(work, releases)
        _stage(api, work / "library")
        web = work / "web"
        shutil.copytree(WEB, web)
        server = _serve(api, web)
        port = server.server_address[1]
        out = IMAGES if arguments.apply else work / "out"
        out.mkdir(parents=True, exist_ok=True)
        # The shelf is one row of cards, so its picture is short; the mixing
        # table is read down the page.
        shots = {"screen-library.png": ("library", 520), "screen-mixing.png": ("mixing", 900)}
        for name, (tab, height) in shots.items():
            url = f"http://127.0.0.1:{port}/index.html?tab={tab}"
            _shoot(url, out / name, work / "chrome", height)
            time.sleep(0.2)
        server.shutdown()
        if not arguments.apply:
            keep = Path(tempfile.mkdtemp(prefix="diglibrary-screens-rehearsal-"))
            for name in shots:
                shutil.copy2(out / name, keep / name)
            out = keep
        for name in shots:
            print(f"wrote {out / name}")


if __name__ == "__main__":
    main()
