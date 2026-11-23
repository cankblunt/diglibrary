"""Which sentence the credit field's hint says is the server's answer per track.

An album is no longer all one kind: on an artist's album a track somebody else
leads is named by its whole credit, and the track beside it names its guests
after the title. A hint chosen by a fact about the album says the wrong thing
about one of the two.
"""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "src/diglibrary/ui/web/app.js"


def test_the_hint_is_chosen_by_the_track_and_not_by_the_album() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "hint: track.credit_first ? STR.editArtist : STR.editGuests" in source
    assert "album.is_various ? STR.editArtist" not in source
