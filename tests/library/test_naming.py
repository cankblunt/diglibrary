"""Unit tests for the naming policy and its templates."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from diglibrary.application.contracts import (
    ArtistMetadata,
    MetadataSources,
    ReleaseMetadata,
    TrackMetadata,
)
from diglibrary.library.audio import AudioProperties
from diglibrary.library.naming import (
    NAMING_STYLES,
    NamingError,
    NamingPolicy,
    TrackFacts,
    existing_designator,
    existing_kind,
    format_label,
    is_various,
    odd_track_labels,
    replace_format_segment,
    sanitize_component,
)


def test_default_templates_reproduce_the_library_s_existing_pattern() -> None:
    """The shipped default renders the `Artist - Album (year) [FORMAT]` pattern."""
    policy = NamingPolicy()
    release = _release("LANTERNE", artists=("QRX & VELLUMO",), year=2011)

    folder = policy.folder_name(release, "FLAC")
    track = policy.track_name(
        release,
        title="Let Me Quarrel",
        track_number=3,
        disc_number=1,
        disc_count=1,
        track_artist=None,
        extension=".flac",
    )

    assert folder == "QRX & VELLUMO - LANTERNE (2011) [FLAC]"
    assert track == "03. Let Me Quarrel.flac"


def test_a_various_artists_release_credits_each_track() -> None:
    """A compilation names the track's own artist; the folder carries the label."""
    policy = NamingPolicy()
    release = _release("Quarrel Wave", artists=("Various",), year=2015)

    folder = policy.folder_name(release, "FLAC")
    track = policy.track_name(
        release,
        title="Some Lantern",
        track_number=1,
        disc_number=1,
        disc_count=1,
        track_artist="Some Artist",
        extension=".flac",
    )

    assert folder == "VA - Quarrel Wave (2015) [FLAC]"
    assert track == "01. Some Artist - Some Lantern.flac"


def test_a_collaboration_is_not_a_compilation() -> None:
    """Two credited artists is a collaboration; only the source declares Various."""
    policy = NamingPolicy()
    release = _release("LANTERNE", artists=("QRX", "VELLUMO"), year=2011)

    assert policy.folder_name(release, "FLAC").startswith("QRX & VELLUMO")


def test_multi_disc_tracks_carry_a_disc_prefix() -> None:
    """The D-NN form appears only when the release actually has more than one disc."""
    policy = NamingPolicy()
    release = _release("Anthology", artists=("Some Artist",), year=1998)

    single = policy.track_name(
        release, "Track", 4, disc_number=1, disc_count=1, track_artist=None, extension=".flac"
    )
    multi = policy.track_name(
        release, "Track", 4, disc_number=2, disc_count=2, track_artist=None, extension=".flac"
    )

    assert single == "04. Track.flac"
    assert multi == "2-04. Track.flac"


def test_an_unknown_year_leaves_no_empty_parentheses() -> None:
    """An optional segment disappears whole rather than rendering punctuation."""
    policy = NamingPolicy()
    release = _release("Untitled", artists=("Artist",), year=None)

    assert policy.folder_name(release, "FLAC") == "Artist - Untitled [FLAC]"
    assert policy.folder_name(release, "") == "Artist - Untitled"


def test_values_are_never_rewritten_by_the_policy() -> None:
    """The template arranges what the source published, it does not edit it."""
    policy = NamingPolicy()
    release = _release("all lowercase: a STRANGE.title", artists=("z.q. marrow",), year=1992)

    folder = policy.folder_name(release, "FLAC")

    assert "all lowercase" in folder
    assert "z.q. marrow" in folder
    assert "STRANGE" in folder


def test_a_custom_template_is_honoured() -> None:
    """The template is configuration."""
    policy = NamingPolicy(
        folder_template="%year% — %albumartist% — %album%",
        track_template="%track% %title%",
    )
    release = _release("Album", artists=("Artist",), year=1970)

    assert policy.folder_name(release, "FLAC") == "1970 — Artist — Album"
    assert (
        policy.track_name(release, "Song", 7, 1, 1, None, ".mp3", track_number_width=3)
        == "007 Song.mp3"
    )


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("%artist% - %nonsense%", "unknown placeholders"),
        ("   ", "empty"),
        ("%album% {(%year%)", "unbalanced"),
        ("no placeholders here", "no placeholder"),
    ],
)
def test_a_broken_template_is_rejected_when_configured(template: str, message: str) -> None:
    """A typo in a setting must not surface at the moment of a rename."""
    with pytest.raises(NamingError, match=message):
        NamingPolicy(folder_template=template)


def test_a_path_separator_can_never_survive_in_a_name() -> None:
    """A title containing a slash must not silently create a directory level."""
    assert sanitize_component("QX/ZR") == "QX-ZR"
    assert sanitize_component("  spaced   out  ") == "spaced out"

    with pytest.raises(NamingError, match="empty"):
        sanitize_component("   ")


def test_a_name_that_is_only_dots_is_refused_because_it_is_a_path_step() -> None:
    """`..` holds no separator, so every other rule here lets it through.

    It is the one name that would move an album *out* of where it was planned:
    the planner sanitizes a folder name typed by hand and otherwise takes it
    verbatim, so a `..` in that field is a rename into the parent's parent. `.`
    is the same shape and means this folder.
    """
    for step in ("..", ".", "...", " .. "):
        with pytest.raises(NamingError, match="path step"):
            sanitize_component(step)

    # Neighbours that must survive: a dot inside or at the end of a real name is
    # a full stop, not a path step.
    assert sanitize_component("Mr.") == "Mr."
    assert sanitize_component("...And Lanterns For All") == "...And Lanterns For All"
    assert sanitize_component("A.") == "A."


def test_any_word_written_in_the_designator_convention_is_kept() -> None:
    """The application never invents a designator, so one found in a folder was written there.

    A closed list of known words would delete any other word from a folder
    that was named exactly right.
    """
    kept = [
        ("The Marrowbys - Quarrelino (Remastered - 1994) [FLAC]", "Quarrelino", "Remastered"),
        (
            "Zedd Q7 - Over The Quarry (Lanternia Records - 2015)",
            "Over The Quarry",
            "Lanternia Records",
        ),
        ("VA - Quarrel Tools (Marrow Gold Records - 2014)", "Quarrel Tools", "Marrow Gold Records"),
        # Canonical casing still wins for the words this project knows by name.
        ("Artist - Album (ep - 2015)", "Album", "EP"),
    ]
    for folder, title, expected in kept:
        assert existing_kind(folder, title) == expected


def test_each_parenthesis_carries_its_own_year_and_only_the_years_there_are() -> None:
    """Each parenthesis carries its own year, and a single year is written once."""
    policy = NamingPolicy(include_edition=True)
    reissue = _release("In Through The Quarry Yard", artists=("Zed Marrowby",), year=1979)
    reissue = replace(reissue, released_on=date(1994, 1, 1), original_released_on=date(1979, 1, 1))
    single_year = _release("Quarrelino", artists=("The Marrowbys",), year=1994)

    assert policy.folder_name(reissue, "MP3 320", kind="Remaster", kind_year="1994") == (
        "Zed Marrowby - In Through The Quarry Yard (Remaster - 1994) (1979) [MP3 320]"
    )
    assert policy.folder_name(reissue, "FLAC") == (
        "Zed Marrowby - In Through The Quarry Yard (Ed. 1994) (1979) [FLAC]"
    )
    assert policy.folder_name(single_year, "FLAC", kind="Remastered") == (
        "The Marrowbys - Quarrelino (Remastered - 1994) [FLAC]"
    )
    assert policy.folder_name(single_year, "FLAC") == "The Marrowbys - Quarrelino (1994) [FLAC]"


def test_a_remaster_written_any_way_is_read_with_the_year_it_carries() -> None:
    """A remaster is read in braces, with the year before the word, or with no year at all."""
    assert existing_designator("Zed Marrowby - Quarrelo (1982) {1994 Remaster}", "Quarrelo") == (
        ("Remaster", "1994")
    )
    assert (
        existing_designator(
            "Mirén Zoélle - Lantern Quarrel (2013 Remaster) (1973)", "Lantern Quarrel"
        )[1]
        == "2013"
    )
    assert existing_designator("Artist - Album {Remaster}", "Album") == ("Remaster", "")
    # Only remaster words are read this loosely: everything else must be written
    # in the designator convention or be a designator this project knows.
    assert existing_designator("Some Artist - Live at Marrowby (1970)", "Live at Marrowby") == (
        "",
        "",
    )


def test_a_word_the_catalogue_itself_carries_is_title_not_designator() -> None:
    """A word the title itself carries is title, and that holds for any word."""
    folder = "Zed Marrowby - Marrowby Quarrel Onstage (Onstage - 2019)"

    assert existing_kind(folder, "Marrowby Quarrel Onstage") == ""
    assert existing_kind(folder, "Another Title") == "Onstage"


def test_a_bare_year_is_not_a_designator() -> None:
    """`(1994)` is the year segment this project writes itself, not something to keep."""
    assert existing_kind("Artist - Album (1994) [FLAC]", "Album") == ""


def test_a_colon_never_reaches_a_folder_the_finder_will_show(tmp_path: Path) -> None:
    """macOS stores a colon and then draws it as the separator itself.

    A title with a colon would be read in the Finder with a slash in its place,
    which is the one thing a name may never look like.
    """
    assert sanitize_component("Siftin' Lanterns 5: The Quarry") == "Siftin' Lanterns 5 - The Quarry"
    # A colon inside a number is not punctuation and must not grow a space.
    assert sanitize_component("10:15 Lantern Night") == "10-15 Lantern Night"

    written = tmp_path / sanitize_component("Siftin' Lanterns 5: The Quarry")
    written.mkdir()
    assert ":" not in written.name and "/" not in written.name


def test_format_label_reports_a_mixed_folder_honestly() -> None:
    """A folder holding two formats says so instead of picking one."""
    assert format_label([_stream("flac")]) == "FLAC"
    assert format_label([_stream("mp4a.40.2")]) == "M4A"
    assert format_label([_stream("alac")]) == "ALAC"
    assert format_label([_stream("flac"), _stream("mp3", 320_000)]) == "FLAC, MP3 320"
    assert format_label([]) == ""


def test_only_a_lossy_format_carries_its_bitrate() -> None:
    """FLAC is FLAC; two MP3 rips of one album are told apart by the number."""
    assert format_label([_stream("flac", 900_000)]) == "FLAC"
    assert format_label([_stream("mp3", 320_000)]) == "MP3 320"
    assert format_label([_stream("mp3", 244_800)]) == "MP3 245"
    assert format_label([_stream("mp3")]) == "MP3", "an unmeasured bitrate is not invented"


def test_a_mixed_bitrate_folder_names_both_qualities() -> None:
    """The label says what is in the folder, not what its floor is.

    Naming only the worst track hides that the album is mixed at all, which is
    the one thing a mixed folder most needs to say.
    """
    label = format_label([_stream("mp3", 320_000), _stream("mp3", 192_000)])

    assert label == "MP3 192, MP3 320"


def test_every_shipped_style_renders_the_name_it_advertises() -> None:
    """A style's description is what the user chooses by, so it must be true."""
    release = _release("A Lantérn of Quarrel", artists=("Zed Marrowby",), year=1974)
    rendered = {
        name: NamingPolicy.from_style(name).folder_name(release, "FLAC") for name in NAMING_STYLES
    }

    assert rendered == {
        "spotiflac": "Zed Marrowby - A Lantérn of Quarrel (1974) [FLAC]",
        "bracketed_year": "Zed Marrowby - A Lantérn of Quarrel [1974]",
        "year_first": "(1974) Zed Marrowby - A Lantérn of Quarrel [FLAC]",
        "year_only": "Zed Marrowby - A Lantérn of Quarrel (1974)",
        "year_and_format": "Zed Marrowby - A Lantérn of Quarrel (1974) [FLAC]",
        "minimal": "Zed Marrowby - A Lantérn of Quarrel",
    }


def test_the_two_yearly_styles_never_name_the_pressing() -> None:
    """These two styles carry no edition, and without one is what they are.

    `include_edition` switches the edition off wherever a template puts it.
    These leave no place for it at all, which is a different thing to want: a
    reissue of a 1974 record is still named for 1974 here.
    """
    reissue = _release("A Lantérn of Quarrel", artists=("Zed Marrowby",), year=1974)

    for name, expected in (
        ("year_only", "Zed Marrowby - A Lantérn of Quarrel (1974)"),
        ("year_and_format", "Zed Marrowby - A Lantérn of Quarrel (1974) [FLAC]"),
    ):
        policy = NamingPolicy.from_style(name)
        assert "%edition%" not in policy.folder_template
        assert policy.folder_name(reissue, "FLAC") == expected


def test_a_style_that_never_names_the_pressing_still_keeps_a_written_word() -> None:
    """Refusing the pressing and dropping a written designator are two refusals.

    `%kind%` carries what was written on the folder by hand, read back by
    `existing_designator`, and not a decoration this application chose. A style
    without an edition still has to keep it.
    """
    reissue = _release("Élan Never Quarrels", artists=("Marrowby",), year=1977)

    for name, expected in (
        ("year_only", "Marrowby - Élan Never Quarrels (Remastered - 1977)"),
        ("year_and_format", "Marrowby - Élan Never Quarrels (Remastered - 1977) [FLAC]"),
    ):
        policy = NamingPolicy.from_style(name)
        assert "%edition%" not in policy.folder_template
        rendered = policy.folder_name(reissue, "FLAC", kind="Remastered", kind_year="1977")
        assert rendered == expected


def test_a_style_can_be_taken_with_one_field_changed() -> None:
    """Wanting a shipped arrangement with another disc folder is not a fork of it."""
    policy = NamingPolicy.from_style("minimal", disc_folder_template="Disc %disc%")

    assert policy.folder_template == NAMING_STYLES["minimal"].folder_template
    assert policy.disc_folder_name(2) == "Disc 2"


def test_an_unknown_style_is_refused_by_name() -> None:
    """A typo in a setting must say what the valid answers are."""
    with pytest.raises(NamingError, match="spotiflac"):
        NamingPolicy.from_style("spotifiac")


def test_the_edition_segment_can_be_switched_off() -> None:
    """The pressing can be named beside the album's year; some libraries do not want it."""
    reissue = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="A Lantérn of Quarrel",
        artists=(ArtistMetadata(name="Zed Marrowby"),),
        released_on=date(2006, 1, 1),
        original_released_on=date(1974, 1, 1),
    )

    with_edition = NamingPolicy(include_edition=True).folder_name(reissue, "FLAC")
    without = NamingPolicy().folder_name(reissue, "FLAC")

    assert with_edition == "Zed Marrowby - A Lantérn of Quarrel (Ed. 2006) (1974) [FLAC]"
    assert without == "Zed Marrowby - A Lantérn of Quarrel (1974) [FLAC]"
    assert "2006" not in without, "the pressing is not named anywhere else either"


def _stream(codec: str, bitrate: int | None = None) -> AudioProperties:
    return AudioProperties(
        codec=codec, duration_ms=1000, sample_rate=44_100, channels=2, bitrate=bitrate
    )


def _release(title: str, artists: tuple[str, ...], year: int | None = None) -> ReleaseMetadata:
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title=title,
        artists=tuple(ArtistMetadata(name=name) for name in artists),
        released_on=date(year, 1, 1) if year else None,
    )


def test_a_folder_is_named_with_the_separator_the_source_publishes() -> None:
    """The wiring, not the part: the name a folder actually gets.

    A source may publish a word such as `Presents` between two artists, and the
    folder must carry it instead of a hard-coded ampersand. Written against the
    folder name because that is what reaches the disk: a test of the joining
    function alone could pass while the name went on saying `&`.
    """
    policy = NamingPolicy()
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="The Importance of Being Lanterns",
        artists=(
            ArtistMetadata(name="Mirén Zoélle", joined_by="Presents"),
            ArtistMetadata(name="The Lantern Band Of Zoélle"),
        ),
        released_on=date(2011, 1, 1),
    )

    assert policy.folder_name(release, "FLAC") == (
        "Mirén Zoélle Presents The Lantern Band Of Zoélle - "
        "The Importance of Being Lanterns (2011) [FLAC]"
    )


def test_a_reissue_is_named_by_the_album_s_own_year() -> None:
    """The album is from 1974 and the CD from 2006: the album's year names it."""
    policy = NamingPolicy(include_edition=True)
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="A Lantérn of Quarrel",
        artists=(ArtistMetadata(name="Zed Marrowby Zed"),),
        released_on=date(2006, 1, 1),
        original_released_on=date(1974, 1, 1),
    )

    assert (
        policy.folder_name(release, "FLAC")
        == "Zed Marrowby Zed - A Lantérn of Quarrel (Ed. 2006) (1974) [FLAC]"
    )


def test_an_original_pressing_carries_no_edition() -> None:
    """The edition is named only when it actually differs from the album's year."""
    policy = NamingPolicy()
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="r1",
        title="Quarrélia",
        artists=(ArtistMetadata(name="Zed Marrowby"),),
        released_on=date(1971, 1, 1),
        original_released_on=date(1971, 1, 1),
    )

    assert policy.folder_name(release, "FLAC") == "Zed Marrowby - Quarrélia (1971) [FLAC]"


def test_a_folder_names_every_quality_it_holds() -> None:
    """Every quality a folder holds is named, comma separated.

    The transcoded word replaces the codec rather than qualifying it: a folder
    holding honest FLAC, a transcode and an MP3 says all three, and the
    transcode is named for what it is rather than for the container it wears.
    """
    files = (_stream("flac"), _stream("flac"), _stream("mp3", 128_000))

    label = format_label(files, (False, True, False), "Fake")

    assert label == "FLAC, Fake, MP3 128"


def test_two_constant_bitrates_are_named_as_two_qualities() -> None:
    """128 beside 320 is two rips, and hiding that defeats the point of the label."""
    files = (_stream("mp3", 128_000), _stream("mp3", 320_000))

    assert format_label(files) == "MP3 128, MP3 320"


def test_a_variable_bitrate_rip_is_one_label_not_eight() -> None:
    """One variable-bitrate album spans many averages.

    A CBR encoder reports a nominal rate exactly; a VBR one reports an average.
    None of these is nominal, so this is one album breathing, not six
    qualities, and naming each would make an unreadable folder.
    """
    files = tuple(_stream("mp3", rate * 1000) for rate in (167, 178, 180, 182, 184, 188))

    assert format_label(files) == "MP3 VBR"


def test_an_album_that_agrees_with_itself_reads_as_one_label() -> None:
    """The common case must not pay for the mixed one."""
    assert format_label((_stream("flac"),) * 12) == "FLAC"


def test_the_transcoded_word_is_configurable() -> None:
    """It lands in folder names, in whatever language those are written."""
    files = (_stream("flac"),)

    assert format_label(files, (True,)) == "Lossy"
    assert format_label(files, (True,), "Fake") == "Fake"


def test_a_lossy_origin_track_says_the_word_whether_or_not_a_rate_was_measured() -> None:
    """One situation has one name, whether or not a rate was measured.

    A track marked `[128 kbps]` where the audio had been measured and `[Lossy]`
    where it had not would wear two names for one situation, and some verdicts
    are reached by a path that yields no bitrate by design.

    So the same album, measured and unmeasured, reads the same. The rate is
    still on record and shown per track on the Quality screen; it does not
    reach a name.
    """
    album = [_stream("flac"), _stream("flac"), _stream("flac")]

    with_rate = odd_track_labels(album, [False, True, False], "Lossy", [None, 128, None])
    without = odd_track_labels(album, [False, True, False], "Lossy", [None, None, None])

    assert with_rate == (None, "Lossy", None)
    assert with_rate == without, "the name does not depend on whether it was measured"


def test_a_lossy_origin_track_keeps_the_word_when_no_number_is_attributable() -> None:
    """An absent number means nobody could attribute one.

    A bitrate reaches the application only when its measurement is provably
    that file's. Without this, a file could take into its name a number
    measured from another recording of the same shape.
    """
    album = [_stream("flac"), _stream("flac"), _stream("flac")]

    marks = odd_track_labels(album, [False, True, False], "Lossy", [None, None, None])

    assert marks == (None, "Lossy", None)


def test_the_folder_says_the_word_and_the_track_says_the_number() -> None:
    """The folder carries the word only, never the rate.

    A rate reaches this only once the audio has been measured, so a folder
    carrying it would wear two names for one situation, and would rename itself
    because a measurement later succeeded.
    """
    album = [_stream("flac"), _stream("flac")]

    assert format_label(album, [False, True], "Lossy", [None, 128]) == "FLAC, Lossy"


def test_an_honest_lossy_track_still_reads_its_declared_rate() -> None:
    """The tilde belongs to an inference. A real MP3 declares its bitrate and says so."""
    album = [_stream("mp3", 320_000), _stream("mp3", 128_000), _stream("mp3", 320_000)]

    marks = odd_track_labels(album, [False, False, False], "Lossy")

    assert marks == (None, "128kbps", None)


def test_an_album_wholly_lossy_at_one_rate_says_it_once_on_the_folder() -> None:
    """An album wholly lossy says so once, on the folder; tracks are marked only when they differ.

    The folder already speaks for every file in it, so repeating the mark on
    each one would be noise, which is the reasoning that governs the per-track
    suffix.
    """
    album = [_stream("flac") for _ in range(4)]

    # The folder says the word; the tracks stay silent because they all agree.
    assert format_label(album, [True] * 4, "Lossy", [128] * 4) == "Lossy"
    assert odd_track_labels(album, [True] * 4, "Lossy", [128] * 4) == (None,) * 4


def test_an_album_of_several_rates_says_lossy_once_and_leaves_the_files_alone() -> None:
    """Tracks of lossy origin at several rates read alike inside one folder.

    The cost of the uniform word is that such files are not told apart by
    name. The Quality screen still tells them apart, per track.
    """
    album = [_stream("flac") for _ in range(4)]
    rates = [128, 128, 320, 320]

    # The word once, on the folder. The files agree with the folder and with
    # each other, so there is nothing for a suffix to distinguish.
    assert format_label(album, [True] * 4, "Lossy", rates) == "Lossy"
    assert odd_track_labels(album, [True] * 4, "Lossy", rates) == (None,) * 4


def test_a_name_cannot_carry_a_character_that_reverses_how_it_reads() -> None:
    """U+202E is the classic way a filename shows one extension and carries
    another, and this application's whole job is writing filenames from strings
    a catalogue published or a stranger wrote into a tag."""
    assert sanitize_component("Amber‮Yard‬") == "AmberYard"
    assert sanitize_component("Song⁦Title⁩") == "SongTitle"


def test_two_names_differing_by_nothing_visible_do_not_become_two_files() -> None:
    """A zero-width character walks straight past the collision check: folding
    case and accent has no reason to expect a character with no width, so two
    tracks land under names nobody can tell apart."""
    assert sanitize_component("Interlude") == sanitize_component("Inter​lude")
    assert sanitize_component("Inter﻿lude") == "Interlude"


def test_control_characters_never_reach_a_filename() -> None:
    """They survive every other rule here — they are neither separator nor NUL."""
    assert sanitize_component("Bad\x01Name\x7f") == "BadName"
    assert (
        sanitize_component("Tab\tSeparated") == "Tab Separated"
    ), "tabs are whitespace and fold into the single space, rather than vanishing"


def test_a_word_the_source_published_is_never_deleted_to_suit_the_scanner() -> None:
    """A leading dot hides the folder from this application's own scan, and the
    fix for that is not to delete it: a title may begin with an ellipsis, and a
    value its source published is arranged, never rewritten. Pinned so the
    one-line change cannot come back without an argument.
    """
    assert sanitize_component("...And Lanterns For All") == "...And Lanterns For All"


def test_a_name_can_be_composed_from_what_the_catalogue_published() -> None:
    """A name can be composed from the fields a catalogue publishes.

    The engine has placeholders and optional groups; beside title, artist and
    year it knows country, label, catalogue number, genre and style, which are
    what a source publishes for most releases.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Quarrélia",
        artists=(ArtistMetadata(name="Marrowby"),),
        released_on=date(1996, 1, 1),
        labels=("Lantern Latin", "Quarry Records"),
        catalog_numbers=("LQZ-10001", "2-100002"),
        country="US",
        genres=("Latin",),
        styles=("Polka", "Waltz"),
    )
    policy = NamingPolicy(
        folder_template="%albumartist% - %album%{ (%year%)}{ [%format%]}{ (%label% %catalog%)}",
    )

    name = policy.folder_name(release, "FLAC")

    assert (
        name == "Marrowby - Quarrélia (1996) [FLAC] (Lantern Latin LQZ-10001)"
    ), "the first of each, because a name holds one"


def test_a_brace_cannot_be_a_literal_in_a_name_and_that_is_declared() -> None:
    """The one thing the free template cannot express, written down rather than met by surprise.

    `{` and `}` are the optional group, so a template asking for literal braces
    — `{ {%label%}}` — has its inner pair eaten as a group and produces the
    value with no braces around it. Nothing is lost and nothing is wrong, but
    somebody composing a name will try it: parentheses and brackets are literal,
    braces are syntax.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Quarrélia",
        artists=(ArtistMetadata(name="Marrowby"),),
        labels=("Lantern Latin",),
    )

    name = NamingPolicy(folder_template="%albumartist% - %album%{ {%label%}}").folder_name(
        release, ""
    )

    assert name == "Marrowby - Quarrélia Lantern Latin", "the braces are syntax, not decoration"


def test_what_the_catalogue_left_empty_takes_its_group_with_it() -> None:
    """A release may have no catalogue number, and its group leaves with it.

    A placeholder that renders empty must not leave its decoration behind: an
    empty `{}` in a folder name is the stray-parenthesis failure the optional
    group exists to prevent.
    """
    bare = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Quarrélia",
        artists=(ArtistMetadata(name="Marrowby"),),
        released_on=date(1996, 1, 1),
    )
    policy = NamingPolicy(
        folder_template="%albumartist% - %album%{ (%year%)}{ {%label% %catalog%}}{ [%country%]}",
    )

    assert policy.folder_name(bare, "FLAC") == "Marrowby - Quarrélia (1996)"


def test_a_track_is_named_with_the_facts_its_own_file_carries() -> None:
    """Two rips of one album are not one album, and the catalogue cannot tell them apart.

    These four come from the stream header the scan already read, so they are
    available for every file, unlike the measured ones.
    """
    release = _release("Quarrélia", ("Marrowby",), 1996)
    policy = NamingPolicy(
        track_template="%track%. %title%{ [%bitdepth%bit %samplerate%kHz]}{ [%duration%]}",
    )

    name = policy.track_name(
        release,
        title="Zêd",
        track_number=2,
        disc_number=1,
        disc_count=1,
        track_artist=None,
        extension=".flac",
        facts=TrackFacts(bit_depth=16, sample_rate=44100, duration_ms=225_400),
    )

    assert name == "02. Zêd [16bit 44.1kHz] [3m45s].flac"
    # Minutes and seconds joined by a colon would reach macOS with a hyphen and
    # read as a range: the colon is kept out of every name.
    assert ":" not in name


def test_a_key_is_written_only_where_one_was_measured() -> None:
    """The key is written only on tracks that have been analysed.

    So one template serves both: the measured track carries its key, and the
    unmeasured one produces an ordinary name rather than a gap, a placeholder,
    or a guess. Nothing measures anything on the way to a rename.
    """
    release = _release("Quarrélia", ("Marrowby",), 1996)
    policy = NamingPolicy(track_template="{%key% - }%track%. %title%{ [%bpm%]}")
    common = {
        "release": release,
        "title": "Zêd",
        "track_number": 2,
        "disc_number": 1,
        "disc_count": 1,
        "track_artist": None,
        "extension": ".flac",
    }

    measured = policy.track_name(**common, facts=TrackFacts(camelot="8A", bpm=123.6))
    unmeasured = policy.track_name(**common, facts=TrackFacts())

    assert measured == "8A - 02. Zêd [124].flac", "the DJ's name begins with the key"
    assert unmeasured == "02. Zêd.flac", "and nothing is invented for the rest"


def test_the_classical_notation_is_a_second_token_not_a_setting() -> None:
    """Both notations are offered, chosen in the template.

    `%key%` is the Camelot code DJ software reads; `%keyname%` is what anyone
    else reads. Two tokens rather than one token plus a preference, so a
    template is readable on its own.
    """
    release = _release("Quarrélia", ("Marrowby",), 1996)
    common = {
        "release": release,
        "title": "Zêd",
        "track_number": 2,
        "disc_number": 1,
        "disc_count": 1,
        "track_artist": None,
        "extension": ".flac",
    }
    facts = TrackFacts(camelot="8A", key_name="A minor")

    camelot = NamingPolicy(track_template="{%key% - }%track%. %title%").track_name(
        **common, facts=facts
    )
    classical = NamingPolicy(track_template="%track%. %title%{ (%keyname%)}").track_name(
        **common, facts=facts
    )

    assert camelot == "8A - 02. Zêd.flac"
    assert classical == "02. Zêd (A minor).flac"


def test_an_album_is_not_in_a_key() -> None:
    """A folder template cannot name what only one recording has.

    Twelve tracks hold up to twelve keys, and a folder name carrying one of them
    would be a confident statement about eleven files it is not true of. Refused
    where a template is configured, which is the only moment before a rename.
    """
    for placeholder in ("key", "keyname", "bpm", "bitdepth", "duration"):
        with pytest.raises(NamingError) as refused:
            NamingPolicy(folder_template=f"%albumartist% - %album% [%{placeholder}%]")
        assert placeholder in str(refused.value)


def test_the_catalogues_own_row_number_does_not_reach_a_folder_name() -> None:
    """The suffix comes off the name, and only the name.

    Discogs appends `(2)`, `(3)` to tell its own duplicate entries apart. It is
    not part of the label's name, and it says nothing to whoever reads the
    folder.

    Only where a name is composed: the tag still carries what the source
    published.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Quarrélio",
        artists=(ArtistMetadata(name="Zed E Marrow"),),
        released_on=date(1976, 1, 1),
        labels=("Lanternal (3)",),
        catalog_numbers=("1-02-303-004",),
    )
    policy = NamingPolicy(
        folder_template="%albumartist% - %album%{ (%year%)}{ (%label% %catalog%)}"
    )

    name = policy.folder_name(release, "FLAC")

    assert name == "Zed E Marrow - Quarrélio (1976) (Lanternal 1-02-303-004)"
    # A number that is part of the name stays, and `(3)` is a row. Only a
    # parenthesised number at the very end, after a space, is one.
    kept = replace(release, labels=("Quarry 242",))
    assert "Quarry 242" in policy.folder_name(kept, "FLAC")


@pytest.mark.parametrize(
    ("written", "label", "expected"),
    [
        # A name corrected by hand to be rid of a bitrate must not freeze this
        # application's `Lossy` along with it.
        (
            "Marrowby - Lantern Remixes (2013) [FLAC, Lossy]",
            "FLAC",
            "Marrowby - Lantern Remixes (2013) [FLAC]",
        ),
        (
            "Marrowby - Lantern Remixes (2013) [FLAC, Lossy 128 kbps]",
            "FLAC",
            "Marrowby - Lantern Remixes (2013) [FLAC]",
        ),
        # A bracket written by hand that is not a format. Both of them
        # survive: the format is replaced where it is, in place.
        (
            "Zed Marrow - Swear I Quarrel (1975) [FLAC] [3 tracks missing]",
            "FLAC, Lossy",
            "Zed Marrow - Swear I Quarrel (1975) [FLAC, Lossy] [3 tracks missing]",
        ),
        # Nothing is appended to a name that carries no format segment: the
        # brackets may have been taken off deliberately, and putting them back
        # would be inventing a name nobody asked for.
        ("Someone - Record (1980)", "FLAC", "Someone - Record (1980)"),
        ("Someone - Record [3 tracks missing]", "FLAC", "Someone - Record [3 tracks missing]"),
        # A word that is not a codec is left alone even when the format follows.
        (
            "Someone - Onstage [Deluxe] (1980) [MP3 320]",
            "FLAC",
            "Someone - Onstage [Deluxe] (1980) [FLAC]",
        ),
    ],
)
def test_a_corrected_name_keeps_its_words_and_not_this_apps_format(
    written: str, label: str, expected: str
) -> None:
    """The format segment is a measurement, so it is never frozen.

    A folder name corrected by hand is taken verbatim, and the name being
    corrected is one this application wrote, so the label it had put there
    would become part of the typed name. Before preserving what is on the
    disk, ask whether this application could have put it there.
    """
    assert replace_format_segment(written, label) == expected


def _curated(
    title: str, curator: str, track_artists: tuple[tuple[str, ...], ...]
) -> ReleaseMetadata:
    """A release credited to one person whose tracks are credited to others."""
    return ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="c1",
        title=title,
        artists=(ArtistMetadata(name=curator),),
        tracks=tuple(
            TrackMetadata(
                title=f"Track {position}",
                position=position,
                artists=tuple(ArtistMetadata(name=name) for name in names),
                duration_ms=200_000,
            )
            for position, names in enumerate(track_artists, start=1)
        ),
        released_on=date(2013, 1, 1),
    )


def test_a_compilation_credited_to_its_curator_is_still_a_compilation() -> None:
    """A compilation credited to whoever compiled it is still a compilation.

    A catalogue may credit the release to its curator, so the literal test for
    `Various` answers no, and `{%artist% - }` is an optional group, so the
    credit that tells unrelated songs apart leaves the file names without
    anything failing. Written against the rendered name rather than against
    the predicate alone.
    """
    policy = NamingPolicy()
    release = _curated(
        "É Lantern Quarrel Vol. 2",
        "Zed Marrowby",
        (("The Lanterners",), ("Quarrelle",), ("Mirén Zoélle",)),
    )

    assert is_various(release) is True
    assert policy.folder_name(release, "FLAC") == "VA - É Lantern Quarrel Vol. 2 (2013) [FLAC]"
    assert (
        policy.track_name(
            release,
            title="Children Of The Quarry",
            track_number=1,
            disc_number=1,
            disc_count=1,
            track_artist="The Lanterners",
            extension=".flac",
        )
        == "01. The Lanterners - Children Of The Quarry.flac"
    )


def test_an_album_whose_guests_are_credited_per_track_is_not_a_compilation() -> None:
    """*The tracklist names more than one artist* alone is too wide a reading.

    It would turn every record with a guest into a compilation. What separates
    them is that the album's own artist is on those tracks.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="g1",
        title="An Album",
        artists=(ArtistMetadata(name="Zed Marrowby"),),
        tracks=(
            TrackMetadata(title="One", position=1, artists=(ArtistMetadata(name="Zed Marrowby"),)),
            TrackMetadata(
                title="Two",
                position=2,
                artists=(ArtistMetadata(name="Zed Marrowby"), ArtistMetadata(name="Quarry 70")),
            ),
        ),
        released_on=date(1999, 1, 1),
    )

    assert is_various(release) is False


def test_a_best_of_by_one_artist_is_not_a_compilation() -> None:
    """*The source declares a compilation* alone is too wide a reading.

    A catalogue marks a best-of by one artist `Compilation` in `formats`. A
    rule reading that alone would write the artist's name on every track of
    that artist's own record.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="b1",
        title="Nobody Can Quarrel Forever",
        artists=(ArtistMetadata(name="Zed Marrowby"),),
        tracks=(TrackMetadata(title="One", position=1), TrackMetadata(title="Two", position=2)),
        formats=("Vinyl", "LP", "Compilation", "Remastered"),
        released_on=date(2012, 1, 1),
    )

    assert is_various(release) is False


def test_a_release_whose_tracks_spell_one_artist_two_ways_is_not_a_compilation() -> None:
    """One person is not several, however the catalogue spells them.

    Discogs publishes `credited_as` beside the canonical name, so a
    single-artist release whose tracks carry a credit at all must not become a
    compilation of one person on a spelling difference.
    """
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="s1",
        title="An Album",
        artists=(ArtistMetadata(name="Someone Else"),),
        tracks=(
            TrackMetadata(title="One", position=1, artists=(ArtistMetadata(name="The Band"),)),
            TrackMetadata(title="Two", position=2, artists=(ArtistMetadata(name="The Band"),)),
        ),
        released_on=date(1979, 1, 1),
    )

    assert is_various(release) is False


def test_the_style_in_a_folder_name_is_spelled_as_the_tag_is() -> None:
    """`%style%` and the genre tag read one list, so they cannot disagree."""
    release = ReleaseMetadata(
        source=MetadataSources.DISCOGS,
        source_release_id="1",
        title="Quarrelzon",
        artists=(ArtistMetadata(name="Mirén Zoélle"),),
        styles=("jazzfunk", "MPB"),
    )
    policy = NamingPolicy(folder_template="%albumartist% - %album%{ [%style%]}")

    assert policy.folder_name(release, "FLAC") == "Mirén Zoélle - Quarrelzon [Jazz-Funk]"
