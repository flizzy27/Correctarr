# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The major number only goes up for changes
that break an existing installation.

## Unreleased

### Added

* **A finding that was held back now says why.** A rule can be told to wait for
  an age, to leave anything over a size alone, or to act only above a
  confidence. When one of those stops it, the reason has been recorded since
  the day conditions were added — and shown to nobody. A rule waiting out its
  own age limit looked exactly like a rule that had done nothing.
* **Severity is written out, not only coloured.** It was a three pixel stripe
  down the left edge of a finding and nothing else, which is no difference at
  all to a fair number of people.
* **A severity filter on the findings page**, beside the rule filter.
* **Five more rules, and Sonarr stops being a second-class citizen.** The
  library rules that read a file — missing audio language, unreadable file,
  below the profile — used to say "Radarr only". That was never a statement
  about the question; a series file lacks a language, fails to read and falls
  below a profile exactly the way a film does. It was a statement about this
  program, which only knew how to find a movie's file. It knows how to find a
  series' files now, and says so.
* **`premature_grab`.** A release for something that is not out yet. There is
  no honest copy of a film three weeks from its cinema date and no honest copy
  of an episode that has not aired; what turns up under those names is a
  re-encode of a trailer or a different film with the right name on it.
  Reporting by default, with a day of grace, because release dates carry no
  time zone.
* **`cutoff_unmet`.** There is a file, but it is below the quality the profile
  asks for. The services keep this list and will act on it when asked — the
  problem is that nobody asks. A cutoff that is never met is invisible: the
  title looks complete in every view and it plays.
* **`season_gaps`** *(Sonarr)*. A season that is *partly* there, which is a
  different thing from one nobody has started: eight of ten episodes means a
  season pack that imported partly, or two episodes that failed months ago and
  were never noticed. Costs no extra request — the counts arrive with the
  series list.
* **`series_incomplete`** *(Sonarr)*. A series that has finished airing and is
  still missing episodes. A running series missing last week is waiting for an
  indexer to catch up; one that ended four years ago is missing them for good.
* **`stale_blocklist`.** The blocklist is permanent and nothing ever reviews
  it. A release refused months ago because it failed to unpack once is still
  refused today, and if it was the only copy anyone had, the title simply never
  comes. Raised only where the title is still missing, with a new action —
  **take off the blocklist and search again** — because removing the entry on
  its own changes nothing.

### Changed

* **The result of an action is translated.** It used to be a sentence, built
  where the action ran and stored as written, so the German interface read
  "blocklisted, new search started" in English — on the one line that says what
  actually happened to somebody's files. Results now travel as a key and are
  rendered when they are read, including for rows written months ago. The
  English wording is kept in the store as well, because that column is queried
  for the dry run and failure markers and those queries are not localised.
* **One definition of "this really happened".** There were three copies of it —
  in the store's counting, in the notification filter and in the interface —
  and they did not agree about `FAILED:`.
* The history is fetched once per connection per run instead of once per
  question. Blocklisting ten orphaned files asked for the same five hundred
  rows ten times, and a deep pass asked every service for them twice.

### Fixed

* **Several messages at once showed as one.** The strip they appear in had no
  styling of its own, so every message was positioned at the same fixed corner
  and landed exactly on top of the one before it. A run reporting three errors
  showed the third.
* **The sidebar could not be scrolled on a short window.** A flex child does
  not shrink below its content unless it is told to, so on a laptop at 125 %
  or a phone turned sideways the list grew past the bottom of the screen and
  took the sign-out button with it.
* **Full height was measured against the wrong thing on a phone.** A phone's
  toolbars slide in and out, and `100vh` is the height without them — so a
  full-height element was taller than the visible area and its last row sat
  under the address bar.
* **The back button left the application.** Views replaced each other in the
  address bar instead of stacking, so going back from the fourth page left the
  site altogether. An address pasted into the bar of a page that was already
  open did nothing at all.
* **Tab escaped the setup dialog** into the page behind it, where every control
  is hidden from the eye but not from the keyboard, with no way back except the
  mouse. The dialog also takes the focus when it opens and returns it when it
  closes.
* **"open" appeared where a name belongs.** With the login switched off the
  server answers with a stand-in account by that name; printed in the sidebar
  it read as though somebody were signed in under it, untranslated in both
  languages.
* A table that continues past the edge of the screen now says so.
* The filters on the findings page were given their widths in the markup,
  which a narrow screen cannot argue with: two filters side by side came out
  different lengths for no visible reason.
* **A stalled download was never reported on an install with more than one
  service.** The rule that measures whether a download is still moving keeps
  its measurements in the store, and once per service it dropped every entry it
  did not recognise. With two services they took turns deleting each other's
  rows, so nothing survived to a second pass and nothing could ever be measured
  as standing still. Measurements now belong to the connection that made them.
* **An action could be carried out against the wrong instance.** Two Radarr
  instances are the same *kind* and share nothing else: queue id 41 names one
  download in the first and a different one — or none — in the second. Findings
  now come back to the instance that produced them.
* **Sonarr: a missing episode was searched for as if it were a series.** Sonarr
  answers the "what is missing" question with episodes, where `id` is the
  episode; that id was passed to a series search. Missing episodes now carry
  their series, and the search asks for exactly the episodes that are missing
  rather than re-searching the whole series.
* **Sonarr: importing a file reported success and imported nothing.** Sonarr
  imports episodes, not series. A file handed over with only a series id is
  accepted and then quietly does nothing, so the action claimed success on
  every run while the file stayed where it was. The episodes now travel with
  the import.
* **The free space rule could switch itself off.** The services answer the disk
  question per mount, and a mount is usually a shorter path than the library
  folder on it — often just `/`. Comparing the two for equality discarded every
  row on such an install. Mounts and root folders are now matched by
  containment, and when nothing matches at all everything is reported rather
  than nothing.
* **Blocklisting a release that had left the queue often did nothing.** The
  recorded release name and the folder on disk shorten each other in either
  direction, and only one of the two directions was tested.
* **A deleted file could claim an orphaned folder.** History entries were
  searched for a matching name without regard to what kind of event they were,
  and a row about a deleted or renamed file carries a path rather than a
  release name.
* **Sonarr: searching for several series searched for one.** Sonarr's series
  search takes a single series; the remaining ids were dropped without a word.
* **Failed actions were counted as fixes.** The badge and the tiles counted
  anything with a result, including `FAILED:`.
* **An indexer whose grabs all scored zero looked like it had no history.** A
  score of zero is a measurement, not a missing one. It was excluded from the
  quality benchmark and additionally told it had too little data to judge.
* The download percentage in the queue view could read below zero while a
  repair was running.
* **The year check condemned episodes of any series running longer than a
  year.** A series is not *from* a year the way a film is — it runs. An episode
  of a show that started in 2015 carries this year's date, and daily
  programmes are named by date outright: `Show.Name.2024.03.04.1080p.WEB`. Held
  against the year the series is filed under, every one of those read as a
  different programme — and that rule blocklists and searches again by default,
  so good episodes were thrown away as fast as they arrived. The whole
  broadcast span is accepted now, left open at the top unless the service says
  the series has ended *and* says when it last aired.
* **`manual_import` could never fire for a series.** It demanded that the
  release state a year, which is how a film identifies itself. An episode
  states an episode instead, so every one of them sat waiting for somebody to
  press the button by hand.

## 1.1.0

### Added

* **An action per rule, instead of a switch.** Every rule now declares what it
  is allowed to do and you pick from that: report only, remove from the queue,
  blocklist, blocklist and search again, import, import and clean up, search,
  read the file again, delete, acknowledge a warning, remove an entry, resume.
* **Conditions.** A rule can be told to wait a minimum age, to leave anything
  over a size alone, or to act only above a confidence. A finding that fails a
  condition is still reported, with the reason recorded next to it. A rule only
  offers the conditions its own findings can answer.
* **Five more notification channels.** Telegram, Discord, ntfy, Gotify and a
  plain webhook, alongside Pushover. Each connection filters for itself by
  severity, rule, category or "only what was changed", so several can run side
  by side without either becoming noise.
* **A setup assistant**, opening by itself on a fresh install: services with a
  connection test each, paths, the callback address, notifications, and a dry
  run to finish on.
* **`api_changes` rule.** The services mark parts of their API as replaced
  before removing them. That notice is now surfaced as an ordinary finding,
  months before anything stops working.
* **Permissions are explained.** The paths page works out which rules are set
  to remove files and asks for write access only where that matters, naming the
  rules.
* **`ca_profile.xml`**, required for a Community Applications submission.

### Changed

* **A fresh install starts in dry run.** Two rules delete by default and nobody
  installing for the first time has agreed to that yet. Everything is found and
  reported, nothing is touched, until the switch is turned off deliberately. An
  existing installation keeps whatever it was set to.
* **The year check was rebuilt.** It was a subtraction, and wrong both ways. A
  title that is itself a number supplied its own year, so
  `1917.German.DL.1080p` — which states no release year at all — was compared
  against 2019; the same for `2012`, `Blade Runner 2049` and a resolution
  written out as `1920x1080`. In the other direction it rejected legitimate
  differences: a festival premiere one year and cinemas the next, a limited
  December run with a March disc, a home release a year ahead of the Western
  one. It now discounts numbers belonging to the title and accepts the premiere
  year and the release dates the service already holds, reporting a confidence
  so a near miss and a twenty year gap can be treated differently.
* Acting is dispatched on the chosen action rather than the rule name, so four
  queue rules share one blocklist implementation instead of four copies.
* Searching the rules list no longer refetches from the server on every
  keystroke.

### Fixed

* **Health webhooks never arrived.** The setting is called `onHealthIssue` but
  the value sent on the wire is `Health`, and the receiver matched the setting
  name.
* **Registering a webhook could fail at startup.** The services send a test
  call to the address before storing it, which nothing answers while this
  container is still coming up. The save is now forced.
* The remaining size of a queue entry is read under both its current and its
  announced future spelling, so the stalled rule survives that rename.
* The download client status call no longer asks for the dashboard, which was
  resolving public addresses and doing a DNS lookup on every pass.
* History events are matched by name rather than by numbers that mean
  different things in each application.
* A service that answers "still starting up" is asked again instead of being
  reported as an outage.

---

## 1.0.0

First public release.

### Features

* **26 rules across six categories** — queue, import, library, download client,
  indexers and system. Each one switches independently between *check* and
  *fix*, with a global dry run that stops every change.
* **Three ways of working at once**: webhooks for a reaction within seconds, a
  short schedule for the queue, a deep pass for the library and the indexers.
* **Re-scoring of waiting downloads** against the profile rules that apply
  today. A release is scored only once, when it is grabbed; change a profile
  afterwards and a download already in flight runs to nothing.
* **Matching of orphaned files** through the grab history first and name
  matching second, comparing against every alternate title.
* **Indexer rating** from yield, quality of the grabs, reliability and speed —
  read only, never changed.
* **Authentication.** The first start creates an account; after that the
  interface is protected. PBKDF2-HMAC-SHA256 with 600,000 rounds, sessions
  stored only as a digest, failed sign-ins throttled with a growing delay. Can
  be disabled with `AUTH=off` when something in front already handles it.
* **English and German interface**, following the browser by default. Findings
  recorded months ago are re-rendered in whichever language is active, because
  the store keeps the message key and its parameters rather than a finished
  sentence.
* **Seven dark themes** and three density settings. There is deliberately no
  light theme.
* **Everything configurable in the interface** — schedule, paths, thresholds,
  cleanup, indexers, notifications, appearance and maintenance — with an
  explanation for every value and server side validation.
* **Configurable paths** for finished and incomplete downloads, movies and
  series. The overview shows whether those paths actually exist inside the
  container, which is the single most common setup mistake.
* **Reverse proxy support**: `BASE_URL` for sub paths, `X-Forwarded-Proto` and
  `X-Forwarded-For` honoured.
* **PUID, PGID and UMASK** are applied. Only `/config` is taken over —
  never the mounted media folders.
* **Schema versioning.** The version lives in `PRAGMA user_version`, migrations
  run exactly once at startup, and a backup of the file is written before
  anything changes. A store written by a pre-release build is adopted along with
  its settings, services and history.
* **A dedicated liveness endpoint** at `/api/alive` that makes no outbound call
  at all.
* **503 tests**, including end to end calls through the whole stack and a
  check that every shipped language is complete — a forgotten translation fails
  the build instead of reaching a browser as a raw key.

### Found while testing, and fixed

These were caught before release by the tests and by driving the interface in a
browser. They are listed because the reasoning behind each fix is worth keeping.

* **An unknown setting key crashed the request.** The helper that builds a
  translated error took its translation key as a parameter called `key`, and
  `error.unknown_setting` has a placeholder of the same name. The two collided
  and raised a TypeError, turning a clean 400 into a 500.
* **The overview stayed blank for twelve seconds.** Services were contacted one
  after another, so four unreachable ones added up — every thirty seconds,
  because the page refreshes itself. They are now contacted at once, with a
  shorter timeout for the probe.
* **Behind a reverse proxy that does not strip its prefix, nothing matched.**
  The health check would have landed on the sign-in page and the container
  would have been reported as unhealthy while working perfectly.
* **Adopting an older store could strand data.** The copy ran only as part of a
  migration; if the schema step had once succeeded and the copy then failed,
  the next start skipped both.
* **Two different health problems from the same source were deduplicated into
  one.** Findings without an identifier of their own now fall back to their
  message rather than the title alone.
* **`q=0` in an `Accept-Language` header could select a language.** It means
  "not acceptable" and is now treated that way.
* **Password fields were not inside a form**, so password managers neither
  offered to fill them nor to store the new password.

### Notes for anyone upgrading from a pre-release build

* The container, the database file and the webhook entry in Radarr and Sonarr
  are all named *Correctarr* now. An existing webhook is adopted and renamed
  rather than leaving a duplicate behind.
* The store is migrated automatically. Settings, services and history are kept.
* The rule names have changed. Any rule you had switched off will be back at its
  default and needs setting again.
