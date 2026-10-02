"""A decoration this application wrote into a file name is not the user's to keep again.

The catalogue writes `Canto Só (feat: Lia Moraes)`; the file is named `(feat -
Lia Moraes)`, because a colon is not stored in a name. Read back as an aside the
user had written, it would be appended to a title that already carries it.
"""

import pytest

from diglibrary.library.naming import sanitize_component
from diglibrary.library.planner import _with_kept_decorations


@pytest.mark.parametrize(
    "title",
    [
        "Canto Só (feat: Lia Moraes)",
        'Zorvel Quay (Poem: "The Drim Of Old Zorvel")',
        "Plimwick Session (rehearsal / Varnholt, QX, take 3)",
    ],
)
def test_the_title_s_own_aside_as_the_file_name_writes_it_is_not_repeated(title: str) -> None:
    stem = sanitize_component(f"04. {title}")
    assert _with_kept_decorations(title, stem) == title


def test_an_aside_only_the_file_carries_is_still_kept() -> None:
    """What the catalogue leaves out rides along."""
    assert _with_kept_decorations("Parado", "01 - Parado (feat. Beto Lins)") == (
        "Parado (feat. Beto Lins)"
    )


def test_an_aside_the_title_carries_in_another_case_is_not_repeated() -> None:
    assert _with_kept_decorations(
        "Canto Só (Feat: Lia Moraes)", "04. Canto Só (feat - Lia Moraes)"
    ) == ("Canto Só (Feat: Lia Moraes)")


def test_words_stay_whole_so_a_short_aside_is_not_swallowed() -> None:
    assert _with_kept_decorations("Zunda (Remixed)", "03. Zunda (Mix)") == "Zunda (Remixed) (Mix)"
