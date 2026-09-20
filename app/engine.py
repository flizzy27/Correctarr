"""The run: gather state, apply rules, fix, notify, record.

On rule scope
-------------
Rules come in two kinds, and the distinction matters:

``scope="service"``
    Runs per Arr service. The queue rules, for instance — each service has its
    own queue.

``scope="once"``
    Runs once per pass, however many Arr services there are. Anything about the
    download client, the indexers or the filesystem: there is only one of those,
    and checking twice would mean reporting twice.

An earlier build solved this with ``if arr.kind != "radarr": return []``. That
was wrong twice over: with two Radarr instances it ran twice, and on an install
**without** Radarr — Sonarr only — ten of twenty-six rules silently did nothing,
with no sign of it anywhere.
"""
from __future__ import annotations

import collections
import logging
import os
import shutil
import threading
import time
from typing import Any

from . import notifications, policy
from . import settings as S
from .arr import Arr, ArrError
from .indexers import build_views, rate
from .matching import build_candidates
from .prowlarr import Prowlarr, ProwlarrError
from .rules import ALL, Finding
from .sab import Sab, SabError
from .storage import Store

log = logging.getLogger(__name__)

# How often housekeeping runs — not every pass, that would be wasteful on a
# 60 second schedule.
HOUSEKEEP_EVERY = 120

DRY_RUN_PREFIX = "DRY RUN"


class Engine:
    def __init__(self, store: Store):
        self.store = store
        self.running = False
        self.last_trigger: str | None = None
        self.last_error: str | None = None
        self._gate = threading.Lock()
        self._runs_since_housekeeping = 0

    # -- services --------------------------------------------------------------
    def arr_services(self) -> list[Arr]:
        return [Arr(s["kind"], s["url"], s["api_key"], name=s["name"])
                for s in self.store.services(enabled_only=True)
                if s["kind"] in ("radarr", "sonarr")]

    def downloaders(self) -> list[tuple[dict, Sab]]:
        return [(s, Sab(s["url"], s["api_key"], name=s["name"]))
                for s in self.store.services(enabled_only=True)
                if s["kind"] == "sabnzbd"]

    def indexer_managers(self) -> list[tuple[dict, Prowlarr]]:
        return [(s, Prowlarr(s["url"], s["api_key"], name=s["name"]))
                for s in self.store.services(enabled_only=True)
                if s["kind"] == "prowlarr"]

    # -- configuration ---------------------------------------------------------
    def config(self) -> dict:
        """Defaults, overridden by whatever is in the database.

        Unknown keys left over from an older build are skipped rather than
        poisoning the configuration.
        """
        cfg = S.defaults()
        for key, value in self.store.all_settings().items():
            if key in S.BY_KEY:
                cfg[key] = value
            elif key != "rules":
                log.debug("Setting %s is unknown and will be ignored", key)

        cfg["rules"] = self.rule_settings()
        cfg["cleanup_paths"] = S.cleanup_paths(cfg)
        return cfg

    def rule_settings(self) -> dict[str, dict]:
        """Every rule's configuration, filled in and ready to be saved again.

        Deliberately plain dictionaries rather than ``Policy`` objects: the same
        shape goes to the interface, back from it, and into the store, and one
        shape that survives a round trip through JSON is worth more than a
        slightly tidier type.
        """
        stored = self.store.get("rules", {}) or {}
        if not isinstance(stored, dict):
            stored = {}
        out: dict[str, dict] = {}
        for rule in ALL:
            saved = stored.get(rule.name)
            saved = saved if isinstance(saved, dict) else {}
            # "fix" is what the previous version stored. Translating it on the
            # way out rather than rewriting the row keeps the upgrade
            # reversible: an older build reading this store still finds what it
            # expects, right up until something is saved here.
            if "action" not in saved and "fix" in saved:
                saved = {**saved,
                         **policy.from_legacy_switch(bool(saved["fix"]),
                                                     rule.default_action)}
            chosen = policy.parse(saved, rule.actions, rule.default_action,
                                  rule.conditions)
            out[rule.name] = {"enabled": bool(saved.get("enabled", True)),
                              **chosen.as_dict()}
        return out

    def rule_enabled(self, cfg: dict, name: str) -> bool:
        return bool((cfg["rules"].get(name) or {}).get("enabled", True))

    def rule_policy(self, cfg: dict, name: str) -> policy.Policy:
        rule = next((r for r in ALL if r.name == name), None)
        if rule is None:
            return policy.Policy()
        return policy.parse(cfg["rules"].get(name) or {}, rule.actions,
                            rule.default_action, rule.conditions)

    def notification_targets(self) -> list[dict]:
        return self.store.notifications(enabled_only=True)

    # -- per service state -----------------------------------------------------
    def _service_context(self, arr: Arr, cfg: dict, deep: bool) -> dict:
        """Everything this service's rules need, fetched in one go.

        Each lookup costs an HTTP request, so only what at least one enabled
        rule actually uses is fetched.
        """
        enabled = lambda name: self.rule_enabled(cfg, name)
        ctx: dict[str, Any] = {
            "queue": arr.queue(),
            "profiles": arr.profiles(),
            "formats": arr.custom_formats(),
            "health": arr.health() if enabled("service_health") else [],
            "items": [],
            "missing": [],
            "disk_space": [],
            "root_folders": [],
            "history": [],
            "store": self.store,
        }

        def safe(label: str, call, target: str) -> None:
            try:
                ctx[target] = call()
            except ArrError as e:
                log.warning("Could not read %s from %s: %s", label, arr.name, e)

        if enabled("disk_space"):
            safe("disk space", arr.disk_space, "disk_space")
            safe("root folders", arr.root_folders, "root_folders")
        if enabled("grab_loop"):
            safe("history", lambda: arr.history(500), "history")
        if deep and enabled("missing_items"):
            safe("missing items", arr.missing, "missing")
        if deep and any(enabled(n) for n in ("missing_audio_language", "unreadable_file",
                                             "below_profile", "grab_loop", "wrong_title")):
            safe("item list", arr.items, "items")
        return ctx

    # -- shared state ----------------------------------------------------------
    def _shared_context(self, services: list[Arr], cfg: dict, deep: bool) -> dict:
        """What exists only once: download client, filesystem, indexers."""
        enabled = lambda name: self.rule_enabled(cfg, name)
        ctx: dict[str, Any] = {
            "import_candidates": [],
            "items": [],
            "history": [],
            "profiles": [],
            "formats": [],
            "downloader_names": set(),
            "downloader_reachable": None,
            "downloaders": [],
            "indexer_state": [],
            "match_candidates": None,
            "all_queues": [],
            "store": self.store,
        }

        needs_downloader = any(enabled(n) for n in (
            "downloader_warning", "downloader_paused", "downloader_disk_space",
            "downloader_update", "downloader_stale_entry", "leftover_files",
            "unpack_failed", "unmatched_files"))
        needs_files = any(enabled(n) for n in (
            "leftover_files", "unpack_failed", "unmatched_files", "detached_folder"))

        if needs_downloader:
            clients = self.downloaders()
            if clients:
                ctx["downloader_reachable"] = False
            for entry, client in clients:
                try:
                    ctx["downloaders"].append({
                        "name": entry["name"], "id": entry["id"],
                        "status": client.status(),
                        "warnings": client.warnings() if enabled("downloader_warning") else [],
                        "waiting": len(client.queue().get("slots") or []),
                        "history": (client.history(200)
                                    if enabled("downloader_stale_entry") else []),
                    })
                    ctx["downloader_names"] |= client.known_names()
                    ctx["downloader_reachable"] = True
                except SabError as e:
                    log.warning("Could not read %s: %s", entry["name"], e)
                finally:
                    client.close()

        if needs_files:
            for service in services:
                try:
                    ctx["all_queues"] += service.queue()
                except ArrError as e:
                    log.warning("Could not read the queue of %s: %s", service.name, e)

        # Import candidates come from the first reachable service — they
        # describe the contents of the download folder, and that is the same
        # for everyone.
        lead = self._lead_service(services)
        if needs_files and lead is not None:
            try:
                ctx["import_candidates"] = lead.import_candidates(
                    folder=cfg.get("path_downloads") or "/downloads")
                ctx["profiles"] = lead.profiles()
                ctx["formats"] = lead.custom_formats()
            except ArrError as e:
                log.warning("Could not read the import candidates: %s", e)

        if (enabled("unmatched_files") or enabled("downloader_stale_entry")) and lead:
            try:
                ctx["history"] = lead.history(500)
            except ArrError as e:
                log.warning("Could not read the history: %s", e)
            everything = []
            for service in services:
                try:
                    batch = service.items()
                except ArrError as e:
                    log.warning("Could not read the item list of %s: %s", service.name, e)
                    continue
                # Record where each title came from: a series belongs to Sonarr
                # even when the download folder was listed through Radarr.
                # Without this a movie import lands on the series service.
                for item in batch:
                    item["_kind"] = service.kind
                everything += batch
            if everything:
                ctx["items"] = everything
                ctx["match_candidates"] = build_candidates(everything)

        if deep and any(enabled(n) for n in ("indexer_disabled", "indexer_ineffective",
                                             "indexer_ranking", "indexer_unknown")):
            ctx["indexer_state"] = self._indexer_state(cfg)
        return ctx

    def _lead_service(self, services: list[Arr]) -> Arr | None:
        """The service the shared lookups go through.

        Radarr is preferred because the file rules are built on movies. If there
        is none, Sonarr will do — previously everything simply fell away in that
        case.
        """
        for service in services:
            if service.kind == "radarr":
                return service
        return services[0] if services else None

    # -- indexers --------------------------------------------------------------
    def _indexer_state(self, cfg: dict) -> list[dict]:
        """Merge Prowlarr's numbers with the history of the Arr services.

        Prowlarr knows how often an indexer was queried and how often that
        turned into a grab. Radarr and Sonarr know how good the grab was. Only
        together do they add up to a judgement.
        """
        managers = self.indexer_managers()
        if not managers:
            return []

        history: dict[str, list] = collections.defaultdict(list)
        for service in self.arr_services():
            try:
                entries = service.history(500)
            except ArrError as e:
                log.warning("Could not read the history of %s: %s", service.name, e)
                continue
            finally:
                service.close()
            for entry in entries:
                if entry.get("eventType") not in ("grabbed", 1):
                    continue
                data = entry.get("data") or {}
                name = data.get("indexer")
                if not name:
                    continue
                try:
                    points = int(data.get("customFormatScore")
                                 or entry.get("customFormatScore") or 0)
                except (TypeError, ValueError):
                    points = 0
                try:
                    gb = int(data.get("size") or 0) / 1024 ** 3
                except (TypeError, ValueError):
                    gb = 0.0
                history[name].append((points, gb))

        out = []
        for entry, manager in managers:
            try:
                indexers = manager.indexers()
                stats = manager.stats(int(cfg.get("indexer_days", 30)))
                statuses = manager.indexer_status()
            except ProwlarrError as e:
                log.warning("Could not read %s: %s", entry["name"], e)
                continue
            finally:
                manager.close()
            out.append({"name": entry["name"], "indexers": indexers,
                        "statuses": statuses,
                        "views": rate(build_views(indexers, stats, statuses, history))})
        return out

    # -- acting ----------------------------------------------------------------
    def _perform(self, action: str, arr: Arr, finding: Finding,
                 cfg: dict) -> str | None:
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

    # Every handler returns a sentence saying what it did, or None when there
    # was nothing to do. A result starting with FAILED counts as an error, one
    # starting with the dry run prefix counts as no change.

    def _act_remove(self, arr: Arr, finding: Finding, cfg: dict,
                    dry: bool) -> str | None:
        """Out of the queue, but not blocklisted — it may be grabbed again."""
        if finding.entry_id is None:
            return None
        if dry:
            return f"{DRY_RUN_PREFIX}: would remove from the queue"
        arr.remove_from_queue(finding.entry_id, blocklist=False, search_again=False)
        return "removed from the queue"

    def _act_blocklist(self, arr: Arr, finding: Finding, cfg: dict,
                       dry: bool) -> str | None:
        if finding.entry_id is None:
            return self._blocklist_by_history(arr, finding, dry, search=False)
        if dry:
            return f"{DRY_RUN_PREFIX}: would blocklist"
        arr.remove_from_queue(finding.entry_id, blocklist=True, search_again=False)
        return "blocklisted"

    def _act_blocklist_and_search(self, arr: Arr, finding: Finding, cfg: dict,
                                  dry: bool) -> str | None:
        # A finding that was never in the queue has no entry to remove — an
        # unmatched file, for instance, left it long ago. The grab is still in
        # the history though, and marking that failed has the same effect.
        if finding.entry_id is None:
            return self._blocklist_by_history(arr, finding, dry, search=True)
        if dry:
            return f"{DRY_RUN_PREFIX}: would blocklist and search again"
        arr.remove_from_queue(finding.entry_id, blocklist=True, search_again=True)
        return "blocklisted, new search started"

    def _act_search(self, arr: Arr, finding: Finding, cfg: dict,
                    dry: bool) -> str | None:
        item_id = finding.data.get("item_id")
        if not item_id:
            return None
        if dry:
            return f"{DRY_RUN_PREFIX}: would search again"
        arr.search([item_id])
        return "search for a replacement started"

    def _act_refresh(self, arr: Arr, finding: Finding, cfg: dict,
                     dry: bool) -> str | None:
        item_id = finding.data.get("item_id")
        if not item_id:
            return None
        if dry:
            return f"{DRY_RUN_PREFIX}: would rescan"
        if arr.kind == "radarr":
            arr.command("RefreshMovie", movieIds=[item_id])
        else:
            arr.command("RefreshSeries", seriesId=item_id)
        return "rescanned"

    def _act_import(self, arr: Arr, finding: Finding, cfg: dict,
                    dry: bool) -> str | None:
        if finding.rule == "unmatched_files":
            return self._import_match(arr, finding, dry, clean=False)
        return self._import_waiting(arr, finding, dry)

    def _act_import_and_clean(self, arr: Arr, finding: Finding, cfg: dict,
                              dry: bool) -> str | None:
        if finding.rule == "unmatched_files":
            return self._import_match(arr, finding, dry, clean=True)
        return self._import_waiting(arr, finding, dry)

    def _act_delete(self, arr: Arr, finding: Finding, cfg: dict,
                    dry: bool) -> str | None:
        return self._delete_path(finding, cfg, dry)

    def _act_clear_warning(self, arr: Arr, finding: Finding, cfg: dict,
                           dry: bool) -> str | None:
        if dry:
            return f"{DRY_RUN_PREFIX}: would clear the warning"
        return self._on_client(finding, lambda c: (c.clear_warnings(),
                                                   "recorded and cleared")[1])

    def _act_remove_entry(self, arr: Arr, finding: Finding, cfg: dict,
                          dry: bool) -> str | None:
        if not finding.data.get("nzo_id"):
            return None
        if dry:
            return f"{DRY_RUN_PREFIX}: would remove the entry and its source folder"
        gb = finding.data.get("gb")
        return self._on_client(finding, lambda c: (
            c.delete_history_entry(finding.data["nzo_id"], with_files=True),
            f"entry and source folder removed ({gb} GB)")[1])

    def _act_resume(self, arr: Arr, finding: Finding, cfg: dict,
                    dry: bool) -> str | None:
        if dry:
            return f"{DRY_RUN_PREFIX}: would resume"
        return self._on_client(finding, lambda c: (c.resume(), "resumed")[1])

    def _blocklist_by_history(self, arr: Arr, finding: Finding, dry: bool,
                              search: bool) -> str | None:
        """Blocklist something that is no longer in the queue."""
        release = finding.data.get("release") or finding.title
        item_id = finding.data.get("item_id")
        if dry:
            return (f"{DRY_RUN_PREFIX}: would blocklist and search again" if search
                    else f"{DRY_RUN_PREFIX}: would blocklist")
        blocked = self._blocklist_release(arr, release)
        if search and item_id:
            arr.search([item_id])
        self._clear_client_entry(release)
        if not search:
            return "blocklisted" if blocked else "source folder cleared"
        return ("blocklisted, new search started" if blocked
                else "source folder cleared, new search started")

    def _on_client(self, finding: Finding, action) -> str | None:
        """Run something against the download client that reported this."""
        for entry in self.store.services(enabled_only=True):
            if entry["kind"] == "sabnzbd" and entry["name"] == finding.data.get("client"):
                client = Sab(entry["url"], entry["api_key"], name=entry["name"])
                try:
                    return action(client)
                except SabError as e:
                    return f"FAILED: {e}"
                finally:
                    client.close()
        return None

    def _import_waiting(self, arr: Arr, finding: Finding,
                        dry: bool) -> str | None:
        """Import what the service is holding back and waiting on."""
        if dry:
            return f"{DRY_RUN_PREFIX}: would import"
        download_id = finding.data.get("downloadId")
        if not download_id:
            return None
        files = []
        for candidate in arr.import_candidates(download_id=download_id):
            item = candidate.get("movie") or candidate.get("series") or {}
            if not item.get("id"):
                continue
            payload = {"path": candidate["path"], "quality": candidate.get("quality"),
                       "languages": candidate.get("languages") or [],
                       "downloadId": download_id}
            payload["movieId" if arr.kind == "radarr" else "seriesId"] = item["id"]
            files.append(payload)
        if not files:
            return None
        arr.manual_import(files)
        return f"{len(files)} file(s) imported"

    def _import_match(self, arr: Arr, finding: Finding, dry: bool,
                      clean: bool) -> str | None:
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
        if dry:
            return (f"{DRY_RUN_PREFIX}: would import as item {item_id}"
                    + (" and clean up afterwards" if clean else ""))
        payload = {"path": path, "quality": finding.data.get("quality"),
                   "languages": finding.data.get("languages") or []}
        payload["movieId" if arr.kind == "radarr" else "seriesId"] = item_id
        arr.manual_import([payload])
        if not clean:
            return f"matched and imported ({finding.data.get('gb', 0)} GB)"
        # The download client never learns about a manual import — its entry and
        # source folder would stay. Observed on Crank and Transporter, both of
        # which were still in the history afterwards.
        cleared = self._clear_client_entry(finding.title)
        return (f"matched and imported ({finding.data.get('gb', 0)} GB)"
                + (f", {cleared} cleaned up" if cleared else ""))

    def _delete_path(self, finding: Finding, cfg: dict,
                     dry: bool) -> str | None:
        """Delete a file or folder — the only action that cannot be undone."""
        path = finding.data.get("path")
        if not path:
            return None
        if dry:
            size = finding.data.get("mb")
            return (f"{DRY_RUN_PREFIX}: would delete {size} MB" if size is not None
                    else f"{DRY_RUN_PREFIX}: would delete {os.path.basename(path)}")
        # Last guard immediately before deleting: the path must still sit below
        # a configured directory. Checked twice, on detection and here — minutes
        # can pass between the two.
        allowed = [os.path.realpath(p) for p in (cfg.get("cleanup_paths") or [])]
        real = os.path.realpath(path)
        if not allowed or not any(real.startswith(a + os.sep) for a in allowed):
            return "FAILED: path lies outside the configured directories"
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
        except OSError as e:
            return f"FAILED to delete: {e}"
        return f"deleted, {finding.data.get('mb', 0)} MB freed"

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
                if entry.get("eventType") not in ("grabbed", 1):
                    continue
                source = entry.get("sourceTitle") or ""
                if source[:45] == release[:45] or release[:45] in source:
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
                    again = Sab(entry["url"], entry["api_key"], name=entry["name"])
                    try:
                        again.delete_history_entry(slot["nzo_id"], with_files=True)
                        return f"{entry['name']} entry"
                    except SabError as e:
                        log.warning("Could not delete the entry: %s", e)
                    finally:
                        again.close()
        return None

    # -- the run ---------------------------------------------------------------
    def run(self, deep: bool | None = None, trigger: str = "schedule") -> dict:
        # Checking and setting have to happen together, otherwise two triggers
        # get through at once — observed as two runs one second apart.
        with self._gate:
            if self.running:
                return {"skipped": "already running"}
            self.running = True
            self.last_trigger = trigger
        started = time.monotonic()
        cfg = self.config()
        deep = bool(deep)

        findings: list[Finding] = []
        fixed = 0
        errors: list[str] = []
        services: list[Arr] = []

        try:
            for arr in self.arr_services():
                ok, info = arr.reachable()
                if not ok:
                    errors.append(f"{arr.name}: {info}")
                    arr.close()
                    continue
                services.append(arr)

            # 1. per service rules
            for arr in services:
                try:
                    ctx = self._service_context(arr, cfg, deep)
                except ArrError as e:
                    errors.append(f"{arr.name}: {e}")
                    continue
                for rule in ALL:
                    if rule.scope != "service":
                        continue
                    if rule.only_kinds and arr.kind not in rule.only_kinds:
                        continue
                    findings += self._apply(rule, arr, ctx, cfg, deep, errors)

            # 2. rules that exist only once per pass
            lead = self._lead_service(services)
            if lead is not None and any(r.scope == "once" for r in ALL):
                try:
                    shared = self._shared_context(services, cfg, deep)
                except Exception as e:                          # noqa: BLE001
                    log.exception("Could not gather the shared state")
                    errors.append(f"shared state: {e}")
                    shared = None
                if shared is not None:
                    for rule in ALL:
                        if rule.scope != "once":
                            continue
                        findings += self._apply(rule, lead, shared, cfg, deep, errors)

            # 3. act
            for finding in findings:
                chosen = self.rule_policy(cfg, finding.rule)
                verdict = policy.decide(chosen, finding)
                if not verdict.act:
                    # Held back rather than hidden: the finding is still
                    # reported, and the reason it was not acted on travels
                    # with it so it is visible why.
                    if verdict.reason and verdict.reason != "policy.report_only":
                        finding.data["_held"] = verdict.reason
                        finding.data["_held_params"] = verdict.params or {}
                    continue
                target = next((s for s in services if s.kind == finding.service), lead)
                if target is None:
                    continue
                try:
                    finding.action = self._perform(verdict.action, target, finding, cfg)
                    if finding.action and not str(finding.action).startswith(
                            (DRY_RUN_PREFIX, "FAILED")):
                        fixed += 1
                except ArrError as e:
                    finding.action = f"FAILED: {e}"
                    errors.append(str(e))
                except Exception as e:                          # noqa: BLE001
                    log.exception("Acting on %s failed", finding.rule)
                    finding.action = f"FAILED: {e}"
                    errors.append(f"{finding.rule}: {e}")

            # 4. record
            for finding in findings:
                if finding.is_new or finding.action:
                    self.store.record(_recordable(finding))

            duration = int((time.monotonic() - started) * 1000)
            self.last_error = "; ".join(errors) if errors else None
            self.store.record_run(duration, len(findings), fixed,
                                  self.last_error, deep, trigger)
            self._housekeep(cfg)

            # 5. notify
            worth_reporting = [f for f in findings if f.is_new or f.action]
            notified: list[dict] = []
            targets = self.notification_targets()
            if targets and worth_reporting:
                language = cfg.get("language")
                notified = notifications.dispatch(
                    targets, worth_reporting,
                    language=language if language in ("en", "de") else "en",
                    url=cfg.get("public_url", ""),
                    dry_run=bool(cfg.get("dry_run")))

            return {"found": len(findings), "new": len(worth_reporting), "fixed": fixed,
                    "duration_ms": duration, "deep": deep, "trigger": trigger,
                    "services": len(services), "errors": errors,
                    "notified": notified,
                    "findings": [f.as_dict() for f in findings]}
        finally:
            for arr in services:
                arr.close()
            self.running = False

    def _apply(self, rule, arr: Arr, ctx: dict, cfg: dict,
               deep: bool, errors: list[str]) -> list[Finding]:
        rule_config = cfg["rules"].get(rule.name) or {}
        if not rule_config.get("enabled", True):
            return []
        if rule.deep and not deep:
            return []
        try:
            found = rule.check(arr, ctx, cfg)
        except Exception as e:                                  # noqa: BLE001
            # A single rule must never abort the whole run.
            log.exception("Rule %s failed", rule.name)
            errors.append(f"{rule.name}: {e}")
            return []
        for finding in found:
            finding.is_new = self.store.is_new(
                _dedup_key(finding), int(cfg.get("recheck_hours", 12)))
        return found

    def _housekeep(self, cfg: dict) -> None:
        """Tidy up — but not on every pass."""
        self._runs_since_housekeeping += 1
        if self._runs_since_housekeeping < HOUSEKEEP_EVERY:
            return
        self._runs_since_housekeeping = 0
        try:
            self.store.prune_seen(30)
            self.store.prune_sessions()
            self.store.trim_findings(keep=int(cfg.get("log_keep", 20000)),
                                     days=int(cfg.get("log_days", 90)))
        except Exception:                                       # noqa: BLE001
            log.exception("Housekeeping failed")


def _dedup_key(finding: Finding) -> str:
    """What makes this finding the same finding as last time.

    Do not report a long-running problem on every pass — it is still fixed,
    just quietly. Most findings carry something that identifies the thing they
    are about; those that do not fall back to the rendered message, because the
    title alone is not enough. Two different health problems reported by the
    same source share a title, and deduplicating on that would hide the second
    one entirely.
    """
    identifier = (finding.data.get("release") or finding.data.get("file")
                  or finding.data.get("path") or finding.data.get("indexer")
                  or finding.data.get("nzo_id"))
    if not identifier:
        identifier = finding.describe("en")[:120]
    return "|".join((finding.service, finding.rule, finding.title, str(identifier)))


class _Recordable:
    """What the store writes: the English rendering plus the key and its
    parameters, so the same finding can be shown in another language later."""

    __slots__ = ("rule", "severity", "title", "description", "service",
                 "action", "data")

    def __init__(self, finding: Finding):
        self.rule = finding.rule
        self.severity = finding.severity
        self.title = finding.title
        self.description = finding.describe("en")
        self.service = finding.service
        self.action = finding.action
        self.data = {**finding.data, "_msg": finding.message,
                     "_params": finding.params}


def _recordable(finding: Finding) -> _Recordable:
    return _Recordable(finding)
