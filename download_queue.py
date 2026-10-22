from __future__ import annotations

import json
import os
import threading
from typing import Optional

from app_paths import app_support_dir
from live.auth import extract_event_id

_STATE_DIR = app_support_dir()
os.makedirs(_STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(_STATE_DIR, "download_queue.json")

_persist_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"queue": [], "queue_paused": False}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"queue": [], "queue_paused": False}


def _save(queue: list, queue_paused: bool) -> None:
    with _persist_lock:
        with open(STATE_FILE, "w") as f:
            json.dump({"queue": queue, "queue_paused": queue_paused}, f, indent=2)


class QueueState:
    """
    A manually-managed queue of eBay Live event URLs to monitor and
    download, one at a time - replaces the old seller-page auto-discovery
    (see seller_watch.py, no longer wired into the app). Items are only
    ever added explicitly via add(); there's no polling/watch thread here,
    just the queue itself. The existing single-job monitor pipeline
    (app.py: _maybe_start_next_from_queue) pulls the next QUEUED item
    whenever the job slot is free.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        persisted = _load()
        self.queue: list = persisted.get("queue", [])
        for item in self.queue:
            if item["status"] == "ACTIVE":
                # No thread is actually processing this anymore (we just
                # started up) - put it back in line rather than losing it.
                item["status"] = "QUEUED"
        self.queue_paused: bool = persisted.get("queue_paused", False)

    def _persist_locked(self) -> None:
        _save(list(self.queue), self.queue_paused)

    def add(self, url: str) -> dict:
        with self.lock:
            event_id = extract_event_id(url)
            item = {"id": event_id or url, "url": url, "status": "QUEUED"}
            self.queue.append(item)
            self._persist_locked()
            return item

    def pop_next_queued(self) -> Optional[dict]:
        with self.lock:
            for item in self.queue:
                if item["status"] == "QUEUED":
                    item["status"] = "ACTIVE"
                    self._persist_locked()
                    return item
            return None

    def set_item_status(self, item: dict, status: str) -> None:
        with self.lock:
            item["status"] = status
            self._persist_locked()

    def remove(self, item_id: str) -> bool:
        with self.lock:
            before = len(self.queue)
            self.queue = [i for i in self.queue if not (i["id"] == item_id and i["status"] == "QUEUED")]
            removed = len(self.queue) != before
            if removed:
                self._persist_locked()
            return removed

    def retry(self, item_id: str) -> bool:
        """Puts a failed (ERROR) item back in line as QUEUED - e.g. after a
        transient failure like running out of disk space is resolved.
        Caller is responsible for kicking _maybe_start_next_from_queue()
        afterward so it actually starts if the job slot is free."""
        with self.lock:
            for item in self.queue:
                if item["id"] == item_id and item["status"] == "ERROR":
                    item["status"] = "QUEUED"
                    self._persist_locked()
                    return True
            return False

    def clear(self) -> None:
        """Drops every queue entry except one currently ACTIVE (a job in
        progress keeps its row so the running job's later status update -
        DONE/ERROR/SKIPPED - still has somewhere to land)."""
        with self.lock:
            self.queue = [i for i in self.queue if i["status"] == "ACTIVE"]
            self._persist_locked()

    def is_queue_paused(self) -> bool:
        with self.lock:
            return self.queue_paused

    def set_queue_paused(self, paused: bool) -> None:
        with self.lock:
            self.queue_paused = paused
            self._persist_locked()

    def to_dict(self) -> dict:
        with self.lock:
            return {"queue": list(self.queue), "queue_paused": self.queue_paused}
