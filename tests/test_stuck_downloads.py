"""No download may sit stuck unnoticed.

One test per state a download can be stuck in — in the queue of Radarr or
Sonarr, or in SABnzbd — and what becomes of it: which rule speaks for it, what
it suggests, and whether that suggestion is carried out unasked or offered as a
button. The texts are the services' own, word for word where it matters.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app import policy
from app import settings as S
from app.engine import Engine
from app.rules import (
    BY_NAME,
    Finding,
    check_manual_import,
    check_not_an_upgrade,
    check_stuck_in_downloader,
    check_stuck_in_queue,
    stuck_cause,
)
from app.storage import Store


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeArr:
    def __init__(self, kind="radarr", service_id=1):
        self.kind = kind
        self.name = kind.capitalize()
        self.service_id = service_id


class ClockStore:
    """A store whose clock says every state has been seen for ``minutes``."""

    def __init__(self, minutes=0.0):
        self.minutes = minutes
        self.seen: dict[str, int] = {}
        self.pruned: set[str] | None = None

    def check_progress(self, key, value):
        self.seen[key] = value
        return self.minutes

    def prune_progress(self, active, prefix=""):
        self.pruned = set(active)


def config(**overrides):
    cfg = S.defaults()
    cfg["rules"] = {}
    cfg.update(overrides)
    return cfg


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def entry(title="The.Film.2018.1080p.BluRay.x264-GRP", *, state="importBlocked",
          status="completed", tracked="warning", messages=(), error=None,
          added_hours=1.0, item_title="The Film", year=2018, item_id=7,
          kind="radarr", **item_extra):
    item = ({"id": item_id, "title": item_title, "year": year, **item_extra}
            if item_id else None)
    row = {
        "id": 41, "title": title, "downloadId": "SABnzbd_nzo_abc",
        "status": status, "trackedDownloadStatus": tracked,
        "trackedDownloadState": state, "size": 8 * 1024 ** 3, "sizeleft": 0,
        "added": ago(added_hours),
        "statusMessages": [{"title": title, "messages": list(messages)}] if messages else [],
    }
    if error:
        row["errorMessage"] = error
    if item is not None:
        row["movie" if kind == "radarr" else "series"] = item
    return row


def stuck(*entries, minutes=0.0, cfg=None, kind="radarr"):
    ctx = {"queue": list(entries), "store": ClockStore(minutes)}
    return check_stuck_in_queue(FakeArr(kind), ctx, cfg or config())


HOURS = 4 * 60   # comfortably past the three-hour default

MATCHED_BY_ID = ("Found matching movie via grab history, but release was "
                 "matched to movie by ID. Manual Import required.")


# ===========================================================================
# Radarr / Sonarr queue
# ===========================================================================
def test_manual_import_with_a_fitting_name_is_left_to_the_manual_import_rule():
    row = entry(messages=[MATCHED_BY_ID], added_hours=48)
    assert len(check_manual_import(FakeArr(), {"queue": [row]}, config())) == 1
    assert stuck(row, minutes=HOURS) == []


def test_matched_by_id_with_a_name_that_does_not_agree_is_reported_with_import_offered():
    # The case that sat for two days: the grab history names the film, the
    # release name does not quite say so, and manual_import rightly declines.
    row = entry("Totally.Different.Name.1080p.WEB-DL", messages=[MATCHED_BY_ID],
                added_hours=48)
    assert check_manual_import(FakeArr(), {"queue": [row]}, config()) == []
    [found] = stuck(row, minutes=0)          # found on the very first pass
    assert found.rule == "stuck_in_queue"
    assert found.data["cause"] == "matched_by_id"
    assert found.data["suggested"] == "import"
    assert found.data["certain"] is False
    assert found.data["hold"] == "policy.needs_a_look"
    assert found.data["messages"] == [MATCHED_BY_ID]
    assert found.data["downloadId"] == "SABnzbd_nzo_abc"
    assert found.data["age_hours"] >= 47
    assert MATCHED_BY_ID in found.describe("en")


def test_a_final_state_that_arrived_minutes_ago_is_not_reported_yet():
    row = entry("Totally.Different.Name.1080p.WEB-DL", messages=[MATCHED_BY_ID],
                added_hours=0.5)
    assert stuck(row, minutes=0) == []


def test_sonarr_says_manual_import_differently_and_is_still_imported():
    row = entry("The.Show.S01E02.1080p.WEB-DL", kind="sonarr", item_title="The Show",
                messages=["Found matching series via grab history, but release was "
                          "matched to series by ID. Automatic import is not possible. "
                          "See the FAQ for details."])
    found = check_manual_import(FakeArr("sonarr"), {"queue": [row]}, config())
    assert len(found) == 1


def test_manual_import_accepts_a_release_named_after_an_alternate_title():
    row = entry("Der.Film.2018.German.DL.1080p.BluRay.x264-GRP",
                messages=[MATCHED_BY_ID], item_title="Completely Other Words",
                alternateTitles=[{"title": "Der Film"}])
    assert len(check_manual_import(FakeArr(), {"queue": [row]}, config())) == 1


def test_not_an_upgrade_in_import_blocked_now_reaches_its_own_rule():
    row = entry(messages=["Not an upgrade for existing movie file(s). Existing "
                          "quality: Bluray-1080p. New Quality WEBDL-1080p."])
    assert len(check_not_an_upgrade(FakeArr(), {"queue": [row]}, config())) == 1
    assert stuck(row, minutes=HOURS) == []


def test_not_a_custom_format_upgrade_is_left_to_the_not_an_upgrade_rule():
    row = entry(state="importPending",
                messages=["Not a Custom Format upgrade for existing movie file(s). "
                          "New: [] (0) do not improve on Existing: [x] (10)"])
    assert len(check_not_an_upgrade(FakeArr(), {"queue": [row]}, config())) == 1
    assert stuck(row, minutes=HOURS) == []


def test_not_an_upgrade_with_its_rule_switched_off_is_reported_but_not_acted_on():
    row = entry(messages=["Not an upgrade for existing movie file(s)."])
    cfg = config(rules={"not_an_upgrade": {"enabled": False}})
    [found] = stuck(row, minutes=HOURS, cfg=cfg)
    assert found.data["suggested"] == "blocklist"
    assert found.data["certain"] is False
    assert found.data["hold"] == "policy.needs_a_look"


def test_a_sample_is_offered_for_blocklisting_but_not_blocklisted_unasked():
    row = entry(messages=["Unable to determine if file is a sample"])
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "sample"
    assert found.data["suggested"] == "blocklist_and_search"
    assert found.data["hold"] == "policy.needs_a_look"


def test_nothing_importable_is_replaced_once_it_has_been_seen_that_long():
    row = entry(messages=["No files found are eligible for import in /downloads/x"],
                added_hours=48)
    # Certain remedies wait for the state to have been *observed*, however
    # long ago the download was added.
    assert stuck(row, minutes=10) == []
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "no_files"
    assert found.data["suggested"] == "blocklist_and_search"
    assert found.data["certain"] is True
    assert "hold" not in found.data


def test_an_entry_already_imported_is_removed():
    row = entry(messages=["Movie file already imported at 2026-09-01"])
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("already_imported", "remove")
    assert found.data["certain"] is True


def test_missing_episodes_offer_importing_what_is_there():
    row = entry("The.Show.S01.1080p.WEB-DL", kind="sonarr", item_title="The Show",
                messages=["One or more episodes expected in this release were not "
                          "imported or missing from the release"])
    [found] = stuck(row, minutes=HOURS, kind="sonarr")
    assert (found.data["cause"], found.data["suggested"]) == ("missing_episodes", "import")
    assert found.data["hold"] == "policy.needs_a_look"


def test_a_tba_episode_title_waits_for_observed_time_only():
    row = entry("The.Show.S01E05.1080p.WEB-DL", kind="sonarr", item_title="The Show",
                messages=["Episode has a TBA title and recently aired"], added_hours=48)
    # Sonarr resolves this itself once the title is known.
    assert stuck(row, minutes=10, kind="sonarr") == []
    [found] = stuck(row, minutes=HOURS, kind="sonarr")
    assert (found.data["cause"], found.data["suggested"]) == ("tba_title", "import")


def test_a_failure_the_service_never_processed_is_blocklisted_and_searched():
    row = entry(state="failedPending", status="failed", tracked="error",
                error="Aborted, cannot be completed - https://sabnzbd.org/not-complete")
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "failed_pending"
    assert found.data["suggested"] == "blocklist_and_search"
    assert found.data["certain"] is True
    assert found.severity == "error"


def test_a_failure_already_processed_is_only_removed():
    row = entry(state="failed", status="failed", tracked="error",
                error="Download failed")
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("failed", "remove")


@pytest.mark.parametrize("text", [
    "Out of retention",
    "Download failed: 312 articles were missing",
    "Unpacking failed, archive requires a password",
    "Repair failed, not enough repair blocks (30 short)",
])
def test_a_dead_download_reported_by_the_client_counts_as_failed(text):
    row = entry(state="downloading", status="warning", tracked="warning", error=text)
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "failed_pending"


def test_a_full_disk_is_not_mistaken_for_a_broken_release():
    row = entry(state="downloading", status="warning", tracked="warning",
                error="Unpacking failed, write error or disk is full?")
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("path_problem", "report")


def test_a_missing_remote_path_mapping_is_reported_without_a_button():
    row = entry(state="importBlocked",
                messages=["[/data/complete/x] is not a valid local path. "
                          "You may need a Remote Path Mapping."])
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("path_problem", "report")
    assert "hold" not in found.data
    assert policy.decide(policy.Policy(policy.AS_SUGGESTED), found).reason == \
        "policy.nothing_safe"


def test_an_unreachable_download_client_is_only_reported():
    row = entry(state="downloading", status="downloadClientUnavailable",
                tracked="warning", messages=["Download client not available"])
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("client_unavailable", "report")


def test_somebody_elses_download_is_left_alone():
    row = entry(state="importBlocked", item_id=None,
                messages=["Download wasn't grabbed by Radarr and not in a category, Skipping."])
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("not_grabbed", "report")


def test_an_unknown_title_without_an_item_has_nothing_to_import_into():
    row = entry(state="importBlocked", item_id=None,
                messages=["Movie title mismatch, automatic import is not possible. "
                          "Manual Import required."])
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "unknown_item"
    assert found.data["suggested"] == "report"


def test_an_import_that_runs_for_hours_is_reported():
    row = entry(state="importing", tracked="ok")
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("importing", "report")


def test_import_pending_for_hours_without_a_reason_offers_an_import():
    row = entry(state="importPending", tracked="ok")
    [found] = stuck(row, minutes=HOURS)
    assert (found.data["cause"], found.data["suggested"]) == ("import_pending", "import")
    assert found.data["hold"] == "policy.needs_a_look"


def test_an_unfamiliar_message_is_still_reported_word_for_word():
    text = "Something entirely new the service started saying"
    row = entry(state="importBlocked", messages=[text])
    [found] = stuck(row, minutes=HOURS)
    assert found.data["cause"] == "other"
    assert found.data["messages"] == [text]
    assert text in found.describe("de")


@pytest.mark.parametrize("state,status", [
    ("downloading", "downloading"), ("downloading", "queued"),
    ("downloading", "delay"), ("downloading", "paused"), ("imported", "completed"),
])
def test_normal_states_are_never_stuck(state, status):
    row = entry(state=state, status=status, tracked="ok")
    assert stuck(row, minutes=HOURS) == []


def test_the_clock_restarts_when_the_state_changes(tmp_path):
    store = Store(tmp_path / "s.db")
    row = entry(state="importPending", tracked="ok")
    ctx = {"queue": [row], "store": store}
    check_stuck_in_queue(FakeArr(), ctx, config())
    key = next(iter(_progress_keys(store)))
    # Pretend it has been sitting there for a day.
    with store._conn() as c:
        c.execute("UPDATE progress SET since=? WHERE key=?", (ago(24), key))
    assert len(check_stuck_in_queue(FakeArr(), ctx, config())) == 1
    # Now the service says something new: that is movement, not a stuck entry.
    row["statusMessages"] = [{"messages": ["Episode has a TBA title and recently aired"]}]
    assert check_stuck_in_queue(FakeArr(), ctx, config()) == []


def test_its_progress_keys_do_not_collide_with_the_stalled_rule(tmp_path):
    store = Store(tmp_path / "s.db")
    ctx = {"queue": [entry(state="importPending", tracked="ok")], "store": store}
    check_stuck_in_queue(FakeArr(), ctx, config())
    # check_stalled prunes everything under "<instance>:" that it did not see.
    store.prune_progress(set(), "1:")
    assert _progress_keys(store)


def _progress_keys(store):
    with store._conn() as c:
        return [r["key"] for r in c.execute("SELECT key FROM progress").fetchall()]


# ===========================================================================
# Acting on the suggestion
# ===========================================================================
def _finding(suggested, certain=True, **data):
    body = {"suggested": suggested, "certain": certain, "release": "x", **data}
    if suggested != policy.REPORT and not certain:
        body["hold"] = "policy.needs_a_look"
    return Finding(rule="stuck_in_queue", severity="warning", title="t",
                   message="finding.stuck.other", entry_id=41, data=body)


def test_a_certain_suggestion_is_carried_out_by_default():
    rule = BY_NAME["stuck_in_queue"]
    assert rule.default_action == policy.AS_SUGGESTED
    verdict = policy.decide(policy.Policy(rule.default_action),
                            _finding("blocklist_and_search"))
    assert verdict.act and verdict.action == policy.AS_SUGGESTED


def test_an_uncertain_suggestion_waits_for_a_person():
    verdict = policy.decide(policy.Policy(policy.AS_SUGGESTED),
                            _finding("import", certain=False))
    assert not verdict.act and verdict.reason == "policy.needs_a_look"


def test_a_fixed_action_is_not_applied_to_a_finding_that_asks_for_another():
    verdict = policy.decide(policy.Policy("blocklist_and_search"), _finding("remove"))
    assert not verdict.act and verdict.reason == "policy.suggests_otherwise"
    assert policy.decide(policy.Policy("remove"), _finding("remove")).act


def test_the_button_offers_the_suggestion_itself():
    rule = BY_NAME["stuck_in_queue"]
    assert Engine.suggested_action(rule, _finding("import", certain=False)) == "import"
    assert Engine.suggested_action(rule, _finding(policy.REPORT)) == policy.REPORT


def test_the_interface_gets_no_button_where_nothing_would_help():
    from app.api.findings import _offers
    rule = BY_NAME["stuck_in_queue"]
    assert _offers(rule, {"suggested": policy.REPORT}) == []
    offers = _offers(rule, {"suggested": "import"})
    assert "import" in offers and policy.AS_SUGGESTED not in offers


class ActingArr(FakeArr):
    def __init__(self, kind="radarr", service_id=1):
        super().__init__(kind, service_id)
        self.removed, self.imported, self.candidates = [], [], []

    def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
        self.removed.append((entry_id, blocklist, search_again))

    def import_candidates(self, download_id=None, folder=None):
        return self.candidates

    def manual_import(self, files):
        self.imported.append(files)


def test_doing_what_it_suggests_carries_out_that_action(tmp_path):
    engine = Engine(Store(tmp_path / "e.db"))
    arr = ActingArr()
    outcome = engine._perform(policy.AS_SUGGESTED, arr, _finding("blocklist_and_search"),
                              {"dry_run": False})
    assert outcome.state == "done"
    assert arr.removed == [(41, True, True)]


def test_a_suggestion_outside_the_rule_is_ignored(tmp_path):
    engine = Engine(Store(tmp_path / "e.db"))
    arr = ActingArr()
    assert engine._perform(policy.AS_SUGGESTED, arr, _finding("delete"),
                           {"dry_run": False}) is None
    assert arr.removed == []


def test_a_dry_run_says_what_it_would_have_done(tmp_path):
    engine = Engine(Store(tmp_path / "e.db"))
    outcome = engine._perform(policy.AS_SUGGESTED, ActingArr(), _finding("remove"),
                              {"dry_run": True})
    assert outcome.state == "dry"


# ===========================================================================
# SABnzbd
# ===========================================================================
def client(slots=(), history=(), paused=False):
    return {"name": "SABnzbd", "id": 3, "status": {"paused": paused},
            "slots": list(slots), "history": list(history)}


def job(status="Paused", labels=(), nzo="SABnzbd_nzo_abc", name="Some.Release.2020"):
    return {"nzo_id": nzo, "filename": name, "status": status,
            "labels": list(labels), "mb": "4096"}


def done_job(status="Failed", nzo="SABnzbd_nzo_abc", name="Some.Release.2020",
             hours=10.0, fail_message="", storage="/downloads/complete/x"):
    return {"nzo_id": nzo, "name": name, "status": status,
            "completed": int(time.time() - hours * 3600) if hours else 0,
            "bytes": 4 * 1024 ** 3, "fail_message": fail_message,
            "storage": storage}


def grabbed(nzo="SABnzbd_nzo_abc", item_id=7, extra=()):
    rows = [{"eventType": "grabbed", "downloadId": nzo, "movieId": item_id,
             "sourceTitle": "Some.Release.2020"}]
    return [{"kind": "radarr", "instance": 1, "rows": rows + list(extra)}]


def sab(*clients, minutes=HOURS, queues=(), history=(), items=()):
    ctx = {"downloaders": list(clients), "all_queues": list(queues),
           "arr_history": list(history), "items": list(items),
           "store": ClockStore(minutes)}
    return check_stuck_in_downloader(FakeArr(), ctx, config())


def test_a_job_paused_on_its_own_offers_resuming_just_that_job():
    [found] = sab(client(slots=[job()]))
    assert (found.data["cause"], found.data["suggested"]) == ("dl_paused", "resume")
    assert found.data["hold"] == "policy.needs_a_look"
    assert found.data["nzo_id"] == "SABnzbd_nzo_abc"
    assert found.data["client"] == "SABnzbd"


def test_jobs_in_a_paused_client_are_left_to_downloader_paused():
    assert sab(client(slots=[job()], paused=True)) == []


def test_a_job_paused_recently_is_not_reported():
    assert sab(client(slots=[job()]), minutes=30) == []


def test_an_encrypted_job_a_service_tracks_is_blocklisted_through_that_service():
    queued = {"id": 55, "downloadId": "SABnzbd_nzo_abc", "_kind": "sonarr",
              "_instance": 2, "series": {"id": 9}}
    [found] = sab(client(slots=[job(labels=["ENCRYPTED"])]), queues=[queued])
    assert (found.data["cause"], found.data["suggested"]) == \
        ("dl_encrypted", "blocklist_and_search")
    assert found.data["certain"] is True
    assert (found.service, found.entry_id, found.data["_instance"]) == ("sonarr", 55, 2)


def test_a_tracked_job_is_found_again_by_its_download_once_its_queue_id_changed(tmp_path):
    """Radarr 6 hands out a new queue id for the same entry over time. A
    button pressed an hour later has to find the entry by its download, the
    way the queue rules' findings are found — not remove a stale id and be
    told the entry is gone while it is still sitting there."""
    queued = {"id": 55, "downloadId": "SABnzbd_nzo_abc", "_kind": "radarr",
              "_instance": 1, "movie": {"id": 7}}
    [found] = sab(client(slots=[job(labels=["ENCRYPTED"])]), queues=[queued])

    class Later(FakeArr):
        def __init__(self):
            super().__init__()
            self.removed = []

        def queue(self):
            return [{"id": 9055, "downloadId": "SABnzbd_nzo_abc", "movieId": 7}]

        def remove_from_queue(self, entry_id, blocklist=True, search_again=True):
            self.removed.append(entry_id)

    arr = Later()
    Engine(Store(tmp_path / "e.db"))._perform(
        "blocklist_and_search", arr, found, {"dry_run": False})
    assert arr.removed == [9055]


def test_an_encrypted_job_nobody_tracks_is_only_reported():
    [found] = sab(client(slots=[job(labels=["ENCRYPTED"])]))
    assert found.data["suggested"] == policy.REPORT


def test_a_job_with_an_unwanted_extension_is_blocklisted():
    queued = {"id": 56, "downloadId": "SABnzbd_nzo_abc", "_kind": "radarr",
              "_instance": 1, "movie": {"id": 7}}
    [found] = sab(client(slots=[job(labels=["UNWANTED"])]), queues=[queued])
    assert (found.data["cause"], found.data["certain"]) == ("dl_unwanted", True)


def test_a_job_that_never_finishes_fetching_is_reported():
    [found] = sab(client(slots=[job(status="Fetching")]))
    assert (found.data["cause"], found.data["suggested"]) == ("dl_fetching", "report")


@pytest.mark.parametrize("status", ["Downloading", "Queued", "Propagating", "Checking"])
def test_ordinary_queue_states_are_not_stuck(status):
    assert sab(client(slots=[job(status=status)])) == []


def test_post_processing_that_takes_hours_is_reported():
    [found] = sab(client(history=[done_job(status="Extracting", hours=0)]))
    assert (found.data["cause"], found.data["suggested"]) == ("dl_postprocessing", "report")


def test_a_failure_the_grabbing_service_never_noticed_is_blocklisted_there():
    failed = done_job(fail_message="Out of retention")
    [found] = sab(client(history=[failed]), history=grabbed())
    assert found.data["cause"] == "dl_failed_unnoticed"
    assert found.data["suggested"] == "blocklist_and_search"
    assert found.data["certain"] is True
    assert (found.service, found.data["item_id"], found.data["_instance"]) == ("radarr", 7, 1)
    assert found.data["release"] == "Some.Release.2020"
    assert "Out of retention" in found.data["messages"]


def test_a_failure_the_service_already_recorded_is_not_stuck():
    extra = [{"eventType": "downloadFailed", "downloadId": "SABnzbd_nzo_abc", "movieId": 7}]
    assert sab(client(history=[done_job()]), history=grabbed(extra=extra)) == []


def test_a_failure_still_on_a_service_queue_is_left_to_the_queue_rule():
    queued = {"id": 55, "downloadId": "SABnzbd_nzo_abc", "_kind": "radarr", "_instance": 1}
    assert sab(client(history=[done_job()]), history=grabbed(), queues=[queued]) == []


def test_a_failure_no_service_grabbed_is_not_ours():
    assert sab(client(history=[done_job()])) == []


def test_a_finished_job_nobody_picked_up_offers_an_import():
    finished = done_job(status="Completed")
    [found] = sab(client(history=[finished]), history=grabbed())
    assert (found.data["cause"], found.data["suggested"]) == ("dl_completed_unclaimed", "import")
    assert found.data["path"] == "/downloads/complete/x"
    assert found.data["hold"] == "policy.needs_a_look"


def test_a_finished_job_the_service_imported_is_not_stuck():
    extra = [{"eventType": "downloadFolderImported", "downloadId": "SABnzbd_nzo_abc"}]
    assert sab(client(history=[done_job(status="Completed")]),
               history=grabbed(extra=extra)) == []


def test_a_finished_job_imported_by_hand_is_not_stuck():
    library = [{"id": 7, "movieFile": {"sceneName": "Some.Release.2020"}}]
    assert sab(client(history=[done_job(status="Completed")]), history=grabbed(),
               items=library) == []


def test_a_job_that_finished_recently_is_not_stuck_yet():
    assert sab(client(history=[done_job(status="Completed", hours=1)]),
               history=grabbed()) == []


def test_resuming_a_stuck_job_resumes_only_that_job(tmp_path, monkeypatch):
    engine = Engine(Store(tmp_path / "e.db"))
    calls = []

    class Client:
        def resume_job(self, nzo):
            calls.append(("job", nzo))

        def resume(self):
            calls.append(("all", None))

    monkeypatch.setattr(engine, "_on_client", lambda finding, action: action(Client()))
    found = Finding(rule="stuck_in_downloader", severity="warning", title="x",
                    message="finding.stuck.dl_paused",
                    data={"client": "SABnzbd", "nzo_id": "SABnzbd_nzo_abc"})
    outcome = engine._perform("resume", FakeArr(), found, {"dry_run": False})
    assert outcome.state == "done"
    assert calls == [("job", "SABnzbd_nzo_abc")]


def test_importing_a_forgotten_job_takes_only_files_for_the_grabbed_title(tmp_path):
    engine = Engine(Store(tmp_path / "e.db"))
    arr = ActingArr()
    arr.candidates = [
        {"path": "/downloads/complete/x/a.mkv", "movie": {"id": 7}, "quality": {}},
        {"path": "/downloads/complete/x/b.mkv", "movie": {"id": 8}, "quality": {}},
    ]
    found = Finding(rule="stuck_in_downloader", severity="warning", title="x",
                    message="finding.stuck.dl_completed_unclaimed",
                    data={"item_id": 7, "path": "/downloads/complete/x"})
    outcome = engine._perform("import", arr, found, {"dry_run": False})
    assert outcome.state == "done"
    [files] = arr.imported
    assert [f["path"] for f in files] == ["/downloads/complete/x/a.mkv"]
    assert files[0]["movieId"] == 7


def test_every_cause_can_be_said_in_every_language():
    from app import i18n
    from app.rules import DOWNLOADER_CAUSES, STUCK_CAUSES
    for code in i18n.AVAILABLE:
        bundle = i18n.bundle(code)
        for cause in (*STUCK_CAUSES, *DOWNLOADER_CAUSES):
            assert f"finding.stuck.{cause}" in bundle, (code, cause)


def test_every_suggestion_is_something_its_rule_may_do():
    from app.rules import DOWNLOADER_CAUSES, STUCK_CAUSES
    for rule, causes in (("stuck_in_queue", STUCK_CAUSES),
                         ("stuck_in_downloader", DOWNLOADER_CAUSES)):
        for cause, (suggested, _certain) in causes.items():
            assert suggested in BY_NAME[rule].actions, (rule, cause)


def test_the_cause_reader_prefers_the_disk_over_the_failure():
    assert stuck_cause({"errorMessage": "Unpacking failed, write error or disk is full?",
                        "status": "warning"}) == "path_problem"
