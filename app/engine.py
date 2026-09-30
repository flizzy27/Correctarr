"""The run: gather state, apply rules, fix, notify, record.

What each action does is in :mod:`app.actions`; this decides when to do it.

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
import json
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from . import compat, notifications, policy, safety
from . import settings as S
from .actions import Actions
from .arr import Arr, ArrError, GoneError
from .i18n import t
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

done, failed = policy.done, policy.failed


class Engine(Actions):
    def __init__(self, store: Store):
        self.store = store
        self.running = False
        self.last_trigger: str | None = None
        self.last_error: str | None = None
        self._gate = threading.Lock()
        self._runs_since_housekeeping = 0
        # The queue as it is right now, per open connection, for actions
        # that have to find their entry again. See _queue_id.
        self._fresh_queues: dict[int, dict] = {}

    # -- services --------------------------------------------------------------
    #: The services whose certificate is accepted without being checked. A
    #: list of ids under one setting rather than a column of the service
    #: table: it is the exception, off unless somebody switches it on.
    UNVERIFIED = "tls_unverified"

    def verifies(self, service: dict) -> bool:
        """Is this service's certificate checked? Yes, unless switched off."""
        unverified = self.store.get(self.UNVERIFIED, []) or []
        return int(service.get("id") or 0) not in {int(i) for i in unverified}

    def arr_services(self) -> list[Arr]:
        return [Arr(s["kind"], s["url"], s["api_key"], name=s["name"],
                    service_id=s["id"], verify=self.verifies(s))
                for s in self.store.services(enabled_only=True)
                if s["kind"] in ("radarr", "sonarr")]

    def downloader(self, s: dict) -> Sab:
        return Sab(s["url"], s["api_key"], name=s["name"], verify=self.verifies(s))

    def downloaders(self) -> list[tuple[dict, Sab]]:
        return [(s, self.downloader(s))
                for s in self.store.services(enabled_only=True)
                if s["kind"] == "sabnzbd"]

    def indexer_managers(self) -> list[tuple[dict, Prowlarr]]:
        return [(s, Prowlarr(s["url"], s["api_key"], name=s["name"],
                             verify=self.verifies(s)))
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
            # A rule's own condition defaults apply only where nothing has
            # been decided. Somebody who has set a condition, including back
            # to zero, has decided; a later build must not quietly put its own
            # number back.
            starting = {key: value for key, value in rule.default_conditions.items()
                        if key not in saved}
            chosen = policy.parse({**starting, **saved}, rule.actions,
                                  rule.default_action, rule.conditions)
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
            "files": [],
            "missing": [],
            "below_cutoff": [],
            "blocklist": [],
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
        if enabled("disk_space") or (deep and enabled("stray_files")):
            safe("root folders", arr.root_folders, "root_folders")
        if enabled("grab_loop") or (deep and enabled("profile_loop")):
            safe("history", lambda: arr.history(500), "history")
        if deep and enabled("missing_items"):
            safe("missing items", arr.missing, "missing")
        if deep and enabled("cutoff_unmet"):
            safe("titles below the cutoff", arr.below_cutoff, "below_cutoff")

        library_rules = ("missing_audio_language", "unreadable_file",
                         "below_profile", "grab_loop", "wrong_title",
                         "season_gaps", "series_incomplete", "stale_blocklist",
                         "cutoff_unmet", "profile_loop", "stray_files")
        if deep and any(enabled(n) for n in library_rules):
            safe("item list", arr.items, "items")

        if deep and enabled("stale_blocklist") and ctx["items"]:
            safe("blocklist", arr.blocklist, "blocklist")

        # Only Sonarr, and only when something is going to read them. A movie
        # carries its file in the list above; a series carries none of them, so
        # the files are a call of their own — one request per thirty series,
        # which is worth paying for a rule that is actually switched on and
        # nothing else.
        #
        # Deliberately not listed here: downloader_stale_entry. It runs once
        # per pass against the shared state, not against this one, so a fetch
        # made here would be paid for and never read. Matching a finished
        # download against episode files would mean fetching them for every
        # series on every deep pass, and that rule is on by default.
        needs_files = any(enabled(n) for n in ("missing_audio_language",
                                               "unreadable_file", "below_profile",
                                               "stray_files"))
        if deep and arr.kind == "sonarr" and needs_files and ctx["items"]:
            ids = [i["id"] for i in ctx["items"] if i.get("id")]
            safe("episode files", lambda: arr.files(ids), "files")
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
            "arr_history": [],
            "store": self.store,
        }

        # The rule that looks for forgotten jobs is a deep-pass rule; nothing
        # it needs is fetched on the fast pass on its behalf.
        stuck = deep and enabled("stuck_in_downloader")
        needs_downloader = stuck or any(enabled(n) for n in (
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
                    slots = client.queue().get("slots") or []
                    ctx["downloaders"].append({
                        "name": entry["name"], "id": entry["id"],
                        "status": client.status(),
                        "warnings": client.warnings() if enabled("downloader_warning") else [],
                        "waiting": len(slots),
                        "slots": slots if stuck else [],
                        "history": (client.history(200)
                                    if enabled("downloader_stale_entry") or stuck
                                    else []),
                    })
                    ctx["downloader_names"] |= client.known_names()
                    ctx["downloader_reachable"] = True
                except SabError as e:
                    log.warning("Could not read %s: %s", entry["name"], e)
                finally:
                    client.close()

        if needs_files or stuck:
            for service in services:
                try:
                    batch = service.queue()
                except ArrError as e:
                    log.warning("Could not read the queue of %s: %s", service.name, e)
                    continue
                # Which service each entry belongs to. A queue id means
                # something only to the service that handed it out, and a job
                # found through the download client has to be acted on there.
                for queued in batch:
                    queued.setdefault("_kind", service.kind)
                    queued.setdefault("_instance", getattr(service, "service_id", None))
                ctx["all_queues"] += batch

        if stuck:
            # Every service's own history: which download ids it grabbed and
            # which it is done with. Memoised per connection for the run, so
            # a deep pass that has already asked pays nothing more.
            ctx["arr_history"] = []
            for service in services:
                try:
                    ctx["arr_history"].append({
                        "kind": service.kind,
                        "instance": getattr(service, "service_id", None),
                        "rows": service.history(500)})
                except ArrError as e:
                    log.warning("Could not read the history of %s: %s", service.name, e)

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
            ctx["indexer_state"] = self._indexer_state(cfg, services)
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
    def _indexer_state(self, cfg: dict,
                       services: list[Arr] | None = None) -> list[dict]:
        """Merge Prowlarr's numbers with the history of the Arr services.

        Prowlarr knows how often an indexer was queried and how often that
        turned into a grab. Radarr and Sonarr know how good the grab was. Only
        together do they add up to a judgement.

        ``services`` are the connections the run already has open. Passing them
        in matters: without it this built a second set of connections and asked
        every service for the same five hundred history rows a second time, on
        every deep pass. Called from outside a run — the indexer page does —
        there is nothing to reuse and it opens its own.
        """
        managers = self.indexer_managers()
        if not managers:
            return []

        borrowed = services is not None
        history: dict[str, list] = collections.defaultdict(list)
        for service in (services if borrowed else self.arr_services()):
            try:
                entries = service.history(500)
            except ArrError as e:
                log.warning("Could not read the history of %s: %s", service.name, e)
                continue
            finally:
                if not borrowed:
                    service.close()
            for entry in entries:
                if not compat.event_is(entry, compat.GRABBED):
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

    # -- acting on one finding, on purpose -------------------------------------
    #: How long to wait for a search to come back before answering anyway. A
    #: search is the one action whose result is worth waiting for: the whole
    #: question is whether anything was found, and the answer arrives in
    #: seconds. Past this it is reported as still running rather than as a
    #: failure, because it usually is.
    SEARCH_PATIENCE = 25.0

    def act_many(self, rows: list[dict], action: str | None,
                 language: str = "en") -> dict:
        """Do the same to a list of findings, opening the services once.

        The point is the "once". Acting on forty findings one call at a time
        meant contacting every service forty times over just to find out it was
        still there — on an unreachable one that is forty timeouts in a row,
        and the page waits through all of them.

        A search is not waited on here either. One search is worth twenty
        seconds of somebody's attention; forty of them is not, and the answer
        arrives in the findings list on the next pass regardless.
        """
        services = self._reachable_services()
        self._fresh_queues.clear()
        results, done_count = [], 0
        try:
            for row in rows:
                try:
                    answer = self._act_one(row, action, services, language,
                                           verify=False)
                    done_count += 1 if answer["state"] == "done" else 0
                except ValueError as e:
                    answer = {"state": "failed", "result": str(e),
                              "action": action or ""}
                except ArrError as e:
                    answer = {"state": "failed", "result": str(e)[:200],
                              "action": action or ""}
                results.append({"id": row.get("id"), **answer})
        finally:
            for arr in services:
                arr.close()
        return {"ok": True, "done": done_count,
                "failed": len(results) - done_count, "results": results}

    def _reachable_services(self) -> list[Arr]:
        services = []
        for arr in self.arr_services():
            ok, _info = arr.reachable()
            if ok:
                services.append(arr)
            else:
                arr.close()
        return services

    def act_now(self, row: dict, action: str | None,
                language: str = "en") -> dict:
        """Carry out one action on one recorded finding, because somebody asked.

        This is not the scheduled pass and it does not obey the dry run. The
        dry run is a guard against changes nobody asked for; a button is the
        opposite of that, and a button that quietly does nothing because of a
        setting on another page is worse than no button. What it does obey is
        what the rule is *allowed* to do: an action outside that list is
        refused here exactly as it is refused when it is configured.
        """
        services = self._reachable_services()
        self._fresh_queues.clear()
        try:
            return self._act_one(row, action, services, language, verify=True)
        finally:
            for arr in services:
                arr.close()

    def _act_one(self, row: dict, action: str | None, services: list[Arr],
                 language: str, *, verify: bool) -> dict:
        """One finding, against connections somebody else opened and closes."""
        finding = _from_row(row)
        rule = next((r for r in ALL if r.name == finding.rule), None)
        if rule is None:
            raise ValueError("error.no_such_rule")
        current = self.config()
        chosen = action or self.suggestion_for(rule, finding, current)
        if chosen not in rule.actions:
            raise ValueError("error.action_not_allowed")
        if chosen == policy.REPORT:
            raise ValueError("error.nothing_to_do")

        cfg = {**current, "dry_run": False}
        target = self._target_for(finding, services, self._lead_service(services))
        if target is None:
            raise ValueError("error.no_services")

        started = time.time()
        try:
            outcome = self._perform(chosen, target, finding, cfg)
        except GoneError:
            outcome = done("action.result.already_gone")
        except ArrError as e:
            outcome = failed("action.result.service_refused", error=str(e)[:200])
        except Exception as e:                              # noqa: BLE001
            log.exception("Acting on %s by hand failed", finding.rule)
            outcome = failed("action.result.service_refused", error=str(e)[:200])

        if outcome is None:
            raise ValueError("error.nothing_to_do")

        answer = {"ok": outcome.state != "failed", "action": chosen,
                  "state": outcome.state,
                  "result": outcome.text(language),
                  "outcome": outcome.as_dict()}
        if verify and outcome.state == "done" and chosen in (
                "search", "blocklist_and_search", "unblocklist"):
            answer["found"] = self._what_the_search_found(
                target, finding, started, language)

        # Never blocked by the safety guards — a person pressing a button is
        # the opposite of a runaway — but counted, so that a title somebody
        # has already dealt with three times today is not taken up again
        # automatically on top of that.
        safety.Guard(self.store).note(finding, chosen, by_hand=True,
                                      state=outcome.state)

        data = {**finding.data, "_action": outcome.as_dict(), "_by_hand": True,
                "_acted_at": datetime.now(UTC).isoformat()}
        data.pop("_held", None)
        data.pop("_held_params", None)
        self.store.note_action(int(row["id"]), outcome.text("en"), data)
        return answer

    @staticmethod
    def suggested_action(rule, finding: Finding, configured: str | None = None) -> str:
        """What to offer when nobody has said which action they want.

        The finding itself first, when it names one: a rule that has looked at
        this particular case can know better than its own setting — a pack it
        is only half sure about is better blocklisted than searched for again.
        It only counts when the rule is allowed to do it; a suggestion from
        outside that list is ignored rather than offered.

        Then what the rule is set to do, then what it does out of the box —
        that is what it would do on its own, so doing it now is no surprise. A
        rule left on "report only" still has something worth offering, and it
        is the first thing it can do.
        """
        # A finding that names its own remedy is taken at its word — within
        # what the rule may do. That includes "nothing": a download client that
        # is not answering gets no button, because no button would help. The
        # stuck-download rules write ``suggested``; others ``suggested_action``.
        data = getattr(finding, "data", None) or {}
        own = data.get("suggested") or data.get("suggested_action")
        if own == policy.REPORT and data.get("suggested") == policy.REPORT:
            return policy.REPORT
        concrete = [a for a in rule.actions
                    if a not in (policy.REPORT, policy.AS_SUGGESTED)]
        for candidate in (own, configured, rule.default_action):
            if candidate and candidate in concrete:
                return str(candidate)
        return concrete[0] if concrete else policy.REPORT

    def suggestion_for(self, rule, finding: Finding, cfg: dict | None = None) -> str:
        """The suggestion, taking this installation's own setting into account."""
        configured = self.rule_policy(cfg or self.config(), rule.name).action
        return self.suggested_action(rule, finding, configured)

    def _what_the_search_found(self, arr: Arr, finding: Finding, started: float,
                               language: str) -> dict:
        """Wait for the search to finish and read what it grabbed.

        The point of pressing the button is the answer to "is there anything
        out there", and that answer exists within seconds — the service asks
        every indexer, decides, and writes what it grabbed into its history.
        Reading that back is the difference between "a search was started" and
        "it found this".
        """
        deadline = started + self.SEARCH_PATIENCE
        # The history is memoised per connection for the length of a run. This
        # is not a run, and the whole point is to see what has just happened.
        arr.forget_history()
        while time.time() < deadline:
            time.sleep(2.0)
            arr.forget_history()
            grabbed = self._grabs_since(arr, finding, started)
            if grabbed:
                return {"state": "grabbed", "releases": grabbed,
                        "message": t("search.grabbed", language,
                                     count=len(grabbed),
                                     release=grabbed[0]["release"])}
        return {"state": "nothing", "releases": [],
                "message": t("search.nothing", language,
                             seconds=int(self.SEARCH_PATIENCE))}

    def _grabs_since(self, arr: Arr, finding: Finding, started: float) -> list[dict]:
        """Anything grabbed for this title since the search was started."""
        item_id = finding.data.get("item_id")
        episodes = {int(e) for e in (finding.data.get("episode_ids") or [])}
        out = []
        try:
            entries = arr.history(100)
        except ArrError as e:
            log.warning("Could not read the history back: %s", e)
            return []
        for entry in entries:
            if not compat.event_is(entry, compat.GRABBED):
                continue
            when = _moment(entry.get("date"))
            if when is None or when < started - 5:
                continue
            belongs = (item_id and (entry.get("movieId") == item_id
                                    or entry.get("seriesId") == item_id))
            if episodes and entry.get("episodeId") in episodes:
                belongs = True
            if not belongs:
                continue
            out.append({"release": str(entry.get("sourceTitle") or "")[:120],
                        "indexer": str((entry.get("data") or {}).get("indexer") or "")})
        return out

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
        self._fresh_queues.clear()
        guard = self.guard(cfg)

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
            handled: dict[str, tuple[str, bool, str]] = {}
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
                target = self._target_for(finding, services, lead)
                if target is None:
                    continue

                # A failure is not retried on the next pass. The same import
                # was attempted every minute for an hour and a half and failed
                # ninety-six times in the same way, and nothing about the
                # ninety-sixth attempt was going to be different. The wait
                # doubles with each failure: an outage that clears in minutes
                # is retried soon, a refusal that never will is left alone.
                waiting = self._still_backing_off(finding)
                if waiting:
                    finding.data["_held"] = "policy.failed_recently"
                    finding.data["_held_params"] = {"hours": waiting}
                    continue

                # One download, one action. Sonarr lists a season pack as one
                # queue entry per episode, and removing the first removes the
                # lot — the other twenty-one were each sent again, each
                # answered with "not found", and each one counted.
                download = _download_key(finding)
                if download and download in handled:
                    action_taken, attempted_then, state_then = handled[download]
                    finding.data["_held"] = "policy.same_download"
                    finding.data["_held_params"] = {}
                    if attempted_then:
                        # Covered by the same action, so it counts for this
                        # episode as well — a replacement grabbed for it is
                        # then recognised as a replacement.
                        guard.note(finding, action_taken, state=state_then)
                    continue

                # The last word, whichever rule this is: how often this title
                # has been acted on, whether it came straight back after being
                # thrown out, and whether automatic actions as a whole are
                # running away. See app/safety.py.
                hold = guard.check(finding, verdict.action)
                if hold is not None:
                    self._hold(finding, hold, cfg)
                    if hold.tripped:
                        self._announce_pause(guard, cfg)
                    continue

                # Counted as attempted whether or not the service says it
                # worked: an entry answered with "not found" had been marked
                # as failed in the service's own history at the same second,
                # eight times in four minutes.
                attempted = True
                try:
                    outcome = self._perform(verdict.action, target, finding, cfg)
                    attempted = outcome is not None and outcome.state != "dry"
                except GoneError:
                    # It left the queue between being found and being acted
                    # on — imported, or removed by somebody. Nothing went wrong
                    # and nothing is left to do.
                    outcome = None
                    finding.data["_held"] = "policy.already_gone"
                    finding.data["_held_params"] = {}
                except ArrError as e:
                    outcome = failed("action.result.service_refused",
                                     error=str(e)[:200])
                    errors.append(str(e))
                except Exception as e:                          # noqa: BLE001
                    log.exception("Acting on %s failed", finding.rule)
                    outcome = failed("action.result.service_refused",
                                     error=str(e)[:200])
                    errors.append(f"{finding.rule}: {e}")
                state = outcome.state if outcome is not None else ""
                if attempted:
                    guard.note(finding, verdict.action, state=state)
                if download:
                    handled[download] = (verdict.action, attempted, state)
                if outcome is not None and outcome.state == "failed":
                    self._note_failure(finding)
                if outcome is not None:
                    # The English rendering goes in the column that is queried
                    # for the prefixes; the key travels beside it so the same
                    # line can be read back in another language later.
                    finding.action = outcome.text("en")
                    finding.data["_action"] = outcome.as_dict()
                    if outcome.state == "done":
                        fixed += 1
                        self.store.forget_attempt("fail:" + _dedup_key(finding))

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

    #: The first wait after a failure, doubled with each one after, and the
    #: most it is ever allowed to grow to.
    BACKOFF_FIRST_HOURS = 1.0
    BACKOFF_MOST_HOURS = 24.0

    def _still_backing_off(self, finding: Finding) -> float:
        """Hours left before a finding that failed may be acted on again."""
        record = self.store.attempt("fail:" + _dedup_key(finding))
        if not record:
            return 0.0
        tries = int(record.get("tries") or 1)
        wait = min(self.BACKOFF_MOST_HOURS,
                   self.BACKOFF_FIRST_HOURS * 2 ** (tries - 1))
        last = _moment(record.get("last_try"))
        if last is None:
            return 0.0
        left = wait - (time.time() - last) / 3600
        return round(left, 1) if left > 0 else 0.0

    # -- the safety guards -----------------------------------------------------
    def guard(self, cfg: dict | None = None) -> safety.Guard:
        return safety.Guard(self.store,
                            safety.Limits.from_config(cfg or self.config()))

    def _hold(self, finding: Finding, hold: safety.Hold, cfg: dict) -> None:
        """Hold a finding back for safety, and make sure somebody hears of it.

        A finding seen on every pass is only recorded when it is new, and a
        title held because it keeps coming back is exactly the one that is not
        new any more. So being held counts as news of its own — once per
        reporting interval, not on every pass.
        """
        finding.data["_held"] = hold.reason
        finding.data["_held_params"] = hold.params
        try:
            if self.store.is_new("held:" + _dedup_key(finding),
                                 int(cfg.get("recheck_hours", 12))):
                finding.is_new = True
        except Exception:                                       # noqa: BLE001
            log.exception("Could not note the hold on %s", finding.rule)

    def _announce_pause(self, guard: safety.Guard, cfg: dict) -> None:
        """Tell every connection that automatic actions have stopped.

        Past every filter and every cooldown a connection has: somebody who
        set theirs to "errors from the queue rules only" still wants to know
        that nothing is being fixed any more.
        """
        state = guard.paused() or {}
        language = cfg.get("language")
        language = language if language in ("en", "de") else "en"
        notice = Finding(
            rule=safety.NOTICE_RULE, severity="error",
            title=t(str(state.get("reason") or "safety.too_many"), language,
                    **(state.get("params") or {})),
            message=str(state.get("reason") or "safety.too_many"),
            params=dict(state.get("params") or {}), service="correctarr")
        try:
            notifications.alert(self.notification_targets(), notice,
                                language=language,
                                url=cfg.get("public_url", ""))
        except Exception:                                       # noqa: BLE001
            log.exception("Could not announce the pause")

    def _note_failure(self, finding: Finding) -> None:
        try:
            self.store.note_attempt("fail:" + _dedup_key(finding))
        except Exception:                                       # noqa: BLE001
            log.exception("Could not remember the failure")

    def _target_for(self, finding: Finding, services: list[Arr],
                    lead: Arr | None) -> Arr | None:
        """The service an action on this finding has to be carried out against.

        The instance it came from wins. Two Radarr instances are the same kind
        and share nothing else: queue id 41 names one entry in the first and a
        different entry — or none — in the second, so removing "the finding"
        against the wrong one either does nothing or removes something nobody
        asked about.

        A finding may name a different kind than the service that produced it:
        an orphaned file listed through Radarr can belong to a series. Those
        fall back to the first service of the kind they name.
        """
        instance = finding.data.get("_instance")
        if instance is not None:
            exact = next((s for s in services if s.service_id == instance), None)
            if exact is not None and exact.kind == finding.service:
                return exact
        return next((s for s in services if s.kind == finding.service), lead)

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
        queue = {entry.get("id"): entry for entry in (ctx.get("queue") or [])
                 if isinstance(entry, dict)}
        for finding in found:
            # Which connection produced it, so acting on it later comes back
            # here rather than to whichever service happens to be first.
            # A rule that already knows better — a job found through the
            # download client that belongs to another service — has said so.
            finding.data.setdefault("_instance", getattr(arr, "service_id", None))
            # What it is about, for the safety guards. Worked out here, where
            # the queue entry is still at hand: most queue rules do not copy
            # the title's id into their findings, and a guard that relied on
            # every rule remembering to would not be a guard.
            entry = queue.get(finding.entry_id) if finding.entry_id is not None else None
            finding.data["_subject"] = safety.subject(finding, entry)
            if entry and entry.get("downloadId") and not finding.data.get("_download"):
                finding.data["_download"] = entry["downloadId"]
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
            self.store.prune_dismissed(30)
            self.store.prune_sessions()
            # "There is nothing better out there" is true about the world on
            # the day it was decided, and the world gets new releases.
            self.store.prune_attempts(180)
            # Far longer than any window the safety guards count in, and as
            # long as the insights look back.
            self.store.prune_acts()
            self.store.trim_findings(keep=int(cfg.get("log_keep", 20000)),
                                     days=int(cfg.get("log_days", 90)))
        except Exception:                                       # noqa: BLE001
            log.exception("Housekeeping failed")


def _moment(timestamp) -> float | None:
    """A history timestamp as seconds since the epoch."""
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _from_row(row: dict) -> Finding:
    """Rebuild a finding from the row it was written down as.

    Everything an action reads lives in ``data``; the queue id is kept there
    too, under a name of its own, because the column it came from is not part
    of the table.
    """
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    data = data or {}
    entry_id = data.get("_entry_id")
    return Finding(
        rule=str(row.get("rule") or ""), severity=str(row.get("severity") or "info"),
        title=str(row.get("title") or ""), message=str(data.get("_msg") or ""),
        params=dict(data.get("_params") or {}),
        service=str(row.get("service") or "radarr"),
        entry_id=int(entry_id) if isinstance(entry_id, int) else None,
        action=row.get("action"), data=data)


def _download_key(finding: Finding) -> str | None:
    """The download a queue finding belongs to, on the instance it came from."""
    download = finding.data.get("_download") or finding.data.get("downloadId")
    if not download or finding.entry_id is None:
        return None
    return f"{finding.data.get('_instance')}:{download}"


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


def identity(row: dict) -> tuple[str, str]:
    """``(seen key, dismissal key)`` for a finding as it was written down.

    The first is the key every pass touches while the finding is still there,
    so it answers "is this still open". The second adds what the finding says
    about it — its severity and which message — so a dismissal holds while the
    finding stays the same and lapses the moment it says something new. The
    parameters of the message are left out on purpose: "untouched for 12 days"
    becoming "untouched for 13 days" is not news.
    """
    finding = _from_row(row)
    if finding.message:
        seen = _dedup_key(finding)
    else:
        # Rows from before the message key was stored carry only the English
        # sentence, which is what the key was built from in the first place.
        identifier = (finding.data.get("release") or finding.data.get("file")
                      or finding.data.get("path") or finding.data.get("indexer")
                      or finding.data.get("nzo_id")
                      or str(row.get("description") or "")[:120])
        seen = "|".join((finding.service, finding.rule, finding.title,
                         str(identifier)))
    return seen, "|".join((seen, finding.severity, finding.message))


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
        # The queue id travels with the row. Without it a finding written down
        # today can never be acted on tomorrow: the action would have nothing
        # to remove, and the button offering it would be a button that does
        # nothing.
        self.data = {**finding.data, "_msg": finding.message,
                     "_params": finding.params, "_entry_id": finding.entry_id}


def _recordable(finding: Finding) -> _Recordable:
    return _Recordable(finding)
