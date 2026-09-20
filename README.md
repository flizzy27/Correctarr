<div align="center">

# Correctarr

**Finds and fixes the problems Radarr, Sonarr, SABnzbd and Prowlarr leave
lying around — the ones that sit there because nobody is looking.**

[![Build](https://github.com/flizzy27/Correctarr/actions/workflows/docker.yml/badge.svg)](https://github.com/flizzy27/Correctarr/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

<img src="docs/screenshot-overview.png" alt="The overview page" width="900">

</div>

---

## What it is for

Radarr does notice some of these, but it never resolves them on its own:

| Problem | What happens |
|---|---|
| **Wrongly matched releases** | Radarr stops with "Manual Import required" and then waits forever. From production: `Halloween.2018…` had been matched to *Halloween (1978)*. |
| **Downloads after a profile change** | A release is scored exactly once, when it is grabbed. Change a profile afterwards and downloads already in flight run to nothing. |
| **Files that cannot be matched** | Radarr does not find `Crank.I.2006`, although the film is in the library as *Crank* with the alternate title *Crank 1*. Files like that sit there finished for days. |
| **3D releases** | Radarr structurally cannot reject them — its parser swallows markers placed before the year. |
| **Leftover debris** | Half RAR sets, empty folders, failed downloads. |
| **Quiet indexer problems** | A disabled indexer is easy to miss but affects every single search. |

## How it works

Three ways, at the same time:

1. **Events** — Radarr and Sonarr report through a webhook the moment they grab
   something or need manual work. Reaction within seconds, before a wrong
   download has even finished.
2. **Fast pass** — 60 seconds by default, queue rules only.
3. **Deep pass** — 90 minutes by default, additionally the library and the
   indexers.

## The rules

**26 rules across six categories.** Each one can be switched independently
between *check* and *fix*, plus a global dry run that stops every change.

<details>
<summary><strong>All 26 rules</strong></summary>

### Queue
| Rule | What it finds | Default |
|---|---|---|
| `wrong_year` | Year in the release name does not match the title | fixes |
| `wrong_title` | Release name resembles no known title | reports |
| `profile_violation` | Download breaks today's profile rules | fixes |
| `not_an_upgrade` | Import would not be an upgrade | fixes |
| `stalled` | A started download stopped moving | reports |
| `grab_loop` | The same title is grabbed over and over | reports |

### Import
| Rule | What it finds | Default |
|---|---|---|
| `manual_import` | Import waiting on manual work, but the match is right | fixes |
| `unpack_failed` | Unpacking really failed | reports |
| `detached_folder` | Service and container see different download folders | reports |
| `leftover_files` | Leftover download debris | **deletes** |
| `unmatched_files` | File without a match — matches it itself | **imports** |

### Library *(deep pass only)*
| Rule | What it finds | Default |
|---|---|---|
| `missing_audio_language` | Existing file lacks the wanted audio language | reports |
| `unreadable_file` | File cannot be read | reports |
| `below_profile` | Existing file would be blocked today | reports |
| `missing_items` | Monitored and released, but no file | reports |

### Download client
| Rule | What it finds | Default |
|---|---|---|
| `downloader_warning` | SABnzbd is reporting a warning | **clears it** |
| `downloader_stale_entry` | Entry finished, file long since imported | **cleans up** |
| `downloader_paused` | The download client is paused | reports |
| `downloader_disk_space` | Little free space | reports |
| `downloader_update` | A new version is available | reports |

### Indexers *(read only)*
| Rule | What it finds | Default |
|---|---|---|
| `indexer_disabled` | Prowlarr has switched an indexer off | reports |
| `indexer_ineffective` | Many queries, practically no grabs | reports |
| `indexer_ranking` | The order contradicts the measured usefulness | reports |
| `indexer_unknown` | Indexer bypasses Prowlarr | reports |

### System
| Rule | What it finds | Default |
|---|---|---|
| `service_health` | The service reports a problem about itself | reports |
| `disk_space` | Free space is running low | reports |

</details>

**Guiding principle: when in doubt, do nothing.** A rule that is not sure only
reports. Better one finding left alone than one good file thrown away.

<img src="docs/screenshot-rules.png" alt="Every rule can be switched between check and fix" width="900">

Every rule shows what it does before you turn it on: whether it only reports,
acts, or deletes; whether it runs on every pass or only the deep one; and how
often it has matched so far.

## Two things Radarr cannot do itself

**Radarr does not see the whole release name.** A custom format with a title
regex is applied to what is left after the recognised title has been split off.
Markers placed before the year are swallowed by the parser:

```
Black.Panther.3D.HOU.2018…  → parsed title "Black Panther 3D HOU", 3D not detected
Meg.2018.HSBS…              → parsed title "Meg",                  3D detected
```

Radarr structurally cannot reject those. Correctarr checks the raw name.

**Radarr scores only once.** Waiting downloads are never re-examined against
changed profile rules. Correctarr re-scores them.

Everything else that turned up in production is written down in
[`docs/background.md`](docs/background.md).

## Setup

### Unraid

Search for *Correctarr* in **Community Applications**. The required settings are
the port, the configuration directory and the folder holding finished downloads.

> **About the paths:** they have to be the same inside the container as the ones
> Radarr and Sonarr see. Mount the download folders exactly the way you mounted
> them there. The overview page shows you whether that worked.

### Docker

```bash
docker run -d \
  --name correctarr \
  --restart unless-stopped \
  -p 8099:8099 \
  -v /path/to/appdata/correctarr:/config \
  -v /path/to/downloads:/downloads \
  -v /path/to/incomplete:/incomplete \
  -e PUID=99 -e PGID=100 \
  -e TZ=Europe/Berlin \
  ghcr.io/flizzy27/correctarr:latest
```

### Docker Compose

```yaml
services:
  correctarr:
    image: ghcr.io/flizzy27/correctarr:latest
    container_name: correctarr
    restart: unless-stopped
    ports:
      - "8099:8099"
    volumes:
      - ./appdata/correctarr:/config
      - /path/to/downloads:/downloads
      - /path/to/incomplete:/incomplete
    environment:
      PUID: 99
      PGID: 100
      TZ: Europe/Berlin
      AUTH: "on"
```

### Then

1. Open the interface → **create an account** (username and password).
2. **Services** → add Radarr, Sonarr, SABnzbd and Prowlarr, and test each one.
3. **Settings → Appearance** → enter the public address of this interface.
   Only then can the webhook be set up.
4. **Settings → Schedule** → turn *dry run* on and watch one pass before
   anything is changed.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUTH` | `on` | `on` = built-in login, `off` = none (only behind Authelia or similar) |
| `PORT` | `8099` | Port inside the container |
| `HOST` | `0.0.0.0` | Bind address |
| `BASE_URL` | – | Sub path behind a reverse proxy, e.g. `/correctarr` |
| `PUID` / `PGID` | `99` / `100` | Ids the service runs as |
| `UMASK` | `022` | Permission mask for new files |
| `TZ` | `Etc/UTC` | Timezone |
| `LOGLEVEL` | `INFO` | `DEBUG` for troubleshooting only |

Everything else — schedule, paths, thresholds, notifications, appearance — is
configured in the interface and survives every update.

<img src="docs/screenshot-settings.png" alt="Every setting has an explanation" width="900">

## Seeing what it did

<img src="docs/screenshot-fixed.png" alt="A record of every change that was made" width="900">

The *Fixed* page lists only changes that actually happened — plain reports and
dry runs are not in there. It is the record of what the program did on your
behalf, which matters for something that is allowed to delete files.

## Languages

The interface ships in **English and German** and follows your browser by
default. You can pin either one under *Settings → Appearance*. Findings recorded
months ago are re-rendered in whichever language is active, because the store
keeps the message key and its parameters rather than a finished sentence.

## Behind a reverse proxy

If the interface runs under a sub path, set `BASE_URL`. For nginx:

```nginx
location /correctarr/ {
    proxy_pass         http://127.0.0.1:8099/;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

`X-Forwarded-Proto` is not optional: without that header the service treats an
HTTPS connection as plain and sets the session cookie without `Secure`.

## Notifications

Pushover, bundled at the end of a pass and grouped by rule. Two brakes stop
floods: **deduplication** (the same finding reports again no sooner than 12
hours later) and a **cooldown** (5 minutes of quiet after a message).

## Security

* Built-in login, PBKDF2-HMAC-SHA256 with 600,000 rounds and a per-user salt.
* Sessions are stored as a digest, never as the token itself.
* API keys never leave the server — not through the interface and not into the
  log.
* Failed sign-ins are throttled with a growing delay.

`AUTH=off` disables the login when something in front already handles it.
Without one of the two, the port does not belong on the open internet.

## Updates

Settings, services and history live under `/config` and survive every update.
The schema is upgraded automatically at startup, and a backup of the file is
written next to it before anything changes.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest ruff
.venv/bin/python -m pytest
.venv/bin/python -m ruff check app/ tests/
```

The test suite also verifies that every shipped language is complete, so a
forgotten translation fails the build instead of reaching a browser as a raw
key.

## License

[MIT](LICENSE)
