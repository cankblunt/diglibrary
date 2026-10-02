// Every user-facing string, in one place, so a translation is a file and not a
// rewrite.
export const STR = {
  tabLibrary: "Library",
  tabMixing: "Mixing",
  // The mixing screen, in a DJ's vocabulary: keys are Camelot codes, speeds are
  // tempos, and what the app is unsure of says so.
  mixSortKey: "Key, around the wheel",
  mixSortTempo: "Tempo, slowest first",
  mixSortArtist: "Artist A–Z",
  mixColKey: "Key",
  mixColTrack: "Track",
  mixColArtist: "Artist",
  mixColTempo: "Tempo",
  mixColSure: "Key confidence",
  tipMixColSure:
    "How far ahead of the runner-up the key beside it came in \u2014 nothing to do "
    + "with the tempo, which carries its own figure in its column. Where the "
    + "call was close, the key it beat is named here too.",
  tipMixColTempo:
    "Beats per minute, read from the audio. Where a second number follows, the "
    + "audio does not settle between the two \u2014 they are one octave apart.",
  // **What this number is, said where it is read.** The correlation between
  // the recording's twelve pitch classes and a key's profile
  // (`harmonic/keys.py`) stays well under 1 even on a track nobody would argue
  // about, because real music holds notes outside its own key — drawn as a
  // percentage it reads as the application being barely sure. It also says how
  // much the recording looks like music in that key, not whether the reading
  // beat its rivals. So the word comes from the distance to the runner-up, and
  // the sentence says in words how often a reading that decided lands on
  // rekordbox's own answer. In words, because the share depends on the music
  // measured and only the order of the cases holds everywhere.
  mixKeySure: (margin, howOften) =>
    `This key came in ${margin} ahead of the next of the twenty-four. `
    + `Readings this decided ${howOften} land `
    + "on rekordbox's own key, or on one that mixes with it.",
  // One per case, strongest first. A tie between two neighbours on the wheel
  // is worded apart from the middle band it shares a badge with: it is at
  // least as good as that band, and the row is told about its own case.
  mixKeyUsually: "usually",
  mixKeyOften: "often",
  mixKeyTieOften: "often, even this close to the runner-up,",
  mixKeyLessOften: "less often than not",
  mixKeyStaleMark: "older reading",
  mixKeyStale:
    "This key was measured before this application kept the distance to the "
    + "runner-up, and with an earlier key profile. Measure the "
    + "album again to see how decided the reading is.",
  mixKeyOr: (camelot) => `or ${camelot}`,
  mixKeyTwin: (camelot) =>
    `${camelot} came in almost level with it, and the two are neighbours on the `
    + "wheel. A reading this close usually still mixes with rekordbox's answer "
    + "when the pair are neighbours \u2014 so both are worth trying.",
  // The other tie, and it is the one that earns the doubt. Its own sentence,
  // because what holds for a runner-up that mixes with the winner does not
  // hold here, and saying it would describe a different situation.
  mixKeyTwinApart: (camelot) =>
    `${camelot} came in almost level with it, and the two do not mix. Readings `
    + "this close, between keys this far apart, land on rekordbox's answer less "
    + "often than not \u2014 this is a doubt, and the one to check by ear.",
  mixTempoSure: (percent) => `The tempo is this steady: ${percent}%`,
  mixTempoTwin: (bpm) => `The audio does not settle: it could be ${Math.round(bpm)} BPM`,
  // Said while the library is still coming back, because this wheel is built
  // from the albums that have arrived — and a part of the answer drawn as the
  // whole answer is the one thing this screen must never do.
  mixingStillArriving:
    "Your library is still coming back — keys measured in albums that have "
    + "not arrived yet will appear here in a moment.",
  ariaCamelot: "The Camelot wheel, showing the keys you have measured",
  emptyMixing:
    "Nothing has been measured for mixing yet. Open an album and choose "
    + "\u201cMeasure key and tempo\u201d \u2014 it reads the audio, writes nothing to your "
    + "files, and takes about a second for a whole album.",
  measureHarmonics: "Measure key and tempo",
  measureAlbums: "Measure albums…",
  measureMarked: (count) =>
    count === 1 ? "Measure 1 album for mixing" : `Measure ${count} albums for mixing`,
  measureHeading: "Measure key and tempo",
  measureNote:
    "Pick the albums to read. Nothing is written to your files, and each album "
    + "takes about a second.",
  measureStart: (count) =>
    count === 1 ? "Measure 1 album" : `Measure ${count} albums`,
  measureAlready: "already measured",
  measureNothingToPick: "There are no albums on the shelf to measure yet.",
  tipMeasureAlbums: "Choose albums from your shelf to read for key and tempo.",
  tipMeasureSearch: "Search your shelf by artist, album or folder.",
  tipMeasureStart: "Read the audio of the albums you ticked. Nothing is written.",
  measuringHarmonics: "Reading the audio\u2026",
  measuringAlbums: (done, total) => `Reading the audio — ${done} of ${total} albums`,
  busyMeasuringKeys: "Reading key and tempo",
  measuredHarmonics: (tracks, albums) => {
    const one = tracks === 1 ? "1 track" : `${tracks} tracks`;
    return albums === 1
      ? `${one} measured. They are on the Mixing screen.`
      : `${one} measured across ${albums} albums. They are on the Mixing screen.`;
  },
  measuredNothing: "Nothing could be measured — no audio here could be read.",
  tipMixSearch: "Search the measured tracks by name, artist or album.",
  // It offers one thing, so it says one thing: the menu holds this entry and
  // nothing else.
  tipMixMore: "Show every key again",
  tipMixMoreEmpty:
    "Nothing to offer yet \u2014 press a key on the wheel, and this can put the "
    + "whole list back.",
  mixClearKey: "Show every key again",
  mixChosen: (code, key, count) =>
    `${code} \u00b7 ${key} \u2014 ${count === 1 ? "1 track" : `${count} tracks`}`,
  mixNoKeyChosen: "Press a lit segment to see only that key.",
  mixPartners: (name) => `Mixes with \u201c${name}\u201d`,
  mixNoPartners: (name) =>
    `Nothing measured mixes with \u201c${name}\u201d \u2014 by key and by tempo together.`,
  // Said in words, because a suggestion the screen cannot explain cannot be
  // judged.
  mixWhy: (relation, tempo) => `${relation} \u00b7 ${tempo}`,
  mixTempoUnknown: "speed unknown",
  mixOr: (bpm) => `or ${Math.round(bpm)}`,
  mixNoKey: "\u2014",
  mixNoTempo: "\u2014",
  tabHistory: "History",
  scan: "Scan",
  // The button says what it is about to do to how many, because with marking
  // it is not obvious.
  scanMarked: (count) => `Scan ${count} marked`,
  opening: "Reading that folder…",
  // Nothing arrives marked, however it arrives, so the sentence only says what
  // to do about it.
  nothingMarked: "No album is marked. Tick the ones you want looked up.",
  // The other nothing: albums are ticked, and every one of them is already
  // organized, which this gesture has promised to leave exactly as it is.
  nothingToScan:
    "Every album you marked is already organized, so a scan would leave them "
    + "exactly as they are. Open one and choose “Plan this album again” to look "
    + "at it.",
  tipMark:
    "Include this album in the next Scan. Only marked albums are looked up, "
    + "identified and planned.",
  scanning: "Scanning…",
  measuring: (done, total) =>
    `Measuring the audio of ${done} of ${total} albums… (once per library)`,
  identifying: (done, total) => `Identifying ${done} of ${total}…`,
  // **What the run did, and never how many albums are on the shelf.** The
  // count is the run's own: a scan of two marked albums must not report the
  // whole shelf as identified. The flash and the line that stays both use it.
  scanFinished: (count) =>
    count === 0
      ? "Scan finished — nothing identified."
      : `Scan finished — ${count} album${count === 1 ? "" : "s"} identified.`,
  // Said here rather than as a notice of its own, because a run that identified
  // nothing and left two albums exactly as they are is **one** fact about what
  // happened, and the line that stays is where it is read. A toast that leaves
  // takes half the sentence with it.
  scanFinishedLeaving: (count, untouched) =>
    `${STR.scanFinished(count).slice(0, -1)}; `
    + (untouched === 1
      ? "1 album was already organized and left exactly as it is."
      : `${untouched} albums were already organized and left exactly as they are.`),
  // What a run was decided *without*. Three states and no more, the same three
  // the acquisition provider reports — the key is missing, the key was refused,
  // the service did not answer — because a window that describes one kind of
  // failure in two vocabularies has to be learned twice.
  // **A clause, not a sentence**, so it is given the sentence it continues:
  // concatenated after one that has already ended, the line would carry a full
  // stop in the middle. The stop is taken off by pattern rather than by
  // position, so a sentence that ends some other way loses no letter.
  scanWithout: (sentence, names) => `${sentence.replace(/\.$/, "")} \u2014 without ${names}.`,
  sourceDownReasons: {
    no_key: "no key is set for it",
    refused: "its key was refused",
    unreachable: "it did not answer",
  },
  // The dialog's quiet clause for an album identified while a source was out.
  // `STR.` and never `this.` — see `sourceName` below, which is why.
  headMissed(name, reason) {
    const why = STR.sourceDownReasons[reason] || "it could not be searched";
    return `without ${name} \u2014 ${why}`;
  },
  scanCompleted: (count) =>
    count === 0
      ? "Scan completed — nothing identified."
      : `Scan completed — ${count} album${count === 1 ? "" : "s"} identified.`,
  // What the top bar shows while something is running on the other side of the
  // bridge. Most of this app's work happens on a worker thread, and a screen
  // without a progress bar has no other way to say so.
  busy: "Working…",
  busyMeasuring: "Measuring audio…",
  busySearching: "Searching…",
  tipBusy: "Something is still running. It is safe to wait — this goes when it finishes.",
  // A card in a run that has not reported yet. The album is being worked on;
  // the bar above says how far the run has got.
  cardIdentifying: "identifying…",
  reach: (folders, files, extensions) =>
    `This scan may touch ${folders} album folder${folders === 1 ? "" : "s"} and ` +
    `${files} audio file${files === 1 ? "" : "s"} (${extensions}). ` +
    "It writes tags, file and folder names, and cover images — nothing else.",
  reachExcluded: (names) => ` Left alone: ${names}.`,
  // The first screen of an empty installation. It names the gesture, says what
  // the gesture *does* — a scan identifies, which nobody can guess — and ends on
  // the promise that nothing is written without approval and everything can be
  // undone.
  emptyLibrary:
    "Drag your album folders here, or add one with \u{1F4C1}+ — then press Scan. "
    + "DigLibrary reads them where they sit and looks each album up on Discogs and "
    + "MusicBrainz. Nothing is renamed or retagged until you read a plan and approve "
    + "it — and everything it writes can be undone from History.",
  restoringLibrary: "Reading your library…",
  emptyHistory: "Nothing has been applied yet. Every change will appear here, reversible.",

  // --- the one bar every screen wears ----------------------------------------
  // Each screen's name, and the sentence that explains it. The sentence is the
  // title's tooltip rather than a paragraph under it: it teaches on the first
  // visit and would otherwise be read on every visit after that.
  acquireNote:
    "Searches run on your own slskd, and what you ask for lands in its downloads "
    + "folder. Nothing is renamed or moved until you organize it.",
  mixingHeading: "What mixes with what",
  mixingNote:
    "Tracks you have measured for key and tempo, laid around the Camelot wheel. "
    + "Press a key to see what mixes with it.",
  historyHeading: "What this app has done",
  historyNote:
    "Every run this application has applied, and every download it recorded. A "
    + "run can be reverted from here for as long as its replaced images are kept.",

  // The History's own filter, in the same shape as the Library's above: the
  // first entry is the one that asks nothing.
  historyGroupAll: "everything",
  historyGroupApplied: "applied",
  historyGroupPartial: "stopped partway",
  historyGroupReverted: "reverted",
  historyGroupDownload: "downloads",
  tipSearchHistory:
    "Find a run by the album it was about or by the folder it wrote to. Every "
    + "word has to appear somewhere, in any order, and accents do not matter.",
  tipHistoryMore: "What can be done with this run",
  historyNone: "No run here answers to that. Clear the search, or ask for everything.",

  groupAll: "all",
  // An album whose folder was merely opened or dropped. It is not "to review":
  // nothing has been compared, so there is no answer to review.
  groupUnlooked: "not scanned yet",
  groupAutomatic: "automatic",
  groupReview: "to review",
  groupNoMatch: "no match",
  // One word for one fact: this app organized this album, whichever session
  // did it. "Applied" is the same thing said about a run.
  groupOrganized: "organized",
  applyAutomatic: (count) => `Apply ${count} automatic`,
  groupRejected: "rejected",
  statusLabel: (name, count) => `Status: ${name} (${count}) ▾`,
  // There is no `all`, because these two are exhaustive.
  find: "Find",
  lookingUpLink: "Looking up that release…",
  searchFinished: (count) =>
    count === 0
      ? "Search finished — nothing found. Try other words, or paste a link."
      : `Search finished — ${count} candidate${count === 1 ? "" : "s"}.`,
  applyingCandidate: "Adopting…",
  loading: "Loading…",
  masterYear: (year) => `album ${year}`,
  editionYear: (year) => `edition ${year}`,
  titleEvidence: (agreeing, compared) => `${agreeing}/${compared} titles match`,
  durationEvidence: (agreeing, compared) => `${agreeing}/${compared} lengths match`,
  trackCounts: (release, local) => `${release} tracks vs ${local} files`,
  planSummary: (count) =>
    count === 0 ? "Nothing would change" : `What will change (${count})`,
  applyCovers: (count) => `Write ${count} embedded cover${count === 1 ? "" : "s"}`,
  coversFound: (count) =>
    `${count} coverless album${count === 1 ? " carries" : "s carry"} a picture in the files.`,
  // Named, not counted. "1 cover written" does not say which album was
  // touched, and beside "Applied to 0 albums" it reads as a contradiction —
  // the two sentences count different things.
  coversWritten: (count, albums) => {
    const written = `${count} cover${count === 1 ? "" : "s"} written from the files`;
    const named = (albums || []).slice(0, 3).join(", ");
    const rest = (albums || []).length - 3;
    if (!named) return `${written}.`;
    return `${written}: ${named}${rest > 0 ? `, and ${rest} more` : ""}.`;
  },
  applying: "Applying…",
  appliedTitle: (count) => `Applied to ${count} album${count === 1 ? "" : "s"}.`,
  openFolder: "Open folder",
  exportReport: "Export from/to report",
  revertRun: "Revert this run",
  runReverted: (count) => `Run reverted — ${count} album${count === 1 ? "" : "s"} back exactly as they were.`,
  // The undo of the undo. Worded as repeating what happened rather
  // than as "redo", which in an application that renames files could be read as
  // "work it out again" — and that is the other gesture, `Plan this album again`.
  redoRun: "Do this run again",
  tipRedoRun:
    "Apply this run again exactly as it was — the same names, the same tags, the "
    + "same pictures. No catalogue is asked and nothing is worked out again.",
  runRedone: (count) =>
    `Done again — ${count} album${count === 1 ? "" : "s"} organized exactly as before.`,
  menuRedo: "Do this again",
  certificateLine: (c) =>
    `Certificate: audio intact ${c.audio_intact}/${c.audio_checked} · ` +
    `writes verified ${c.writes_verified}/${c.writes_checked} · everything reversible.`,
  certificateTrouble: "⚠ A proof failed — open the album for details. Nothing was rolled back on its own.",
  evidenceHeading: "Evidence: your files against this release",
  // What actually proved this identification, said plainly. "Proven by audio"
  // reads as a fingerprint and is not one: it is every track's *length* compared
  // with what the source published.
  // The header's one line: which catalogue this match uses, and which ones only
  // watched. Both are links, so the name is the way to the page.
  usingSource: "using",
  // The header of an arrangement: the claim's honest size in one breath.
  // `using your tags` reads like a source had answered, and none had.
  arrangedHeader: "arranged from your tags — no source was asked",
  // The other way to be arranged from your tags: a catalogue did answer, and its
  // answer was set aside. The first sentence would contradict what happened.
  arrangedInsteadHeader: "arranged from your tags, instead of the answer a catalogue gave",
  witnessesLabel: "witnesses:",
  // How many of the album's file names a source publishes, beside that source's
  // own name. In parentheses because it qualifies the name it follows rather
  // than standing beside it as a second fact.
  sourceScore: (agreeing, compared) => ` (${agreeing}/${compared})`,
  viewDetails: "View details",
  // What the same control says once it has been used. A fold whose label never
  // changes asks the reader to work out which way it goes from the marker
  // alone — and `display: inline-block` on a `<summary>` takes the disclosure
  // triangle away.
  hideDetails: "Hide details",
  tipOpenSource: (name) => `Open this release on ${name}`,
  proofAudio: "lengths match the release",
  proofTitles: "titles match the release",
  proofText: "matched on text alone",
  tipProofAudio:
    "Each of your files was measured and its length compared with the length "
    + "this release publishes for that track. It is a comparison of durations, "
    + "not a fingerprint of the audio — a fingerprint says so on its own line.",
  tipProofTitles:
    "Your files' titles were compared with the track names this release "
    + "publishes. No length could be compared, because the source published none.",
  tipProofText:
    "Only the album and artist text could be compared — no track length and no "
    + "track title agreed. This is the weakest evidence there is, and it is why "
    + "an album like this waits for you.",
  evidencePaired: (delta) => (delta === null ? "✓ title" : `✓ ${(delta / 1000).toFixed(1)}s`),
  evidenceFileOnly: "no track on this release",
  evidenceTrackOnly: "no file here",
  reportWritten: (rows) => `Report written — ${rows} change${rows === 1 ? "" : "s"}.`,
  planHeading: "What will change",
  emptyPlan: "Nothing to change — this album is already exactly as it should be.",
  candidatesHeading: "Candidates",
  searchAgainArtist: "Artist",
  searchAgainAlbum: "Album",
  searching: "Searching…",
  noCandidates: "No candidates yet. Correct the artist or album, or paste a link, and find again.",
  useCandidate: "Use this",
  // The identifier a source declares is lowercase and internal — `discogs`,
  // `musicbrainz`. What is shown is the name the site goes by. Not a closed
  // list: any registered provider declares its own identifier, so an unknown
  // one is title-cased rather than dropped or shown raw.
  sourceNames: {
    discogs: "Discogs.com",
    musicbrainz: "MusicBrainz.org",
    itunes: "iTunes",
    // Not a catalogue and not a site: the album's own files, read as they
    // are. The header reads `using your tags`, in the same place and the same
    // words it names Discogs, so the two proposals can be told apart at a
    // glance.
    tags: "your tags",
  },
  // **`STR.`, never `this.`** — a method here is read as a value as often as it
  // is called: `missing.map(STR.sourceName)` passes the function without its
  // receiver, `this` is undefined inside it, and the throw stops the whole
  // window drawing. Naming the object cannot come unbound.
  sourceName(source) {
    const key = String(source || "").toLowerCase();
    return STR.sourceNames[key] || (key ? key[0].toUpperCase() + key.slice(1) : "");
  },
  viewSource(source) {
    return `View on ${STR.sourceName(source)} ↗`;
  },
  // The name composer: the pieces of file and folder names are chosen and
  // ordered by the user, instead of depending on fixed styles.
  composerTitle: "Or compose your own",
  composerReset: "Use the style above",
  composerFolder: "Folder",
  composerTrack: "Track",
  composerAdd: "Add a piece…",
  // The three that come from a measurement rather than a catalogue. Named
  // apart because they are the only pieces here that depend on work having been
  // done, and nothing measures anything on the way to a rename.
  composerMeasured: "only where the track has been analysed",
  composerRemove: "Take this piece out.",
  composerEarlier: "Move it earlier.",
  composerLater: "Move it later.",
  composerNoAlbum: "Identify an album and this shows the name it would get.",
  // Two lines and not one. The top is the arrangement itself, every piece
  // standing for its value; the bottom is what this album really produces. A
  // piece the album cannot fill vanishes from the second line only, and the
  // difference between the two is the answer to "why is my year not there".
  composerPreview: (shape, real) => `${shape}\n${real}`,
  composerPair: (folder, track) => `${folder}\n   └ ${track}`,
  // Said in words as well, because a reader should not have to diff two lines
  // to find the piece that went missing.
  composerMissing: (names) =>
    `${names} ${names.includes(",") ? "are" : "is"} empty for this album, so ` +
    `${names.includes(",") ? "they do" : "it does"} not appear in the name.`,
  approve: "Approve & apply",
  // Said as a count when only part of the album paired up, so the button never
  // promises more than it will do. It counts *files* and says so: a count with
  // no noun reads as the release's tracks, and twelve files against a release
  // of thirteen would read `12 of 12` beside a line saying one track has no
  // file. When it covers every file it stops counting at all: the partial is
  // about the release, and nothing is partial about what is written.
  approvePart: (matched, total) =>
    matched >= total ? "Approve & apply" : `Approve & apply to ${matched} of ${total} files`,
  approvedPartToast:
    "Applied what paired up. The rest of the files were left exactly as they were.",
  // What it takes back is *the last gesture*, never the album: the word has to
  // say that without a sentence, and a label reading "undo" would claim the
  // files as well, which is what `Revert` promises and this does not.
  cancelLast: "Cancel",
  reject: "Reject",
  // "Send to review", not "back": an automatic album was never there, and
  // "back" would claim a history the album does not have. A ⋯ entry.
  hold: "Send to review",
  sendToQuality: "Send to Quality",
  planAnyway: "Plan this album again",
  // What only this does. An organized album draws its names and does not open
  // them, and this is the gesture that opens them: the release it was organized
  // as, with every typed name and every pairing put back on it, planned against
  // the files as they stand now. It asks no catalogue, so the tooltip says where
  // another release comes from.
  tipPlanAnyway:
    "Open this album for editing as it was organized: the same release, with the "
    + "names you typed and the files you paired. No catalogue is asked. Once it "
    + "is open, another release is a search or a pasted link away.",
  held: "Held — it will wait for your decision.",
  cancelled: "Back to how it was before the last thing you asked for.",
  // `cancelledKeepingWords` is said only when something was typed **after** the
  // gesture, the one case where typed words leave the screen. They are kept and
  // applied again the next time the album is planned; without the sentence the
  // screen looks exactly like one that threw them away.
  // Leaving an album with a proposal on it. The heading states the fact and the
  // two buttons are the two answers, named by what they do rather than by yes
  // and no — *Leave it waiting* keeps the plan for the next time the album is
  // opened, *Take it back* is the footer's `Cancel` reached from here.
  // The album whose folder is gone. The sentence is the API's, which is the
  // side that knows what it could not read and whether the same audio is on
  // record somewhere that still exists — the window states nothing of its own
  // here. The two buttons are the two repairs, and the primary one is the one
  // that keeps the album.
  albumGoneRelocate: "Point it at its folder…",
  albumGoneForget: "Let this card go",
  leaveAlbumHeading: "This album has a plan waiting",
  leaveAlbum:
    "You asked this app to look at it again, and nothing has been written. "
    + "Leaving it waiting keeps the plan for next time; taking it back returns "
    + "the album to what it was. Either way, anything you typed is kept.",
  leaveAlbumKeep: "Leave it waiting",
  leaveAlbumUndo: "Take it back",
  // Asking again about an album every catalogue already answered for.
  // The sentence claims only what is on record — no source was missing — and
  // never that nothing has changed, which this app cannot know.
  scanAgainHeading: "This album was already read",
  scanAgain:
    "Every catalogue answered when it was read, so nothing on record says it "
    + "needs asking again. A re-scan asks them about each track once more, one "
    + "call at a time, and that takes minutes.",
  scanAgainYes: "Scan it again anyway",
  scanAgainBecause: (source) =>
    `${source} did not answer when this album was read. Asking again.`,
  cancelledKeepingWords:
    "Back to how it was before the last thing you asked for. "
    + "What you typed is kept, and comes back when you plan this album again.",
  rejected: "Rejected — the files were left untouched.",
  approvedToast: "Applied. It can be reverted from History.",
  revert: "Revert",
  reverted: "Reverted — the album is back exactly as it was.",
  // Albums that hold some of the same audio. The note is not decoration: it
  // has to be plain that the application will not touch the files, because
  // every other thing this screen offers does.
  // The witness is looking at something else and fits it better. Three words,
  // on the witness already named: the two scores beside it say how much
  // better.
  witnessAnother: "— another record",
  sharedHeading: "Also in another album",
  // The same fold when the album is its own pair: a fixed heading would
  // announce another album above a body that says "in this same folder", and
  // the two cannot both be true.
  sharedHeadingWithin: "The same audio, twice in this album",
  sharedNote:
    "DigLibrary never moves, renames or deletes any of these files. Your answer "
    + "here only records what you decided, so this stops asking.",
  sharedTracks: (count) =>
    count === 1 ? "1 track also in another album" : `${count} tracks also in another album`,
  sharedWithin: (count) =>
    count === 1 ? "1 track twice in this album" : `${count} tracks twice in this album`,
  sharedSame: "the same audio, in this same folder",
  sharedKeepBoth: "They belong in both",
  sharedHandled: "I have dealt with it",
  sharedUndo: "Ask me again",
  sharedShowFolders: "Show both folders",
  sharedShowFolder: "Show this folder",
  // This *is* the "listen to them" gesture: both folders open in the Finder
  // rather than in a player. The tooltip says so, because the button's name
  // says what it does and not what it is for.
  tipSharedShowFolders:
    "Opens both folders in Finder, so you can play the two files and compare them yourself.",
  sharedAnsweredBoth: "You said these belong in both places.",
  sharedAnsweredHandled: "You said you have dealt with this.",
  tipSharedTracks:
    "These files hold the same audio, byte for byte. Nothing is moved or deleted — "
    + "open the album to see which tracks, and to answer.",
  // The two words the left column's marks carry. They are read one at a time,
  // on hover, which is why the column itself has to be legible without them.
  // Said of a row whose pairing rests on nothing but a length within six
  // seconds, and of the album whose names barely met the release's.
  planMarkByLength:
    "Paired by length alone \u2014 this file's own name does not agree with this "
    + "track's.",
  // **The count is in the head**, beside the source's name under the title, so
  // this line keeps only the half that is advice — the one thing on this screen
  // that asks for something to be done before applying.
  namesDisagree: () => "Check that this is the right record before applying anything.",
  // A rating. The only number in this application that no source can supply,
  // so nothing here explains or qualifies it.
  ratingSet: (value) => `Rate this ${value} of 5 — press again to clear`,
  ratingAndUp: (value) => `${"★".repeat(value)}${"☆".repeat(5 - value)}${value < 5 ? " and up" : ""}`,
  ratingNone: "Not rated yet",
  // The rating list's way back, and it says *any* rather than *all*: `All` on a
  // list of stars would read as *every star*, which is a filter, and this is
  // the absence of one. The status list can say `all` because what it counts
  // are albums.
  ratingAny: "Any rating",
  // The two questions this menu answers, each over its own list. Pressing an
  // answer again withdraws it, so neither list needs an entry meaning "all".
  filterStatus: "Status",
  filterRating: "Rating",
  planMarkConfirmed: "This file and this track are settled — only the name changes.",
  planMarkOffered: "This pairing is offered, not settled. Answer it beside the name.",
  // One word apart, and no way to tell which side typed it wrong: a catalogue
  // can misspell a title, and so can a folder. Both spellings, no verdict.
  planYours: (spelling) => `Your file says “${spelling}” — one word apart.`,
  // The same offer on stronger evidence, and the sentence says the evidence
  // rather than the conclusion: what was measured is that the witness agreed
  // with every title in the files, and *the catalogue is wrong* is a
  // conclusion nobody measured. It never quotes the witness — a verification
  // source contributes no written value, and what is shown here is the file's
  // own word.
  planYoursBacked: (spelling, witness) =>
    `Your file says “${spelling}”, and ${witness} agrees with every title here.`,
  planYoursKeep: "Keep yours",
  planYoursKeepTip:
    "Write the spelling your file already carries instead of the catalogue's. "
    + "You can withdraw it later like any other correction.",
  planYoursKept: "Your spelling is what will be written.",
  // The same aside, after it has been answered. It states what stands
  // rather than what was offered, because *kept* is the only word on this
  // screen that says a decision was made here at all — the field beside it
  // looks exactly like a field the catalogue filled.
  planYoursStanding: (spelling) => `Kept: “${spelling}”.`,
  planYoursDrop: "Use the catalogue's",
  planYoursDropTip:
    "Withdraw your spelling and let the catalogue name this track again. "
    + "The offer to keep yours comes back, so nothing is lost either way.",
  planYoursDropped: "The catalogue names that track again.",
  // The album's own gesture, which reaches the typed titles as well — so it
  // says the number first, and the number is the whole question.
  planYoursDropAll: "Give them all back to the catalogue",
  planYoursDropAllHeading: "Give these titles back to the catalogue",
  planYoursDropAllAsk: (count) =>
    count === 1
      ? "One title of this album carries your word. Withdraw it and let the "
        + "catalogue name it again?"
      : `${count} titles of this album carry your word. Withdraw all of them and `
        + "let the catalogue name them again?",
  planYoursDropAllYes: "Withdraw them",
  planYoursDropAllWords: (count) =>
    count === 1
      ? "One title here carries your word."
      : `${count} titles here carry your word.`,
  planYoursDroppedAll: (count) =>
    count === 1 ? "One correction withdrawn." : `${count} corrections withdrawn.`,
  planNote:
    "Every name below is editable — your word outranks the catalogue, and what "
    + "you see here is what gets written.",
  // The same fold with no name in it: an album whose names are already written
  // and whose plan is now artwork, or tags, alone. The promise above would be
  // false here.
  planNoteWithoutNames: "Nothing below changes a name. What is listed is what gets written.",
  // The same table, said for the album that needs nothing. "Nothing would
  // change" is a claim; these rows are what makes it checkable, and they are
  // the reason to open the fold rather than fold the evidence away.
  planNoteUnchanged: "Nothing here would change — every name already reads as it should.",
  // What the sentence above carries on hover. The rows being editable is the
  // part worth keeping somewhere: they are shown so the claim can be checked,
  // and a claim that cannot be acted on can only be believed.
  tipPlanNoteUnchanged:
    "The names below are shown the same way a change would be, so the claim can "
    + "be checked. Each one is still editable: your word outranks the catalogue.",
  // The third reading of an empty plan: not "no change is needed" but "no
  // change could be worked out". A blocked album must not print that every name
  // already reads as it should below the line saying why nothing could be
  // planned. The rows are still worth reading: they are the album as it
  // stands, which is what the reason above is about.
  // The Library's own search. `album or artist` rather than a magnifying glass
  // alone, because the box has to say what it will look through — the folder
  // name is searched too, and that is the only name an album nobody has
  // identified yet has.
  searchAlbums: "Search album or artist…",
  tipSearchAlbums:
    "Find an album by its title, its artist, its year or the folder it lives in. "
    + "Every word has to appear somewhere, in any order, and accents do not matter.",
  tipSearchClear: "Clear the search",
  searchFoundNothing: (typed) =>
    `No album here answers to “${typed}”. The search reads the album, the artist, `
    + `the year and the folder name.`,
  // What `Plan this album again` answered. Said out loud because the dialog it
  // redraws is identical when the answer is "nothing", and a button that
  // answers invisibly is a button that gets pressed again.
  plannedNothing: "Planned again — every name and picture here already reads as it should.",
  plannedChanges: (count) =>
    count === 1
      ? "Planned again — 1 change to look at."
      : `Planned again — ${count} changes to look at.`,
  plannedBlocked: "Nothing could be planned. The reason is in the dialog.",
  planSummaryBlocked: "Nothing was planned",
  planNoteBlocked:
    "Nothing below would be written: the reason above stopped this album from "
    + "being planned at all. The rows show your folder as it stands today.",
  qMeasuring: "Measuring…",
  qNotMeasured: "not measured",
  qColFile: "Track",
  // The track table's own heading, named apart from the album table's
  // `qColVerdict`. Two keys with one name in one object literal is a silent
  // overwrite — the later wins — and this column would read the album table's
  // word.
  qColTrackVerdict: "Measured",
  qColWall: "Wall",
  // Two rungs, because that is what was measured: the audio was still there at
  // the first and gone at the second, and it really stops somewhere between.
  qWallBetween: (low, high) => `${low.toFixed(1)}–${high.toFixed(1)} kHz`,
  qWallAt: (kilohertz) => `${kilohertz.toFixed(1)} kHz`,
  tipWallOldRuler:
    "Measured before walls were found by their step, when a wall was the first "
    + "band under a fixed level. That reading stands as it was taken — nothing "
    + "here re-judges a number against a rule it was not measured for. Analyze "
    + "this album again to have it read the new way.",
  qColFall: "Fall",
  qColCeiling: "Ceiling",
  qColYours: "Your word",
  // The picture. The button only appears where this app has accused the audio
  // of having been lossy.
  qAnalyzeQuality: "Analyze quality",
  qHideSpectrogram: "Hide spectrogram",
  tipAnalyzeQuality:
    "Draw this track's spectrogram and show it beside what was measured. "
    + "Nothing is drawn until you ask: it costs about a second and a megabyte, "
    + "kept in a cache you can clear in Settings.",
  specAlt: (file) => `Spectrogram of ${file}`,
  // **The unit is written out, on both rulers of this picture.** One is a
  // frequency and the other a bitrate; abbreviated to `k` on both, they read
  // as the same measure.
  specKilohertz: (kilohertz) => `${kilohertz} kHz`,
  specWallBetween: (low, high) => `stops ${low.toFixed(1)}–${high.toFixed(1)} kHz`,
  specWallBelow: (high) => `stops at or below ${high.toFixed(1)} kHz`,
  // Two readings, and each gets its own words: between two frequencies, or at
  // the lowest one asked about, where there is no lower frequency to name. One
  // sentence for both called the second an interval it has not got.
  tipSpecWall: (open) => {
    const found = open
      ? "This audio stops at this frequency or lower. It is the lowest one the "
        + "measurement asks about, and no audio was found there, so nothing says "
        + "how far below it the audio really ends. "
      : "This audio stops somewhere between these two frequencies. The "
        + "measurement asks at a few fixed frequencies only: it found audio at "
        + "the lower one and none at the upper one, and cannot say where in "
        + "between. The label sits at the upper one. ";
    return (
      found
      + "This comes from the numbers, never from the picture — if it and what "
      + "you see disagree, trust your eyes and say so with “It is lossy” or "
      + "“Not lossy”."
    );
  },
  // The reference rungs, always shown and always quiet. Short on the picture,
  // because three of these share it with the measured wall.
  specEncoderWall: (bitrate) => `${bitrate} kbps`,
  // And the caveats said out loud. A ruler whose limits are not written down is
  // a ruler that will be read as a verdict.
  tipSpecEncoderWall: (bitrate, kilohertz) =>
    `Where an MP3 encoder at ${bitrate} kbps usually stops writing, about `
    + `${kilohertz.toFixed(1)} kHz. A habit, not a law, and it is not a finding `
    + "about this file:\n\n"
    + "• Encoders disagree. LAME, Fraunhofer and any VBR setting each cut in a "
    + "different place, and LAME's own V0 reaches past 20 kHz.\n"
    + "• AAC and Opus do not use these numbers at all. Opus sits near 20 kHz at "
    + "almost any bitrate.\n"
    + "• The mark moves with the sample rate. This picture is drawn against this "
    + "stream's own Nyquist limit, so the same wall sits at a different height "
    + "in a 48 kHz file than in a 44.1 kHz one.\n"
    + "• A wall at 16 kHz is also what an old analogue master or a tape source "
    + "looks like. It is not proof of an MP3.\n"
    + "• This application probes at eight frequencies, so a wall between two of "
    + "them is reported as the interval it fell in and never as a single number.",
  specReading: (shown) => {
    const parts = [];
    if (shown.cutoff) parts.push(`wall ${(shown.cutoff / 1000).toFixed(1)} kHz`);
    if (shown.decay_db !== null && shown.decay_db !== undefined) {
      parts.push(`fall ${shown.decay_db.toFixed(1)} dB`);
    }
    if (shown.ceiling_db !== null && shown.ceiling_db !== undefined) {
      parts.push(`ceiling ${shown.ceiling_db.toFixed(1)} dB`);
    }
    const measured = parts.length ? parts.join(" · ") : "not measured";
    const held = `${(shown.cache_bytes / (1024 * 1024)).toFixed(0)} MB cached`;
    return `${measured} — ${shown.from_cache ? "already drawn" : "drawn now"} · ${held}`;
  },
  qColHeard: "The audio",
  qHeardNamed: "named",
  qHeardUnknown: "unknown",
  qHeardNotAsked: "—",
  // Said as a count, because the interesting number is often zero and a blank
  // column cannot say whether the engine ran. A catalogue may be largely absent
  // from this service, and that is a fact worth reading rather than a silence
  // to leave on screen.
  qAcousticCoverage: (named, unknown, total) =>
    named + unknown === 0
      ? `The audio has not been asked about this album (${total} tracks).`
      : `The audio named ${named} of ${named + unknown} tracks it was asked about`
        + `${unknown ? `; ${unknown} it has never heard of` : ""}.`,
  tipHeardNamed: (score) =>
    `AcoustID recognized this recording${
      score === null ? "" : `, and is ${Math.round(score * 100)}% sure`
    }. Click to open it on MusicBrainz.`,
  tipHeardUnknown:
    "The fingerprint was sent and AcoustID has never heard of this recording. "
    + "That is the ordinary answer for a catalogue the service does not hold — "
    + "it is not a failure to measure.",
  tipHeardNotAsked:
    "This file has not been put to AcoustID. It is asked about an album whose "
    + "names did not settle it, and about the album you open.",
  qSayHonest: "Not lossy",
  qSayTranscoded: "It is lossy",
  qSayClear: "Undo — use the measurement",
  qVerdictSaved: "Recorded. The name follows it the next time this album is planned.",
  // Says how many it will reach, because that is the whole point of it: the
  // gesture it replaces is pressing the same word once per track.
  // Named for the tracks and never for the album — no album word is written,
  // and a label saying `this album` would promise one.
  qEveryTrack: (count) => `All ${count} tracks:`,
  qEverySaved: (count) =>
    count === 1
      ? "Recorded on 1 track."
      : `Recorded on all ${count} tracks. The names follow the next time this album is planned.`,
  tipSayEveryHonest:
    "Say “not lossy” about every track of this album at once. It writes the same "
    + "word the buttons in the rows below write, on all of them — press it again to withdraw it.",
  tipSayEveryTranscoded:
    "Say “it is lossy” about every track of this album at once. It writes the same "
    + "word the buttons in the rows below write, on all of them — press it again to withdraw it.",
  tipSayEveryClear:
    "Withdraw what you said about every track here and let the measurements decide again.",
  // The flag says what one click does, first, and why afterwards: a tooltip
  // that explains a state without naming the action leaves the action unknown.
  tipSayHonest:
    "Unmark as lossy — say this audio is what its container claims. The "
    + "measurement is kept and shown, but the folder is no longer named [Lossy] for it.",
  tipSayTranscoded:
    "Mark as lossy — say this audio was lossy before it arrived, whatever the "
    + "measurement found. The folder is named [Lossy] for it from the next plan on.",
  tipSayClear:
    "Withdraw what you said here and let the measurement decide again.",
  planHeadNow: "Now on disk",
  planHeadAfter: "After applying",
  planFolder: "Folder",
  planDerived: (name) => `written as ${name}`,
  editFolder:
    "Edit the folder name — what you type here is the name written to disk, "
    + "and it outranks the catalogue.",
  editTitle:
    "Edit the track title — what you type here is written into the file's tag "
    + "and into its file name, and it outranks the catalogue.",
  // Only on a compilation, which is the only kind of album that writes a track's
  // artist into its file name. The sentence says the one thing the
  // field does not look like it does: the name keeps the credit whole, and the
  // tag gets it as separate artists, because that is what a player groups by.
  editArtist:
    "Edit who this track is credited to — what you type here is written into its "
    + "file name exactly as you write it, and it outranks the catalogue. In the "
    + "tag it is stored as separate artists, split at every “,” and “&”, so a "
    + "player can group by each of them.",
  planNoArtist: "no artist",
  historyAlbum: "Artist - Album",
  historyWhen: "Applied",
  historyOperations: "Changes",
  historyState: "State",
  stateApplied: "applied",
  stateReverted: "reverted",
  statePartial: "partial",
  dismiss: "Dismiss",
  partialCount: (written, planned) => `${written} of ${planned}`,
  listArtist: "Artist",
  listAlbum: "Album",
  listYear: "Year",
  listFolder: "Folder",
  listTracks: "Tracks",
  listMatched: "Matched",
  listConfidence: "Confidence",
  listState: "State",
  settingsHeading: "Settings",
  settingNamingStyle: "Folder naming style",
  settingEdition: "Name the pressing (Ed. 2006) when it differs from the album's year",
  settingArtwork: "Download cover art from the Cover Art Archive",
  settingThreshold: "Automatic threshold",
  settingSpectrogramCache: "Spectrogram cache limit (MB)",
  settingClearSpectrograms: "Clear the spectrogram cache",
  spectrogramsCleared: (megabytes) =>
    `The drawn spectrograms are gone — ${megabytes.toFixed(0)} MB back. `
    + "No audio file was touched; they are drawn again when you ask.",
  settingMetadataCache: "Catalogue answers cache limit (MB)",
  settingClearMetadata: "Clear the catalogue cache",
  // Said plainly, because the word "cache" invites the fear that something
  // identified is being thrown away. What a release IS lives in the database;
  // this folder holds the answers that were read to find out.
  metadataCleared: (megabytes) =>
    `The saved catalogue answers are gone — ${megabytes.toFixed(0)} MB back. `
    + "Nothing identified was lost; the catalogues are simply asked again.",
  settingArtworkCache: "Cover picture cache limit (MB)",
  settingClearArtwork: "Clear the cover cache",
  artworkCleared: (megabytes) =>
    `The saved cover pictures are gone — ${megabytes.toFixed(0)} MB back. `
    + "A cover already written into an album is a file in that album and stayed there.",
  settingBackupLimit: "Replaced-image backup limit (MB)",
  // Said where the button would be, because its absence is the design.
  settingBackupNote:
    "These are the pictures a write replaced. They are what a revert puts back, "
    + "so there is no button to delete them — only a limit, and the oldest go first.",
  // What the folders hold right now, so the limits above are not abstract.
  cacheHeld: (spectrograms, metadata, artwork, backups, databaseCopies) =>
    `Held right now: ${spectrograms.toFixed(0)} MB of spectrograms, `
    + `${metadata.toFixed(0)} MB of catalogue answers, `
    + `${artwork.toFixed(0)} MB of cover pictures, `
    + `${backups.toFixed(0)} MB of replaced images, `
    + `${databaseCopies.toFixed(0)} MB of database copies.`,
  settingDatabaseCopies: "Database copy limit (MB)",
  settingDatabaseCopiesNote:
    "A compressed copy of your database is kept whenever the window opens on "
    + "something that changed.",
  // What decides the number, for whoever is setting it: on hover, because the
  // line above it is what has to be read to know what the setting is at all.
  tipSettingDatabaseCopiesNote:
    "These copies are the only record of your library outside the live file, so "
    + "the oldest go first as the limit is reached and the newest is never "
    + "discarded, whatever this number says.",
  settingConnections: "Connected services…",
  welcomeHeading: "DigLibrary is ready",
  // Said first, and true: the application identifies albums, fetches covers and
  // measures audio with nothing configured at all, so no key stands between an
  // installation and its first use.
  welcomeReady:
    "It already works — albums are identified against MusicBrainz, covers come "
    + "from the Cover Art Archive, and everything about audio quality is "
    + "measured on your own machine. Nothing below is required.",
  welcomeDiscogs: "Discogs",
  // Measured on a real library: most identified albums were settled by a
  // Discogs release.
  welcomeDiscogsWhy:
    "Music matches much better with Discogs connected — especially for "
    + "catalogues outside the US and UK, where it is often the only source that "
    + "lists the pressing you actually have.",
  welcomeDiscogsHow: "How to get a Discogs token ↗",
  welcomeAcoustid: "AcoustID",
  welcomeAcoustidWhy:
    "Lets an album be identified by the sound of the audio itself, which is what "
    + "answers when the file and folder names say nothing useful. Free for "
    + "personal use, and it also needs Chromaprint installed.",
  welcomeAcoustidHow: "How to get an AcoustID key ↗",
  // Both fields say the link is welcome, because the Spotify bridge has no
  // field of its own and an invitation nobody sees is a feature nobody finds.
  // How the shelf is ordered. "Recently added" is the rule and the
  // default: the last record in is the first one seen. The others are named for
  // what they put at the top, because that is the only part of a sort anyone
  // reads.
  sortBy: "What puts these albums in order",
  sortRecent: "Recently added",
  sortOldest: "Oldest first",
  sortArtist: "Artist A–Z",
  sortAlbum: "Album A–Z",
  sortYear: "Release year, newest",
  // Named for the top, like the rest — and the end of both is the same place:
  // an album with no stars follows every album with them, whichever way the
  // scale is read.
  sortRatingBest: "Rating, best first",
  sortRatingWorst: "Rating, worst first",
  acquireQueryPlaceholder: "Artist and album — or paste a Spotify link",
  // It says what pasting a link is *for*, because the box beside `By artist &
  // album` otherwise reads as a second way to do the same search.
  pasteLinkPlaceholder:
    "Paste a Discogs, MusicBrainz or Spotify link to find one specific release",
  welcomeSpotify: "Spotify",
  // Says what the key buys and what it does not, because this one is the
  // easiest to mistake for a fourth catalogue.
  welcomeSpotifyWhy:
    "Only for reading a link you paste: the album you are listening to becomes "
    + "words to search Soulseek, Discogs and MusicBrainz with. Nothing from "
    + "Spotify is ever stored, tagged, or written into a name. Without a key an "
    + "album link still works, by title alone, and a track link cannot be read "
    + "at all.",
  welcomeSpotifyId: "Client ID",
  welcomeSpotifySecret: "Client secret",
  welcomeSpotifyHow: "How to create a Spotify app ↗",
  welcomeSlskd: "Soulseek (slskd)",
  welcomeSlskdWhy:
    "The only way this application acquires music: your own slskd server, which "
    + "it asks for searches and downloads. Nothing here reaches Soulseek "
    + "directly, and without this key Find music cannot be used at all.",
  // Not a link, because there is no page to send anyone to: unlike the other
  // keys, this one is not issued by a service — it is written into the user's
  // own slskd configuration and the same value is pasted here. The address and
  // port stay in config.toml, which already carries a working default.
  welcomeSlskdHow:
    "This is the API key from your own slskd configuration — the same value, "
    + "pasted here. Where the server lives is set in config.toml.",
  connectSave: "Connect",
  connected: "Connected.",
  disconnected: "Disconnected.",
  connectFailed: "That key could not be saved.",
  // Never the key, only whether there is one. A value that reaches the page is
  // a value that can reach a screenshot.
  //
  // Every service, slskd included: this line is the only thing on screen that
  // says what is connected, and without slskd a whole tab does nothing.
  connectionState: (discogs, acoustid, spotify, slskd) =>
    `Discogs: ${discogs ? "connected" : "not connected"} · `
    + `AcoustID: ${acoustid ? "connected" : "not connected"} · `
    + `Spotify: ${spotify ? "connected" : "not connected"} · `
    + `slskd: ${slskd ? "connected" : "not connected"}`,
  // Ticked by default, and it promises what this actually does. Not "in my
  // Dock": macOS only lets a person put something there, and pinning would mean
  // writing the Dock's own preferences and restarting it, which is the
  // application operating the user's controls.
  welcomeIcon: "Put DigLibrary in my Applications folder, so I can open it without the Terminal",
  settingsIcon: "Put DigLibrary in my Applications folder",
  // Which of the two happened, because a gesture that reports nothing is one
  // somebody repeats. The path is shown: it is the only way to go and look.
  iconCreated: (where) =>
    `Created ${where} — open it from your Applications folder, and drag it to the Dock if you want it there.`,
  iconReplaced: (where) =>
    `Replaced ${where} — open it from your Applications folder, and drag it to the Dock if you want it there.`,
  // Quoting what failed rather than naming a cause nobody measured.
  iconFailed: (why) => `The icon could not be built: ${why}`,
  welcomeStart: "Start using DigLibrary",
  // The same screen reached from Settings, opened to look at a key and not to
  // be started.
  welcomeDone: "Done",
  settingsNote:
    "Cover art is copyrighted by its respective rights holders and provided by the " +
    "Cover Art Archive for archival purposes, at your own risk. Everything " +
    "else is configured in config.toml.",
  save: "Save",
  saved: "Saved.",
  confidence: (value) => `${Math.round(value * 100)}%`,
  tracks: (count) => `${count} tracks`,
  operationRenameFile: "Rename file",
  operationRenameFolder: "Rename folder",
  operationWriteTags: "Write tags",
  operationWriteImage: "Write cover",
  operationEmbedImage: "Embed cover",
  badgeAutomatic: "auto",
  badgeReview: "review",
  badgeOrganized: "✓ organized",
  badgeRejected: "rejected",
  badgeBlocked: "blocked",
  badgeNoMatch: "no match",
  badgeHeld: "held",
  // Said as a state of its own, because "review" claims this app looked and
  // wants a second opinion — and it has not looked at all yet.
  badgeUnlooked: "not scanned",
  // An album that is not in the folder it was recorded in. Said rather than
  // guessed at: "gone" would claim a deletion this app cannot see, and the
  // ordinary cause is that it was moved. It outranks every other badge because
  // no other fact about the album can be acted on until this one is answered.
  badgeFolderGone: "not where it was",
  tipFolderGone:
    "This album is not in the folder it was recorded in — it was moved, renamed "
    + "or removed. Scanning it can do nothing until it is found. Open it and "
    + "choose Relocate… to point at where it lives now.",
  relocateThis: "Relocate…",
  relocated: (name) => `Found — this album now reads from “${name}”.`,
  // Named with the count, because the run's own line says how many albums it
  // answered for and this is what it did not answer for.
  scanMisplaced: (count) =>
    count === 1
      ? "1 marked album is not where it was recorded, so nothing could be looked up for it."
      : `${count} marked albums are not where they were recorded, so nothing could be `
        + "looked up for them.",
  // The two proposals are not the same claim, and it is said here — the tooltip
  // of the badge an unscanned album already wears — and in the dialog's own
  // header, not on the shelf, where amber words read as a fault.
  tipFromTags:
    "This name was arranged from the album's own tags — nothing was asked of "
    + "any source. Scan it to look the pressing up, which is what a scan is for.",
  // *Whose* tags, said in the label: it is offered in the ⋯ and in the footer
  // of an album in review, and the tags in question are the album's own, which
  // is the whole of what it does.
  arrangeThis: "Arrange by its own tags",
  // What the gesture does, all of it. The cover line is here because the folder
  // getting a picture would otherwise be met in the plan without having been
  // announced — and it is the album's own picture, taken from the files, never
  // fetched.
  tipArrangeThis:
    "Name the folder and the files from what this album's own tags say, "
    + "ignoring the catalogue that answered for it. The folder also gets the "
    + "cover the files already carry. Nothing is written until you approve "
    + "the plan.",
  // The Cover Art Archive is reached through MusicBrainz, so an album no
  // MusicBrainz release matched can never be offered a sleeve. This is how one
  // reaches them.
  chooseCover: "Choose a cover…",
  // The same gesture over an album that already has a picture.
  changeCover: "Change cover…",
  tipChooseCover:
    "Pick an image from this Mac. It becomes the folder's cover, the picture "
    + "inside every track and the tracks' icons in the Finder. Nothing is "
    + "written until you have seen it and pressed Write this cover.",
  // The three places a cover lives, named — the Finder icon above all, since it
  // is the one seen without opening anything.
  coverWaiting: (files, icons, w, h) =>
    `A cover you chose is waiting: ${w}×${h}, for the folder, inside `
    + `${files} file${files === 1 ? "" : "s"}`
    + (icons ? ` and on ${icons} Finder icon${icons === 1 ? "" : "s"}.` : "."),
  coverWaitingReplaces: (n) =>
    ` It would replace ${n} picture${n === 1 ? "" : "s"} already there.`,
  coverWrite: "Write this cover",
  coverDiscard: "Not this one",
  coverWritten: "The cover was written. History can put the old one back.",
  // A ⋯ entry, so the count is part of the label — a menu line has no tooltip
  // to carry the rest of the sentence.
  arrangeMarked: (count) =>
    count ? `Arrange ${count} by tags` : "Arrange by tags",
  arrangedDone: (count, reasons) => {
    const done =
      count === 1 ? "1 album arranged from its tags." : `${count} albums arranged from their tags.`;
    return reasons.length ? `${done} ${reasons.join(" ")}` : done;
  },
  arrangeSkipped: (reason, count) =>
    ({
      organized: `${count} organized — untouched; arrange one from its own dialog.`,
      scanned: `${count} already answered by a scan — kept.`,
      wanting: `${count} whose tags do not say enough or whose name is taken.`,
      failed: `${count} could not be read or applied.`,
    })[reason] || "",
  // The catalogue's answer read against what the album's own files were saying.
  tagsAgree: "The source agrees with your tags.",
  tagsDiffer: (diffs) => `The source differs from your tags: ${diffs.join("; ")}.`,
  // The dialog of an album nothing has been compared for. A fold is the wrong
  // place for it: there is no `now → after` table to read, and the one gesture
  // the album is waiting for belongs in the middle of the notice rather than in
  // the footer among the others.
  contestedHeading: "This album could not be settled on its own",
  // Both directions: telling the reader to check needs a way to answer "no" as
  // well. One sentence stays on screen, because it is the one that says where
  // to look and what can be done anyway; the rest is on hover, where length
  // costs nothing.
  contestedNote:
    "The rows in amber are the ones in doubt \u2014 answer them beside the name, "
    + "or apply the rest of the album meanwhile.",
  tipContestedNote:
    "Some of these tracks are close enough in length that the app cannot tell "
    + "which file is which, and their titles do not say. Each amber row shows "
    + "your file against the release track it was matched to: say \u201cyes, this "
    + "one\u201d, or use \u201cnot this file\u201d to pick a different one. The "
    + "files in doubt are left exactly as they are.",
  confirmPairings: "These pairings are right",
  tipConfirmPairings:
    "Record these pairings as yours. Nothing is written to your files by this; "
    + "it only settles which file is which so the plan can be approved.",
  pairingsConfirmed: "Recorded. The plan can be approved now.",
  unscannedHeading: "Album not scanned yet",
  // The one thing that is always true here, which is also the thing that
  // decides whether Apply should be pressed: a dropped folder arrives arranged
  // from its own tags, so there may be a table below, and no catalogue has been
  // asked about it.
  unscannedNote:
    "No catalogue has been asked about this album. Any names below come from "
    + "its own tags — scanning is what compares it with an online source.",
  scanThisAlbum: "Scan this album",
  // The same gesture on an album this app has already looked at: the word
  // `again` says the button re-asks rather than asks, as `Plan this album
  // again` does.
  scanThisAlbumAgain: "Scan this album again",
  // What the same button says while the scan it started is running. The button
  // is the thing that was pressed, so it is where the answer to *did that
  // work?* belongs — the shelf's progress bar is behind the dialog.
  scanningThisAlbum: "Scanning this album…",
  // What the audio answered, said in one short line in every case, including
  // the ordinary one: silence would read the same for "never asked" and for
  // "asked, and the service does not know this record". It lives inside
  // `View details`, so saying it always costs nothing. Every one of these names
  // the service, so it is clear what spoke, and each is a label rather than a
  // sentence. The tooltip below carries the whole explanation for the one
  // state that earns it.
  acousticNotAsked: "AcoustID: not asked — the names settled it",
  acousticUnknown: "AcoustID: does not know these recordings",
  // Said plainly, because it is how this album was found and not a footnote.
  // Worded away from `proofAudio` ("proven by audio", which is about durations
  // agreeing): this one is the fingerprint naming the recording.
  acousticNamed: "AcoustID: recognised this album by its fingerprint",
  tipAcousticNamed:
    "The audio itself was fingerprinted with Chromaprint (fpcalc) and the "
    + "fingerprint was sent to AcoustID, whose database maps it to MusicBrainz "
    + "recordings. Three tracks spread across the album are asked, and only the "
    + "releases all three agree on are considered — then they are scored on the "
    + "same evidence as any other candidate. Nothing is written from this.",
  acousticUnavailable: "AcoustID: unavailable — fpcalc or the key is missing",
  // One line for every tag write. A line per file listing the same field names
  // is a wall that gets skipped, and the values are already on the rows above.
  // `repairs` is deliberately worded apart from `covers`: a repair puts the
  // file's own picture back, byte for byte, so calling it an embedded cover
  // would claim a new image where none arrives.
  tagsFolded: (files, fields, covers = 0, repairs = 0, icons = 0) =>
    `${files} file${files === 1 ? "" : "s"} · ${fields.join(", ")}`
    + (covers ? `, cover (embedded in ${covers} of them)` : "")
    + (repairs ? `, cover label repaired in ${repairs} of them (same image)` : "")
    // The third place a cover lives, named apart from the other two: this one
    // is what the Finder draws on the file, and it is only ever given to a file
    // that has none.
    + (icons ? `, Finder icon drawn on ${icons} of them (from their own cover)` : ""),
  tagsFollowTheNames: "Each file is written the values shown for its own track above.",
  tipScanThisAlbum:
    "Look this one album up now. Nothing is written — whatever it finds waits for "
    + "you to approve it.",


  // --- what each button does, and what it costs ------------------------------
  // Said as a consequence rather than as a restatement of the label: the label
  // already says the verb, the tooltip is where the price goes.
  // Choosing a folder adds its albums to the shelf the moment it is chosen, so
  // the tooltip says that first and what Scan will read second.
  tipChooseFolder:
    "Add a folder's albums to the shelf, keeping everything already on it. "
    + "Nothing is looked up until you press Scan, and Scan reads this folder.",
  tipScan: "Read this folder and identify what is in it. Nothing is written — every change waits for you to approve it.",
  tipSettings: "Naming style, cover art, and how sure this app must be before it acts without asking.",
  tipGridView: "Show albums as sleeves.",
  tipListView: "Show albums as rows, with the folder each one is in.",
  tipApplyAutomatic: "Apply every album this app is sure about, in one run. All of it can be undone afterwards from History.",
  tipRevertRun: "Put every album of the last run back exactly as it was — names, tags and pictures.",
  tipExportReport: "Write a spreadsheet beside your library: one row per album, what went in and what came out.",
  tipOpenFolder: "Open the scanned folder in the Finder.",
  tipFindLink: "Paste the address of a release on Discogs or MusicBrainz to say what this album is.",
  tipFindNames: "Search the catalogues by artist and album name.",
  tipFind: "Look this album up again and list what the catalogues offer.",
  tipUseCandidate: "Identify this album as this release, and re-plan the names from it.",
  tipApprove: "Write everything shown above to disk: names, tags and pictures. Reversible from History.",
  tipCancelLast:
    "Go back to how this album was before the last thing you asked for. "
    + "Nothing on disk is touched either way.",
  tipReject: "Say this identification is wrong. The files are left untouched and the album drops out of the run.",
  tipHold: "Keep this album out of the automatic run and decide about it yourself later.",
  tipAlbumMore: "The ways back in: scan or plan this album again, or arrange it by its own tags.",
  // Walking the shelf from inside the dialog, and saying the position.
  // The words name the shelf on purpose: what these move through is the list
  // the Library is drawing, filter and search and order included.
  tipAlbumPrevious: "The album before this one on the shelf.",
  tipAlbumNext: "The next album on the shelf. Filter the Library to walk only what you mean to review.",
  albumPosition: (at, total) => `${at} of ${total}`,
  tipCloseDialog: "Close. Nothing is written by closing.",

  // --- quality ----------------------------------------------------------------

  // --- acquisition ---------------------------------------------------------
  tabAcquire: "Find music",
  tabTransfers: "Transfers",
  transfersHeading: "What is coming in",
  transfersNote:
    "Every download your slskd is holding, from everyone. It keeps going while " +
    "this window is shut.",
  transfersNothing: "Nothing is downloading.",
  transfersOffline:
    "Your slskd server is not answering, so there is nothing to report.",
  transfersClear: "Clear downloaded",
  transfersClearTip:
    "Remove finished transfers from this list. Nothing is deleted — the files " +
    "stay where they were downloaded.",
  // Said when the window itself fails: a screen that never finished drawing
  // looks exactly like a screen with nothing on it.
  // --- when this window, or the bridge under it, fails ---
  unknownFailure: "Something failed and said nothing about what. Please report this.",
  bridgeMissing:
    "This window has lost its connection to the application. Quit DigLibrary and open it again.",
  callMissing: (name) =>
    `This window asked the application for “${name}”, which it does not offer. ` +
    "The window and the application are probably out of step — quit DigLibrary " +
    "and open it again, rather than only reopening the window.",
  callFailed: (name, detail) => `“${name}” failed: ${detail}`,
  // Said rather than swallowed: the gestures behind the album menu act on
  // whichever album is open, so an album that could not be opened has to stop
  // them — otherwise `Reject` rejects the one looked at before it.
  albumGone: "That album could not be opened, so nothing was done to it.",
  windowFailed: (detail) =>
    `This window failed to draw part of itself, so what you are seeing may be ` +
    `incomplete. Please send this line: ${detail}`,
  // **The shelf below this sentence is empty because nothing was ever read**,
  // and that is the whole reason to say it: an empty library and a library that
  // could not be reached look exactly alike. It names the remedy first, because
  // quitting and opening again is what fixes it nearly every time.
  windowNeverMetTheApplication:
    "This window never reached DigLibrary itself, so nothing below has been " +
    "read from your library — it is not empty, it was never asked. Quit " +
    "DigLibrary and open it again. If it happens twice, the window and the " +
    "application are out of step: reinstall with the last two commands from " +
    "the README.",
  transfersCleared: "Finished transfers cleared. The files are untouched.",
  // --- acting on a transfer, from the right-click menu ---
  // Pausing and cancelling are the same act with different intentions: this
  // network has no pause, and what makes a stop reversible is that the partial
  // file stays. So the wording says what is kept, not what is done.
  // Named apart from the search screen's `menuSeeAllFiles`, which means "show me
  // the rest of this share" and not "reveal this on my disk". Two keys with one
  // name in one object literal is a silent overwrite: the later wins and the
  // earlier menu quietly reads the wrong words.
  menuRevealFolder: "Open folder",
  // Clear, not delete. The row goes; the album, the files and the History entry
  // all stay. The same word as Clear downloads and Clear downloaded, on purpose.
  menuForget: "Clear from Library",
  tipForget:
    "Take this album off the Library screen. Nothing is deleted — the files stay "
    + "where they are, History still answers for it, and the next scan of that "
    + "folder brings it back.",
  forgotten: "Cleared from the Library. Nothing on disk was touched.",
  tipOpenAlbumFolder: "Reveal this album's own folder in the Finder.",
  tipSendToQuality:
    "Put this album on the Quality bench and open it there. Nothing is decoded "
    + "— measuring is still a gesture you make on that screen.",
  // A menu entry, not a button of its own: the gesture is rare here, and
  // Transfers already offers it on every row.
  menuOpenDownloads: "Open downloads folder",
  tipLibraryMore:
    "The rarer gestures: open the downloads folder, or take organized albums "
    + "off the shelf.",
  // The button says what pressing it would do; the setting says how a screen
  // opens. Two different sentences on purpose.
  // "Tree" is what the control is made of, not what is being asked for, and it
  // translates badly. Expand and collapse are what every list in every
  // application calls this.
  openTree: "Expand all",
  closeTree: "Collapse all",
  tipOpenTree:
    "Show everything on this screen unfolded, for now. What you closed by hand "
    + "stays closed, and Settings decides how it opens next time.",
  tipCloseTree:
    "Fold everything on this screen, for now. Settings decides how it opens "
    + "next time.",
  menuPause: (count) => (count === 1 ? "Pause download" : `Pause these ${count}`),
  menuRetry: (count) => (count === 1 ? "Retry download" : `Retry these ${count}`),
  menuCancel: (count) => (count === 1 ? "Cancel download" : `Cancel these ${count}`),
  // Off the list, never off the disk — the same distinction as Clear
  // downloaded, said again because the cost of getting it wrong is music.
  // One sentence for every way a transfer can be over — cancelled, failed,
  // finished, or in a state this window has no word for. `Cancel` over an
  // album already cancelled reads as doing it again rather than as taking the
  // row off the screen.
  menuRemoveTransfer: (count) =>
    count === 1 ? "Remove from this list" : `Remove these ${count} from this list`,
  // A folder that stopped short does not join the Library on its own, because
  // a file cut off mid-write measures as a recording that does not exist. A
  // peer that went away leaves one that will never be whole, so this is how
  // that is overruled — and `Retry` is on the same menu for whoever would
  // rather have the rest.
  menuTakeIncomplete: "Take it as it is",
  transfersTakenIncomplete: (what) =>
    `${what} will be taken as it stands, with the files that arrived.`,
  // Every one of these names what it acted on, because the same menu answers
  // for one track, one album and one user, and afterwards the only way to know
  // which it was is to be told.
  transfersPaused: (count, what) =>
    `Paused ${count} in ${what}. What arrived is kept; resuming carries on from there.`,
  transfersResumed: (count, what) =>
    `Asked again for ${count} in ${what}, from where they stopped.`,
  transfersCancelled: (count, what) =>
    `Cancelled ${count} in ${what}. Nothing already on disk was deleted.`,
  transfersNotThereYet: (what) =>
    `${what} has not arrived yet — this is the folder it will appear in.`,
  transfersResumedOnOpen: (count) =>
    count === 1
      ? "One transfer had stopped and was asked for again."
      : `${count} transfers had stopped and were asked for again.`,
  // Said when a download finishes reading itself into the library, so the album
  // appearing in the list is an event and not a surprise.
  collected: (albums, folder) =>
    albums === 1 && folder
      ? `${folder} finished downloading and is in your library.`
      : `${albums} downloaded ${albums === 1 ? "album is" : "albums are"} in your library.`,
  // --- folders dropped onto the window ---
  dropHere: "Drop album folders here to add them",
  dropNote: "They are read where they are. Nothing is copied or moved.",
  adopting: "Reading what you dropped…",
  adopted: (albums) =>
    albums === 1 ? "One album added to your library." : `${albums} albums added to your library.`,
  adoptedNothing: (folders) =>
    folders === 1
      ? "Nothing in that folder looked like an album."
      : "Nothing in those folders looked like an album.",
  // Found, and already here. Said apart from the sentence above: an album
  // already on the shelf is not a folder where nothing looked like an album.
  // **Said, and nothing else.** The album is named, so the search is the
  // user's to use with the words in front of them; the application does not
  // write into the search box.
  adoptedKnown: (names) =>
    names.length === 1
      ? `“${names[0]}” is already in your library. Search for it by name to find it.`
      : `${names.length} of these albums are already in your library: `
        + `${STR.someNamed(names)}. Search for them by name to find them.`,
  // Up to three names, quoted, and a count of the rest.
  someNamed: (names) => {
    const quoted = names.slice(0, 3).map((name) => `“${name}”`);
    const rest = names.length - quoted.length;
    if (rest) return `${quoted.join(", ")} and ${rest} more`;
    return quoted.length > 1
      ? `${quoted.slice(0, -1).join(", ")} and ${quoted[quoted.length - 1]}`
      : quoted.join("");
  },
  // A new album whose every stream is already on the shelf, in another folder.
  // Accepted as its own album, and said at the moment it arrives. Nothing is
  // moved or deleted, and the sentence says so.
  // The copy already here is named by where it is: the two usually carry the
  // same folder name, and a sentence naming one twice says nothing.
  arrivedSameAudio: (found) =>
    found.length === 1
      ? `“${found[0].album}” holds the same audio as an album already in your library, `
        + `at ${found[0].twin_folder}. Both are kept; nothing was moved or deleted.`
      : `${found.length} albums that just arrived hold the same audio as albums already `
        + `in your library: ${STR.someNamed(found.map((entry) => entry.album))}. `
        + "Both copies of each are kept; nothing was moved or deleted.",
  sameAudioHeading: "Already in your library",
  sameAudioOk: "OK",
  // The relocation the database refused because the folder is another album's,
  // with the same audio: the offer is to let this card go.
  secondCopyHeading: "This card is a second copy",
  secondCopyYes: "Let this card go",
  // The fourth answer: an album is followed by its audio when the folder it was
  // recorded in no longer exists, so dropping a moved album re-anchors it.
  // Saying where it landed is what makes that a gesture instead of a shrug.
  adoptedMoved: (album, now) => `${album} moved, and the library now points at ${now}.`,
  // The same sentence for the same act, said by the two gestures that also do
  // it: planning an album again, and arranging one by its own tags. Both look
  // for a moved album where this app has been shown, and both re-anchor the row
  // when the audio agrees.
  followedTo: (album, now) => `${album} had moved — found it, and the library now points at ${now}.`,
  adoptedMovedMany: (albums) =>
    `${albums} albums had moved, and the library now points at where they are.`,
  colProgress: "Progress",
  // The columns a Soulseek client shows, in the same words.
  colName: "Name",
  colStatus: "Status",
  colEta: "ETA",
  colPlace: "Place",
  // Said as a word rather than as the service's own vocabulary, and never as
  // an underscore: `in_progress` is a state name, not something to read.
  transferState: (state) =>
    ({
      queued: "Queued",
      in_progress: "Downloading",
      completed: "Complete",
      failed: "Failed",
      cancelled: "Cancelled",
      // Everything stopped and not all of it here — which slskd can report as
      // `completed` on every file while a byte count says otherwise.
      incomplete: "Incomplete",
      unknown: "—",
    })[state] || "—",
  tipResize: "Drag to resize this column.",
  // --- history, for what arrived rather than what was renamed ---
  historyDownloaded: "Downloaded",
  // What a download row says when it did *not* become an album: a request
  // whose files never arrived must not read `Downloaded`.
  historyArriving: "Nothing arrived yet",
  historyWaiting: "On disk, not filed yet",
  historyCleared: "Set aside",
  tipHistoryArriving: (asked) =>
    `Nothing of this has reached the disk. Asked for on ${asked}. ` +
    "It may still be coming, or the person sharing it may be gone — " +
    "right-click to stop waiting for it.",
  tipHistoryWaiting: "The folder is here; the next pass will file it.",
  menuForgetDownload: "Stop waiting for this",
  tipMenuForgetDownload:
    "Takes it off the waiting list. No file is touched and the row stays in History.",
  forgotDownload: "No longer waiting for that download.",
  historyFrom: (source) => `From ${source}`,
  historyFiles: (files, size) => `${files} file${files === 1 ? "" : "s"}, ${size}`,
  historyNotArrived:
    "This one has not finished arriving, so the downloads folder was opened instead.",
  acquireHeading: "Find music on Soulseek",
  acquireSearch: "Search",
  acquireSearching: "Searching… peers answer over a few seconds.",
  // A count beats a spinner: the wait is several seconds long and the numbers
  // moving are the evidence that it is working.
  acquireHeard: (sources, files) =>
    sources
      ? `${sources} user${sources === 1 ? "" : "s"} answered, ${files} file` +
        `${files === 1 ? "" : "s"} so far…`
      : "Searching… peers answer over a few seconds.",
  acquireTabClose: "Close this search",
  acquireNoTabs: "Search for an artist and album. Each search opens its own tab.",
  // --- the folder, read from its source ---
  acquireReading: "reading the folder…",
  acquireFolderOffline: "This user is not online, so only what the search found is shown.",
  acquireFolderFull: (shown, matched) =>
    `${shown} file${shown === 1 ? "" : "s"} in the folder; the search matched ${matched}.`,
  // --- right-click ---
  menuDownloadFolder: "Download folder",
  // Once the folder has been read there is a real number, and it is worth
  // saying: a compilation is where "the whole folder" surprises you.
  menuDownloadFolderCount: (n) => `Download folder (${n} file${n === 1 ? "" : "s"})`,
  menuDownloadFile: "Download this file",
  // A search answers with the tracks whose names carried the words — on a
  // compilation that may be one track of many — and this is how the folder
  // they are in becomes visible and takeable.
  menuSeeAllFiles: "See all files",
  menuCopyName: "Copy name",
  menuCopied: "Copied.",
  acquireAlreadyRequested: "You have already asked for this.",
  acquireStaleEngine:
    "This window is newer than the app running behind it, so results are not " +
    "being ranked and matching folders are not opening. Quit DigLibrary and " +
    "open it again — reopening the window alone is not enough.",
  // The same promise as the two Clears above, about the albums whose question
  // is already answered. A menu entry in the bar's ⋯; the promise — nothing
  // deleted, nothing moved — is kept by the toast that answers it.
  clearOrganized: "Clear organized",
  organizedCleared: (n) =>
    `${n} organized album${n === 1 ? "" : "s"} taken off the Library. No file was touched.`,
  settingDensity: "How much room the lists take",
  densityCompact: "Compact — more on screen",
  densityCosy: "Cosy",
  densityRoomy: "Roomy — easier to read",
  // Some shares call a whole music dump one folder: a search can match one
  // file in a folder of hundreds.
  acquireBigFolder: (files, size) =>
    `This folder holds ${files} files — ${size}. Download all of it?`,
  acquireBigFolderYes: "Download it all",
  acquireBigFolderHeading: "That is a large folder",
  // The reversal follows an album that has been filed away, and says
  // where it is about to write before it writes: undoing inside the music
  // library is a different thing from undoing inside a downloads folder, and
  // the folder is named so that the answer is about a place and not a word.
  revertFiledAwayHeading: "This album has been filed away",
  revertFiledAway: (count, folder, parent) =>
    `${folder} is in ${parent} now, not where it was organized. `
    + `Undoing puts ${count} change${count === 1 ? "" : "s"} back there, `
    + "under the names the album had before.",
  revertFiledAwayRun: (albums, folder, parent) =>
    albums === 1
      ? `${folder} is in ${parent} now, not where it was organized. `
        + "Undoing puts it back there, under the names it had before."
      : `${albums} albums of this run have been filed away since — ${folder} is `
        + `in ${parent} now. Undoing puts each one back where it is, `
        + "under the names it had before.",
  revertFiledAwayYes: "Undo it there",
  settingKeepTrees: "Expand lists automatically in Transfers and Find music",
  acquireFound: (folders, sources) =>
    `${folders} folder${folders === 1 ? "" : "s"} from ${sources} ` +
    `user${sources === 1 ? "" : "s"}.`,
  acquireNothing: "Nobody answered. Try fewer words, or a different spelling.",
  // What is wrong, one sentence each, and each naming the one thing to do about
  // it. A single sentence about a server not answering would be wrong for a
  // server that is answering and only wants a key.
  acquireOffline:
    "Your slskd server is not answering. Check that it is running, and that "
    + "config.toml points at its address.",
  acquireNoKey:
    "Your slskd server needs an API key, and this application has not been given "
    + "one. Add it in Settings → Connections.",
  acquireKeyRejected:
    "Your slskd server refused the API key it was given. Check the key in "
    + "Settings → Connections against the one in your slskd configuration.",
  // Anything this window has no sentence for. It says so and quotes the backend
  // rather than picking one of the sentences above and being confidently wrong.
  acquireUnusable: (detail) =>
    detail
      ? `Acquisition cannot be used right now: ${detail}`
      : "Acquisition cannot be used right now.",
  acquireAbsent: "This build has no acquisition provider.",
  acquireDisabled: "Acquisition is switched off in the configuration.",
  colUser: "User",
  colFree: "Free",
  colRate: "K/s",
  colFolder: "Folder",
  colFile: "File",
  colSize: "Size",
  colAttributes: "Attributes",
  acquireFileCount: (n) => `${n} file${n === 1 ? "" : "s"}`,
  // Before the folder has been read, the only number this app has is how many
  // files the *words* matched — and saying "1 file" about a folder of eighteen
  // states something it does not know and got wrong. A search on this network
  // cannot report a folder's size; only browsing it can. So it says what the
  // number is, and the ⋯ says there is more to find out.
  acquireMatchedCount: (n) => `${n} matched ⋯`,
  tipAcquireMatched:
    "How many files in this folder matched your words. The folder almost certainly "
    + "holds more — right-click and choose “See all files” to ask the user sharing it.",
  acquireQueued: (n, source) =>
    `${n} file${n === 1 ? "" : "s"} on the way from ${source}.`,
  // What was asked for and what was taken are two numbers, and the window must
  // show the second. A compilation reported as eighteen on the way, with one
  // arriving, misstates the only thing the gesture was for.
  acquireQueuedShort: (queued, asked, source) =>
    `${source} took only ${queued} of the ${asked} files asked for. `
    + "The rest were refused — the peer may no longer offer them, or they may "
    + "already be in your Transfers list.",
  acquireOrganize: "Organize what I downloaded",
  acquireOrganizeTip:
    "Point the organizer at the folder finished downloads land in. Nothing is " +
    "renamed until you approve the plan, as always.",
  tipAcquireFree: "This user has an upload slot free right now.",
  tipAcquireQueued: (n) => `${n} people are already waiting in this user's queue.`,
  tipAcquireRow: "Click to see the files inside. Double-click to download the whole folder.",
  tipAcquireTrack: "Double-click to download just this track.",
  tabQuality: "Quality",
  qualityHeading: "What is this audio, really?",
  // What this screen actually does: the bench shows what is already known and
  // decodes nothing until asked. `applyStrings` writes `textContent`
  // unconditionally, so this sentence and not the fallback in `index.html` is
  // what is seen.
  qualityNote:
    "Folders you add stay here. Nothing is decoded until you ask, and nothing is "
    + "ever renamed, tagged or written from this screen.",
  qualityMeasuring: (done, total) => `Measuring ${done} of ${total} albums…`,
  qualityFinished: (albums, suspect) =>
    suspect === 0
      ? `Measured ${albums} album${albums === 1 ? "" : "s"} — every one is what it claims to be.`
      : `Measured ${albums} album${albums === 1 ? "" : "s"} — ${suspect} ${
          suspect === 1 ? "is" : "are"
        } not what the container claims.`,
  // The same promise as `qualityNote`: the bench reads nothing until asked.
  emptyQuality:
    "Add a folder — the whole library if you like. Its albums stay here between "
    + "sessions, showing what is already known about them.",
  benchAdd: "Add folder…",
  tipBenchAdd:
    "Put a folder on the bench, with everything under it. Reading it takes a moment — about half " +
    "a minute for a whole library — and it decodes nothing: what appears is what is already known.",
  // The same two questions the shelf's icons ask, in the same words, because
  // it is the same gesture on a different screen.
  tipBenchCardsView: "Show these albums as cards.",
  tipBenchListView: "Show these albums as rows. A bench of thousands reads better as rows.",
  benchWalking: (albums) => `Reading… ${albums} albums so far`,
  benchStarted: (folder) => `Reading ${folder}…`,
  benchFinished: (folder, albums) =>
    `${folder} is on the bench — ${albums} album${albums === 1 ? "" : "s"}.`,
  // On the album's own right-click, and naming the folder in both: a bench
  // folder may hold many albums, so "this" said over one row would be hundreds
  // of rows leaving at once without saying so. Removing clears rows and touches
  // no file and no measurement.
  benchRewalk: (folder) => `Read “${folder}” again`,
  benchRemove: (folder, albums) =>
    albums === 1
      ? `Take “${folder}” off the bench`
      : `Take “${folder}” off the bench (${albums} albums)`,
  menuSendToBench: "Send to quality analyzer",
  benchSent: (folder) => `${folder} is on the bench, in the Quality tab.`,
  // The word above the verdict: how much the verdict is worth, before acting.
  confStrong: "strong",
  confFair: "fair",
  confWeak: "worth a look",
  tipConfidence: (reasons) =>
    reasons.length === 0
      ? "Every track measured, from these files, and nothing near a line."
      : reasons.map((reason) => `· ${reason}`).join("\n"),
  benchAnalyze: (albums) => `Analyze ${albums} in depth`,
  // The same measurement, asked of the one album on screen. Named
  // `this album` rather than counted, because it is reached from the album
  // itself and a number there would be a count of one.
  benchAnalyzeThis: "Analyze this album in depth",
  tipBenchAnalyze:
    "Measure the marked albums properly: thirteen readings from 14 to 21.5 kHz instead of five, " +
    "the whole file instead of a minute, and what is left above the wall band by band. About " +
    "40 seconds an album. Nothing is written until you say so.",
  // Names the method rather than promising the outcome: "Identify N in depth"
  // would claim an answer the app does not have.
  // Not "signature" either: in this application that word already means the
  // digest of a container's declared shape, and the quality engine exists to
  // tell that apart from what the audio actually is.
  benchIdentify: (albums) => `Identify ${albums} by audio fingerprint`,
  tipBenchIdentify:
    "Fingerprint every file of the marked albums with Chromaprint and ask AcoustID "
    + "which recording each one is. One request a file against a service that allows "
    + "about three a second, so it is minutes rather than seconds. Nothing is renamed "
    + "and nothing is decided — the answers are written down and shown here.",
  benchIdentifying: (done, total, folder) =>
    `Asking about ${done} of ${total}${folder ? ` — ${folder}` : ""}…`,
  benchIdentified: (files, named, seconds) =>
    named === 0
      ? `Asked about ${files} file${files === 1 ? "" : "s"} in ${seconds}s — AcoustID knows none of them.`
      : `Asked about ${files} file${files === 1 ? "" : "s"} in ${seconds}s — AcoustID knows ${named}.`,
  benchAnalyzing: (done, total, folder) =>
    `Measuring ${done} of ${total}${folder ? ` — ${folder}` : ""}…`,
  benchAnalyzedNothing: (files, seconds) =>
    `Measured ${files} file${files === 1 ? "" : "s"} in ${seconds}s — every one agrees with what was already on record.`,
  benchAnalyzedHead: (files, seconds, differing) =>
    `Measured ${files} file${files === 1 ? "" : "s"} in ${seconds}s. ${differing} album${
      differing === 1 ? "" : "s"
    } would change.`,
  benchAdopt: "Write these down",
  benchDiscard: "Discard",
  tipBenchAdopt:
    "Replace what is stored for these files with what was just measured. Each row records the " +
    "audio it came from, so it answers for that file alone.",
  benchAdopted: (files) => `${files} measurement${files === 1 ? "" : "s"} written down.`,
  benchDiscarded: "Nothing was written. What was stored is untouched.",
  benchMoved: (folder, was, now) => `${folder}: ${was} → ${now}`,
  benchSameVerdict: (folder, changed) =>
    `${folder}: same verdict, ${changed} track${changed === 1 ? "" : "s"} read differently`,
  benchBandsHead: "Above the wall, band by band — a floor is flat, music keeps falling",
  // **Which reading convicted, said on the line itself.**
  //
  // Two proofs disagree about what a picture shows. A wall is a cliff anybody
  // can see in a spectrogram. The encoder's frame grid is invisible in one, and
  // a file caught by it draws a spectrum that looks perfectly healthy. The
  // word `transcoded` alone cannot tell those two apart, so the line does.
  // What this word is standing on, when it is not this file's own audio. A
  // content signature is codec, sample count, rate, channels and depth, and two
  // different songs of the same length in the same format agree on all five.
  // A row written before the audio had a name answers for every one of them,
  // and must not be drawn exactly like a word measured here.
  qSharedMeasurement: "another file's reading",
  tipSharedMeasurement:
    "This word comes from a measurement that was not taken from this file. It was "
    + "written before measurements recorded which audio they came from, so any file "
    + "of the same length and format reads it. Scan this album again to measure it "
    + "here.",
  qProofGrid: "frame grid",
  qProofWall: "wall",
  qProofSpectrum: "spectrum",
  tipProofGrid:
    "Convicted by the encoder's own frame grid, which a spectrogram cannot show. " +
    "Open the picture and read the note beside it.",
  // The same conviction on a file that also stops at a wall. It does not point
  // at the note, because there is none: the note exists to get in front of the
  // scepticism a healthy-looking picture creates, and this picture is not
  // healthy-looking.
  tipProofGridAndWall:
    "Convicted by the encoder's own frame grid, which a spectrogram cannot show — " +
    "and this file stops at a wall as well, which the picture does show.",
  tipProofWall:
    "Convicted by where the audio stops: a cliff at or below 19 kHz, which is a " +
    "filter and not an instrument. This one the spectrogram does show.",
  tipProofSpectrum:
    "Convicted by the pair of spectral signs — how steeply the energy falls, and " +
    "how little is left above the top band. Both had to be met.",

  // **The sentence that gets in front of the scepticism before the picture
  // creates it.** A reader who knows spectrograms and has never heard of a
  // frame grid sees a spectrum that reaches the top and concludes the
  // measurement is wrong, so the note is drawn beside the picture rather than
  // left in a tooltip nobody opens.
  qGridPictureHead: "The picture will not show this one.",
  qGridPictureBody:
    "What convicted it is not where the sound ends but where the frames begin: an " +
    "MP3 encoder chops audio into blocks of 576 samples and quantizes each block on " +
    "that grid, and decoding back to FLAC restores the waveform without erasing the " +
    "grid. This file still carries one. A spectrum that is full to the top does not " +
    "clear it — a 320 kbps MP3 is full to the top too.",

  // "Readings agree" reads, to anybody who has not been told otherwise, as one
  // measurement repeated — which would agree with itself and prove nothing.
  // What makes the agreement evidence is that the six readings could have
  // disagreed, so that is the half the sentence leads with.
  //
  // **At least four, because that is what convicts.** This is said about an
  // album, whose tracks need not agree with each other on the count, and the
  // rule asks four readings of six of each of them. *Six of six* was the rule
  // once, and stayed on the screen after the rule moved.
  qGridReadingsHead: "At least four of six readings agree.",
  qGridReadingsBody:
    "Not the same measurement repeated: the file is swept at all 576 possible frame " +
    "alignments, and read six different ways — two views of the channels, three " +
    "thresholds each. Each reading is free to name any of the 576. On every track " +
    "convicted here, at least four named the same one.",
  qGridPeak: (z) =>
    `The winning alignment stands ${z} deviations above the spread of the other 575.`,

  qYourWord: "your word",
  tipYourWord:
    "This verdict is what you said, not what was measured. What it measured is still on the " +
    "row, and open the album to withdraw your word and give the measurement back.",
  qColConfidence: "How sure",
  qAll: "all",
  qWeak: "worth a look",
  qTranscoded: "not lossless",
  qOverstated: "overstated",
  qHonest: "as declared",
  qBorderline: "borderline",
  qDamaged: "damaged",
  qBorderlineBadge: "borderline",
  tipBorderline:
    "Measured just short of the line that convicts: one sign of a transcode is fully there and " +
    "the other nearly is. Nothing is renamed and no label changes — this is only a mark asking " +
    "you to look. Open the row to judge it track by track.",
  planPaired: (fileMs, trackMs, deltaMs) => {
    const seconds = (ms) => (ms == null ? "—" : `${Math.round(ms / 1000)}s`);
    const gap = deltaMs == null ? "" : ` · ${(deltaMs / 1000).toFixed(1)}s apart`;
    return `${seconds(fileMs)} yours · ${seconds(trackMs)} published${gap}`;
  },
  // Said beside the numbers, never instead of them: the pair was settled by the
  // name and the published length disagrees anyway, which may be the only thing
  // on the screen saying the file is not the take its own name claims. It does
  // not block; it is read.
  planLengthOff: "this file's length does not match this track",
  // The truthful version for the case where files *are* there and none of them
  // was recognized. Saying "no file here" beside a file visible in the Finder
  // calls the folder empty.
  planTrackUnrecognized: "no file here was recognized as this track",
  evidenceTrackUnrecognized: "not recognized",
  planFileOnly: "no track on this release matches this file",
  planNoTrack: "left alone",
  // The release does not list this file, and the user may say which track it
  // is instead of leaving it behind. The number is offered, never inferred: the
  // one the file already carries describes that copy's order, and the plan is
  // renumbering everything to the release's.
  planAdoptLabel: "Make this track",
  planAdopt: "Add",
  planAdoptTip:
    "Give this file a place on the tracklist. Nothing is written until you "
    + "approve the plan, as always.",
  // The same control on a track already added, where the number is no longer a
  // proposal but still editable.
  planAdoptMoveDo: "Move",
  planAdoptMoveTip:
    "Put this track at another position. Everything from there down moves "
    + "one, and the plan above redraws so you can see it.",
  planAdoptNumberTip: "The place this file takes on the tracklist.",
  planAdoptMoved: (at) => `Now track ${at}. Everything from there down moved one.`,
  planAdoptStayed: (at) => `Still track ${at} — type another number to move it.`,
  planAdoptedDrop: "not a track after all",
  planAdoptedDropTip: "Leave this file alone again. Nothing on disk has been touched.",
  planYourNumbering:
    "The numbering below is yours, not the catalogue's: a track you added sits "
    + "where the release had another one.",
  // The two halves of one failed pairing, brought together. The word
  // is "unclaimed", not "probably this one": the pairing failed because nothing
  // cleared the bar, and naming a winner here would be the app guessing where it
  // had just refused to.
  // Said once under the table when several tracks are asking, instead of the
  // per-track heading above. Two sentences, because a release that matched
  // nothing is not the same situation as one that matched most of itself.
  planUnclaimedHere: (tracks, files) =>
    `${tracks} tracks found no file here, and ${files} ${files === 1 ? "file was" : "files were"} `
    + "claimed by none of them:",
  // The one notice of a record nothing matched. The fact is measured and stops
  // there — *this is the wrong record* is a conclusion nobody measured, and the
  // screen leaves it to the reader.
  wrongRecordFact: "Nothing in this folder matched this record.",
  // **Naming the ways out, and that nothing is written meanwhile.** A gesture
  // whose consequence is unknown is one nobody uses, however well it works.
  wrongRecordWays:
    "Check that it is the right one under Find this album, reject the match, "
    + "or press Arrange by its own tags to see the plan your files' own names "
    + "would make — nothing is written until you approve.",
  // The witness's way out. It says what the witness *is* in the same breath as
  // what it found, because a source that knows the record and may not name it
  // is the one thing on this screen that needs explaining.
  witnessSaid: (agreeing, compared) =>
    `iTunes knows this record, and ${agreeing} of ${compared} of your files' `
    + "names are on it. It is a witness, so it never names your files — but the "
    + "catalogues that do can be searched with its words.",
  // **Each control says what pressing it does**, for the same reason.
  witnessRead: "Read its tracklist",
  witnessSearch: "Search Discogs and MusicBrainz with its words",
  witnessHide: "Hide its tracklist",
  witnessLooking: "Looking…",
  // Asked again and answering differently is possible — the first answer was
  // bought at identification and this one is bought now — so it is said rather
  // than left as a control that did nothing.
  witnessGone: "iTunes does not answer for this record now.",
  witnessTrack: (position, name, ms) => `${position}. ${name} · ${(ms / 1000).toFixed(1)}s`,
  // What the record is over there, in the witness's own words — the answer to
  // *does this album exist anywhere*, which is what a folder no catalogue could
  // name actually asks. Written from what came back and never from a guess: a
  // year or a count the answer did not carry is simply absent.
  witnessHead: (record) =>
    [
      record.title,
      record.artist,
      record.year,
      record.track_count ? STR.tracks(record.track_count) : null,
    ]
      .filter(Boolean)
      .join(" · "),
  planLooseHead: (count) =>
    count === 1 ? "one file here is unclaimed:" : `${count} files here are unclaimed:`,
  planLooseWhy: (candidate) => {
    const parts = [`name ${Math.round((candidate.similarity || 0) * 100)}%`];
    if (candidate.delta_ms !== null && candidate.delta_ms !== undefined) {
      parts.push(`${(candidate.delta_ms / 1000).toFixed(1)}s apart`);
    } else if (candidate.file_ms) {
      parts.push(`${Math.round(candidate.file_ms / 1000)}s, no length published`);
    }
    return parts.join(" · ");
  },
  planLooseMore: (count) => `and ${count} more`,
  // Pairing a file to a track by hand. The verb is the user's — *this file is
  // this track* — and the app never says it back as though it had worked it
  // out: a row paired by hand says who paired it, and offers to take it back.
  planPickThis: "It's this one",
  planPickTip: "Say that this file is this track. Nothing is written until you apply.",
  planPickOther: "another file…",
  planPickOtherTip: "Choose any file in this folder for this track.",
  // The two answers a doubtful row needs. `planRepair` beside it is the other
  // one: pick a different file, or take the pairing back.
  // Said before the apply, naming the number: an apply that leaves files
  // untouched must say so, or the album looks finished and is not.
  // Two headings and two answers, because these questions are asked in the
  // window's own dialogs, which hold markup; a system box holds plain text.
  applyLeavesDoubtfulHeading: "Not every track matched exactly",
  applyLeavesDoubtful: (count) =>
    `${count} ${count === 1 ? "file is" : "files are"} still in doubt and will be left `
    + `exactly as ${count === 1 ? "it is" : "they are"} — no rename, no tags. `
    + `Everything else in the album is applied.`,
  // The button says what it does. `OK` says nothing about which of the two
  // things on screen is about to happen.
  applyLeavesDoubtfulYes: "Apply what is certain",
  // The other confirmation before a write, and the one that says what the write
  // is *made of*. An album nobody has scanned still arrives with a plan — its
  // own tags, worked out on the way in — and that plan is applied by a button
  // that looks exactly like the one applying a catalogue's answer.
  applyWithoutScanHeading: "This album has not been scanned",
  applyWithoutScan:
    "No catalogue has been asked about it, so nothing here was compared with an "
    + "online source. What would be written is what this album's own tags say — "
    + "the names below and nothing else.",
  // The other album that reaches this dialog, and the pair `arrangedHeader` and
  // `arrangedInsteadHeader` already draw one fold above it.
  // The write is the same and the reason to read it is the same; what is not
  // true of this one is that nobody looked.
  applyInsteadOfScanHeading: "This writes your tags, not the answer a catalogue gave",
  applyInsteadOfScan:
    "A catalogue has answered about this album, and this plan sets that answer "
    + "aside. What would be written is what this album's own tags say — the "
    + "names below and nothing else.",
  applyWithoutScanYes: "Apply the tags",
  planConfirmThis: "yes, this one",
  planConfirmThisTip:
    "Record this pairing as yours. Nothing is written to your files by this; "
    + "it settles which file this track is, so the album can be applied.",
  planRepair: "not this file",
  planRepairTip: "Pair a different file with this track, or take the pairing back.",
  planPickNone: "— no file —",
  planPickClaimed: (position) => `now on track ${String(position).padStart(2, "0")}`,
  planByHand: "you paired this",
  planUndoPair: "undo",
  planUndoPairTip: "Take back this pairing and let the matcher answer again.",
  planPairedByHand: "paired by you, not measured",
  // Pairings made against a release this album no longer carries. Kept, not
  // spent: the positions are the user's work, and only the user can say whether
  // the new tracklist is ordered like the one they were made against.
  // `corrections` rather than `pairings`: a track the user added is held here
  // too, and it is not a pairing. The word the database uses covers both
  // without calling either one the other.
  pairingsHeld: (count) =>
    count === 1
      ? "1 correction you made against the release this album had before"
      : `${count} corrections you made against the release this album had before`,
  pairingsHeldWhy:
    "Track numbers belong to a tracklist, so they are not carried over on their own.",
  pairingsHeldRow: (position, file, added) =>
    `track ${String(position).padStart(2, "0")} · ${file || "a file no longer in this folder"}` +
    (added ? " · a track you added" : ""),
  pairingsReuse: "Use them here",
  pairingsDiscard: "Discard them",
  pairingsReused: (count) =>
    count === 1 ? "1 correction reused." : `${count} corrections reused.`,
  pairingsDiscarded: (count) =>
    count === 1 ? "1 correction discarded." : `${count} corrections discarded.`,
  planMatchesShort: (paired, total) => `${paired}/${total}`,
  planMatches: (paired, total) => `Matches ${Math.round((100 * paired) / total)}%`,
  // The sentence behind the fraction says which half is open: `10 of 13
  // tracks` over a folder holding twelve files reads as thirteen files, when
  // the thirteen is twelve tracks plus the one file no track took.
  planMatchesDetail: (paired, filesUnpaired, tracksUnpaired) =>
    [
      `${paired} paired both ways`,
      filesUnpaired
        ? `${filesUnpaired} ${filesUnpaired === 1 ? "file" : "files"} without a track`
        : "",
      tracksUnpaired
        ? `${tracksUnpaired} ${tracksUnpaired === 1 ? "track" : "tracks"} without a file`
        : "",
    ]
      .filter(Boolean)
      .join(" · "),
  qColFolder: "Folder",
  qColVerdict: "Verdict",
  qColBitrate: "Looks like",
  qColMedian: "Median fall",
  qColTracks: "Measured",
  qColFindings: "Findings",
  qMeasured: (analyzed, tracks) =>
    analyzed === tracks ? `${tracks}` : `${analyzed} of ${tracks}`,
  encodingLossless: "lossless",
  encodingTranscoded: "was lossy",
  encodingLossy: "lossy",
  encodingOverstated: "overstated",
  encodingUndecided: "undecided",
  findingTranscoded: "transcoded",
  findingOverstatedBitrate: "bitrate overstated",
  findingInflatedBitDepth: "bit depth inflated",
  findingClipping: "clipping",
  findingDcOffset: "DC offset",
  findingSilent: "silent track",
  // Said on Quality and on Mixing, because it is one absence and one machine:
  // without it the screens hide a button without a word and blame the audio.
  // The install line is the whole point: naming the cause alone leaves the
  // reader with nothing to do.
  qualityUnavailable:
    "ffmpeg was not found, so no audio can be measured on this machine. "
    + "Install it (on macOS: brew install ffmpeg) and reopen DigLibrary.",

  // --- identification review ------------------------------------------------
  findByLink: "By link",
  findByNames: "By artist & album",
  pasteLinkFirst:
    "Paste a Discogs, MusicBrainz or Spotify link first — a release, a master, " +
    "or the album you are listening to.",
  // A Spotify link says what to look for; it never says what the album *is*.
  // The sentence has to make that difference visible, because the two are the
  // same gesture in the same field.
  lookingUpSpotify: "Reading that Spotify link…",
  searchingWith: (artist, album) =>
    artist
      ? `Searching Discogs and MusicBrainz for “${artist} — ${album}”…`
      : `Searching Discogs and MusicBrainz for “${album}”…`,
  spotifyWithoutArtist:
    "Spotify names the album but not the artist without a key of your own, " +
    "so this search is by title alone.",
  typeNamesFirst: "Type an artist or an album first.",
  inUse: "in use",
  evYourFiles: "Your files",
  evReleaseTracks: (source) => (source ? `This release (${source})` : "This release"),
  nowUsing: (title, facts) =>
    `Now using “${title}”${facts ? ` — ${facts}` : ""}.`,
  readyToApply: "Approve & apply is ready.",
  partialCoverage: "It does not cover every file — see below.",
};
