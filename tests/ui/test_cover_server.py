"""What the cover server refuses, which is the whole point of it.

Every test here names the thing that would go wrong. A media route on the
loopback interface is the shape that turns into "read any file this user can
read", and this project ships to strangers.
"""

import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from diglibrary.ui.covers import CoverServer

LOGGER = logging.getLogger("test.covers")


@pytest.fixture()
def cache(tmp_path: Path) -> Path:
    root = tmp_path / "covers"
    root.mkdir()
    (root / "abc123.jpg").write_bytes(b"\xff\xd8\xff" + b"J" * 900)
    # Something valuable that is NOT in the cache, which is what a broken
    # media route hands out.
    (tmp_path / "taxes.pdf").write_bytes(b"private")
    return root


@pytest.fixture()
def server(cache: Path):
    running = CoverServer(LOGGER)
    running.start()
    yield running
    running.stop()


def _fetch(url: str):
    return urllib.request.urlopen(urllib.request.Request(url), timeout=5)


def test_a_registered_cover_is_served_by_an_id_that_names_no_file(server, cache: Path) -> None:
    cover = cache / "abc123.jpg"

    url = server.image_url_for(cover, cache)

    assert url is not None
    assert "/cover/" in url and "abc123" not in url, "the URL names an id, never the file"
    answer = _fetch(url)
    assert answer.status == 200
    assert answer.headers["Content-Type"] == "image/jpeg"
    assert answer.read() == cover.read_bytes()
    # Its name is the digest of its own bytes, so the URL can never come to mean
    # a different picture — which is what lets a redraw cost nothing.
    assert "immutable" in (answer.headers["Cache-Control"] or "")


def test_a_file_outside_the_cache_is_never_given_a_url(server, cache: Path, tmp_path: Path) -> None:
    """The failure this whole module exists to prevent."""
    assert server.image_url_for(tmp_path / "taxes.pdf", cache) is None


def test_a_path_that_climbs_out_is_not_inside(server, cache: Path) -> None:
    """`..` in a string is a path that looks contained and is not."""
    assert server.image_url_for(cache / ".." / "taxes.pdf", cache) is None


def test_a_symlink_pointing_out_is_not_inside(server, cache: Path, tmp_path: Path) -> None:
    """The same trick wearing a different hat, and the reason the check
    resolves rather than compares text."""
    link = cache / "escape.jpg"
    link.symlink_to(tmp_path / "taxes.pdf")
    assert server.image_url_for(link, cache) is None


def test_without_the_token_nothing_is_served(server, cache: Path) -> None:
    """Any page in any browser on this machine can send a request here. It
    cannot read the answer, but it can see whether the request *worked* — and
    that alone tells it whether this library holds a given cover."""
    url = server.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None
    bare = url.split("?")[0]

    with pytest.raises(urllib.error.HTTPError) as refused:
        _fetch(bare)
    assert refused.value.code == 404

    with pytest.raises(urllib.error.HTTPError):
        _fetch(f"{bare}?token=guessed")


def test_an_unregistered_id_answers_exactly_like_a_bad_token(server, cache: Path) -> None:
    """Two different refusals that can be told apart are a way to ask this
    server what it holds."""
    url = server.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None
    token = url.split("token=")[1]
    base = url.split("/cover/")[0]

    with pytest.raises(urllib.error.HTTPError) as unknown:
        _fetch(f"{base}/cover/{'0' * 32}?token={token}")
    with pytest.raises(urllib.error.HTTPError) as wrong_token:
        _fetch(f"{url.split('?')[0]}?token=nope")
    assert unknown.value.code == wrong_token.value.code == 404


def test_a_token_with_characters_outside_ascii_is_refused_like_any_other(
    server, cache: Path
) -> None:
    """`compare_digest` refuses non-ASCII text with an exception, and an
    exception in the handler is a dropped connection and a traceback — a third
    kind of answer, where this server promises exactly one."""
    url = server.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None

    with pytest.raises(urllib.error.HTTPError) as refused:
        _fetch(f"{url.split('?')[0]}?token=caf%C3%A9")
    assert refused.value.code == 404


def test_only_the_cover_route_exists(server, cache: Path) -> None:
    """The server serves covers and nothing else. A registered id asked for
    under any other word is refused, so there is no second path into the
    registry."""
    url = server.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None

    for word in ("audio", "whatever"):
        with pytest.raises(urllib.error.HTTPError) as refused:
            _fetch(url.replace("/cover/", f"/{word}/"))
        assert refused.value.code == 404


def test_a_file_that_leaves_the_cache_stops_being_served(server, cache: Path) -> None:
    """A file taken away while the window is open must not stay reachable
    through an id handed out earlier."""
    url = server.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None
    (cache / "abc123.jpg").unlink()

    with pytest.raises(urllib.error.HTTPError):
        _fetch(url)


def test_nothing_is_served_before_it_starts(cache: Path) -> None:
    assert CoverServer(LOGGER).image_url_for(cache / "abc123.jpg", cache) is None


def test_stopping_releases_the_port_and_forgets_every_file(cache: Path) -> None:
    """A server that is never stopped keeps its socket, its `serve_forever`
    thread and the registry of every file the session has offered for the life
    of the process, after the window has closed."""
    running = CoverServer(LOGGER)
    running.start()
    url = running.image_url_for(cache / "abc123.jpg", cache)
    assert url is not None and _fetch(url).status == 200

    running.stop()

    with pytest.raises(urllib.error.URLError):
        _fetch(url)
    assert running.image_url_for(cache / "abc123.jpg", cache) is None


def test_nothing_in_the_cover_server_opens_a_file_for_writing() -> None:
    """Written as *no open is anything but binary read* rather than as a list of
    the modes that would be wrong, so a mode nobody has used yet cannot slip in.
    """
    source = (
        Path(__file__).parent.parent.parent / "src" / "diglibrary" / "ui" / "covers.py"
    ).read_text(encoding="utf-8")
    opens = re.findall(r"\.open\(([^)]*)\)", source)
    assert opens, "nothing opens a file here, so this guard is reading the wrong module"
    for mode in opens:
        assert mode.strip() == '"rb"', f"the cover server opens a file as {mode.strip()}"


def test_the_window_closing_stops_the_cover_server() -> None:
    """The wiring, not the method. A `stop()` nobody calls is the leak itself,
    and a test that only proves the method exists checks the part and not the
    plumbing.
    """
    ui_app = Path(__file__).parent.parent.parent / "src" / "diglibrary" / "ui" / "app.py"
    after_start = ui_app.read_text(encoding="utf-8").split("webview.start(", 1)

    assert len(after_start) == 2, "the window is no longer started where this test looks"
    assert "covers.stop()" in after_start[1], (
        "the cover server is never stopped after the window closes, so its "
        "loopback socket and its thread outlive the window"
    )
