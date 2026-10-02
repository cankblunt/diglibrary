"""Opening the DigLibrary window: one window, one bridge, no business rules.

Everything the interface can do lives in ``LibraryApi``; this module only owns
what is genuinely the platform's — creating the window, serving the static
files, and the native folder dialog, which is injected into the API as a plain
callable so the application layer never imports pywebview.
"""

import html
import logging
import subprocess
import sys
import threading
from pathlib import Path

import webview

from diglibrary.application.composition import create_api, create_application
from diglibrary.config.home import resolve
from diglibrary.config.loader import ConfigurationError
from diglibrary.desktop.bundle import make_icon
from diglibrary.ui import drops, folders, tooltips, wakefulness
from diglibrary.ui.covers import CoverServer

_WEB_DIRECTORY = Path(__file__).parent / "web"
_WINDOW_TITLE = "DigLibrary"
_BACKGROUND = "#0e0f12"


def _configuration_failure_page(config_path: Path, message: str) -> str:
    """Build the one page this application can still draw when it cannot start.

    Self-contained on purpose: the window's own files are served by a bridge that
    does not exist yet at this point, and everything that would style this page
    is loaded by the application that failed to compose.

    Both values are escaped. They are a filesystem path and an exception's text,
    which is to say they are not this project's own words — a folder with a `<`
    in its name is perfectly legal and would otherwise end the paragraph early.
    """
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>"
        "body{background:#0e0f12;color:#e6e7e9;font:14px/1.5 -apple-system,sans-serif;"
        "margin:0;padding:32px}"
        "h1{font-size:16px;margin:0 0 12px}"
        "code{background:#1a1c21;padding:2px 5px;border-radius:4px;word-break:break-all}"
        "p{margin:0 0 12px;max-width:44em}.why{color:#e5a04c}"
        "</style></head><body>"
        "<h1>DigLibrary cannot start</h1>"
        "<p>Its settings file could not be read:<br>"
        f"<code>{html.escape(str(config_path))}</code></p>"
        f"<p class='why'>{html.escape(message)}</p>"
        "<p>Correct that line and open DigLibrary again. To run against a "
        "different installation entirely, start it with "
        "<code>--config /some/other.toml</code>.</p>"
        "</body></html>"
    )


def _report_thread_deaths(logger: logging.Logger) -> None:
    """Send what kills a background thread to the log instead of to nowhere.

    Background threads do the slow work here — scanning, identifying,
    measuring, collecting downloads, restoring the library — and Python's
    default for one that raises is to print a traceback on stderr and let the
    thread end. An application launched from the Dock has no stderr anyone
    reads, so the only evidence would be a progress bar that never finishes.

    This does not rescue the run; a thread that has raised is over. What it
    changes is that the next question — *why did the bar stop?* — has an answer
    in the same JSONL log as everything else, with the operation field the rest
    of this application writes.
    """

    def report(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        logger.error(
            "A background thread ended in an unhandled exception.",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            extra={
                "operation": "thread.unhandled",
                # Not `thread`: `logging` refuses to let an `extra` field
                # overwrite a `LogRecord` attribute, and `thread` is one of its
                # own. Using it raises `KeyError` inside this hook before a
                # word is written, and the death goes unreported after all.
                "thread_name": getattr(args.thread, "name", "unknown"),
            },
        )

    threading.excepthook = report


def run(config_path: Path | None, debug: bool = False) -> None:
    """Compose the application and open its window, blocking until it closes.

    **A settings file it cannot read is a window, not a traceback.** Left
    uncaught, ``ConfigurationError`` launched from the Dock is an icon that
    bounces and stops: no window, and the reason written only to a log file.
    The message names the file and the key, so it is shown. Only this one
    failure is answered this way, because it is the one somebody can act on.

    ``None`` means nobody said which file, so it is chosen here — inside the same
    guard, because choosing can fail the way reading can: a directory holding a
    database and no settings is refused rather than replaced with an empty
    library.
    """
    settings = config_path
    try:
        settings = resolve(config_path)
        application = create_application(settings)
    except ConfigurationError as error:
        # stderr as well, for whoever ran this from a terminal and is watching it
        # rather than the screen.
        print(f"DigLibrary cannot start: {error}", file=sys.stderr)
        webview.create_window(
            _WINDOW_TITLE,
            html=_configuration_failure_page(settings or Path("config.toml"), str(error)),
            width=620,
            height=340,
            background_color=_BACKGROUND,
        )
        webview.start()
        return
    _report_thread_deaths(application.logger)
    window_box: list[webview.Window] = []

    def pick_folder() -> str | None:
        if not window_box:
            return None
        # A panel wide enough to read an album folder name, which the one
        # pywebview opens is not. Where it cannot be opened — any platform but
        # macOS — the system's own dialog opens instead.
        if folders.available():
            # No start directory: the panel reopens where it was last used,
            # which the system already stores.
            return folders.choose_folder()
        chosen = window_box[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not chosen:
            return None
        return str(chosen[0])

    def pick_image() -> str | None:
        # The platform's own open panel, asked for through pywebview: an image
        # is one file and any folder, so the wide panel — which is about
        # reading long album folder names — has nothing to add here.
        if not window_box:
            return None
        chosen = window_box[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Images (*.jpg;*.jpeg;*.png;*.webp)", "All files (*.*)"),
        )
        if not chosen:
            return None
        return str(chosen[0])

    def open_path(path: str) -> None:
        # Revealing a folder is the platform's job, like the folder dialog: the
        # application layer receives it as a plain callable.
        command = "open" if sys.platform == "darwin" else "xdg-open"
        # Bounded, because this runs on the bridge's thread: a handler that
        # never returns is a window that never answers again.
        subprocess.run([command, path], check=False, timeout=30)

    def add_application_icon() -> dict[str, object]:
        # Assembling a bundle is the platform's business, so it reaches the
        # application layer as a plain callable like the folder dialog does.
        # It is fast because the icon is carried rather than drawn, which is
        # what lets a window offer it without a worker thread and a waiting
        # state.
        bundle, replaced = make_icon()
        return {"where": str(bundle), "replaced": replaced}

    # A machine that sleeps mid-download fails every transfer in flight. While
    # anything is still coming in, it is asked to stay awake — and asking that
    # is the platform's business, so it arrives here as a plain callable like
    # the folder dialog does.
    awake = wakefulness.Wakefulness(application.logger)
    # The one place a cover's bytes leave this disk for the window. It belongs
    # here because it is the interface talking to itself: a loopback port, a
    # token, and a registry of files the application chose to offer.
    covers = CoverServer(application.logger)
    covers.start()
    api = create_api(
        application,
        folder_picker=pick_folder,
        image_picker=pick_image,
        path_opener=open_path,
        icon_maker=add_application_icon,
        keep_awake=awake.set,
        # Covers travel by URL: a shelf drawn from `data:` URIs is much slower
        # than one drawn from URLs, and every refresh redraws it.
        images=covers.image_url_for,
    )
    threading.Thread(target=api.acquisition_watch_sleep, daemon=True).start()
    window = webview.create_window(
        _WINDOW_TITLE,
        url=str(_WEB_DIRECTORY / "index.html"),
        js_api=api,
        width=1280,
        height=860,
        min_size=(980, 640),
        background_color=_BACKGROUND,
    )
    window_box.append(window)

    def receive_drop(folders: tuple[str, ...]) -> None:
        # Only the platform can say where a dropped folder is, so the reading is
        # done there and the application layer is handed plain paths — the same
        # arrangement as the folder dialog.
        # Called even when the drop carried no folder, because the window is
        # already waiting by then and something has to release it.
        api.adopt_folders(list(folders))

    def attach_drop() -> None:
        # After the page has loaded, because there is no body to listen on
        # before it. Registering is also what switches the native collection of
        # paths on, so it cannot be skipped and read later.
        drops.listen(window, receive_drop)

    window.events.loaded += attach_drop
    # Before the window starts: the platform reads this when it first needs it,
    # and what explains a number on these screens is usually in a tooltip.
    tooltips.appear_sooner()
    webview.start(http_server=True, debug=debug)
    # The window has closed. Nothing is being watched any more, so the machine
    # is released rather than left held by a process nobody will end.
    api.close()
    awake.set(False)
    # And the loopback port: left open, the server keeps its socket, its
    # `serve_forever` thread and the registry of every file this session
    # offered the window for the life of the process.
    covers.stop()
