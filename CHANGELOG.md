# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The major number only goes up for changes
that break an existing installation.

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
