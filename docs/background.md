# Background

This file explains **why** Correctarr is built the way it is in a few places.
Every point here turned up in production and cost real work. Anyone who undoes
one of them rebuilds the same bug.

---

## 1. Radarr does not see the whole release name

`ReleaseTitleSpecification` is **not** applied to the full release name. It is
applied to what is left after the recognised title has been split off. A marker
placed **before** the year is swallowed by the parser. Verified through
`/api/v3/parse`:

```
Black.Panther.3D.HOU.2018.German.DTS.DL.1080p.BluRay.x264-LeetHD
    → parsed title:     "Black Panther 3D HOU"
    → formats detected: 1080p Bluray, DTS          (3D NOT detected)

Meg.2018.HSBS.German.Dubbed.AC3.DL.1080p.BluRay.x264-miHD
    → parsed title:     "Meg"
    → formats detected: … 3D Formats …             (3D detected)
```

The published 3D patterns work around this by requiring the marker to appear
after the year: `(?<=\b[12]\d{3}\b).*\b(…3d|sbs…)\b`.

So Radarr structurally cannot reject such a release. `app/scoring.py` checks the
**raw** release name instead and catches what Radarr misses.

## 2. Python cannot read Radarr's patterns

Radarr runs on .NET. The patterns from the published profile databases use
variable width lookbehind throughout, for example `(?<=^|[\s.-])YIFY\b`. Python's
built-in `re` refuses those — measured, **523 of 814 patterns failed (64
percent)**, so the scoring was silently incomplete. The `regex` module handles
variable lookbehind the way .NET does: **814 of 814**.

That is why `app/scoring.py` does `import regex as re`. Do not undo it.

## 3. Language must not be checked before the import

Before the import, the language is taken from the file name. The marker `.DL.`
common in German releases (German plus original audio) is not something the
parser knows, so those entries often report only `["English"]` even though the
file does have a German track. Only **after** the import are the real audio
tracks read.

Proof: `Zodiac.DC.2007.1080p.BluRay.AC3.DL.x264-HDC` reports English in the
queue; the same group reports German+English after the import.

`LanguageSpecification` is therefore **deliberately excluded** in
`app/scoring.py`. Without that exclusion good releases are thrown away in bulk —
measured at 19 false positives in a single pass.

The `missing_audio_language` rule is reliable precisely because it measures the
real audio tracks *after* the import.

## 4. Localised distribution titles

Comparing a release name only against the title and the original title flags
every localised release as wrongly matched — "Stichtag" for *Due Date*, "Wir"
for *Us*, "Der Unsichtbare" for *The Invisible Man*.

Those titles live in `alternateTitles`, but **only in the full item list**; the
queue entry has the field empty. That is why `wrong_title` needs the deep pass
and stays quiet otherwise.

With alternate titles: **0 false positives**. Without them: **23 of 23**.

## 5. `_UNPACK_` is not a fault

That is what SABnzbd calls a folder **while** it is unpacking. An earlier build
reported every such folder immediately — 18 of 25 findings per hour were exactly
that, pure noise. During a download wave new ones appear constantly, and each
counted as a new finding.

Three things were wrong with it:

* **No age comparison.** A folder being unpacked at that very moment counted as
  stuck.
* **No question to the download client.** SABnzbd would have known it was still
  working on it.
* **File time used as the measure** — see point 6.

And the description was wrong too: the reported folders held **no RAR file at
all** any more, just a finished video each. Unpacking had succeeded; only the
rename had not happened.

The distinction now:

| State | Detected by | Handling |
|---|---|---|
| still running | the download client knows the job | nothing |
| done, not renamed | a usable video is in there | `unmatched_files` imports it |
| failed | no video, old enough, unknown | `unpack_failed` reports it |

## 6. The file time lies

Unpacked files carry the timestamp stored inside the archive. Measured: **3.8
years** for a folder that was in fact 2.6 hours old.

For anything to do with age, always take the time of the **folder**, never the
file.

## 7. Folders are created on queueing, not on download

SABnzbd creates the folder when a download enters the queue, not when it starts.
With a long queue a folder can therefore sit untouched for hours and be
perfectly fine.

Age alone is therefore **not** a usable criterion for debris. It always needs
the comparison against the download client's queue and history. If no download
client is configured, or it does not answer, `leftover_files` deletes **nothing**
and says so.

Verified in production: of 129 folders exactly two were removed — one from
25 August, one from 24 April. Two further folders with nearly identical names
(`Sonic.The.Hedgehog.2020…TSCC` next to `Sonic.the.Hedgehog.2020…toto`) were left
alone because SABnzbd still tracked them.

## 8. The grab history is the most reliable source

Radarr records which title it grabbed a release for. On import that link is
lost — from then on only the file name is parsed, and that is exactly what fails
for `Crank.I.2006` or `The.Transporter.The.Mission.2005`. The history still has
the answer.

`app/matching.py` is therefore only the second port of call: first the history
(a fact), then name matching (a guess).

Both of those files sat there finished for 13 and 14 hours, 20 GB together. For
Transporter, Radarr had already deleted the old file in anticipation of the
upgrade — the movie was left with no file at all.

## 9. Three traps in name matching

* **Non-Latin scripts produce phantom matches.** The Russian alternate title
  "Хроники Нарнии 1" boils down to exactly "1" after normalisation — and "1" sits
  entirely inside "crank 1", giving a 100 percent match with a completely
  different film. Such transliteration ruins are rejected before the comparison.
* **A single shared word proves nothing.** "Saw" sits entirely inside "Saw II"
  but is a different film. The containment measure therefore requires at least
  two words and similar lengths.
* **A perfect match beats the margin rule.** "Saw II" matches "Saw II" at 100
  percent while "Saw III" reaches 92 — the margin is small but the match is
  beyond doubt. Without that exception the clearest match of all would be
  rejected.

## 10. Minimum sizes are not a verdict on the library

`below_profile` initially reported 189 files, 170 of them only because they fell
below the minimum size. But the lower bounds in a profile say what should still
be **grabbed** — they are not a judgement on what is already there.

Pure size violations are therefore excluded. What is left is the substantive
cases: mic dubs, AAC, 3D, Xvid.

## 11. Missing data is not a bad report card

In indexer rating, quality initially entered as 0 when no history samples were
available — which handed the indexer with the best score in the field (540,000)
a grade of 0.

Now that part drops out of the calculation entirely and the remaining weights
are scaled back up to 100 percent.

When comparing against the configured order, what counts is the **rank**, not
the absolute number: if the best indexer sits at priority 1 there is nothing to
say, whether that number reads 1, 5 or 10. A deviation is only reported from two
places; with a tolerance of 1, five of six indexers came up in testing, which is
noise.

## 12. Notifications need two brakes

On a short schedule every pass reports the same long-running problems. Both of
these are needed:

* **Deduplication** — the same finding reports again no sooner than 12 hours
  later.
* **Cooldown** — 5 minutes of quiet after a message.

And Pushover needs `html: 1` in the call, otherwise the `<b>` markers show up
literally.

## 13. Size alone does not identify a film

`downloader_stale_entry` detects a finished job by finding a library file of
matching size. An early draft compared size only — and reported that *Crank* was
"already present as *Hangover 3*". Several films happen to be the same size to
within one percent.

The title has to match as well.

## 14. Rules run either per service or once

Ten rules concern things that exist only once: the download client, the
filesystem, the indexers. An earlier build solved that with
`if arr.kind != "radarr": return []`.

That was wrong twice over:

* With **two** Radarr instances they ran twice and reported twice.
* On an install **without** Radarr — Sonarr only — ten of twenty-six rules
  silently did nothing, with no sign of it anywhere.

Every rule now carries its scope (`service` or `once`) and, where relevant, a
restriction to certain kinds. What applies to Radarr only says so in the
interface.

## 15. The store grows faster than you expect

Measured: **1,053 entries in 22 hours** on a 60 second schedule. Extrapolated,
roughly 400,000 rows a year. Without a limit the store becomes sluggish.

`log_keep` and `log_days` trim automatically, and both can be applied
immediately under *Settings → Maintenance*.

## 16. Credentials do not belong in the log

httpx logs every request including the full URL at INFO level. SABnzbd expects
its key as part of the URL. The result was the key appearing in clear text in
`docker logs` several times a minute — and so in every log excerpt anyone pastes
into a forum thread while asking for help.

`app/logging_setup.py` therefore moves httpx to WARNING and puts a filter over
all output that redacts anything looking like a key.

---

## Working practices that have proved themselves

* **Dry run first, always.** The switch under *Settings → Schedule* stops every
  modifying rule. Every rule that touches anything was tested dry first — and
  every single time that turned up a mistake.
* **Measure against real data, do not guess.** German audio detection rests on
  an `ffprobe` measurement of all 470 files, not on assumptions about release
  names.
* **When in doubt, do nothing.** A rule that is not sure only reports. Better one
  finding left alone than one good file thrown away.
* **Write every trap down in the code.** The comments in `app/rules.py` and
  `app/matching.py` name the concrete case each guard came from. That is
  deliberate — it stops somebody removing the guard later.
