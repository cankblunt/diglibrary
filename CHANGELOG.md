# Changelog

What changed, grouped by what it means for your files and your screen rather
than by where the code moved. It begins with the changes that make up 1.7.0;
earlier versions are not described here.

## 1.7.0

### Removed

- The player, its queue and the saved playlists. The playlists are removed from
  the database the first time 1.7.0 opens; the copy it takes before any schema
  change still holds them.

### Changed

- Names that come from a catalogue are written in one case.
- The genre tag carries the catalogue's styles before its genres, each in one spelling.
- Nothing is drawn across a spectrogram in Quality.
- Tooltips open sooner on macOS.
- `Plan this album again` opens the album as it was organized, and asks no catalogue.
- Candidates found by a Discogs search show their label and catalogue number.
- A second copy of an album is said the moment it arrives.
- An album you drop or open again from the folder it already lives in is only named.
- You can add folders while a scan is running.
- A cover is chosen from under the picture in the album's own dialog, on any album, and you see the picture before it is written.
- Closing a finished album you asked to plan again puts it back as it was.
- The album you have open in Quality is marked on its row.
- `All N tracks:` and its chips, which decide a whole album at once, are drawn as a control of their own.
- The album's own proof is named once instead of on every track.

### Fixed

- An album arranged from its own tags no longer has its files renamed to each other's names.
- A lossless file made from a low-bitrate MP3 is no longer cleared by one noisy reading.
- The Quality screen says how many readings agreed, and no longer "six of six".
- The benchmark writes its transcoded arms as ordinary 16-bit FLAC files again.
- A file name no longer repeats an aside this application wrote into it.
- Moving an album onto another disk keeps the Finder tags on its folder.
- Confirming a pairing no longer discards a folder name you typed.
- Pairings set aside for another release no longer change what is planned.
- A finished album stays organized when a catalogue has nothing to say.
- Typing a title over a track no longer takes its file away.
- An album you rejected and then approved no longer reads `Rejected`.
- A rejected album stays rejected after the shelf is planned again.
- `Relocate…` onto a folder that already belongs to another album says no.
- A copy of an album you deleted is no longer followed into the folder of the copy you kept.
- Pressing `Approve & apply` twice applies once.
- A title that every file in a folder repeats is no longer read as those files' titles.
- A release naming an edition your files do not name now loses the score the rule states.
- Putting a file at a track number the release already publishes pairs it with that track instead of inserting an extra one.
- A track you added against a release the album no longer carries is now held and reported, like a pairing always was.
