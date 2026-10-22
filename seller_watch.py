from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Callable, Optional

from monitor import build_event_url, fetch_seller_event_ids, wait_or_stop

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "seller_state.json")

_persist_lock = threading.Lock()


def _load_all() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"sellers": {}}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"sellers": {}}


def load_seller_data(seller_url: str) -> dict:
    with _persist_lock:
        data = _load_all()
        return data.get("sellers", {}).get(seller_url, {"queue": [], "queue_paused": False})


def save_seller_data(seller_url: str, queue: list, queue_paused: bool) -> None:
    with _persist_lock:
        data = _load_all()
        data.setdefault("sellers", {})[seller_url] = {"queue": queue, "queue_paused": queue_paused}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)


class SellerWatchState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.seller_url: Optional[str] = None
        self.poll_interval: int = 300
        self.seen_ids: set = set()
        self.queue: list = []
        self.logs: deque = deque(maxlen=200)
        self.queue_paused: bool = False

    def reset(self, seller_url: str, poll_interval: int) -> None:
        with self.lock:
            self._reset_locked()
            self.seller_url = seller_url
            self.poll_interval = poll_interval
            persisted = load_seller_data(seller_url)
            queue = persisted.get("queue", [])
            for item in queue:
                if item["status"] == "ACTIVE":
                    # No thread is actually processing this anymore (we just
                    # started up) - put it back in line rather than losing it.
                    item["status"] = "QUEUED"
            self.queue = queue
            self.seen_ids = {item["id"] for item in queue}
            self.queue_paused = persisted.get("queue_paused", False)

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": message})

    def _persist_locked(self) -> None:
        save_seller_data(self.seller_url, list(self.queue), self.queue_paused)

    def ingest_ids(self, event_ids: list) -> list:
        """Adds any not-yet-seen event ids to the queue and persists them. Returns the new items."""
        with self.lock:
            new_items = []
            for event_id in event_ids:
                if event_id in self.seen_ids:
                    continue
                self.seen_ids.add(event_id)
                item = {"id": event_id, "url": build_event_url(event_id), "status": "QUEUED"}
                self.queue.append(item)
                new_items.append(item)
            if new_items:
                self._persist_locked()
            return new_items

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

    def is_queue_paused(self) -> bool:
        with self.lock:
            return self.queue_paused

    def set_queue_paused(self, paused: bool) -> None:
        with self.lock:
            self.queue_paused = paused
            self._persist_locked()

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "seller_url": self.seller_url,
                "poll_interval": self.poll_interval,
                "queue": list(self.queue),
                "logs": list(self.logs),
                "running": self.is_running(),
                "queue_paused": self.queue_paused,
            }


def seller_watch_loop(
    state: SellerWatchState,
    on_new_ids: Callable[[], None],
    should_stop: Callable[[], bool],
) -> None:
    """
    Polls the seller's planned-events page until stopped, queueing any newly
    discovered event ids and notifying on_new_ids so the caller can attempt
    to start the next queued job right away.
    """
    state.log(f"Starting seller watch for: {state.seller_url}")
    state.log(f"Checking every {state.poll_interval} seconds...")

    while not should_stop():
        event_ids, message = fetch_seller_event_ids(state.seller_url)
        if message:
            state.log(message)

        new_items = state.ingest_ids(event_ids)
        if new_items:
            state.log(f"Found {len(new_items)} new event(s), added to queue.")
            on_new_ids()

        if should_stop():
            break
        if wait_or_stop(state.poll_interval, should_stop):
            break

    state.log("Seller watch stopped.")
