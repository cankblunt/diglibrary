# Changelog

What changed, grouped by what it means for your files and your screen rather
than by where the code moved. It begins with the changes that make up 1.7.0;
earlier versions are not described here.

## 1.8.0

### Added

- Collection, a screen for the records on your shelf. A record is added from a
  Discogs link, a barcode or a search, or imported from a list you already keep,
  column by column and rehearsed before anything is written; each one keeps its
  pressing, its condition, and the places and categories you define. Nothing in
  it is priced.
- A search for a record lists each album first with its pressings under it, and every line gives both the album's first year and the pressing's year.
- The collection's columns can be resized by dragging.
- Clicking the cover in an album's dialog opens it at full size, bounded by the window and never past the picture's own pixels.

### Changed

- A folder's bracket gives every lossy format the rate it declares, as it did for MP3: an AAC album reads `[M4A 256]`.
- An MP3 whose audio is coarser than its header says is named by the rate measured, and a folder holding two qualities names the better first: `[MP3 320, MP3 128]`.
- A lossless file that stops at one frequency in a large share of its moments is called a transcode.
- Arranging an album by its own tags also writes its tags and cover, takes a missing year from the folder's name, and credits a compilation as one: two artists as `A & B`, more as `Various Artists`.
- Names arranged from an album's own tags are written in one case too, and a capital slipped inside a word is lowered while a deliberate one is kept.
- An album dropped or opened again while already on the shelf moves to the top with the arrivals and is highlighted.
- A verdict that would rename an album already organized offers to plan it again.
- Reading a folder again says which files joined the album or left it.
- A card shows the album's front cover before any larger picture in its folder.
- The cover cache keeps only the pictures a plan can still write.

### Fixed

- Dropping folders onto a large shelf no longer leaves the window waiting with no answer.
- An album taken off the shelf and dropped again arrives, instead of being called already here.
- A card taken off with the ✕, or a star rating, is no longer undone by a slow read of the shelf.
- A scanned album dropped or opened again keeps its identification instead of reading `Not scanned`.
- An album whose tracks all sit inside a larger one is no longer announced as a second copy.
- A cover waiting to be written is no longer discarded by the cache.
- A track named up to the filesystem's length limit can have its tags and cover written.

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
