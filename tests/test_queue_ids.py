"""Queue entries are found again by their download, not by their id.

Radarr 6 hands out a new id for the same queue entry over time. An entry that
had waited two days was found under one id and was listed under another when
its button was pressed; the removal was answered with "not found" while the
entry was still there.
"""
from __future__ import annotations

import pytest

from app import settings as S
from app.arr import GoneError
from app.engine import Engine
from app.rules import Finding
from app.storage import Store


class Queue:
    kind, name, service_id = "radarr", "Radarr", 1

    def __init__(self, records):
        self.records = records
        self.removed = []
        self.asked = 0

    def queue(self):
        self.asked += 1
        return self.records

    def remove_from_queue(self, entry_id, blocklist, search_again):
        self.removed.append((entry_id, blocklist, search_again))


@pytest.fixture
def engine(tmp_path):
    return Engine(Store(tmp_path / "q.db"))


def stuck(entry_id=332146117, download="SABnzbd_nzo_abc", item=599):
    return Finding(rule="stuck_in_queue", severity="warning", service="radarr",
                   entry_id=entry_id, title="Some Film", message="finding.stuck",
                   data={"downloadId": download, "item_id": item})


def config():
    return {**S.defaults(), "dry_run": False}


def test_an_entry_whose_id_changed_is_found_by_its_download(engine):
    arr = Queue([{"id": 1389512447, "downloadId": "SABnzbd_nzo_abc", "movieId": 599}])
    engine._act_blocklist_and_search(arr, stuck(), config(), False)
    assert arr.removed == [(1389512447, True, True)]


def test_an_unchanged_id_is_kept(engine):
    arr = Queue([{"id": 7, "downloadId": "SABnzbd_nzo_abc", "movieId": 1},
                 {"id": 332146117, "downloadId": "sabnzbd_nzo_ABC", "movieId": 599}])
    engine._act_remove(arr, stuck(), config(), False)
    assert arr.removed == [(332146117, False, False)]


def test_of_several_entries_for_one_download_the_titles_own_is_taken(engine):
    arr = Queue([{"id": 10, "downloadId": "SABnzbd_nzo_abc", "movieId": 3},
                 {"id": 11, "downloadId": "SABnzbd_nzo_abc", "movieId": 599}])
    engine._act_blocklist(arr, stuck(entry_id=1), config(), False)
    assert arr.removed == [(11, True, False)]


def test_a_download_that_left_the_queue_is_gone_not_failed(engine):
    arr = Queue([{"id": 5, "downloadId": "SABnzbd_nzo_other", "movieId": 1}])
    with pytest.raises(GoneError):
        engine._act_blocklist_and_search(arr, stuck(), config(), False)
    assert arr.removed == []


def test_without_a_download_id_the_stored_id_is_all_there_is(engine):
    arr = Queue([])
    finding = stuck()
    finding.data.pop("downloadId")
    engine._act_remove(arr, finding, config(), False)
    assert arr.removed == [(332146117, False, False)] and arr.asked == 0


def test_the_queue_is_read_once_for_a_batch(engine):
    arr = Queue([{"id": 1, "downloadId": "a"}, {"id": 2, "downloadId": "b"}])
    engine._fresh_queues.clear()
    engine._act_remove(arr, stuck(entry_id=9, download="a"), config(), False)
    engine._act_remove(arr, stuck(entry_id=9, download="b"), config(), False)
    assert arr.asked == 1
    assert [r[0] for r in arr.removed] == [1, 2]
