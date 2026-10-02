"""What counts as a volume of a series, and what is just a number in a title."""

from diglibrary.library.series import group_series, split_title


def test_an_explicit_marker_is_a_volume() -> None:
    """`Vol.`, `Pt`, `#` — the catalogue said so, so it is one."""
    parts = split_title("Mr. Zorca Record Circle Vol. 4")

    assert parts.stem == "Mr. Zorca Record Circle"
    assert parts.volume == 4
    assert parts.subtitle == ""


def test_a_number_inside_a_title_is_part_of_the_title() -> None:
    """`Vurco de 7 Plimbas, Vol. 1` has a seven in its name and a volume of one."""
    parts = split_title("Vurco de 7 Plimbas, Vol. 1")

    assert parts.stem == "Vurco de 7 Plimbas"
    assert parts.volume == 1


def test_a_subtitle_is_kept_apart_from_the_stem() -> None:
    """What one volume alone carries must survive, whatever a template does."""
    parts = split_title("Zunkin' Further Vol. 5 - The Roots Of Vorca Funk")

    assert parts.stem == "Zunkin' Further"
    assert parts.volume == 5
    assert parts.subtitle == "The Roots Of Vorca Funk"


def test_a_parenthesised_subtitle_is_a_subtitle() -> None:
    """A series may carry each volume's description in brackets, and each differs."""
    parts = split_title("Zunto e Plimba 3 (A Selection Of Coastal Boogie)")

    assert parts.subtitle == "A Selection Of Coastal Boogie"


def test_an_unnumbered_title_states_no_volume() -> None:
    """Most first volumes are unnumbered, and nothing here guesses a number."""
    parts = split_title("Zeke Uno Presents É Vorca Plimba")

    assert parts.volume is None
    assert parts.stem == "Zeke Uno Presents É Vorca Plimba"


def test_a_bare_number_becomes_a_volume_when_a_sibling_vouches() -> None:
    """`Zunkin' Further 4` and `Zunkin' Further 6` are volumes of one run."""
    series = group_series(
        [
            ("a", "VA", "Zunkin' Further 4: The Roots of Vorca Funk"),
            ("b", "VA", "Zunkin' Further 6: The Roots of Vorca Funk"),
        ]
    )

    assert len(series) == 1
    assert [member.parts.volume for member in series[0].members] == [4, 6]
    assert series[0].stem == "Zunkin' Further"


def test_a_lone_bare_number_is_left_alone() -> None:
    """With nothing to compare against, a trailing number is part of a name."""
    series = group_series(
        [
            ("a", "Zarão Plimelho", "Zarão Plimelho 2"),
            ("b", "Vorcão Zurbana", "Dois"),
        ]
    )

    assert series == ()


def test_the_unnumbered_volume_joins_the_run_it_belongs_to() -> None:
    """A first volume with no number, then Vol. 2 and Vol. 3 — one series."""
    series = group_series(
        [
            ("a", "VA", "Zeke Uno Presents É Vorca Plimba"),
            ("b", "VA", "Zeke Uno Presents É Vorca Plimba, Vol. 2"),
            ("c", "VA", "Zeke Uno Presents É Vorca Plimba, Vol. 3"),
        ]
    )

    assert len(series) == 1
    assert len(series[0].members) == 3
    assert [member.reference for member in series[0].unnumbered] == ["a"]
    assert not series[0].is_aligned, "one member spelled unlike the others is the whole point"


def test_a_run_spelled_alike_is_reported_as_aligned() -> None:
    """Most runs are already uniform, and a uniform run needs nothing."""
    series = group_series(
        [
            ("a", "VA", "Plimba Steppers Vol. 1"),
            ("b", "VA", "Plimba Steppers Vol. 2"),
            ("c", "VA", "Plimba Steppers Vol. 3"),
        ]
    )

    assert series[0].is_aligned


def test_two_artists_with_the_same_title_are_not_one_series() -> None:
    """A stem is only a series within one artist's shelf."""
    series = group_series(
        [
            ("a", "Some Artist", "Live Vol. 1"),
            ("b", "Another Artist", "Live Vol. 2"),
        ]
    )

    assert series == ()


def test_accents_and_case_do_not_split_a_run() -> None:
    """`Compilação ZUMA` and `Compilação Zuma` are the same shelf."""
    series = group_series(
        [
            ("a", "VA", "Zop Tun - Compilação ZUMA Vol. 1"),
            ("b", "VA", "Zop Tun - Compilação Zuma Vol. 2"),
        ]
    )

    assert len(series) == 1
    assert series[0].stem == "Zop Tun - Compilação ZUMA", "the dash here is inside the name"
    assert [member.parts.volume for member in series[0].members] == [1, 2]


def test_a_dash_inside_a_name_is_not_a_subtitle() -> None:
    """The marker is found past the whole name, so the volume is not lost with half the title."""
    parts = split_title("Zeke Uno Presents É Vorca Plimba, Vol. 2")

    assert parts.stem == "Zeke Uno Presents É Vorca Plimba"
    assert parts.volume == 2
    assert parts.subtitle == ""


def test_a_disc_is_not_a_volume() -> None:
    """`Dub Anthology (CD1)` and its siblings are one release, not four volumes.

    Reading `CD` as a volume marker would offer to renumber a boxed set, and
    multi-disc albums already have their own layout.
    """
    parts = split_title("Dub Anthology CD2")

    assert parts.volume is None
    assert parts.stem == "Dub Anthology CD2"


def test_albums_sharing_a_title_are_not_a_series() -> None:
    """Three copies of `ZORCA` — a duplicate and an instrumental — have no order."""
    series = group_series(
        [
            ("a", "ZBC, Vhorr", "ZORCA"),
            ("b", "ZBC, Vhorr", "ZORCA (Instrumental)"),
            ("c", "ZBC, Vhorr", "ZORCA"),
        ]
    )

    assert series == ()
