"""What each action does to a finding.

One method per action, ``_act_`` followed by the action as :mod:`app.policy`
names it, and :meth:`Actions._perform` to find the right one.

They are a part of :class:`app.engine.Engine` and use what it holds: the
store, the connections to the download clients (``downloader`` and
``downloaders``), and the queue as it was last read in this run.
"""
from __future__ import annotations

import logging
import os
import shutil

from . import compat, policy
from .arr import Arr, ArrError, GoneError
from .rules import ALL, Finding
from .rules import SEARCH_GAP_HOURS as rules_gap_hours
from .rules import _allowed_quality_ids as rules_allowed_ids
from .rules import retune_key as rules_retune_key
from .rules import searching_for as rules_searching_for
from .sab import SabError
from .storage import Store

log = logging.getLogger(__name__)

done, dry, failed = policy.done, policy.dry, policy.failed


class Actions:
    """The actions an :class:`~app.engine.Engine` can carry out."""

    store: Store
    #: The queue as it is right now, per open connection. See _queue_id.
    _fresh_queues: dict[int, dict]

    def _perform(self, action: str, arr: Arr, finding: Finding,
                 cfg: dict) -> policy.Outcome | None:
        """Carry out one action on one finding.

        Looked up by name rather than decided by a chain of ``if`` statements on
        the rule. That way the set of actions is open: a new one needs a method
        and an entry in ``policy.ACTIONS``, and the interface offers it without
        anything here changing. It also means a rule and an action are no longer
        the same thing — the same "blocklist and search again" is shared by four
        rules that used to each carry their own copy of it.
        """
        handler = getattr(self, "_act_" + action, None)
        if handler is None:
            # Reachable if a stored configuration names an action this build
            # does not have — a downgrade, essentially. Report it and do
            # nothing; the finding is still recorded either way.
            log.warning("No handler for action %r (rule %s)", action, finding.rule)
            return None
        return handler(arr, finding, cfg, bool(cfg.get("dry_run")))

    # Every handler hands back an Outcome — a translation key, its parameters
    # and which of the three things happened — or None when there was nothing
    # to do. Not a sentence: the result is the line somebody reads on the
    # findings page, and a sentence built here can only be in one language.

    def _act_remove(self, arr: Arr, finding: Finding, cfg: dict,
                    is_dry: bool) -> policy.Outcome | None:
        """Out of the queue, but not blocklisted — it may be grabbed again."""
        if finding.entry_id is None:
            return None
        if is_dry:
            return dry("action.dry.remove")
        arr.remove_from_queue(self._queue_id(arr, finding),
                              blocklist=False, search_again=False)
        return done("action.result.removed")

    def _act_blocklist(self, arr: Arr, finding: Finding, cfg: dict,
                       is_dry: bool) -> policy.Outcome | None:
        if finding.entry_id is None:
            return self._blocklist_by_history(arr, finding, is_dry, search=False)
        if is_dry:
            return dry("action.dry.blocklist")
        arr.remove_from_queue(self._queue_id(arr, finding),
                              blocklist=True, search_again=False)
        return done("action.result.blocklisted")

    def _act_blocklist_and_search(self, arr: Arr, finding: Finding, cfg: dict,
                                  is_dry: bool) -> policy.Outcome | None:
        # A finding that was never in the queue has no entry to remove — an
        # unmatched file, for instance, left it long ago. The grab is still in
        # the history though, and marking that failed has the same effect.
        if finding.entry_id is None:
            return self._blocklist_by_history(arr, finding, is_dry, search=True)
        if is_dry:
            return dry("action.dry.blocklist_and_search")
        arr.remove_from_queue(self._queue_id(arr, finding),
                              blocklist=True, search_again=True)
        return done("action.result.blocklisted_searched")

    def _queue_id(self, arr: Arr, finding: Finding) -> int:
        """The id the queue entry has *now*.

        Radarr 6 does not keep a queue entry's id. Measured on a live
        instance: an entry blocked for two days was listed as 332146117 when it
        was found and as 1389512447 when somebody pressed its button, and the
        removal was answered with "not found" — for an entry that was still
        sitting there. A pass acts on ids it has just read, so it never saw
        this; a button pressed an hour later always did.

        So the entry is looked up again by what does not change: its download
        id, and the title it is for. Gone from the queue means gone — raised as
        such, not reported as a failure. Without a download id there is nothing
        better than the stored id to go on.
        """
        download = str(finding.data.get("_download")
                       or finding.data.get("downloadId") or "").lower()
        if not download:
            return finding.entry_id
        cache = self._fresh_queues.setdefault(id(arr), {})
        if "records" not in cache:
            cache["records"] = arr.queue()
        matching = [e for e in cache["records"]
                    if str(e.get("downloadId") or "").lower() == download]
        if not matching:
            raise GoneError(f"{arr.name}: the download has left the queue")
        stored = finding.entry_id
        for entry in matching:
            if entry.get("id") == stored:
                return stored
        item = finding.data.get("item_id")
        for entry in matching:
            if item and item in (entry.get("movieId"), entry.get("seriesId"),
                                 entry.get("episodeId")):
                return entry["id"]
        return matching[0]["id"]

    def _act_search(self, arr: Arr, finding: Finding, cfg: dict,
                    is_dry: bool) -> policy.Outcome | None:
        item_id = finding.data.get("item_id")
        episodes = [int(e) for e in (finding.data.get("episode_ids") or [])]
        # A gap in a season names the season but not the episodes in it — that
        # would mean fetching every episode of every series on every pass, for
        # a rule that mostly has nothing to report. So the list is worked out
        # here, once, for the one series being acted on.
        if not episodes and arr.kind == "sonarr" and item_id \
                and finding.data.get("season") is not None:
            episodes = self._gaps_in_season(arr, item_id, finding.data["season"])
        by_episode = bool(episodes) and arr.kind == "sonarr"
        if not item_id and not episodes:
            return None
        if is_dry:
            return (dry("action.dry.search_episodes", count=len(episodes))
                    if by_episode else dry("action.dry.search"))
        # Searching a whole series to fill one gap makes Sonarr query every
        # indexer for every episode it already has. Where the finding knows
        # which episodes are missing, ask for those.
        self._note_search(arr, finding)
        if by_episode:
            arr.search_episodes(episodes)
            return done("action.result.search_episodes", count=len(episodes))
        arr.search([item_id])
        return done("action.result.search_started")

    def _note_search(self, arr: Arr, finding: Finding) -> None:
        """Count one search for this title.

        Whether it helped is not recorded, because it does not have to be: the
        next full pass answers that for free. A title searched for last time
        that is still on the list is a title the search did not help, and after
        a few of those there is a conclusion to draw.
        """
        item_id = finding.data.get("item_id")
        if not item_id:
            return
        key = rules_searching_for(arr, finding.rule, item_id,
                                  finding.data.get("season"))
        try:
            # Only counts as a new try once enough time has passed since the
            # last one. See rules.SEARCH_GAP_HOURS.
            self.store.note_attempt(key, gap_hours=rules_gap_hours)
        except Exception:                                       # noqa: BLE001
            log.exception("Could not count the search for %s", key)

    def _act_refresh(self, arr: Arr, finding: Finding, cfg: dict,
                     is_dry: bool) -> policy.Outcome | None:
        item_id = finding.data.get("item_id")
        if not item_id:
            return None
        if is_dry:
            return dry("action.dry.refresh")
        name = compat.command_for(arr.kind, "refresh")
        if not name:
            return None
        if arr.kind == "radarr":
            arr.command(name, movieIds=[item_id])
        else:
            arr.command(name, seriesId=item_id)
        return done("action.result.rescanned")

    def _act_import(self, arr: Arr, finding: Finding, cfg: dict,
                    is_dry: bool) -> policy.Outcome | None:
        if finding.rule == "unmatched_files":
            return self._import_match(arr, finding, is_dry, clean=False)
        if finding.rule == "stuck_in_downloader":
            return self._import_folder(arr, finding, is_dry)
        return self._import_waiting(arr, finding, is_dry)

    def _act_as_suggested(self, arr: Arr, finding: Finding, cfg: dict,
                          is_dry: bool) -> policy.Outcome | None:
        """Do what this one finding says is right for it.

        Resolved here, at the last moment, rather than when the rule is set up:
        the same rule finds a download that only wants importing and one that
        can only be replaced, and one fixed answer for both would be wrong for
        one of them. A suggestion outside what the rule may do is ignored —
        a stored finding is data, and data does not widen what a rule is
        allowed to do.
        """
        chosen = finding.data.get("suggested")
        rule = next((r for r in ALL if r.name == finding.rule), None)
        if (rule is None or not chosen or chosen not in rule.actions
                or chosen in (policy.REPORT, policy.AS_SUGGESTED)):
            return None
        outcome = self._perform(chosen, arr, finding, cfg)
        if outcome is None and is_dry:
            return dry("action.dry.as_suggested")
        return outcome

    def _act_import_and_clean(self, arr: Arr, finding: Finding, cfg: dict,
                              is_dry: bool) -> policy.Outcome | None:
        if finding.rule == "unmatched_files":
            return self._import_match(arr, finding, is_dry, clean=True)
        return self._import_waiting(arr, finding, is_dry)

    def _act_delete(self, arr: Arr, finding: Finding, cfg: dict,
                    is_dry: bool) -> policy.Outcome | None:
        return self._delete_path(finding, cfg, is_dry)

    def _act_clear_warning(self, arr: Arr, finding: Finding, cfg: dict,
                           is_dry: bool) -> policy.Outcome | None:
        if is_dry:
            return dry("action.dry.clear_warning")
        return self._on_client(finding, lambda c: (
            c.clear_warnings(), done("action.result.warning_cleared"))[1])

    def _act_remove_entry(self, arr: Arr, finding: Finding, cfg: dict,
                          is_dry: bool) -> policy.Outcome | None:
        if not finding.data.get("nzo_id"):
            return None
        if is_dry:
            return dry("action.dry.remove_entry")
        gb = finding.data.get("gb")
        return self._on_client(finding, lambda c: (
            c.delete_history_entry(finding.data["nzo_id"], with_files=True),
            done("action.result.entry_removed", gb=gb))[1])

    def _act_unblocklist(self, arr: Arr, finding: Finding, cfg: dict,
                         is_dry: bool) -> policy.Outcome | None:
        """Let a release that was refused long ago be tried again.

        Removing the entry on its own changes nothing — the blocklist is only
        consulted when something goes looking — so a search follows it. Without
        that, the action would appear to work and the title would stay exactly
        as missing as it was.
        """
        entry_id = finding.data.get("blocklist_id")
        if not entry_id:
            return None
        if is_dry:
            return dry("action.dry.unblocklist")
        arr.remove_from_blocklist(int(entry_id))
        item_id = finding.data.get("item_id")
        if item_id:
            arr.search([item_id])
            return done("action.result.unblocklisted_searched")
        return done("action.result.unblocklisted")

    def _act_retune_profile(self, arr: Arr, finding: Finding, cfg: dict,
                            is_dry: bool) -> policy.Outcome | None:
        """Stop a quality profile upgrading on score.

        Sets *upgrade until custom format score* to zero, which the services
        document as "do not upgrade on score at all". The scores go on choosing
        the best release when something is grabbed; resolution upgrades go on
        happening. What stops is the service deciding that a file it has is not
        good enough because of a number it can never reach.

        A cutoff that points at a disallowed quality is moved to the best
        quality the profile does allow. The floor is not touched: that decides
        what is grabbed at all, and is somebody's taste rather than a fault.
        """
        pid = finding.data.get("profile_id")
        if not pid:
            return None
        if is_dry:
            return dry("action.dry.retune_profile")
        profile = arr.quality_profile(int(pid))
        if not profile:
            return None
        body = dict(profile)
        changed = []
        if int(body.get("cutoffFormatScore") or 0) > 0:
            body["cutoffFormatScore"] = 0
            changed.append("target")
        allowed = rules_allowed_ids(body)
        if allowed and body.get("cutoff") not in allowed:
            # The last allowed rung in the list is the best one.
            best = None
            for entry in body.get("items") or []:
                ident = ((entry.get("quality") or {}).get("id")
                         if entry.get("quality") else entry.get("id"))
                if ident in allowed:
                    best = ident
            if best is not None:
                body["cutoff"] = best
                changed.append("cutoff")
        if not body.get("minUpgradeFormatScore"):
            body["minUpgradeFormatScore"] = 1
        if not changed:
            return None
        arr.save_quality_profile(body, int(pid))
        # Remembered, so that finding it set back later can be told apart
        # from finding it for the first time.
        self.store.note_attempt(rules_retune_key(arr, pid))
        return done("action.result.retuned", profile=str(profile.get("name")))

    def _act_resume(self, arr: Arr, finding: Finding, cfg: dict,
                    is_dry: bool) -> policy.Outcome | None:
        # A finding about one job resumes that job and nothing else. Resuming
        # the whole client because one job in it was paused would overrule
        # every other pause somebody made on purpose.
        job = finding.data.get("nzo_id") if finding.rule == "stuck_in_downloader" else None
        if is_dry:
            return dry("action.dry.resume_job") if job else dry("action.dry.resume")
        if job:
            return self._on_client(finding, lambda c: (
                c.resume_job(str(job)), done("action.result.job_resumed"))[1])
        return self._on_client(finding, lambda c: (
            c.resume(), done("action.result.resumed"))[1])

    def _blocklist_by_history(self, arr: Arr, finding: Finding, is_dry: bool,
                              search: bool) -> policy.Outcome | None:
        """Blocklist something that is no longer in the queue."""
        release = finding.data.get("release") or finding.title
        item_id = finding.data.get("item_id")
        if is_dry:
            return dry("action.dry.blocklist_and_search" if search
                       else "action.dry.blocklist")
        blocked = self._blocklist_release(arr, release)
        if search and item_id:
            arr.search([item_id])
        self._clear_client_entry(release)
        if not search:
            return done("action.result.blocklisted" if blocked
                        else "action.result.folder_cleared")
        return done("action.result.blocklisted_searched" if blocked
                    else "action.result.folder_cleared_searched")

    def _on_client(self, finding: Finding, action) -> policy.Outcome | None:
        """Run something against the download client that reported this."""
        for entry in self.store.services(enabled_only=True):
            if entry["kind"] == "sabnzbd" and entry["name"] == finding.data.get("client"):
                client = self.downloader(entry)
                try:
                    return action(client)
                except SabError as e:
                    return failed("action.result.client_refused", error=str(e)[:200])
                finally:
                    client.close()
        return None

    def _import_waiting(self, arr: Arr, finding: Finding,
                        is_dry: bool) -> policy.Outcome | None:
        """Import what the service is holding back and waiting on."""
        if is_dry:
            return dry("action.dry.import")
        download_id = finding.data.get("downloadId")
        if not download_id:
            return None
        files, blind = [], 0
        for candidate in arr.import_candidates(download_id=download_id):
            item = candidate.get("movie") or candidate.get("series") or {}
            if not item.get("id"):
                continue
            payload = {"path": candidate["path"], "quality": candidate.get("quality"),
                       "languages": candidate.get("languages") or [],
                       "downloadId": download_id}
            if arr.kind == "radarr":
                payload["movieId"] = item["id"]
            else:
                # Sonarr imports episodes, not series. A file handed over with
                # only a series id is accepted and then quietly imports
                # nothing, so the action reported success on every single run
                # while the file stayed exactly where it was.
                episodes = _episode_ids(candidate)
                if not episodes:
                    blind += 1
                    continue
                payload["seriesId"] = item["id"]
                payload["episodeIds"] = episodes
            files.append(payload)
        if not files:
            return failed("action.result.no_episodes") if blind else None
        arr.manual_import(files)
        if blind:
            return done("action.result.imported_partly",
                        count=len(files), skipped=blind)
        return done("action.result.imported", count=len(files))

    def _import_folder(self, arr: Arr, finding: Finding,
                       is_dry: bool) -> policy.Outcome | None:
        """Import a finished job the service that grabbed it lost track of.

        Asking by download id does not work here — the service answers that
        question only for downloads it is still tracking, and this one it is
        not. So the folder is listed instead, and only files the service
        itself assigns to the title its own grab history names are imported.
        A folder it cannot see, or files it assigns elsewhere, import nothing,
        which is the right answer to both.
        """
        item_id = finding.data.get("item_id")
        path = finding.data.get("path")
        if not item_id or not path:
            return None
        if is_dry:
            return dry("action.dry.import")
        files = []
        for candidate in arr.import_candidates(folder=path):
            item = candidate.get("movie") or candidate.get("series") or {}
            if item.get("id") != item_id or not candidate.get("path"):
                continue
            payload = {"path": candidate["path"], "quality": candidate.get("quality"),
                       "languages": candidate.get("languages") or []}
            if arr.kind == "radarr":
                payload["movieId"] = item_id
            else:
                episodes = _episode_ids(candidate)
                if not episodes:
                    continue
                payload["seriesId"] = item_id
                payload["episodeIds"] = episodes
            files.append(payload)
        if not files:
            return None
        arr.manual_import(files)
        return done("action.result.imported", count=len(files))

    def _import_match(self, arr: Arr, finding: Finding, is_dry: bool,
                      clean: bool) -> policy.Outcome | None:
        """Import a file this build matched to a title itself.

        ``todo`` is set by the check and says what is actually possible for this
        one file. A finding that found no match at all cannot be imported no
        matter which action is chosen — for those the answer is to blocklist,
        which is a separate action the person can pick.
        """
        if finding.data.get("todo") != "import":
            return None
        item_id = finding.data.get("item_id")
        path = finding.data.get("path")
        if not item_id or not path:
            return None
        if is_dry:
            return dry("action.dry.import_and_clean" if clean
                       else "action.dry.import")
        # The folder was listed through whichever service answered first —
        # usually Radarr — and that listing describes the file in that
        # service's words. They are not the same words: Radarr calls a source
        # "webdl" where Sonarr expects "web", and handing Radarr's description
        # to Sonarr is answered with a 500. That happened ninety-six times in a
        # row on one season pack before anybody saw it.
        #
        # So the service about to do the import is asked what *it* makes of the
        # file, and its answer is what goes back to it: quality, languages and,
        # for a series, which episodes are in there.
        own = self._candidate_at(arr, path)
        payload = {"path": path,
                   "quality": (own or {}).get("quality")
                   or (finding.data.get("quality") if arr.kind == "radarr" else None),
                   "languages": (own or {}).get("languages")
                   or finding.data.get("languages") or []}
        if arr.kind == "radarr":
            payload["movieId"] = item_id
        else:
            episodes = _episode_ids(own) if own else []
            if not episodes or not payload["quality"]:
                return failed("action.result.no_episodes")
            payload["seriesId"] = item_id
            payload["episodeIds"] = episodes
        arr.manual_import([payload])
        gb = finding.data.get("gb", 0)
        if not clean:
            return done("action.result.matched_imported", gb=gb)
        # The download client never learns about a manual import — its entry and
        # source folder would stay. Observed on Crank and Transporter, both of
        # which were still in the history afterwards.
        cleared = self._clear_client_entry(finding.title)
        if not cleared:
            return done("action.result.matched_imported", gb=gb)
        return done("action.result.matched_imported_cleaned", gb=gb,
                    client=cleared)

    def _gaps_in_season(self, arr: Arr, series_id: int, season) -> list[int]:
        """The monitored episodes of one season that have no file.

        Searching the series instead would make Sonarr query every indexer for
        every episode it already has — a full season to fill two holes, on
        every indexer, against whatever daily limit they impose.
        """
        try:
            listing = arr.episodes(int(series_id))
        except (ArrError, TypeError, ValueError) as e:
            log.warning("Could not read the episodes of series %s: %s", series_id, e)
            return []
        return [episode["id"] for episode in listing
                if episode.get("seasonNumber") == season
                and episode.get("monitored") and not episode.get("hasFile")
                and episode.get("id")]

    def _candidate_at(self, arr: Arr, path: str) -> dict | None:
        """What this service makes of one file, in its own words."""
        folder = os.path.dirname(path.replace("\\", "/"))
        try:
            candidates = arr.import_candidates(folder=folder)
        except ArrError as e:
            log.warning("Could not look up %s: %s", path, e)
            return None
        wanted = path.replace("\\", "/")
        for candidate in candidates:
            if (candidate.get("path") or "").replace("\\", "/") == wanted:
                return candidate
        return None

    def _delete_path(self, finding: Finding, cfg: dict,
                     is_dry: bool) -> policy.Outcome | None:
        """Delete a file or folder — the only action that cannot be undone."""
        path = finding.data.get("path")
        if not path:
            return None
        if is_dry:
            size = finding.data.get("mb")
            return (dry("action.dry.delete", what=f"{size} MB") if size is not None
                    else dry("action.dry.delete", what=os.path.basename(path)))
        # Last guard immediately before deleting: the path must still sit below
        # a configured directory. Checked twice, on detection and here — minutes
        # can pass between the two.
        allowed = [os.path.realpath(p) for p in (cfg.get("cleanup_paths") or [])]
        real = os.path.realpath(path)
        if not allowed or not any(real.startswith(a + os.sep) for a in allowed):
            return failed("action.result.outside_paths")
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
        except OSError as e:
            return failed("action.result.delete_failed", error=str(e)[:200])
        return done("action.result.deleted", mb=finding.data.get("mb", 0))

    def _blocklist_release(self, arr: Arr, release: str) -> bool:
        """Put a release on the blocklist so it does not come back.

        Not possible through the queue any more — the entry left it long ago. So
        through the history: the grab is recorded there, and that can be marked
        as failed.
        """
        if not release:
            return False
        try:
            for entry in arr.history(500):
                if not compat.event_is(entry, compat.GRABBED):
                    continue
                source = entry.get("sourceTitle") or ""
                if not source:
                    continue
                # Both directions. The recorded source title and the folder on
                # disk differ in either direction depending on who shortened
                # what, and testing only one of them meant a release whose
                # history entry was the shorter of the two was never found —
                # so the blocklist quietly did nothing.
                if (source[:45] == release[:45] or release[:45] in source
                        or source[:45] in release):
                    arr.mark_grab_failed(entry["id"])
                    return True
        except ArrError as e:
            log.warning("Could not blocklist the release: %s", e)
        return False

    def _clear_client_entry(self, name: str) -> str | None:
        """Remove the matching entry and its files from the client history."""
        short = (name or "")[:40]
        if not short:
            return None
        for entry, client in self.downloaders():
            try:
                history = client.history(200)
            except SabError as e:
                log.warning("Could not read the history of %s: %s", entry["name"], e)
                continue
            finally:
                client.close()
            for slot in history:
                slot_name = str(slot.get("name", ""))
                if short in slot_name or slot_name[:40] in name:
                    again = self.downloader(entry)
                    try:
                        again.delete_history_entry(slot["nzo_id"], with_files=True)
                        return f"{entry['name']} entry"
                    except SabError as e:
                        log.warning("Could not delete the entry: %s", e)
                    finally:
                        again.close()
        return None


def _episode_ids(candidate: dict) -> list[int]:
    """The episode ids on an import candidate, under either spelling."""
    out = []
    for episode in (candidate.get("episodes") or []):
        value = episode.get("id") if isinstance(episode, dict) else episode
        if isinstance(value, int):
            out.append(value)
    for value in (candidate.get("episodeIds") or []):
        if isinstance(value, int) and value not in out:
            out.append(value)
    return out
