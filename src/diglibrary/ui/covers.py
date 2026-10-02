"""Hand the window one cover at a time, and nothing else on this disk.

Covers reach the window by URL rather than as `data:` URIs, because a shelf
drawn from `data:` URIs is much slower: a `data:` URI is decoded again every
time a card is rebuilt, and the base64 that carries it is a third larger than
the image before it crosses at all. Serving files over the loopback interface
is how a URL exists, and it is also the shape that can go badly wrong, so the
rules are here and not spread across whoever calls this.

**A path never arrives from the window.** The application registers a file and
gets back an opaque id; the URL carries the id. A request cannot name a file
this server was not told about, so the oldest failure of this shape — a media
route that becomes a way to read any file the user can read — has nothing to
work with.

**And each file is registered with the folder it has to be inside**, proved by
resolving rather than by reading the string — the same rule `_landed_folder`
and `_a_folder_on_this_disk` already carry.

**A token, made fresh every run.** Any page in any browser on this machine can
send a request to `127.0.0.1`. It cannot read the answer — the browser stops
that — but it can see whether the request *worked*, and that alone tells it
whether this library holds a given cover. The token makes those requests fail
identically whether the file exists or not.
"""

import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
"""Named here rather than left to `mimetypes`, whose answers differ between
machines — and a wrong type is a picture the element refuses to draw."""


class CoverServer:
    """Purpose: serve registered cover images to this window and to nothing else.

    Responsibilities: hold the registry of id → path, answer GET and HEAD, and
    refuse everything that is not both registered and under the folder it was
    registered with. Boundaries: it reads no tag, and never lists what it holds
    — an id is the only way in. Dependencies: the standard library. Constraints:
    bound to the loopback interface on a port the operating system chooses, with
    a token minted per run.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Create a server that holds nothing until a file is registered."""
        self._logger = logger
        self._token = secrets.token_urlsafe(24)
        # id -> (the file, the folder it had to be inside when it was offered)
        self._files: dict[str, tuple[Path, Path]] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._port: int | None = None

    def start(self) -> None:
        """Begin listening on a loopback port the system picks."""
        if self._server is not None:
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        server.daemon_threads = True
        self._server = server
        self._port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._logger.info(
            "The cover server is listening.",
            extra={"operation": "covers.server.started", "port": self._port},
        )

    def stop(self) -> None:
        """Stop listening and forget every registered file."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            self._files.clear()

    def image_url_for(self, path: Path, inside: Path) -> str | None:
        """Register one picture, and return the URL that draws it.

        ``None`` when the file is not inside ``inside`` or is not there at all,
        so the caller can fall back rather than hand the window a URL that fails
        later with no reason.
        """
        if self._server is None:
            return None
        settled = _contained(path, inside)
        if settled is None:
            return None
        # Keyed by the resolved path, so asking twice for one file does not
        # grow the registry for the life of the session.
        key = _identifier(settled)
        with self._lock:
            self._files[key] = (settled, inside)
        return f"http://127.0.0.1:{self._port}/cover/{key}?token={self._token}"

    def _resolve(self, key: str, token: str) -> Path | None:
        """Return the file this request may have, or ``None``.

        Both locks, in the order that costs least: a wrong token is refused
        before the disk is touched, and a registered file is proved to still be
        under its folder.
        """
        # Compared as bytes: `compare_digest` refuses non-ASCII text, and a
        # query string can carry any character — the refusal must be the same
        # 404 as every other, not an exception.
        if not secrets.compare_digest(token.encode("utf-8"), self._token.encode("ascii")):
            return None
        with self._lock:
            registered = self._files.get(key)
        if registered is None:
            return None
        path, inside = registered
        # Proved again rather than trusted: the file may have gone between the
        # moment the window was given this URL and the moment it asked.
        return _contained(path, inside)


def _contained(path: Path, inside: Path) -> Path | None:
    """Return this file, proved to sit inside that folder, or ``None``.

    Resolved rather than compared as text: `..` in a string is a path that
    looks contained and is not, and a symlink is the same trick wearing a
    different hat.
    """
    try:
        settled = Path(path).resolve(strict=True)
        folder = Path(inside).resolve(strict=True)
    except OSError:
        return None
    if not settled.is_file() or not folder.is_dir():
        return None
    return settled if settled.is_relative_to(folder) else None


def _identifier(path: Path) -> str:
    """A name for one file that says nothing about where it is."""
    from hashlib import blake2b

    return blake2b(str(path).encode("utf-8"), digest_size=16).hexdigest()


def _content_type(path: Path) -> str:
    return IMAGE_TYPES.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


def _handler_for(server: CoverServer) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to one server's registry."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            # The default writes every request to stderr, which in this
            # application means one line per cover in the terminal.
            return

        def do_HEAD(self) -> None:
            self._respond(body=False)

        def do_GET(self) -> None:
            self._respond(body=True)

        def _respond(self, body: bool) -> None:
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            token = parse_qs(parsed.query).get("token", [""])[0]
            if len(parts) != 2 or parts[0] != "cover":
                self._refuse()
                return
            path = server._resolve(parts[1], token)
            if path is None:
                # The same answer for a bad token, an unknown id and a file
                # that has moved: a page that can tell them apart can ask this
                # server what the library holds.
                self._refuse()
                return
            try:
                with path.open("rb") as handle:
                    data = handle.read()
            except OSError:
                self._refuse()
                return
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", _content_type(path))
                # A cover's file name is the digest of its own bytes, so the URL
                # can never mean a different picture. Letting the webview keep it
                # is what makes a redraw cost nothing instead of fetching the
                # whole shelf again.
                self.send_header("Cache-Control", "private, max-age=86400, immutable")
                self.end_headers()
                if body:
                    self.wfile.write(data)
            except (OSError, ConnectionError):
                # The window dropped the request — a card rebuilt before its
                # picture arrived. Nothing is wrong with the file.
                return

        def _refuse(self) -> None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler
