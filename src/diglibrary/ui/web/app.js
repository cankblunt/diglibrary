// The window's behavior: render what the API reports, forward gestures back.
// No business rule lives here — every decision comes from LibraryApi.

import { STR } from "./strings.js";

const $ = (id) => document.getElementById(id);

const state = {
  albums: [],
  history: [],
  settings: {},
  root: null,
  // Whether the chosen folder is still worth showing. The path is an unfinished
  // instruction — "this is what Scan will read" — and once the scan has read it
  // the frame has no reason to keep carrying it beside a Scan button.
  rootShown: false,
  // Which screen is in front. The Library's own controls live in the window's
  // frame, and answer for nothing while another tab is showing.
  tab: "library",
  scanning: false,
  // The Library is still being read back on the other side of the bridge. Like
  // a running scan, this keeps the event poll alive — that is how the window
  // hears that the albums arrived.
  restoring: false,
  // Whether this run was started by "Organize what I downloaded", so its end can
  // open the album it brought instead of leaving it to be found on the shelf.
  organizing: false,
  // How the shelf is ordered, and which albums were pointed at this session —
  // those sit above whatever the order says, newest gesture first.
  sort: "recent",
  pointedAt: [],
  // The History row last clicked, by `historyKey`. One at a time, and for the
  // session: it is about which row is being read, not about the run.
  historyCurrent: null,
  // Whether trees open by themselves, on both screens that have one. Settings
  // owns the default; this is the override for the current session, and the
  // button on each screen moves it.
  keepTreesOpen: true,
  // Which albums the next Scan will look up. Marking replaces an exclusions
  // dialog: unchecking a folder in a modal and unmarking the card on the shelf
  // are the same act, and only one of them can be seen.
  marked: new Set(),
  // The text in the Library's search box. Session state, like the shelf and the
  // status beside it, and deliberately not remembered between sessions: a
  // window that reopened onto an old search would look like a library that had
  // lost most of itself.
  search: "",
  // Which albums the running scan has not reported on yet, so a card can say
  // that it is being worked on instead of looking untouched while its badge
  // changes several seconds later.
  identifying: new Set(),
  // From the first press of `Approve & apply` until its answer is in.
  approving: false,
  // Which albums hold some of the same audio as another one, by unit id, and
  // the pairs behind it. Read with the shelf and never per card: this is one
  // query for the whole Library.
  shared: new Map(),
  sharedPairs: [],
  // Whether a scan finished in *this* session. Deriving the line at the top
  // from "there are albums on screen" would make a Library restored from the
  // database announce a scan that happened in an earlier session.
  scanFinished: false,
  openAlbum: null,
  // What the witness recognised for the album on screen, held for as long as
  // this dialog is drawing it and no longer. Cleared by every render, including
  // the render of the next album: a verification source's words are never
  // kept, and one held across albums would be a tracklist drawn under the
  // wrong notice.
  witnessRecord: null,
  covers: new Map(),
  // The albums the server answered for with no picture at all, so the dialog
  // can tell *no cover* from *not asked yet* and offer the right words.
  coverless: new Set(),
  coversPlanned: 0,
  // The run's own two numbers, kept because the line that stays is drawn again
  // on every tab change and every refresh, long after the event that carried
  // them. Reading the shelf instead would make that line describe a library
  // rather than a scan.
  scanIdentified: 0,
  scanUntouched: 0,
  searching: false,
  // Which rating the shelf is filtered to: `all`, `none`, or a number meaning
  // that many stars and better. Session state like the status filter beside
  // it, and not a setting: it is about what is being looked for now, not about
  // how the application should behave.
  rating: "all",
  adopted: null,
  // The row last clicked on each list, so the whole line stays lit while it is
  // read across. Kept in state rather than on the node, because both lists
  // rebuild themselves constantly — a poll every 700ms on one of them — and a
  // highlight that lives on the element would not survive a rebuild.
  picked: { acquire: null, transfers: null },
  lastRun: null,
  // Which way the last run can still go. A run is either applied or reverted, so
  // only one of `Revert this run` and `Do this run again` is ever offered.
  lastRunReverted: false,
  filter: "all",
  // What the status menu last counted, so the menu can be built from the same
  // numbers the button is showing.
  statusCounts: {},
  // The same two questions, asked of the History, which is otherwise a record
  // of every run findable only by scrolling. Not remembered between sessions,
  // for the reason written above the Library's `search`.
  historyFilter: "all",
  historySearch: "",
  historyCounts: {},
  view: "grid",
  reach: null,
  quality: [],
  qualityFilter: "all",
  // Which metadata sources the last run went without, and why. A fact about
  // that run rather than about the machine: it is what the line at the top
  // needs to qualify the count beside it.
  sourcesDown: {},
  qualityRunning: false,
  // The folders on the bench, and whether one is being read right now. They
  // outlive the session, so stored measurements are drawn again at the next
  // launch instead of waiting for a new survey.
  benchRoots: [],
  benchView: "list",
  // Whether ffmpeg is here at all. Without it nothing can be measured, and the
  // button that would measure is not offered rather than offered dead — and the
  // screen says why. One flag for every screen, because it is one fact about
  // the machine: read off the bench payload alone, Mixing would have no way to
  // know it and would blame the audio.
  canMeasureAudio: true,
  // What is marked for the next measurement, by album id. The same gesture the
  // Library's mark box is, because it is the same question: which of these is
  // meant.
  benchMarked: new Set(),
  // Which albums are unfolded on the quality screen, and what their tracks
  // measured — fetched per album, because a library may hold tens of thousands
  // of tracks.
  qualityOpen: new Set(),
  qualityTracks: new Map(),
  // The spectrograms that were asked for, by album folder and file name. Kept
  // here because the tracks table is rebuilt whole on every redraw, and a
  // picture that vanished when a verdict changed would have to be asked for
  // twice. Nothing is ever put here without a press.
  spectrograms: new Map(),
  findMode: "link",
  // A finished download is being read off the disk on the other side of the
  // bridge. Like a running scan, it keeps the event poll alive — that is how
  // the window hears that the album arrived.
  collecting: false,
  // Dropped folders are being read on the other side of the bridge. Like a
  // scan, it keeps the event poll alive until the answer arrives. A count, not
  // a flag: reads queue behind each other, and each one is released by its own
  // `adopted`, so the first answer must not let go of the second drop's wait.
  adopting: 0,
  // The same for a folder chosen with the +. It has its own counter rather than
  // borrowing `scanning`: a read beside a real scan would otherwise switch that
  // scan's bar off when it finished.
  opening: 0,
};

// --- bridge ---------------------------------------------------------------

/** The bridge, wrapped so that a call which fails can never fail quietly.
 *
 * Many places in this file call across to Python, and asking each of them to
 * remember to report a failure is asking for the one that forgets. Two things
 * can go wrong before a result exists at all, and neither is an ordinary
 * outcome:
 *
 * - the method is not there, which throws a `TypeError` synchronously and is
 *   not caught by any `.catch()` on the promise the call never returned. That
 *   shape leaves the whole window blank;
 * - Python raised, which rejects.
 *
 * Both are reported here, named with the method that did it, and then rethrown
 * so a caller that does handle them still can. What is *not* reported here is
 * `{ok: false}` — that is an answer, not a failure, and for some calls it is
 * the ordinary one: an album with no cover art answers exactly that.
 */
function errorText(error) {
  return error?.message || String(error);
}

/** Say what went wrong, once, and mark it so nothing says it a second time.
 *
 * These are rethrown so a caller that handles them still can — which means most
 * of them also reach the window's own last-resort handler, and that would print
 * a stack beside the sentence that already named the method. Worse, two stacks
 * of the same failure differ in their line numbers, so they do not even
 * collapse into one notice.
 */
function reported(error, message) {
  toastError(message);
  if (error && typeof error === "object") error.__reported = true;
  return error;
}

function api() {
  const bridge = window.pywebview?.api;
  if (!bridge) {
    throw reported(new Error(STR.bridgeMissing), STR.bridgeMissing);
  }
  return new Proxy(bridge, {
    get(target, name) {
      const value = target[name];
      if (typeof value !== "function") {
        if (typeof name === "string" && name !== "then") {
          // Reading a method that is not there returns undefined and the call
          // throws one line later, where the name is no longer in hand. Said
          // here, where it still is.
          toastError(STR.callMissing(String(name)));
        }
        return value;
      }
      return (...args) => {
        callBegan();
        try {
          return Promise.resolve(value.apply(target, args)).then(
            (answer) => {
              callEnded();
              return answer;
            },
            (error) => {
              callEnded();
              throw reported(error, STR.callFailed(String(name), errorText(error)));
            },
          );
        } catch (error) {
          callEnded();
          throw reported(error, STR.callFailed(String(name), errorText(error)));
        }
      };
    },
  });
}

// --- saying that something is running --------------------------------------
// Nearly everything this app does happens on a worker thread on the other side
// of the bridge. Without an indicator, restoring, collecting, reading a peer's
// folder or writing a report runs in a silence indistinguishable from a dead
// window.

/** How long a call may take before it is worth mentioning.
 *
 * The threshold is what keeps this honest, and it is why nothing has to be
 * listed: a call that answers in twenty milliseconds must not flash anything,
 * and a call still unanswered after this long is worth announcing. Anything
 * slow announces itself by being slow — including the calls nobody thought to
 * instrument.
 */
const SLOW_CALL_MS = 400;

let callsOutstanding = 0;
let slowCallTimer = null;
let callsAreSlow = false;

function callBegan() {
  callsOutstanding += 1;
  if (slowCallTimer !== null) return;
  slowCallTimer = setTimeout(() => {
    slowCallTimer = null;
    callsAreSlow = callsOutstanding > 0;
    renderActivity();
  }, SLOW_CALL_MS);
}

function callEnded() {
  callsOutstanding = Math.max(0, callsOutstanding - 1);
  if (callsOutstanding > 0) return;
  if (slowCallTimer !== null) clearTimeout(slowCallTimer);
  slowCallTimer = null;
  callsAreSlow = false;
  renderActivity();
}

/** The flags that mean this window is waiting on a job of its own.
 *
 * One list, because two places ask this — the poll loop and the activity
 * indicator — and two lists can disagree. A job whose flag is in neither never
 * has its events read: it emits progress and a finished notice into a queue
 * nobody is draining, and the screen stays empty until the tab is opened again.
 *
 * Adding a job means adding its flag here, once, and both consumers follow.
 */
const RUNNING_FLAGS = [
  "scanning",
  "searching",
  "qualityRunning",
  "collecting",
  "adopting",
  "opening",
  "restoring",
  "measuringHarmonics",
];

function anythingRunning() {
  return RUNNING_FLAGS.some((flag) => Boolean(state[flag]));
}

/** Whether it is worth asking for events, which is not the same question.
 *
 * A download that has not landed yet is answered by a collect this window never
 * started: it runs on the sleep-watch thread every thirty seconds and emits
 * `collected`. Nothing here is "running" while that happens, so a poll driven
 * by `anythingRunning` alone would already have stopped, and the event would
 * sit in the queue until some other gesture drained it — the album in the
 * library and the Library screen still showing what it showed before.
 *
 * Deliberately separate from `anythingRunning`, which drives the activity
 * indicator: waiting for a download is not something to put a spinner on, it
 * is only a reason to keep listening.
 */
function worthPolling() {
  return anythingRunning() || Number(state.awaiting) > 0;
}

/** How long to wait before asking again.
 *
 * A job in flight reports progress and deserves the fast beat. Waiting on a
 * download that the backend checks every thirty seconds does not: three polls a
 * second for an hour of downloading is thousands of crossings to learn nothing.
 */
function pollDelay() {
  return anythingRunning() ? 300 : 2000;
}

/** Start listening if it is worth it and nobody already is.
 *
 * The poll re-arms itself at the end of each pass, so the only thing missing
 * was a way to *begin* one when a download is queued while the window is idle.
 * Guarded by a flag rather than by hope: two calls a second into this without
 * one would stack a second timer chain onto the first, and the beats would
 * multiply every time a refresh happened.
 */
function ensurePolling() {
  if (state.polling || !worthPolling()) return;
  state.polling = true;
  setTimeout(pollEvents, pollDelay());
}

/** Show or hide the one indicator that answers "is anything happening?".
 *
 * Two different things count as running and neither implies the other: a call
 * this window is waiting on, and a job the API started on a thread of its own —
 * which owes this window nothing until it emits an event, so no call is
 * outstanding while a long scan is running.
 */
function renderActivity() {
  const node = $("activity");
  if (!node) return;
  // Named rather than generic wherever the window knows the name: "Working…"
  // says only that waiting is the right thing to do, and "Reading your library…"
  // says that and what for.
  // `collecting` is deliberately absent. Collecting is housekeeping this app
  // does to itself on every refresh, and the waiting list keeps any download
  // that has not finished — so with one transfer queued, every refresh would
  // start a collect and the indicator would never go out. It also fails the one
  // question this indicator answers: nobody asked for it or is waiting on it.
  const named =
    (state.scanning && STR.scanning) ||
    (state.restoring && STR.restoringLibrary) ||
    (state.qualityRunning && STR.busyMeasuring) ||
    (state.measuringHarmonics && STR.busyMeasuringKeys) ||
    (state.adopting && STR.adopting) ||
    (state.opening && STR.opening) ||
    (state.searching && STR.busySearching) ||
    null;
  node.hidden = !(named || callsAreSlow);
  $("activity-label").textContent = named || STR.busy;
}

/** Run something while the button that started it says it is running.
 *
 * A disabled button says "not now". A disabled button with a turning ring says
 * "this is the thing you are waiting for", which is a different sentence and the
 * one that was missing. The previous disabled state is restored rather than
 * assumed: `Scan` is disabled again the moment its run begins, and `Measure this
 * library` stays disabled until the survey ends.
 */
async function withSpinner(button, run) {
  const was = button.disabled;
  button.disabled = true;
  button.classList.add("is-working");
  try {
    return await run();
  } finally {
    button.classList.remove("is-working");
    button.disabled = was;
  }
}

/** Whether the bridge is not merely present but actually carries its methods.
 *
 * `window.pywebview.api` is created as an empty object, and filled later.
 * pywebview injects three scripts in order: one that declares
 * `window.pywebview = {api: {}, …}`, one that runs this window's own code, and
 * a last one that calls `_createApi(...)` and then fires `pywebviewready`. So
 * between the first and the last there is a real window in which the bridge
 * exists, is perfectly truthy, and has not one method on it.
 *
 * Testing the object for truth therefore proves nothing: when the window loses
 * that race every call in the opening refresh is `undefined`, the first of them
 * throws before anything has been drawn, and the Library and the History come
 * up empty against a database that has rows in it — which reads as data that
 * was never saved.
 */
function bridgeIsReady() {
  return Boolean(window.pywebview?.api && Object.keys(window.pywebview.api).length);
}

// How long the bridge may take before the window says it is alone.
// Not a measurement. The event arrives in milliseconds on a launch that works,
// and this is a ceiling set far above any plausible slow one, because the cost
// of it being too low is a false alarm on somebody's first run.
const BRIDGE_PATIENCE_MS = 10_000;

function whenBridgeReady(callback) {
  // Both orders are covered: either the last script has already run — in which
  // case the methods are there and the event is long gone — or it has not, and
  // the event is still coming. There is no gap between the check and the
  // listener, because nothing else runs between them.
  if (bridgeIsReady()) {
    callback();
    return;
  }
  // A third case, which is neither order: the event never comes. Then this
  // window would wait for ever, drawing the empty shelf it starts with, and say
  // nothing — no toast, no band, nothing in the log, because nothing threw. A
  // library that could not be reached would look exactly like a library that
  // is empty, so a timer says so.
  const alone = window.setTimeout(sayTheWindowIsAlone, BRIDGE_PATIENCE_MS);
  window.addEventListener(
    "pywebviewready",
    () => {
      window.clearTimeout(alone);
      callback();
    },
    { once: true },
  );
}

/** Say, in the band, that the window and the application never met.
 *
 * Written into the band's own elements rather than through a toast: a toast
 * goes away, and this is a state rather than an event — nothing is going to
 * arrive later and make it untrue. The band's ✕ is wired by the inline script
 * in `index.html`, which is deliberately independent of this module, so it
 * closes without anything here.
 */
function sayTheWindowIsAlone() {
  if (bridgeIsReady()) return;
  const band = $("window-failure");
  const text = $("window-failure-text");
  if (!band || !text) return;
  text.textContent = STR.windowNeverMetTheApplication;
  band.hidden = false;
}

// --- static strings -------------------------------------------------------

function applyStrings() {
  for (const node of document.querySelectorAll("[data-str]")) {
    const value = STR[node.dataset.str];
    if (typeof value === "string") node.textContent = value;
  }
  // A control whose label is its icon still needs words for the tooltip.
  for (const node of document.querySelectorAll("[data-str-title]")) {
    const value = STR[node.dataset.strTitle];
    if (typeof value === "string") {
      // An SVG has no `title` property and no `title` attribute: setting either
      // is silently nothing, and a tooltip is a `<title>` child element. Without
      // this branch an SVG control named through `data-str-title` has no tooltip
      // at all, which looks exactly like a string that was never written.
      if (node.namespaceURI === "http://www.w3.org/2000/svg") {
        let label = node.querySelector(":scope > title");
        if (!label) {
          label = document.createElementNS(node.namespaceURI, "title");
          node.prepend(label);
        }
        label.textContent = value;
      } else {
        node.title = value;
      }
      node.setAttribute("aria-label", value);
    }
  }
  $("search-artist").placeholder = STR.searchAgainArtist;
  $("search-album").placeholder = STR.searchAgainAlbum;
  $("library-search").placeholder = STR.searchAlbums;
  // The Spotify bridge has no field of its own — it is these two. A gesture
  // nothing on screen invites is a gesture nobody makes, so the placeholders
  // name every source each field accepts.
  $("acquire-query").placeholder = STR.acquireQueryPlaceholder;
  $("paste-link-url").placeholder = STR.pasteLinkPlaceholder;
}

// --- grouping and filters ---------------------------------------------------

function groupOf(album) {
  // Never automatic: applying writes to a folder, and this album's folder is
  // not there. Counted as automatic, it would be part of the batch button's
  // number while its own card shows no AUTO chip.
  //
  // Tested first, so the group and the badge answer as one. `badgeFor` tests it
  // first as well, and the two functions have to move together: a card wearing
  // `not where it was` inside the `organized` filter is the shelf disagreeing
  // with itself about which question it is answering.
  if (album.folder_missing) return "review";
  if (album.rejected) return "rejected";
  // One word for one fact: this app organized this album. Whether that happened
  // in this session or an earlier one is about a run, not an album.
  if (album.organized) return "organized";
  // Nothing has been compared for this album, so there is no answer to review.
  // Grouping it with `review` would claim this app had looked and wanted a
  // second opinion, when it has not looked at all.
  if (album.looked_at === false) return "unlooked";
  if (album.decision === "unidentified") return "nomatch";
  if (
    album.decision === "automatic" && album.applicable && !album.held
  ) {
    return "automatic";
  }
  return "review";
}

function matchesState(album) {
  return state.filter === "all" || groupOf(album) === state.filter;
}

/** Whether this album answers the rating the shelf is filtered to.
 *
 * `n` means *n stars or better*, which is the only reading that makes a filter
 * of a scale: asked for four, a five is not something to hide. `none` is the
 * complement: the albums that have not been rated yet.
 */
function matchesRating(album) {
  if (state.rating === "all") return true;
  if (state.rating === "none") return !album.rating;
  return Number(album.rating || 0) >= Number(state.rating);
}

/** A name as it is compared: accent-blind and case-blind, like the matcher's own.
 *
 * `André` typed without the accent has to find `André`, because the accent is
 * a spelling and not a different name — the same reasoning `fold_accents` is
 * built on in Python.
 */
function folded(text) {
  return (text || "")
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/** Whether this album answers to the text in the search box.
 *
 * Three things are searched because an album can be named by any of them: its
 * title, its artist, and the folder it lives in — the last because an album
 * this app has not identified yet has no title or artist of its own, and its
 * folder name is all there is to look for.
 *
 * Every word has to be found, in any of the three and in any order, so
 * `andre riviere` reaches `André Valmont — La Rivière` without requiring
 * anyone to remember which field holds which half.
 */
function matchesSearch(album) {
  const wanted = folded(state.search).split(/\s+/).filter(Boolean);
  if (!wanted.length) return true;
  const haystack = folded(
    [album.title, album.artist, album.folder, album.year].filter(Boolean).join(" "),
  );
  return wanted.every((word) => haystack.includes(word));
}

/** The three controls cross: status says what happened, the rating how it was
 * judged, and the search which album is being looked for. */
function visibleAlbums() {
  const shown = state.albums.filter(
    (album) => matchesState(album) && matchesRating(album) && matchesSearch(album),
  );
  return sortAlbums(shown);
}

/** Order the shelf, by the default rule and then by whichever lens is chosen.
 *
 * The rule is recently added, and it is the default: the last record in is the
 * first one seen. `unit_id` *is* that order — a row keeps its id for life, so
 * an album re-read, renamed or organised never moves — and it is on every card
 * already, including the ones the shelf draws straight from the database
 * before anything has been read off the disk.
 *
 * An album that has just been pointed at floats above all of it, in the order
 * of the gestures. Dropping a record already on the shelf is exactly the case
 * where a sort by date would bury it: the row is old, the gesture is not. It
 * lasts the session, because it is about the session rather than the album.
 *
 * An unrated record is never ordered by a rating it was not given. Both rating
 * lenses put the unrated after everything rated, so reversing the scale
 * reverses the rated albums and nothing else.
 *
 * Ties fall back to the rule, so no lens ever leaves two albums in an order
 * that changes between two draws of the same shelf.
 */
function sortAlbums(albums) {
  const recent = (a, b) => b.unit_id - a.unit_id;
  const text = (value) => (value || "").toLocaleLowerCase("pt-BR");
  // Named `ratingOf` and not `rating`, because `state.rating` is already a word
  // in this file and it means the *filter* — `all`, `none` or a floor.
  const ratingOf = (album) => Number(album.rating || 0);
  // Not rated is not a nought on the scale: it takes no part in the order and
  // follows every rated album in *both* directions. The `year` lens below can
  // say "absent last" with a single `-Infinity` because it only ever runs one
  // way; a lens offered both ways has to say it in a clause of its own, or the
  // ascending half opens on the albums the rating has nothing to say about.
  const byRating = (bestFirst) => (a, b) => {
    const one = ratingOf(a);
    const other = ratingOf(b);
    if (!one || !other) {
      // Two unrated albums are not a tie in the rating — they are two albums
      // the rating cannot order at all, so the shelf's own rule answers, as it
      // does after every other lens here.
      if (one === other) return recent(a, b);
      return one ? -1 : 1;
    }
    return (bestFirst ? other - one : one - other) || recent(a, b);
  };
  const by = {
    recent,
    oldest: (a, b) => a.unit_id - b.unit_id,
    artist: (a, b) => text(a.artist).localeCompare(text(b.artist)) || recent(a, b),
    album: (a, b) => text(a.title || a.folder).localeCompare(text(b.title || b.folder)) || recent(a, b),
    // No year sorts last rather than as zero, which would put every album the
    // sources could not date at the top of a list about years.
    year: (a, b) => (b.year || -Infinity) - (a.year || -Infinity) || recent(a, b),
    rating_best: byRating(true),
    rating_worst: byRating(false),
  };
  const chosen = by[state.sort] || recent;
  // The gesture floats an album out of the order it was made to rescue, and out
  // of no other. Floating exists for the case a sort by *date* buries — the row
  // is old, the gesture is not. Under any lens chosen explicitly it would put
  // an unrated album above the best-rated ones on a shelf sorted by rating: a
  // default that is usually right must not be what refuses an explicit request.
  const floats = state.sort === "recent" || !by[state.sort];
  const pointed = (album) => (floats ? state.pointedAt.indexOf(album.unit_id) : -1);
  return [...albums].sort((a, b) => {
    const first = pointed(a);
    const second = pointed(b);
    if (first !== -1 || second !== -1) {
      // Later gesture, higher up. Anything untouched sits below both.
      //
      // The index is the age of the gesture, not its rank: the newest lands at
      // 0, so the one that comes first is the one with the *smaller* index.
      // Subtracting the other way would put the oldest drop of the session on
      // top and bury each new one under it.
      if (first === -1) return 1;
      if (second === -1) return -1;
      return first - second;
    }
    return chosen(a, b);
  });
}

/** Whether this album is on screen waiting to be looked up, and not being. */
function isWaiting(album) {
  return album.looked_at === false && !state.identifying.has(album.unit_id);
}

function badgeFor(album) {
  // Above every other badge on purpose: an album that is not where it was
  // recorded cannot be scanned, planned or applied, so any word about its plan
  // is a word about a folder that is not there.
  //
  // That includes `rejected` and `organized`: tested first, they would leave a
  // finished album whose folder has gone wearing `✓ organized`,
  // indistinguishable from a whole one. Organized earlier and gone now are
  // both true; only one of them can be acted on.
  if (album.folder_missing) return ["badge-gone", STR.badgeFolderGone, STR.tipFolderGone];
  if (album.rejected) return ["badge-rejected", STR.badgeRejected];
  if (album.organized) return ["badge-organized", STR.badgeOrganized];
  // While a run is under way, an album it has not reported on yet says so. The
  // progress bar counts the run; this says which records it is about.
  if (state.identifying.has(album.unit_id)) {
    return ["badge-working", STR.cardIdentifying];
  }
  if (album.looked_at === false) return ["badge-unlooked", STR.badgeUnlooked];
  if (album.held) return ["badge-review", STR.badgeHeld];
  // The album its own tags named, said *instead of* `review` rather than beside
  // it: the card's foot is one line of items that refuse to wrap, so an extra
  // badge would be clipped by the card's own overflow.
  //
  // An arrangement is not an identification: nothing was asked of any
  // catalogue, so from the shelf this album is exactly what it was before —
  // waiting to be scanned. Where the words came from is said in the dialog,
  // under the header, and in this badge's own tooltip.
  //
  // And only for the album no catalogue has been asked about. `not scanned` is
  // a claim about the album's history, and `source === "tags"` is a fact about
  // the proposal in front of it. An album that was scanned and then arranged
  // by its own tags would otherwise wear `not scanned` while `groupOf` files it
  // under `review` — the badge and the group disagreeing about one album. An
  // album a catalogue has answered for falls through to `review`.
  if (album.source === "tags" && !album.catalogue_asked) {
    return ["badge-unlooked", STR.badgeUnlooked, STR.tipFromTags];
  }
  if (album.decision === "unidentified") return ["badge-nomatch", STR.badgeNoMatch];
  if (album.decision === "automatic" && album.applicable) {
    return ["badge-automatic", STR.badgeAutomatic];
  }
  if (album.decision === "automatic") return ["badge-blocked", STR.badgeBlocked];
  return ["badge-review", STR.badgeReview];
}

// Named rather than derived from the key: `nomatch` capitalises to
// `groupNomatch` and the string is `groupNoMatch`, so a derived lookup would
// read "nomatch (0)". A lookup built by string surgery fails silently and
// shows the identifier.
const STATUS_GROUPS = [
  ["all", "groupAll"],
  ["unlooked", "groupUnlooked"],
  ["automatic", "groupAutomatic"],
  ["review", "groupReview"],
  ["nomatch", "groupNoMatch"],
  ["rejected", "groupRejected"],
  ["organized", "groupOrganized"],
];

/** The rating filter's own entries, in the same menu.
 *
 * A number means *that many stars and better*. `none` selects by absence rather
 * than by a number, and comes last because it is where the scale runs out
 * rather than a rung on it.
 *
 * `all` comes first, by the same rule the status list follows: both lists
 * offer the answer that means *no filter here*, so each has a visible way back.
 */
const RATING_FILTERS = ["all", "5", "4", "3", "2", "1", "none"];

/** The label for one rating filter, read from the strings like every other. */
function ratingFilterName(value) {
  if (value === "all") return STR.ratingAny;
  return value === "none" ? STR.ratingNone : STR.ratingAndUp(Number(value));
}

function statusName(group) {
  const found = STATUS_GROUPS.find(([key]) => key === group);
  return found ? STR[found[1]] : group;
}

function renderSummary() {
  // Each control counts what the *other* one's selection leaves, so a number
  // describes what clicking would show rather than a total that disagrees with
  // the screen.
  // One shelf, counted once. Two shelves chosen before anything is counted
  // would let a selection sitting on the empty one draw a library that looks
  // like it has nothing in it.
  const counts = { all: state.albums.length };
  for (const [group] of STATUS_GROUPS.slice(1)) counts[group] = 0;
  for (const album of state.albums) counts[groupOf(album)] += 1;

  // The bar itself stays: it carries the gestures that put albums on an empty
  // shelf. What hides is the group that asks about albums on screen.
  $("library-questions").hidden = state.albums.length === 0;

  // The status reads as a sentence on the button, so the one that is selected
  // is legible without opening anything.
  $("status-menu").textContent = STR.statusLabel(
    statusName(state.filter),
    counts[state.filter] ?? 0,
  );
  state.statusCounts = counts;

  renderBatchButton();
}

/** Say what `Apply` would write, on the button itself.
 *
 * This function owns that button, the way `renderMarkCount` owns Scan's. The
 * count is *what is marked*, so every way the marks move has to redraw this:
 * ticking a card that called `renderMarkCount` alone would leave the button
 * disabled over a marked AUTO album. Kept apart from `renderSummary` rather
 * than called from inside `renderMarkCount`, because the two draw each other's
 * buttons and a cycle between them is one edit away.
 */
function renderBatchButton() {
  const counts = state.statusCounts || {};
  const button = $("apply-automatic");
  // What the click would actually write, which is what is marked. Counting
  // every automatic album in the library would offer to organize records that
  // were not chosen, with no ticks on the shelf to explain the number.
  const marked = new Set(markedOnScreen());
  const ready = state.albums.filter(
    (album) => marked.has(album.unit_id) && groupOf(album) === "automatic",
  ).length;
  // Both halves of this button read the same marks. A cover count taken over
  // the whole library would offer to write a picture over a shelf with nothing
  // ticked, into an album that was never pointed at.
  const coversPending = state.albums.filter(
    (album) => marked.has(album.unit_id) && album.cover_pending && !album.applied,
  ).length;
  const offers = ready > 0 || coversPending > 0;
  // Away entirely until there is something to accept. A disabled control in
  // the most crowded bar in the window says nothing can be done with it for as
  // long as the shelf is full. The bench's `Analyze` and `Identify` follow the
  // same pattern: offered when they are about something, absent when they are
  // not.
  button.hidden = !offers;
  button.disabled = !offers;
  // The accent is an invitation, so it is worn only when there is something to
  // accept. A filled amber button that does nothing is the loudest thing on the
  // screen making the emptiest promise.
  button.classList.toggle("button-primary", offers);
  button.textContent = ready > 0 ? STR.applyAutomatic(ready) : STR.applyCovers(coversPending);
  button.title = STR.tipApplyAutomatic;
}

// --- covers, lazily ---------------------------------------------------------
// Covers cross the same serialized bridge as everything else; requesting all
// of them at once would bury the "finished" event behind them. Only a card
// that is actually on screen asks for its picture.

const coverObserver = new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      coverObserver.unobserve(entry.target);
      requestCover(Number(entry.target.dataset.unitId));
    }
  },
  { rootMargin: "200px" },
);

async function requestCover(unitId) {
  if (state.covers.has(unitId)) return;
  try {
    const uri = await api().cover(unitId);
    if (!uri) {
      state.coverless.add(unitId);
      return;
    }
    state.coverless.delete(unitId);
    state.covers.set(unitId, uri);
    // The node that asked for this picture may be gone by now: every redraw
    // builds new cards, and a scan redraws constantly, so a cover arriving
    // mid-scan may belong to a card already discarded. Whichever node carries
    // this album now is the one to fill; the cache above means a later redraw
    // draws it too.
    for (const node of document.querySelectorAll(`[data-unit-id="${unitId}"]`)) {
      fillCover(node, uri);
    }
  } catch {
    /* a missing cover is a normal outcome */
  }
}

/** Put a picture where a card or a row is still showing its placeholder. */
function fillCover(node, uri) {
  const placeholder = node.querySelector(".card-cover-placeholder");
  if (!placeholder) return;
  const image = document.createElement("img");
  // A row's thumbnail and a card's cover are the same picture at two sizes,
  // so the class comes from whatever the placeholder already was.
  image.className = placeholder.classList.contains("list-cover") ? "list-cover" : "card-cover";
  image.src = uri;
  image.alt = "";
  placeholder.replaceWith(image);
}

// --- rendering ------------------------------------------------------------

/** What can be done to one album from the Library, without opening it.
 *
 * Everything here is either a decision that can already be made inside the
 * dialog or a way out to the disk. Nothing new is invented: a menu that offers
 * a gesture with no home elsewhere is a gesture nobody will find twice.
 */
function albumActions(album) {
  const items = [{ label: STR.menuRevealFolder, run: () => openAlbumFolder(album.unit_id) }];
  // First of all on the card that needs it, because nothing else on this menu
  // will work until it is answered — and the dialog cannot be opened for such
  // an album at all.
  if (album.folder_missing) {
    items.push({ label: STR.relocateThis, run: () => relocateFromMenu(album.unit_id) });
  }
  // `applicable` alone is the full plan; an album offering a partial one needs
  // the second flag or it never gets this entry. It asks the same question the
  // footer's button asks, and the two must keep asking it in the same words.
  if ((album.applicable || album.partial_applicable) && !album.written) {
    items.push({ label: STR.approve, run: () => approveFromMenu(album.unit_id) });
  }
  if (album.applied || album.organized) {
    // An album that is done is not finished with: a small change to a name
    // this application chose must not require a rescan.
    items.push({
      label: STR.planAnyway,
      title: STR.tipPlanAnyway,
      run: () => replanFromMenu(album.unit_id),
    });
  } else {
    // The other half, so that "plan this again" is in the menu whatever state
    // the album is in. For an album that is not organized the gesture with that
    // meaning is `Scan this album`: the two cover every state between them and
    // overlap in none, exactly as they do in the dialog's own footer.
    // `again` for the album that has been looked at, as the footer says it.
    items.push({
      label: album.looked_at === false ? STR.scanThisAlbum : STR.scanThisAlbumAgain,
      run: () => scanOneFromMenu(album.unit_id),
    });
  }
  if (!album.rejected && !album.applied) {
    items.push({ label: STR.reject, run: () => rejectFromMenu(album.unit_id) });
  }
  if (album.url) {
    items.push({
      label: STR.viewSource(album.source),
      run: () => reportIfRefused(api().open_url(album.url)),
    });
  }
  // The bench, from the album on the card. Its own folder goes on, not the
  // shelf around it, so sending one album does not drag in every album beside
  // it.
  items.push({ label: STR.menuSendToBench, run: () => sendToBench(album.unit_id) });
  // Last, and worded as what it is: the row goes, the album does not.
  items.push({ label: STR.menuForget, run: () => forgetAlbum(album.unit_id) });
  return items;
}

/** Put this album's folder on the quality bench, and go there.
 *
 * The rows appear on another tab, and a gesture whose result is on a screen
 * that is not showing reads as a gesture that did nothing. Every way of sending
 * an album lands on the bench the same way.
 */
async function sendToBench(unitId) {
  const response = await api().send_to_bench(unitId);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  const album = state.albums.find((entry) => entry.unit_id === unitId);
  await landOnBench(response.root, album ? album.folder : "");
}

function albumMenu(node, album) {
  node.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    openAcquireMenu(event, albumActions(album));
  });
}

/** The box that decides whether the next Scan looks this album up.
 *
 * Always visible, unlike the ✕ beside it: what is marked has to be readable
 * without moving the pointer over every card, because the whole point is to
 * choose several before pressing anything. It sits in the card's foot beside
 * the badge — the sleeve is how a record is recognised, and a control on top of
 * it is in the way.
 */
function markBox(album) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "card-mark";
  box.checked = state.marked.has(album.unit_id);
  box.title = STR.tipMark;
  const stop = (event) => event.stopPropagation();
  // The card underneath is a button that opens the album, and the row is
  // clickable for the same reason. Marking is not opening.
  box.addEventListener("click", stop);
  box.addEventListener("change", (event) => {
    stop(event);
    setMarked(album.unit_id, box.checked);
  });
  return box;
}

function setMarked(unitId, wanted) {
  if (wanted) state.marked.add(unitId);
  else state.marked.delete(unitId);
  renderMarkCount();
  // The marks drive two buttons, and this is the one gesture that moves them
  // without anything else redrawing afterwards.
  renderBatchButton();
}

/** The marks that name an album the window is actually holding.
 *
 * `state.marked` is a set of ids and nothing prunes it: an album that was marked
 * and then forgotten, cleared, or dropped from the shelf leaves its id behind.
 * A button and a gesture that both counted the raw set would agree with each
 * other and could both be wrong about the library — and the server, which looks
 * the ids up, is the one that finds out.
 *
 * Derived rather than stored, so the label, the gesture and the answer cannot
 * drift apart.
 */
function markedOnScreen() {
  const here = new Set((state.albums || []).map((album) => album.unit_id));
  return [...state.marked].filter((unitId) => here.has(unitId));
}

/** The marks a scan would actually act on.
 *
 * The mark is on every card, including the organized ones, and a run hands an
 * organized album no plan. Counting those would light `Scan 2 albums` over a
 * gesture guaranteed to change nothing.
 *
 * Written as the complement — everything the run does **not** promise to leave
 * exactly as it is — so a state nobody has met yet is offered to the scan rather
 * than silently withheld from it. `organized` is the same word the run counts
 * `left_alone` by, and the two have to stay the same word.
 */
function scannableMarks() {
  const shelf = new Map((state.albums || []).map((album) => [album.unit_id, album]));
  return markedOnScreen().filter((unitId) => !shelf.get(unitId)?.organized);
}

/** Wire one search box in a screen's bar so its field is a mark until wanted.
 *
 * Every bar that holds a search gets the same behaviour. The dialog's picker
 * keeps its open field: there the search is what the screen is about.
 *
 * It stays open while it holds words — a question that has been asked must
 * remain visible, or the shelf is filtered by something nobody can see.
 */
function wireBarSearch(fieldId) {
  const field = $(fieldId);
  const box = field.closest(".library-search");
  const mark = box.querySelector(".search-open");
  mark.addEventListener("click", () => {
    box.classList.add("is-open");
    field.focus();
  });
  field.addEventListener("blur", () => {
    if (!field.value) box.classList.remove("is-open");
  });
}

/** Put a search box back in step with the search it is about.
 *
 * The ✕ appears only when there is something to clear, the box itself is
 * written to as well as read from — clearing with Escape has to move the field,
 * not only the state behind it — and the field stays open while it holds words.
 */
function renderBarSearch(fieldId, value) {
  const field = $(fieldId);
  if (field.value !== value) field.value = value;
  const box = field.closest(".library-search");
  if (value) box.classList.add("is-open");
  else if (document.activeElement !== field) box.classList.remove("is-open");
}

function renderSearchBox() {
  renderBarSearch("library-search", state.search);
  $("library-search-clear").hidden = !state.search;
}

function clearLibrarySearch() {
  state.search = "";
  renderSearchBox();
  renderGrid();
}

/** Say how many albums the Scan button is about, on the button itself.
 *
 * This function owns the button — its words and whether it can be pressed — so
 * everything that moves the marks has to call it: a scan finishing, a restore,
 * an apply, a forgotten album and a dropped folder all change the set. A label
 * left standing would say `Scan 1 marked` over an empty set and refuse the
 * click that follows.
 */
function renderMarkCount() {
  const button = $("scan");
  // What it would scan, not what is ticked: an organized album is marked like
  // any other and is left exactly as it is, so counting it here would make this
  // button promise work it is guaranteed not to do.
  const count = scannableMarks().length;
  button.textContent = count ? STR.scanMarked(count) : STR.scan;
  button.disabled = state.scanning || (!count && !state.root);
  // The marks drive two gestures: the scan asks the world, the arrangement asks
  // the files. The arrangement lives in the bar's ⋯ — its entry is built when
  // the menu opens, so there is no second label here to fall out of step.
}

/** Show the chosen folder while choosing it is still an unfinished instruction.
 *
 * The path answers "what is Scan about to read?", so once the scan has read it
 * the answer is history: left beside a Scan button, it reads as a scan still
 * waiting to happen. It is a Library control like the two beside it, so it is
 * also hidden on every other tab.
 */
function renderRootPath() {
  const node = $("root-path");
  const show = Boolean(state.root) && state.rootShown && state.tab === "library";
  node.hidden = !show;
  node.textContent = show ? state.root : "";
  node.title = show ? state.root : "";
}

/** The small ✕ that takes one album off this screen and nothing off the disk. */
function forgetButton(album) {
  const close = document.createElement("button");
  close.type = "button";
  close.className = "card-forget";
  close.textContent = "✕";
  close.title = STR.tipForget;
  close.addEventListener("click", (event) => {
    // The card underneath is a button that opens the album; this one is not a
    // way of opening it.
    event.stopPropagation();
    forgetAlbum(album.unit_id);
  });
  return close;
}

async function forgetAlbum(unitId) {
  state.marked.delete(unitId);
  const result = await api().forget_album(unitId);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  state.albums = state.albums.filter((album) => album.unit_id !== unitId);
  renderGrid();
  toast(STR.forgotten);
}

async function openAlbumFolder(unitId) {
  const result = await api().open_album(unitId);
  if (!result.ok) toastError(result.error);
}

// Each of these acts on `state.openAlbum`, so each stops when the album it was
// asked about is not the one that got opened.
async function approveFromMenu(unitId) {
  if (!(await openAlbum(unitId))) return;
  approveAlbum();
}

async function rejectFromMenu(unitId) {
  if (!(await openAlbum(unitId))) return;
  await rejectAlbum();
}

async function replanFromMenu(unitId) {
  if (!(await openAlbum(unitId))) return;
  await planAnyway();
}

/** Point a marked card at the folder it lives in now, without opening it first.
 *
 * The album dialog **cannot open** for the one album this exists for: an album
 * whose folder does not answer is never read back into memory, so clicking its
 * card answers "Unknown album". Without this entry the card would be drawn,
 * marked `not where it was`, and every way to repair it would refuse.
 */
async function relocateFromMenu(unitId) {
  const folder = await api().choose_folder();
  if (!folder) return;
  const response = await api().relocate_album(unitId, folder);
  if (!response.ok) {
    if (response.second_copy) await offerToLetSecondCopyGo(response);
    else toastError(response.error);
    return;
  }
  toast(STR.relocated(response.album.folder));
  await refresh();
}

/** Look one album up again, from its own card, without opening the dialog first.
 *
 * The dialog's button reads `currentTarget` to know where to put the spinner,
 * and there is no button here — the menu has already closed by the time the
 * work starts. So the card asks directly and the progress bar answers, which is
 * where a scan reports anyway.
 */
async function scanOneFromMenu(unitId) {
  const response = await api().scan_selected([unitId]);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  scanBegan([unitId]);
}

/** The whole of an album's name, for the hover of a card or a row.
 *
 * Both views cut their text off with an ellipsis — a card is a fixed width and
 * a column is a share of one — so long names are exactly the ones that cannot
 * be read where they are drawn.
 *
 * The folder goes on the second line, because it is the one thing the card does
 * not say out loud and the only way back to the file manager from a card. When
 * the credit and the title together already *are* the folder name, which is
 * what an organized album looks like, it is not repeated.
 */
function fullNameFor(album) {
  const named = [album.artist, album.title].filter(Boolean).join(" — ");
  if (!named) return album.folder;
  // An organized album's folder *is* its credit and title, plus the year and
  // the format — so the two lines would say the same thing twice:
  // `Artist — Title` over `Artist - Title (1987) [FLAC]`. Compared on letters and digits alone, because the difference
  // between them is punctuation: this app joins a credit with an em dash and
  // names a folder with a hyphen.
  //
  // Both lines are kept when the folder does *not* already contain the name,
  // which is an album planned and not yet applied — there the first line is the
  // name it would get and the second is the name it still has, and that is two
  // facts rather than one said twice.
  const saidAlready = [album.artist, album.title]
    .filter(Boolean)
    .every((piece) => sameWords(album.folder, piece));
  return saidAlready ? album.folder : `${named}\n${album.folder}`;
}

/** Whether one name already says everything another one says.
 *
 * Each piece is asked for separately rather than the joined line, because a
 * folder may put them in its own order or repeat one of them.
 */
function sameWords(whole, part) {
  const plain = (text) =>
    String(text || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "");
  const inside = plain(part);
  return Boolean(inside) && plain(whole).includes(inside);
}

function card(album) {
  const button = document.createElement("button");
  // Dimmed while it is waiting to be looked up, and not while it is being looked
  // up: a card that is working is the liveliest thing on the shelf.
  button.className = isWaiting(album) ? "card is-unlooked" : "card";
  // On the card itself rather than on the line that is cut off, so hovering
  // anywhere on the sleeve answers. An inner element with its own `title` wins
  // over this one, which is why the name line below carries none.
  button.title = fullNameFor(album);
  button.dataset.unitId = album.unit_id;
  button.addEventListener("click", () => openAlbum(album.unit_id));
  albumMenu(button, album);
  button.appendChild(forgetButton(album));

  const cover = state.covers.get(album.unit_id);
  if (cover) {
    const image = document.createElement("img");
    image.className = "card-cover";
    image.src = cover;
    image.alt = "";
    button.appendChild(image);
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "card-cover-placeholder";
    placeholder.textContent = "♪";
    button.appendChild(placeholder);
    coverObserver.observe(button);
  }

  // On the sleeve, and only there. The foot has no room: anything added to it
  // wraps it onto a second line. The sleeve's bottom-left corner is empty, the
  // ✕ is in the opposite one, and the card's height does not change.
  //
  // Quiet at rest and shown on hover, which is the ✕'s own rule beside it.
  //
  // The box is the sleeve's, not the card's. Anchored with `bottom` it would
  // measure against `.card`, which is the positioned ancestor, and the stars
  // would land on the mark box and the badge. The sleeve is square and spans
  // the card, so a box of `width: 100%; aspect-ratio: 1` at the top *is* the
  // sleeve, and the strip is pushed to the bottom of it. No number is guessed.
  //
  // Three things make a stray press survivable: the strip is not there until
  // the pointer is, the press stops at the star instead of opening the album,
  // and pressing the star already chosen puts the rating back to nothing.
  //
  // At rest it is the number and one star; the five open under the pointer.
  // Five glyphs take the same width whatever the rating, so a fully rated
  // shelf would wear the same block on every card; `4★` is less than half of
  // it and says the same thing. An unrated album draws nothing at all, so
  // nothing is ever drawn to mean *nothing*.
  const stars = document.createElement("div");
  stars.className = "card-stars";
  if (album.rating) stars.appendChild(briefRating(album.rating));
  stars.appendChild(starStrip(album.rating, (value) => rateFromCard(album, value)));
  button.appendChild(stars);

  const body = document.createElement("div");
  body.className = "card-body";
  const title = document.createElement("div");
  title.className = "card-title";
  title.textContent = album.title || album.folder;
  const sub = document.createElement("div");
  sub.className = "card-sub";
  sub.textContent = [album.artist, album.year].filter(Boolean).join(" · ") || STR.tracks(album.tracks);
  // No third line with the folder on disk: on an organized album it would
  // repeat the two lines above it. Nothing is lost — an album no source has
  // named already shows its folder as the title, and the folder is on the
  // tooltip for the one case where the two differ, which is an album planned
  // and not yet applied.
  const foot = document.createElement("div");
  foot.className = "card-foot";
  const [badgeClass, badgeText, badgeTip] = badgeFor(album);
  const badge = document.createElement("span");
  badge.className = `badge ${badgeClass}`;
  badge.textContent = badgeText;
  if (badgeTip) badge.title = badgeTip;
  // The badge is what the card is for: confidence is already distilled into
  // it — `automatic` means it cleared the threshold — and its exact value
  // stays in the open album, where there is room to read it. The fraction of
  // settled tracks is on the list's column and in the dialog, both under a
  // heading that says what it counts. The shared-tracks mark is not here
  // either: the foot would wrap, and a detail of one album is read in the
  // dialog, under `Also in another album`.
  // Marking is here in the foot, beside the card's other controls, so the
  // sleeve stays a sleeve.
  foot.append(markBox(album), badge);
  body.append(title, sub, foot);
  button.appendChild(body);
  return button;
}

/** Five stars, drawn the same way wherever they are read.
 *
 * Outlines for what is not chosen, never a filled star in a paler colour: a
 * half-lit star would be a third state on a scale that has two, and a mark
 * must not mean two things.
 *
 * No amber and no yellow: amber on the shelf already means *this pairing is
 * offered*, and a rating is not a warning about anything.
 *
 * `onPick` absent makes it a reading rather than a control.
 */
function starStrip(rating, onPick = null) {
  const strip = document.createElement("span");
  strip.className = onPick ? "stars is-pickable" : "stars";
  const chosen = Number(rating || 0);
  for (let value = 1; value <= 5; value += 1) {
    const star = document.createElement(onPick ? "button" : "span");
    star.className = value <= chosen ? "star is-on" : "star";
    star.textContent = value <= chosen ? "★" : "☆";
    if (onPick) {
      star.type = "button";
      star.title = STR.ratingSet(value);
      // Pressing the star already chosen withdraws the rating: five values and
      // a way back out, which is the whole vocabulary a scale of five has. The
      // alternative was a sixth control meaning *never mind*.
      star.addEventListener("click", (event) => {
        // The card underneath is a button that opens the album, exactly as it
        // is under the ✕, and rating a record is not a way of opening it.
        // Harmless in the dialog, where nothing is listening above this.
        event.stopPropagation();
        onPick(value === chosen ? null : value);
      });
    }
    strip.appendChild(star);
  }
  return strip;
}

/** Write one rating and put it in every copy of it this window holds.
 *
 * The album in hand, the shelf's row and the server's column are three copies
 * of one fact, and the card is drawn from the second. One function because
 * there are two ways in — the card and the dialog — and the second one written
 * is where a forgotten copy would live.
 */
async function applyRating(unitId, value) {
  const response = await api().rate_album(unitId, value);
  if (!response.ok) {
    toastError(response.error);
    return false;
  }
  const card = state.albums.find((one) => one.unit_id === unitId);
  if (card) card.rating = value;
  if (state.openAlbum && state.openAlbum.unit_id === unitId) {
    state.openAlbum.rating = value;
  }
  return true;
}

/** Rate from the shelf, which redraws it: the filter may no longer hold this. */
async function rateFromCard(album, value) {
  if (!(await applyRating(album.unit_id, value))) return;
  album.rating = value;
  renderGrid();
}

/** The rating a rated card wears at rest: the number given, and one star.
 *
 * Never drawn for an unrated album — there is no number to say, and a row of
 * empty outlines on a card nothing has been said about is clutter.
 */
function briefRating(rating) {
  const brief = document.createElement("span");
  brief.className = "stars card-rating-brief";
  const count = document.createElement("span");
  count.className = "star is-on card-rating-count";
  count.textContent = String(rating);
  const one = document.createElement("span");
  one.className = "star is-on";
  one.textContent = "★";
  brief.append(count, one);
  return brief;
}

/** Draw the rating row inside the open album, and write what is pressed. */
function renderRating(album) {
  const host = $("album-rating");
  if (!host) return;
  host.replaceChildren(
    starStrip(album.rating, async (value) => {
      if (!(await applyRating(album.unit_id, value))) return;
      album.rating = value;
      renderRating(album);
      renderGrid();
    }),
  );
}

function listRow(album) {
  const row = document.createElement("tr");
  if (isWaiting(album)) row.className = "is-unlooked";
  // The same answer the card gives, on the whole row: the artist and title
  // columns are each a share of the table's width, so they are cut off exactly
  // where the longest names are.
  row.title = fullNameFor(album);
  row.dataset.unitId = album.unit_id;
  row.addEventListener("click", () => openAlbum(album.unit_id));
  albumMenu(row, album);

  const markCell = document.createElement("td");
  markCell.className = "list-mark-cell";
  markCell.appendChild(markBox(album));
  markCell.addEventListener("click", (event) => event.stopPropagation());

  // The same cover the grid shows, at thumbnail size: recognising a record by
  // its sleeve is faster than reading its name, in either view.
  const coverCell = document.createElement("td");
  coverCell.className = "list-cover-cell";
  const cover = state.covers.get(album.unit_id);
  if (cover) {
    const image = document.createElement("img");
    image.className = "list-cover";
    image.src = cover;
    image.alt = "";
    coverCell.appendChild(image);
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "list-cover card-cover-placeholder";
    placeholder.textContent = "♪";
    coverCell.appendChild(placeholder);
    coverObserver.observe(row);
  }

  const artist = document.createElement("td");
  artist.textContent = album.artist || "";
  const title = document.createElement("td");
  title.textContent = album.title || album.folder;
  const year = document.createElement("td");
  year.textContent = album.year || "";
  // Which folder on disk this row is about — the column that lets a result be
  // taken back to the Finder.
  const folder = document.createElement("td");
  folder.className = "list-folder";
  folder.textContent = album.folder;
  const tracks = document.createElement("td");
  tracks.textContent = album.tracks;
  // The list has room for both numbers where the card has room for one, and
  // they never read alike here: one is a column of percentages, the other a
  // column of fractions, each under its own heading.
  const matched = document.createElement("td");
  if (!(album.organized || album.applied)) matched.append(matchBadge(album, { compact: true }));
  const sharedRow = sharedBadge(album);
  if (sharedRow) {
    // Inside a cell rather than on it: a `display` on a `<td>` stops it being a
    // cell and paints the row's border on the wrong box.
    const holder = document.createElement("div");
    holder.className = "list-shared";
    holder.append(sharedRow);
    matched.append(holder);
  }
  const confidence = document.createElement("td");
  // Confidence measures a candidate against the disk, and an arranged album's
  // candidate IS the disk — a number here could only be 100% forever.
  confidence.textContent = album.source === "tags" ? "—" : STR.confidence(album.confidence);
  const badgeCell = document.createElement("td");
  const [badgeClass, badgeText, badgeTip] = badgeFor(album);
  const badge = document.createElement("span");
  badge.className = `badge ${badgeClass}`;
  badge.textContent = badgeText;
  if (badgeTip) badge.title = badgeTip;
  badgeCell.appendChild(badge);
  // The same ✕ the card carries, so the list is not the one view where an
  // album cannot be taken off the screen without the right-click menu.
  const forgetCell = document.createElement("td");
  forgetCell.className = "list-forget-cell";
  forgetCell.appendChild(forgetButton(album));
  row.append(
    markCell, coverCell, artist, title, year, folder, tracks, matched, confidence,
    badgeCell, forgetCell,
  );
  return row;
}

function renderGrid() {
  renderSearchBox();
  const albums = visibleAlbums();
  const grid = $("grid");
  const list = $("list");
  // The view that is not in front is emptied rather than left standing. Its
  // children are unreachable either way, and left standing they are what
  // `replaceAlbum` would find — and what the intersection observer would keep
  // holding, since a node nobody can see never intersects and is never
  // released.
  if (state.view === "grid") {
    grid.replaceChildren(...albums.map(card));
    $("list-body").replaceChildren();
    grid.hidden = false;
    list.hidden = true;
  } else {
    $("list-body").replaceChildren(...albums.map(listRow));
    grid.replaceChildren();
    list.hidden = false;
    grid.hidden = true;
  }
  $("view-grid").classList.toggle("is-active", state.view === "grid");
  $("view-list").classList.toggle("is-active", state.view === "list");
  // While the Library is still being read, the screen says so rather than
  // inviting a scan of a folder that was already scanned — an empty library
  // and a library that has not arrived yet are different things, and showing
  // the wrong one looks like lost data.
  const empty = $("empty-library");
  // A search that finds nothing is not an empty library, and offering to scan a
  // folder is the wrong sentence for it: the albums are there, none of them
  // answers to the search. Three states, not two.
  const searching = state.search.trim().length > 0;
  empty.hidden = (searching ? albums.length > 0 : state.albums.length > 0) || state.scanning;
  empty.textContent = state.restoring
    ? STR.restoringLibrary
    : searching
      ? STR.searchFoundNothing(state.search.trim())
      : STR.emptyLibrary;
  renderSummary();
}

/** Where the albums of the view that is in front live.
 *
 * Albums are added and replaced one at a time: a scan of a large library
 * reports thousands of albums one by one, and redrawing every card for each of
 * them would be quadratic.
 */
function albumContainer() {
  return state.view === "grid" ? $("grid") : $("list-body");
}

function replaceAlbum(album) {
  // Searched inside the shelf rather than across the document. If the view
  // that is not in front kept its children, `#grid` would come first in
  // document order, and in list view every `identified` event would find the
  // *hidden* card and graft a `<tr>` into a `<div>` while the visible row went
  // on saying `not scanned`. History rows carry `data-unit-id` too, so a
  // document-wide search could put a card inside a `<tbody>`.
  const node = albumContainer().querySelector(`[data-unit-id="${album.unit_id}"]`);
  if (!node) {
    appendAlbum(album);
    return;
  }
  node.replaceWith(state.view === "grid" ? card(album) : listRow(album));
  renderSummary();
}

function appendAlbum(album) {
  // Both controls, the same pair `visibleAlbums` crosses. Asking only about
  // the status filter would mean that typing in the search box while a scan
  // runs appends every album the run identifies, search or no search.
  if (matchesState(album) && matchesSearch(album)) {
    albumContainer().appendChild(state.view === "grid" ? card(album) : listRow(album));
  }
  $("empty-library").hidden = true;
  renderSummary();
}

/** How long a finished-action line stays up before it clears itself.
 *
 * A status line is about something that just happened, so it stops being true
 * by simply staying: left up it becomes a claim about now. Long enough to read
 * two lines on the way to something else, and no longer.
 */
const NOTICE_SECONDS = 12;
/** The same, for the panel that also carries `Revert this run`.
 *
 * Longer because it is not only a message. What it offers survives it: a run
 * stays revertible from History's own menu, so clearing the panel takes away
 * the reminder and not the undo.
 */
const RESULT_SECONDS = 45;
let scanDoneTimer = 0;
let applyResultTimer = 0;

/** Give a finished-action line its lifetime, at the moment it becomes true.
 *
 * Armed by the event and never by the renderer: `renderScanDone` runs inside
 * `renderAll`, so re-arming there would restart the clock on every refresh and
 * the line would outlive the session it describes.
 */
function clearScanDoneLater() {
  clearTimeout(scanDoneTimer);
  scanDoneTimer = setTimeout(() => {
    state.scanFinished = false;
    renderScanDone();
  }, NOTICE_SECONDS * 1000);
}

/** Say which metadata sources this run went without — on the run's own line.
 *
 * Three states and no more, the same three the acquisition provider has: the
 * key is missing, the key was refused, the service did not answer. A window
 * that describes one kind of failure in two vocabularies is a window that has
 * to be learned twice.
 *
 * No notice over the shelf: a notice about the whole library that never leaves
 * on its own is louder than the fact. The condition lives in two quiet places
 * instead — the scan line's own clause here, and a per-album note in the
 * dialog of every album whose identification actually went without the source.
 */
function sourceOutage(down) {
  const names = Object.keys(down || {});
  if (!names.length) return;
  state.sourcesDown = down;
  renderScanDone();
}

function renderScanDone() {
  const done = $("scan-done");
  // A Library fact, so it is a Library line: on Find music, Transfers or
  // History it would describe a scan of somewhere else.
  //
  // And a fact about *this* session: derived from the album count it would
  // survive a restart, so a restored Library would open saying a scan had just
  // finished.
  if (
    state.scanning ||
    !state.scanFinished ||
    state.albums.length === 0 ||
    state.tab !== "library"
  ) {
    done.hidden = true;
    return;
  }
  done.hidden = false;
  // The run's own line carries what it was decided *with*. A count of albums
  // identified says nothing about whether a catalogue was even reachable, and
  // that is the difference between an answer and a guess.
  const missing = Object.keys(state.sourcesDown || {});
  // What the run was, and what it was decided without — one sentence. The
  // clause is handed the sentence it belongs to instead of being appended
  // after its full stop. The covers are a separate fact and follow, rather
  // than sitting between a sentence and its own clause.
  //
  // Called, not handed over: `map` passes an index as a second argument to
  // whatever it is given, and a bare `STR.sourceName` also arrives without its
  // receiver.
  const run = state.scanUntouched
    ? STR.scanFinishedLeaving(state.scanIdentified, state.scanUntouched)
    : STR.scanFinished(state.scanIdentified);
  const said = missing.length
    ? STR.scanWithout(run, missing.map((source) => STR.sourceName(source)).join(", "))
    : run;
  $("scan-done-label").textContent =
    said + (state.coversPlanned ? ` ${STR.coversFound(state.coversPlanned)}` : "");
  if (state.reach) {
    const extensions = (state.reach.extensions || []).join(" ");
    let text = STR.reach(state.reach.folders, state.reach.files, extensions);
    if ((state.reach.excluded || []).length > 0) {
      text += STR.reachExcluded(state.reach.excluded.join(", "));
    }
    $("reach-label").textContent = text;
  } else {
    $("reach-label").textContent = "";
  }
}

/** What a History row is about: who it is by and what it is called.
 *
 * A folder name alone answers neither on a long screen of rows — and for a
 * download it is not even a folder name but the path it had on someone else's
 * disk. The folder stays as the tooltip, because that is what takes a row back
 * to the file manager.
 */
function historyName(entry, fallback) {
  const named = [entry.artist, entry.title].filter(Boolean).join(" - ");
  return named || fallback;
}

/** What a right-click on a History row offers.
 *
 * The same rule the Library's menu follows: only gestures with a home elsewhere
 * on the row. Nothing here is new — it is the row's own button, the way out to
 * the disk, and the name, reachable without hunting for a small target at the
 * far right of a wide table.
 */
function historyActions(entry) {
  const items = [];
  if (entry.kind === "download") {
    items.push({ label: STR.menuRevealFolder, run: () => openDownload(entry.download_id) });
    // The way out of a download that is never going to arrive. Written as the
    // complement — anything not yet collected or set aside — so a state added
    // later is offered it too, rather than falling through to no way out.
    if (entry.arrival !== "collected" && entry.arrival !== "cleared") {
      items.push({
        label: STR.menuForgetDownload,
        title: STR.tipMenuForgetDownload,
        run: () => forgetDownload(entry.download_id),
      });
    }
  } else if (entry.unit_id) {
    items.push({ label: STR.menuRevealFolder, run: () => openAlbumFolder(entry.unit_id) });
  }
  if (entry.kind === "plan" && (entry.state === "applied" || entry.state === "failed")) {
    items.push({ label: STR.revert, run: () => revertPlan(entry.plan_id) });
  }
  // The other direction, in the same place. A reverted run stays on this list
  // so that it can be done again, and this entry is the lasting way to do it.
  if (entry.kind === "plan" && entry.state === "reverted") {
    items.push({ label: STR.menuRedo, run: () => redoPlan(entry.plan_id) });
  }
  const name = historyName(entry, entry.folder_path || entry.folder || "");
  if (name) items.push({ label: STR.menuCopyName, run: () => copyName(name) });
  return items;
}

/** Every gesture a run has, in one control, in the row's last column.
 *
 * `Revert` and `Do this run again` are the two directions of one run, so they
 * are offered by one gesture: a ⋯, which is the way in the Library's bar and
 * the album dialog already use, opening the same menu the right-click does.
 * An icon button also keeps the row at the height of every other album table
 * in the window, which a text button does not.
 */
function historyMoreCell(entry) {
  const cell = document.createElement("td");
  const items = historyActions(entry);
  if (!items.length) return cell;
  const more = document.createElement("button");
  more.className = "button button-icon history-more";
  more.textContent = "⋯";
  more.title = STR.tipHistoryMore;
  more.setAttribute("aria-label", STR.tipHistoryMore);
  more.addEventListener("click", (event) => {
    // The row itself opens nothing, but the ⋯ must not be read as a click on
    // whatever the row may learn to do later.
    event.stopPropagation();
    openAcquireMenu(event, historyActions(entry));
  });
  cell.appendChild(more);
  return cell;
}

function historyMenu(row, entry) {
  // The row last pointed at, one at a time, so the screen says which run a
  // menu belongs to.
  row.dataset.historyKey = historyKey(entry);
  if (state.historyCurrent === row.dataset.historyKey) row.classList.add("is-current");
  row.addEventListener("click", () => pointAtHistoryRow(entry));
  row.addEventListener("contextmenu", (event) => {
    const items = historyActions(entry);
    if (!items.length) return;
    event.preventDefault();
    // Pointed at as well as opened, so the menu and the eye never disagree
    // about which row is being acted on.
    pointAtHistoryRow(entry);
    openAcquireMenu(event, items);
  });
}

/** Name one History row across a redraw.
 *
 * A run and a download are numbered in different tables, so `7` means two
 * different rows on one screen — the prefix is what keeps them apart.
 */
function historyKey(entry) {
  return entry.kind === "download" ? `d:${entry.download_id}` : `p:${entry.plan_id}`;
}

/** Make one row the current one, and take the mark off whichever held it. */
function pointAtHistoryRow(entry) {
  state.historyCurrent = historyKey(entry);
  for (const row of $("history-body").children) {
    row.classList.toggle("is-current", row.dataset.historyKey === state.historyCurrent);
  }
}

function coverCell(unitId) {
  const cell = document.createElement("td");
  cell.className = "list-cover-cell";
  if (!unitId) return cell;
  const cover = state.covers.get(unitId);
  if (cover) {
    const image = document.createElement("img");
    image.className = "list-cover";
    image.src = cover;
    image.alt = "";
    cell.appendChild(image);
    return cell;
  }
  const placeholder = document.createElement("div");
  placeholder.className = "list-cover card-cover-placeholder";
  placeholder.textContent = "\u266a";
  cell.appendChild(placeholder);
  return cell;
}

/** What a History row is, and which of them a question is about.
 *
 * Written as a table for the same reason the bench's is (see `QUALITY_GROUPS`):
 * every kind carries its own meaning, so none can be filtered for without being
 * offered, and a kind this list learns later cannot quietly land in whichever
 * branch happened to be last.
 *
 * `stopped partway` is not a state the database records — it is read from the
 * counts, so that runs written before that state was recorded are found too.
 */
const HISTORY_GROUPS = [
  ["all", "historyGroupAll", () => true],
  ["applied", "historyGroupApplied", (entry) =>
    entry.kind !== "download" && entry.state === "applied" && !historyPartial(entry)],
  ["partial", "historyGroupPartial", (entry) =>
    entry.kind !== "download" && historyPartial(entry)],
  ["reverted", "historyGroupReverted", (entry) =>
    entry.kind !== "download" && entry.state === "reverted"],
  ["download", "historyGroupDownload", (entry) => entry.kind === "download"],
];

/** A run that stopped partway, read from the counts rather than from the state.
 *
 * The one definition, so the row's colour, the row's words and the filter that
 * finds it can never disagree. */
function historyPartial(entry) {
  if (entry.kind === "download") return false;
  const written = entry.applied_operations ?? entry.operations;
  return entry.state === "failed" || written !== entry.operations;
}

function historyGroupName(group) {
  const found = HISTORY_GROUPS.find(([key]) => key === group);
  return found ? STR[found[1]] : group;
}

function historyIn(group, rows) {
  const found = HISTORY_GROUPS.find(([key]) => key === group);
  return found ? rows.filter(found[2]) : rows;
}

/** Whether this run answers to the text in the search box.
 *
 * The same rule as the Library's search and for the same reason: every word has
 * to appear somewhere, in any order, accents ignored. What is searched is what
 * the row shows — the album's name and the folder on disk — because those are
 * the two ways a run is named when it is being looked for.
 */
function matchesHistorySearch(entry) {
  const wanted = folded(state.historySearch).split(/\s+/).filter(Boolean);
  if (!wanted.length) return true;
  const haystack = folded(
    [entry.artist, entry.album, entry.folder, entry.folder_path, entry.directory]
      .filter(Boolean)
      .join(" "),
  );
  return wanted.every((word) => haystack.includes(word));
}

function visibleHistory() {
  return historyIn(state.historyFilter, state.history).filter(matchesHistorySearch);
}

function renderHistorySearchBox() {
  renderBarSearch("history-search", state.historySearch);
  $("history-search-clear").hidden = !state.historySearch;
}

function clearHistorySearch() {
  state.historySearch = "";
  renderHistorySearchBox();
  renderHistory();
}

function renderHistory() {
  const all = state.history;
  const rows = visibleHistory();
  // Three different nothings, and the screen says which one it is: a search
  // that matches nothing must not look like a History with nothing in it.
  $("history-questions").hidden = all.length === 0;
  $("empty-history").hidden = all.length > 0;
  $("history-none").hidden = all.length === 0 || rows.length > 0;
  $("history-table").hidden = rows.length === 0;
  state.historyCounts = {};
  for (const [group] of HISTORY_GROUPS) {
    state.historyCounts[group] = historyIn(group, all).length;
  }
  $("history-status").textContent = STR.statusLabel(
    historyGroupName(state.historyFilter),
    state.historyCounts[state.historyFilter] ?? 0,
  );
  renderHistorySearchBox();
  const body = $("history-body");
  body.replaceChildren(
    ...rows.map((entry) => {
      // Two things happen to a folder, and this list tells both: it arrived,
      // and then this app renamed it. A plan can be reverted; a download can be
      // opened.
      if (entry.kind === "download") return downloadRow(entry);
      const row = document.createElement("tr");
      // A run that stopped partway says how far it got: how many operations
      // were written out of how many planned. The reading itself lives in
      // `historyPartial`, so the colour of this row, the words in it and the
      // filter that finds it cannot drift apart.
      const written = entry.applied_operations ?? entry.operations;
      const partial = historyPartial(entry);
      if (entry.state === "reverted") row.className = "is-reverted";
      else if (partial) row.className = "is-partial";
      if (entry.unit_id) {
        row.dataset.unitId = entry.unit_id;
        if (!state.covers.has(entry.unit_id)) coverObserver.observe(row);
      }
      historyMenu(row, entry);
      const name = document.createElement("td");
      const onDisk = entry.folder_path.split("/").pop();
      name.textContent = historyName(entry, onDisk);
      name.title = entry.folder_path;
      const when = document.createElement("td");
      when.textContent = entry.applied_at || "";
      const operations = document.createElement("td");
      operations.textContent = partial
        ? STR.partialCount(written, entry.operations)
        : entry.operations;
      const stateCell = document.createElement("td");
      stateCell.textContent =
        entry.state === "reverted"
          ? STR.stateReverted
          : partial
            ? STR.statePartial
            : STR.stateApplied;
      row.append(
        coverCell(entry.unit_id), name, when, operations, stateCell, historyMoreCell(entry),
      );
      return row;
    }),
  );
}

function downloadRow(entry) {
  const row = document.createElement("tr");
  if (entry.unit_id) {
    row.dataset.unitId = entry.unit_id;
    if (!state.covers.has(entry.unit_id)) coverObserver.observe(row);
  }
  historyMenu(row, entry);

  const name = document.createElement("td");
  name.textContent = historyName(entry, entry.folder || entry.directory);
  name.title = `${entry.folder}\n${STR.historyFrom(entry.source)}`;

  const when = document.createElement("td");
  when.textContent = entry.requested_at || "";

  const what = document.createElement("td");
  what.textContent = STR.historyFiles(entry.files, megabytes(entry.size));

  // What actually happened to it, rather than one word for every row.
  // `arrival` is computed against the disk (api.py), so a request whose files
  // never came does not claim they did.
  const stateCell = document.createElement("td");
  if (entry.arrival === "absent") {
    stateCell.textContent = STR.historyArriving;
    // `muted` rather than `is-unknown`: the latter exists only inside
    // `.transfer-state` and `.q-heard`, so on this cell it would be a class
    // that styles nothing.
    stateCell.className = "muted";
    stateCell.title = STR.tipHistoryArriving(entry.requested_at || "");
  } else if (entry.arrival === "waiting") {
    stateCell.textContent = STR.historyWaiting;
    stateCell.title = STR.tipHistoryWaiting;
  } else if (entry.arrival === "cleared") {
    stateCell.textContent = STR.historyCleared;
  } else {
    stateCell.textContent = STR.historyDownloaded;
  }

  // The same one control the applied runs carry, so the last column means the
  // same gesture whichever kind of row it is on.
  row.append(coverCell(entry.unit_id), name, when, what, stateCell, historyMoreCell(entry));
  return row;
}

async function forgetDownload(id) {
  const result = await api().forget_download(id);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  toast(STR.forgotDownload);
  await refresh();
}

async function openDownload(id) {
  const result = await api().open_download(id);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  // It opens the downloads folder itself when the album has not finished
  // arriving, and saying so explains why the folder looks empty.
  if (!result.arrived) toast(STR.historyNotArrived);
}

// --- album dialog ---------------------------------------------------------

const READING_ORDER = ["rename_folder", "rename_file", "write_tags", "write_image", "embed_image"];

/** Order the plan for reading, from the folder inwards.
 *
 * This is display only. The plan runs in its own order and that order is
 * load-bearing — tags are written where a file will be, disc folders are
 * renamed before anything moves into them, images are written last so no
 * rename can invalidate their paths. What is read first is the name the folder
 * will carry.
 */
function inReadingOrder(operations) {
  return [...operations].sort(
    (one, other) =>
      READING_ORDER.indexOf(one.kind) - READING_ORDER.indexOf(other.kind) ||
      one.sequence - other.sequence,
  );
}

const basename = (path) => String(path).split("/").pop();

/** Fold every tag write into one entry, because they all say the same thing.
 *
 * One line per file listing the same field names is a block of lines that
 * differ only in a name already printed on the row above — a wall that gets
 * skipped rather than read. What is worth knowing is which fields are touched
 * and how many files; the values are the names in the table above, per track.
 *
 * The union of the fields, not the first file's: a compilation whose credit was
 * corrected writes one field more on that track, and a fold that showed only
 * the first file's set would quietly under-report what is written.
 */
function foldTagWrites(operations) {
  const writes = operations.filter((operation) => operation.kind === "write_tags");
  if (writes.length < 2) return operations;
  const fields = new Set();
  for (const write of writes) {
    for (const name of Object.keys(write.after?.tags || {})) fields.add(name);
  }
  // A picture embedded in a file *is* a tag, so it belongs in the same sentence
  // as the fields rather than on one `Embed cover` line per track. The cover
  // written *beside* the files is a different thing and keeps its own line: it
  // is a file in the folder, not a tag.
  const embedded = operations.filter((operation) => operation.kind === "embed_image");
  // Two sentences, because they are two different promises: a cover *arrives*,
  // and a repair puts back what the file already had with its label corrected.
  const repairs = embedded.filter((operation) => operation.after?.repair);
  // And the icon the Finder draws, which is neither an arriving cover nor a
  // repaired label — it is the same picture put where the Finder can see it.
  const icons = embedded.filter((operation) => operation.after?.icon);
  return [
    ...operations.filter(
      (operation) => operation.kind !== "write_tags" && operation.kind !== "embed_image",
    ),
    {
      kind: "write_tags",
      sequence: writes[0].sequence,
      folded: {
        files: writes.length,
        fields: [...fields],
        covers: embedded.length - repairs.length - icons.length,
        repairs: repairs.length,
        icons: icons.length,
      },
    },
  ];
}

function operationLine(operation) {
  const item = document.createElement("li");
  const kind = document.createElement("span");
  kind.className = "op-kind";
  const detail = document.createElement("span");
  detail.className = "op-detail";
  if (operation.kind === "rename_file" || operation.kind === "rename_folder") {
    kind.textContent =
      operation.kind === "rename_file" ? STR.operationRenameFile : STR.operationRenameFolder;
    detail.innerHTML = "";
    detail.append(basename(operation.target), " → ");
    const target = document.createElement("b");
    target.textContent = basename(operation.after.path);
    detail.append(target);
  } else if (operation.kind === "write_tags") {
    kind.textContent = STR.operationWriteTags;
    if (operation.folded) {
      // The arrow belongs on `Write tags` itself, and what it opens is one
      // sentence — which fields, on how many files, and that each file gets the
      // values shown on its own row above — rather than a per-file list.
      const fold = document.createElement("details");
      fold.className = "op-fold";
      const summary = document.createElement("summary");
      summary.textContent = STR.operationWriteTags;
      const said = document.createElement("div");
      said.className = "op-fold-said";
      said.textContent = STR.tagsFolded(
        operation.folded.files,
        operation.folded.fields,
        operation.folded.covers,
        operation.folded.repairs,
        operation.folded.icons,
      );
      const note = document.createElement("span");
      note.className = "muted";
      note.textContent = ` ${STR.tagsFollowTheNames}`;
      said.append(note);
      fold.append(summary, said);
      item.append(fold);
      return item;
    } else {
      const tags = operation.after.tags || {};
      detail.textContent = `${basename(operation.target)}: ${Object.keys(tags).join(", ")}`;
    }
  } else if (operation.kind === "write_image") {
    kind.textContent = STR.operationWriteImage;
    detail.textContent = `${basename(operation.target)} (${operation.after.width}×${operation.after.height})`;
  } else {
    kind.textContent = STR.operationEmbedImage;
    detail.textContent = basename(operation.target);
  }
  item.append(kind, detail);
  return item;
}

function sourceLink(url, source) {
  const link = document.createElement("button");
  link.className = "link-button";
  link.textContent = STR.viewSource(source);
  link.addEventListener("click", (event) => {
    event.preventDefault();
    reportIfRefused(api().open_url(url));
  });
  return link;
}

/** Format words that say nothing about which edition this is.
 *
 * `Album` is on almost every release — it distinguishes a record from a single,
 * which is not the question a review is asking.
 *
 * A list of words to drop is an enumeration, so it is written so that its
 * failure is benign: a descriptor nobody listed keeps being shown, which is a
 * word too many rather than a fact made invisible.
 */
const EMPTY_FORMAT_WORDS = new Set(["Album"]);

/** The label as it is called, without the catalogue's own disambiguator.
 *
 * Discogs writes `Harbor Records (4)` when it holds four labels of that name.
 * The number is about Discogs' index, not about this record.
 */
function labelAsCalled(label) {
  return label.replace(/\s*\(\d+\)\s*$/, "");
}

/** The edition's own facts, minus any the caller has already stated or does not want.
 *
 * ``saidYear`` is the year the screen has already printed, when it has printed
 * one. `album 1971` beside a header reading `1971` is the same fact twice.
 * Written as *unless it is the year already said* rather than as *never*,
 * because a release whose album year differs from the one the header names is
 * the case worth reading.
 *
 * ``brief`` is for the screen that is describing an album already chosen, where
 * the catalogue number adds nothing. The candidate rows do not pass it: there,
 * telling one pressing from another is the entire job, and the catalogue
 * number is often the only thing that does it.
 */
function candidateFacts(candidate, { saidYear = null, brief = false } = {}) {
  // What tells a CD edition from a vinyl master, which is the whole question a
  // review answers. Every entry is verifiable on the linked page.
  const facts = [];
  const formats = (candidate.formats || []).filter((word) => !EMPTY_FORMAT_WORDS.has(word));
  if (formats.length) facts.push(formats.join(", "));
  // What kind of record it is. One genre: Discogs lists two or three and the
  // second is almost always the first said again more narrowly.
  if ((candidate.genres || []).length) facts.push(candidate.genres[0]);
  if (candidate.master_year && candidate.master_year !== saidYear) {
    facts.push(STR.masterYear(candidate.master_year));
  }
  if (candidate.year) facts.push(STR.editionYear(candidate.year));
  if (candidate.country) facts.push(candidate.country);
  if (candidate.labels.length) {
    const catalogue = !brief && candidate.catalog_numbers.length
      ? ` ${candidate.catalog_numbers[0]}`
      : "";
    facts.push(labelAsCalled(candidate.labels[0]) + catalogue);
  }
  if (candidate.barcode && !brief) facts.push(`⌗ ${candidate.barcode}`);
  return facts;
}

function candidateEvidence(candidate) {
  const evidence = [];
  if (candidate.titles_compared) {
    evidence.push(STR.titleEvidence(candidate.titles_agreeing, candidate.titles_compared));
  }
  if (candidate.durations_compared) {
    evidence.push(
      STR.durationEvidence(candidate.durations_agreeing, candidate.durations_compared),
    );
  }
  evidence.push(STR.trackCounts(candidate.tracks, candidate.local_tracks));
  return evidence;
}

function candidateLine(candidate) {
  const item = document.createElement("li");
  item.className = "candidate";
  // "In use" comes from the album payload, not from session memory, so the
  // release the scan itself chose is marked too — before any gesture.
  const active =
    state.openAlbum &&
    state.openAlbum.source === candidate.source &&
    String(state.openAlbum.release_id) === String(candidate.release_id);
  if (active) item.classList.add("is-adopted");

  const head = document.createElement("div");
  head.className = "cand-head";
  const main = document.createElement("button");
  main.type = "button";
  main.className = "cand-main";
  const title = document.createElement("div");
  title.className = "cand-title";
  title.textContent = `${candidate.artist} — ${candidate.title}`;
  if (active) {
    const badge = document.createElement("span");
    badge.className = "cand-badge";
    badge.textContent = STR.inUse;
    title.append(badge);
  }
  const sub = document.createElement("div");
  sub.className = "cand-sub";
  sub.textContent = [
    STR.confidence(candidate.confidence),
    candidate.source,
    ...candidateFacts(candidate),
  ]
    .filter(Boolean)
    .join(" · ");
  main.append(title, sub);

  const use = document.createElement("button");
  use.className = "button";
  use.textContent = STR.useCandidate;
  use.title = STR.tipUseCandidate;
  use.addEventListener("click", () => adoptCandidate(candidate, use));

  // The page this candidate came from, one click away: "Use this" is a
  // decision, and nobody should have to make it blind.
  head.append(main);
  if (candidate.url) {
    const view = document.createElement("button");
    view.type = "button";
    view.className = "cand-link";
    view.textContent = STR.viewSource(candidate.source);
    view.addEventListener("click", (event) => {
      event.stopPropagation();
      reportIfRefused(api().open_url(candidate.url));
    });
    head.append(view);
  }
  head.append(use);

  const body = document.createElement("div");
  body.className = "cand-body";
  body.hidden = true;
  main.addEventListener("click", () => {
    body.hidden = !body.hidden;
    if (!body.hidden) expandCandidate(candidate, body);
  });

  item.append(head, body);
  return item;
}

async function expandCandidate(candidate, body) {
  if (body.dataset.loaded === "1") return;
  body.replaceChildren(Object.assign(document.createElement("p"), {
    className: "muted",
    textContent: STR.loading,
  }));
  const response = await api().candidate_detail(
    state.openAlbum.unit_id,
    candidate.source,
    candidate.release_id,
  );
  // The scored candidate keeps its evidence; the fetch only adds what a
  // summary could not carry — the tracklist and the album's own year.
  const full = response.ok ? { ...candidate, ...response.candidate } : candidate;
  body.dataset.loaded = "1";
  body.replaceChildren();

  const facts = document.createElement("p");
  facts.className = "cand-facts";
  facts.textContent = candidateFacts(full).join(" · ");
  const evidence = document.createElement("p");
  evidence.className = "cand-evidence";
  evidence.textContent = candidateEvidence(full).join(" · ");
  body.append(facts, evidence);

  if (full.explanation) {
    const why = document.createElement("p");
    why.className = "muted";
    why.textContent = full.explanation;
    body.appendChild(why);
  }
  if (full.track_titles && full.track_titles.length) {
    const list = document.createElement("ol");
    list.className = "cand-tracks";
    list.replaceChildren(
      ...full.track_titles.map((name) => {
        const entry = document.createElement("li");
        entry.textContent = name;
        return entry;
      }),
    );
    body.appendChild(list);
  }
  if (full.url) body.appendChild(sourceLink(full.url, full.source));
}

/** Draw the album as it will be, with every name in it editable.
 *
 * One surface, not two. A plan listing what changes beside a separate form
 * holding the names is the same decisions in two vocabularies, with only one
 * of them sending anything: a folder name typed into the form would be ignored
 * by the dialog's primary button. Here what is shown is what will be written,
 * so there is nothing left to forget to send.
 *
 * ``offered`` is the plan the dialog's button would actually run. It is always
 * the album's own plan: an album is never applied in part, and a plan that
 * only partly matches shows its blockers instead of a smaller offer.
 */
function renderPlanEditor(album, offered) {
  const editor = $("plan-editor");
  editor.replaceChildren();
  // Pairings made against the release this album carried before. They are
  // neither applied nor thrown away, because a position belongs to a tracklist
  // and this is a different one — so the question is asked here, once, with the
  // pairings named. It lives outside the table because a table takes rows, not
  // paragraphs.
  const perch = $("plan-held");
  perch.replaceChildren();
  const held = album.applied ? [] : album.suspended_pairings || [];
  perch.hidden = held.length === 0;
  if (held.length) perch.append(suspendedPairings(album, held));
  // An organized album draws its names too. The restore marks every album
  // organized by an earlier run as applied, so hiding the names on `applied`
  // would leave every organized album with none on screen, and `Plan this
  // album again` would answer "Nothing would change" over an empty dialog.
  //
  // What must not be drawn is the plan that has **already run**: its rows name
  // files that no longer carry those names, and a name typed into them would be
  // an edit made against the past. That is an applied album still holding the
  // operations it applied — the same condition the server reads before it takes
  // a correction, so the screen and the answer cannot disagree.
  // `written` is the album's own answer to *has the plan on screen been run*,
  // which `applied` does not answer: an organized album re-planned into
  // artwork writes alone is applied and has written nothing.
  const written = album.written && (offered || []).length > 0;
  // An organized album's names wait to be asked for. Restored from a restart,
  // such an album draws its table — and drawing it is not inviting typing into
  // it: nothing has planned those names against the files since they were
  // written, so an edit would be made against a reading of unknown age. Only
  // `Plan this album again` wakes it. An album still in review is untouched by
  // this — it has never been organized, and its plan is the one it was just
  // given.
  //
  // The condition is computed by the server and read here (`_asleep` there):
  // one sentence stated in two languages is two places for it to drift.
  const asleep = album.asleep;
  if (written || asleep || (album.release_tracks || []).length === 0) {
    editor.hidden = true;
    return;
  }
  editor.hidden = false;

  // The positions adopted by hand, read once for the whole table.
  // Position → the signature adopted there, so the row can offer to move it.
  // A Set of positions would answer *is this one adopted* and nothing else, and
  // the gesture that changes the number needs to name the file it is about.
  const adoptedPositions = new Map(
    Object.entries(album.extra_positions || {}).map(([signature, at]) => [
      Number(at),
      { signature, position: Number(at), last: album.next_position },
    ]),
  );
  const renames = new Map(
    (offered || [])
      .filter((operation) => operation.kind === "rename_file")
      .map((operation) => [basename(operation.target), basename(operation.after.path)]),
  );
  const pairedByTrack = new Map(
    (album.evidence || [])
      .filter((row) => row.status === "paired" && row.track_position !== null)
      .map((row) => [row.track_position, row]),
  );
  // A track nothing claimed, and the files nothing claimed either. Reported
  // far apart — `no file here` up at the track and `left alone` for the file at
  // the very bottom — they read as the app having lost a file that is plainly
  // in the folder, so they are drawn together.
  const looseByTrack = new Map(
    (album.evidence || [])
      .filter((row) => row.status === "unmatched_track" && row.track_position !== null)
      .map((row) => [row.track_position, row]),
  );
  // How many tracks are asking "which file is this?". One asking track gets the
  // files on its own row; several get one list below the table, because the
  // same block drawn per track is one thing said many times.
  const missingTracks = looseByTrack.size;
  const corrections = new Map(
    (album.corrections || [])
      .filter((entry) => entry.field === "track_title")
      .map((entry) => [entry.track_position, entry.value]),
  );
  const credits = new Map(
    (album.corrections || [])
      .filter((entry) => entry.field === "track_artist")
      .map((entry) => [entry.track_position, entry.value]),
  );
  // Every file of this album, whoever holds it, so a track can be pointed at any
  // one of them. Built from the evidence rather than from the plan: a file no
  // track claims has no plan row, and it is the likeliest one to be pointed at.
  const everyFile = (album.evidence || [])
    .filter((row) => row.file && row.signature)
    .map((row) => ({
      file: row.file,
      signature: row.signature,
      claimedBy: row.status === "paired" ? row.track_position : null,
    }));

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  // Every file can be told what it is, so the column is always there — narrow,
  // and empty-looking until a row is hovered or something has been said about
  // it.
  const overrides = album.quality_overrides || {};
  const paired = (album.evidence || []).filter((row) => row.status === "paired");
  for (const [text, className] of [
    ["", "plan-label"],
    [STR.planHeadNow, ""],
    ["", "plan-arrow"],
    [STR.planHeadAfter, ""],
    ["", "plan-word"],
  ]) {
    const cell = document.createElement("th");
    cell.textContent = text;
    if (className) cell.className = className;
    headRow.appendChild(cell);
  }
  head.appendChild(headRow);

  const body = document.createElement("tbody");
  // The album's own folder, from the plan being offered — a multi-disc plan
  // renames `CD1` first, and that is not the name this album claims.
  const folderRename = (offered || []).find(
    (operation) =>
      operation.kind === "rename_folder" && basename(operation.target) === album.folder,
  );
  // The plan's own answer, and never a second one worked out here. The plan is
  // built *with* any stored `folder_name` correction in it, so reading the
  // correction first agrees with the plan only until a rule changes what the
  // plan does with it — and then `AFTER APPLYING` names a folder that is not
  // the one about to be written. A screen that says what a gesture will do has
  // to read the gesture.
  const planned = folderRename
    ? basename(folderRename.after.path)
    : album.planned_folder || album.folder;
  body.append(
    planRow({
      key: "folder",
      label: STR.planFolder,
      hint: STR.editFolder,
      value: planned,
      before: album.folder,
      after: planned,
      dataset: { key: "folder", field: "folder" },
      // No flag here. A folder holds no audio, so it cannot have been lossy
      // before it arrived. The word is said on the files; the folder name
      // follows from them.
    }),
  );
  // The wrong-record quieting, asked once for the whole editor.
  const wrongRecord = nothingMatchedHere(album);
  for (const track of releaseRowsToDraw(album)) {
    const pairedRow = pairedByTrack.get(track.position);
    const file = pairedRow ? pairedRow.file : undefined;
    body.append(
      planRow({
        key: `track-${track.position}`,
        label: String(track.position).padStart(2, "0"),
        hint: STR.editTitle,
        value: corrections.get(track.position) || track.title,
        before: file || null,
        // No rename operation means the file already carries the name this
        // plan would give it, so the column after is the column before.
        after: file ? renames.get(file) || file : null,
        missing: !file,
        // The proof that this file is this track, folded into the row that
        // renames it: one surface per album, not two. Except on an
        // arrangement, whose "published" length IS the file's own — a proof
        // line there would be a file agreeing with itself wearing the words of
        // proof, so an arrangement's rows carry none.
        proof:
          album.source === "tags"
            ? ""
            : pairedRow
              ? STR.planPaired(pairedRow.file_ms, pairedRow.track_ms, pairedRow.delta_ms)
              : // The cell above already says it. A release track with no file
                // draws `no file here` in the column; this line says nothing
                // where it would only say the same words again.
                (looseByTrack.get(track.position) || {}).candidates?.length
                ? STR.planTrackUnrecognized
                : "",
        proofDisagrees: Boolean(
          album.source !== "tags" && pairedRow && pairedRow.length_disagrees,
        ),
        // The doubt, in the amber this window already uses to say *look at
        // this*. The row is offered, not decided: nothing is written from it,
        // and the album waits on it.
        // One word apart from what the file calls itself, and both spellings
        // plausible. Not a verdict and not a flag on the pairing — the file is
        // the right file; it is the *word* that is spelled two ways. The row
        // shows the file's spelling and offers to keep it.
        yours: track.yours || "",
        // Which of the two questions lit this row, and who answered it. Read
        // and not recomputed: the album's witness is not on this row, and the
        // window would have to reassemble a condition the server already
        // settled.
        yoursReason: track.yours_reason || "",
        yoursWitness: track.yours_witness || "",
        // And the same aside once it has been answered: what stands on this
        // line, and the way back to the catalogue. The server fills it only
        // where withdrawing restores this very offer, so pressing the two
        // buttons in turn is a round trip and never a loss.
        kept: track.kept || "",
        position: track.position,
        offered: Boolean(pairedRow && pairedRow.suggested),
        // And its opposite, said out loud rather than left as the absence of
        // amber. A column of ticks answers *are these fine* without each row
        // being read, and the one row missing its tick is the message.
        confirmed: Boolean(pairedRow && !pairedRow.suggested),
        // What holds the pair: a pairing made by hand, the names, or only the
        // clock.
        byLength: Boolean(pairedRow && pairedRow.paired_by === "length"),
        // Shown on this very row, where the question is: which of the files
        // nothing claimed might this be? Offered and not asserted — the pairing
        // failed because nothing cleared the bar, so naming a winner here would
        // be the app guessing where it had just refused to.
        //
        // Only while one track is asking it. On an album where *every* track
        // asks — a folder identified as the wrong record — the same block
        // would be drawn once per track over the same files. Past one asking
        // track the list is said once, below the table.
        loose: missingTracks > 1 ? [] : (looseByTrack.get(track.position) || {}).candidates || [],
        // What this row can say about *which file* this track is: the pairing
        // already made by hand, if any, and every file that could be pointed at
        // instead. Not on an arrangement — there each file simply IS its own
        // tag's track, so "not this file" would offer to correct a pairing that
        // was never decided.
        pairing:
          album.source === "tags"
            ? null
            : {
                position: track.position,
                byHand: Boolean(pairedRow && pairedRow.by_hand),
                paired: Boolean(pairedRow),
                signature: pairedRow ? pairedRow.signature : null,
                files: everyFile,
              },
        dataset: { key: `track-${track.position}`, position: track.position, field: "title" },
        // A compilation writes a credit into each file name, and the
        // catalogue's credit is not always the one wanted. So the credit is a
        // field of its own beside the title — never a raw name field, which
        // would leave the app guessing which half is which. It is offered
        // empty too: a compilation track the catalogue credits to nobody is
        // exactly the one worth naming.
        credit: album.is_various
          ? {
              key: `track-${track.position}-artist`,
              value: credits.get(track.position) || track.artist || "",
              hint: STR.editArtist,
              placeholder: STR.planNoArtist,
              dataset: {
                key: `track-${track.position}-artist`,
                position: track.position,
                field: "artist",
              },
            }
          : null,
        // The user's verdict on this file, offered where its consequence
        // shows: the label is written into this very name.
        quality: pairedRow
          ? {
              unitId: album.unit_id,
              signature: pairedRow.signature,
              marked: Boolean(pairedRow.marked_fake),
              measured: Boolean(pairedRow.measured_fake),
              said: overrides[pairedRow.signature] || null,
            }
          : null,
        // A track placed here by hand carries the way back out, on its own
        // row. Anywhere else it would be a control about one row living
        // somewhere the eye has to hunt for.
        adopted: adoptedPositions.get(track.position) || null,
      }),
    );
  }
  // A file no track claims is invisible in a table built from the release, and
  // it is exactly the file worth seeing: it will not be renamed at all.
  // Every file in the folder gets a row, and the left column is the folder.
  // Skipping a file because it is already named as a candidate on some asking
  // track's row would, where nothing matched at all, skip every file and leave
  // only the release's tracks under a column headed NOW ON DISK. A file
  // offered inside a track's picker is a control for pairing it; it is not the
  // folder being listed.
  for (const row of album.evidence || []) {
    if (row.status !== "unmatched_file") continue;
    body.append(
      planRow({
        key: `orphan-${row.file}`,
        label: "—",
        value: row.file,
        before: row.file,
        after: null,
        orphan: true,
        // Said once above the plan when the whole folder is in this position,
        // not on every row. On the album that mostly matched it stays: there
        // one or two rows carry it, and it is the information of those rows.
        proof: wrongRecord ? "" : STR.planFileOnly,
        dataset: {},
        // The right-hand column repeats the name and offers a place on the
        // tracklist, instead of saying only that the file is left behind.
        // Not against a record nothing matched: adoption controls under a
        // release that is not this album would offer to place every file onto
        // the wrong record's tracklist.
        adopt: wrongRecord ? null : {
          signature: row.signature,
          position: album.next_position,
          last: album.next_position,
        },
      }),
    );
  }
  editor.append(head, body);
  renderUnclaimed(album, looseByTrack, missingTracks, paired.length);
}

/** Whether the record on this album is one that nothing here matched.
 *
 * The one condition behind every quieting this dialog does for a wrong
 * identification: no track of the release paired with any file. Computed in
 * one place because five surfaces read it — the track rows, the notice, the
 * blockers list, the file rows' controls, and the sentence under the table —
 * and a rule stated in five places ends up applied in some of them. No
 * threshold of its own: `paired === 0` is a fact, not a cut.
 */
function nothingMatchedHere(album) {
  if (!(album.release_tracks || []).length) return false;
  return !(album.evidence || []).some((row) => row.status === "paired");
}

/** How much of the album's own naming a witness must publish before it *knows* this record. */
const WITNESS_KNOWS_IT = 0.5;

/** What the witness's stored testimony lets this screen state, or `null`.
 *
 * `null` is not *the witness knows nothing* — it is *nothing measured here says
 * it does*, which is why the offer to ask it stands either way. Stored
 * testimony may have been obtained with a candidate's words rather than the
 * album's own, so its numbers may be about a different record; they are quoted
 * only when they clear the bar, and the live lookup behind `Read its
 * tracklist` is what answers for the rest.
 *
 * Its own named function so the rule can be run rather than read.
 */
function witnessKnowsThisRecord(album) {
  const witness = (album.positions || []).find(
    (position) => position.source === "itunes" && position.witness,
  );
  if (!witness || !witness.asked) return null;
  const compared = witness.titles_compared || 0;
  if (!compared) return null;
  return (witness.titles_agreeing || 0) / compared >= WITNESS_KNOWS_IT ? witness : null;
}

/** Whether this screen has a witness to offer at all — configured and reached. */
function witnessIsAvailable(album) {
  const witness = (album.positions || []).find(
    (position) => position.source === "itunes" && position.witness,
  );
  return Boolean(witness) && witness.reached !== false;
}

/** Draw the witness's way out under the wrong-record notice.
 *
 * Shown only where the record is wrong: an album that exists somewhere must
 * not be met with silence because the writing sources failed. An album the
 * catalogues named correctly has nothing to be silent about, and putting this
 * on every screen would spend attention on an album that is already right.
 *
 * The sentence is drawn only where it was measured, and the controls are drawn
 * either way: an offer to ask costs nothing until it is pressed, and a screen
 * that stays quiet because a *stored* number was low is the silence this is
 * here to end.
 */
function renderWitnessWay(album, wrongRecord) {
  const way = $("album-witness-way");
  const shown = wrongRecord && witnessIsAvailable(album);
  way.hidden = !shown;
  // Dropped whenever the dialog draws: the words are somebody else's catalogue
  // and this application keeps none of them. A record left in a variable
  // across albums would be storing them, and it would draw one album's
  // tracklist under another's notice.
  state.witnessRecord = null;
  const tracks = $("album-witness-tracks");
  tracks.replaceChildren();
  tracks.hidden = true;
  const head = $("album-witness-head");
  head.replaceChildren();
  head.hidden = true;
  $("album-witness-read").textContent = STR.witnessRead;
  if (!shown) return;
  const known = witnessKnowsThisRecord(album);
  const said = $("album-witness-said");
  said.hidden = !known;
  said.textContent = known
    ? STR.witnessSaid(known.titles_agreeing || 0, known.titles_compared || 0)
    : "";
}

/** Fetch what the witness recognised, and draw it — or fold it away again.
 *
 * Live, every time it is opened, because nothing of it is kept between one
 * opening and the next. Two requests against a service that allows about
 * twenty a minute, spent only when they are asked for.
 */
async function readWitnessTracklist(button) {
  const tracks = $("album-witness-tracks");
  const head = $("album-witness-head");
  if (!head.hidden) {
    tracks.hidden = true;
    head.hidden = true;
    button.textContent = STR.witnessRead;
    return;
  }
  const record = await witnessRecordNow(button);
  if (!record) return;
  // What the record is, first: the album existing somewhere complete is the
  // fact, and the tracklist is the detail under it.
  head.replaceChildren(STR.witnessHead(record));
  if (record.url) head.append(" ", sourceLink(record.url, "itunes"));
  head.hidden = false;
  tracks.replaceChildren();
  for (const track of record.tracks || []) {
    const line = document.createElement("li");
    line.textContent = STR.witnessTrack(track.position, track.name, track.duration_ms);
    tracks.append(line);
  }
  // A record with no tracklist is an answer too — the album exists over there
  // and the lookup published no songs — and a list with nothing in it draws as
  // a blank gap that looks like the fold failed.
  tracks.hidden = tracks.children.length === 0;
  button.textContent = STR.witnessHide;
}

/** Search the catalogues that may write with the words the witness used.
 *
 * What it searches with cannot come from storage, because none of it is
 * stored, so the words are fetched at the moment it is pressed. Nothing of the
 * witness is adopted here — what comes back is candidates from Discogs and
 * MusicBrainz, judged on their own durations like any other.
 */
async function searchWithWitnessWords(button) {
  const record = await witnessRecordNow(button);
  if (!record) return;
  const answer = await api().search_again(state.openAlbum.unit_id, record.artist, record.title);
  if (!answer.ok) toastError(answer.error);
}

/** The witness's record, from this dialog's hand or fetched now. */
async function witnessRecordNow(button) {
  if (state.witnessRecord) return state.witnessRecord;
  const was = button.textContent;
  button.textContent = STR.witnessLooking;
  const answer = await withSpinner(button, () => api().witness_record(state.openAlbum.unit_id));
  button.textContent = was;
  if (!answer.ok) {
    toastError(answer.error);
    return null;
  }
  if (!answer.record) {
    toast(STR.witnessGone);
    return null;
  }
  state.witnessRecord = answer.record;
  return state.witnessRecord;
}

/** Which of the release's tracks get a row of their own — all of them, or none.
 *
 * A release nothing matched does not get to say a file is missing. Where at
 * least one track paired, this record *is* the album: a track with no file is
 * a real gap, `no file here` is the truth, and the row is where one is paired.
 * Where nothing paired at all, the record is a different one, and rows reading
 * `no file here` would accuse the folder of missing tracks it never had. What
 * belongs on screen there is the files that are on the disk.
 *
 * Nothing is lost by it: the notice above the plan says nothing matched, and
 * the files each keep a row of their own.
 *
 * Its own function so the rule can be run rather than read.
 */
function releaseRowsToDraw(album) {
  const tracks = album.release_tracks || [];
  if (!tracks.length) return [];
  return nothingMatchedHere(album) ? [] : tracks;
}

function renderUnclaimed(album, looseByTrack, missingTracks, pairedCount) {
  const perch = $("plan-unclaimed");
  perch.replaceChildren();
  // Only the pairing problem speaks here. The album nothing matched is said by
  // the notice above the plan, once. One asking track keeps its files on its
  // own row.
  perch.hidden = missingTracks <= 1 || pairedCount === 0;
  if (perch.hidden) return;

  // Counted off the same rows the table draws, so the sentence and the table
  // are about one population. Counting the candidates the asking tracks offer
  // is a different set the moment a file is offered by none of them.
  const loose = (album.evidence || []).filter((row) => row.status === "unmatched_file");

  const heading = document.createElement("p");
  heading.className = "plan-loose-head";
  heading.textContent = STR.planUnclaimedHere(missingTracks, loose.length);
  perch.append(heading);

  // No list of names here: every file has a row of its own in the table above,
  // and repeating them underneath would be the same thing said twice.
}

/** Lay the name this row would write out around the pieces that are edited.
 *
 * A track's name on disk is derived: the position, the credit, the title, the
 * extension. Showing the whole derived name in the column and letting only the
 * editable pieces be typed keeps the two columns comparable — file against
 * file — while each field stays exactly one fact: the title that reaches the
 * tag as well as the name, and, on a compilation, the credit beside it.
 *
 * Returns a list alternating affixes and fields, or null when the derived name
 * does not literally contain a piece — which is what sanitizing does to a `:`
 * or a `/`, and what two identical pieces would do to the split. The row then
 * says outright what will be written instead of pretending to a split.
 *
 * The pieces are walked in the order the name writes them rather than searched
 * independently, so a credit and a title that read the same — `Luma - Luma` —
 * still land in their own fields. A piece with nothing in it cannot be found at
 * all, so it is *placed*: immediately before the next piece that was, which is
 * where the template would have put it.
 */
function editableParts(after, fields) {
  if (!after || !fields.length) return null;
  const segments = [];
  let waiting = [];
  let rest = after;
  for (const field of fields) {
    if (!field.value) {
      waiting.push(field);
      continue;
    }
    const at = rest.indexOf(field.value);
    if (at < 0) return null;
    segments.push({ affix: rest.slice(0, at) }, ...waiting, field);
    waiting = [];
    rest = rest.slice(at + field.value.length);
  }
  segments.push(...waiting, { affix: rest });
  return segments;
}

/** The same layout for a track this album does not have a file for.
 *
 * There is no derived name to split, because nothing will be written until a
 * file turns up. So the row shows the pieces it holds, joined the one way this
 * app's track template joins them — a correction typed here is recorded now and
 * spends itself when the file arrives (a rescan, a download).
 */
function looseParts(fields) {
  const segments = [];
  fields.forEach((field, index) => {
    if (index && field.value && fields[index - 1].value) segments.push({ affix: " - " });
    segments.push(field);
  });
  return segments;
}

/** Load which albums hold some of the same audio as another.
 *
 * One call for the whole shelf rather than one per card. Nothing is asked of
 * the disk here — this reads what the walks already wrote.
 */
async function loadShared() {
  let answer;
  try {
    answer = await api().shared_albums();
  } catch (error) {
    console.error("The shared-audio map could not be read.", error);
    return;
  }
  const pairs = (answer && answer.pairs) || [];
  state.sharedPairs = pairs;
  const counts = new Map();
  for (const pair of pairs) {
    if (pair.verdict) continue;
    for (const side of [pair.first, pair.second]) {
      if (side.unit_id === null || side.unit_id === undefined) continue;
      const seen = counts.get(side.unit_id) || { tracks: 0, within: pair.same_album };
      seen.tracks += pair.tracks.length;
      if (!pair.same_album) seen.within = false;
      counts.set(side.unit_id, seen);
    }
  }
  state.shared = counts;
}

/** The quiet mark on an album that shares audio with another one.
 *
 * Deliberately not an alarm: nothing is wrong with holding one recording in two
 * places — a track can be on the artist's album and on a compilation and
 * belong on both. It says what was measured and opens the place to answer.
 * Silent, never zero: an album whose audio this app has not named yet says
 * nothing at all, because an absence that reads as "no duplicates" would be a
 * claim nothing measured.
 */
function sharedBadge(album, { icon = false } = {}) {
  const seen = state.shared.get(album.unit_id);
  if (!seen) return null;
  const said = seen.within ? STR.sharedWithin(seen.tracks) : STR.sharedTracks(seen.tracks);
  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = icon ? "shared-badge is-icon" : "shared-badge";
  if (icon) {
    // A sleeve has room for a mark, not for a sentence. Drawn rather than
    // typed: a glyph depends on a font having it, and one that does not draws
    // an empty box.
    badge.append(twoSheets());
    badge.setAttribute("aria-label", said);
  } else {
    badge.textContent = said;
  }
  badge.title = `${said} — ${STR.tipSharedTracks}`;
  badge.addEventListener("click", (event) => {
    event.stopPropagation();
    openAlbum(album.unit_id, { openShared: true });
  });
  return badge;
}

/** Two sheets, one behind the other: the same thing, held twice. */
function twoSheets() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("aria-hidden", "true");
  for (const [x, y] of [[5.5, 2.5], [2.5, 5.5]]) {
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", String(x));
    rect.setAttribute("y", String(y));
    rect.setAttribute("width", "8");
    rect.setAttribute("height", "8");
    rect.setAttribute("rx", "1.5");
    rect.setAttribute("fill", "none");
    rect.setAttribute("stroke", "currentColor");
    rect.setAttribute("stroke-width", "1.2");
    svg.append(rect);
  }
  return svg;
}

/** What this album shares with another, and the answer that can be given.
 *
 * The pair is the unit of the question, not the track: two rips of one album
 * can hold several identical tracks, and deciding per track would be the same
 * click repeated for each.
 *
 * Nothing here writes to the disk, and the note above the block says so: every
 * other control in this dialog changes a file, so one that does not has to say
 * which kind it is.
 */
function renderSharedAudio(album) {
  const fold = $("shared-details");
  const holder = $("shared-pairs");
  const mine = (state.sharedPairs || []).filter(
    (pair) => pair.first.unit_id === album.unit_id || pair.second.unit_id === album.unit_id,
  );
  fold.hidden = mine.length === 0;
  if (!mine.length) {
    holder.replaceChildren();
    return;
  }
  // "Also in another album" over a body that says "in this same folder" is the
  // fold contradicting itself. An album can be its own pair, and when every
  // pair here is that kind, the heading says so.
  $("shared-summary").textContent = mine.every((pair) => pair.same_album)
    ? STR.sharedHeadingWithin
    : STR.sharedHeading;
  holder.replaceChildren(...mine.map((pair) => sharedPairBlock(pair, album)));
}

function sharedPairBlock(pair, album) {
  const block = document.createElement("div");
  block.className = "shared-pair";
  const other = pair.first.unit_id === album.unit_id ? pair.second : pair.first;

  const heading = document.createElement("p");
  heading.className = "shared-pair-name";
  heading.textContent = pair.same_album ? STR.sharedSame : other.album;
  block.append(heading);
  // And where it is. The name alone settles nothing when the two copies are
  // the same record under the same name, so the other folder's path is drawn.
  if (!pair.same_album && other.path) {
    const where = document.createElement("p");
    where.className = "shared-pair-where";
    where.textContent = other.path;
    block.append(where);
  }

  const list = document.createElement("ul");
  list.className = "shared-track-list";
  for (const track of pair.tracks) {
    const line = document.createElement("li");
    const mine = pair.first.unit_id === album.unit_id ? track.first : track.second;
    const theirs = pair.first.unit_id === album.unit_id ? track.second : track.first;
    line.textContent = mine === theirs ? mine : `${mine}  =  ${theirs}`;
    list.append(line);
  }
  block.append(list);

  if (pair.verdict) {
    const said = document.createElement("p");
    said.className = "muted";
    said.textContent =
      pair.verdict === "both" ? STR.sharedAnsweredBoth : STR.sharedAnsweredHandled;
    block.append(said, sharedButton(pair, "", STR.sharedUndo));
    return block;
  }
  const actions = document.createElement("div");
  actions.className = "shared-actions";
  actions.append(
    sharedButton(pair, "both", STR.sharedKeepBoth),
    sharedButton(pair, "handled", STR.sharedHandled),
  );
  const folders = document.createElement("button");
  folders.type = "button";
  folders.className = "link-button";
  folders.textContent = pair.same_album ? STR.sharedShowFolder : STR.sharedShowFolders;
  folders.title = STR.tipSharedShowFolders;
  folders.addEventListener("click", async () => {
    const paths = pair.same_album
      ? [pair.first.path]
      : [pair.first.path, pair.second.path];
    const result = await api().open_folders(paths);
    if (!result.ok) toastError(result.error);
  });
  actions.append(folders);
  block.append(actions);
  return block;
}

function sharedButton(pair, verdict, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    await withSpinner(button, () =>
      api().set_shared_verdict(pair.first.signature, pair.second.signature, verdict),
    );
    await loadShared();
    // The shelf behind the dialog carries the badge this answer removes, and
    // the dialog carries the block the answer was given in.
    renderGrid();
    if (state.openAlbum) renderSharedAudio(state.openAlbum);
  });
  return button;
}

/** The share of an album's files the plan covers before its chip reads as good. */
const STRONG_MATCH = 0.9;

function matchBadge(album, { compact = false } = {}) {
  const badge = document.createElement("span");
  badge.className = "match";
  // An arrangement pairs each file with its own tag, so `9/9` here is a file
  // agreeing with itself dressed as a match count. Nothing is shown, because
  // there is nothing measured to show.
  if (album.source === "tags") return badge;
  if (!album.match_total) return badge;
  // It defers to the notice: a record nothing matched already says so in
  // amber, and `Matches 0%` is that fact again as a percentage — of the wrong
  // record's tracklist, which makes the denominator another record's. Only in
  // the dialog, where the notice is: the shelf card has no notice, so taking
  // the fraction off there would remove the fact rather than repeat it once less.
  if (!compact && nothingMatchedHere(album)) return badge;
  // The colour follows the share, not the verdict: whether the plan may run is
  // a decision the buttons already announce, and a bright green on every card
  // would shout. The soft green `organized` wears above the bar, grey below it.
  //
  // `is-strong` / `is-weak` rather than `is-whole` / `is-open`: `is-open` means
  // *expanded* elsewhere in this file, and a class must not mean one thing here
  // and another there.
  const share = album.match_total ? album.match_paired / album.match_total : 0;
  badge.classList.add(share >= STRONG_MATCH ? "is-strong" : "is-weak");
  // A fraction on the card, where it sits beside a badge and must never read
  // as a second percentage; the sentence where there is room for one.
  badge.textContent = compact
    ? STR.planMatchesShort(album.match_paired, album.match_total)
    : STR.planMatches(album.match_paired, album.match_total);
  badge.title = STR.planMatchesDetail(
    album.match_paired,
    album.match_files_unpaired,
    album.match_tracks_unpaired,
  );
  return badge;
}

/** One row of the plan: the name on disk, and the name applying would write.
 *
 * Side by side and column-aligned, like the evidence table, because names are
 * compared with the eye and a line break defeats that. The name is text until
 * the pencil is asked for it: a row of live inputs per track is as many chances
 * to change a title with a stray click, and an edit here is a write to disk.
 */
function planRow({
  key,
  label,
  value,
  before,
  after,
  hint,
  credit = null,
  loose = [],
  missing = false,
  orphan = false,
  proof = "",
  proofDisagrees = false,
  dataset,
  quality = null,
  pairing = null,
  offered = false,
  confirmed = false,
  byLength = false,
  adopt = null,
  adopted = null,
  yours = "",
  yoursReason = "",
  yoursWitness = "",
  kept = "",
  position = null,
}) {
  const row = document.createElement("tr");
  row.className = offered ? "plan-row is-offered" : "plan-row";
  const changed = !missing && !orphan && before !== after;
  if (!changed && !missing && !orphan) row.classList.add("is-unchanged");
  if (missing) row.classList.add("is-missing");
  if (orphan) row.classList.add("is-orphan");

  const caption = document.createElement("td");
  caption.className = "plan-label";
  // The verdict for this one row, in the column that already runs down the
  // left. A tick for a pairing that is settled, a question mark for one that is
  // offered — the amber row says *look at this* and this says what to look for.
  // Kept out of the folder's row and an unclaimed file's, which are not
  // pairings and would be answering a question nobody asked of them.
  if (confirmed || offered) {
    const mark = document.createElement("span");
    // A third mark, because there are two answers and three states. A settled
    // pairing held by a length and nothing else is not the same fact as one the
    // names agree on: under one green tick, every row of a wrongly matched
    // compilation would read as confirmed over names that share not one word.
    const held = confirmed && byLength;
    mark.className = `plan-mark ${confirmed ? (held ? "is-length" : "is-yes") : "is-asking"}`;
    mark.textContent = confirmed ? (held ? "\u2248" : "✓") : "?";
    mark.title = confirmed
      ? held
        ? STR.planMarkByLength
        : STR.planMarkConfirmed
      : STR.planMarkOffered;
    caption.appendChild(mark);
  }
  caption.append(label);

  const now = document.createElement("td");
  now.className = "plan-now";
  // "no file here" is false when files are sitting right there unclaimed: what
  // failed is the recognition, not the folder.
  now.textContent = missing
    ? loose.length
      ? STR.evidenceTrackUnrecognized
      : STR.evidenceTrackOnly
    : before;
  if (proof) {
    const seen = document.createElement("div");
    seen.className = "plan-proof";
    // A pair made by hand is not evidence the app gathered, and saying "249s
    // yours · 249s published" under it would read as though it had been
    // measured into place. The row says who decided instead.
    seen.textContent = pairing && pairing.byHand ? STR.planPairedByHand : proof;
    now.appendChild(seen);
    // A name settled this pair and the published length disagrees anyway — the
    // one sign on this row that the file is not what its own name says. Said in
    // words beside the numbers, because a large gap is drawn exactly like
    // `0.0s apart` and would read as one more measurement.
    if (proofDisagrees && !(pairing && pairing.byHand)) {
      const off = document.createElement("div");
      off.className = "plan-proof is-off";
      off.textContent = STR.planLengthOff;
      now.appendChild(off);
    }
  }
  // The pairing already made by hand, with the way back. Said on the row rather
  // than in a list of corrections elsewhere: a decision that cannot be seen is
  // one that cannot be undone.
  if (pairing && pairing.byHand) {
    const mark = document.createElement("div");
    mark.className = "plan-hand";
    const said = document.createElement("span");
    said.className = "plan-hand-said";
    said.textContent = STR.planByHand;
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "plan-hand-undo";
    undo.textContent = STR.planUndoPair;
    undo.title = STR.planUndoPairTip;
    undo.addEventListener("click", () => pairFile(pairing.position, ""));
    mark.append(said, undo);
    now.appendChild(mark);
  }
  // The files nothing claimed, on the row of the track nothing claimed: the two
  // halves of one failure, together. Nearest first, by the matcher's own
  // reading, with the numbers that say why it was not near enough.
  if (missing && loose.length) {
    const heading = document.createElement("div");
    heading.className = "plan-loose-head";
    heading.textContent = STR.planLooseHead(loose.length);
    const list = document.createElement("ul");
    list.className = "plan-loose";
    // Three, because the list repeats on every track that has no file, each
    // offering the same files. The nearest are the ones worth reading, and the
    // count says what is not shown.
    for (const candidate of loose.slice(0, 3)) {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.className = "plan-loose-name";
      name.textContent = candidate.file;
      const why = document.createElement("span");
      why.className = "plan-loose-why";
      why.textContent = STR.planLooseWhy(candidate);
      item.append(name, why);
      // The offer becomes answerable where it is made. Nothing here asserts
      // which file it is — the button is the user's statement, not the app's.
      if (pairing && candidate.signature) {
        const pick = document.createElement("button");
        pick.type = "button";
        pick.className = "plan-pick";
        pick.textContent = STR.planPickThis;
        pick.title = STR.planPickTip;
        pick.addEventListener("click", () => pairFile(pairing.position, candidate.signature));
        item.append(pick);
      }
      list.append(item);
    }
    now.append(heading, list);
    if (loose.length > 3) {
      const rest = document.createElement("div");
      rest.className = "plan-loose-why";
      rest.textContent = STR.planLooseMore(loose.length - 3);
      now.append(rest);
    }
  }
  // Any file at all, for the track whose file is not among the three offered —
  // and for the pairing the app made and got wrong, which is the case no
  // threshold covers and the reason this exists.
  if (pairing && pairing.files.length) {
    now.append(filePicker(pairing));
  }
  // The two answers a doubtful row needs, side by side: *yes, this one* — and
  // "not this file", which is the picker already beside it. One row at a time,
  // because the doubt is about one file and must not have to be settled
  // wholesale.
  if (offered && pairing && pairing.position && pairing.signature) {
    const agree = document.createElement("button");
    agree.type = "button";
    agree.className = "button button-row plan-agree";
    agree.textContent = STR.planConfirmThis;
    agree.title = STR.planConfirmThisTip;
    agree.addEventListener("click", () =>
      confirmOnePairing(pairing.position, pairing.signature, agree),
    );
    now.append(agree);
  }

  const arrow = document.createElement("td");
  arrow.className = "plan-arrow";
  arrow.textContent = changed ? "→" : "";

  const next = document.createElement("td");
  next.className = "plan-after";
  const cell = document.createElement("div");
  cell.className = "plan-cell";

  // What this row accepts as typing, in the order the name writes it. A
  // compilation writes a credit before the title, and both are editable; every
  // other row holds one piece. A file no track claims holds none: it is not
  // being renamed, so there is nothing here to write.
  const fields = orphan
    ? []
    : credit
      ? [credit, { key, value, hint, dataset }]
      : [{ key, value, hint, dataset }];
  const parts = orphan ? null : missing ? looseParts(fields) : editableParts(after, fields);

  if (parts) {
    for (const segment of parts) {
      cell.append(segment.affix === undefined ? planField(segment) : planAffix(segment.affix));
    }
  } else {
    // Nothing to split around, so the cell says the written name whole and the
    // fields wait behind their pencils — with the name repeated underneath
    // while one is open, because a piece the name does not literally contain
    // can only be read against the name it produces.
    const stem = document.createElement("span");
    stem.className = "plan-stem";
    // The name, repeated, and a way to give it a place. `left alone` by itself
    // leaves nothing to read on the right and nothing to type for a file that
    // has no track. The number is offered — the one past the release's last —
    // and never read out of the file's own name.
    stem.textContent = orphan ? (adopt ? value : STR.planNoTrack) : after;
    if (orphan && adopt) stem.classList.add("plan-stem-orphan");
    cell.append(stem, ...fields.map((field) => planField(field, { silent: true })));
    if (orphan && adopt) cell.append(adoptControl(adopt));
  }
  if (adopted !== null) {
    // The number stays changeable after the track is added; otherwise the only
    // way to a different position would be to take the track out and put it
    // back. It is also what makes the renumbering legible: moving this one
    // redraws every row it pushes.
    cell.append(adoptControl(adopted, { moving: true }));
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "link-button plan-adopted-drop";
    undo.textContent = STR.planAdoptedDrop;
    undo.title = STR.planAdoptedDropTip;
    undo.addEventListener("click", () => dropAdoptedTrack(adopted.position));
    cell.append(undo);
  }
  // The file's spelling, beside the catalogue's, with nothing decided. Where
  // the two differ by a letter or two, the catalogue is wrong about as often as
  // the file is, and no rule can pick between two plausible words, so neither
  // does this.
  //
  // The same area says so after an answer is given, instead of vanishing: one
  // aside with two states, the offer and what was decided.
  if ((yours || kept) && position !== null) {
    cell.append(spellingAside({ yours, yoursReason, yoursWitness, kept, position }));
  }
  next.appendChild(cell);
  // A title that sanitizing rewrites cannot be shown as a slice of the name it
  // produces, so the name it produces is said in full underneath.
  if (!missing && !parts && after && after !== value) {
    const derived = document.createElement("div");
    derived.className = "plan-derived";
    derived.textContent = STR.planDerived(after);
    next.appendChild(derived);
  }

  // The quality flag lives in a column of its own, outside the cell that opens
  // for typing: a control inside the field being edited reads as part of the
  // name, and every click in there is a click near a caret.
  const word = document.createElement("td");
  word.className = "plan-word";
  if (quality) word.append(qualityWord(quality));
  row.append(caption, now, arrow, next, word);
  return row;
}

/** The pairings waiting on a question, and the two answers.
 *
 * They are listed, not counted: "3 pairings from another release" with nothing
 * to look at asks for a decision about work that cannot be seen. Reusing keeps
 * the positions exactly as they were recorded, which is a claim that this
 * tracklist runs like the one they were paired against — the table underneath
 * shows at once whether it was true, and undoing one pairing is a click on its
 * own row.
 */
function suspendedPairings(album, held) {
  const panel = document.createElement("div");
  panel.className = "plan-held-panel";

  const heading = document.createElement("div");
  heading.className = "plan-held-head";
  heading.textContent = STR.pairingsHeld(held.length);

  const why = document.createElement("p");
  why.className = "muted";
  why.textContent = STR.pairingsHeldWhy;

  const list = document.createElement("ul");
  list.className = "plan-held-list";
  for (const row of held) {
    const item = document.createElement("li");
    item.textContent = STR.pairingsHeldRow(row.track_position, row.file, row.added);
    list.append(item);
  }

  const actions = document.createElement("div");
  actions.className = "plan-held-actions";
  const reuse = document.createElement("button");
  reuse.type = "button";
  reuse.className = "button";
  reuse.textContent = STR.pairingsReuse;
  reuse.addEventListener("click", async () => {
    reuse.disabled = true;
    const response = await api().reuse_pairings(album.unit_id);
    if (!response.ok) {
      reuse.disabled = false;
      toastError(response.error);
      return;
    }
    toast(STR.pairingsReused(response.moved));
    renderAlbumDialog(response.album);
    await refresh();
  });
  const drop = document.createElement("button");
  drop.type = "button";
  drop.className = "button button-quiet";
  drop.textContent = STR.pairingsDiscard;
  drop.addEventListener("click", async () => {
    drop.disabled = true;
    const response = await api().discard_pairings(album.unit_id);
    if (!response.ok) {
      drop.disabled = false;
      toastError(response.error);
      return;
    }
    toast(STR.pairingsDiscarded(response.dropped));
    renderAlbumDialog(response.album);
    await refresh();
  });
  actions.append(reuse, drop);

  panel.append(heading, why, list, actions);
  return panel;
}

/** A way to point this track at any file in the folder.
 *
 * Closed until asked for, like the pencil: a plan is read for its names, and a
 * select on every row would be read instead of them. Open, it lists every file
 * this album holds and says which track each one is on right now — pointing at
 * a file that is already somewhere takes it from there, and that consequence
 * has to be visible before the click, not after it.
 */
function filePicker(pairing) {
  const holder = document.createElement("div");
  holder.className = "plan-picker";

  const open = document.createElement("button");
  open.type = "button";
  open.className = "plan-pick-open";
  open.textContent = pairing.paired ? STR.planRepair : STR.planPickOther;
  open.title = pairing.paired ? STR.planRepairTip : STR.planPickOtherTip;

  const choose = document.createElement("select");
  choose.className = "plan-pick-list";
  choose.hidden = true;
  const none = document.createElement("option");
  none.value = "";
  none.textContent = STR.planPickNone;
  choose.append(none);
  for (const file of pairing.files) {
    const option = document.createElement("option");
    option.value = file.signature;
    option.textContent =
      file.claimedBy && file.claimedBy !== pairing.position
        ? `${file.file} — ${STR.planPickClaimed(file.claimedBy)}`
        : file.file;
    option.selected = file.signature === pairing.signature;
    choose.append(option);
  }
  open.addEventListener("click", () => {
    choose.hidden = !choose.hidden;
    if (!choose.hidden) choose.focus();
  });
  // Committing on `change` and not on a second button: the list is the question
  // and choosing is the answer. Choosing what is already there says nothing, so
  // it sends nothing.
  choose.addEventListener("change", () => {
    if (choose.value === (pairing.signature || "")) {
      choose.hidden = true;
      return;
    }
    pairFile(pairing.position, choose.value);
  });
  holder.append(open, choose);
  return holder;
}

/** Record that one file is one track — or, with an empty signature, that it is not. */
async function pairFile(position, signature) {
  await sendCorrections(null, { [String(position)]: signature });
}

/** The part of a name that is derived, not typed: quiet, and never a field. */
function planAffix(text) {
  const affix = document.createElement("span");
  affix.className = "plan-affix";
  affix.textContent = text;
  return affix;
}

/** One typeable piece of a name: the text at rest, the field, and the pencil.
 *
 * An element of its own rather than the whole cell, because a compilation's row
 * holds two of them — the credit and the title — and only one may be open at a
 * time. Each carries its own pencil, next to the piece it opens: one pencil for
 * two fields would be a control with no way to say which.
 *
 * ``silent`` is the row whose written name could not be split. The cell already
 * says that name in full, so the piece contributes no text of its own — only a
 * way in.
 */
function planField({ key, value, hint, dataset, placeholder = "" }, { silent = false } = {}) {
  const field = document.createElement("span");
  field.className = "plan-field";

  const part = document.createElement("span");
  part.className = "plan-part";
  const rest = () => {
    part.textContent = silent ? "" : input.value || placeholder;
    part.classList.toggle("is-placeholder", !silent && !input.value && Boolean(placeholder));
  };

  const input = document.createElement("input");
  input.type = "text";
  input.value = value || "";
  // Read-only is the resting state; the pencil is the only way in.
  input.readOnly = true;
  input.autocomplete = "off";
  input.spellcheck = false;
  // Said in the empty field as well as in the text beside it: a compilation
  // track the catalogue credits to nobody offers a field with nothing in it,
  // and an empty box says nothing about what belongs there.
  input.placeholder = placeholder;
  input.dataset.applied = value || "";
  input.title = hint;
  // Sized to its own text, so the extension it sits before stays beside it and
  // the row keeps reading as one name rather than two ends of a line.
  input.size = Math.max(10, (value || "").length + 2);
  for (const [name, held] of Object.entries(dataset)) input.dataset[name] = held;
  input.addEventListener("blur", (event) => {
    endEdit(field);
    rest();
    sendIfEdited(event);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      input.blur();
    } else if (event.key === "Escape") {
      // Leaving is committing, so leaving by mistake must be undoable: Escape
      // puts back what was there and the blur that follows sends nothing.
      event.preventDefault();
      input.value = input.dataset.applied;
      input.blur();
    }
  });
  rest();

  const pencil = document.createElement("button");
  pencil.type = "button";
  pencil.className = "plan-pencil";
  pencil.textContent = "✎";
  // The tooltip says what the field does and what it costs, not what it is: a
  // title typed here reaches the tag and the file name, not only this row, and
  // a credit reaches the tag already split.
  pencil.title = hint;
  // The key travels on the pencil too, so an edit in flight knows that the
  // caret was on its way to this field and can reopen it after the redraw.
  pencil.dataset.key = key;
  pencil.addEventListener("click", () => beginEdit(field));

  field.append(part, input, pencil);
  return field;
}

/** What this file is, said in the row that shows its name.
 *
 * One flag with one meaning — *this audio was lossy before it arrived* — lit
 * when that is what stands, whoever said so. Clicking asserts the opposite;
 * when the opposite is what the measurement already found, the stored verdict
 * is withdrawn rather than a second one stored, so `said` never drifts into
 * agreeing with the very thing it was meant to overrule.
 *
 * Silent by default and revealed on hover, exactly like the pencil: a plan is
 * read for its names, and a lit control on every row would be read instead.
 */
function qualityWord(quality) {
  const holder = document.createElement("span");
  holder.className = "plan-quality";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "plan-quality-button";
  button.textContent = "⚑";
  button.classList.toggle("is-fake", Boolean(quality.marked));
  button.classList.toggle("is-said", Boolean(quality.said));
  button.title = qualityTip(quality);
  button.setAttribute("aria-label", button.title);
  button.addEventListener("click", async () => {
    button.disabled = true;
    const wanted = !quality.marked;
    const verdict =
      wanted === Boolean(quality.measured) ? "clear" : wanted ? "transcoded" : "honest";
    const response = await api().set_track_quality(quality.unitId, quality.signature, verdict);
    if (!response.ok) {
      button.disabled = false;
      toastError(response.error);
      return;
    }
    // The plan is redrawn from what came back, so the label leaves the file
    // name and the folder name in the same breath.
    renderAlbumDialog(response.album);
    renderGrid();
  });
  holder.append(button);
  return holder;
}

/** Say what one click would do here, in terms of what gets written. */
function qualityTip(quality) {
  return quality.marked ? STR.tipSayHonest : STR.tipSayTranscoded;
}

/** Open one piece of a name for typing, and put the caret where the eye is. */
function beginEdit(field) {
  if (!field) return;
  const input = field.querySelector("input");
  if (!input) return;
  // One field open at a time, across the whole table. Leaving a field is what
  // sends it, so a field still open is a field still unsent: two of them on
  // screen would be two claims about what the plan holds. A compilation's row
  // has two, and they take turns for the same reason.
  for (const open of document.querySelectorAll(".plan-field.is-editing")) {
    if (open !== field) endEdit(open);
  }
  field.classList.add("is-editing");
  // The cell is marked too, and only so that the name it produces can be said
  // underneath while anything in it is open.
  const cell = field.closest(".plan-cell");
  if (cell) cell.classList.add("is-editing");
  input.readOnly = false;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

/** Close one piece back to text, and the cell with it once nothing is open. */
function endEdit(field) {
  if (!field) return;
  field.classList.remove("is-editing");
  const cell = field.closest(".plan-cell");
  if (cell && !cell.querySelector(".plan-field.is-editing")) cell.classList.remove("is-editing");
  const input = field.querySelector("input");
  if (input) input.readOnly = true;
}

/** Whether `Approve & apply` is a gesture that can run for this album right now.
 *
 * Read by the dialog when it draws and by `approveAlbum` when it lets go, so the
 * button is closed by one rule however it came to be drawn.
 */
function approveIsClosed(album) {
  const partOnly = !album.applicable && Boolean(album.partial_applicable);
  return (
    state.approving ||
    state.identifying.has(album.unit_id) ||
    !(album.applicable || partOnly) ||
    album.written
  );
}

function renderAlbumDialog(album) {
  state.openAlbum = album;
  renderSharedAudio(album);
  $("album-title").textContent = album.title
    ? `${album.artist} — ${album.title}`
    : album.folder;
  // The edition's own facts — format, country, label. Every edition of one
  // album shares a title and a year, so without these the header reads
  // identically before and after "Use this".
  //
  // Not for an arrangement: its "release" is the album's own files, which have
  // no country, no label, and an "edition" derived from its own year — edition
  // facts about a pressing nobody ever named.
  // The source is not named here: the header and the source panel both name
  // it, as links and in the window's own spelling.
  // The verdict first, then what the record is. This line lives in the
  // dialog's head rather than in the fold: the match share is the largest
  // single thing the screen says about the identification, and the label, the
  // kind of record and the pressing are what a match is checked against.
  // Neither is about the *matching*, which is what the fold below keeps.
  //
  // It does not repeat the line above it either: the header already draws the
  // year and the track count.
  const arranged = album.source === "tags";
  const releaseFacts =
    album.release && !arranged
      ? candidateFacts(album.release, { saidYear: album.year, brief: true })
      : [];
  const standing = $("album-standing");
  standing.replaceChildren();
  const badge = matchBadge(album);
  badge.id = "album-match";
  if (badge.textContent) standing.append(badge);
  // Not `said`: this function already has one further down, and a `const`
  // declared twice in one scope is a SyntaxError that stops the whole script.
  const factsLine = releaseFacts.filter(Boolean).join(" · ");
  if (factsLine) {
    const facts = document.createElement("span");
    facts.className = "album-standing-facts";
    facts.textContent = factsLine;
    standing.append(facts);
  }
  standing.hidden = standing.children.length === 0;
  // The blockers are listed below, in red, one per line, so they are not
  // repeated here: the heading says what the match was instead.
  // The evidence line, not the whole explanation. What the server sends as
  // `evidence_line` is the same sentence with the three facts this dialog
  // states elsewhere taken out: the titles, which the source panel gives beside
  // a link; the track counts, which the tags line and a blocker both give; and
  // the lengths, which are the proof line below. Read and never reassembled
  // here.
  //
  // It is drawn on every album, not only where the plan has blockers, so the
  // place that answers *why this is the right record* does so for an organized
  // album too. The state sentence is what is left where there is no evidence
  // to state — an album nothing has been compared for, or one whose lookup
  // failed — and where the album is held by hand, because that is a decision
  // and not something a match measured.
  const reason = $("album-reason");
  reason.textContent = album.held
    ? album.reason || ""
    : album.evidence_line || album.explanation || album.reason || "";
  reason.hidden = !reason.textContent;
  // One short line, and only when the audio was actually asked. It goes here,
  // under the release, because it is a statement about *what this album is* —
  // the same question the line above answers — rather than another paragraph
  // in a crowded dialog.
  const heard = {
    unknown: "acousticUnknown",
    named: "acousticNamed",
    unavailable: "acousticUnavailable",
  };
  const acousticLine = $("album-acoustic");
  // Never silent: an album that settled on its names says so, which is the
  // difference between a feature that did not run and a feature that cannot be
  // found. Except on an arrangement, where "the names settled this album" would
  // claim a contest the fingerprint never entered — nothing settled anything,
  // because nothing was asked.
  const said = heard[album.acoustic] || "acousticNotAsked";
  acousticLine.hidden = arranged;
  acousticLine.textContent = arranged ? "" : STR[said];
  // An album the fingerprint found says so with the weight of an answer; the
  // other two cases are notes about an attempt, and stay quiet.
  acousticLine.className = album.acoustic === "named" ? "reason" : "muted";
  acousticLine.title = album.acoustic === "named" ? STR.tipAcousticNamed : "";

  renderEssentials(album);
  renderRating(album);

  // The files that said something else, under the arranged sentence. An
  // arrangement is a majority's answer, and a majority is a choice — what it
  // chose against is the one thing worth reading before approving.
  const dissentList = $("album-dissent");
  const dissent = (album.tag_reading || {}).dissent || [];
  dissentList.hidden = !arranged || !dissent.length;
  dissentList.replaceChildren(
    ...dissent.map((line) => {
      const item = document.createElement("li");
      item.textContent = line;
      return item;
    }),
  );

  // The catalogue's answer read against what the files' own tags say — one
  // line, always. "Agrees" settles as much as a difference unsettles, so
  // agreement is not silence.
  const tagsSay = $("album-tags-say");
  const said_by_tags = album.tags_say || {};
  if (said_by_tags.agrees === undefined) {
    tagsSay.hidden = true;
    tagsSay.textContent = "";
  } else {
    tagsSay.hidden = false;
    // Amber marks what would be written, and this line writes nothing:
    // `is-differing` is grey in the stylesheet, so the colour is decided in
    // one place.
    tagsSay.className = said_by_tags.agrees ? "tags-say is-agreeing" : "tags-say is-differing";
    tagsSay.textContent = said_by_tags.agrees
      ? STR.tagsAgree
      : STR.tagsDiffer(said_by_tags.diffs || []);
  }

  drawDialogCover(album.unit_id);
  if (!state.covers.has(album.unit_id)) {
    // The dialog can open on an album whose card never scrolled into view, so
    // its picture was never asked for — and a frame reading *Choose a cover…*
    // over an album that has one is the screen naming a cause it did not
    // measure. Asked here, and drawn when it answers.
    const unitId = album.unit_id;
    requestCover(unitId).then(() => {
      if (state.openAlbum?.unit_id === unitId) drawDialogCover(unitId);
    });
  }

  const proof = $("album-proof");
  const proofLabel = { audio: STR.proofAudio, titles: STR.proofTitles, text: STR.proofText }[
    album.proof
  ];
  // Only what proved it. The witnesses are named in the header, where they are
  // links, so they are not repeated here.
  //
  // And nothing proved it where nothing matched: `matched on text alone` is the
  // notice's fact in another wording, since text alone is what is left when no
  // name and no length agreed. It keeps its words on every album where
  // something was proved.
  //
  // The evidence sentence above does not state the lengths — the server
  // subtracts them — so this line is the one place they are spoken of, on
  // every album.
  //
  // Named for what it decides rather than for how it looks: `quiet` is a word
  // this file already uses for a button style, and a vague local inside a long
  // render is how a name comes to mean two things.
  const provedNothing = nothingMatchedHere(album);
  proof.hidden = provedNothing || !(proofLabel && album.release_id);
  proof.textContent = !provedNothing && proofLabel && album.release_id ? proofLabel : "";
  proof.className = `proof-line proof-${album.proof}`;
  // What that phrase means, for anyone who reads "lengths match" and wonders
  // whether the audio was fingerprinted. It was not — that is its own line.
  proof.title =
    { audio: STR.tipProofAudio, titles: STR.tipProofTitles, text: STR.tipProofText }[
      album.proof
    ] || "";
  const evidence = album.evidence || [];
  // One surface per album: the durations and the delta sit in the plan's own
  // rows, so a second table of the same tracks stays hidden.
  $("evidence-details").hidden = true;
  $("ev-head-files").textContent = STR.evYourFiles;
  $("ev-head-tracks").textContent = STR.evReleaseTracks(album.source);
  $("evidence-body").replaceChildren(
    ...evidence.map((row) => {
      const line = document.createElement("tr");
      line.className = `ev-${row.status}`;
      const seconds = (ms) => (ms == null ? "—" : `${Math.round(ms / 1000)}s`);
      const cells = [
        row.file || "—",
        seconds(row.file_ms),
        row.status === "paired" ? STR.evidencePaired(row.delta_ms) :
          row.status === "unmatched_file" ? STR.evidenceFileOnly : STR.evidenceTrackOnly,
        seconds(row.track_ms),
        row.track ? `${String(row.position || "").padStart(2, "0")} ${row.track}` : "—",
      ];
      line.replaceChildren(
        ...cells.map((text) => {
          const cell = document.createElement("td");
          cell.textContent = text;
          return cell;
        }),
      );
      return line;
    }),
  );

  // A blocker and its warning say the same thing about the same files. When
  // the album can be applied in part, the warning is the true statement — the
  // files are not blocking anything, they are being left alone — so the red
  // list gives way rather than repeating it.
  const lines = (node, texts) =>
    node.replaceChildren(
      ...texts.map((text) => {
        const item = document.createElement("li");
        item.textContent = text;
        return item;
      }),
    );
  // The dialog shows the album's own blockers and plan plainly — a plan that
  // only partly matches is a blocked plan, said as such, not a smaller offer.
  // What blocks the whole plan is not what blocks the offered one: when the
  // partial plan is what Approve would run, the alignment's complaints are
  // warnings about files left alone, not refusals.
  const offeringPart = !album.applicable && Boolean(album.partial_applicable);
  // What the names said, where it is read first. Fewer than half of them
  // meeting is the fact that decides whether the plan below is about this
  // record at all, so it must not be a clause in a grey paragraph of details
  // under a green line saying the lengths agreed.
  // A record nothing matched gets one amber sentence, and everything else goes
  // quiet. Amber is a claim on attention, and two of them compete; the fact is
  // said once, and the ways out once, in grey.
  const wrongRecord = nothingMatchedHere(album);
  $("album-wrong-record").hidden = !wrongRecord;
  renderWitnessWay(album, wrongRecord);
  const names = $("album-names-disagree");
  const compared = album.names_compared || 0;
  const agreeing = album.names_agreeing || 0;
  names.hidden = wrongRecord || !(compared > 0 && agreeing / compared < 0.5);
  names.textContent = names.hidden ? "" : STR.namesDisagree();
  // The blockers stand in the album's record untouched — what is quieted is
  // the drawing, because each of those sentences is the notice's fact again
  // with a list appended, and the lists are the table and the details.
  lines($("album-blockers"), offeringPart || wrongRecord ? [] : album.blockers);
  // The plan's own warnings belong here too. A library folder that already
  // holds this album's name is exactly such a warning: the album is organized
  // where it stands.
  // One list, not two. The offered plan's own warnings say everything it leaves
  // behind, the doubt included. Adding the blocked plan's reasons beside them
  // would print the same alignment twice in two voices.
  lines($("album-warnings"), [
    ...(offeringPart ? album.partial_warnings || [] : []),
    ...(album.warnings || []),
  ]);

  // The plan the button would actually run: what is shown is what gets
  // written, so there is no second surface to disagree.
  const shown = offeringPart ? album.partial_operations || [] : album.operations;
  renderPlanEditor(album, shown);
  // The names live in the editor above; this list carries what has no name to
  // edit — the tags, and the pictures.
  const rest = $("plan-editor").hidden
    ? shown
    : shown.filter((operation) => !operation.kind.startsWith("rename_"));
  const operations = $("album-operations");
  // Named once because two things read it: the line below, and the note above
  // the rows, which is drawn much further down.
  const nothingIsListed = shown.length === 0 && album.blockers.length === 0;
  if (nothingIsListed) {
    const item = document.createElement("li");
    // Only ever read by an album that *was* compared: one that was not has this
    // whole fold taken away below, and reads the notice instead.
    item.textContent = STR.emptyPlan;
    // The hover explains why rows are drawn for an album that changes nothing.
    // The note that would carry it is not on screen when this line is, so it
    // comes here.
    item.title = STR.tipPlanNoteUnchanged;
    operations.replaceChildren(item);
  } else {
    operations.replaceChildren(...inReadingOrder(foldTagWrites(rest)).map(operationLine));
  }
  // A blocked album has no operations because the planner refused to make any,
  // which is the opposite claim from "every name already reads as it should".
  // An empty plan means two different things and the screen has to say which
  // one.
  //
  // An album whose partial plan is the one on offer is not blocked: the full
  // plan's blockers were demoted to warnings above, the rows on the right are
  // real renames, and Approve would run them. Blocked means *nothing is on
  // offer*, the same question `offeringPart` already answered.
  const blocked = !offeringPart && album.blockers.length > 0;
  $("plan-summary").textContent = blocked
    ? STR.planSummaryBlocked
    : STR.planSummary(shown.length);
  // Open whenever there is a table to read, not only when something changes.
  // "Nothing would change" is a claim, and folding the evidence away behind it
  // asks to be believed; the same `now → after` rows that show a rename are
  // what show that a name is already right.
  $("plan-details").open = shown.length > 0 || !$("plan-editor").hidden;
  // The sentence stands directly above the editor and speaks about it, so it is
  // chosen by whether that editor has names on screen — not by whether the plan
  // has operations of any kind. A plan of artwork writes alone has operations
  // and no name at all.
  const namesOnScreen = !$("plan-editor").hidden;
  // The provenance the header claims stops being true after a renumbering. An
  // insertion moves tracks the catalogue numbered, so the folder ends up
  // carrying numbers that disagree with the release named at the top of this
  // dialog. That is said here, above the names it is about, and only when it is
  // true — an appended track renumbers nothing and gets no notice.
  // What a chosen cover would do, said before it does it.
  const choice = album.cover_choice;
  $("cover-choice").hidden = !choice;
  if (choice) {
    $("cover-choice-words").textContent =
      STR.coverWaiting(choice.files, choice.icons || 0, choice.width, choice.height)
      + (choice.replaces ? STR.coverWaitingReplaces(choice.replaces) : "");
    // The picture itself, beside the sentence about it: a size and a count
    // cannot tell a sleeve from a logo.
    $("cover-choice-preview").hidden = !choice.preview;
    if (choice.preview) $("cover-choice-preview").src = choice.preview;
  }
  const numbering = $("plan-numbering");
  numbering.hidden = !album.user_numbering;
  numbering.textContent = album.user_numbering ? STR.planYourNumbering : "";
  // The album's way out of its corrected titles, offered only while it has any
  // and only where the names are on screen — a line about the table has nothing
  // to say above a plan that is artwork alone.
  const corrected = album.title_corrections || 0;
  $("plan-yours-standing").hidden = !corrected || !namesOnScreen;
  $("plan-yours-standing-words").textContent = corrected
    ? STR.planYoursDropAllWords(corrected)
    : "";
  // One fact, one sentence. This note is a caption for the rows beneath it,
  // and where there are no rows it would be a caption over nothing, repeating
  // `emptyPlan` and the heading. The condition is the list's own, not a second
  // one worked out here.
  // Written through `$("plan-note")` rather than through a local, because two
  // guards read this assignment out of the file by name.
  $("plan-note").hidden = nothingIsListed;
  $("plan-note").textContent = blocked
    ? STR.planNoteBlocked
    : !namesOnScreen
      ? STR.planNoteWithoutNames
      : shown.length > 0
        ? STR.planNote
        : STR.planNoteUnchanged;
  // The hover belongs to the sentence, so it is written on the same pass and
  // emptied for every reading. Set once and left standing, it would explain the
  // unchanged album while the blocked one was on screen — the unchanged
  // album's own hover travels with `emptyPlan`.
  $("plan-note").title = "";

  // An album nothing has been compared for is the one case where those rows do
  // not exist to be read. The fold is taken away rather than left saying
  // "Nothing would change" — which is the sentence of an album that was read
  // and found already right, and is the opposite claim from this one.
  const unscanned = album.looked_at === false;
  // The notice is for the album nobody has asked about, which is not the same
  // as an album whose outcome no catalogue answered. Arranging by tags after a
  // scan produces the second, and the banner over it would say *no catalogue
  // has been asked* about an album that was scanned and set aside on purpose.
  $("album-unscanned").hidden = !unscanned || Boolean(album.catalogue_asked);
  // The notice and the table, together. An album that has not been looked up
  // can still have a plan: a dropped folder comes back arranged from its own
  // tags. Removing the fold there would hide a plan that can be applied behind
  // a notice saying there is no before-and-after to read. The fold goes only
  // when there is nothing in it.
  $("plan-details").hidden = unscanned && shown.length === 0;

  const candidates = $("album-candidates");
  if (album.candidates.length > 0) {
    candidates.replaceChildren(...album.candidates.map(candidateLine));
  } else {
    // An identified album says nothing here: the header already names the
    // source it is identified by, and the button that finds other pressings
    // sits right above. The album that found *nothing* still says so, because
    // there the emptiness needs a reason.
    candidates.replaceChildren();
    if (!album.release_id) {
      const item = document.createElement("li");
      item.className = "muted";
      item.textContent = STR.noCandidates;
      candidates.append(item);
    }
  }

  $("search-artist").value = album.artist || "";
  $("search-album").value = album.title || "";
  $("paste-link-url").value = "";
  // The footer carries the decisions about the plan. The other ways back in —
  // planning again, arranging by tags — are entries of the footer's ⋯, built
  // from this album when it is opened, so their rules live in `albumMoreItems`
  // beside the states they read. The never-scanned album asks from the middle
  // of its own notice, where the plan would have been.
  // The scan is in the footer because it is a decision about the plan — the
  // one that asks for a plan to exist — and a dialog is often opened on an
  // album that has not been scanned. Never primary beside another primary,
  // because two filled buttons are two invitations at once.
  // Two gestures, one slot, and the same condition decides both — written once
  // so they cannot both appear or both vanish. An album this app has finished
  // with has nothing to scan for; one it has not has nothing to plan again.
  const finishedWith = Boolean(album.organized || album.applied);
  $("album-scan-now").hidden = finishedWith || unscanned;
  // A scan that is running says so inside the dialog. The dialog does not
  // close itself when a scan starts, so the shelf's progress bar behind it is
  // not a visible sign.
  //
  // Closed while it runs, for the same reason: a second press could only ever be
  // refused, and a control whose one outcome is a refusal should not be live.
  // Declared here, above the first line that reads it: `const` has a temporal
  // dead zone, so reading it earlier in the function is a `ReferenceError` that
  // takes the whole dialog down rather than one control with it.
  const beingLookedUp = state.identifying.has(album.unit_id);
  const scanning = $("album-scan-now");
  scanning.disabled = beingLookedUp || Boolean(state.scanning);
  scanning.classList.toggle("is-working", beingLookedUp);
  scanning.textContent = beingLookedUp ? STR.scanningThisAlbum : STR.scanThisAlbumAgain;
  $("album-plan-again").hidden = !finishedWith;
  // Only where a gesture is actually waiting to be taken back: an entry that
  // always fails is worse than an absent one. The API answers this — the
  // window never works out for itself whether a way back exists, because the
  // thing that would have to be undone is on that side.
  $("album-cancel").hidden = !album.can_cancel;
  // An album whose files and tracks did not all pair up must still be
  // approvable, or it stays in review for ever: Approve reaches the partial
  // plan.
  //
  // Only the *partial* plan, never an ambiguous one: `is_partial` means real
  // pairings exist, none of them a guess, and what is missing is missing
  // rather than uncertain. A file that fits two tracks still blocks, because a
  // wrong rename is worse than no rename.
  const partOnly = !album.applicable && Boolean(album.partial_applicable);
  // Hidden because there is nothing to write, not because the album is
  // organized. The two are different sentences: a re-plan can find work on a
  // finished album — artwork writes, for example — and that work needs a
  // button. Every door to Approve asks the same thing: whether the plan this
  // click would run has anything in it.
  const wouldRun = (partOnly ? album.partial_operations : album.operations) || [];
  $("album-approve").hidden = album.organized && (album.written || wouldRun.length === 0);
  $("album-reject").hidden = album.organized;
  // And never while this album is being looked up. The dialog stays open
  // during a re-scan, and the plan on screen during those seconds is the one
  // the scan is in the middle of replacing — approving it would write files
  // from a reading this app had already decided to redo.
  $("album-approve").disabled = approveIsClosed(album);
  $("album-approve").textContent = partOnly
    ? STR.approvePart(album.partial_matched, album.tracks)
    : STR.approve;
  // Closed over an album that is already organized as well: the API refuses it
  // there, and a control that always fails is worse than an absent one. What
  // to press instead is in the same row — `Cancel` for the proposal, and the
  // History's `Revert` for the files.
  $("album-reject").disabled =
    album.applied || album.organized_on_record || beingLookedUp;
  // The way out of an identification nothing believes, in the row rather than
  // only behind the ⋯. `review` is this application's own word for not being
  // convinced, so this hangs off it and invents no threshold.
  $("album-arrange-tags").hidden = album.decision !== "review" || album.written;
  // Which of the two is the invitation. While no catalogue has been asked
  // about this album, what is on offer was worked out from its own tags on the
  // way in, and the gesture that would change that is the scan — so the scan
  // is the filled button there and `Approve & apply` is not. They swap, and
  // only here: two filled buttons would be two invitations at once.
  // Read from the album rather than from the button beside it: the scan is
  // offered from the footer for an album in review and from the notice for one
  // nothing has looked up.
  // The invitation may never land on a button that is not there. The scan is
  // hidden for an album this app has finished with and for one it has never
  // looked at, so handing it the amber unconditionally would leave such a row
  // with *no* filled button and nothing saying what comes next.
  // And a plan the tags produced answers the question the scan was for. Once
  // the tags have arranged the album, the row holds a plan that renames files,
  // and the question is whether to write it. `Approve & apply` being pressable
  // is exactly *there is a plan here* — it is disabled while nothing is
  // applicable, while the album is being looked up, and once the plan is
  // written — so it is the question asked, rather than a second reading of the
  // same facts.
  const noSourceAsked = !album.source || album.source === "tags";
  const scanIsOffered = !$("album-scan-now").hidden;
  const approveCanRun = !$("album-approve").disabled;
  const scanIsTheInvitation = noSourceAsked && scanIsOffered && !approveCanRun;
  $("album-approve").classList.toggle("button-primary", !scanIsTheInvitation && approveCanRun);
  $("album-scan-now").classList.toggle("button-primary", scanIsTheInvitation);
  // The album nothing could settle, and the one gesture that settles it. Only
  // when every file did pair with a track — an album with files left over is
  // missing something, not merely undecided, and confirming would be vouching
  // for pairings that do not all exist.
  const contested =
    !album.applicable &&
    !album.applied &&
    !album.organized &&
    (album.blockers || []).some((line) => line.includes("fits more than one")) &&
    (album.evidence || []).every((row) => row.status === "paired") &&
    (album.evidence || []).length > 0;
  $("album-contested").hidden = !contested;
  renderAlbumSteps(album);

  if (!$("album-dialog").open) $("album-dialog").showModal();
}

/** Say where this album sits on the shelf, and offer the two steps.
 *
 * The shelf as it is drawn, which is `visibleAlbums()` — the current filter,
 * search and order. Never a set of the dialog's own: walking a list the screen
 * behind does not show would be the app operating the user's controls, and
 * filtering the Library by `In review` is what turns this into a review queue.
 *
 * An album that is not in that list — opened from a notice, or filtered out by
 * something typed since — has no position to state, so the control says
 * nothing at all rather than counting from a place that cannot be seen.
 */
function renderAlbumSteps(album) {
  const shelf = visibleAlbums();
  const at = shelf.findIndex((one) => one.unit_id === album.unit_id);
  const previous = $("album-previous");
  const next = $("album-next");
  const position = $("album-position");
  const known = at >= 0 && shelf.length > 1;
  previous.hidden = !known;
  next.hidden = !known;
  position.hidden = !known;
  if (!known) {
    position.textContent = "";
    return;
  }
  position.textContent = STR.albumPosition(at + 1, shelf.length);
  // Stopped at the ends rather than wrapping: the last album of a review pass
  // is the end of the pass, and a control that quietly starts over shows the
  // same album twice.
  previous.disabled = at === 0;
  next.disabled = at === shelf.length - 1;
}

/** Open the album one step away on the shelf, in the direction pressed. */
async function stepThroughShelf(step) {
  const open = state.openAlbum;
  if (!open) return;
  const shelf = visibleAlbums();
  const at = shelf.findIndex((one) => one.unit_id === open.unit_id);
  const going = shelf[at + step];
  // `at` of -1 makes `shelf[-1 + 1]` the first album, which would answer a
  // press about an album that is not on the shelf by opening one that is.
  if (at < 0 || !going) return;
  // Walking away is leaving. A finished album planned again goes back to how
  // it was on every route out, and the arrows are one: left standing, its
  // proposal would draw it on the shelf as `Auto`.
  if (open.leaving_takes_back) {
    const response = await api().cancel(open.unit_id);
    if (!response.ok) toastRefusal(response.error);
    else if (response.kept_words) toast(STR.cancelledKeepingWords);
  }
  await withSpinner(step < 0 ? $("album-previous") : $("album-next"), () =>
    openAlbum(going.unit_id),
  );
}

/** The album dialog's ⋯: the ways back in, read from the album that is open.
 *
 * Built when the menu opens rather than kept as buttons, so each entry's rule
 * sits here beside the state it reads.
 */
function albumMoreItems() {
  const album = state.openAlbum || {};
  const unscanned = album.looked_at === false;
  const items = [];
  // `Plan this album again`, `Arrange by its own tags` and `Choose a cover…`
  // are not here: each has its home elsewhere in the dialog — the footer, and
  // under the picture — and one gesture has one home.
  // Relocating, only on the album that needs it. The automatic search looks
  // for this folder's own name in the places it knows, so it finds a move and
  // never a rename — and a rename is what this app itself does.
  if (album.folder_missing) items.push({ label: STR.relocateThis, run: relocateAlbum });
  // Key and tempo, read from this album's own audio. Here rather than on the
  // Mixing screen because the rule is one album that is pointed at, and this
  // dialog is where one already is.
  items.push({
    label: STR.measureHarmonics,
    run: () => measureHarmonics([album.unit_id]),
  });
  // `Send to review` is here rather than in the footer: it is the rarer,
  // quieter question, and the footer keeps its four.
  //
  // Offered only where there is something to hold back: not for an album
  // already organized, already applied, already held, or that the app was
  // never going to touch on its own.
  if (
    !album.organized &&
    !album.applied &&
    !album.held &&
    album.decision === "automatic"
  ) {
    items.push({ label: STR.hold, title: STR.tipHold, run: holdAlbum });
  }
  return items;
}

/** Draw what is left of an album whose folder this app cannot read.
 *
 * Everything said here comes from the answer: the API is the side that knows
 * which folder is missing and whether the same audio is on record somewhere that
 * still exists. A screen that worked either out for itself would be a second
 * rule for one thing.
 */
function showAlbumGone(unitId, answer) {
  const gone = $("album-gone-dialog");
  gone.dataset.unitId = String(unitId);
  const folder = String(answer.folder || "");
  $("album-gone-title").textContent = folder.split("/").filter(Boolean).pop() || STR.albumGone;
  $("album-gone-message").textContent = answer.error || "";
  // Pointing it somewhere keeps the album; letting the card go is the answer
  // when the twin makes this one a duplicate. Both are the card's own gestures,
  // reached from here because the card is where an album with no folder lives.
  if (!gone.open) gone.showModal();
}

async function openAlbum(unitId, { openShared = false } = {}) {
  const response = await api().album(unitId);
  if (!response.ok) {
    // Said out loud, and answered `false`. Returning in silence would let the
    // three `…FromMenu` gestures go on to read `state.openAlbum` — which is
    // still the album that was open *before* — so choosing `Reject` on an
    // album whose folder had moved would reject a different album.
    // An album whose folder is gone gets a screen, not only a sentence. The
    // dialog it cannot have is the one built around a folder; what it can have
    // is the two repairs, which otherwise live only in the card's menu.
    if (response.folder_is_gone) {
      showAlbumGone(unitId, response);
      return false;
    }

    toastError(response.error || STR.albumGone);
    return false;
  }
  if (!state.openAlbum || state.openAlbum.unit_id !== unitId) {
    // Whatever the last album's search said belongs to that album, not this
    // one: the status line and the adopted marker are per-album state.
    state.adopted = null;
    findStatus("", false);
  }
  renderAlbumDialog(response.album);
  // Opened by the badge, which offers the chance to resolve rather than only
  // to be told.
  if (openShared) $("shared-details").open = true;
  return true;
}

// --- gestures -------------------------------------------------------------

/** Whether a fresh answer about this album may be drawn in the open dialog.
 *
 * Two events carry one: the witness's late reply, and the identification a
 * re-scan produces while the dialog stays open. The rule is written once
 * because it is one rule, and both callers must hold it.
 *
 * Never over a field that is being typed in. A redraw replaces what is on
 * screen, and a correction half typed would be taken away by an answer nobody
 * asked to watch. It waits for the next time the album is opened.
 */
function canRedrawOpenAlbum(unitId) {
  if (unitId === undefined || !$("album-dialog").open) return false;
  if (state.openAlbum?.unit_id !== unitId) return false;
  return !(
    $("album-dialog").contains(document.activeElement)
    && ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)
  );
}

/** Draw an answer that arrived complete, for the album that is open.
 *
 * The witness's reply is a whole dialog payload and is drawn as it stands. It
 * is deliberately **not** re-asked for: `album()` starts the witness question
 * again on its way out, so a redraw that fetched would answer itself for ever.
 */
function redrawOpenAlbum(album) {
  if (!album || !canRedrawOpenAlbum(album.unit_id)) return false;
  renderAlbumDialog(album);
  return true;
}

/** Mark every album an arrival brought in, whatever brought it.
 *
 * One rule, every arrival — a folder opened, folders dropped, a download
 * collected — so several albums brought in together can be scanned at once
 * without ticking each card, and a shelf never has some arrivals checked and
 * others not. Coming back from a restart is not an arrival and marks nothing:
 * arrivals were pointed at, and the shelf was not.
 */
function markArrivals(unitIds) {
  for (const unitId of unitIds || []) state.marked.add(unitId);
  renderMarkCount();
}

async function chooseFolder() {
  const folder = await withSpinner($("choose-folder"), () => api().choose_folder());
  if (!folder) return;
  state.root = folder;
  state.rootShown = true;
  renderRootPath();
  renderMarkCount();
  // Choosing a folder puts its albums on the shelf at once, marked, and Scan
  // looks them up; otherwise choosing would change nothing anyone could see.
  const response = await api().open_folder(folder);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  // A read, not a scan. It has its own counter and not `scanning`: beside a
  // real scan, its `opened` would switch that scan's bar off.
  state.opening += 1;
  renderActivity();
  ensurePolling();
}

async function startScan() {
  // The same reading the button was drawn from, so a press can never mean
  // something the label did not say.
  const marked = scannableMarks();
  if (!marked.length) {
    // Two different nothings, and they need different sentences: nothing is
    // ticked at all, or everything ticked is already organized and this gesture
    // has promised not to touch it.
    toastError(markedOnScreen().length ? STR.nothingToScan : STR.nothingMarked);
    return;
  }
  const response = await withSpinner($("scan"), () => api().scan_selected(marked));
  if (!response.ok) {
    // One sentence about the state of things, not a refusal listing paths:
    // it goes on its own, where an apply failure waits to be read.
    toastError(response.error);
    return;
  }
  scanBegan(marked);
}

/** Show that a scan is running, whichever gesture started it.
 *
 * Shared by every gesture that starts a scan: "Organize what I downloaded"
 * starts an ordinary scan of the download folder, and without this it would
 * run with no progress bar and no event polling — real and invisible, with a
 * later press on Scan refused by "a scan is already running".
 */
function scanBegan(units = []) {
  state.scanning = true;
  // The albums this run is about, so their cards can say they are being worked
  // on. A card whose badge goes from `not scanned` to `auto` several seconds
  // later with nothing in between reads as the app changing its mind.
  state.identifying = new Set(units);
  // The albums already on screen stay. With a Library that persists, emptying
  // the list here would make scanning one folder remove every other album
  // from the screen.
  state.reach = null;
  state.filter = "all";
  // `state.scanning` above is what closes the button; this says so in the one
  // place that owns it, so nothing else has to remember to open it again.
  renderMarkCount();
  $("apply-result").hidden = true;
  $("scan-done").hidden = true;
  $("progress").hidden = false;
  $("progress-fill").style.width = "0";
  $("progress-label").textContent = STR.scanning;
  renderGrid();
  renderActivity();
  pollEvents();
}

async function pollEvents() {
  let events;
  try {
    events = await api().events();
  } catch (error) {
    // The scan goes on without this window. One failed call across the bridge
    // must not end progress reporting for the rest of it, leaving the bar
    // frozen with nothing to do but restart.
    console.error("Polling failed; will try again.", error);
    // The same question as at the end of this function, answered by the same
    // function, so a failed poll during any job keeps the reporting alive.
    state.polling = worthPolling();
    if (state.polling) {
      setTimeout(pollEvents, pollDelay());
    }
    return;
  }
  for (const event of events) {
    if (event.type === "measuring") {
      $("progress-label").textContent = STR.measuring(0, event.payload.albums);
    } else if (event.type === "measured") {
      const { done, total } = event.payload;
      $("progress-fill").style.width = `${(done / total) * 100}%`;
      $("progress-label").textContent = STR.measuring(done, total);
    } else if (event.type === "scanned") {
      state.reach = event.payload.reach || null;
    } else if (event.type === "identified") {
      const { done, total, album } = event.payload;
      $("progress-fill").style.width = `${(done / total) * 100}%`;
      $("progress-label").textContent = STR.identifying(done, total);
      // This one has been answered for, so its card stops saying it is being
      // worked on. Before the badge is chosen, because the badge reads this.
      state.identifying.delete(album.unit_id);
      // Rescanning a folder that is already on screen re-reads its albums, and
      // pushing blindly would show each of them twice. An album is its unit id.
      const already = state.albums.findIndex((a) => a.unit_id === album.unit_id);
      if (already === -1) {
        state.albums.push(album);
        appendAlbum(album);
      } else {
        state.albums[already] = album;
        replaceAlbum(album);
      }
      // `Scan this album again` leaves the dialog open, so the open album is
      // redrawn in place — and `Cancel` is on that footer for as long as the
      // reading it replaced can be put back.
      //
      // Asked for, not taken from the event. What rides on `identified` is the
      // card's summary: no reason, no operations, no blockers, no way back.
      // Drawing the dialog from it would empty half of the screen — one album,
      // two answers, and the smaller one winning because it arrived last.
      if (canRedrawOpenAlbum(album.unit_id)) await openAlbum(album.unit_id);
    } else if (event.type === "finished") {
      state.scanning = false;
      // This session has now had a scan, which is what lets the line at the top
      // be about something that happened rather than about the album count.
      state.scanFinished = true;
      clearScanDoneLater();
      // The work asked for on those folders is done, so the marks that asked
      // for it are spent. Left standing, the next gesture would silently
      // inherit a selection made for the previous one.
      state.marked.clear();
      // Whatever the run did not report on, it is no longer working on. An album
      // left in this set would say `identifying…` for the rest of the session.
      state.identifying.clear();
      state.coversPlanned = event.payload.covers || 0;
      // The run's own count, not the shelf's. `albums` is what it read; the
      // ones it deliberately left alone are counted apart and the two are said
      // in one sentence.
      state.scanIdentified = (event.payload.albums ?? 0) - (event.payload.left_alone || 0);
      state.scanUntouched = event.payload.left_alone || 0;
      $("progress").hidden = true;
      $("progress-fill").style.width = "0";
      $("progress-label").textContent = "";
      renderMarkCount();
      // The chosen folder has been read, so the path stops being shown: left in
      // the frame it reads as a scan still pending on that folder. `state.root`
      // itself stays — it is what "Measure this library" measures, and what a
      // rescan of the same place needs — so this hides a finished instruction,
      // it does not forget the folder.
      state.rootShown = false;
      renderRootPath();
      // In a `finally`, because the announcement of a run's end must not be
      // downstream of a call that can fail. If `refresh()` throws while
      // drawing, the ending must still be said — otherwise the bar simply
      // vanishes with no line and no notice.
      try {
        await refresh();
      } finally {
        renderScanDone();
        // Said in the middle of the window, once, because the line that stays is
        // read on the way to something else rather than at the moment it happens.
        // What this run answered for, not how many albums the shelf holds:
        // `state.albums.length` is the whole shelf, and the count the run
        // emits is the only one that is about the run.
        flash(STR.scanCompleted(event.payload.albums ?? 0));
        // What the run could not answer for. Without this the run reports its
        // successes and an album it never reached — a marked one whose folder
        // had moved — says nothing at all.
        if (event.payload.misplaced) toastError(STR.scanMisplaced(event.payload.misplaced));
        // What it read and left alone on purpose is part of the line that
        // stays, rather than a notice of its own. `renderScanDone` above says
        // both numbers together.
        // Which sources were not part of this answer. A source that raises is
        // caught and treated as having found nothing, so the run goes on with
        // what is left. The albums that scan produced are as good as the
        // sources that answered, so which those were is said before any of
        // them is approved.
        sourceOutage(event.payload.sources_down);
        sayArrivedSameAudio(event.payload.same_audio);
        // *Organize what I downloaded* ends with the album open in its dialog,
        // so that it goes through the ordinary review before anything is
        // written.
        //
        // One album opens; several do not, because a dialog that picked one of
        // five would be the app choosing. They are all on the shelf either way.
        const brought = state.organizing;
        state.organizing = false;
        if (brought && state.albums.length) {
          const arrived = state.albums.filter((album) => album.origin === "downloaded");
          if (arrived.length === 1) openAlbum(arrived[0].unit_id);
        }
      }
    } else if (event.type === "harmonics-started") {
      // The ring and the bar, for the same reason every other worker job has
      // them: a job of minutes must not run saying nothing.
      state.measuringHarmonics = true;
      $("progress-fill").style.width = "0%";
      $("progress-label").textContent = STR.measuringAlbums(0, event.payload.albums);
      // Both indicators are owned by their own renderers; setting `hidden`
      // here would be undone by the next chrome pass.
      renderLibraryChrome();
      renderActivity();
    } else if (event.type === "harmonics-album") {
      const { done, total } = event.payload;
      $("progress-fill").style.width = `${(done / total) * 100}%`;
      $("progress-label").textContent = STR.measuringAlbums(done, total);
      // The wheel fills as albums land, so the Mixing screen is redrawn while
      // it is the one in front.
      if (state.tab === "mixing") refreshMixing();
    } else if (event.type === "harmonics") {
      state.measuringHarmonics = false;
      renderLibraryChrome();
      renderActivity();
      // The measurement runs on the worker thread, so its end is an event and
      // not a return value. Said out loud either way: a gesture that finishes
      // in silence is indistinguishable from a button that did nothing.
      const measured = event.payload.measured || 0;
      if (measured) {
        toast(STR.measuredHarmonics(measured, event.payload.albums || 1));
        refreshMixing();
      } else if (event.payload.no_ffmpeg) {
        // The run that never opened a file. `measuredNothing` is about audio
        // that was read and gave nothing back, and saying it here would blame
        // the files for what the missing binary did.
        toastError(STR.qualityUnavailable);
      } else {
        toastError(STR.measuredNothing);
      }
    } else if (event.type === "searching") {
      state.searching = true;
    } else if (event.type === "searched") {
      state.searching = false;
      $("find-button").disabled = false;
      if (event.payload.error) {
        findStatus(event.payload.error, false);
      } else {
        findStatus(STR.searchFinished(event.payload.found), false);
        if (state.openAlbum && state.openAlbum.unit_id === event.payload.unit_id) {
          state.openAlbum.candidates = event.payload.candidates || [];
          $("album-candidates").replaceChildren(
            ...state.openAlbum.candidates.map(candidateLine),
          );
        }
      }
    } else if (event.type === "quality_started") {
      $("quality-label").textContent = STR.qualityMeasuring(0, event.payload.albums);
    } else if (event.type === "quality_measured") {
      const { done, total, album } = event.payload;
      $("quality-fill").style.width = `${(done / total) * 100}%`;
      $("quality-label").textContent = STR.qualityMeasuring(done, total);
      state.quality.push(album);
      appendQuality(album);
    } else if (event.type === "quality_finished") {
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      toast(STR.qualityFinished(event.payload.albums, event.payload.suspect));
    } else if (event.type === "quality_error") {
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      toastError(event.payload.message);
    } else if (event.type === "bench_started") {
      state.qualityRunning = true;
      $("bench-add").disabled = true;
      $("quality-progress").hidden = false;
      // A walk has no total to count towards — it finds out how many albums
      // there are by finding them — so the bar reports what it has read rather
      // than pretending to know how far along it is.
      $("quality-fill").style.width = "100%";
      $("quality-label").textContent = STR.benchStarted(event.payload.folder);
    } else if (event.type === "bench_walking") {
      $("quality-label").textContent = STR.benchWalking(event.payload.albums);
    } else if (event.type === "bench_finished") {
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      toast(STR.benchFinished(event.payload.folder, event.payload.albums));
      refreshBench();
    } else if (event.type === "album") {
      // The witness answered. It is asked on a thread, because asking it
      // inline delays opening the dialog by seconds — so the answer arrives
      // after the screen was drawn and has to find its way onto the album it
      // belongs to, if that is still the one that is open.
      redrawOpenAlbum(event.payload.album);
    } else if (event.type === "bench_analyzing") {
      const { done, total, folder } = event.payload;
      state.qualityRunning = true;
      $("bench-add").disabled = true;
      $("quality-progress").hidden = false;
      $("quality-fill").style.width = `${total ? (done / total) * 100 : 0}%`;
      $("quality-label").textContent = STR.benchAnalyzing(done, total, folder);
    } else if (event.type === "bench_identifying") {
      const { done, total, folder } = event.payload;
      state.qualityRunning = true;
      $("bench-add").disabled = true;
      $("quality-progress").hidden = false;
      $("quality-fill").style.width = `${total ? (done / total) * 100 : 0}%`;
      $("quality-label").textContent = STR.benchIdentifying(done, total, folder);
    } else if (event.type === "bench_identified") {
      const { files, named, seconds } = event.payload;
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      toast(STR.benchIdentified(files, named, seconds));
      // The answers are per file, and the open album's table is what shows
      // them — so what was on screen is re-read rather than left stale.
      state.qualityTracks.clear();
      renderQuality();
    } else if (event.type === "bench_analyzed") {
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      showBenchAnalysis();
    } else if (event.type === "bench_error") {
      state.qualityRunning = false;
      $("quality-progress").hidden = true;
      $("bench-add").disabled = false;
      toastError(event.payload.message);
    } else if (event.type === "resumed") {
      // Whatever a sleeping machine broke was asked for again on the way in.
      toast(STR.transfersResumedOnOpen(event.payload.files));
      refreshTransfers();
    } else if (event.type === "adopted") {
      state.adopting = Math.max(0, state.adopting - 1);
      // What was just pointed at, so the shelf can put it in front whatever it
      // is sorted by. Dropping a record already on the shelf is exactly the
      // case a sort by date buries: the row is old, the gesture is not.
      // Recorded before the refresh, because the refresh draws the shelf.
      for (const unitId of event.payload.unit_ids || []) {
        state.pointedAt = [unitId, ...state.pointedAt.filter((id) => id !== unitId)];
      }
      // Marked, like every other arrival: what was new, and what was already
      // here but had moved. An album dropped again from the folder it already
      // lives in is not among them — it is named in the sentence below and
      // nothing else.
      markArrivals(event.payload.unit_ids);
      //
      // Timed: the API's half of an adoption rides on the event, and
      // everything from here to the album being on screen is this window's
      // half, so both are logged.
      const drawingFrom = performance.now();
      await refresh();
      console.info(
        `adopt: ${event.payload.seconds ?? "?"}s reading, ` +
          `${((performance.now() - drawingFrom) / 1000).toFixed(2)}s drawing, ` +
          `${state.albums.length} albums on the shelf`,
      );
      // A refusal has already said why, in its own toast. This event is here to
      // let go of the spinner, and a second sentence about albums would be
      // about a folder nothing ever read. `continue`, never `return`: this is a
      // loop over everything the bridge had queued, and leaving it early would
      // drop whatever came after.
      if (event.payload.refused) continue;
      // An album already on the shelf, dropped again, is named by floating to
      // the top of the shelf (`state.pointedAt` above) and by the toast below.
      // It is never named by typing into the search box: that hides every
      // other album, which is indistinguishable from the library emptying
      // itself.
      //
      // What was pointed at is visible. A search or a status group can hide
      // the album that has just been dropped — and then the gesture that
      // worked shows `nothing found`. The same reasoning `scanBegan` follows
      // for the filter: a shelf that does not show what was asked for reads as
      // a shelf that lost it. The filter and the search are cleared, so the
      // shelf that comes back is the whole one.
      state.filter = "all";
      // `clearLibrarySearch` owns that assignment and redraws with it; the
      // filter alone still needs the shelf and the status count drawn again,
      // and `renderGrid` ends in `renderSummary`, which owns the label.
      if (state.search) clearLibrarySearch();
      else renderGrid();
      const moved = event.payload.moved || [];
      // Four answers. The fourth: an album that is not where it was is
      // followed by its audio, and saying so is the difference between a
      // re-anchoring and a screen that claims nothing happened.
      if (moved.length) {
        toast(
          moved.length === 1
            ? STR.adoptedMoved(moved[0].album, moved[0].now)
            : STR.adoptedMovedMany(moved.length),
        );
      } else {
        // Three answers: nothing here is an album, these are new, and these
        // were already here. Each has its own words.
        if (event.payload.albums) toast(STR.adopted(event.payload.albums));
        else if (!event.payload.known) toast(STR.adoptedNothing(event.payload.folders));
      }
      sayKnownAgain(event.payload.known_albums);
      sayArrivedSameAudio(event.payload.same_audio);
    } else if (event.type === "collected") {
      // The album is in the library now; the screen still shows the library as
      // it was before it arrived.
      state.collecting = false;
      // A download that arrives is an arrival. Leaving this one out would give
      // a shelf with some arrivals checked and others not, and nothing on
      // screen saying why.
      markArrivals(event.payload.unit_ids);
      if (event.payload.albums) {
        await refresh();
        toast(STR.collected(event.payload.albums, event.payload.folder));
      }
      sayArrivedSameAudio(event.payload.same_audio);
    } else if (event.type === "arranged") {
      // The batch's answer, with its refusals counted by reason: a number that
      // silently shrank from what was marked reads as albums lost.
      state.scanning = false;
      renderMarkCount();
      await refresh();
      const skipped = event.payload.skipped || {};
      const reasons = Object.entries(skipped)
        .map(([reason, count]) => STR.arrangeSkipped(reason, count))
        .filter(Boolean);
      toast(STR.arrangedDone(event.payload.albums, reasons));
    } else if (event.type === "opened") {
      // Everything a folder brings in arrives marked. Pointing at a folder is
      // the whole instruction, and Scan is still the gesture that spends a
      // catalogue's rate limit. An album opened again from the folder it
      // already lives in is not an arrival — it is named, the same as a drop,
      // because the two are one gesture.
      markArrivals(event.payload.albums);
      // Its own wait, and nothing else's: a scan beside it keeps its bar.
      state.opening = Math.max(0, state.opening - 1);
      refresh();
      sayKnownAgain(event.payload.known_albums);
      sayArrivedSameAudio(event.payload.same_audio);
    } else if (event.type === "restored") {
      // The albums are there now. Asked for rather than pushed, because the
      // restore reports a count and the window wants the rows.
      state.restoring = false;
      // Coming back is not choosing: an album read out of the database was not
      // pointed at, so the first sight of the app has nothing marked and claims
      // no scan.
      state.marked.clear();
      state.scanFinished = false;
      refresh();
      // And the screens built from that map are asked again, because `refresh()`
      // draws the shelf and nothing else. A Mixing screen that asked early in
      // the restore would otherwise hold a fraction of the tracks until the
      // tab is changed.
      if (state.tab === "mixing") refreshMixing();
    } else if (event.type === "error") {
      // An error about a folder being read is not the end of a scan. Reads run
      // beside scans, so this ending the run would switch a live scan's bar
      // off. A read is released by its own `opened` or `adopted`, which the
      // server sends however the read ended — including a refused drop.
      if (!event.payload.reading) {
        state.scanning = false;
        $("progress").hidden = true;
      }
      renderMarkCount();
      renderLibraryChrome();
      toastError(event.payload.message);
    } else {
      // The complement. The chain above names the kinds this window knows, and
      // a kind the application learns to send later belongs to none of them —
      // it would be drained from the queue and dropped without a trace. It
      // cannot be *handled* here, but it can refuse to be invisible: the
      // console line names the event.
      console.warn("An event this window does not know was dropped:", event.type, event);
    }
  }
  // Every branch above can start or end a job, and the one indicator that says
  // whether anything is running is read from those flags. Written here, once,
  // rather than in each branch — where the one that forgot would leave the
  // window claiming to be idle while it worked.
  renderActivity();
  // One place where the chain either continues or stops, and the flag says
  // which — so `ensurePolling` can tell whether anybody is already listening.
  state.polling = worthPolling();
  if (state.polling) {
    setTimeout(pollEvents, pollDelay());
  }
}

async function applyAutomatic() {
  const button = $("apply-automatic");
  button.textContent = STR.applying;
  // The marks are the instruction, so they are what is sent — the same ids the
  // button counted. Sending nothing would let the server choose, and it would
  // choose every automatic album in the library.
  const response = await withSpinner(button, () => api().apply_automatic(markedOnScreen()));
  $("apply-result").hidden = false;
  clearTimeout(applyResultTimer);
  applyResultTimer = setTimeout(() => {
    $("apply-result").hidden = true;
  }, RESULT_SECONDS * 1000);
  // Applying is the other action taken on marked folders, and it ends the same
  // way: what was asked for has happened.
  state.marked.clear();
  state.lastRun = response.run_id || null;
  renderRunButtons(false);
  const certificate = response.certificate || null;
  const trouble =
    certificate &&
    (certificate.audio_intact < certificate.audio_checked ||
      certificate.writes_verified < certificate.writes_checked);
  // A run that only wrote covers is not a run that organized nothing: "Applied
  // to 0 albums. 1 cover written" puts two different denominators in one
  // breath and reads as a contradiction. When no album was organized, the
  // covers are the whole sentence.
  const organized = response.applied > 0 ? STR.appliedTitle(response.applied) : "";
  const covers = response.covers
    ? STR.coversWritten(response.covers, response.cover_albums)
    : "";
  $("apply-result-title").textContent =
    [organized || (covers ? "" : STR.appliedTitle(0)), covers]
      .filter(Boolean)
      .join(" ") +
    (certificate && certificate.audio_checked ? ` ${STR.certificateLine(certificate)}` : "") +
    (trouble ? ` ${STR.certificateTrouble}` : "");
  $("apply-result-failures").replaceChildren(
    ...response.failures.map((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      return item;
    }),
  );
  await refresh();
}

/** Report whether the plan editor holds an edit the plan does not carry.
 *
 * The dialog's primary button applies the plan as it stands. A name typed into
 * a field and not yet sent would be ignored by it: the plan would write the
 * catalogue's name and the edit would go nowhere, with no warning. Fields that
 * are filled in advance look as though they already hold what will be written,
 * which makes that easier to walk into.
 */
function editedCorrections() {
  if (!state.openAlbum || $("plan-editor").hidden) return false;
  return [...$("plan-editor").querySelectorAll("input")].some(
    (input) => (input.value || "").trim() !== (input.dataset.applied || "").trim(),
  );
}

async function approveAlbum() {
  // One press is one apply. Everything before the call waits — a confirmation,
  // the correction still in the air, the re-plan it sends — and a button left
  // live for those seconds lets two applies reach the API milliseconds apart,
  // the second reporting every file of an album just organized as gone.
  // Closed here, and a dialog redrawn in the meantime keeps it closed through
  // `approveIsClosed`.
  if (state.approving) return;
  state.approving = true;
  $("album-approve").disabled = true;
  try {
    // Which plan the button is offering, read from the album on screen rather
    // than from the button's own words.
    const album = state.openAlbum || {};
    const partOnly = !album.applicable && Boolean(album.partial_applicable);
    // Said before anything is written, and it names the number: an apply that
    // leaves files untouched must say so, or the album looks finished and is
    // not. Counted from the rows on screen, so the sentence cannot drift from
    // what the table shows.
    const doubtful = (album.evidence || []).filter((row) => row.suggested).length;
    if (partOnly && doubtful && !(await confirmApplyLeavesDoubtful(doubtful))) return;
    // And said before the other kind of write nobody asked a catalogue about.
    // An album that was never scanned still carries a plan — its own tags,
    // worked out on the way in — and the button that applies it looks exactly
    // like the one that applies a catalogue's answer, so the dialog says what
    // this is and lets the user answer.
    // It is asked in the words that are true of this album. The question is
    // the same either way — this write is made of your tags — but the heading
    // must not assert that nobody scanned an album that was scanned and then
    // arranged on purpose.
    if (album.source === "tags" && !(await confirmApplyWithoutScan(album.catalogue_asked))) return;
    // A gesture that changes this plan may still be in the air when the button is
    // pressed, and a click that lands before the answer would apply the plan that
    // gesture was about to change. Wait for it, then send anything still unsent.
    // A loop, because waiting is itself a window: one of them ends by
    // redrawing the dialog, and a press during that redraw starts the next.
    while (correctionInFlight) await correctionInFlight;
    if (editedCorrections() && !(await sendCorrections())) return;
    const button = $("album-approve");
    let response;
    try {
      response = await withSpinner(button, () =>
        api().approve(state.openAlbum.unit_id, partOnly),
      );
    } catch (error) {
      toastError(String(error));
      return;
    }
    if (!response.ok) {
      // A failed apply is said in a toast as well as in the status line: the
      // status line is far below the fold, and alone it would make "Approve &
      // apply" look like it did nothing at all.
      toastError(response.error);
      findStatus(response.error, false);
      return;
    }
    toast(partOnly ? STR.approvedPartToast : STR.approvedToast);
    $("album-dialog").close();
    await refresh();
  } finally {
    state.approving = false;
    if (state.openAlbum && $("album-dialog").open) {
      $("album-approve").disabled = approveIsClosed(state.openAlbum);
    }
  }
}

/** Look up the one album on screen, from the screen that is about it.
 *
 * The same run `Scan` starts, over a marked set of one. The dialog stays open,
 * and the `identified` event redraws the album in place.
 */
async function scanThisAlbum(event) {
  const unitId = state.openAlbum?.unit_id;
  if (unitId === undefined) return;
  // Where a source was missing, the reason is said and the scan runs: that is
  // the one thing on record that asks for another reading. Where every
  // catalogue did answer, the cost is stated — a scan spends many catalogue
  // calls — and the user decides.
  //
  // The claim is only ever *no source was missing*, never *nothing has
  // changed*: a file replaced on disk is a reason this app cannot see, and
  // saying otherwise would be the screen naming something it did not measure.
  //
  // An album nobody has asked about has no missing source either, and the
  // absence of a missed source must not be read there as *every catalogue
  // answered*: the card says `NOT SCANNED` and the notice says no catalogue
  // has been asked, so the question about scanning *again* is not asked.
  //
  // `looked_at` is the fact the other two surfaces are drawn from, so asking
  // it here is what makes them agree by construction rather than by care.
  const missed = Object.keys(state.openAlbum?.sources_missed || {});
  if (missed.length) toast(STR.scanAgainBecause(missed[0]));
  else if (state.openAlbum?.looked_at && !scanAgainAsked) {
    $("scan-again-dialog").showModal();
    return;
  }
  scanAgainAsked = false;
  // Asked from two places and never from both at once: the notice of an album
  // that has never been scanned, and the footer for one in review. The spinner
  // goes on the button that was pressed — read here, before the first await,
  // because `currentTarget` is only itself while the event is being
  // dispatched.
  const button = event?.currentTarget || $("album-scan-now");
  const response = await withSpinner(button, () => api().scan_selected([unitId]));
  if (!response.ok) {
    toastRefusal(response.error);
    return;
  }
  // The dialog stays open. Closing it would put the answer on the shelf behind
  // and leave the way back — `Cancel` — in a dialog that has to be reopened.
  // The `identified` event redraws this album where it stands.
  scanBegan([unitId]);
}

/** Settle one doubtful pairing, which is the smallest answer there is here.
 *
 * The dialog's own button records every pairing at once; this records the one
 * row it sits on. A doubt is about one file, and a wholesale answer has no way
 * of saying "no" to one of them. What is written is a manual pairing, the same
 * thing the per-row "not this file" writes: the app still guesses nothing.
 */
async function confirmOnePairing(position, signature, button) {
  const album = state.openAlbum;
  if (!album) return;
  return whileCorrecting(async () => {
    const response = await withSpinner(button, () =>
      api().correct(album.unit_id, null, null, null, { [String(position)]: signature }),
    );
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    toast(STR.pairingsConfirmed);
    await openAlbum(album.unit_id);
    await refresh();
  });
}

/** Keep the spelling the file carries, on the one line that offered it.
 *
 * A correction like any other, written through the same door as a title typed
 * by hand: nothing here is a special kind of value, so withdrawing it later is
 * the ordinary withdrawal and the catalogue comes back. The button saves
 * retyping a word the file already holds.
 */
async function keepYourSpelling(position, spelling, button) {
  const album = state.openAlbum;
  if (!album) return;
  return whileCorrecting(async () => {
    const response = await withSpinner(button, () =>
      api().correct(album.unit_id, null, { [String(position)]: spelling }),
    );
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    toast(STR.planYoursKept);
    await openAlbum(album.unit_id);
    await refresh();
  });
}

/** Give this one line back to the catalogue.
 *
 * The way out of the gesture above, offered where that gesture was made. It is
 * asked as itself rather than by emptying the field — which also withdraws a
 * correction but reads as deleting a name — and the button is drawn only where
 * withdrawing restores the very offer it came from, so this is always a round
 * trip and never the loss of a typed word.
 */
async function dropYourSpelling(position, button) {
  const album = state.openAlbum;
  if (!album) return;
  return whileCorrecting(async () => {
    const response = await withSpinner(button, () =>
      api().withdraw_spelling(album.unit_id, position),
    );
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    toast(STR.planYoursDropped);
    await openAlbum(album.unit_id);
    await refresh();
  });
}

/** Give every corrected title of this album back, once the number has been seen.
 *
 * The broad gesture, and the only one that reaches titles typed by hand — so
 * it is the one that asks, and the question states how many decisions are
 * about to go. The server counts and nothing is written until the second call:
 * a question only the server can ask is answered by calling it again.
 */
async function dropEveryCorrectedTitle() {
  const album = state.openAlbum;
  if (!album) return;
  const button = $("plan-yours-drop-all");
  // The question is held too, not only the writes around it. It is modal, so
  // nothing can reach `Approve & apply` while it is open — but a guard that is
  // correct because of what a dialog happens to cover is a guard resting on
  // somebody else's promise.
  return whileCorrecting(async () => {
    let response = await withSpinner(button, () => api().withdraw_title_corrections(album.unit_id));
    if (response.ok && response.confirm === "withdraw_titles") {
      if (!(await confirmDroppingTitles(STR.planYoursDropAllAsk(response.count)))) return;
      response = await withSpinner(button, () =>
        api().withdraw_title_corrections(album.unit_id, true),
      );
    }
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    toast(STR.planYoursDroppedAll(response.dropped));
    await openAlbum(album.unit_id);
    await refresh();
  });
}

async function confirmPairings() {
  const album = state.openAlbum;
  if (!album) return;
  const pairs = {};
  for (const row of album.evidence || []) {
    if (row.status === "paired" && row.position && row.signature) {
      pairs[String(row.position)] = row.signature;
    }
  }
  if (!Object.keys(pairs).length) return;
  return whileCorrecting(async () => {
    const response = await withSpinner($("album-confirm-pairings"), () =>
      api().correct(album.unit_id, null, null, null, pairs),
    );
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    toast(STR.pairingsConfirmed);
    await openAlbum(album.unit_id);
    await refresh();
  });
}

/** Point this album at the folder it lives in now.
 *
 * The user picks it: the app never searches the disk at large, and the one
 * thing its own search cannot do is find a folder that was renamed. The audio
 * still decides — the server refuses a folder holding a different record — so
 * pointing at the wrong one costs nothing but the answer.
 */
async function relocateAlbum() {
  const album = state.openAlbum;
  if (!album) return;
  const folder = await api().choose_folder();
  if (!folder) return;
  const response = await withSpinner($("album-more"), () =>
    api().relocate_album(album.unit_id, folder),
  );
  if (!response.ok) {
    if (response.second_copy) await offerToLetSecondCopyGo(response);
    else toastError(response.error);
    return;
  }
  renderAlbumDialog(response.album);
  toast(STR.relocated(response.album.folder));
  await refresh();
}

/** What "planned again" answers, said about the plan Approve would actually run.
 *
 * Reading the raw blockers alone would say "could not be planned" over a
 * dialog whose right column shows renames and whose Approve is lit — the
 * album's partial plan is the one on offer, and its blockers stand demoted to
 * warnings. Blocked means nothing is on offer.
 */
function plannedToast(album) {
  const partOnly = !album.applicable && Boolean(album.partial_applicable);
  const changes = (partOnly ? album.partial_operations || [] : album.operations || []).length;
  if (!partOnly && (album.blockers || []).length) return STR.plannedBlocked;
  return changes ? STR.plannedChanges(changes) : STR.plannedNothing;
}

// Set by the one button that answers the re-scan question, and cleared by the
// scan it lets through. A flag rather than an argument because the gesture is
// reached from several places — the footer, the notice of an album never
// scanned, and the ⋯ — and each would otherwise have to remember to pass it.
let scanAgainAsked = false;

/** Close the album, or ask first when a proposal is waiting on it.
 *
 * Every way out — `Cancel`, the ✕, a click outside — must treat the album the
 * same way, so the rule is written once and called by every route.
 *
 * The question is asked only where leaving would cost something that was
 * asked for, so an album merely looked at — or merely scanned — still closes
 * with one press.
 *
 * The condition is the server's own answer, not a reading of `can_cancel`: the
 * scan arms the way back and does not earn the question, and working that
 * difference out here would be the window restating a rule.
 */
function closeAlbumOrAsk() {
  // A finished album that was planned again goes back to how it was, with no
  // question. The server names the case; the way to keep the proposal is to
  // apply it.
  if (state.openAlbum?.leaving_takes_back) {
    leaveTakingBack();
    return;
  }
  if (!state.openAlbum?.ask_before_leaving) {
    $("album-dialog").close();
    return;
  }
  $("leave-album-dialog").showModal();
}

/** Close a re-planned finished album and put it back as it was.
 *
 * A finished album re-planned by mistake needs a door back to *finished*:
 * `Approve & apply` writes files, and `Reject` records the identification as
 * wrong. The API decides what *before* was and hands back that album, so
 * nothing here reconstructs a state.
 *
 * Silent when it works, because nothing the user can see changes — the album
 * was organized and it still is. The one sentence kept is the one about what
 * was typed, which is kept on record and would otherwise look lost.
 */
async function leaveTakingBack() {
  const unitId = state.openAlbum?.unit_id;
  $("album-dialog").close();
  const response = await api().cancel(unitId);
  if (!response.ok) {
    toastRefusal(response.error);
  } else if (response.kept_words) {
    toast(STR.cancelledKeepingWords);
  }
  await refresh();
}

async function cancelLast() {
  const unitId = state.openAlbum?.unit_id;
  if (unitId === undefined) return false;
  const response = await withSpinner($("album-cancel"), () => api().cancel(unitId));
  if (!response.ok) {
    toastRefusal(response.error);
    return false;
  }
  renderAlbumDialog(response.album);
  // The API decides which of the two sentences is true, because it is the side
  // that knows what was on record before the gesture.
  toast(response.kept_words ? STR.cancelledKeepingWords : STR.cancelled);
  // The shelf reads the album's state and its badge, and both may have just
  // moved back, so it is drawn again.
  await refresh();
  return true;
}

async function planAnyway() {
  // A ⋯ entry: the menu has closed by the time the answer is on its way, so
  // the ⋯ that offered it wears the ring.
  const button = $("album-more");
  const response = await withSpinner(button, () => api().plan_anyway(state.openAlbum.unit_id));
  if (!response.ok) {
    toastError(response.error);
    return false;
  }
  renderAlbumDialog(response.album);
  reportIfFollowed(response);
  // A gesture that answers has to say it answered. Planning again an album
  // that is already right redraws an identical dialog, which is
  // indistinguishable from a dead button.
  const album = response.album || {};
  toast(plannedToast(album));
  await refresh();
  return true;
}

async function arrangeThisAlbum(from) {
  // The explicit gesture: propose this one album arranged by its own tags. It
  // answers with a dialog, not with a write — Approve is still a separate
  // press.
  //
  // The ring goes on whichever control was pressed: reached from the ⋯ the
  // menu has closed by then, and reached from the footer the button is still
  // there, so hanging it on one of them would leave the other looking dead for
  // the length of the call.
  const button = from || $("album-more");
  const response = await withSpinner(button, () => api().arrange_by_tags(state.openAlbum.unit_id));
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  renderAlbumDialog(response.album);
  reportIfFollowed(response);
  toast(plannedToast(response.album));
  await refresh();
}

/** Say when the album was found somewhere other than where it was recorded.
 *
 * The app follows a moved album by its audio, and every gesture that does so
 * says it. A re-anchoring nobody is told about is indistinguishable from a
 * screen claiming nothing happened.
 */
function reportIfFollowed(response) {
  const moved = response && response.moved;
  if (moved) toast(STR.followedTo(moved.album, moved.now));
}

function arrangeMarked() {
  // Arrange and apply every marked album from its own tags, asking nothing.
  // The answer arrives as the `arranged` event, because the work runs on the
  // worker thread like every batch.
  reportIfRefused(api().arrange_marked(markedOnScreen()));
  state.scanning = true;
  renderMarkCount();
}

async function rejectAlbum() {
  await reportIfRefused(api().reject(state.openAlbum.unit_id));
  toast(STR.rejected);
  $("album-dialog").close();
  await refresh();
}

async function holdAlbum() {
  const response = await api().hold(state.openAlbum.unit_id);
  if (response.ok) toast(STR.held);
  else toastError(response.error);
  $("album-dialog").close();
  await refresh();
}

function setFindMode(mode) {
  state.findMode = mode;
  $("find-mode-link").classList.toggle("is-active", mode === "link");
  $("find-mode-names").classList.toggle("is-active", mode === "names");
  $("find-names-row").hidden = mode !== "names";
  $("paste-link-url").hidden = mode !== "link";
}

function findStatus(text, busy) {
  const node = $("find-status");
  node.hidden = !text;
  node.textContent = text || "";
  node.classList.toggle("is-busy", Boolean(busy));
}

/** One gesture for both ways of naming an album: a link, or words. */
async function findAlbum(event) {
  event.preventDefault();
  const button = $("find-button");
  // The mode decides, not whichever field happens to hold text: with both
  // always visible, which one the app would use would be anybody's guess.
  const byLink = state.findMode === "link";
  const url = byLink ? $("paste-link-url").value.trim() : "";
  if (byLink && !url) {
    findStatus(STR.pasteLinkFirst, false);
    return;
  }
  if (!byLink && !$("search-artist").value.trim() && !$("search-album").value.trim()) {
    findStatus(STR.typeNamesFirst, false);
    return;
  }
  button.disabled = true;
  const spotify = byLink && /(^spotify:|open\.spotify\.com)/.test(url);
  findStatus(byLink ? (spotify ? STR.lookingUpSpotify : STR.lookingUpLink) : STR.searching, true);

  if (byLink) {
    // Any rejection here — a source that raised, a bridge that died — must
    // still re-enable the button. A promise nobody catches leaves the form
    // spinning forever.
    let response;
    try {
      response = await api().adopt_link(state.openAlbum.unit_id, url);
    } catch (error) {
      button.disabled = false;
      findStatus(String(error), false);
      return;
    }
    button.disabled = false;
    if (!response.ok) {
      findStatus(response.error, false);
      return;
    }
    // A Spotify link does not adopt anything: it hands its words to the same
    // catalogue search the names mode runs, and the `searched` event renders
    // the candidates like any other search.
    if (response.searching) {
      const words = response.words || {};
      findStatus(
        STR.searchingWith(words.artist, words.album) +
          (words.exact ? "" : ` ${STR.spotifyWithoutArtist}`),
        true,
      );
      return;
    }
    state.adopted = { source: response.album.source, release_id: response.album.release_id };
    const facts = response.album.release ? candidateFacts(response.album.release).join(" · ") : "";
    findStatus(STR.nowUsing(response.album.title || "", facts), false);
    renderAlbumDialog(response.album);
    $("album-dialog").querySelector(".dialog-body").scrollTop = 0;
    await refresh();
    return;
  }

  let response;
  try {
    response = await api().search_again(
      state.openAlbum.unit_id,
      $("search-artist").value,
      $("search-album").value,
    );
  } catch (error) {
    button.disabled = false;
    findStatus(String(error), false);
    return;
  }
  if (!response.ok) {
    button.disabled = false;
    findStatus(response.error, false);
    return;
  }
  // Marked here rather than on the "searching" event: the first poll can beat
  // that event, and a poll that finds nothing pending stops rescheduling —
  // which strands the form on "Searching…" with no way back.
  state.searching = true;
  pollEvents();
}

async function adoptCandidate(candidate, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = STR.applyingCandidate;
  const response = await api().adopt(
    state.openAlbum.unit_id,
    candidate.source,
    candidate.release_id,
  );
  button.disabled = false;
  button.textContent = original;
  if (!response.ok) {
    findStatus(response.error, false);
    return;
  }
  // The header changes and the footer buttons change; without saying so and
  // scrolling to it, adopting would look like it did nothing at all.
  state.adopted = { source: candidate.source, release_id: candidate.release_id };
  const facts = candidateFacts(candidate).join(" · ");
  const readiness = response.album.applicable ? STR.readyToApply : STR.partialCoverage;
  findStatus(`${STR.nowUsing(candidate.title, facts)} ${readiness}`, false);
  renderAlbumDialog(response.album);
  $("album-dialog").querySelector(".dialog-body").scrollTop = 0;
  await refresh();
}

let correctionInFlight = null;

/** Run a gesture that changes this album's plan, somewhere `Approve & apply` can see it.
 *
 * `Approve & apply` writes to the disk, and it waits for whatever is in the air
 * first — because a click landing before an edit's answer applies the plan the
 * edit was about to change. Several gestures change the plan: a field left on
 * blur, keeping the file's spelling, withdrawing it, withdrawing an album's
 * titles, confirming one pairing, confirming them all, and adopting or
 * dropping a track. Every one of them runs through here; a gesture that did
 * not would let Approve, pressed straight after it, write the value that
 * gesture had just withdrawn.
 *
 * The whole gesture is held, redraw included, and not only its call: the button
 * reads `partial_applicable` off the album on screen, so applying between the
 * answer and the redraw is applying with the previous album's shape.
 *
 * The slot is cleared only by the gesture still holding it. Clearing it
 * unconditionally would leave a second gesture that started mid-flight
 * unannounced.
 */
function whileCorrecting(run) {
  const mine = Promise.resolve()
    .then(run)
    .finally(() => {
      if (correctionInFlight === mine) correctionInFlight = null;
    });
  correctionInFlight = mine;
  return mine;
}

/** Send what the editor holds the moment a field is left, and redraw from the answer.
 *
 * The edit takes effect where it is made: no second button, no pending state to
 * remember. The plan is recomputed by the API, so what the rows show after this
 * is what applying would write — including the parts an edit derives, like the
 * file name a title produces.
 */
function sendIfEdited(event) {
  const input = event.target;
  if ((input.value || "").trim() === (input.dataset.applied || "").trim()) return;
  const returningTo = event.relatedTarget && event.relatedTarget.dataset
    ? event.relatedTarget.dataset.key
    : null;
  whileCorrecting(() => sendCorrections(returningTo));
}

async function sendCorrections(focusKey = null, pairings = null) {
  const editor = $("plan-editor");
  const titles = {};
  const artists = {};
  // Two fields on one row share a track position, so the position cannot tell
  // them apart — only the mark each field carries can. Reading them by
  // position alone would put a credit into a title.
  for (const input of editor.querySelectorAll('input[data-field="title"]')) {
    titles[input.dataset.position] = input.value;
  }
  for (const input of editor.querySelectorAll('input[data-field="artist"]')) {
    artists[input.dataset.position] = input.value;
  }
  const folder = editor.querySelector('input[data-key="folder"]');
  let response;
  try {
    // A pairing travels with the fields as they stand. The folder field is
    // compared against what it opened with, and an empty one withdraws a folder
    // name that had been typed, so where the field is not drawn nothing is sent
    // about it: `null` leaves the name standing, and only `""` takes it away.
    response = await api().correct(
      state.openAlbum.unit_id,
      folder ? folder.value : null,
      titles,
      artists,
      pairings || {},
    );
  } catch (error) {
    toastError(String(error));
    return false;
  }
  if (!response.ok) {
    toastError(response.error);
    return false;
  }
  renderAlbumDialog(response.album);
  if (focusKey) {
    const returning = $("plan-editor").querySelector(`input[data-key="${focusKey}"]`);
    // The row was rebuilt underneath whoever was typing; give the caret back,
    // which now means reopening the field rather than only focusing it.
    if (returning) beginEdit(returning.closest(".plan-field"));
  }
  await refresh();
  return true;
}

/** Undo one applied plan, asking first when the album has been filed away.
 *
 * The server decides whether the question is owed — it is the only side that
 * knows where the album is now — and answers `confirm` instead of an error.
 * Nothing has been written when that comes back.
 */
async function revertPlan(planId) {
  let response = await api().revert(planId);
  if (response.confirm === "filed_away") {
    if (!(await confirmFiledAway(
      STR.revertFiledAway(response.operations, response.folder, response.parent),
    ))) return;
    response = await api().revert(planId, true);
  }
  if (response.ok) toast(STR.reverted);
  else toastError(response.error);
  await refresh();
}

/** Ask before that many corrected titles go back to the catalogue. */
function confirmDroppingTitles(message) {
  $("drop-titles-message").textContent = message;
  return askThrough($("drop-titles-dialog"), $("drop-titles-yes"), $("drop-titles-no"));
}

/** Say that what was just brought in is already in the library.
 *
 * Two sentences, one fact: an album read again from where it lives, and a new
 * album whose every stream an album on the shelf already holds. Every door ends
 * here — a drop, a folder opened, a scan, a download — so it is said once and
 * the same way. In the middle of the window, until OK is pressed: a toast at
 * its foot is easy to miss. Not awaited, because this is called from the loop
 * that drains every event, and waiting on a click there would freeze a scan's
 * bar behind the dialog; a second sentence while it is open adds its own line.
 */
function sayAlreadyInLibrary(sentence) {
  if (!sentence) return;
  const line = document.createElement("p");
  line.className = "muted";
  line.textContent = sentence;
  const dialog = $("same-audio-dialog");
  if (dialog.open) {
    $("same-audio-message").append(line);
    return;
  }
  $("same-audio-message").replaceChildren(line);
  dialog.showModal();
}

/** The arrivals whose audio was already on the shelf, if any. */
function sayArrivedSameAudio(found) {
  if (found && found.length) sayAlreadyInLibrary(STR.arrivedSameAudio(found));
}

/** The albums read again from the folders they already live in, if any. */
function sayKnownAgain(names) {
  if (names && names.length) sayAlreadyInLibrary(STR.adoptedKnown(names));
}

/** A relocation refused because the folder is another album's: let this card go?
 *
 * Both ways to relocate — the card's menu and the album's own dialog — arrive
 * here, so the offer is the same from either. Answers whether the card was let
 * go.
 */
async function offerToLetSecondCopyGo(response) {
  $("second-copy-message").textContent = response.error;
  const letGo = await askThrough(
    $("second-copy-dialog"),
    $("second-copy-yes"),
    $("second-copy-no"),
  );
  if (!letGo) return false;
  if ($("album-dialog").open) $("album-dialog").close();
  await forgetAlbum(response.second_copy.unit_id);
  return true;
}

/** That album is not where it was organized — undo it where it is? */
function confirmFiledAway(message) {
  $("filed-away-message").textContent = message;
  return askThrough($("filed-away-dialog"), $("filed-away-yes"), $("filed-away-no"));
}

/** Apply one reverted plan again, exactly as it was written. */
async function redoPlan(planId) {
  const response = await api().redo_plan(planId);
  if (response.ok) toast(STR.runRedone(1));
  else toastError(response.error);
  await refresh();
}

// --- settings ---------------------------------------------------------------

/** Put the chosen density on the root, where the whole stylesheet reads it. */
function applyDensity() {
  const chosen = (state.settings && state.settings.ui_density) || "compact";
  document.documentElement.dataset.density = chosen;
}

async function openSettings() {
  const settings = await api().settings();
  await openComposer(settings);
  $("setting-style").value = settings.naming_style || "spotiflac";
  $("setting-edition").checked = settings.include_edition === true;
  $("setting-artwork").checked = settings.artwork_enabled !== false;
  $("setting-keep-trees").checked = settings.keep_trees_open !== false;
  $("setting-density").value = settings.ui_density || "compact";
  $("setting-threshold").value = settings.threshold || 0.9;
  $("setting-spectrogram-cache").value = settings.spectrogram_cache_mb || 500;
  $("setting-metadata-cache").value = settings.metadata_cache_mb || 250;
  $("setting-artwork-cache").value = settings.artwork_cache_mb || 250;
  $("setting-backup").value = settings.backup_mb || 500;
  $("setting-database-copies").value = settings.database_copies_mb || 500;
  showCacheSizes();
  $("settings-dialog").showModal();
}
/** ` (10/12)` for a source that compared names, and nothing for one that did not.
 *
 * Written as *did it compare anything* rather than as a list of the states that
 * have no number — asked, unasked, unreachable, still out — because a state
 * added later would otherwise get a `(0/0)`.
 */
function scoreOf(position) {
  const compared = (position && position.titles_compared) || 0;
  return compared ? STR.sourceScore(position.titles_agreeing || 0, compared) : "";
}

/** The header's one line: what this album is, and which catalogues answered.
 *
 * Only the essentials — the year, how many tracks, and the source this match
 * uses, written as a link so the name *is* the way to the page. The others are
 * named beside it as witnesses and are links too whenever they published one,
 * so "which source is being used, and what do the others say?" is answered by
 * reading one line. Everything else is one fold away.
 */
function renderEssentials(album) {
  const line = $("album-essentials");
  line.replaceChildren();
  const facts = [album.year, STR.tracks(album.tracks)].filter(Boolean).join(" · ");
  if (facts) line.append(facts);

  const positions = album.positions || [];
  const inUse = positions.find((position) => position.in_use);
  const url = album.url || (inUse && inUse.url) || null;
  if (album.source === "tags") {
    // Arranged, not identified: the header says the claim's honest size in one
    // breath — where the words came from, and that nobody was asked.
    //
    // Two sentences, because there are two ways to be here: nobody has ever
    // asked about this album, or a catalogue answered and the answer was set
    // aside. Saying *no source was asked* on the second would contradict what
    // happened.
    line.append(
      line.childNodes.length ? " · " : "",
      album.catalogue_asked ? STR.arrangedInsteadHeader : STR.arrangedHeader,
    );
  } else if (album.source) {
    line.append(line.childNodes.length ? " · " : "", STR.usingSource);
    line.append(" ", sourceName(album.source, url));
    // How much of the album's own naming that source publishes. A witness
    // naming more of the files than the source in use is the tell this number
    // exists for.
    line.append(scoreOf(inUse));
  }

  // What this album's own identification went without — a clause in the same
  // grey line, never a notice over the shelf. Absent for the ordinary album:
  // the fact worth a word is the failure, not its absence.
  for (const [source, reason] of Object.entries(album.sources_missed || {})) {
    line.append(
      line.childNodes.length ? " · " : "",
      STR.headMissed(STR.sourceName(source), reason),
    );
  }

  // A witness is any source that answered and is not the one in use. Named
  // whether it agreed or not: "iTunes says nothing about this record" is an
  // answer too.
  const witnesses = positions.filter(
    (position) => !position.in_use && position.asked && position.source !== album.source,
  );
  // And what it is looking at, when that is not this record. Marked on the
  // witness that is already named, never as a clause of its own, so no source
  // appears twice. The numbers are already here — a score beside the witness
  // and one beside the record in use — so the only thing to add is that they
  // are about *different records*.
  const elsewhere = album.witness_knows_better;
  if (!witnesses.length) return;
  line.append(" · ", STR.witnessesLabel, " ");
  witnesses.forEach((witness, index) => {
    if (index) line.append(", ");
    line.append(sourceName(witness.source, witness.url));
    line.append(scoreOf(witness));
    if (elsewhere && witness.source === elsewhere.witness) {
      line.append(" ", STR.witnessAnother);
    }
  });
}

/** Ask for an image and show what writing it would change.
 *
 * On any album, finished or not: a cover names nothing and asks no catalogue,
 * so a finished album is not planned again for it — which would leave a
 * proposal standing that draws the album back as `Auto`. Nothing is written
 * here: the plan comes back to be read, and `Write this cover` is the gesture
 * that writes.
 */
async function chooseCover() {
  if (!state.openAlbum) return;
  const unitId = state.openAlbum.unit_id;
  const answer = await api().choose_cover(unitId);
  if (!answer.ok) {
    toastError(answer.error);
    return;
  }
  if (answer.cancelled) return;
  renderAlbumDialog(answer.album);
}

/** The dialog's picture, and the words of the gesture under it.
 *
 * Three states, because there are three: a picture (*Change cover…*), no
 * picture (an empty frame and *Choose a cover…*), and not answered yet — where
 * the frame waits empty and says nothing, rather than guessing which of the
 * other two it is.
 */
function drawDialogCover(unitId) {
  const cached = state.covers.get(unitId);
  const known = Boolean(cached) || state.coverless.has(unitId);
  $("album-cover").hidden = !cached;
  if (cached) $("album-cover").src = cached;
  $("album-cover-empty").hidden = Boolean(cached);
  $("album-choose-cover").hidden = !known;
  $("album-choose-cover").textContent = cached ? STR.changeCover : STR.chooseCover;
}

/** Run the cover plan that is on screen. */
async function writeChosenCover() {
  if (!state.openAlbum) return;
  const answer = await api().write_chosen_cover(state.openAlbum.unit_id);
  if (!answer.ok) {
    toastError(answer.error);
    return;
  }
  // The window asks for a cover once and remembers it (`requestCover`), which
  // keeps a shelf of cards from asking again on every redraw. Writing a new
  // picture is the one case where the remembered one has to be forgotten, or
  // the card goes on drawing the old cover.
  state.covers.delete(state.openAlbum.unit_id);
  state.coverless.delete(state.openAlbum.unit_id);
  renderAlbumDialog(answer.album);
  toast(STR.coverWritten);
  await refresh();
}

/** Put it down again, having written nothing. */
async function discardChosenCover() {
  if (!state.openAlbum) return;
  const answer = await api().discard_chosen_cover(state.openAlbum.unit_id);
  if (!answer.ok) {
    toastError(answer.error);
    return;
  }
  renderAlbumDialog(answer.album);
}

/** Draw the one aside where the file's own spelling is on the table, in either state.
 *
 * Two states, one area: *your file says X* with `Keep yours`, and once that is
 * answered, *kept: X* with the way back. No control is born on a line that had
 * none, and the way out of a decision sits exactly where the decision was made.
 *
 * A function of its own because it is a handful of `createElement` calls, which
 * is the shape no static guard over these files can judge: every line here is
 * valid whether the sentence matches the button, whether the button is wired,
 * and whether it is wired to the gesture that undoes rather than the one that
 * repeats. `test_plan_yours_aside.py` runs it.
 */
function spellingAside({ yours, yoursReason, yoursWitness, kept, position }) {
  const aside = document.createElement("div");
  aside.className = kept ? "plan-yours is-kept" : "plan-yours";
  const said = document.createElement("span");
  said.className = "plan-yours-said";
  // The reason travels with the offer. The server decides which of the two it
  // is and names the witness; nothing here reassembles that from the facts it
  // was made of. The same holds for the state: `kept` is filled by the side
  // that read the database, and never inferred here from a field's value
  // matching a spelling.
  said.textContent = kept
    ? STR.planYoursStanding(kept)
    : yoursReason === "witness"
      ? STR.planYoursBacked(yours, STR.sourceName(yoursWitness))
      : STR.planYours(yours);
  const answer = document.createElement("button");
  answer.type = "button";
  answer.className = kept ? "link-button plan-yours-drop" : "link-button plan-yours-keep";
  answer.textContent = kept ? STR.planYoursDrop : STR.planYoursKeep;
  answer.title = kept ? STR.planYoursDropTip : STR.planYoursKeepTip;
  answer.addEventListener("click", () =>
    kept ? dropYourSpelling(position, answer) : keepYourSpelling(position, yours, answer),
  );
  aside.append(said, answer);
  return aside;
}

/** Offer one file a place on the tracklist, with the number that costs nothing.
 *
 * The control arrives set to the position after the release's last, so the
 * case that renumbers nothing needs no choosing. Choosing a position the
 * release already uses inserts there, and the plan above redraws with
 * everything after it moved down, which shows what that choice costs.
 */
function adoptControl({ signature, position, last }, { moving = false } = {}) {
  const box = document.createElement("span");
  box.className = "plan-adopt";
  // The list offers every place the finished list can hold, and no other. With
  // several unlisted files over one release, a ceiling that ignored the tracks
  // already added would make choosing one evict another. `last` arrives as the
  // place after the finished list — added tracks counted in — and a moving
  // track already holds one of those places, so its list is one place shorter.
  // A menu of the legal numbers rather than a typed field: a number the album
  // cannot honour is not an input to clamp, it simply is not on the list.
  const ceiling = Math.max(1, (Number(last) || position || 1) - (moving ? 1 : 0));
  const number = document.createElement("select");
  number.className = "plan-adopt-number";
  number.title = STR.planAdoptNumberTip;
  for (let at = 1; at <= ceiling; at += 1) {
    const option = document.createElement("option");
    option.value = String(at);
    option.textContent = String(at);
    number.append(option);
  }
  number.value = String(Math.min(ceiling, Math.max(1, Number(position) || 1)));
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button plan-adopt-button";
  button.textContent = moving ? STR.planAdoptMoveDo : STR.planAdopt;
  button.title = moving ? STR.planAdoptMoveTip : STR.planAdoptTip;
  // The same call either way: adopting at a position the file already holds is
  // a move, because the row is replaced rather than added to. One gesture with
  // one meaning, instead of a second endpoint that could drift from the first.
  button.addEventListener("click", () => adoptTrack(signature, Number(number.value), position));
  // On a track already added there is no label: the row already says this is a
  // track, and the number and the verb are the whole control. The unclaimed
  // file keeps its label, because there the words are what say the gesture
  // exists at all.
  if (moving) box.append(number, button);
  else {
    const label = document.createElement("label");
    label.className = "plan-adopt-label";
    label.textContent = STR.planAdoptLabel;
    box.append(label, number, button);
  }
  return box;
}

/** Give one file a place on the tracklist and redraw from what came back.
 *
 * It says what it did. Pressing the button without changing the number
 * re-adopts at the same position, which is correct and produces a screen
 * identical to the one before it — indistinguishable from a button that is not
 * wired at all. A gesture whose whole effect can be *no visible change* has to
 * speak.
 */
async function adoptTrack(signature, position, from = null) {
  if (!state.openAlbum) return;
  return whileCorrecting(async () => {
    const answer = await api().adopt_extra_track(state.openAlbum.unit_id, signature, position);
    if (!answer.ok) {
      toastError(answer.error);
      return;
    }
    renderAlbumDialog(answer.album);
    toast(from === position ? STR.planAdoptStayed(position) : STR.planAdoptMoved(position));
    await refresh();
  });
}

/** Take one adoption back, leaving the file exactly as it was found. */
async function dropAdoptedTrack(position) {
  if (!state.openAlbum) return;
  return whileCorrecting(async () => {
    const answer = await api().drop_extra_track(state.openAlbum.unit_id, position);
    if (!answer.ok) {
      toastError(answer.error);
      return;
    }
    renderAlbumDialog(answer.album);
    await refresh();
  });
}

/** A source's name, as a link when it published a page and as text when not. */
function sourceName(source, url) {
  const shown = STR.sourceName(source);
  if (!url) {
    const plain = document.createElement("span");
    plain.textContent = shown;
    return plain;
  }
  const link = document.createElement("button");
  link.className = "link-button";
  link.textContent = shown;
  link.title = STR.tipOpenSource(shown);
  link.addEventListener("click", (event) => {
    event.preventDefault();
    reportIfRefused(api().open_url(url));
  });
  return link;
}


/** Throw away every drawn spectrogram, and say how much disk came back.
 *
 * The pictures only. No audio file and no folder of the library is touched —
 * the same line `Clear downloaded` holds.
 */
async function clearSpectrograms() {
  const response = await withSpinner($("setting-clear-spectrograms"), () =>
    api().clear_spectrograms(),
  );
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  toast(STR.spectrogramsCleared(response.freed / (1024 * 1024)));
}

/** Say what the discardable folders hold right now.
 *
 * A ceiling in megabytes means nothing beside a folder whose size cannot be
 * seen: caches grow without anyone noticing. Asked when the panel opens and
 * again after anything here empties one, because a number that does not move
 * after a `Clear` reads as a button that did nothing.
 */
async function showCacheSizes() {
  const line = $("setting-cache-held");
  const response = await api().cache_sizes();
  if (!response || !response.ok) {
    line.textContent = "";
    return;
  }
  const mb = (bytes) => (bytes || 0) / (1024 * 1024);
  line.textContent = STR.cacheHeld(
    mb(response.spectrograms),
    mb(response.metadata),
    mb(response.artwork),
    mb(response.backups),
    mb(response.database_copies),
  );
}

/** Throw away the saved catalogue answers. Nothing identified is lost by it. */
async function clearMetadataCache() {
  const response = await withSpinner($("setting-clear-metadata"), () =>
    api().clear_metadata_cache(),
  );
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  toast(STR.metadataCleared(response.freed / (1024 * 1024)));
  await showCacheSizes();
}

/** Throw away the staged pictures and the drawn sleeves.
 *
 * A cover already written into an album is a file in that album, and this never
 * reaches it — the same line `Clear downloaded` holds.
 */
async function clearArtworkCache() {
  const response = await withSpinner($("setting-clear-artwork"), () => api().clear_artwork_cache());
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  toast(STR.artworkCleared(response.freed / (1024 * 1024)));
  await showCacheSizes();
}

/** Open the welcome, and say which services are already connected.
 *
 * Opened by the application itself the first time, and from Settings at any
 * time. The fields are always empty on open: this window is never told what a
 * key is, only whether there is one (see `config/credentials.py`).
 */
async function openWelcome({ firstRun = false } = {}) {
  // The same screen on two journeys, and its exit says which one it is on. A
  // first launch is being invited in; a return from Settings came to look at a
  // key and wants out — `Start using DigLibrary` there would read as a dialog
  // with no way out.
  $("welcome-start").textContent = firstRun ? STR.welcomeStart : STR.welcomeDone;
  // Offered on the way in and not on the way back: reached from Settings this
  // dialog is a place to look at a key, and the icon has its own button there.
  $("welcome-icon-row").hidden = !firstRun;
  $("connect-discogs").value = "";
  $("connect-acoustid").value = "";
  $("connect-slskd").value = "";
  // Two boxes that look alike and are not: the client-credentials flow is a
  // pair, and a secret pasted into the id field would fail with a message about
  // the key rather than about which half went where.
  $("connect-spotify-id").value = "";
  $("connect-spotify-id").placeholder = STR.welcomeSpotifyId;
  $("connect-spotify-secret").value = "";
  $("connect-spotify-secret").placeholder = STR.welcomeSpotifySecret;
  await showConnections();
  if (!$("welcome-dialog").open) $("welcome-dialog").showModal();
}

/** Say which services have a key. Never which key. */
async function showConnections() {
  const response = await api().connections();
  if (!response || !response.ok) return;
  const held = response.connected || {};
  $("connect-status").textContent = STR.connectionState(
    Boolean(held.DIGLIBRARY_DISCOGS_TOKEN),
    Boolean(held.DIGLIBRARY_ACOUSTID_KEY),
    // Both halves, because one of them alone reaches nothing — and a line
    // reading `connected` on half a key would be false.
    Boolean(held.DIGLIBRARY_SPOTIFY_CLIENT_ID && held.DIGLIBRARY_SPOTIFY_CLIENT_SECRET),
    // `credentials.connected()` answers for every key it can store, and this
    // line reports each of them.
    Boolean(held.DIGLIBRARY_SLSKD_API_KEY),
  );
}

/** Store one service key, and empty the field so it is not left on screen. */
async function connectService(service, field, button) {
  const value = $(field).value;
  const response = await withSpinner($(button), () => api().connect(service, value));
  if (!response.ok) {
    toastError(response.error || STR.connectFailed);
    return;
  }
  $(field).value = "";
  toast(value.trim() ? STR.connected : STR.disconnected);
  await showConnections();
  // The key reaches this process as well as the file (see `config.credentials`),
  // so acquisition can be asked again right now. Without this the tab the key
  // was meant to unblock stays blocked until the app is restarted, with the old
  // sentence still on it — which reads as a key that was refused.
  if (service === "slskd") await acquireRefreshState();
}

/** Close the welcome, and record that it has been through. */
async function finishWelcome() {
  // Read before the dialog closes, and only on the journey that offered it: the
  // same screen is reached from Settings to look at a key, and building an icon
  // because somebody pressed `Done` there would be this application operating a
  // control nobody touched.
  const wanted = $("welcome-icon-row").hidden === false && $("welcome-icon").checked;
  $("welcome-dialog").close();
  await api().welcomed();
  if (wanted) await addToApplications();
}

async function addToApplications(said) {
  // Quick, because the icon is carried rather than drawn — so there is no
  // working state here, and nothing to poll.
  const response = await api().add_to_applications();
  if (!response.ok) {
    // `toastError`, not `toast`: a refusal that fades is a refusal nobody read,
    // and this one carries whatever the platform actually said.
    if (said) said.textContent = STR.iconFailed(response.error);
    else toastError(STR.iconFailed(response.error));
    return;
  }
  const sentence = response.replaced
    ? STR.iconReplaced(response.where)
    : STR.iconCreated(response.where);
  if (said) said.textContent = sentence;
  else toastToBeRead(sentence);
}

async function saveSettings(event) {
  event.preventDefault();
  const response = await api().set_settings({
    naming_style: $("setting-style").value,
    // Both halves: the template is what the engine reads, the pieces are what
    // this panel reopens. An empty line means nothing was composed there and
    // the style answers for it, which is why they are sent as "" rather than
    // left out — leaving them out could never *clear* an arrangement.
    folder_template: composeTemplate(composer.chosen.folder),
    track_template: composeTemplate(composer.chosen.track),
    folder_parts: JSON.stringify(composer.chosen.folder.map((piece) => piece.token)),
    track_parts: JSON.stringify(composer.chosen.track.map((piece) => piece.token)),
    include_edition: $("setting-edition").checked,
    artwork_enabled: $("setting-artwork").checked,
    keep_trees_open: $("setting-keep-trees").checked,
    ui_density: $("setting-density").value,
    threshold: Number($("setting-threshold").value),
    spectrogram_cache_mb: Number($("setting-spectrogram-cache").value),
    metadata_cache_mb: Number($("setting-metadata-cache").value),
    artwork_cache_mb: Number($("setting-artwork-cache").value),
    backup_mb: Number($("setting-backup").value),
    database_copies_mb: Number($("setting-database-copies").value),
  });
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  state.settings = response.settings || state.settings;
  applyDensity();
  $("settings-dialog").close();
  // Folders open or shut is one of these, and the results on screen have to
  // answer to it now rather than at the next search.
  renderAcquire();
  toast(STR.saved);
}


// --- quality ---------------------------------------------------------------

const ENCODING_LABELS = {
  lossless: () => STR.encodingLossless,
  transcoded: () => STR.encodingTranscoded,
  lossy: () => STR.encodingLossy,
  overstated: () => STR.encodingOverstated,
  undecided: () => STR.encodingUndecided,
};

const FINDING_LABELS = {
  transcoded: () => STR.findingTranscoded,
  overstated_bitrate: () => STR.findingOverstatedBitrate,
  inflated_bit_depth: () => STR.findingInflatedBitDepth,
  clipping: () => STR.findingClipping,
  dc_offset: () => STR.findingDcOffset,
  silent: () => STR.findingSilent,
};

const DAMAGE_FINDINGS = new Set(["clipping", "dc_offset", "silent", "inflated_bit_depth"]);

function qualityGroup(album) {
  if (album.encoding === "transcoded") return "transcoded";
  if (album.encoding === "overstated") return "overstated";
  return "honest";
}

function isDamaged(album) {
  return (album.findings || []).some((name) => DAMAGE_FINDINGS.has(name));
}

/** The kinds the bench can be asked for: the key, its word, and what it means.
 *
 * One table, read by the filter, by the counts and by the menu alike. A set
 * spelled out in several places by naming the known members — a chain of `if`s
 * ending in a fallback, chips in the markup, counting lines beside them — lets
 * a member nobody listed get the wrong label rather than go missing. Here the
 * meaning travels with the key, so a new kind cannot be counted without being
 * offered.
 */
const QUALITY_GROUPS = [
  ["all", "qAll", () => true],
  ["transcoded", "qTranscoded", (album) => qualityGroup(album) === "transcoded"],
  // Borderline crosses the verdicts rather than being one of them: every album
  // here is also counted under the verdict it actually earned.
  ["borderline", "qBorderline", (album) => Boolean(album.borderline)],
  ["overstated", "qOverstated", (album) => qualityGroup(album) === "overstated"],
  ["honest", "qHonest", (album) => qualityGroup(album) === "honest"],
  ["damaged", "qDamaged", isDamaged],
  // So does this one, and it is the actionable list: mostly lossless albums
  // whose FLAC/Lossy answer a deep analysis would settle.
  ["weak", "qWeak", (album) => album.confidence === "weak"],
];

function qualityName(group) {
  const found = QUALITY_GROUPS.find(([key]) => key === group);
  return found ? STR[found[1]] : group;
}

function qualityIn(group, albums) {
  const found = QUALITY_GROUPS.find(([key]) => key === group);
  // A kind nobody declared shows everything rather than nothing: an empty
  // screen is indistinguishable from a bench with nothing on it.
  return found ? albums.filter(found[2]) : albums;
}

function visibleQuality() {
  return qualityIn(state.qualityFilter, state.quality);
}

/** Which albums the bench is showing, and how many of each kind there are.
 *
 * The counts are read through one menu in the bar, the way the Library asks
 * the identical question: one question, one control, on both screens.
 *
 * `QUALITY_GROUPS` is the table this and the menu both read, so a kind cannot be
 * counted here and missing from the menu, which is what two hand-written lists
 * of the same things would eventually become.
 */
function qualityCounts() {
  const albums = state.quality;
  const counts = {};
  for (const [group] of QUALITY_GROUPS) {
    counts[group] = qualityIn(group, albums).length;
  }
  return counts;
}

function renderQualitySummary() {
  const albums = state.quality;
  // The questions hide together while the bench is empty, exactly as the
  // Library's do: a filter over nothing is a question about nothing.
  $("quality-questions").hidden = albums.length === 0;
  state.qualityCounts = qualityCounts();
  $("quality-status").textContent = STR.statusLabel(
    qualityName(state.qualityFilter),
    state.qualityCounts[state.qualityFilter] ?? 0,
  );
  renderBenchAnalyzeButton();
}

/** The mark that says this row is reading the user's verdict rather than the measurement.
 *
 * The measurement stays on screen beside the stated verdict, so the two stay
 * auditable — and a row that silently obeys a stated verdict looks identical
 * to one that measured the same answer. This is the difference, said.
 */
function spokenBadge() {
  const badge = document.createElement("span");
  badge.className = "badge badge-spoken";
  badge.textContent = STR.qYourWord;
  badge.title = STR.tipYourWord;
  return badge;
}

/** The mark that says "this one nearly failed" — and nothing else. */
function borderlineBadge() {
  const badge = document.createElement("span");
  badge.className = "badge badge-borderline";
  badge.textContent = STR.qBorderlineBadge;
  badge.title = STR.tipBorderline;
  return badge;
}

function qualityRow(album) {
  const row = document.createElement("tr");
  row.className = album.suspect ? "quality-suspect" : "";
  if (album.borderline) row.classList.add("quality-borderline");
  row.classList.add("is-openable");
  // Which album is open, said on the row as it is on the card: a panel of
  // tracks must not open under a row drawn exactly like every other one. The
  // row and the panel under it are one block, and the styles say so from
  // `#quality-table`, because `albums-list` is also the Library's table and
  // Mixing's.
  row.classList.toggle("is-open", state.qualityOpen.has(album.path));
  // The reason is the tooltip on the whole row: a verdict the user cannot
  // audit is one they cannot disagree with, and disagreeing is the point.
  row.title = album.reason || "";
  // Which album this node is about, so it can be found again after a redraw —
  // the bench rebuilds itself whole, so a node held in a variable is stale by
  // the time anyone wants to scroll to it.
  row.dataset.benchPath = album.path;
  row.addEventListener("click", () => toggleQualityTracks(album));
  benchRootMenu(row, album);

  const mark = document.createElement("td");
  mark.className = "q-mark-cell";
  mark.append(benchMarkBox(album));
  const folder = document.createElement("td");
  folder.className = "quality-folder";
  folder.textContent = album.folder;
  folder.title = album.path || "";
  const verdict = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `badge badge-${album.encoding}`;
  badge.textContent = (ENCODING_LABELS[album.encoding] || (() => album.encoding))();
  verdict.append(badge);
  if (album.borderline) verdict.append(borderlineBadge());
  if (album.spoken) verdict.append(spokenBadge());
  const sureness = document.createElement("td");
  sureness.append(confidenceBadge(album));
  const bitrate = document.createElement("td");
  bitrate.textContent = album.bitrate ? `${album.bitrate} kbps` : "—";
  const median = document.createElement("td");
  median.textContent = `${album.median_drop_db.toFixed(1)} dB`;
  const tracks = document.createElement("td");
  tracks.textContent = STR.qMeasured(album.analyzed, album.tracks);
  const findings = document.createElement("td");
  findings.className = "quality-findings";
  const written = [];
  for (const name of album.findings || []) {
    const tag = document.createElement("span");
    tag.className = "finding";
    tag.textContent = (FINDING_LABELS[name] || (() => name))();
    written.push(tag.textContent);
    findings.append(tag);
  }
  // The chips stay on one line so they cannot grow the row; what runs past the
  // edge of the column is still readable here.
  findings.title = written.join(", ");

  row.append(mark, folder, verdict, sureness, bitrate, median, tracks, findings);
  return row;
}

/** How much this verdict is worth, in a word, with the numbers behind it.
 *
 * Never a substitute for the verdict and never a reason to hide one: an album
 * whose evidence is thin keeps whatever it measured and says so beside it,
 * because stating a doubtful verdict and suppressing one are different things.
 */
function confidenceBadge(album) {
  const word = album.confidence || "weak";
  const badge = document.createElement("span");
  badge.className = `badge badge-conf badge-conf-${word}`;
  badge.textContent = { strong: STR.confStrong, fair: STR.confFair, weak: STR.confWeak }[word];
  badge.title = STR.tipConfidence(album.confidence_reasons || []);
  return badge;
}

/** The box that says which albums the next measurement means.
 *
 * The Library's own gesture, unchanged: always visible rather than revealed on
 * hover, because the whole point is to choose several before pressing anything.
 */
function benchMarkBox(album) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "card-mark";
  box.checked = state.benchMarked.has(album.id);
  box.title = STR.tipMark;
  box.addEventListener("click", (event) => {
    // The row itself unfolds the album's tracks; marking is not that.
    event.stopPropagation();
  });
  box.addEventListener("change", () => {
    if (box.checked) state.benchMarked.add(album.id);
    else state.benchMarked.delete(album.id);
    renderQualitySummary();
  });
  return box;
}

function renderQuality() {
  const albums = visibleQuality();
  const asCards = state.benchView === "cards";
  const empty = state.quality.length === 0;
  $("quality-table").hidden = empty || asCards;
  $("bench-cards").hidden = empty || !asCards;
  $("empty-quality").hidden = !empty;
  // Whichever view is showing hides its own icon, so the one button on screen
  // is always the view a press would move to — the shelf's rule, unchanged.
  $("bench-view-cards").classList.toggle("is-active", asCards);
  $("bench-view-list").classList.toggle("is-active", !asCards);
  if (asCards) {
    const drawn = [];
    for (const album of albums) {
      drawn.push(benchCard(album));
      // The unfolded album, spanning every column right after its own card.
      if (state.qualityOpen.has(album.path)) drawn.push(qualityDetailPanel(album));
    }
    $("bench-cards").replaceChildren(...drawn);
    $("quality-body").replaceChildren();
    renderQualitySummary();
    return;
  }
  const rows = [];
  for (const album of albums) {
    rows.push(qualityRow(album));
    // An album opened stays opened across the redraws a running survey causes.
    if (state.qualityOpen.has(album.path)) rows.push(qualityTracksRow(album));
  }
  $("quality-body").replaceChildren(...rows);
  renderQualitySummary();
}

/** One album as a card: the same facts the row carries, at a size for a handful.
 *
 * A bench holding thousands of albums is a list. Cards are for the few being
 * worked through, which is why the two are one press apart and nothing else
 * differs between them.
 */
function benchCard(album) {
  const card = document.createElement("div");
  card.className = "bench-card";
  if (album.suspect) card.classList.add("quality-suspect");
  // An open card has to look open, or the panel below it reads as belonging to
  // whichever card the eye lands on first.
  card.classList.toggle("is-open", state.qualityOpen.has(album.path));
  card.title = album.reason || "";
  card.dataset.benchPath = album.path;

  const head = document.createElement("div");
  head.className = "bench-card-head";
  head.append(benchMarkBox(album));
  const name = document.createElement("b");
  name.className = "bench-card-name";
  name.textContent = album.folder;
  head.append(name);

  const badges = document.createElement("div");
  badges.className = "bench-card-badges";
  const verdict = document.createElement("span");
  verdict.className = `badge badge-${album.encoding}`;
  verdict.textContent = (ENCODING_LABELS[album.encoding] || (() => album.encoding))();
  badges.append(verdict);
  if (album.borderline) badges.append(borderlineBadge());
  if (album.spoken) badges.append(spokenBadge());
  badges.append(confidenceBadge(album));

  const facts = document.createElement("div");
  facts.className = "bench-card-facts muted";
  facts.textContent = [
    STR.qMeasured(album.analyzed, album.tracks),
    album.bitrate ? `${album.bitrate} kbps` : null,
    `${album.median_drop_db.toFixed(1)} dB`,
  ]
    .filter(Boolean)
    .join(" · ");

  card.append(head, badges, facts);
  card.addEventListener("click", () => toggleQualityTracks(album));
  benchRootMenu(card, album);
  return card;
}

/** Open or close one album's track-by-track measurements. */
async function toggleQualityTracks(album) {
  if (state.qualityOpen.has(album.path)) {
    state.qualityOpen.delete(album.path);
    renderQuality();
    return;
  }
  state.qualityOpen.add(album.path);
  renderQuality();
  if (!state.qualityTracks.has(album.path)) {
    const response = await api().quality_tracks(album.path);
    state.qualityTracks.set(album.path, response.ok ? response : { ok: false, tracks: [] });
    if (!response.ok) toastError(response.error);
    renderQuality();
  }
}

/** The row that unfolds under an album: every track, and whose word decides. */
function qualityTracksRow(album) {
  const row = document.createElement("tr");
  row.className = "quality-detail";
  const cell = document.createElement("td");
  cell.colSpan = 8;
  cell.append(qualityTracks(album));
  row.append(cell);
  return row;
}

/** The same unfolding, as a full-width panel among the cards.
 *
 * A card is a grid cell and the detail is a table, so it cannot open *inside*
 * one without wrecking the row it sits in. It spans every column instead, right
 * after the card it belongs to, which is where the eye already is.
 */
function qualityDetailPanel(album) {
  const panel = document.createElement("div");
  panel.className = "bench-detail";
  panel.append(qualityTracks(album));
  return panel;
}

/** Every track of one album, and whose word decides each one.
 *
 * Built once and wrapped twice: a table cell in the list, a full-width panel
 * among the cards, so both views know how to unfold an album.
 */
function qualityTracks(album) {
  const cell = document.createElement("div");
  const detail = state.qualityTracks.get(album.path);
  if (!detail) {
    cell.textContent = STR.qMeasuring;
    return cell;
  }

  // No album-wide verdict here either, for the reason there is none on the
  // plan's folder row: only a file can have been lossy before it arrived, so
  // only a file is asked. The album's verdict is what its tracks add up to,
  // and this table is where each of them is decided.
  //
  // What stands here instead says the same thing to every track at once. It
  // saves marking an album's tracks one at a time, and pressing it is exactly
  // those presses: it writes no album-level verdict, so the album still ends
  // up being the majority of its tracks.
  cell.append(everyTrackControls(album, detail));
  // What this album was found to be, and by which reading. Every album on
  // this screen is drawn from a replayed verdict, so the evidence behind it is
  // said on the screen, not under the pointer.
  cell.append(qualityEvidence(album));
  //
  // What the audio itself managed to say, as a count. A blank column would
  // read as an engine that never ran.
  const reach = detail.acoustic;
  if (reach) {
    const coverage = document.createElement("p");
    coverage.className = "muted q-acoustic-reach";
    coverage.textContent = STR.qAcousticCoverage(reach.named, reach.unknown, reach.tracks);
    cell.append(coverage);
  }

  const table = document.createElement("table");
  table.className = "q-tracks";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const text of [
    STR.qColFile,
    STR.qColTrackVerdict,
    STR.qColHeard,
    STR.qColWall,
    STR.qColFall,
    STR.qColCeiling,
    STR.qColYours,
    // The column that holds the button, unnamed: what it opens is a picture,
    // and the button says so itself.
    "",
  ]) {
    const heading = document.createElement("th");
    heading.textContent = text;
    headRow.append(heading);
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const track of detail.tracks || []) {
    const line = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = track.file;
    name.title = track.reason || "";
    const verdict = document.createElement("td");
    const badge = document.createElement("span");
    const encoding = track.encoding || "undecided";
    badge.className = `badge badge-${track.transcoded ? "transcoded" : encoding}`;
    badge.textContent = track.encoding
      ? (ENCODING_LABELS[track.transcoded ? "transcoded" : encoding] || (() => encoding))()
      : STR.qNotMeasured;
    verdict.append(badge);
    // Marked on the track too: an album is borderline because some of its
    // tracks are, and this is the row where each one is decided.
    if (track.borderline) verdict.append(borderlineBadge());
    // Which reading convicted this file, and only where it differs from what
    // convicted the album. The sentence above names the album's proof once;
    // repeating it on each row would put one fact on the screen once per track
    // and leave the chips that differ weighing the same as the ones that do
    // not.
    //
    // Written as the complement — *not the album's proof* — rather than as a
    // list of proofs to fall silent on, so a proof added later draws itself
    // instead of becoming invisible. Two proofs that disagree about what the
    // picture will show still cannot share one word, and that is exactly the
    // row this keeps.
    const proof =
      track.proved_by === album.proved_by
        ? null
        : proofChip(track.proved_by, track.picture_shows_it);
    if (proof) verdict.append(proof);
    const shared = sharedMeasurementChip(track);
    if (shared) verdict.append(shared);
    const heard = heardCell(track);
    const wall = document.createElement("td");
  // The interval, where the row knows one. A wall lies between two rungs and a
  // single number claims a precision the measurement has not got — the same
  // reason the label on the picture names two rungs. A row measured before the
  // lower rung was recorded has none, and says the number it has.
  wall.textContent = track.cutoff
    ? track.wall_low
      ? STR.qWallBetween(track.wall_low / 1000, track.cutoff / 1000)
      : STR.qWallAt(track.cutoff / 1000)
    : "—";
  if (track.cutoff && !track.wall_low) wall.title = STR.tipWallOldRuler;
    const fall = document.createElement("td");
    fall.textContent = track.decay_db === null ? "—" : `${track.decay_db.toFixed(1)} dB`;
    const ceiling = document.createElement("td");
    ceiling.textContent = track.ceiling_db === null ? "—" : `${track.ceiling_db.toFixed(1)} dB`;
    const yours = document.createElement("td");
    yours.append(
      verdictControls({
        current: track.override,
        signature: detail.signature,
        file: track.signature,
        audioKey: track.audio_key,
        path: album.path,
        compact: true,
      }),
    );
    const look = document.createElement("td");
    look.className = "q-look";
    // Offered on every track, not only where the audio was called lossy: a
    // measurement more than one recording answers to leaves a track the app
    // calls clean exactly as doubtful as a condemned one, and that track needs
    // a way to be looked at. Nothing is drawn until this is pressed.
    look.append(spectrogramButton(album, track));
    line.append(name, verdict, heard, wall, fall, ceiling, yours, look);
    body.append(line);
    const shown = state.spectrograms.get(spectrogramKey(album.path, track.file));
    if (shown) body.append(spectrogramRow(shown, track));
  }
  table.append(head, body);
  cell.append(table);
  return cell;
}

/** What the audio itself answered about this track, in three states.
 *
 * `named`, `unknown` and *never asked* are three different things; drawn as one
 * blank column, an engine that has run reads as one that never ran. A named
 * recording links to MusicBrainz, because a claim that cannot be checked has
 * to be taken on faith.
 */
function heardCell(track) {
  const cell = document.createElement("td");
  cell.className = "q-heard";
  if (track.heard === "named" && track.recording_id) {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "link-button";
    link.textContent = STR.qHeardNamed;
    link.title = STR.tipHeardNamed(
      typeof track.recording_score === "number" ? track.recording_score : null,
    );
    link.addEventListener("click", (event) => {
      event.stopPropagation();
      api().open_url(`https://musicbrainz.org/recording/${track.recording_id}`);
    });
    cell.append(link);
    return cell;
  }
  const said = document.createElement("span");
  said.className = track.heard === "unknown" ? "muted is-unknown" : "muted";
  said.textContent = track.heard === "unknown" ? STR.qHeardUnknown : STR.qHeardNotAsked;
  said.title = track.heard === "unknown" ? STR.tipHeardUnknown : STR.tipHeardNotAsked;
  cell.append(said);
  return cell;
}

function spectrogramKey(path, file) {
  return `${path} ${file}`;
}

/** Where the audio really stops, as an interval rather than a number.
 *
 * The measured cutoff is the lowest probed band that came back silent, and the
 * probes are a handful of rungs — so the answer is "above this rung there is
 * nothing, at the rung below there was something", and the truth lies between
 * the two. The rungs come from the application rather than being written here,
 * so the picture and the measurement can never disagree about what was asked.
 *
 * ``open`` is the lowest rung: nothing below it was probed, so all that can be
 * said is *at or below*. Returns null when nothing was measured, and no interval
 * is named — an unmeasured track gets no marks on its picture.
 */
function wallInterval(shown) {
  if (!shown.cutoff) return null;
  const rungs = (shown.probes || []).filter((hertz) => hertz < shown.cutoff);
  if (!rungs.length) return { low: 0, high: shown.cutoff, open: true };
  return { low: Math.max(...rungs), high: shown.cutoff, open: false };
}

/** The one press that draws a picture.
 *
 * Nothing is generated until this is pressed — not on a scan, not on opening the
 * album, not for a track the measurement marked. A picture costs disk and
 * time, and drawing one for every suspicious track would spend both on
 * pictures nobody asked for.
 */
function spectrogramButton(album, track) {
  const key = spectrogramKey(album.path, track.file);
  const shown = state.spectrograms.get(key);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button-row";
  button.textContent = shown ? STR.qHideSpectrogram : STR.qAnalyzeQuality;
  button.title = STR.tipAnalyzeQuality;
  button.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (state.spectrograms.has(key)) {
      state.spectrograms.delete(key);
      renderQuality();
      return;
    }
    const response = await withSpinner(button, () =>
      api().track_spectrogram(album.path, track.file),
    );
    if (!response.ok) {
      toastError(response.error);
      return;
    }
    state.spectrograms.set(key, response);
    renderQuality();
  });
  return button;
}

/** The picture, under the row it is about, with its own ruler.
 *
 * The ruler is drawn here rather than by ffmpeg because the geometry is only
 * knowable that way: the picture is the bare spectrum, so a frequency is a row —
 * linearly, from zero to the top of the picture. ffmpeg's own legend sits
 * inside margins whose size is an internal detail, and a line placed against a
 * guessed margin would be a line pointing near the wall.
 */
function spectrogramRow(shown, track = null) {
  const row = document.createElement("tr");
  row.className = "q-spectrogram";
  const cell = document.createElement("td");
  // Eight: file, verdict, what the audio heard, wall, fall, ceiling, your word,
  // look. A span that does not match the header fails silently — the picture
  // simply stops short of the table.
  cell.colSpan = 8;

  const holder = document.createElement("div");
  holder.className = "spec";

  const ruler = document.createElement("div");
  ruler.className = "spec-ruler";
  // Marked where the question is, not every 5 kHz from nought. No encoder cuts
  // at 5 or 10 kHz, and the whole judgement happens in the top of the picture:
  // these are the rungs a wall is compared against.
  //
  // The picture is capped at `DRAWN_TOP_HERTZ`, so the rungs land in its top
  // third whatever the stream's sample rate. Drawn to a 96 kHz stream's own
  // limit, more than half of the picture would be black by construction.
  for (const kilohertz of [16, 18, 20, 22]) {
    const hertz = kilohertz * 1000;
    if (hertz > shown.top_hertz) continue;
    const tick = document.createElement("span");
    tick.className = "spec-tick";
    tick.style.top = `${(1 - hertz / shown.top_hertz) * 100}%`;
    tick.textContent = STR.specKilohertz(kilohertz);
    ruler.append(tick);
  }

  const frame = document.createElement("div");
  frame.className = "spec-frame";
  const image = document.createElement("img");
  image.src = shown.image;
  image.alt = STR.specAlt(shown.file);
  frame.append(image);
  // **Labels at a height, and no stroke across the picture.** A line
  // laid over a spectrum is read as part of it, and a horizontal one is exactly
  // what a low-pass filter leaves — so a ruler drawn there shows a cut on a file
  // that has none. Each mark below is an anchor with no height that places a
  // label at its frequency, and nothing else is painted over the evidence.
  //
  // Where the common encoders habitually stop, shown always and quietly. The
  // picture exists so that the verdict can be disagreed with, and disagreeing
  // means comparing what is seen against where a filter would have cut — a
  // ruler that appears only under the pointer cannot be compared against.
  // Never coloured like the measured wall: these are references, and only one
  // mark on this picture is a finding about this file.
  for (const reference of shown.encoder_walls || []) {
    const anchor = document.createElement("div");
    anchor.className = "spec-reference";
    anchor.style.top = `${(1 - reference.hertz / shown.top_hertz) * 100}%`;
    const mark = document.createElement("span");
    mark.className = "spec-reference-label";
    mark.textContent = STR.specEncoderWall(reference.bitrate);
    mark.title = STR.tipSpecEncoderWall(reference.bitrate, reference.hertz / 1000);
    anchor.append(mark);
    frame.append(anchor);
  }

  // The wall the numbers found, marked on the picture so the two can be seen to
  // agree — or not. Nothing is concluded from the picture itself: the reading
  // is the analyzer's.
  //
  // An interval, not a frequency. The measured cutoff is the lowest *probed*
  // band that came back silent — so 20 kHz means the audio survived 19 and did
  // not survive 20, and it really stops somewhere between. The label sits at
  // the upper rung and its words carry both, or *at or below* at the lowest
  // rung, where nothing underneath was probed.
  const wall = wallInterval(shown);
  if (wall) {
    const anchor = document.createElement("div");
    anchor.className = "spec-wall";
    anchor.style.top = `${(1 - wall.high / shown.top_hertz) * 100}%`;
    const label = document.createElement("span");
    label.className = "spec-wall-label";
    label.textContent = wall.open
      ? STR.specWallBelow(wall.high / 1000)
      : STR.specWallBetween(wall.low / 1000, wall.high / 1000);
    label.title = STR.tipSpecWall(wall.open);
    anchor.append(label);
    frame.append(anchor);
  }
  holder.append(ruler, frame);

  const said = document.createElement("p");
  said.className = "muted spec-reading";
  said.textContent = STR.specReading(shown);
  // Above the picture, never below it. This note exists to arrive before the
  // doubt the picture creates, not to answer it afterwards: a reader who knows
  // spectrograms and has never heard of a frame grid sees a spectrum reaching
  // the top and concludes the verdict is wrong.
  // And only where the picture really will not show it. A file can carry both
  // proofs, and on one that also has a wall the note would read *The picture
  // will not show this one.* over a spectrogram with the wall drawn across it.
  // `picture_shows_it` is that fact, asked of the measurement rather than
  // rebuilt here.
  const note =
    track && track.proved_by === "grid" && !track.picture_shows_it
      ? gridPictureNote(track)
      : null;
  cell.append(...(note ? [note] : []), holder, said);
  row.append(cell);
  return row;
}

/** Mark a verdict that was measured from audio which is not this file's.
 *
 * A content signature is codec, sample count, rate, channels and depth, and two
 * different songs of the same length in the same format agree on all five. A
 * stored row that does not record which audio it came from may therefore have
 * been measured from another recording, and `measured_from_this_file` says so.
 * The album's own borrowed-measurement warning does not cover this: it counts
 * a file only when a second file *on the bench* reads the same row, and the
 * other recording does not have to be on the bench.
 *
 * It qualifies the verdict rather than replacing it. The row is the best
 * answer there is until the album is measured again, and a blank says less
 * than a qualified answer does.
 *
 * `false`, not falsy. A payload that does not carry the field answers
 * `undefined` — and marking those would put the warning on every line and mean
 * nothing.
 */
function sharedMeasurementChip(track) {
  if (!track || !track.encoding || track.measured_from_this_file !== false) return null;
  const chip = document.createElement("span");
  chip.className = "badge badge-shared-measurement";
  chip.textContent = STR.qSharedMeasurement;
  chip.title = STR.tipSharedMeasurement;
  return chip;
}

/** The chip that says which reading convicted this line, or nothing.
 *
 * Nothing where nothing was convicted: a file the app calls clean has no proof
 * to name, and a chip reading `spectrum` on it would be a claim about evidence
 * that decided the opposite.
 */
function proofChip(proof, pictureShowsIt = false) {
  const label = { grid: STR.qProofGrid, wall: STR.qProofWall, spectrum: STR.qProofSpectrum }[proof];
  if (!label) return null;
  const chip = document.createElement("span");
  chip.className = `badge badge-proof badge-proof-${proof}`;
  chip.textContent = label;
  chip.title = { grid: STR.tipProofGrid, wall: STR.tipProofWall, spectrum: STR.tipProofSpectrum }[
    proof
  ];
  // The tooltip must not point at a note that is not there. `tipProofGrid`
  // ends by saying to open the picture and read the note beside it, and on a
  // file that also carries a wall there is no note — the picture shows where
  // the audio stops.
  if (proof === "grid" && pictureShowsIt) chip.title = STR.tipProofGridAndWall;
  return chip;
}

/** The note that stands beside a picture which cannot show what convicted.
 *
 * In two parts: the lead says plainly that the picture is not the evidence
 * here, and the body explains what a frame grid is — because a reader who has
 * to take *the app found something you cannot see* on faith is a reader who
 * will trust the picture instead.
 */
function gridPictureNote(track) {
  const note = document.createElement("div");
  note.className = "spec-grid-note";
  const head = document.createElement("p");
  head.className = "spec-grid-head";
  head.textContent = STR.qGridPictureHead;
  const body = document.createElement("p");
  body.className = "spec-grid-body";
  body.textContent = STR.qGridPictureBody;
  note.append(head, body);
  if (track.frame_grid_z !== null && track.frame_grid_z !== undefined) {
    const peak = document.createElement("p");
    peak.className = "muted spec-grid-peak";
    peak.textContent = STR.qGridPeak(track.frame_grid_z);
    note.append(peak);
  }
  return note;
}

/** What this album was found to be, in the sentence the measurement wrote.
 *
 * And, where the frame grid convicted it, what that means. *The readings
 * agree* reads as one measurement repeated, and a measurement repeated agrees
 * with itself. The sentence leads with the half that makes agreement
 * evidence — that the six readings could have disagreed.
 */
function qualityEvidence(album) {
  const holder = document.createElement("div");
  holder.className = "q-evidence";
  if (!album.reason) return holder;
  const line = document.createElement("p");
  line.className = "q-evidence-reason";
  line.textContent = album.reason;
  holder.append(line);
  // The gesture that settles this, next to the sentence it would settle. The
  // top bar's button reaches whatever is marked; this one is about the album
  // whose evidence is on screen, and needs nothing marked at all.
  if (state.canMeasureAudio) {
    const settle = document.createElement("button");
    settle.type = "button";
    settle.className = "button button-quiet q-evidence-settle";
    settle.textContent = STR.benchAnalyzeThis;
    settle.title = STR.tipBenchAnalyze;
    settle.disabled = Boolean(state.qualityRunning);
    settle.addEventListener("click", () => startBenchAnalysis([album.id], settle));
    holder.append(settle);
  }
  if (album.proved_by === "grid") {
    // Folded, because it answers a question a reader only asks once. What
    // agreeing readings mean is the same paragraph on every album the grid
    // convicts, and at full weight it would stand between the verdict and the
    // gesture that settles it. The lead stays visible and clickable; the
    // explanation waits to be asked for. `details` is the window's own fold —
    // `op-fold` and `bench-bands` are both this.
    const fold = document.createElement("details");
    fold.className = "q-evidence-fold";
    const head = document.createElement("summary");
    head.className = "q-evidence-head";
    head.textContent = STR.qGridReadingsHead;
    const body = document.createElement("p");
    body.className = "muted q-evidence-body";
    body.textContent = STR.qGridReadingsBody;
    fold.append(head, body);
    holder.append(fold);
  }
  return holder;
}

/** State one verdict about every track of this album at once.
 *
 * Drawn only where it has something to reach: an album whose tracks are not on
 * screen has nothing for this to act on, and an empty control is a gesture
 * that answers with silence.
 *
 * The withdrawing choice appears only when a verdict already stands on every
 * track, because that is the state it withdraws — offered against a half-marked album it
 * would read as a promise to restore the ones it never touched.
 */
function everyTrackControls(album, detail) {
  const holder = document.createElement("div");
  holder.className = "q-every";
  const tracks = detail.tracks || [];
  if (!tracks.length) return holder;
  const caption = document.createElement("span");
  caption.className = "q-verdict-label";
  caption.textContent = STR.qEveryTrack(tracks.length);
  holder.append(caption);
  const said = (word) => tracks.every((track) => track.override === word);
  const choices = [
    ["honest", STR.qSayHonest, STR.tipSayEveryHonest],
    ["transcoded", STR.qSayTranscoded, STR.tipSayEveryTranscoded],
    ["clear", STR.qSayClear, STR.tipSayEveryClear],
  ];
  for (const [verdict, text, tip] of choices) {
    if (verdict === "clear" && !said("honest") && !said("transcoded")) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mode-button";
    button.classList.toggle("is-active", verdict !== "clear" && said(verdict));
    button.textContent = text;
    button.title = tip;
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      // Pressing what already stands on every track withdraws it, which is the
      // same gesture the single-track control offers.
      const wanted = said(verdict) ? "clear" : verdict;
      const response = await withSpinner(button, () =>
        api().say_about_every_track(album.path, wanted),
      );
      if (!response.ok) {
        toastError(response.error);
        return;
      }
      toast(STR.qEverySaved(response.tracks));
      // The album above them and the rows underneath, in one call:
      // `refreshBench` re-reads both, so re-reading the tracks here as well
      // would be a second statement of one rule.
      await refreshBench();
    });
    holder.append(button);
  }
  return holder;
}

/** The three verdicts a user can state about one album or one track. */
function verdictControls({ label, current, signature, file, audioKey, path, compact = false }) {
  const holder = document.createElement("div");
  holder.className = compact ? "q-verdict is-compact" : "q-verdict";
  holder.addEventListener("click", (event) => event.stopPropagation());
  if (label) {
    const caption = document.createElement("span");
    caption.className = "q-verdict-label";
    caption.textContent = label;
    holder.append(caption);
  }
  const choices = [
    ["honest", STR.qSayHonest, STR.tipSayHonest],
    ["transcoded", STR.qSayTranscoded, STR.tipSayTranscoded],
    ["clear", STR.qSayClear, STR.tipSayClear],
  ];
  for (const [verdict, text, tip] of choices) {
    if (verdict === "clear" && !current) continue;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mode-button";
    button.classList.toggle("is-active", current === verdict);
    button.textContent = text;
    button.title = tip;
    button.addEventListener("click", async () => {
      // Clicking what already stands withdraws it — a verdict is withdrawn,
      // never stacked — so marking a track `Not lossy` can be undone by the
      // same button, without a third control worded as something else.
      const wanted = current === verdict ? "clear" : verdict;
      // The audio the verdict is about, not only the shape it wears: two files
      // can share a signature, and a verdict on one recording must not answer
      // for a different one.
      const response = await api().set_quality_verdict(signature, wanted, file, audioKey || "");
      if (!response.ok) {
        toastError(response.error);
        return;
      }
      // The word is recorded; the name follows the next time it is planned.
      toast(STR.qVerdictSaved);
      const refreshed = await api().quality_tracks(path);
      state.qualityTracks.set(path, refreshed.ok ? refreshed : { ok: false, tracks: [] });
      // And the album above them. Re-reading only the track table would leave
      // the header showing the verdict it had before: every track marked lossy
      // under a header still announcing `LOSSLESS`.
      //
      // The whole bench rather than this one album: `bench_state` re-judges
      // from the database with no disk walked, so it is cheap. It renders on
      // its way out, so it goes last and there is one draw rather than two.
      await refreshBench();
    });
    holder.append(button);
  }
  return holder;
}

/** Add one measured album, without rebuilding the table under it.
 *
 * Measuring a library is hours of decoding reported album by album, and
 * redrawing every row for each one makes the screen slower the longer it runs.
 */
function appendQuality(album) {
  const filter = state.qualityFilter;
  const shown =
    filter === "all" ||
    (filter === "damaged" ? isDamaged(album) : qualityGroup(album) === filter);
  if (shown) $("quality-body").appendChild(qualityRow(album));
  $("quality-table").hidden = false;
  $("empty-quality").hidden = true;
  renderQualitySummary();
}

/** Put a folder on the bench — the whole library, if that is what is chosen.
 *
 * The walk is the cost and it is paid once. It decodes nothing: what appears
 * is what is already on record.
 */
async function addBenchFolder() {
  const folder = await api().choose_folder();
  if (!folder) return;
  const response = await withSpinner($("bench-add"), () => api().bench_add(folder));
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  pollEvents();
}

/** Send the open album to the bench, and go there with it.
 *
 * One gesture with one destination: adding without going leaves it unclear
 * whether it landed, and the bench is where the answer is. The path comes back
 * from the bridge rather than from this screen, so that the row the walk writes
 * and the row this opens are the same string.
 */
async function sendAlbumToBench() {
  const album = state.openAlbum;
  if (!album) return;
  const response = await withSpinner($("album-to-bench"), () =>
    api().send_to_bench(album.unit_id),
  );
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  $("album-dialog").close();
  await landOnBench(response.root, album.folder);
}

/** Land on the album that was just sent, open, and scrolled into view.
 *
 * The album is found by the **root the bridge just returned**, never by a path
 * carried across from the other screen: two spellings of one accented name do
 * not compare equal, and a path compared that way can point at a folder the
 * album has since left.
 */
async function landOnBench(root, folder) {
  switchTab("quality");
  await refreshBench();
  const arrived = state.quality.filter((row) => row.root === root);
  // A filter set earlier can hide what was just sent. An album that lands
  // outside the chosen kind is an album that did not arrive as far as anyone
  // can see. Asking for everything shows what was sent without pretending the
  // album is something it is not.
  const shown = new Set(visibleQuality());
  if (arrived.length && !arrived.some((row) => shown.has(row))) {
    state.qualityFilter = "all";
    renderQuality();
  }
  for (const entry of arrived) {
    if (!state.qualityOpen.has(entry.path)) await toggleQualityTracks(entry);
  }
  // Opened is not the same as seen: a row unfolded below the visible part of a
  // long bench is indistinguishable from a gesture that did nothing. Read from
  // the document after the last redraw, because every redraw rebuilds the
  // bench whole.
  const landed = arrived[0];
  if (landed) {
    const node = document.querySelector(
      `[data-bench-path="${CSS.escape(landed.path)}"]`,
    );
    if (node) node.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  toast(STR.benchSent(folder));
}

/** Read the bench back out of the database, which is what opening it costs.
 *
 * No disk is walked here: albums are judged from what is on record, which is
 * why this can be the answer to arriving at the tab.
 */
async function refreshBench() {
  const bench = await api().bench_state();
  state.benchRoots = bench.roots || [];
  state.quality = bench.albums || [];
  state.canMeasureAudio = bench.can_measure !== false;
  renderQuality();
  // And whatever is standing open under it, because a fold is drawn from the
  // same rows this was just re-judged from. Stated here rather than at each
  // place that re-reads the bench: a track table cached before a write would
  // otherwise go on showing the old chips under an album whose verdict changed.
  await refreshOpenQualityFolds();
}

/** Re-read the tracks of every album standing open on the bench.
 *
 * Cleared *and* re-read, never only cleared: an emptied cache draws
 * `Measuring…` under an album nobody is measuring, because the fetch belongs
 * to the gesture that opens a fold and that gesture is not happening. A screen
 * frozen on a status it will never leave is the same failure as a stale one,
 * with a better disguise.
 */
async function refreshOpenQualityFolds() {
  const open = [...state.qualityOpen];
  if (!open.length) return;
  for (const path of open) {
    const refreshed = await api().quality_tracks(path);
    state.qualityTracks.set(path, refreshed.ok ? refreshed : { ok: false, tracks: [] });
  }
  renderQuality();
}

/** The two things that can be done to the folder an album came in on.
 *
 * They live on the album itself rather than on a row of folder chips above the
 * list: when every bench folder is one album, such chips repeat the list
 * underneath them.
 *
 * The folder is named in both labels, because a bench folder may hold many
 * albums: right-clicking one row must not offer to remove "this", meaning all
 * of them, without saying so.
 */
function benchRootMenu(node, album) {
  node.addEventListener("contextmenu", (event) => {
    const root = (state.benchRoots || []).find((entry) => entry.id === album.root);
    if (!root) return;
    event.preventDefault();
    openAcquireMenu(event, [
      // First, and about the album rather than the folder it arrived in: it is
      // the one gesture here that answers the question this screen asks. Left
      // out where nothing can measure, because an entry that always fails is
      // worse than an absent one.
      ...(state.canMeasureAudio
        ? [{ label: STR.benchAnalyzeThis, run: () => startBenchAnalysis([album.id]) }]
        : []),
      { label: STR.benchRewalk(root.name), run: () => walkBenchRoot(root.id) },
      { label: STR.benchRemove(root.name, root.albums), run: () => removeBenchRoot(root.id) },
    ]);
  });
}

async function walkBenchRoot(rootId) {
  const response = await api().bench_walk(rootId);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  pollEvents();
}

/** The button that costs something, offered only when something is marked.
 *
 * Hidden rather than disabled with nothing marked: a control that can do
 * nothing in the current state is not offered.
 *
 * Nothing marked and no ffmpeg would produce the same missing button, so a
 * machine without the binary would look like a screen waiting for a selection.
 * The tool's absence is therefore stated above the list, whatever is marked,
 * because it is not a fact about the marks.
 */
function renderBenchAnalyzeButton() {
  const marked = state.benchMarked.size;
  const button = $("bench-analyze");
  button.hidden = marked === 0 || !state.canMeasureAudio;
  button.textContent = STR.benchAnalyze(marked);
  button.disabled = Boolean(state.qualityRunning);
  $("quality-no-ffmpeg").hidden = state.canMeasureAudio;
  // Its sibling: measuring says what the audio *is*, this asks what recording
  // it holds. Shown on the same condition, because both cost minutes and both
  // are meaningless with nothing marked.
  const identify = $("bench-identify");
  identify.hidden = marked === 0;
  identify.textContent = STR.benchIdentify(marked);
  identify.disabled = Boolean(state.qualityRunning);
}

/** Ask AcoustID which recording each file of the marked albums holds.
 *
 * Identification asks about an album only when its names failed, so this is
 * the way to ask about any other file. Nothing is renamed and nothing is
 * decided — the answers are written down, and the column beside each track is
 * where they show.
 */
async function startBenchIdentification() {
  const marked = [...state.benchMarked];
  if (marked.length === 0) return;
  const response = await withSpinner($("bench-identify"), () =>
    api().bench_identify(marked),
  );
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  pollEvents();
}

/** Measure the marked albums properly. Writes nothing — the answer comes back
 * as a proposal to adopt or discard, with what it cost. */
async function startBenchAnalysis(albums = null, button = null) {
  // The same gesture is reachable from the album's own menu and from its open
  // fold, not only from the top-bar button that exists while something is
  // marked: a way to settle a verdict has to be offered where the doubt is.
  const wanted = albums || [...state.benchMarked];
  if (wanted.length === 0) return;
  const press = button || $("bench-analyze");
  const response = await withSpinner(press, () => api().bench_analyze(wanted));
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  pollEvents();
}

/** What a finished deep measurement would change, before anything is written. */
async function showBenchAnalysis() {
  const analysis = await api().bench_analysis();
  const panel = $("bench-analysis");
  if (!analysis.pending) {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  const differing = (analysis.albums || []).filter((album) => album.changed > 0);
  const head = document.createElement("p");
  head.className = "bench-analysis-head";
  head.textContent =
    differing.length === 0
      ? STR.benchAnalyzedNothing(analysis.files, analysis.seconds)
      : STR.benchAnalyzedHead(analysis.files, analysis.seconds, differing.length);

  const list = document.createElement("ul");
  list.className = "bench-analysis-list";
  for (const album of differing) {
    const line = document.createElement("li");
    line.textContent = album.verdict_moved
      ? STR.benchMoved(album.folder, album.was, album.now)
      : STR.benchSameVerdict(album.folder, album.changed);
    if (album.verdict_moved) line.classList.add("is-moved");
    list.append(line);
  }

  const actions = document.createElement("div");
  actions.className = "bench-analysis-actions";
  const adopt = document.createElement("button");
  adopt.type = "button";
  adopt.className = "button button-primary";
  adopt.textContent = STR.benchAdopt;
  adopt.title = STR.tipBenchAdopt;
  adopt.addEventListener("click", () => adoptBenchAnalysis(true));
  const discard = document.createElement("button");
  discard.type = "button";
  discard.className = "button button-quiet";
  discard.textContent = STR.benchDiscard;
  discard.addEventListener("click", () => adoptBenchAnalysis(false));
  actions.append(adopt, discard);

  panel.replaceChildren(head, ...(differing.length ? [list] : []), actions);
  panel.hidden = false;
  // The band-pass reading under each album, so that no external spectrum
  // viewer is needed to see it.
  for (const album of analysis.albums) {
    panel.insertBefore(benchBands(album), actions);
  }
}

/** Every measured track's band-pass table: what is left above the wall, and
 * whether it is a floor or music still falling. */
function benchBands(album) {
  const holder = document.createElement("details");
  holder.className = "bench-bands";
  const summary = document.createElement("summary");
  summary.textContent = `${album.folder} — ${STR.benchBandsHead}`;
  holder.append(summary);
  for (const track of album.tracks || []) {
    if (!(track.bands || []).length) continue;
    const line = document.createElement("div");
    line.className = "bench-band-row";
    const name = document.createElement("span");
    name.className = "bench-band-name";
    name.textContent = track.file;
    line.append(name);
    for (const band of track.bands) {
      const cell = document.createElement("span");
      cell.className = "bench-band";
      // Flat between neighbours is the encoder's floor; that is the one thing
      // this table exists to make visible, so it is marked rather than left to
      // be spotted among fourteen numbers.
      if (band.step !== null && Math.abs(band.step) < 1.0) cell.classList.add("is-flat");
      cell.textContent = `${band.low / 1000}k ${band.db}`;
      cell.title = band.step === null ? "" : `${band.step > 0 ? "+" : ""}${band.step} dB to the next`;
      line.append(cell);
    }
    holder.append(line);
  }
  return holder;
}

async function adoptBenchAnalysis(accept) {
  const response = await api().bench_adopt(accept);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  $("bench-analysis").hidden = true;
  $("bench-analysis").replaceChildren();
  toast(accept ? STR.benchAdopted(response.written) : STR.benchDiscarded);
  // After any batch action, nothing stays marked.
  state.benchMarked.clear();
  // What was measured changes what every row reads, so the bench is re-read
  // rather than patched: the verdict is judged from the numbers, not stored.
  await refreshBench();
}

async function removeBenchRoot(rootId) {
  const response = await api().bench_remove(rootId);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  // Whatever was marked was marked on rows that no longer exist.
  state.benchMarked.clear();
  refreshBench();
}



// --- acquisition -------------------------------------------------------------
// A Soulseek result is a folder with files inside it, drawn the way Soulseek
// clients draw it: a parent row read across, its tracks beneath once opened.
// A folder is the usual wish; ticking files takes only those.

const ACQUIRE_SORTS = {
  user: { read: (f) => f.source.toLowerCase(), biggestFirst: false },
  free: { read: (f) => (f.free ? 0 : 1 + (f.queue || 0)), biggestFirst: false },
  rate: { read: (f) => f.rate || 0, biggestFirst: true },
  folder: { read: (f) => f.folder.toLowerCase(), biggestFirst: false },
  files: { read: (f) => f.files || 0, biggestFirst: true },
  size: { read: (f) => f.size || 0, biggestFirst: true },
};
// How each column orders, and which way its first click points. A column opened
// to answer "which is fastest / biggest" starts at the top of that; a name
// starts at A. `biggestFirst` is declared rather than faked by negating the
// value, because a negated value makes the little arrow point the opposite way
// to what the list is actually showing.

const acquire = {
  // One entry per open search, the way a Soulseek client holds several at
  // once. Everything a search owns lives on its own tab — its rows, which of
  // them are open, what is ticked, how it is sorted — because a tab is a whole
  // separate search and nothing about it belongs to its neighbour.
  tabs: [],
  active: null,
  polling: null,
};

function activeTab() {
  return acquire.tabs.find((tab) => tab.id === acquire.active) || null;
}

// Reading a folder asks its source directly, so a hundred rows on screen must
// not become a hundred simultaneous questions to a hundred peers. They queue,
// a few at a time, and each row fills itself in as its answer lands.
// --- the row being read ----------------------------------------------------
// Both of these lists are wide and dense: seven columns at twenty-two pixels a
// row, read across from a user to an attribute. The picked row is lit, because
// a mis-read row in Find music is a different album downloaded.

const PICKED_CONTAINERS = { acquire: "acquire-rows", transfers: "transfers-rows" };

/** Light this row when it is clicked, and keep it lit through every redraw.
 *
 * Registered *before* whatever else the row does with a click. Opening a folder
 * rebuilds the whole list, so a handler that recorded the pick afterwards
 * recorded it into rows that had already been thrown away.
 */
function pickRow(line, screen, key) {
  line.dataset.pick = key;
  if (state.picked[screen] === key) line.classList.add("is-picked");
  line.addEventListener("click", () => {
    state.picked[screen] = key;
    repaintPicked(screen);
  });
}

/** Move the highlight without redrawing anything, which is all that changed. */
function repaintPicked(screen) {
  const container = $(PICKED_CONTAINERS[screen]);
  if (!container) return;
  for (const node of container.querySelectorAll("[data-pick]")) {
    node.classList.toggle("is-picked", node.dataset.pick === state.picked[screen]);
  }
}

const reading = { queue: [], busy: 0, atOnce: 3 };

/** Did the folder's own name carry every word of the query? */
function nameMatched(folder) {
  return Boolean(folder.relevance) && folder.relevance[0] >= 1;
}

/** Ask a source for the rest of one folder, unless there is a reason not to.
 *
 * `asked` means the request was explicit — "See all files" — and it overrides
 * every shortcut here except a read already in flight. Without it the gesture
 * would be refused in silence for exactly the folders whose name matched the
 * query, because `nameMatched` assumes the search already answered with the
 * whole folder.
 *
 * That assumption is a good default and a bad promise. It is why a row is not
 * read on its own — reading a hundred rows asks a hundred peers a question
 * nobody asked — but a peer can answer a search with one file of a folder of
 * many, so the assumption must not refuse an explicit request.
 */
function readFolderSoon(tab, folder, { asked = false } = {}) {
  if (folder.reading) return;
  if (asked) {
    // Asked again even if it was read before: a share changes, and this is the
    // gesture that means "show me what is really there, now".
    folder.full = false;
    folder.unreachable = false;
  } else {
    if (folder.full || folder.unreachable) return;
    // Nothing to read: the search already answered with the whole folder, which
    // is what a matching folder name usually does on this network.
    if (nameMatched(folder)) return;
  }
  folder.reading = true;
  reading.queue.push([tab, folder]);
  pumpFolderReads();
}

function pumpFolderReads() {
  while (reading.busy < reading.atOnce && reading.queue.length) {
    const [tab, folder] = reading.queue.shift();
    reading.busy += 1;
    readFolder(tab, folder).then(() => {
      reading.busy -= 1;
      pumpFolderReads();
    });
  }
}

async function readFolder(tab, folder) {
  let result;
  try {
    result = await api().acquisition_folder(tab.id, folder.id);
  } catch (error) {
    result = { ok: false, error: String(error) };
  }
  folder.reading = false;
  // The tab may have been closed while this was in flight, which is ordinary.
  if (!acquire.tabs.includes(tab)) return;
  if (result.ok) {
    folder.matched = folder.matched ?? folder.tracks.length;
    folder.tracks = result.tracks;
    folder.files = result.files;
    folder.size = result.size;
    folder.full = true;
  } else {
    folder.unreachable = true;
    folder.note = result.offline ? STR.acquireFolderOffline : result.error;
  }
  // Only this row is redrawn. Rebuilding the tree for each of a hundred answers
  // would throw away scroll position and focus a hundred times over.
  if (tab.id === acquire.active) redrawFolder(tab, folder);
}

/** Show everything in one folder, from whichever row asked.
 *
 * One function because two rows offer it and they must not drift: the folder row
 * and any track inside it. The row is opened first, because the answer is a list
 * and a list under a shut row is an answer nobody sees.
 */
function seeAllFiles(tab, folder) {
  if (!folderIsOpen(tab, folder)) toggleFolder(tab, folder);
  readFolderSoon(tab, folder, { asked: true });
  redrawFolder(tab, folder);
}

function redrawFolder(tab, folder) {
  const rows = $("acquire-rows");
  const row = rows.querySelector(`.acquire-row[data-folder="${folder.id}"]`);
  const box = rows.querySelector(`.acquire-files[data-folder="${folder.id}"]`);
  if (row) row.replaceWith(acquireRow(tab, folder));
  if (box) box.replaceWith(acquireFiles(tab, folder));
}

function folderIsOpen(tab, folder) {
  // "Keep trees open" is a standing setting, and a row shut by hand still
  // shuts: the exception is remembered rather than the rule.
  if (state.keepTreesOpen) return !tab.shut.has(folder.id);
  return tab.open.has(folder.id);
}

function toggleFolder(tab, folder) {
  if (state.keepTreesOpen) {
    // Opening a row by hand means it, even when everything is already open:
    // that is the gesture that asks the source for the rest of the folder.
    if (tab.shut.has(folder.id)) {
      tab.shut.delete(folder.id);
      tab.open.add(folder.id);
    } else if (tab.open.has(folder.id)) {
      tab.open.delete(folder.id);
      tab.shut.add(folder.id);
    } else {
      tab.shut.add(folder.id);
    }
  } else if (tab.open.has(folder.id)) {
    tab.open.delete(folder.id);
  } else {
    tab.open.add(folder.id);
  }
  renderAcquire();
}

/** Say whether music can be acquired, and when it cannot, which thing is wrong.
 *
 * The fallback describes nothing rather than describing offline. A map ending
 * in `|| STR.acquireOffline` would show every unlisted reason — a key that was
 * never given, a key the server refused, an unexpected failure — as a server
 * that is not answering. A reason this window has no sentence for carries the
 * backend's own text.
 */
async function acquireRefreshState() {
  const state = await api().acquisition_state();
  const say = {
    no_provider: STR.acquireAbsent,
    disabled: STR.acquireDisabled,
    // Two states, two gestures: give this application a key, or correct the key
    // it already has. Both name where the field is, because the field exists.
    no_credential: STR.acquireNoKey,
    credential_rejected: STR.acquireKeyRejected,
    offline: STR.acquireOffline,
  };
  const blocked = !state.available;
  $("acquire-go").disabled = blocked;
  $("acquire-query").disabled = blocked;
  $("acquire-organize").disabled = blocked;
  if (blocked) {
    $("acquire-status").textContent =
      say[state.reason] || STR.acquireUnusable(state.detail || state.reason || "");
  }
  return state.available;
}

async function startAcquireSearch() {
  const query = $("acquire-query").value.trim();
  if (!query) return;
  // Plain text, the way Soulseek asks. The network does not distinguish an
  // album search from a track one; the words are the whole question.
  const result = await withSpinner($("acquire-go"), () => api().acquisition_search(query));
  if (!result.ok) {
    $("acquire-status").textContent = result.error;
    return;
  }
  // A search always opens its own tab, so a second one never takes the first
  // one's place — and the ones you have found stay found.
  acquire.tabs.push({
    id: result.id,
    // A pasted Spotify link is answered with the words it became, and the tab
    // is labelled with those — a tab labelled with the address would name the
    // gesture instead of the search.
    query: result.query || query,
    folders: [],
    searching: true,
    status: STR.acquireSearching,
    sort: null,
    descending: false,
    open: new Set(), // opened by hand, when folders are not all open
    shut: new Set(), // shut by hand, when they are
    requested: new Map(), // folder id -> Set of file names already asked for
  });
  acquire.active = result.id;
  $("acquire-query").value = "";
  renderAcquireTabs();
  renderAcquire();
  if (!acquire.polling) acquire.polling = setInterval(pollAcquire, 700);
}

async function pollAcquire() {
  // One timer for every tab: each asks after its own search, and the timer
  // stops once none of them is still waiting on the network.
  const waiting = acquire.tabs.filter((tab) => tab.searching);
  if (!waiting.length) {
    clearInterval(acquire.polling);
    acquire.polling = null;
    return;
  }
  for (const tab of waiting) {
    const result = await api().acquisition_results(tab.id);
    // The tab may have been closed while this was in flight, which is ordinary.
    if (!acquire.tabs.includes(tab)) continue;
    if (result.searching) {
      tab.status = STR.acquireHeard(result.sources || 0, result.files || 0);
      if (tab.id === acquire.active) $("acquire-status").textContent = tab.status;
      continue;
    }
    tab.searching = false;
    if (result.error) {
      tab.status = result.error;
    } else {
      tab.folders = result.folders || [];
      // The window and the engine on the other side of the bridge are updated
      // by different gestures — reopening this window brings new script, and
      // only restarting the application brings new Python. When they disagree
      // this field is missing, every row scores the same, and the list quietly
      // reverts to the order peers answered in with nothing open. That is
      // indistinguishable from a broken sort, so it is said out loud.
      if (tab.folders.length && !tab.folders.some((folder) => folder.relevance)) {
        toastError(STR.acquireStaleEngine);
      }
      // A folder whose own name matched comes open. Soulseek clients do this,
      // and they can: when the name matched, the search already answered with
      // every file in that folder, so opening it costs nothing and asks nobody.
      for (const folder of tab.folders) if (nameMatched(folder)) tab.open.add(folder.id);
      const sources = new Set(tab.folders.map((f) => f.source)).size;
      tab.status = tab.folders.length
        ? STR.acquireFound(tab.folders.length, sources)
        : STR.acquireNothing;
    }
    renderAcquireTabs();
    if (tab.id === acquire.active) renderAcquire();
  }
}

async function closeAcquireTab(id) {
  const index = acquire.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return;
  acquire.tabs.splice(index, 1);
  if (acquire.active === id) {
    // Land on the neighbour rather than on nothing, which is what a closed tab
    // does everywhere else.
    const next = acquire.tabs[index] || acquire.tabs[index - 1] || null;
    acquire.active = next ? next.id : null;
  }
  renderAcquireTabs();
  renderAcquire();
  // Told last, and not awaited before the tab goes: closing is instant on
  // screen, and stopping the search at the server is the server's own business.
  await api().acquisition_close(id);
}

function renderAcquireTabs() {
  const strip = $("acquire-tabs");
  strip.textContent = "";
  strip.hidden = acquire.tabs.length === 0;
  for (const tab of acquire.tabs) {
    const chip = document.createElement("div");
    chip.className = `acquire-tab${tab.id === acquire.active ? " is-active" : ""}`;

    const name = document.createElement("button");
    name.type = "button";
    name.className = "acquire-tab-name";
    name.setAttribute("role", "tab");
    name.setAttribute("aria-selected", tab.id === acquire.active ? "true" : "false");
    name.textContent = tab.query;
    name.title = tab.status;
    name.addEventListener("click", () => {
      acquire.active = tab.id;
      renderAcquireTabs();
      renderAcquire();
    });

    const count = document.createElement("span");
    count.className = "acquire-tab-count";
    count.textContent = tab.searching ? "…" : String(tab.folders.length);

    const shut = document.createElement("button");
    shut.type = "button";
    shut.className = "acquire-tab-close";
    shut.textContent = "×";
    shut.title = STR.acquireTabClose;
    shut.addEventListener("click", (event) => {
      event.stopPropagation();
      closeAcquireTab(tab.id);
    });

    chip.append(name, count, shut);
    strip.appendChild(chip);
  }
}

function sortedFolders(tab) {
  // Until a column is clicked, the order is relevance: the network answers in
  // the order peers reply, which can put single tracks from compilations above
  // every album by the artist searched for. The score is a list of ranks,
  // compared in order, so speed can never outweigh the artist's name being in
  // the folder.
  if (!tab.sort) {
    return [...tab.folders].sort((a, b) => compareRanks(b.relevance, a.relevance));
  }
  const { read } = ACQUIRE_SORTS[tab.sort];
  const ordered = [...tab.folders].sort((a, b) => {
    const left = read(a);
    const right = read(b);
    return left < right ? -1 : left > right ? 1 : 0;
  });
  return tab.descending ? ordered.reverse() : ordered;
}

function compareRanks(left, right) {
  const a = left || [];
  const b = right || [];
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const difference = (a[i] || 0) - (b[i] || 0);
    if (difference) return difference;
  }
  return 0;
}

function sortAcquireBy(column) {
  const tab = activeTab();
  if (!tab || !ACQUIRE_SORTS[column]) return;
  // Same column again reverses it; a different column starts its own way round.
  tab.descending = tab.sort === column ? !tab.descending : ACQUIRE_SORTS[column].biggestFirst;
  tab.sort = column;
  // The rows are about to be rebuilt in a new order, so which folders were open
  // is kept by identity rather than by position.
  renderAcquire();
}

function renderAcquire() {
  const rows = $("acquire-rows");
  const tab = activeTab();
  rows.textContent = "";
  $("acquire-results").hidden = !tab || tab.folders.length === 0;
  $("acquire-status").textContent = tab ? tab.status : STR.acquireNoTabs;
  for (const head of document.querySelectorAll("[data-sort]")) {
    const sorted = tab && head.dataset.sort === tab.sort;
    head.classList.toggle("is-sorted", Boolean(sorted));
    head.dataset.direction = sorted ? (tab.descending ? "desc" : "asc") : "";
  }
  if (!tab) return;
  for (const folder of sortedFolders(tab)) {
    rows.appendChild(acquireRow(tab, folder));
    if (!folderIsOpen(tab, folder)) continue;
    rows.appendChild(acquireFiles(tab, folder));
    // Only a row opened by hand is read from its source. Soulseek clients do
    // not browse either: they show what the search answered, which is the
    // whole folder when the folder's name matched and one track when one track
    // did. Reading every row of a hundred because "show every folder's files"
    // is on would ask a hundred peers a question nobody asked.
    if (tab.open.has(folder.id)) readFolderSoon(tab, folder);
  }
}

function acquireRow(tab, folder) {
  const row = document.createElement("button");
  row.className = "acquire-row";
  row.type = "button";
  row.dataset.folder = folder.id;
  row.setAttribute("aria-expanded", folderIsOpen(tab, folder) ? "true" : "false");
  row.title = STR.tipAcquireRow;
  row.style.userSelect = "none";

  const user = document.createElement("span");
  user.textContent = folder.source;
  user.className = "acquire-folder";

  // A dot when a slot is free, and nothing at all when there is not. A queue
  // length on every busy row is noise in a list read by scanning; the number
  // is on hover, where it costs nothing until it is wanted.
  const free = document.createElement("span");
  free.className = `acquire-free ${folder.free ? "is-free" : "is-busy"}`;
  free.textContent = folder.free ? "●" : "";
  free.title = folder.free ? STR.tipAcquireFree : STR.tipAcquireQueued(folder.queue);

  const rate = document.createElement("span");
  rate.className = "acquire-num";
  rate.textContent = Math.round(folder.rate / 1000);

  const name = document.createElement("span");
  name.className = "acquire-folder";
  // The whole path, so a truncated one is still readable in full on hover.
  name.title = folder.folder;
  const twist = document.createElement("span");
  twist.className = "twist";
  twist.textContent = "▸";
  name.append(twist, ` ${folder.folder}`);
  if (wasRequested(tab, folder)) {
    row.classList.add("is-requested");
    // The arrow says it was asked for; the colour says it without being read.
    const taken = document.createElement("span");
    taken.className = "requested-mark";
    taken.textContent = "↓";
    taken.title = STR.acquireAlreadyRequested;
    name.append(" ", taken);
  }

  // The folder occupies the Folder column; File and Attributes belong to the
  // files underneath it, so here they carry what describes the folder as a
  // whole and nothing more.
  const howMany = document.createElement("span");
  howMany.className = "muted";
  // Two different facts. Until the folder is read, the number is how many
  // files the *words* matched, and it is labelled as that — otherwise a folder
  // of eighteen files reads as a folder of one. Once the folder has been read
  // it is the folder's own count and says so.
  if (folder.reading) {
    howMany.textContent = STR.acquireReading;
  } else if (folder.full) {
    howMany.textContent = STR.acquireFileCount(folder.files);
  } else {
    howMany.textContent = STR.acquireMatchedCount(folder.files);
    howMany.title = STR.tipAcquireMatched;
  }

  const size = document.createElement("span");
  size.className = "acquire-num";
  size.textContent = megabytes(folder.size);

  const spread = document.createElement("span");
  spread.className = "muted";
  spread.textContent = folderAttributes(folder);

  row.append(user, free, rate, name, howMany, size, spread);
  // Before the toggle, which redraws the list: a pick recorded afterwards is
  // recorded into rows that no longer exist.
  pickRow(row, "acquire", `folder:${folder.id}`);
  // A double click fetches the whole folder. It also fires two plain clicks
  // first, which toggle this row open and shut again — the row ends where it
  // started, so nothing has to be undone.
  row.addEventListener("dblclick", (event) => {
    event.preventDefault();
    downloadFolder(tab, folder, []);
  });
  row.addEventListener("click", () => toggleFolder(tab, folder));
  row.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    openAcquireMenu(event, [
      {
        // The count, when it is known, because "the whole folder" is the answer
        // to "how much of this am I about to ask for?" and a compilation folder
        // is where that number can be very large.
        label: folder.full
          ? STR.menuDownloadFolderCount(folder.tracks.length)
          : STR.menuDownloadFolder,
        run: () => downloadFolder(tab, folder, []),
      },
      {
        // The gesture that answers the compilation case: the search shows the
        // tracks whose names carried the words, and this shows the folder they
        // are in — the rest of the album, the cover, the log.
        label: STR.menuSeeAllFiles,
        run: () => seeAllFiles(tab, folder),
      },
      { label: STR.menuCopyName, run: () => copyName(folder.folder) },
    ]);
  });
  return row;
}

function acquireFiles(tab, folder) {
  const box = document.createElement("div");
  box.className = "acquire-files";
  box.dataset.folder = folder.id;

  // What the row is showing, said plainly: the folder, or only what the words
  // matched because its source has gone offline.
  if (folder.reading || folder.unreachable || folder.full) {
    const note = document.createElement("p");
    note.className = "acquire-note muted";
    note.textContent = folder.reading
      ? STR.acquireReading
      : folder.unreachable
        ? folder.note || STR.acquireFolderOffline
        : STR.acquireFolderFull(folder.tracks.length, folder.matched ?? folder.tracks.length);
    box.appendChild(note);
  }

  for (const track of folder.tracks) {
    const line = document.createElement("div");
    line.className = "acquire-file";

    // No tick beside the name. A track downloads on a double click and from
    // its own menu, so a box would be a second way to say a thing already
    // sayable, taking room on every line of every open album.
    const label = document.createElement("span");
    label.title = STR.tipAcquireTrack;
    label.textContent = track.name;
    if (wasRequested(tab, folder, track.name)) {
      line.classList.add("is-requested");
      const taken = document.createElement("span");
      taken.className = "requested-mark";
      taken.textContent = "↓";
      taken.title = STR.acquireAlreadyRequested;
      label.append(" ", taken);
    }

    const size = document.createElement("span");
    size.className = "acquire-num";
    size.textContent = megabytes(track.size);

    const attrs = document.createElement("span");
    attrs.className = "muted";
    attrs.textContent = track.attributes;

    pickRow(line, "acquire", `file:${folder.id}:${track.name}`);
    // Double clicking one track fetches that track alone.
    line.addEventListener("dblclick", (event) => {
      event.preventDefault();
      downloadFolder(tab, folder, [track.name]);
    });

    line.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      openAcquireMenu(event, [
        { label: STR.menuDownloadFile, run: () => downloadFolder(tab, folder, [track.name]) },
        {
          label: folder.full
            ? STR.menuDownloadFolderCount(folder.tracks.length)
            : STR.menuDownloadFolder,
          run: () => downloadFolder(tab, folder, []),
        },
        // Here as well as on the folder row above. The track that matched the
        // words is where the request starts — this is the one searched for,
        // now show the rest of the record — so this row offers it too.
        { label: STR.menuSeeAllFiles, run: () => seeAllFiles(tab, folder) },
        { label: STR.menuCopyName, run: () => copyName(track.name) },
      ]);
    });

    // The name sits in the File column; the columns to its left belong to the
    // folder, and a file has no user or rate of its own.
    const cell = document.createElement("span");
    cell.className = "acquire-file-name";
    cell.append(label);

    // Four empty cells so this file lands under File, Size and Attributes.
    for (let i = 0; i < 4; i += 1) line.appendChild(document.createElement("span"));
    line.append(cell, size, attrs);
    box.appendChild(line);
  }

  // No button under the list. Downloading is a double click on a folder or a
  // track, or an entry of the right-click menu, and a button repeating that
  // would be noise in every open row.
  return box;
}

function openAcquireMenu(event, items) {
  // A menu with nothing in it is not a menu. Handed an empty list, this would
  // open a small bordered box with no text in it. Every caller is guarded from
  // here rather than at each call site.
  //
  // Nothing is drawn and nothing is left open: a press that has nothing to
  // offer must not leave the previous menu standing either.
  if (!items.length) {
    closeAcquireMenu();
    return;
  }
  // Opening a menu is not a way of selecting text. The window makes everything
  // selectable on purpose — a path, a refusal — and WebKit takes a right-click
  // on a word as a selection, so the row is left highlighted behind the menu.
  const selection = window.getSelection();
  if (selection && selection.isCollapsed === false) selection.removeAllRanges();

  const menu = $("acquire-menu");
  // A modal dialog makes everything outside itself unclickable, and this menu
  // lives at the end of the body. With a dialog open, `elementFromPoint` over
  // a drawn menu item answers `dialog`: the item is painted in the top layer
  // and the press never reaches it.
  //
  // So the menu goes *inside* whichever dialog is open, and back to the body
  // when none is. Written as "the open dialog" rather than by naming one
  // dialog, so a menu opened from a modal screen added later works too.
  const host = document.querySelector("dialog[open]") || document.body;
  if (menu.parentElement !== host) host.appendChild(menu);
  menu.textContent = "";
  for (const item of items) {
    // A heading is not an entry. Two lists in one menu read as one list of
    // everything unless each is headed by the question it answers. Drawn as
    // text and never focusable, so it cannot be arrowed onto or pressed.
    if (item.heading) {
      const heading = document.createElement("div");
      heading.className = "context-heading";
      heading.textContent = item.heading;
      menu.appendChild(heading);
      continue;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "context-item";
    button.textContent = item.label;
    // A menu entry can explain itself. Without this the sentences written for
    // the most consequential entries could never be read by anybody.
    if (item.title) button.title = item.title;
    button.addEventListener("click", () => {
      closeAcquireMenu();
      item.run();
    });
    menu.appendChild(button);
  }
  closeAcquireMenu();
  menu.showPopover();
  // Placed after it is shown, because a hidden popover measures zero and a menu
  // has to know its own size to stay inside the window. Both edges are held:
  // keeping it off the right edge alone puts it off the left one instead the
  // moment the window is narrower than the menu.
  menu.style.left = `${within(event.clientX, menu.offsetWidth, window.innerWidth)}px`;
  menu.style.top = `${within(event.clientY, menu.offsetHeight, window.innerHeight)}px`;
  // Dismissed by this window's own rules rather than the popover's. An `auto`
  // popover light-dismisses on the next press outside it, and the rest of the
  // very right-click that opened it counts as outside: the menu appeared and
  // vanished before it could be read. These listeners are hung a tick later, so
  // the click that opened the menu cannot be the click that closes it.
  setTimeout(() => {
    document.addEventListener("pointerdown", dismissAcquireMenu, true);
    document.addEventListener("keydown", dismissAcquireMenu, true);
    document.addEventListener("pointermove", dismissMenuWhenPointerLeaves, true);
  }, 0);
}

function dismissAcquireMenu(event) {
  // A press on the menu itself is a choice, not a dismissal; the item's own
  // handler closes it. Any key at all closes it, Escape included.
  if (event.type === "pointerdown" && event.target.closest("#acquire-menu")) return;
  closeAcquireMenu();
}

/** How far the pointer may stray from the menu before it is gone, in pixels.
 *
 * Not zero. The menu opens at the pointer, so its first entry is already under
 * the hand, but reaching the third one means travelling down its edge — and a
 * menu that closes the instant the pointer clips its border is a menu that
 * cannot be used with a trackpad.
 */
const MENU_REACH = 56;

/** Close the menu once the pointer has gone somewhere else.
 *
 * Otherwise it stays on screen, over whatever is behind it, until something is
 * pressed.
 *
 * Written as *distance from the menu* rather than as "the pointer entered and
 * then left", because that second form needs a flag to remember the entering,
 * and the flag is wrong in the case that matters: a menu the pointer never
 * reached would keep the old value and never close.
 */
function dismissMenuWhenPointerLeaves(event) {
  const box = $("acquire-menu").getBoundingClientRect();
  const strayed = Math.max(
    box.left - event.clientX,
    event.clientX - box.right,
    box.top - event.clientY,
    event.clientY - box.bottom,
  );
  if (strayed > MENU_REACH) closeAcquireMenu();
}

function closeAcquireMenu() {
  document.removeEventListener("pointerdown", dismissAcquireMenu, true);
  document.removeEventListener("keydown", dismissAcquireMenu, true);
  document.removeEventListener("pointermove", dismissMenuWhenPointerLeaves, true);
  const menu = $("acquire-menu");
  if (menu.matches(":popover-open")) menu.hidePopover();
}

function within(at, size, limit) {
  return Math.max(8, Math.min(at, limit - size - 8));
}

async function copyName(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(STR.menuCopied);
  } catch {
    // Some webviews refuse the clipboard API outright. The old way still works
    // there, and a name that cannot be copied at all is worth saying nothing
    // about rather than raising an error nobody can act on.
    const hidden = document.createElement("textarea");
    hidden.value = text;
    hidden.style.position = "fixed";
    hidden.style.opacity = "0";
    document.body.appendChild(hidden);
    hidden.select();
    const copied = document.execCommand("copy");
    hidden.remove();
    if (copied) toast(STR.menuCopied);
  }
}

// What counts as a folder somebody meant to take whole. Above either of these
// it is asked about first: a fully read folder can hold hundreds of files
// behind one matched name, and a double click should not quietly queue that.
const BIG_FOLDER_FILES = 50;
const BIG_FOLDER_BYTES = 1e9;

function asksFirst(folder) {
  return folder.files > BIG_FOLDER_FILES || folder.size > BIG_FOLDER_BYTES;
}

/** Ask through one of the window's own dialogs, and read the answer off its
 * two buttons.
 *
 * Not `confirm()`: a native modal is one this webview may decline to show, and
 * a question that never appears reads as a download that never happened.
 *
 * And not the dialog's own `close` event either. In this webview the dialog
 * can close with no `close` event arriving here, so a promise waiting on one
 * waits forever. The two buttons are the answer; `cancel` is listened to as
 * well so the Escape key still means no where it does arrive.
 *
 * General on purpose: everything above is a fact about this webview and not
 * about any one question, so every confirmation goes through here.
 */
function askThrough(dialog, yes, no) {
  return new Promise((decide) => {
    let answered = false;
    const settle = (taken) => {
      if (answered) return;
      answered = true;
      yes.removeEventListener("click", take);
      no.removeEventListener("click", refuse);
      dialog.removeEventListener("cancel", refuse);
      if (dialog.open) dialog.close();
      decide(taken);
    };
    const take = () => settle(true);
    const refuse = () => settle(false);
    yes.addEventListener("click", take);
    no.addEventListener("click", refuse);
    dialog.addEventListener("cancel", refuse);
    dialog.showModal();
  });
}

/** Applying leaves files in doubt exactly as they are — go ahead? */
function confirmApplyLeavesDoubtful(count) {
  // The count is the whole of this question, so it is written into the dialog
  // rather than into a heading that would then be true for one album only.
  $("apply-doubtful-message").textContent = STR.applyLeavesDoubtful(count);
  return askThrough(
    $("apply-doubtful-dialog"),
    $("apply-doubtful-yes"),
    $("apply-doubtful-no"),
  );
}

/** This write is made of your tags — say which of the two albums this is, and ask.
 *
 * An album still arrives with a plan when nobody has scanned it, and the
 * button that applies it looks exactly like the one applying a catalogue's
 * answer, so the write says what it is made of before it runs.
 *
 * Written here rather than left to `data-str`, because the sentence depends on
 * the album: a dialog whose words are filled once at load would say the same
 * thing about both cases.
 */
function confirmApplyWithoutScan(catalogueAsked) {
  $("apply-unscanned-heading").textContent = catalogueAsked
    ? STR.applyInsteadOfScanHeading
    : STR.applyWithoutScanHeading;
  $("apply-unscanned-message").textContent = catalogueAsked
    ? STR.applyInsteadOfScan
    : STR.applyWithoutScan;
  return askThrough(
    $("apply-unscanned-dialog"),
    $("apply-unscanned-yes"),
    $("apply-unscanned-no"),
  );
}

/** That is a large folder — take the whole thing? */
function confirmBigFolder(folder) {
  $("big-folder-message").textContent = STR.acquireBigFolder(folder.files, megabytes(folder.size));
  return askThrough($("big-folder-dialog"), $("big-folder-yes"), $("big-folder-no"));
}

/** Has this row, or this track in it, been asked for?
 *
 * A folder counts as taken only when the folder was taken. Asking for one
 * track must not mark the whole row, which would claim an album was requested
 * when one song of it was.
 */
function wasRequested(tab, folder, track) {
  if (track) return (tab.requested.get(folder.id) || new Set()).has(track);
  return Boolean(folder.requested);
}

function markRequested(tab, folder, names) {
  if (!names || names.length === 0) {
    folder.requested = true;
    tab.requested.set(folder.id, new Set(folder.tracks.map((t) => t.name)));
  } else {
    const already = tab.requested.get(folder.id) || new Set();
    for (const name of names) already.add(name);
    tab.requested.set(folder.id, already);
  }
  redrawFolder(tab, folder);
}

async function downloadFolder(tab, folder, names) {
  const whole = !names || names.length === 0;
  if (whole && asksFirst(folder) && !(await confirmBigFolder(folder))) return;
  // The search is named as well as the folder: a folder identifier means
  // something only inside the search that minted it, and two tabs both have an
  // ``f0``.
  const result = await api().acquisition_download(tab.id, folder.id, names);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  // Said on the row as well as in a message that disappears: what has already
  // been asked for is the one thing a results list cannot otherwise show.
  markRequested(tab, folder, names);
  // What the service TOOK, not what it was asked for. Reporting eighteen
  // queued while one was accepted misstates the only thing the gesture was
  // for, and it is a failure, so it stays on screen.
  const queued = result.queued;
  if (typeof queued === "number" && queued < result.requested) {
    toastError(STR.acquireQueuedShort(queued, result.requested, folder.source));
    return;
  }
  toast(STR.acquireQueued(typeof queued === "number" ? queued : result.requested, folder.source));
}

/** Ask for finished downloads to be brought in, and wait to be told they were.
 *
 * Reading an album off the disk happens on a worker thread, so asking and then
 * reading the library reads it before the answer exists: the snapshot races
 * the question and shows a library without the album that has just arrived.
 *
 * `state.collecting` keeps the event poll alive until the answer comes back,
 * the same way a running scan does.
 */
function collectDownloads() {
  if (state.collecting) return;
  state.collecting = true;
  api()
    .acquisition_collect()
    .then((result) => {
      // Nothing was waiting, so there is no event coming and nothing to wait
      // for. `busy` means another collect is already running and will emit.
      if (!result || (!result.started && !result.busy)) state.collecting = false;
      else pollEvents();
    })
    .catch(() => {
      state.collecting = false;
    });
}

async function organizeDownloads() {
  const result = await api().acquisition_organize();
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  // What this press is for, so the end of the run can finish the gesture: the
  // album goes to the Library and its dialog opens, and from there it is the
  // ordinary sequence — nothing is organized without being asked.
  state.organizing = true;
  switchTab("library");
  scanBegan();
  refresh();
}

function folderAttributes(folder) {
  // What every file in the folder agrees on, when they agree — the shorthand a
  // person reads to tell a FLAC rip from a 320 one without opening the row.
  const seen = new Set(
    folder.tracks.map((track) => (track.attributes || "").split(",")[0].trim()).filter(Boolean),
  );
  return seen.size === 1 ? [...seen][0] : "";
}

// --- transfers ---------------------------------------------------------------
// Everything slskd is holding, from everyone. It is asked without naming a
// source, because this screen cannot know the sources in advance: asking is how
// it finds out which they are.
//
// Drawn as one tree in one Name column, rather than as User, Folder and File
// side by side. Three name columns, each holding something as long as an
// album folder's full name, push everything worth reading — the status, the
// estimate, the speed — off to the right of a very wide screen. Indentation
// says the same thing in one column and gives the rest of the width back.

const transfers = {
  polling: null,
  users: [],
  open: new Set(),
  status: "",
  // Whether every user and album comes open is `state.keepTreesOpen`: the
  // list is short enough that open is a reasonable place to start.

};

/** Open or close both trees for this session, without touching the default.
 *
 * Settings says how a screen opens; this says what is wanted right now.
 * Keeping them apart is what lets everything be shut once without losing the
 * setting that was chosen.
 */
function setKeepTreesOpen(wanted) {
  state.keepTreesOpen = wanted;
  setTreeButtons();
  renderTransfers();
  renderAcquire();
}

/** Each button says what pressing it would do, not what is already true. */
function setTreeButtons() {
  const label = state.keepTreesOpen ? STR.closeTree : STR.openTree;
  const tip = state.keepTreesOpen ? STR.tipCloseTree : STR.tipOpenTree;
  for (const id of ["transfers-keep-open", "acquire-keep-open"]) {
    const button = $(id);
    if (!button) continue;
    button.textContent = label;
    button.title = tip;
  }
}

/** Whether one node is unfolded: the setting, unless this node was toggled. */
function nodeIsOpen(key) {
  return state.keepTreesOpen ? !transfers.open.has(`shut:${key}`) : transfers.open.has(key);
}

function toggleTransferNode(key) {
  // With "keep open" on, the set remembers what has been *shut*; with it off,
  // it remembers what has been opened. One set, read either way, so turning
  // the setting on does not throw away what was already unfolded.
  const marker = state.keepTreesOpen ? `shut:${key}` : key;
  if (transfers.open.has(marker)) transfers.open.delete(marker);
  else transfers.open.add(marker);
  renderTransfers();
}

const TRANSFER_COLUMNS = "minmax(28ch, 1fr) 12ch 13ch 8ch 10ch 12ch 7ch";

function watchTransfers(watching) {
  clearInterval(transfers.polling);
  transfers.polling = null;
  if (!watching) return;
  refreshTransfers();
  transfers.polling = setInterval(refreshTransfers, 2000);
}

async function refreshTransfers() {
  const result = await api().acquisition_transfers();
  if (!result.ok) {
    transfers.users = [];
    $("transfers-rows").textContent = "";
    $("transfers-status").textContent = result.error || STR.transfersOffline;
    return;
  }
  transfers.users = result.users || [];
  renderTransfers();
  // A download finishing is the moment the library most needs to be brought
  // up to date, and nothing else on this screen refreshes the library. Asked
  // only when something has actually finished — the question is cheap, but it
  // is not free, and this runs every two seconds.
  if (transfers.users.some((user) => user.done)) collectDownloads();
}

async function clearTransfers() {
  const result = await api().acquisition_clear();
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  toast(STR.transfersCleared);
  refreshTransfers();
}

/** Everything under one node, whichever kind of node it is.
 *
 * A right-click answers to where it lands: on a track it means that track, on
 * an album the whole album, on a user everything of theirs. One function
 * flattens whichever was clicked so the rest of this file never has to ask.
 */
function filesUnder(node) {
  if (node.files && Array.isArray(node.files)) return node.files;
  if (node.folders) return node.folders.flatMap((folder) => folder.files);
  return [node];
}

function renderTransfers() {
  const rows = $("transfers-rows");
  const results = $("transfers-results");
  const head = $("transfers-head");
  const nothing = transfers.users.length === 0;
  $("transfers-status").textContent = nothing ? STR.transfersNothing : transfers.status;
  results.hidden = nothing;

  if (head.dataset.built !== "yes") {
    head.dataset.built = "yes";
    head.textContent = "";
    for (const label of [
      STR.colName,
      STR.colStatus,
      STR.colProgress,
      STR.colEta,
      STR.colRate,
      STR.colSize,
      STR.colPlace,
    ]) {
      const cell = document.createElement("span");
      cell.textContent = label;
      head.appendChild(cell);
    }
    results.style.setProperty("--transfer-columns", TRANSFER_COLUMNS);
    resizableGrid("transfers.tree", head, results, "--transfer-columns");
  }

  rows.textContent = "";
  for (const user of transfers.users) {
    rows.appendChild(userRow(user));
    if (!nodeIsOpen(`u:${user.source}`)) continue;
    for (const folder of user.folders) {
      rows.appendChild(folderRow(user, folder));
      if (!nodeIsOpen(`f:${user.source}:${folder.directory}`)) continue;
      for (const file of folder.files) rows.appendChild(fileRow(user, folder, file));
    }
  }
}

/** One row of the tree, at whatever depth, with the columns every row shares. */
function transferLine(depth, label, node, source) {
  const line = document.createElement("div");
  line.className = `transfer-line is-depth-${depth}`;
  // Before the callers add their own click, which opens the node and redraws
  // the tree. Depth is part of the key because a folder and the file in it can
  // carry the same name.
  pickRow(line, "transfers", `${depth}:${source}:${label}`);

  const name = document.createElement("span");
  name.className = "transfer-name";
  name.title = label;
  const files = filesUnder(node);

  if (depth < 2) {
    const twist = document.createElement("span");
    twist.className = "twist";
    twist.textContent = "▸";
    name.appendChild(twist);
  }
  // Indentation says which level a row is on, but not what a level *means*,
  // so each row names its kind: a user, a folder or a file.
  const kind = document.createElement("span");
  kind.className = "transfer-kind";
  kind.textContent = [STR.colUser, STR.colFolder, STR.colFile][depth];
  name.appendChild(kind);

  const text = document.createElement("span");
  text.className = "transfer-label";
  text.textContent = label;
  name.appendChild(text);

  // A folder that stopped short says so, whatever its files are called. slskd
  // can report every transfer `completed` and still hand over a file with
  // bytes missing, and the row must not read as finished while the Library
  // refuses the folder. The server works the condition out (`stopped_short`)
  // and this reads it.
  const summary = node.stopped_short ? "incomplete" : summaryState(files);
  const status = document.createElement("span");
  status.className = `transfer-state is-${summary}`;
  status.textContent = STR.transferState(summary);

  const progress = document.createElement("span");
  progress.className = "transfer-progress";
  const share = shareOf(files);
  // The bar and the number together: the bar is read across a list to find
  // which is nearly there, and the number is read on one row to know how near.
  if (share !== null) {
    const bar = document.createElement("span");
    bar.className = "transfer-bar";
    const fill = document.createElement("span");
    fill.className = "transfer-fill";
    fill.style.width = `${share}%`;
    bar.appendChild(fill);
    progress.append(bar, percent(share));
  }

  const eta = document.createElement("span");
  eta.className = "transfer-num";
  eta.textContent = etaOf(node, files);

  const rate = document.createElement("span");
  rate.className = "transfer-num";
  rate.textContent = kilobytesPerSecond(node.rate);

  const size = document.createElement("span");
  size.className = "transfer-num";
  size.textContent = megabytes(sizeOf(files));

  const place = document.createElement("span");
  place.className = "transfer-num";
  place.textContent = placeOf(files);

  line.append(name, status, progress, eta, rate, size, place);
  transferMenu(line, source, node, label);
  return line;
}

function userRow(user) {
  const key = `u:${user.source}`;
  const line = transferLine(0, user.source, user, user.source);
  line.classList.toggle("is-open", nodeIsOpen(key));
  line.addEventListener("click", () => toggleTransferNode(key));
  return line;
}

function folderRow(user, folder) {
  const key = `f:${user.source}:${folder.directory}`;
  const line = transferLine(1, folder.folder, folder, user.source);
  line.classList.toggle("is-open", nodeIsOpen(key));
  line.addEventListener("click", () => toggleTransferNode(key));
  return line;
}

function fileRow(user, folder, file) {
  return transferLine(2, file.name, file, user.source);
}

// --- what one row is, read from the transfers under it ----------------------

/** The one word that describes a set of transfers, worst-news-first.
 *
 * A folder with one failed track has something wrong with it and should say so
 * rather than averaging its way to "downloading".
 */
function summaryState(files) {
  const has = (state) => files.some((file) => file.state === state);
  if (has("in_progress")) return "in_progress";
  if (has("failed")) return "failed";
  if (has("queued")) return "queued";
  if (has("cancelled")) return "cancelled";
  return files.every((file) => file.state === "completed") ? "completed" : "unknown";
}

/** How far along, as a whole number, or nothing while nothing has started. */
function shareOf(files) {
  const started = files.filter((file) => file.state !== "queued");
  if (!started.length) return null;
  const size = files.reduce((total, file) => total + (file.size || 0), 0);
  if (!size) return 0;
  const moved = files.reduce(
    (total, file) => total + (file.state === "completed" ? file.size || 0 : file.moved || 0),
    0,
  );
  return Math.min(100, Math.round((moved / size) * 100));
}

function sizeOf(files) {
  return files.reduce((total, file) => total + (file.size || 0), 0);
}

/** When this row ends: everything still to arrive, at the rate it is arriving.
 *
 * A peer sends one file at a time, so the folder's estimate is the whole queue
 * and never the file in front of it: the longest estimate among the files in
 * progress describes one file's share of the wait. Summing what is left and
 * dividing by the rate slskd itself reports is close to the real duration.
 *
 * `still_coming` is counted on the other side of the bridge, where the set of
 * states that are ends is defined once (`connectors/models.py`), so that set
 * has no second copy here.
 *
 * Silent rather than wrong when there is no rate. Nothing moving, or a
 * transfer slskd has not averaged yet, gives no ground for a number, and none
 * is made up.
 */
function etaOf(node, files) {
  // A file row keeps the service's own estimate for itself, unchanged: it is
  // the one row slskd's number is actually about.
  if (typeof node.still_coming !== "number") {
    const moving = files.filter((file) => file.state === "in_progress" && file.eta);
    if (!moving.length) return "—";
    const longest = moving.reduce((furthest, file) =>
      durationSeconds(file.eta) > durationSeconds(furthest.eta) ? file : furthest,
    );
    return shortDuration(longest.eta);
  }
  if (!node.still_coming) return "—";
  if (!node.rate) return "—";
  return roughDuration(node.still_coming / node.rate);
}

/** Where this waits in its source's queue, when the source says at all. */
function placeOf(files) {
  const waiting = files
    .filter((file) => file.state === "queued" && typeof file.place === "number")
    .map((file) => file.place);
  return waiting.length ? String(Math.min(...waiting)) : "—";
}

// --- acting on what was right-clicked ----------------------------------------
// The menu answers to where it lands: a track means that track, an album means
// the whole album, a user means everything of theirs. What it offers is only
// what the states under the pointer allow.
//
// A finished transfer still has a menu. There is no transfer left to pause or
// resume — but there is a *file*, and a file can be revealed, and a finished
// row can be taken off the list.

function transferActions(source, node, label) {
  const files = filesUnder(node);
  const items = [];
  const idsIn = (states) =>
    files.filter((file) => states.includes(file.state)).map((file) => file.id);
  const namesIn = (states) =>
    files.filter((file) => states.includes(file.state)).map((file) => file.remote);
  const moving = idsIn(["queued", "in_progress"]);
  const stopped = namesIn(["cancelled", "failed"]);
  // Everything that is not moving, whatever it is called. Written as "not
  // moving" rather than as a list of terminal states for two reasons. Over a
  // folder already cancelled, `Cancel download` reads as doing it again rather
  // than as taking the row off the screen, so removal is its own entry. And a
  // state this window does not know — slskd wording that maps to `unknown` —
  // would belong to no list at all, so its row could never be removed.
  const done = files.filter((file) => !["queued", "in_progress"].includes(file.state));
  const directory = node.directory || node.folders?.[0]?.directory || node.remote || "";

  // Always first, and always there: wherever this is going is a question that
  // has an answer in every state, including "it has not arrived yet".
  items.push({
    label: STR.menuRevealFolder,
    run: () => openTransferFolder(directory, source, label),
  });
  if (moving.length) {
    items.push({
      label: STR.menuPause(moving.length),
      run: () => stopTransfers(source, moving, false, label),
    });
  }
  if (stopped.length) {
    items.push({
      label: STR.menuRetry(stopped.length),
      run: () => resumeTransfers(source, stopped, label),
    });
  }
  // Everything stopped and not all of it here. The folder does not join the
  // Library short — a truncated file measured is a measurement of a recording
  // that does not exist — but a peer that vanished leaves an album that will
  // never be whole, and refusing it for ever is not this application's call.
  // The server worked out the condition; this only reads it.
  if (node.stopped_short) {
    items.push({
      label: STR.menuTakeIncomplete,
      run: () => takeIncomplete(node.directory || "", label),
    });
  }
  if (moving.length) {
    items.push({
      label: STR.menuCancel(moving.length),
      run: () => stopTransfers(source, moving, true, label),
    });
  }
  if (done.length) {
    // Off the list, never off the disk — the same distinction as Clear
    // downloaded, and stated twice because the cost of getting it wrong is
    // the music itself. One sentence for every way a transfer can be over:
    // cancelled, failed, finished, or in a state this window has no word for.
    items.push({
      label: STR.menuRemoveTransfer(done.length),
      run: () => stopTransfers(source, done.map((file) => file.id), true, label),
    });
  }
  return items;
}

function transferMenu(row, source, node, label) {
  row.addEventListener("contextmenu", (event) => {
    const items = transferActions(source, node, label);
    if (!items.length) return;
    event.preventDefault();
    event.stopPropagation();
    openAcquireMenu(event, items);
  });
}

/** Say what was done and to what, on the screen it was done on. */
function sayTransfers(message) {
  transfers.status = message;
  $("transfers-status").textContent = message;
  toast(message);
}

async function stopTransfers(source, identifiers, remove, label) {
  const result = await api().acquisition_stop(source, identifiers, remove);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  sayTransfers(
    remove
      ? STR.transfersCancelled(result.stopped, label)
      : STR.transfersPaused(result.stopped, label),
  );
  refreshTransfers();
}

async function resumeTransfers(source, names, label) {
  const result = await api().acquisition_resume(source, names);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  // Asked for again from where it stopped: the partial file is still on disk,
  // which is what makes this a resume rather than a fresh download.
  sayTransfers(STR.transfersResumed(result.requested, label));
  refreshTransfers();
}

/** Take a folder that stopped short, exactly as it stands. */
async function takeIncomplete(directory, label) {
  const result = await api().accept_incomplete_download(directory);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  sayTransfers(STR.transfersTakenIncomplete(label));
  refreshTransfers();
}

async function openTransferFolder(directory, source, label) {
  const result = await api().acquisition_open_transfer(directory, source);
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  if (!result.arrived) sayTransfers(STR.transfersNotThereYet(label));
}

// --- how a number is written -------------------------------------------------
// Two decimals and a point, everywhere in this application: `54.22 MB`.
//
// The interface is written in English, and a decimal comma in an English
// sentence reads as a thousands separator. One convention per interface, and
// the interface picks it.

function decimal(value, places = 2) {
  return value.toFixed(places);
}

function megabytes(bytes) {
  if (!bytes) return "—";
  // Named for what it usually says, but a folder is not always album-sized at
  // either end. The cover and the log are kilobytes, and rounding them to
  // megabytes wrote "0.0 MB" against a real file; a share that calls its whole
  // collection one folder is gigabytes, and "7400 MB" is a number nobody reads.
  if (bytes < 1e6) return `${decimal(bytes / 1e3)} KB`;
  if (bytes >= 1e9) return `${decimal(bytes / 1e9)} GB`;
  return `${decimal(bytes / 1e6)} MB`;
}

function percent(share) {
  return `${decimal(share, 0)}%`;
}

/** What the service measured, in kilobytes per second. */
function kilobytesPerSecond(rate) {
  return rate ? decimal(rate / 1e3) : "—";
}

/** One `hh:mm:ss` estimate as a number, or -1 when it is not one at all.
 *
 * `shortDuration` reads the string through this, so there is one reading of
 * it and not two that can drift apart.
 */
function durationSeconds(text) {
  const parts = String(text).split(":").map(Number);
  if (!parts.length || parts.some(Number.isNaN)) return -1;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function shortDuration(text) {
  const seconds = durationSeconds(text);
  if (seconds < 0) return String(text);
  return roughDuration(seconds);
}

/** A number of seconds in the words this screen uses for a wait.
 *
 * Split out of `shortDuration` rather than written beside it: a folder's
 * estimate is arithmetic and arrives as a number, a file's is a string slskd
 * wrote, and two ways of turning a wait into words is how they come to disagree
 * about what nine and a half minutes is called.
 */
function roughDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(seconds / 3600)}h${String(Math.floor((seconds % 3600) / 60)).padStart(2, "0")}m`;
}

// --- resizable columns ------------------------------------------------------
// A handle on the right edge of every heading but the last. Two kinds of table
// live in this window — CSS grids, where every row repeats the same track list,
// and real `<table>`s — so the drag writes whichever of the two that table
// reads, and both remember the widths under a name of their own.

/** These widths are kept by the application, not by the browser.
 *
 * `localStorage` does not survive quitting the app, for two independent
 * reasons: pywebview's `private_mode` defaults to `True` and this window never
 * turns it off, so the webview is given a non-persistent data store; and the
 * window is served from `http://127.0.0.1:<random port>`, so even a persistent
 * store would be a different origin — and therefore a different
 * `localStorage` — at every launch.
 *
 * So they go where `library_sort` goes, which is the store the application
 * owns. One JSON string holding every table's widths, because what is stored is
 * a string either way and a shape that layer does not have to understand is a
 * shape it cannot get half right.
 *
 * Nothing in this window reads `localStorage` for anything.
 */
function allStoredWidths() {
  try {
    const stored = JSON.parse(state.settings.column_widths || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    // A settings row edited by hand is not a reason to lose the window.
    return {};
  }
}

function storedWidths(key) {
  const widths = allStoredWidths()[key];
  return Array.isArray(widths) ? widths : null;
}

async function rememberWidths(key, widths) {
  const all = allStoredWidths();
  all[key] = widths;
  state.settings.column_widths = JSON.stringify(all);
  await api().set_settings({ ...state.settings, column_widths: state.settings.column_widths });
}

// How each table puts widths on itself, kept so they can be applied again when
// the settings arrive — the grips are built while the document is, which is
// before the application has said anything.
const columnAppliers = [];

/** Give every table the widths it was left at, once the settings are here. */
function applyStoredColumnWidths() {
  for (const { key, count, apply } of columnAppliers) {
    const widths = storedWidths(key);
    if (widths && widths.length === count) apply(widths);
  }
}

/** Make a CSS-grid table resizable, by writing its track list as a variable.
 *
 * The variable lives on an ancestor of the heading and of every row, so rows
 * built after the drag — and this window rebuilds rows constantly — pick the
 * widths up without being told.
 */
function resizableGrid(key, head, scope, variable) {
  const apply = (widths) => scope.style.setProperty(variable, widths.map((w) => `${w}px`).join(" "));
  columnAppliers.push({ key, count: head.children.length, apply });
  const stored = storedWidths(key);
  if (stored && stored.length === head.children.length) apply(stored);
  addHandles(key, [...head.children], apply);
}

/** Make a real `<table>` resizable. Its heading survives a redrawn body, so the
 * widths sit on the heading cells themselves. */
function resizableTable(key, table) {
  const headings = [...table.querySelectorAll("thead th")];
  if (!headings.length) return;
  table.style.tableLayout = "fixed";
  const apply = (widths) => {
    headings.forEach((cell, index) => {
      cell.style.width = `${widths[index]}px`;
    });
  };
  columnAppliers.push({ key, count: headings.length, apply });
  const stored = storedWidths(key);
  if (stored && stored.length === headings.length) apply(stored);
  addHandles(key, headings, apply);
}

function addHandles(key, headings, apply) {
  headings.forEach((heading, index) => {
    // The last column takes what is left, so there is nothing to its right to
    // drag against.
    if (index === headings.length - 1 || heading.querySelector(".column-grip")) return;
    heading.classList.add("has-grip");
    const grip = document.createElement("span");
    grip.className = "column-grip";
    grip.title = STR.tipResize;
    grip.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      event.stopPropagation();
      // Measured at the moment of grabbing, because until now these were
      // fractions and characters, and a drag has to work in pixels.
      const widths = headings.map((cell) => cell.getBoundingClientRect().width);
      const startX = event.clientX;
      const startWidth = widths[index];
      // Capture keeps the drag alive when the pointer runs off the grip, which
      // it does immediately. Its absence is not worth losing the drag over.
      try {
        grip.setPointerCapture(event.pointerId);
      } catch {
        // Then the pointer events stop at the edge of the grip, and the column
        // stops growing there. Still a resize, just a shorter one.
      }
      const move = (moved) => {
        widths[index] = Math.max(MIN_COLUMN_PX, startWidth + (moved.clientX - startX));
        apply(widths);
      };
      const done = () => {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", done);
        grip.removeEventListener("pointercancel", done);
        rememberWidths(key, widths);
        // A pointer sequence ends with a click on whatever is underneath, and
        // underneath is the heading — which in the search results is also the
        // control that sorts. Without this, widening a column re-orders the
        // list.
        heading.addEventListener("click", swallow, { capture: true, once: true });
        setTimeout(() => heading.removeEventListener("click", swallow, true), 0);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", done);
      grip.addEventListener("pointercancel", done);
    });
    heading.appendChild(grip);
  });
}

const MIN_COLUMN_PX = 40;

function swallow(event) {
  event.stopPropagation();
  event.preventDefault();
}

/** Keep the column headings parked just below the top bar as the list scrolls.
 *
 * The bar is not one height: the progress strip appears while a scan runs and
 * the scan-done line after it, and a heading pinned to a stale number floats
 * over the bar or leaves a gap under it. So it is measured, and measured again
 * whenever it changes.
 */
function followChromeHeight() {
  const chrome = document.querySelector(".chrome");
  if (!chrome) return;
  const write = () =>
    document.documentElement.style.setProperty(
      "--chrome-height",
      `${Math.round(chrome.getBoundingClientRect().height)}px`,
    );
  write();
  if (typeof ResizeObserver === "function") new ResizeObserver(write).observe(chrome);
  else window.addEventListener("resize", write);
}

/** Give every table in the window its grips, once the window has one. */
function makeColumnsResizable() {
  resizableGrid(
    "acquire",
    document.querySelector(".acquire-head"),
    $("acquire-results"),
    "--acquire-columns",
  );
  resizableTable("library", $("list"));
  resizableTable("quality", $("quality-table"));
  resizableTable("history", $("history-table"));
  // Every table with headings is listed here by hand, so a table added to the
  // window has to be added to this list to get its grips.
  resizableTable("mixing", $("mixing-table"));
}

// --- tabs, refresh, toast ---------------------------------------------------

function switchTab(name) {
  const arriving = state.tab !== name;
  state.tab = name;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("is-active", tab.dataset.tab === name);
  }
  // Written as the complement: every screen is hidden except the one
  // arriving. Listed by name, this line would have to be edited each time a
  // tab is added, and a new screen left out of it would draw itself into a
  // section that stays `hidden`.
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view-${name}`;
  }
  renderLibraryChrome();
  // `refresh` asks whether folders are still where they were before every
  // draw. This asks as well because arriving at a tab does not redraw: the
  // shelf comes back exactly as it was left, so nothing else here would ask.
  // Redrawn only when the answer says a card is now wrong — a quiet arrival
  // costs the question, which is far cheaper than a redraw.
  if (arriving && name === "library") {
    checkFoldersStillThere().then((moved) => {
      if (moved) refresh();
    });
  }
  // Only asked while it is on screen: a download list nobody is looking at is
  // a question put to the slskd server every two seconds for nothing.
  watchTransfers(name === "transfers");
  // And ask for the sleeves this tab is now showing. History draws while it is
  // hidden — every refresh redraws it, on whichever tab is in front — so its
  // rows are handed to the observer with nothing to intersect, and arriving at
  // the tab is not something the observer hears about. Without this every row
  // would stay on its placeholder.
  requestVisibleCovers();
}

/** Ask whether the albums on screen still have their folders, and mark those
 * that do not.
 *
 * An album whose folder is gone keeps its row and its card wears `not where
 * it was`; it is never taken off the shelf here, because a card nobody can
 * see is indistinguishable from a row that was deleted.
 *
 * So the shelf is redrawn instead of trimmed: whether a folder answers is asked
 * of the disk wherever a card is drawn, so a refresh is all it takes for the
 * mark to appear.
 */
async function checkFoldersStillThere() {
  // `collecting` is not a reason to skip this. It is chronically true — every
  // refresh starts one while any download is unfinished — so including it
  // here would mean arriving at the Library often does not check at all. A
  // collect reads a folder that has just appeared; it never takes one away, so
  // the two cannot disagree.
  if (state.scanning || state.restoring || state.adopting || state.opening) return false;
  let response;
  try {
    response = await api().library_present();
  } catch {
    // Already named by the bridge proxy, and a row left on screen for one more
    // minute is not worth a second sentence about it.
    return false;
  }
  const missing = new Set(response?.missing || []);
  // And the ones it found. The sweep behind this call follows an album moved
  // in the file manager and corrects its row; the card is drawn from that row,
  // so it is wrong the moment the row is right. Reading only `missing` would
  // mean a sweep that fixed everything answers "nothing to do" and leaves
  // `not where it was` on screen over albums that are on the disk.
  const followed = response?.followed?.length || 0;
  if (!missing.size && !followed) return false;
  // A marked album is no longer a candidate for the next Scan: it cannot be
  // read where it is recorded, and a run that reaches it can only report it as
  // misplaced. The row stays, and so does every gesture offered on it.
  for (const unitId of missing) {
    state.marked.delete(unitId);
    state.identifying.delete(unitId);
  }
  if (missing.size) renderMarkCount();
  // Whether a card on screen is now wrong, which is the caller's to act on:
  // `refresh` is about to draw anyway and ignores this, and arriving at a tab
  // draws nothing on its own and needs it.
  return true;
}

/** Show the frame's Library feedback only while the Library is the screen.
 *
 * The Library's own buttons live inside the Library's own bar, so the tab
 * switch hides them by hiding the screen they are part of. What is left to
 * say here is the frame's scan feedback, which stays pinned with the header
 * and so cannot move in with them.
 */
function renderLibraryChrome() {
  const onLibrary = state.tab === "library";
  renderRootPath();
  // The frame's bar belongs to whatever run is happening, not to one tab.
  // Reading key and tempo can be started from three screens, and a bar shown
  // only for a Library scan would leave that run working behind a window that
  // looks idle.
  const running = (onLibrary && state.scanning) || state.measuringHarmonics;
  $("progress").hidden = !running;
  renderScanDone();
}

/** Ask for the sleeve of every row on screen that is still a placeholder.
 *
 * The observer stays, and is what covers scrolling. This covers becoming
 * visible at all, which it does not.
 */
function requestVisibleCovers() {
  for (const node of document.querySelectorAll("[data-unit-id]")) {
    if (!node.querySelector(".card-cover-placeholder")) continue;
    if (!node.getClientRects().length) continue;
    requestCover(Number(node.dataset.unitId));
  }
}

async function refresh() {
  // A downloaded album joins the library by itself once it has arrived, so the
  // question is asked wherever the window refreshes rather than only after
  // the download folder is scanned by hand.
  collectDownloads();
  // Whatever draws the chip has asked first. `not where it was` is computed
  // from the recorded folder every time a card is drawn, so the sweep that
  // repairs that folder runs before every draw and not only on arriving at the
  // Library.
  //
  // Before the read rather than after it, so this redraw is the corrected one:
  // asking afterwards would mean drawing the stale shelf and then paying for a
  // second draw to say what was already known. The sweep's own work is small
  // beside what this read costs, and it stands aside entirely while a run is
  // going.
  await checkFoldersStillThere();
  const snapshot = await api().state();
  // The survey outlives a tab switch and a reopened window, so its findings
  // are read back rather than kept only in whatever events this session saw.
  const bench = await api().bench_state();
  state.benchRoots = bench.roots || [];
  state.quality = bench.albums || [];
  // Off the dashboard, not off the bench: it is a fact about this machine, and
  // reading it from the bench payload is what left Mixing unable to know it.
  state.canMeasureAudio = snapshot.can_measure !== false;
  state.qualityRunning = state.qualityRunning || Boolean(bench.running);
  state.albums = snapshot.albums;
  state.history = snapshot.history;
  // What is still on its way. Read before `ensurePolling` below, which is what
  // keeps this window listening for the collect it never started.
  state.awaiting = snapshot.awaiting || 0;
  ensurePolling();
  await loadShared();
  state.settings = snapshot.settings;
  // The order the shelf was left in. Read here rather than at start-up alone,
  // because Settings can be saved from another screen and the shelf has to
  // agree with the picker that says what it is doing.
  state.sort = state.settings.library_sort || "recent";
  $("library-sort").value = state.sort;
  // The stored column widths. Read here rather than when the grips were built,
  // because the grips are built with the document and the settings arrive with
  // the application.
  applyStoredColumnWidths();
  // The first launch of a new installation, and only ever the first: `welcomed`
  // is written once and never unset, so the welcome is a door rather than a
  // greeting that keeps happening. Settings has the way back to it.
  if (state.settings.welcomed === false && !state.welcomeShown) {
    state.welcomeShown = true;
    openWelcome({ firstRun: true });
  }
  state.scanning = snapshot.scanning && state.scanning;
  state.keepTreesOpen = state.settings.keep_trees_open !== false;
  // A run that organized albums in different places has no "the folder".
  $("open-folder").hidden = snapshot.has_root === false;
  setTreeButtons();
  const wasRestoring = state.restoring;
  state.restoring = Boolean(snapshot.restoring);
  if (state.restoring && !wasRestoring) pollEvents();
  state.reach = snapshot.reach || state.reach;
  applyDensity();
  if (snapshot.root) {
    state.root = snapshot.root;
    // Shown only while choosing it is still an unfinished instruction. The path
    // itself is remembered — Quality measures it, and a rescan needs it — but a
    // window that reopens onto a folder path from an earlier session is
    // announcing a scan that session made.
    renderRootPath();
    // Whether Scan can be pressed is `renderMarkCount`'s to say, and it is said
    // below. A second writer of the same button lets the label and the gesture
    // come apart.
    $("bench-add").disabled = state.qualityRunning;
  }
  renderLibraryChrome();
  renderActivity();
  renderGrid();
  // After the grid, because the count is about the albums the grid is holding:
  // a mark whose album is no longer on the shelf is not something Scan can be
  // pressed for.
  renderMarkCount();
  renderQuality();
  renderHistory();
  renderScanDone();
}

// --- saying what happened, and what went wrong ------------------------------
// Two kinds of message and one rule between them: something that worked says so
// and goes away; something that failed stays longer, and never takes the place
// of another failure.
//
// A stack rather than a slot, because in a slot the second message overwrites
// the first, and a screen with two things wrong reports whichever happened to
// be last. Every notice carries its own ✕.

// A notice stays as long as it takes to read, and no notice stays for ever.
// One duration for every message would give `Saved.` and a sentence fifteen
// times its length the same time on screen.
//
// The floor is what a glance costs, whatever the words are. The rate is a
// reading speed and therefore a choice, not a measurement — 15 characters a
// second, about 180 words a minute, deliberately slower than reading something
// that was looked for, because a notice arrives beside whatever was actually
// being done.
//
// The ceiling matters because some messages carry a folder name, or two, and
// a folder name can run past a hundred characters. When checking these
// durations, render each message with the longest thing that can be inside it
// rather than with a short stand-in.
const TOAST_FLOOR_MS = 5000;
const TOAST_CEILING_MS = 12000;
const TOAST_MS_PER_CHARACTER = 67;

// What a failure earns instead of waiting for a click. A failure that stays
// until dismissed leaves a red band over the shelf until it is closed by hand,
// every time. Same rate per character, twice the floor and nearly twice the
// ceiling: the longest refusal this window writes earns the full twenty
// seconds, and it keeps its ✕.
//
// It is not a way of forgetting a failure that is still happening: a notice
// that arrives again restarts its own timer, so a band goes away twenty seconds
// after the thing stops, not twenty seconds after it started.
const STAYING_FLOOR_MS = 10000;
const STAYING_CEILING_MS = 20000;

/** How long this notice earns, by its length, between the floor and the ceiling. */
function toastMilliseconds(text, stays = false) {
  const floor = stays ? STAYING_FLOOR_MS : TOAST_FLOOR_MS;
  const ceiling = stays ? STAYING_CEILING_MS : TOAST_CEILING_MS;
  return Math.min(ceiling, floor + String(text ?? "").length * TOAST_MS_PER_CHARACTER);
}

// Long enough to be read without looking for it, short enough that it is gone
// before it is in the way of the shelf it is announcing.
const FLASH_SECONDS = 1800;
const FLASH_FADE_MS = 400;

/** Notices on screen now, by their text, so the same one is never stacked twice.
 *
 * A poll asks every two seconds. A failure it hits is the *same* failure each
 * time, and without this the screen fills with thirty copies a minute of one
 * sentence — which buries the other failures and is its own kind of silence.
 */
const notices = new Map();

/** Report a refusal from a call whose answer nobody was reading.
 *
 * `{ok: false}` is an answer rather than a failure, so the bridge does not
 * report it — but an answer thrown away is the same silence by another route.
 * These are the calls that open something: a link, a folder, the file manager.
 * They can be refused, and the refusal is shown.
 */
async function reportIfRefused(promise) {
  const result = await promise;
  if (result && result.ok === false && result.error) toastError(result.error);
  return result;
}

/** Say something that worked. It goes away on its own. */
function toast(message) {
  showNotice(message, false);
}

/** Say something that worked, and give it a failure's worth of reading time.
 *
 * For the answers that are instructions rather than announcements: a message
 * that names a folder to go and look in needs longer than an announcement
 * gets. Not `toastError`, which would put a red band on something that
 * worked — which is why the flag below decides the colour and the reading
 * time separately.
 */
function toastToBeRead(message) {
  showNotice(message, false, true);
}

/** Say something that went wrong, and leave it up for a failure's reading time.
 *
 * A refusal names every problem a plan has, including full paths, and its text
 * is selectable so a path can be copied out of it.
 */
function toastError(message) {
  showNotice(message, true);
}

/** Say why a gesture was refused, and let it go on an announcement's timer.
 *
 * An error stays longer because a failure nobody saw is a failure nobody
 * fixes. A refusal is not that: it answers something pressed a second ago, it
 * is being looked at, and it asks for nothing to be done. Same red, same
 * words, and it fades the way an answer should.
 */
function toastRefusal(message) {
  showNotice(message, true, false);
}

/** Announce that something finished, in the middle of the window, briefly.
 *
 * A progress bar disappearing is an absence, not an announcement, and the
 * green line that stays afterwards is read on the way to something else, not
 * at the moment the run ends. This is the moment.
 *
 * One at a time, and the newest wins: two of these overlapping would be two
 * announcements in the same place.
 */
let flashTimer = null;

function flash(message) {
  const box = $("flash");
  box.textContent = String(message ?? "").trim();
  if (!box.textContent) return;
  clearTimeout(flashTimer);
  // Arriving is instant and only leaving is gentle, which is the way round that
  // does not depend on a frame ever being painted. Fading *in* needs the class
  // to land in a later frame than the one the popover opens in, and
  // `requestAnimationFrame` does not run at all while the window is not being
  // drawn: the notice would open, the class would never arrive, and it would
  // sit at zero opacity for its whole life. A window occluded during a long
  // scan is exactly that case.
  box.classList.remove("is-leaving");
  if (!box.matches(":popover-open")) box.showPopover();
  flashTimer = setTimeout(() => {
    box.classList.add("is-leaving");
    // After the fade, not with it: hiding the popover first takes the element
    // out of the top layer and there is nothing left to fade.
    flashTimer = setTimeout(() => {
      if (box.matches(":popover-open")) box.hidePopover();
      box.classList.remove("is-leaving");
    }, FLASH_FADE_MS);
  }, FLASH_SECONDS);
}

// `stays` defaults to `isError`: a failure gets the longer reading time, an
// announcement does not. It is a separate parameter because a green notice can
// have reading in it too, and a red refusal can need none.
function showNotice(message, isError, stays = isError) {
  const text = String(message ?? "").trim() || STR.unknownFailure;
  const box = $("toast");
  const seen = notices.get(text);
  if (seen) {
    // The same thing has happened again. Counted rather than repeated, because
    // "3×" is information and three identical paragraphs are not.
    seen.count += 1;
    seen.tally.textContent = seen.count > 1 ? `${seen.count}×` : "";
    restartNoticeTimer(seen);
    return;
  }

  const notice = document.createElement("div");
  notice.className = `notice ${isError ? "is-error" : "is-good"}`;
  const tally = document.createElement("b");
  tally.className = "notice-tally";
  const words = document.createElement("span");
  words.className = "notice-message";
  words.textContent = text;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.textContent = "✕";
  close.title = STR.dismiss;
  close.addEventListener("click", () => dismissNotice(text));
  notice.append(tally, words, close);
  // Newest on top: the thing that just happened is the thing being looked for.
  box.prepend(notice);

  const entry = { node: notice, tally, text, count: 1, timer: null, isError, stays };
  notices.set(text, entry);
  restartNoticeTimer(entry);
  if (!box.matches(":popover-open")) box.showPopover();
}

function restartNoticeTimer(entry) {
  clearTimeout(entry.timer);
  // The message it was made with, which is also the key it is filed under —
  // read off the entry rather than back out of the DOM, so the length this is
  // measuring is the length that was written. The tally does not count: `3×`
  // is two characters and nothing that has to be read.
  entry.timer = setTimeout(
    () => dismissNotice(entry.text),
    toastMilliseconds(entry.text, entry.stays),
  );
}

function dismissNotice(text) {
  const entry = notices.get(text);
  if (!entry) return;
  clearTimeout(entry.timer);
  entry.node.remove();
  notices.delete(text);
  if (!notices.size) hideToast();
}

function hideToast() {
  for (const entry of notices.values()) clearTimeout(entry.timer);
  notices.clear();
  const box = $("toast");
  box.textContent = "";
  if (box.matches(":popover-open")) box.hidePopover();
}

applyStrings();
// --- folders dropped onto the window ----------------------------------------
// Where a dropped folder *is* can only be answered natively, so the path is
// read on the other side of the bridge and nothing here ever sees one. This
// half does the two things a page must: stop the webview from navigating to
// whatever was dropped, and say that the window will take it.
//
// The counter exists because dragenter and dragleave both fire when the
// pointer crosses onto a child element, so tracking a boolean makes the
// highlight flicker across every row it passes over.
let dragDepth = 0;

function showDropTarget(showing) {
  document.body.classList.toggle("is-dropping", showing);
}

document.addEventListener("dragenter", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
  dragDepth += 1;
  // Always the library, whichever screen is open: a dropped folder is an album
  // being added, and that is the one thing this gesture can mean.
  if (dragDepth === 1) {
    switchTab("library");
    showDropTarget(true);
  }
});

document.addEventListener("dragover", (event) => {
  // Without this the drop never happens at all — the default is to navigate.
  if (event.dataTransfer?.types?.includes("Files")) event.preventDefault();
});

document.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) showDropTarget(false);
});

document.addEventListener("drop", (event) => {
  if (!event.dataTransfer?.types?.includes("Files")) return;
  event.preventDefault();
  dragDepth = 0;
  showDropTarget(false);
  // The paths arrive natively, so there is nothing to send — only the waiting
  // to start. The answer is guaranteed, including when the drop held no
  // folder, which is what lets this be set without a timeout to undo it.
  state.adopting += 1;
  $("empty-library").textContent = STR.adopting;
  // Not `pollEvents()` directly: a drop can land while a scan is already being
  // listened to, and a second timer chain would double every beat.
  ensurePolling();
});

$("choose-folder").addEventListener("click", chooseFolder);
$("scan").addEventListener("click", startScan);
$("apply-automatic").addEventListener("click", applyAutomatic);
$("apply-result-close").addEventListener("click", () => {
  $("apply-result").hidden = true;
});
$("open-folder").addEventListener("click", () => reportIfRefused(api().open_root()));
$("revert-run").addEventListener("click", async () => {
  if (!state.lastRun) return;
  let response = await withSpinner($("revert-run"), () => api().revert_run(state.lastRun));
  // Asked once for the whole run, before any of it comes back. This answer
  // carries no `failures`, so it is read before the failure branch.
  if (response.confirm === "filed_away") {
    if (!(await confirmFiledAway(
      STR.revertFiledAwayRun(response.albums, response.folder, response.parent),
    ))) return;
    response = await withSpinner(
      $("revert-run"), () => api().revert_run(state.lastRun, true),
    );
  }
  if (!response.ok) {
    toastError(response.failures.join("; "));
    await refresh();
    return;
  }
  toast(STR.runReverted(response.reverted));
  // The strip stays, and the run keeps its name. Clearing everything the
  // instant a revert succeeds would leave no surface for the one question a
  // revert raises — "put it back?" — and no way to answer it but to organize
  // all of it again by hand.
  $("apply-result-title").textContent = STR.runReverted(response.reverted);
  $("apply-result-failures").replaceChildren();
  renderRunButtons(true);
  await refresh();
});

$("redo-run").addEventListener("click", async () => {
  if (!state.lastRun) return;
  const response = await withSpinner($("redo-run"), () => api().redo_run(state.lastRun));
  if (!response.ok) {
    // A refusal here is the ordinary outcome when the disk has moved on, and it
    // names every path that went, so it is shown rather than summarised.
    toastError((response.failures || []).join("; "));
    await refresh();
    return;
  }
  toast(STR.runRedone(response.redone));
  $("apply-result-title").textContent = STR.runRedone(response.redone);
  $("apply-result-failures").replaceChildren();
  renderRunButtons(false);
  await refresh();
});

/** Offer whichever of the two directions this run can still go in.
 *
 * A run is either applied or reverted, so exactly one of these is ever the
 * thing to do.
 */
function renderRunButtons(reverted) {
  const known = Boolean(state.lastRun);
  state.lastRunReverted = reverted;
  $("revert-run").hidden = !known || reverted;
  $("redo-run").hidden = !known || !reverted;
}
$("export-report").addEventListener("click", async () => {
  const response = await withSpinner($("export-report"), () => api().export_report());
  if (response.ok) toast(STR.reportWritten(response.rows));
  else toastError(response.error);
});
$("album-more").addEventListener("click", (event) => openAcquireMenu(event, albumMoreItems()));
$("album-scan-now").addEventListener("click", scanThisAlbum);
$("album-plan-again").addEventListener("click", planAnyway);
$("album-cancel").addEventListener("click", cancelLast);
// Its ✕ means *not yet*: it closes the question and leaves the album open,
// which is what every other confirmation in this window means by ✕.
$("album-gone-close").addEventListener("click", () => $("album-gone-dialog").close());
$("same-audio-ok").addEventListener("click", () => $("same-audio-dialog").close());
$("same-audio-close").addEventListener("click", () => $("same-audio-dialog").close());
$("album-gone-relocate").addEventListener("click", async () => {
  const unitId = Number($("album-gone-dialog").dataset.unitId);
  $("album-gone-dialog").close();
  await relocateFromMenu(unitId);
});
$("album-gone-forget").addEventListener("click", async () => {
  const unitId = Number($("album-gone-dialog").dataset.unitId);
  $("album-gone-dialog").close();
  await forgetAlbum(unitId);
});
$("scan-again-no").addEventListener("click", () => $("scan-again-dialog").close());
$("scan-again-yes").addEventListener("click", () => {
  $("scan-again-dialog").close();
  scanAgainAsked = true;
  scanThisAlbum();
});
$("leave-album-no").addEventListener("click", () => $("leave-album-dialog").close());
$("leave-album-keep").addEventListener("click", () => {
  $("leave-album-dialog").close();
  $("album-dialog").close();
});
$("leave-album-undo").addEventListener("click", async () => {
  $("leave-album-dialog").close();
  if (await cancelLast()) $("album-dialog").close();
});
// The same gesture the ⋯ offers, from the footer of an album in review, and
// told which button to turn.
$("album-arrange-tags").addEventListener("click", (event) =>
  arrangeThisAlbum(event.currentTarget),
);
// The witness's two gestures, both of which only read.
$("album-witness-read").addEventListener("click", (event) =>
  readWitnessTracklist(event.currentTarget),
);
$("album-witness-search").addEventListener("click", (event) =>
  searchWithWitnessWords(event.currentTarget),
);
$("plan-yours-drop-all").addEventListener("click", dropEveryCorrectedTitle);
$("album-previous").addEventListener("click", () => stepThroughShelf(-1));
$("album-next").addEventListener("click", () => stepThroughShelf(1));
$("album-choose-cover").addEventListener("click", chooseCover);
$("cover-choice-write").addEventListener("click", writeChosenCover);
$("cover-choice-drop").addEventListener("click", discardChosenCover);
$("album-scan-here").addEventListener("click", scanThisAlbum);
$("album-confirm-pairings").addEventListener("click", confirmPairings);
$("album-open-folder").addEventListener("click", () => openAlbumFolder(state.openAlbum.unit_id));
$("album-to-bench").addEventListener("click", sendAlbumToBench);
$("album-approve").addEventListener("click", approveAlbum);
$("album-reject").addEventListener("click", rejectAlbum);
$("find-form").addEventListener("submit", findAlbum);
$("find-mode-link").addEventListener("click", () => setFindMode("link"));
$("find-mode-names").addEventListener("click", () => setFindMode("names"));
$("open-settings").addEventListener("click", openSettings);
$("settings-form").addEventListener("submit", saveSettings);
$("settings-icon").addEventListener("click", () => addToApplications($("settings-icon-said")));
$("composer-reset").addEventListener("click", resetComposer);
for (const which of ["folder", "track"]) {
  $(`composer-${which}-add`).addEventListener("change", (event) => {
    const token = event.target.value;
    if (!token) return;
    const piece = composer.offered[which].find((one) => one.token === token);
    if (piece) composer.chosen[which].push(piece);
    drawComposer();
  });
}
$("setting-clear-spectrograms").addEventListener("click", clearSpectrograms);
$("setting-clear-metadata").addEventListener("click", clearMetadataCache);
$("setting-clear-artwork").addEventListener("click", clearArtworkCache);
$("setting-connections").addEventListener("click", () => {
  $("settings-dialog").close();
  openWelcome();
});
$("connect-discogs-save").addEventListener("click", () =>
  connectService("discogs", "connect-discogs", "connect-discogs-save"),
);
$("connect-acoustid-save").addEventListener("click", () =>
  connectService("acoustid", "connect-acoustid", "connect-acoustid-save"),
);
$("connect-slskd-save").addEventListener("click", () =>
  connectService("slskd", "connect-slskd", "connect-slskd-save"),
);
$("connect-spotify-id-save").addEventListener("click", () =>
  connectService("spotify_id", "connect-spotify-id", "connect-spotify-id-save"),
);
$("connect-spotify-secret-save").addEventListener("click", () =>
  connectService("spotify_secret", "connect-spotify-secret", "connect-spotify-secret-save"),
);
$("welcome-start").addEventListener("click", finishWelcome);
$("welcome-close").addEventListener("click", finishWelcome);
// The two links go through the opener rather than navigating this window,
// which has no way back — it is the application, not a browser tab.
$("connect-discogs-link").addEventListener("click", (event) => {
  event.preventDefault();
  api().open_url("https://www.discogs.com/settings/developers");
});
$("connect-acoustid-link").addEventListener("click", (event) => {
  event.preventDefault();
  api().open_url("https://acoustid.org/new-application");
});
$("connect-spotify-link").addEventListener("click", (event) => {
  event.preventDefault();
  api().open_url("https://developer.spotify.com/dashboard");
});
$("view-grid").addEventListener("click", () => {
  state.view = "grid";
  renderGrid();
});
$("view-list").addEventListener("click", () => {
  state.view = "list";
  renderGrid();
});
// Redrawn on every keystroke, and it can be: the filter is a substring test over
// albums already in memory. No request is made and no folder is read — this
// only decides which of the cards already built are shown.
$("library-search").addEventListener("input", (event) => {
  state.search = event.target.value;
  renderSearchBox();
  renderGrid();
});
// Escape clears rather than closing anything: inside a search box it is the
// gesture people already have, and there is no dialog here for it to mean.
$("library-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.search) {
    event.stopPropagation();
    clearLibrarySearch();
  }
});
$("library-search-clear").addEventListener("click", clearLibrarySearch);
// Wiring belongs here, where it runs once. Inside `switchTab` every tab click
// would add another listener to the same control, and the fourth visit to a
// tab would fire four searches from one press of Search.
$("bench-add").addEventListener("click", addBenchFolder);
$("bench-analyze").addEventListener("click", startBenchAnalysis);
$("bench-identify").addEventListener("click", startBenchIdentification);
// Two presses, each naming the view it lands on, exactly as the shelf's pair
// does. A single toggling handler and a pair of buttons cannot agree for long:
// the icon that is hidden is the one whose press would be a no-op, and a
// toggle asked from either of them lands on whichever state it was not in.
$("bench-view-cards").addEventListener("click", () => {
  state.benchView = "cards";
  renderQuality();
});
$("bench-view-list").addEventListener("click", () => {
  state.benchView = "list";
  renderQuality();
});
$("library-sort").addEventListener("change", async (event) => {
  state.sort = event.target.value;
  // Kept where every other preference is kept, so the shelf opens next time in
  // the order it was left in.
  await api().set_settings({ ...state.settings, library_sort: state.sort });
  state.settings.library_sort = state.sort;
  renderGrid();
});
$("acquire-go").addEventListener("click", startAcquireSearch);
$("acquire-organize").addEventListener("click", organizeDownloads);
$("transfers-organize").addEventListener("click", organizeDownloads);
$("transfers-clear").addEventListener("click", clearTransfers);
for (const id of ["transfers-keep-open", "acquire-keep-open"]) {
  $(id).addEventListener("click", () => setKeepTreesOpen(!state.keepTreesOpen));
}
/** The shelf's ✕, reached through the bar's ⋯. */
async function clearOrganized() {
  const result = await api().clear_organized();
  if (!result.ok) {
    toastError(result.error);
    return;
  }
  toast(STR.organizedCleared(result.cleared));
  await refresh();
}

$("library-more").addEventListener("click", (event) => {
  // The bar's rare gestures, built when asked so each entry reflects the shelf
  // as it stands. The same popover every other menu here uses — one menu in
  // this window rather than a second kind that behaves almost like the first.
  const items = [];
  // Arranging by the files' own tags over the marks, first because it is about
  // the albums just chosen — offered exactly while it has something to do and
  // no scan is holding the folder.
  const count = markedOnScreen().length;
  if (count && !state.scanning) {
    items.push({ label: STR.arrangeMarked(count), run: arrangeMarked });
    // Key and tempo over the same marks, so many albums are one gesture
    // instead of one dialog each.
    items.push({
      label: STR.measureMarked(count),
      run: () => measureHarmonics(markedOnScreen()),
    });
  }
  items.push({
    label: STR.menuOpenDownloads,
    run: () => reportIfRefused(api().open_downloads()),
  });
  if ((state.albums || []).some((album) => album.organized || album.applied)) {
    items.push({ label: STR.clearOrganized, run: clearOrganized });
  }
  openAcquireMenu(event, items);
});
// The heading of every table gets its draggable edges once, here, where the
// document is known to be built.
makeColumnsResizable();
followChromeHeight();
$("acquire-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") startAcquireSearch();
});
for (const head of document.querySelectorAll("[data-sort]")) {
  head.addEventListener("click", () => sortAcquireBy(head.dataset.sort));
  head.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      sortAcquireBy(head.dataset.sort);
    }
  });
}
// The bench's filter, opening the same menu the Library's does — the same
// popover, the same shape of line, the same ✓.
$("quality-status").addEventListener("click", (event) => {
  openAcquireMenu(
    event,
    QUALITY_GROUPS.map(([group]) => {
      const count = (state.qualityCounts || {})[group] ?? 0;
      return {
        label: `${group === state.qualityFilter ? "✓ " : "   "}${qualityName(group)} (${count})`,
        run: () => {
          state.qualityFilter = group;
          renderQuality();
        },
      };
    }),
  );
});
// The History's two questions, in the shape the other screens already use.
$("history-status").addEventListener("click", (event) => {
  openAcquireMenu(
    event,
    HISTORY_GROUPS.map(([group]) => {
      const count = (state.historyCounts || {})[group] ?? 0;
      return {
        label: `${group === state.historyFilter ? "✓ " : "   "}${historyGroupName(group)} (${count})`,
        run: () => {
          state.historyFilter = group;
          renderHistory();
        },
      };
    }),
  );
});
$("history-search").addEventListener("input", (event) => {
  state.historySearch = event.target.value;
  renderHistory();
});
$("history-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    clearHistorySearch();
  }
});
$("history-search-clear").addEventListener("click", clearHistorySearch);
$("status-menu").addEventListener("click", (event) => {
  // The same popover the right-click menus use, so there is one menu in this
  // window rather than a second kind that behaves almost like the first.
  // Two questions, two lists, and pressing the answer again withdraws it. One
  // flat run of entries would read as one filter with nine answers.
  // `all` is an entry as well as the toggle: withdrawing by pressing again is
  // invisible, and a gesture nobody can see is not an answer to *show
  // everything*. The entry is ticked exactly when no filter stands, so the
  // list always says where it is.
  openAcquireMenu(event, [
    { heading: STR.filterStatus },
    ...STATUS_GROUPS.map(([group]) => {
      const count = (state.statusCounts || {})[group] ?? 0;
      return {
        label: `${group === state.filter ? "✓ " : "   "}${statusName(group)} (${count})`,
        run: () => {
          state.filter = state.filter === group ? "all" : group;
          renderGrid();
        },
      };
    }),
    // In the menu the shelf already has rather than a fourth control in the
    // bar: sort, status, search and the view toggle already fill it, and a
    // rating is another way of asking *which of these*, which is what this
    // menu is for.
    { heading: STR.filterRating },
    ...RATING_FILTERS.map((value) => ({
      label: `${String(state.rating) === String(value) ? "✓ " : "   "}${ratingFilterName(value)}`,
      run: () => {
        state.rating = String(state.rating) === String(value) ? "all" : value;
        renderGrid();
      },
    })),
  ]);
});
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    switchTab(tab.dataset.tab);
    // The server may have been switched off since the window opened, and the
    // area has to say so rather than fail silently on the first search.
    if (tab.dataset.tab === "acquire") acquireRefreshState();
    if (tab.dataset.tab === "mixing") refreshMixing();
  });
}
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => {
    const dialog = button.closest("dialog");
    // The album is the one dialog that may have something waiting on it, and
    // its ✕ is a way out like any other.
    if (dialog.id === "album-dialog") closeAlbumOrAsk();
    else dialog.close();
  });
}
// And the key that closes a modal without touching anything. This webview
// fires neither `close` nor `cancel` (which is also why the backdrop is a
// listener), so Escape is caught on the way down and answered by the same rule
// the other two routes use.
$("album-dialog").addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.openAlbum?.can_cancel) return;
  event.preventDefault();
  closeAlbumOrAsk();
});

/** Say whether the details are about to open or about to close. */
function sayWhichWayTheFoldGoes() {
  $("album-details-summary").textContent = $("album-details").open
    ? STR.hideDetails
    : STR.viewDetails;
}
/** Let a click outside the panel be a way out, the way every other window is.
 *
 * This webview fires neither `close` nor `cancel`, and does not support
 * `closedby`, which is why this is a listener and not an attribute, and why
 * the exit cannot be hung on the dialog's own event.
 *
 * Two conditions, because either alone is wrong here. `target === dialog` says
 * the click landed on the element itself rather than on anything inside it —
 * which excludes the `<select>` in Settings, whose open list reports
 * coordinates outside the panel while still targeting the control. The
 * rectangle says the point really is outside the panel, which `padding: 0`
 * makes exact.
 *
 * ``run`` is how *this* dialog closes: the welcome has to record that it has
 * been through, so dismissing it is not `close()`.
 */
function dismissOnBackdrop(dialog, run) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    const outside =
      event.clientX < box.left ||
      event.clientX > box.right ||
      event.clientY < box.top ||
      event.clientY > box.bottom;
    if (outside) run();
  });
}
// `big-folder-dialog` and the other questions are left out: their ✕ is already
// their `no`, and a stray click must not answer them. The album dialog closes
// the same way as every other screen, through the rule that asks first when
// something is waiting on it. A click on a modal's backdrop reports the
// *dialog* as its target, so the geometry has to be asked as well.
dismissOnBackdrop($("album-dialog"), closeAlbumOrAsk);
// `View details` / `Hide details`, so the control says which way it goes. On
// `toggle`, which fires however the fold was opened — including the render
// that opens it — rather than on the click, which does not.
$("album-details").addEventListener("toggle", sayWhichWayTheFoldGoes);
dismissOnBackdrop($("welcome-dialog"), finishWelcome);
dismissOnBackdrop($("settings-dialog"), () => $("settings-dialog").close());

// `refresh` is async, so an exception anywhere inside it rejects a promise
// nobody is holding: the screens it had not reached yet simply stay as the
// markup left them — which is empty, and which reads exactly like "there is
// nothing here". A Library and a History opening empty against a database
// that has rows in it is that shape.
//
// Any failure therefore lands in the toast, which sits in the top layer.
window.addEventListener("unhandledrejection", (event) => {
  if (event.reason?.__reported) return;
  toastError(STR.windowFailed(String(event.reason?.stack || event.reason)));
});
window.addEventListener("error", (event) => {
  if (event.error?.__reported) return;
  toastError(STR.windowFailed(String(event.error?.stack || event.message)));
});

async function boot() {
  try {
    await refresh();
  } catch (error) {
    // Caught as well as reported, because a boot that half-drew is worth
    // saying twice: this is the one refresh whose failure leaves every screen
    // blank rather than stale.
    toastError(STR.windowFailed(String(error?.stack || error)));
  }
}

whenBridgeReady(boot);


/* --- the mixing screen --------------------------------------------------- */

// Only what has been measured: the wheel shows the tracks that were asked
// about, never the shelf waiting to be asked. A screen listing what it has not
// measured would be offering mixes it cannot vouch for.
state.mixing = { tracks: [], chosenKey: null, chosenTrack: null, search: "", sort: "camelot" };

// The wheel's twenty-four addresses, outer ring first. Written as one list in
// wheel order so the drawing and the sorting read from the same sentence — two
// orders for one wheel is how the segments and the list drift apart.
const CAMELOT_ORDER = [];
for (let number = 1; number <= 12; number += 1) CAMELOT_ORDER.push(`${number}B`);
for (let number = 1; number <= 12; number += 1) CAMELOT_ORDER.push(`${number}A`);

/** The codes that mix with this one: one step either way, and the same number
 * on the other ring. Derived, never listed, so 12→1 is not a special case
 * somebody has to remember — the same sentence the Python holds. */
function camelotNeighbours(code) {
  const number = Number(code.slice(0, -1));
  const ring = code.slice(-1);
  const before = number === 1 ? 12 : number - 1;
  const after = number === 12 ? 1 : number + 1;
  return [`${before}${ring}`, `${after}${ring}`, `${number}${ring === "A" ? "B" : "A"}`];
}

/** Whether two tempos line up: the pitch fader's reach, and half or double.
 *
 * The same rule as `harmonic/mixing.py`, because a screen that offered a pair
 * the engine refuses would be a second opinion nobody asked for. */
function tempoMeets(source, target) {
  if (!source || !target) return null;
  const ways = [["same speed", target], ["double time", target / 2], ["half time", target * 2]];
  for (const [name, scaled] of ways) {
    if (Math.abs(scaled - source) / source <= 0.06) return name;
  }
  return null;
}

function mixingRows() {
  const words = state.mixing.search.trim().toLowerCase();
  let rows = state.mixing.tracks.filter((track) => {
    if (state.mixing.chosenKey && track.camelot !== state.mixing.chosenKey) return false;
    if (!words) return true;
    return [track.track, track.artist, track.album]
      .filter(Boolean)
      .some((field) => field.toLowerCase().includes(words));
  });
  const order = state.mixing.sort;
  rows = rows.slice().sort((a, b) => {
    if (order === "bpm") return (a.bpm || 0) - (b.bpm || 0);
    if (order === "artist") return (a.artist || "").localeCompare(b.artist || "");
    const left = CAMELOT_ORDER.indexOf(a.camelot);
    const right = CAMELOT_ORDER.indexOf(b.camelot);
    return left === right ? (a.bpm || 0) - (b.bpm || 0) : left - right;
  });
  return rows;
}

/** Where the three words divide — on the margin, which is what predicts.
 *
 * The word is not drawn from the correlation. That number is a Pearson
 * coefficient against a key profile, and real music holds notes outside its
 * own key, so it says how much this recording looks like music in that key —
 * not whether the reading beat its rivals. The margin, the distance to the
 * runner-up of the twenty-four, says exactly that.
 *
 * Compared with a reference key detector over a sample of real tracks,
 * agreement rises with the margin band by band, while the correlation's own
 * bands do not come out in order.
 *
 * The cuts are round numbers, not searched for.
 */
const KEY_WEAK_BELOW = 0.05;
const KEY_STRONG_FROM = 0.1;

/** The four cases a reading can be in, and how often each one holds.
 *
 * Three words on the screen, because the bench has three and the two screens
 * agree — but four cases behind them, because a tie between two keys that mix
 * with each other is not a doubtful reading for mixing. Either answer works
 * at the decks, so the margin is read together with the runner-up stored
 * beside it.
 *
 * `howOften` is per case and never per word, so a row is told what holds for
 * its own situation rather than for two situations taken together. It is a
 * word and not a figure: how often a reading lands on the reference key
 * detector's answer, or on a key that mixes with it, depends on the music
 * measured, so only the order of the cases is stated. A mixable tie is said
 * to be *at least as good as* the middle band, never better.
 *
 * `badge` and not the case name carries the class, because a class this
 * stylesheet has no rule for draws with the browser's defaults and looks
 * nearly right.
 */
const KEY_CASES = {
  strong: { badge: "strong", word: () => STR.confStrong, howOften: () => STR.mixKeyUsually },
  fair: { badge: "fair", word: () => STR.confFair, howOften: () => STR.mixKeyOften },
  mixableTie: { badge: "fair", word: () => STR.confFair, howOften: () => STR.mixKeyTieOften },
  weak: { badge: "weak", word: () => STR.confWeak, howOften: () => STR.mixKeyLessOften },
};

/** Whether the key this reading beat is one it mixes with. */
function runnerUpMixesWithIt(track) {
  if (!track.camelot || !track.runner_up_camelot) return false;
  return (
    track.runner_up_camelot === track.camelot ||
    camelotNeighbours(track.camelot).includes(track.runner_up_camelot)
  );
}

/** Whether this reading was close enough that the key it beat is worth naming. */
function keyWasACloseCall(track) {
  return (
    track.key_margin !== null &&
    track.key_margin !== undefined &&
    track.key_margin < KEY_WEAK_BELOW
  );
}

function keySureness(track) {
  const margin = track.key_margin;
  if (margin === null || margin === undefined) return null;
  if (margin >= KEY_STRONG_FROM) return "strong";
  if (margin >= KEY_WEAK_BELOW) return "fair";
  return runnerUpMixesWithIt(track) ? "mixableTie" : "weak";
}

/** The cell that says how decided a key reading was — three states, not two.
 *
 * A word, the way the Quality bench has always said this — outlined, not
 * filled, because a filled badge in this window is a verdict and this is not
 * one. The margin is in the tooltip, where it can be read beside the sentence
 * that says what it actually is.
 *
 * Its own function so it can be run rather than read (`test_key_sureness_cell.py`):
 * every state here is a branch of valid JavaScript whichever one is wrong, and
 * the one that matters most draws nothing loud.
 */
function keySurenessCell(track) {
  const cell = document.createElement("td");
  const found = keySureness(track);
  if (!track.camelot) {
    cell.textContent = STR.mixNoKey;
    cell.className = "muted";
    return cell;
  }
  if (found === null) {
    // A key measured before this application kept the margin was measured
    // with an earlier key profile. Giving it one of these three words would
    // calibrate a reading on a scale it was never taken with — so this says
    // nothing, and says why.
    cell.textContent = STR.mixKeyStaleMark;
    cell.className = "muted";
    cell.title = STR.mixKeyStale;
    return cell;
  }
  const which = KEY_CASES[found];
  const badge = document.createElement("span");
  badge.className = `badge badge-conf badge-conf-${which.badge}`;
  badge.textContent = which.word();
  badge.title = STR.mixKeySure(track.key_margin.toFixed(2), which.howOften());
  cell.append(badge);
  // The key it beat, wherever it barely beat it — on the word's own terms and
  // not only on the worst one. A close call that reads `fair` reads that way
  // *because* of the second key, so the second key is the half of the answer
  // that has to be on the screen.
  if (keyWasACloseCall(track) && track.runner_up_camelot) {
    const twin = document.createElement("span");
    twin.className = "key-twin";
    // Its own string: `mixOr` rounds a number, and a Camelot code put through
    // it draws `or NaN`.
    twin.textContent = ` ${STR.mixKeyOr(track.runner_up_camelot)}`;
    // Two sentences, because the two ties are two different facts: a
    // runner-up that mixes with the winner, and one that does not.
    twin.title = runnerUpMixesWithIt(track)
      ? STR.mixKeyTwin(track.runner_up_camelot)
      : STR.mixKeyTwinApart(track.runner_up_camelot);
    cell.appendChild(twin);
  }
  return cell;
}

function drawCamelotWheel() {
  const svg = $("camelot-wheel");
  svg.replaceChildren();
  const counts = new Map();
  for (const track of state.mixing.tracks) {
    if (track.camelot) counts.set(track.camelot, (counts.get(track.camelot) || 0) + 1);
  }
  // The wheel is the legend for what the list is showing, so it answers to
  // both gestures: pressing a segment, and pressing a track. Lighting only on
  // its own click would leave the wheel inert while the list is being read.
  const chosenTrack = state.mixing.chosenTrack
    ? state.mixing.tracks.find((track) => track.audio_key === state.mixing.chosenTrack)
    : null;
  const lit = state.mixing.chosenKey || chosenTrack?.camelot || null;
  const partners = lit ? new Set(camelotNeighbours(lit)) : new Set();
  const centre = 110;
  const rings = [
    { codes: CAMELOT_ORDER.slice(0, 12), inner: 72, outer: 104 },
    { codes: CAMELOT_ORDER.slice(12), inner: 40, outer: 72 },
  ];
  for (const ring of rings) {
    ring.codes.forEach((code, index) => {
      const from = (index * 30 - 105) * (Math.PI / 180);
      const to = ((index + 1) * 30 - 105) * (Math.PI / 180);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const point = (radius, angle) =>
        `${centre + radius * Math.cos(angle)} ${centre + radius * Math.sin(angle)}`;
      path.setAttribute(
        "d",
        `M ${point(ring.inner, from)} L ${point(ring.outer, from)} ` +
          `A ${ring.outer} ${ring.outer} 0 0 1 ${point(ring.outer, to)} ` +
          `L ${point(ring.inner, to)} A ${ring.inner} ${ring.inner} 0 0 0 ${point(ring.inner, from)} Z`,
      );
      const held = counts.get(code) || 0;
      const chosen = lit === code;
      path.setAttribute(
        "class",
        `wheel-slice${held ? " has-tracks" : ""}${chosen ? " is-chosen" : ""}` +
          `${!chosen && partners.has(code) && held ? " is-partner" : ""}`,
      );
      if (held) {
        path.addEventListener("click", () => {
          state.mixing.chosenKey = state.mixing.chosenKey === code ? null : code;
          state.mixing.chosenTrack = null;
          renderMixing();
        });
      }
      svg.appendChild(path);

      const middle = (from + to) / 2;
      const radius = (ring.inner + ring.outer) / 2;
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String(centre + radius * Math.cos(middle)));
      label.setAttribute("y", String(centre + radius * Math.sin(middle)));
      label.setAttribute("class", `wheel-label${held ? " on-tracks" : ""}${chosen ? " on-chosen" : ""}`);
      label.textContent = code;
      svg.appendChild(label);
    });
  }
}

/** The row that opens under the pressed track, saying what mixes with it.
 *
 * A row inside the table rather than a section after it: the list answers
 * about that track, and on a long table the foot of the page is a scroll away
 * from the question it answers.
 *
 * Returns the `<tr>` to insert, or `null` when this track has no key to reason
 * from — a track measured with no readable key is not a track with no partners.
 */
function mixingPartnersRow(chosen) {
  if (!chosen || !chosen.camelot) return null;
  const row = document.createElement("tr");
  row.className = "mixing-partner-row";
  const cell = document.createElement("td");
  // The cell stays a cell: the flex lives on the box inside it, the same
  // pattern as `.plan-cell`, because a `display` on a `<td>` puts that
  // column's borders above every other column's.
  cell.colSpan = 5;
  const holder = document.createElement("div");
  holder.className = "mixing-partner-holder";
  const heading = document.createElement("p");
  heading.className = "mixing-partner-heading";
  heading.textContent = STR.mixPartners(chosen.track);
  const list = document.createElement("ul");
  list.className = "mixing-partner-list";

  const allowed = new Set([chosen.camelot, ...camelotNeighbours(chosen.camelot)]);
  const found = [];
  for (const track of state.mixing.tracks) {
    if (track.audio_key === chosen.audio_key || !allowed.has(track.camelot)) continue;
    // Key *and* tempo together: two compatible keys at 92 and 140 do not mix,
    // and offering the pair because the wheel allows it is advice this screen
    // has not checked.
    let why;
    if (!chosen.bpm || !track.bpm) why = STR.mixTempoUnknown;
    else {
      const meeting = tempoMeets(chosen.bpm, track.bpm);
      if (!meeting) continue;
      why = meeting;
    }
    found.push({ track, why: STR.mixWhy(relationBetween(chosen.camelot, track.camelot), why) });
  }

  if (!found.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = STR.mixNoPartners(chosen.track);
    list.appendChild(empty);
  } else {
    list.append(
      ...found.map(({ track, why }) => {
        const line = document.createElement("li");
        line.className = "mixing-partner";
        const chip = document.createElement("span");
        chip.className = "camelot-chip";
        chip.textContent = track.camelot;
        const name = document.createElement("span");
        name.className = "mixing-partner-name";
        name.textContent = track.artist ? `${track.artist} — ${track.track}` : track.track;
        const reason = document.createElement("span");
        reason.className = "mixing-partner-why";
        reason.textContent = track.bpm ? `${why} · ${Math.round(track.bpm)}` : why;
        line.append(chip, name, reason);
        return line;
      }),
    );
  }
  holder.append(heading, list);
  cell.appendChild(holder);
  row.appendChild(cell);
  return row;
}

/** How two Camelot codes are related, in the words the wheel uses.
 *
 * Derived from the codes and not listed as pairs, the same sentence
 * `harmonic/mixing.py` holds — so 12→1 is not a case anybody has to remember.
 */
function relationBetween(source, target) {
  if (source === target) return "same key";
  if (target.slice(-1) !== source.slice(-1)) {
    return source.endsWith("A") ? "relative major" : "relative minor";
  }
  const number = Number(source.slice(0, -1));
  const up = number === 12 ? 1 : number + 1;
  return Number(target.slice(0, -1)) === up ? "one step up" : "one step down";
}

function renderMixing() {
  const has = state.mixing.tracks.length > 0;
  $("mixing-questions").hidden = !has;
  $("mixing-board").hidden = !has;
  $("empty-mixing").hidden = has || !state.canMeasureAudio;
  // A wheel still filling and a wheel with nothing on it are different
  // things, the same rule the Library follows. This list is built from the
  // albums the restore has reached, so it says so until the restore is done —
  // and the empty sentence does not advise measuring something while the
  // answer is on its way.
  $("mixing-restoring").hidden = !state.restoring;
  if (!has) {
    $("empty-mixing").textContent = state.restoring
      ? STR.restoringLibrary
      : STR.emptyMixing;
  }
  // What this screen could not say. Its own emptiness reads as "measure
  // something", which is advice a machine without ffmpeg cannot follow — and the
  // way in is withdrawn rather than left to fail, the way the bench's is. Said
  // even with tracks already on the wheel: they were measured before the binary
  // went away, and the gesture is still gone.
  $("mixing-no-ffmpeg").hidden = state.canMeasureAudio;
  $("mixing-measure").hidden = !state.canMeasureAudio;
  // Before the early return, because an empty wheel is precisely the state in
  // which this button has nothing to offer and has to say so.
  renderMixingMore();
  if (!has) return;

  drawCamelotWheel();
  const chosen = state.mixing.chosenKey;
  const held = chosen
    ? state.mixing.tracks.filter((track) => track.camelot === chosen).length
    : 0;
  const example = chosen
    ? state.mixing.tracks.find((track) => track.camelot === chosen)
    : null;
  $("mixing-selected").textContent = chosen
    ? STR.mixChosen(chosen, example?.key_name || "", held)
    : STR.mixNoKeyChosen;

  const body = $("mixing-body");
  body.replaceChildren(
    ...mixingRows().map((track) => {
      const row = document.createElement("tr");
      row.dataset.audioKey = track.audio_key;
      if (track.audio_key === state.mixing.chosenTrack) row.className = "is-chosen";
      row.addEventListener("click", () => {
        state.mixing.chosenTrack =
          state.mixing.chosenTrack === track.audio_key ? null : track.audio_key;
        renderMixing();
      });

      const key = document.createElement("td");
      const chip = document.createElement("span");
      chip.className = `camelot-chip${track.camelot === chosen ? " is-chosen" : ""}`;
      chip.textContent = track.camelot || STR.mixNoKey;
      key.appendChild(chip);

      const name = document.createElement("td");
      name.textContent = track.track;
      const artist = document.createElement("td");
      artist.textContent = track.artist || track.album || "";

      const tempo = document.createElement("td");
      if (track.bpm) {
        tempo.textContent = String(Math.round(track.bpm));
        // Its own confidence, on its own cell: the column beside this one is
        // about the key only.
        if (track.bpm_confidence !== null && track.bpm_confidence !== undefined) {
          tempo.title = STR.mixTempoSure(Math.round(track.bpm_confidence * 100));
        }
        // The other octave, when the audio did not settle between them — said
        // beside the number it chose, never instead of it.
        if (track.bpm_alternative) {
          const twin = document.createElement("span");
          twin.className = "tempo-twin";
          twin.textContent = ` ${STR.mixOr(track.bpm_alternative)}`;
          twin.title = STR.mixTempoTwin(track.bpm_alternative);
          tempo.appendChild(twin);
        }
      } else tempo.textContent = STR.mixNoTempo;

      row.append(key, name, artist, tempo, keySurenessCell(track));
      return row;
    }),
  );
  // Opened under the track it is about, so the answer and the question are
  // read together.
  const open = state.mixing.chosenTrack
    ? state.mixing.tracks.find((track) => track.audio_key === state.mixing.chosenTrack)
    : null;
  if (open) {
    const chosenRow = [...body.children].find(
      (row) => row.dataset.audioKey === open.audio_key,
    );
    const partners = mixingPartnersRow(open);
    if (chosenRow && partners) chosenRow.after(partners);
  }
}

async function refreshMixing() {
  const response = await api().harmonics();
  if (!response || !response.ok) return;
  state.mixing.tracks = response.tracks || [];
  renderMixing();
}

/** Read the key and tempo of the chosen albums.
 *
 * Reached from three places and never from a sweep: an album's own dialog,
 * the albums marked on the shelf, and the picker on the Mixing screen. */
async function measureHarmonics(unitIds) {
  const response = await api().measure_harmonics(unitIds);
  if (!response.ok) {
    toastError(response.error);
    return;
  }
  toast(STR.measuringHarmonics);
  // Set here rather than waiting for the first event, because the loop that
  // would deliver that event only keeps going while something is running.
  state.measuringHarmonics = true;
  renderActivity();
  pollEvents();
}

/** The Mixing screen's own way in: pick albums off the shelf and read them. */
function openMeasurePicker({ keepSearch = false } = {}) {
  const picked = new Set(
    [...$("measure-list").querySelectorAll(".measure-row")]
      .filter((row) => row.querySelector("input")?.checked)
      .map((row) => Number(row.dataset.unitId)),
  );
  const dialog = $("measure-dialog");
  const list = $("measure-list");
  const start = $("measure-start");
  const known = new Set(state.mixing.tracks.map((track) => track.unit_id));

  function draw() {
    const words = $("measure-search").value.trim().toLowerCase();
    const albums = (state.albums || []).filter((album) => {
      if (!words) return true;
      return [album.artist, album.title, album.folder]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(words));
    });
    start.textContent = STR.measureStart(picked.size);
    start.disabled = picked.size === 0;
    if (!albums.length) {
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = STR.measureNothingToPick;
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(
      ...albums.map((album) => {
        const row = document.createElement("li");
        row.className = "measure-row";
        row.dataset.unitId = String(album.unit_id);
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = picked.has(album.unit_id);
        const name = document.createElement("span");
        name.className = "measure-row-name";
        name.textContent = album.artist
          ? `${album.artist} — ${album.title || album.folder}`
          : album.title || album.folder;
        row.append(box, name);
        if (known.has(album.unit_id)) {
          const already = document.createElement("span");
          already.className = "measure-row-known";
          already.textContent = STR.measureAlready;
          row.appendChild(already);
        }
        row.addEventListener("click", (event) => {
          if (event.target !== box) box.checked = !box.checked;
          if (box.checked) picked.add(album.unit_id);
          else picked.delete(album.unit_id);
          start.textContent = STR.measureStart(picked.size);
          start.disabled = picked.size === 0;
        });
        return row;
      }),
    );
  }

  if (!keepSearch) $("measure-search").value = "";
  draw();
  if (!dialog.open) dialog.showModal();
}

$("mixing-sort").addEventListener("change", (event) => {
  state.mixing.sort = event.target.value;
  renderMixing();
});
$("mixing-search").addEventListener("input", (event) => {
  state.mixing.search = event.target.value;
  $("mixing-search-clear").hidden = !event.target.value;
  renderMixing();
});
$("mixing-search-clear").addEventListener("click", () => {
  state.mixing.search = "";
  renderBarSearch("mixing-search", "");
  $("mixing-search-clear").hidden = true;
  renderMixing();
});
// The three bars that hold a search, wired the same way.
for (const field of ["library-search", "mixing-search", "history-search"]) {
  wireBarSearch(field);
}
$("mixing-measure").addEventListener("click", openMeasurePicker);
$("measure-search").addEventListener("input", () => {
  if ($("measure-dialog").open) openMeasurePicker({ keepSearch: true });
});
$("measure-start").addEventListener("click", () => {
  // Read off each row's own id, never off its position: the list is filtered
  // by the search box, so an index into `state.albums` would measure
  // whichever albums happened to sit at those places.
  const picked = [...$("measure-list").querySelectorAll(".measure-row")]
    .filter((row) => row.querySelector("input")?.checked)
    .map((row) => Number(row.dataset.unitId))
    .filter((id) => Number.isFinite(id));
  $("measure-dialog").close();
  if (picked.length) measureHarmonics(picked);
});
/** What this screen's ⋯ can offer as things stand.
 *
 * One list, read by the button that draws itself and by the press that opens
 * the menu, so the two can never disagree about whether there is anything
 * here: a button drawn once and a list built at each press would leave the ⋯
 * looking pressable over an empty menu.
 */
function mixingMoreItems() {
  const items = [];
  if (state.mixing.chosenKey) {
    items.push({
      label: STR.mixClearKey,
      run: () => {
        state.mixing.chosenKey = null;
        state.mixing.chosenTrack = null;
        renderMixing();
      },
    });
  }
  return items;
}

/** This function owns that button — whether it can be pressed, and its words.
 *
 * A control must not answer a press with nothing. Disabled rather than
 * hidden: the bar keeps its shape, and the tooltip says what would put
 * something in it.
 */
function renderMixingMore() {
  const more = $("mixing-more");
  const offers = mixingMoreItems().length > 0;
  more.disabled = !offers;
  more.title = offers ? STR.tipMixMore : STR.tipMixMoreEmpty;
  more.setAttribute("aria-label", more.title);
}

$("mixing-more").addEventListener("click", (event) => {
  openAcquireMenu(event, mixingMoreItems());
});

// --- the name composer ------------------------------------------------------

/** The pieces chosen, per line, and what the backend offers.
 *
 * Held here rather than read back out of the template: a template is what the
 * engine needs, and reading it backwards to redraw chips would be a parser
 * whose disagreements with the writer are silent. What was built is saved
 * alongside what it produced.
 */
const composer = {
  offered: { folder: [], track: [] },
  chosen: { folder: [], track: [] },
  // Which preview is the current one. Every chip added, removed or moved asks
  // the backend again, and the answers come back over a bridge that does not
  // promise to keep their order — so without this the *last to reply* wins
  // instead of the last to be asked, and the preview can describe an
  // arrangement the chips on screen no longer show.
  asked: 0,
};

/** Turn a line of pieces into the template the engine takes.
 *
 * The rule produces a conventional arrangement without any syntax being
 * written: bare pieces join with " - ", a wrapped piece is preceded by a
 * space, and everything after the first is optional — so a piece the catalogue
 * did not fill takes its own brackets away with it instead of leaving them.
 */
function composeTemplate(pieces) {
  const closing = { "(": ")", "[": "]" };
  return pieces
    .map((piece, index) => {
      const value = `%${piece.token}%`;
      const body = piece.wrap ? `${piece.wrap}${value}${closing[piece.wrap]}` : value;
      if (index === 0) return body;
      // A bare piece follows a dash; a wrapped one just follows a space. The
      // whole segment is optional, which is what keeps " - " off a name whose
      // second piece rendered nothing.
      return piece.wrap ? `{ ${body}}` : `{ - ${body}}`;
    })
    .join("");
}

function drawComposerLine(which) {
  const host = $(`composer-${which}-parts`);
  host.textContent = "";
  composer.chosen[which].forEach((piece, index) => {
    const chip = document.createElement("span");
    chip.className = "composer-chip";
    if (piece.measured) chip.classList.add("is-measured");
    const name = document.createElement("span");
    name.textContent = piece.label;
    chip.append(name);
    // Ordering by button rather than by dragging: the order is the whole point
    // of this panel, and a drag that does not take is a gesture that failed
    // silently.
    for (const [mark, delta, tip] of [
      ["‹", -1, STR.composerEarlier],
      ["›", 1, STR.composerLater],
      ["✕", 0, STR.composerRemove],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = mark;
      button.title = tip;
      button.addEventListener("click", () => {
        const list = composer.chosen[which];
        if (delta === 0) list.splice(index, 1);
        else {
          const to = index + delta;
          if (to < 0 || to >= list.length) return;
          [list[index], list[to]] = [list[to], list[index]];
        }
        drawComposer();
      });
      chip.append(button);
    }
    host.append(chip);
  });
}

function drawComposerAdder(which) {
  const picker = $(`composer-${which}-add`);
  picker.textContent = "";
  const head = document.createElement("option");
  head.value = "";
  head.textContent = STR.composerAdd;
  picker.append(head);
  const taken = new Set(composer.chosen[which].map((piece) => piece.token));
  for (const piece of composer.offered[which]) {
    if (taken.has(piece.token)) continue;
    const option = document.createElement("option");
    option.value = piece.token;
    const note = piece.measured ? STR.composerMeasured : "";
    option.textContent = note ? `${piece.label} — ${note}` : piece.label;
    picker.append(option);
  }
  picker.value = "";
}

function drawComposer() {
  for (const which of ["folder", "track"]) {
    drawComposerLine(which);
    drawComposerAdder(which);
  }
  refreshComposerPreview();
}

/** Ask the backend what these two arrangements would name an album. */
async function refreshComposerPreview() {
  const templates = {
    folder: composeTemplate(composer.chosen.folder),
    track: composeTemplate(composer.chosen.track),
  };
  const box = $("composer-preview");
  if (!templates.folder && !templates.track) {
    box.textContent = "";
    return;
  }
  const mine = ++composer.asked;
  const answer = await api().naming_preview(templates.folder, templates.track);
  // Anything but the newest question is a stale answer, and painting it would
  // describe an arrangement that has already changed.
  if (mine !== composer.asked) return;
  if (answer.reason === "no_album") {
    box.textContent = STR.composerNoAlbum;
    return;
  }
  // A refusal is shown where it happened: the only moment to fix an
  // arrangement is before it names anything.
  const trouble = answer.folder_error || answer.track_error;
  box.classList.toggle("is-bad", Boolean(trouble));
  if (trouble) {
    box.textContent = trouble;
    return;
  }
  box.textContent = STR.composerPreview(
    STR.composerPair(answer.folder_shape || "—", answer.track_shape || "—"),
    STR.composerPair(answer.folder || "—", answer.track || "—"),
  );
  // The pieces this album cannot fill, named rather than left as a difference
  // between two lines that has to be spotted.
  const empty = [...(answer.folder_missing || []), ...(answer.track_missing || [])];
  if (!empty.length) return;
  const note = document.createElement("span");
  note.className = "composer-missing";
  note.textContent = `\n${STR.composerMissing([...new Set(empty)].join(", "))}`;
  box.append(note);
}

/** Load the pieces on offer, and whatever was last built. */
async function openComposer(settings) {
  const offer = await api().naming_composer();
  composer.offered = { folder: offer.folder || [], track: offer.track || [] };
  const byToken = {};
  for (const which of ["folder", "track"]) {
    for (const piece of composer.offered[which]) byToken[`${which}:${piece.token}`] = piece;
  }
  for (const which of ["folder", "track"]) {
    const saved = parseParts(settings[`${which}_parts`]);
    composer.chosen[which] = saved
      .map((token) => byToken[`${which}:${token}`])
      .filter(Boolean);
  }
  drawComposer();
}

/** Read the saved pieces back, tolerating a setting written by anything else. */
function parseParts(value) {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function resetComposer() {
  composer.chosen = { folder: [], track: [] };
  drawComposer();
}
