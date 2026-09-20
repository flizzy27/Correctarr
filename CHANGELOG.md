# Changelog

Versions follow `MAJOR.MINOR.PATCH`. The major number only goes up for changes
that break an existing installation.

## Unreleased

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
