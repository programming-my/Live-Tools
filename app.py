from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from typing import Optional
from urllib.parse import urlparse

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import clips
import hls_segments
import tunnel
from app_paths import app_support_dir
from download_queue import QueueState
from live.auth import extract_event_id
from live.session import EventSession
from monitor import check_event_status, download_replay_with_progress, fetch_event_start_date, monitor_loop
from overlay.pipeline import build_chroma_key_overlay_video, build_standalone_chroma_key_overlay_video, fetch_history_and_timeline
from overlay.timeline import list_distinct_listings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

SETTINGS_PATH = os.path.join(app_support_dir(), "settings.json")


def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_settings(settings: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f)


def _default_downloads_dir() -> str:
    """A packaged app's downloads should land somewhere a user would actually
    look for them (Finder-visible), not buried inside the app bundle/support
    folder."""
    return (
        os.path.join(os.path.expanduser("~/Downloads"), "eBay Live Tools")
        if getattr(sys, "frozen", False)
        else os.path.join(BASE_DIR, "downloads")
    )


_saved_downloads_dir = _load_settings().get("downloads_dir")
DOWNLOADS_DIR = _saved_downloads_dir if _saved_downloads_dir else _default_downloads_dir()
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

DEFAULT_EVENT_POLL_INTERVAL = 15
MAX_DOWNLOAD_RETRIES = 3  # plus the initial attempt = up to 4 tries total

# Bump this on every release posted to GITHUB_REPO's Releases page - compared
# against the latest release tag by /api/check-updates.
APP_VERSION = "1.6.0"
GITHUB_REPO = "programming-my/Live-Tools"


def _parse_version(raw: str) -> Optional[tuple]:
    """'v1.2.0' / '1.2.0' -> (1, 2, 0), so versions compare numerically
    (1.10.0 > 1.9.0) rather than as strings. None if it doesn't parse."""
    cleaned = (raw or "").strip().lstrip("vV")
    if not cleaned:
        return None
    try:
        return tuple(int(part) for part in cleaned.split("."))
    except ValueError:
        return None

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/downloads", StaticFiles(directory=DOWNLOADS_DIR), name="downloads")


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.url: Optional[str] = None
        self.poll_interval: int = DEFAULT_EVENT_POLL_INTERVAL
        self.status: str = "IDLE"
        self.m3u8_url: Optional[str] = None
        self.logs: deque = deque(maxlen=200)
        self.download_progress: Optional[float] = None
        self.download_filename: Optional[str] = None

    def reset(self, url: str, poll_interval: int) -> None:
        with self.lock:
            self._reset_locked()
            self.url = url
            self.poll_interval = poll_interval
            self.status = "WAITING"

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": message})

    def set_status(self, status: str, m3u8_url: Optional[str] = None) -> None:
        with self.lock:
            self.status = status
            if m3u8_url:
                self.m3u8_url = m3u8_url

    def set_progress(self, percent: Optional[float]) -> None:
        with self.lock:
            self.download_progress = percent

    def set_done(self, filename: str) -> None:
        with self.lock:
            self.status = "DONE"
            self.download_filename = filename
            self.download_progress = 100

    def set_error(self) -> None:
        with self.lock:
            self.status = "ERROR"

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "url": self.url,
                "poll_interval": self.poll_interval,
                "status": self.status,
                "m3u8_url": self.m3u8_url,
                "logs": list(self.logs),
                "download_progress": self.download_progress,
                "download_filename": self.download_filename,
                "running": self.is_running(),
            }


def _sidecar_path(filename: str) -> str:
    return os.path.join(DOWNLOADS_DIR, filename + ".json")


FAILED_JOB_LOGS_DIR = os.path.join(app_support_dir(), "logs")


def _persist_failed_job_log(url: str, logs: list) -> None:
    """A failed download's logs otherwise vanish the moment another job
    starts or finishes - state.logs is just an in-memory ring buffer
    (deque(maxlen=200)) that JobState.reset() throws away wholesale on the
    next run. Dumping it to a plain text file here means a failure can still
    be diagnosed after the fact instead of becoming unrecoverable."""
    try:
        os.makedirs(FAILED_JOB_LOGS_DIR, exist_ok=True)
        event_id = extract_event_id(url) or "unknown"
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(FAILED_JOB_LOGS_DIR, f"{timestamp}_{event_id}.log")
        with open(path, "w") as f:
            f.write(f"event_url: {url}\n\n")
            for entry in logs:
                f.write(f"[{entry['ts']}] {entry['msg']}\n")
    except OSError:
        pass


def _cleanup_partial_download(output_path: str) -> None:
    """Removes yt-dlp's own in-progress working files for a download that
    didn't finish (errored out or was stopped) - the final output_path
    itself only ever exists after a *successful* run, so what's actually
    left behind is its "<name>.part" (and, with --concurrent-fragments,
    "<name>.part-FragN.part" per-fragment pieces) and "<name>.ytdl" sidecar.
    Previously only output_path itself was ever removed, which was a no-op
    in practice and left multi-GB orphaned fragments behind indefinitely."""
    candidates = [output_path, output_path + ".part", output_path + ".ytdl"]
    candidates.extend(glob.glob(output_path + ".part-Frag*.part"))
    for path in candidates:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _load_sidecar(filename: str) -> Optional[dict]:
    path = _sidecar_path(filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _find_download_for_event(event_url: str) -> Optional[str]:
    """Reverse lookup for the overlay-export UI (which only takes an event
    URL, not a file) - scans downloads/ (including dated subfolders) for the
    most recently downloaded replay whose sidecar (see _run_job) records this
    same event URL. Returns its downloads-relative filename, or None."""
    canonical = event_url.rstrip("/")
    matches = []
    for root, _dirs, names in os.walk(DOWNLOADS_DIR):
        for name in names:
            # "_overlay.mp4"/"_overlay_item<N>.mp4" (see
            # _try_start_overlay_export) are this feature's own outputs, not
            # replay downloads - skip them regardless of which item's export
            # produced them.
            if not name.lower().endswith(".mp4") or "_overlay" in name.lower():
                continue
            relative_path = os.path.relpath(os.path.join(root, name), DOWNLOADS_DIR)
            sidecar = _load_sidecar(relative_path)
            if sidecar and sidecar.get("event_url", "").rstrip("/") == canonical:
                matches.append((os.path.getmtime(os.path.join(root, name)), relative_path))
    if not matches:
        return None
    matches.sort(key=lambda m: m[0], reverse=True)
    return matches[0][1]


DATE_PREFIX_PATTERN = re.compile(r"^(\d{2}-\d{2}-\d{2}) ")


def _derive_stream_date(source_path: str) -> str:
    """Downloaded replays are named "MM-DD-YY ebay_replay_....mp4" (see
    _run_job) - reuse that same date rather than re-deriving it, falling
    back to the file's own mtime if it's missing for some reason (e.g.
    fetch_event_start_date failed at download time)."""
    match = DATE_PREFIX_PATTERN.match(os.path.basename(source_path))
    if match:
        return match.group(1)
    return time.strftime("%m-%d-%y", time.localtime(os.path.getmtime(source_path)))


def _move_into_dated_folder(source_path: str, output_path: str) -> tuple[str, str]:
    """
    After a successful overlay export, groups the source replay and its new
    overlay video together in downloads/{stream date}/ rather than leaving
    them flat alongside every other download. Also brings the source's
    sidecar JSON along so it stays next to its video. Returns the new
    (source_path, output_path). Each half is only moved if it isn't already
    in the target folder - the source may already be there from an earlier
    export of a different item against this same replay (each gets its own
    output filename - see _try_start_overlay_export - but shares one
    source), in which case only the new output still needs moving.
    """
    date_folder = os.path.join(DOWNLOADS_DIR, _derive_stream_date(source_path))
    os.makedirs(date_folder, exist_ok=True)

    if os.path.dirname(source_path) == date_folder:
        new_source_path = source_path
    else:
        sidecar_src = source_path + ".json"
        new_source_path = os.path.join(date_folder, os.path.basename(source_path))
        shutil.move(source_path, new_source_path)
        if os.path.exists(sidecar_src):
            shutil.move(sidecar_src, new_source_path + ".json")

    if os.path.dirname(output_path) == date_folder:
        new_output_path = output_path
    else:
        new_output_path = os.path.join(date_folder, os.path.basename(output_path))
        shutil.move(output_path, new_output_path)

    return new_source_path, new_output_path


def _move_standalone_output_into_dated_folder(output_path: str) -> str:
    """Standalone overlay exports (no matching downloaded replay - see
    build_standalone_chroma_key_overlay_video) still get grouped into
    downloads/{date}/ for consistency with the regular flow, just with only
    the output file to move (no source replay/sidecar alongside it). There's
    no source filename to read a real stream date from, so this falls back
    straight to _derive_stream_date's mtime-based guess (dated by when the
    export ran, not the original stream)."""
    date_folder = os.path.join(DOWNLOADS_DIR, _derive_stream_date(output_path))
    if os.path.dirname(output_path) == date_folder:
        return output_path

    os.makedirs(date_folder, exist_ok=True)
    new_output_path = os.path.join(date_folder, os.path.basename(output_path))
    shutil.move(output_path, new_output_path)
    return new_output_path


class OverlayExportState:
    """Tracks the single background job that exports a downloaded replay's
    auction overlay as its own standalone chroma-key video (see
    overlay/pipeline.py's build_chroma_key_overlay_video) - same shape/
    single-slot pattern as JobState above."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.filename: Optional[str] = None
        self.event_url: Optional[str] = None
        self.listing_id: Optional[str] = None
        self.status: str = "IDLE"
        # A troubled event's fetch phase (retries, orphaned-event warnings)
        # plus a long render phase can together produce several thousand
        # lines - 500 was small enough that a burst of orphan-event warnings
        # during timeline-building silently evicted the fetch phase's own
        # retry/warning messages before anyone could read them, making a
        # real data-loss warning invisible in practice.
        self.logs: deque = deque(maxlen=5000)
        self.output_filename: Optional[str] = None
        self.progress: Optional[float] = None
        self.time_offset: Optional[float] = None
        self.time_scale: Optional[float] = None

    def reset(
        self, filename: str, event_url: str, listing_id: str,
        time_offset: Optional[float] = None, time_scale: Optional[float] = None,
    ) -> None:
        with self.lock:
            self._reset_locked()
            self.filename = filename
            self.event_url = event_url
            self.listing_id = listing_id
            self.status = "RUNNING"
            self.progress = 0
            self.time_offset = time_offset
            self.time_scale = time_scale

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def clear(self) -> None:
        """Resets back to a blank IDLE state - the UI's "Clear" button, for
        dismissing a finished/errored run's result and log rather than
        leaving them on screen until the next export overwrites them."""
        with self.lock:
            self._reset_locked()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": message})

    def set_progress(self, percent: float) -> None:
        with self.lock:
            self.progress = percent

    def set_done(self, output_filename: str) -> None:
        with self.lock:
            self.status = "DONE"
            self.output_filename = output_filename
            self.progress = 100

    def set_error(self) -> None:
        with self.lock:
            self.status = "ERROR"

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "filename": self.filename,
                "event_url": self.event_url,
                "listing_id": self.listing_id,
                "status": self.status,
                "logs": list(self.logs),
                "output_filename": self.output_filename,
                "progress": self.progress,
                "time_offset": self.time_offset,
                "time_scale": self.time_scale,
                "running": self.is_running(),
            }


class ClipExportState:
    """Tracks the single background job that extracts one or more short clips
    (by start/end timestamp) from an event's replay - either an already
    downloaded file, or directly from the live replay manifest with no
    download at all (see _run_clip_export). Same single-slot pattern as
    OverlayExportState, except one job can produce several output files, so
    each requested clip's own progress/result is tracked individually."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.event_url: Optional[str] = None
        self.status: str = "IDLE"
        self.logs: deque = deque(maxlen=500)
        self.clips: list = []
        self.progress: Optional[float] = None

    def reset(self, event_url: str, clip_requests: list) -> None:
        with self.lock:
            self._reset_locked()
            self.event_url = event_url
            self.status = "RUNNING"
            self.progress = 0
            self.clips = [
                {"start": c["start"], "end": c["end"], "output_filename": None, "status": "PENDING"}
                for c in clip_requests
            ]

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def clear(self) -> None:
        """Resets back to a blank IDLE state - the UI's "Clear" button, for
        dismissing a finished/errored run's results and log rather than
        leaving them on screen until the next export overwrites them."""
        with self.lock:
            self._reset_locked()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": message})

    def set_clip_result(self, index: int, status: str, output_filename: Optional[str] = None) -> None:
        with self.lock:
            self.clips[index]["status"] = status
            self.clips[index]["output_filename"] = output_filename

    def set_progress(self, percent: float) -> None:
        with self.lock:
            self.progress = percent

    def set_done(self) -> None:
        with self.lock:
            self.status = "DONE"
            self.progress = 100

    def set_error(self) -> None:
        with self.lock:
            self.status = "ERROR"

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "event_url": self.event_url,
                "status": self.status,
                "logs": list(self.logs),
                "clips": [dict(c) for c in self.clips],
                "progress": self.progress,
                "running": self.is_running(),
            }


class OverlayItemsState:
    """Tracks the single background job that fetches an event's full
    auction/bid history just to list which items were pinned (by title),
    for the overlay-export UI's "pick which auction to export" dropdown -
    same single-slot pattern as OverlayExportState. The (history, timeline)
    this produces is cached (see _timeline_cache) so a subsequent export of
    any item from this same event reuses it instead of re-fetching."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_event: Optional[threading.Event] = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self.event_url: Optional[str] = None
        self.status: str = "IDLE"
        self.logs: deque = deque(maxlen=5000)
        self.progress: Optional[float] = None
        self.items: list = []

    def reset(self, event_url: str) -> None:
        with self.lock:
            self._reset_locked()
            self.event_url = event_url
            self.status = "RUNNING"
            self.progress = 0

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def clear(self) -> None:
        with self.lock:
            self._reset_locked()

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "msg": message})

    def set_progress(self, percent: float) -> None:
        with self.lock:
            self.progress = percent

    def set_done(self, items: list) -> None:
        with self.lock:
            self.status = "DONE"
            self.items = items
            self.progress = 100

    def set_error(self) -> None:
        with self.lock:
            self.status = "ERROR"

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "event_url": self.event_url,
                "status": self.status,
                "logs": list(self.logs),
                "progress": self.progress,
                "items": self.items,
                "running": self.is_running(),
            }


class TimelineCache:
    """Caches the (history, timeline) from the most recent successful
    "load auctions" job (see OverlayItemsState), keyed by event_url, so
    exporting several different items from the same event only pays for
    the (potentially many-minutes-long, on a troubled event) fetch once.
    Single-slot - a different event_url simply replaces it, same as every
    other piece of state in this app."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.event_url: Optional[str] = None
        self.history: Optional[dict] = None
        self.timeline = None

    def get(self, event_url: str) -> Optional[tuple]:
        with self.lock:
            if self.event_url == event_url and self.history is not None:
                return self.history, self.timeline
            return None

    def set(self, event_url: str, history: dict, timeline) -> None:
        with self.lock:
            self.event_url = event_url
            self.history = history
            self.timeline = timeline


state = JobState()
queue_state = QueueState()
overlay_export_state = OverlayExportState()
overlay_items_state = OverlayItemsState()
clip_export_state = ClipExportState()
timeline_cache = TimelineCache()
job_start_lock = threading.Lock()
overlay_export_start_lock = threading.Lock()
overlay_items_start_lock = threading.Lock()
clip_export_start_lock = threading.Lock()

# "Chat and Auction Monitor" - live overlay/chat sessions, keyed by event id.
# Pure-asyncio (see live/session.py), independent of the threading-based
# state above: each EventSession runs as an asyncio.Task on this process's
# single uvicorn event loop, rather than a dedicated OS thread.
LIVE_EVENTS: dict[str, EventSession] = {}


class SettingsRequest(BaseModel):
    downloads_dir: str


class StartRequest(BaseModel):
    url: str
    poll_interval: int = DEFAULT_EVENT_POLL_INTERVAL


class QueueAddRequest(BaseModel):
    url: str


class OverlayItemsRequest(BaseModel):
    event_url: str


class OverlayExportRequest(BaseModel):
    event_url: str
    listing_id: str
    time_offset: Optional[float] = None
    time_scale: Optional[float] = None


class ClipRequest(BaseModel):
    start: str
    end: str


class ClipExportRequest(BaseModel):
    event_url: str
    clips: list[ClipRequest]


class LiveStartRequest(BaseModel):
    url: str


def _live_canonical_url(event_id: str) -> str:
    return f"https://www.ebay.com/ebaylive/events/{event_id}/stream"


def _live_get_or_start(event_id: str, event_url: str) -> EventSession:
    session = LIVE_EVENTS.get(event_id)
    if session is not None:
        return session
    session = EventSession(event_url, event_id)
    LIVE_EVENTS[event_id] = session
    session.start()
    return session


def _try_start_job(url: str, poll_interval: int, queue_item: Optional[dict] = None) -> bool:
    """Starts a monitoring job if the single job slot is free. Returns whether it started."""
    with job_start_lock:
        if state.is_running():
            return False
        state.reset(url, poll_interval)
        stop_event = threading.Event()
        state.stop_event = stop_event
        thread = threading.Thread(target=_run_job, args=(url, poll_interval, stop_event, queue_item), daemon=True)
        state.thread = thread
        thread.start()
        return True


def _try_start_overlay_items(event_url: str) -> bool:
    """Starts a "load auctions" job if the single slot is free - fetches
    the event's full auction/bid history just to list which items were
    pinned (by title), for the export UI's dropdown. Caches the (history,
    timeline) this produces (see timeline_cache) so a subsequent export of
    any item from this event reuses it instead of re-fetching. Returns
    whether it started."""
    with overlay_items_start_lock:
        if overlay_items_state.is_running():
            return False
        overlay_items_state.reset(event_url)
        stop_event = threading.Event()
        overlay_items_state.stop_event = stop_event
        thread = threading.Thread(target=_run_overlay_items, args=(event_url, stop_event), daemon=True)
        overlay_items_state.thread = thread
        thread.start()
        return True


def _run_overlay_items(event_url: str, stop_event: threading.Event) -> None:
    def on_log(message: str) -> None:
        overlay_items_state.log(message)

    def should_stop() -> bool:
        return stop_event.is_set()

    try:
        history, timeline = fetch_history_and_timeline(
            event_url, on_log=on_log, on_progress=overlay_items_state.set_progress, should_stop=should_stop,
        )
        if should_stop():
            on_log("Stopped by user.")
            overlay_items_state.set_error()
            return
        items = list_distinct_listings(timeline)
        if not items:
            on_log("No pinned auctions found for this event.")
            overlay_items_state.set_error()
            return
        timeline_cache.set(event_url, history, timeline)
        on_log(f"Found {len(items)} auction(s).")
        overlay_items_state.set_done(items)
    except Exception as e:
        overlay_items_state.log(f"Unexpected error: {e}")
        overlay_items_state.set_error()
    finally:
        overlay_items_state.thread = None


def _try_start_overlay_export(
    filename: Optional[str], event_url: str, listing_id: str,
    time_offset: Optional[float] = None, time_scale: Optional[float] = None,
) -> bool:
    """Starts an overlay-export job if the single export slot is free. Returns
    whether it started. filename is the downloads-relative replay to
    calibrate against, when one was found for this event (see
    _find_download_for_event) - if None, the export instead renders directly
    from the event's own data with no source video (see
    build_standalone_chroma_key_overlay_video), and time_offset/time_scale
    are ignored (there's nothing to calibrate against). Exports just
    listing_id's own auction, not the whole event - the output filename
    includes it so exporting a second item for the same event/replay gets
    its own file instead of overwriting the first."""
    with overlay_export_start_lock:
        if overlay_export_state.is_running():
            return False
        item_slug = re.sub(r"[^A-Za-z0-9_-]", "", listing_id) or "item"
        if filename:
            source_path = os.path.join(DOWNLOADS_DIR, filename)
            stem, _ext = os.path.splitext(filename)
            output_path = os.path.join(DOWNLOADS_DIR, f"{stem}_overlay_item{item_slug}.mp4")
        else:
            source_path = None
            event_id = extract_event_id(event_url) or "event"
            output_path = os.path.join(DOWNLOADS_DIR, f"overlay_{event_id}_item{item_slug}_{int(time.time())}.mp4")
        overlay_export_state.reset(filename, event_url, listing_id, time_offset, time_scale)
        stop_event = threading.Event()
        overlay_export_state.stop_event = stop_event
        prefetched = timeline_cache.get(event_url)
        thread = threading.Thread(
            target=_run_overlay_export,
            args=(event_url, source_path, output_path, listing_id, time_offset, time_scale, stop_event, prefetched),
            daemon=True,
        )
        overlay_export_state.thread = thread
        thread.start()
        return True


def _maybe_start_next_from_queue() -> None:
    if queue_state.is_queue_paused():
        return
    item = queue_state.pop_next_queued()
    if item is None:
        return
    started = _try_start_job(item["url"], DEFAULT_EVENT_POLL_INTERVAL, queue_item=item)
    if not started:
        # Lost a race with another start - put it back for the next attempt.
        queue_state.set_item_status(item, "QUEUED")


def _run_job(url: str, poll_interval: int, stop_event: threading.Event, queue_item: Optional[dict] = None) -> None:
    def on_status(status: str, m3u8_url: Optional[str]) -> None:
        state.set_status(status, m3u8_url)

    def on_log(message: str) -> None:
        state.log(message)

    def should_stop() -> bool:
        return stop_event.is_set()

    try:
        try:
            monitor_loop(url, poll_interval, on_status, on_log, should_stop)
        except Exception as e:
            state.log(f"Unexpected monitor error: {e}")
            state.set_error()
            _persist_failed_job_log(url, state.to_dict()["logs"])
            return

        if stop_event.is_set():
            state.log("Monitoring stopped by user.")
            state.set_status("IDLE")
            return

        snapshot = state.to_dict()
        if snapshot["status"] == "ENDED" and snapshot["m3u8_url"]:
            m3u8_url = snapshot["m3u8_url"]
            state.log("Starting download via yt-dlp...")
            state.set_status("DOWNLOADING")
            state.set_progress(0)

            event_date = fetch_event_start_date(url)
            date_prefix = f"{event_date} " if event_date else ""
            filename = f"{date_prefix}ebay_replay_{int(time.time())}.mp4"
            output_path = os.path.join(DOWNLOADS_DIR, filename)

            def on_progress(percent: Optional[float], line: str) -> None:
                if percent is not None:
                    state.set_progress(percent)
                state.log(line)

            success = False
            for attempt in range(MAX_DOWNLOAD_RETRIES + 1):
                if attempt > 0:
                    state.log(f"Retrying download (attempt {attempt + 1}/{MAX_DOWNLOAD_RETRIES + 1})...")
                    state.set_progress(0)

                try:
                    success = download_replay_with_progress(m3u8_url, output_path, on_progress, should_stop)
                except FileNotFoundError:
                    state.log("yt-dlp not found. Install it with 'pip install yt-dlp' inside the venv.")
                    state.set_error()
                    _persist_failed_job_log(url, state.to_dict()["logs"])
                    return

                if success or stop_event.is_set():
                    break
                _cleanup_partial_download(output_path)

            if success:
                state.log(f"Download complete! File saved to: downloads/{filename}")
                state.set_done(filename)
                try:
                    with open(_sidecar_path(filename), "w") as f:
                        json.dump({"event_url": url, "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
                except OSError as e:
                    state.log(f"Could not write metadata sidecar: {e}")
            elif stop_event.is_set():
                state.log("Download stopped by user - partial file removed.")
                state.set_status("IDLE")
                _cleanup_partial_download(output_path)
                return
            else:
                state.log(f"yt-dlp exited with an error after {MAX_DOWNLOAD_RETRIES + 1} attempts.")
                state.set_error()
                _cleanup_partial_download(output_path)
                _persist_failed_job_log(url, state.to_dict()["logs"])
    finally:
        if queue_item is not None:
            final_status = state.to_dict()["status"]
            if final_status == "DONE":
                queue_state.set_item_status(queue_item, "DONE")
            elif final_status == "IDLE":
                # Stopped by the user - pause the whole queue rather than
                # auto-advancing to the next event.
                queue_state.set_item_status(queue_item, "SKIPPED")
                queue_state.set_queue_paused(True)
            else:
                queue_state.set_item_status(queue_item, "ERROR")
        # Clear the thread reference now (we're still inside it) so is_running()
        # reports False and the next queued job can start immediately.
        state.thread = None
        _maybe_start_next_from_queue()


def _run_overlay_export(
    event_url: str, source_path: Optional[str], output_path: str, listing_id: str,
    time_offset: Optional[float] = None, time_scale: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
    prefetched: Optional[tuple] = None,
) -> None:
    def on_log(message: str) -> None:
        overlay_export_state.log(message)

    def should_stop() -> bool:
        return stop_event is not None and stop_event.is_set()

    try:
        if source_path is not None:
            success = build_chroma_key_overlay_video(
                event_url, source_path, output_path, listing_id, on_log=on_log, on_progress=overlay_export_state.set_progress,
                time_offset_override=time_offset, time_scale_override=time_scale, should_stop=should_stop,
                prefetched=prefetched,
            )
        else:
            if prefetched is None:
                on_log("No downloaded replay found for this event - rendering directly from the event's own data instead.")
            success = build_standalone_chroma_key_overlay_video(
                event_url, output_path, listing_id, on_log=on_log, on_progress=overlay_export_state.set_progress,
                should_stop=should_stop, prefetched=prefetched,
            )

        if success:
            try:
                if source_path is not None:
                    _, new_output_path = _move_into_dated_folder(source_path, output_path)
                    on_log(f"Grouped replay + overlay into {os.path.dirname(new_output_path)}")
                else:
                    new_output_path = _move_standalone_output_into_dated_folder(output_path)
                    on_log(f"Saved into {os.path.dirname(new_output_path)}")
            except OSError as e:
                on_log(f"Couldn't move the file into a dated folder ({e}) - leaving it in place.")
                new_output_path = output_path
            overlay_export_state.set_done(os.path.relpath(new_output_path, DOWNLOADS_DIR))
        else:
            overlay_export_state.set_error()
    except Exception as e:
        overlay_export_state.log(f"Unexpected error: {e}")
        overlay_export_state.set_error()
    finally:
        # Clear the thread reference now (we're still inside it) so
        # is_running() reports False and a subsequent export can start.
        overlay_export_state.thread = None


def _try_start_clip_export(event_url: str, clip_requests: list) -> bool:
    """Starts a clip-export job if the single export slot is free. Returns
    whether it started. Each entry in clip_requests is
    {start, end, start_seconds, end_seconds} - start/end are the original
    user-entered strings (kept for display), start_seconds/end_seconds are
    already parsed+validated (by the route handler, before this is called)."""
    with clip_export_start_lock:
        if clip_export_state.is_running():
            return False
        clip_export_state.reset(event_url, clip_requests)
        thread = threading.Thread(target=_run_clip_export, args=(event_url, clip_requests), daemon=True)
        clip_export_state.thread = thread
        thread.start()
        return True


def _clip_output_filename(index: int, clip: dict) -> str:
    return (
        f"clip_{index + 1}_"
        f"{clips.seconds_to_timecode(clip['start_seconds'])}_to_"
        f"{clips.seconds_to_timecode(clip['end_seconds'])}.mp4"
    )


def _run_clip_export(event_url: str, clip_requests: list) -> None:
    def on_log(message: str) -> None:
        clip_export_state.log(message)

    try:
        filename = _find_download_for_event(event_url)
        if filename:
            source = os.path.join(DOWNLOADS_DIR, filename)
            output_dir = os.path.dirname(source) or DOWNLOADS_DIR
            on_log(f"Using already-downloaded replay: {filename}")
        else:
            on_log("No downloaded replay found - checking the event's replay manifest...")
            status, m3u8_url, error_message = check_event_status(event_url)
            if error_message:
                on_log(f"Error checking event status: {error_message}")
            if status != "ENDED" or not m3u8_url:
                raise RuntimeError(
                    "This event hasn't ended yet (or has no replay available) - clips can only be made from a finished event."
                )

            # Try fetching only the segments covering each requested range
            # first - ffmpeg seeking directly into the remote manifest turned
            # out unreliable on real replay CDNs (see hls_segments.py), but a
            # full download of a multi-hour event just to grab one short clip
            # can take tens of minutes. Falls back to a full download only if
            # the fast path isn't usable for this particular event (e.g. an
            # unparseable playlist, or the range spanning a discontinuity).
            on_log("Fetching just the segments covering the requested clip(s)...")
            fast_path_results = []
            fast_path_ok = True
            total_clips = len(clip_requests)
            for index, clip in enumerate(clip_requests):
                output_path = os.path.join(DOWNLOADS_DIR, _clip_output_filename(index, clip))
                on_log(f"Extracting clip {index + 1}/{total_clips} ({clip['start']} to {clip['end']})...")

                def on_segment_progress(fetched: int, total: int) -> None:
                    clip_export_state.set_progress((index + fetched / total) / total_clips * 100)

                success = hls_segments.extract_clip_via_segments(
                    m3u8_url, output_path, clip["start_seconds"], clip["end_seconds"],
                    on_log=on_log, on_progress=on_segment_progress,
                )
                fast_path_results.append((index, output_path))
                if not success:
                    fast_path_ok = False
                    break

            if fast_path_ok:
                for index, output_path in fast_path_results:
                    clip_export_state.set_clip_result(index, "DONE", os.path.relpath(output_path, DOWNLOADS_DIR))
                clip_export_state.set_done()
                return

            on_log("Targeted segment fetch didn't work for this event - falling back to a full download...")
            event_date = fetch_event_start_date(event_url)
            date_prefix = f"{event_date} " if event_date else ""
            filename = f"{date_prefix}ebay_replay_{int(time.time())}.mp4"
            download_path = os.path.join(DOWNLOADS_DIR, filename)

            def on_progress(percent: Optional[float], line: str) -> None:
                on_log(line)
                if percent is not None:
                    clip_export_state.set_progress(percent)

            downloaded = False
            for attempt in range(MAX_DOWNLOAD_RETRIES + 1):
                if attempt > 0:
                    on_log(f"Retrying download (attempt {attempt + 1}/{MAX_DOWNLOAD_RETRIES + 1})...")
                try:
                    downloaded = download_replay_with_progress(m3u8_url, download_path, on_progress)
                except FileNotFoundError:
                    raise RuntimeError("yt-dlp not found. Install it with 'pip install yt-dlp' inside the venv.")
                if downloaded:
                    break
                _cleanup_partial_download(download_path)

            if not downloaded:
                raise RuntimeError(f"Could not download the replay after {MAX_DOWNLOAD_RETRIES + 1} attempts.")

            on_log(f"Download complete: {filename}")
            try:
                with open(_sidecar_path(filename), "w") as f:
                    json.dump({"event_url": event_url, "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
            except OSError as e:
                on_log(f"Could not write metadata sidecar: {e}")

            source = download_path
            output_dir = DOWNLOADS_DIR

        any_failed = False
        for index, clip in enumerate(clip_requests):
            output_path = os.path.join(output_dir, _clip_output_filename(index, clip))
            on_log(f"Extracting clip {index + 1}/{len(clip_requests)} ({clip['start']} to {clip['end']})...")
            success = clips.extract_clip(
                source, output_path, clip["start_seconds"], clip["end_seconds"], on_log=on_log,
            )
            clip_export_state.set_progress((index + 1) / len(clip_requests) * 100)
            if success:
                clip_export_state.set_clip_result(index, "DONE", os.path.relpath(output_path, DOWNLOADS_DIR))
            else:
                clip_export_state.set_clip_result(index, "ERROR")
                any_failed = True

        if any_failed:
            clip_export_state.set_error()
        else:
            clip_export_state.set_done()
    except Exception as e:
        on_log(f"Error: {e}")
        clip_export_state.set_error()
    finally:
        # Clear the thread reference now (we're still inside it) so
        # is_running() reports False and a subsequent export can start.
        clip_export_state.thread = None


@app.on_event("startup")
def _resume_queue_on_startup() -> None:
    """QueueState's own __init__ already reverts any ACTIVE item back to
    QUEUED on load (nothing is actually processing it after a restart - see
    download_queue.py), but nothing was ever kicking the queue back into
    motion afterward - every other call site is a reaction to a user action
    (add/resume/retry) or a job finishing, none of which happen on their own
    after a restart (e.g. the --reload autoreloader restarting mid-download
    on a code change). Without this, a QUEUED backlog just sits idle forever
    until someone happens to touch the queue again."""
    _maybe_start_next_from_queue()


def _remount_downloads_dir(new_dir: str) -> None:
    """Swaps the /downloads StaticFiles mount to the new directory in place -
    StaticFiles binds its directory at construction time, so just reassigning
    the DOWNLOADS_DIR global wouldn't actually change what /downloads links
    serve. Starlette resolves a Mount's .app per-request (not cached
    elsewhere), so replacing it here takes effect immediately, no restart
    needed."""
    for route in app.router.routes:
        if getattr(route, "path", None) == "/downloads":
            route.app = StaticFiles(directory=new_dir)
            return


@app.get("/api/settings")
def get_settings() -> dict:
    return {"downloads_dir": DOWNLOADS_DIR}


@app.post("/api/settings")
def update_settings(req: SettingsRequest) -> dict:
    global DOWNLOADS_DIR
    new_dir = os.path.expanduser(req.downloads_dir.strip())
    if not new_dir:
        raise HTTPException(status_code=400, detail="Please provide a downloads folder path.")
    try:
        os.makedirs(new_dir, exist_ok=True)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Couldn't use that folder: {e}")

    DOWNLOADS_DIR = new_dir
    _remount_downloads_dir(new_dir)
    settings = _load_settings()
    settings["downloads_dir"] = new_dir
    _save_settings(settings)

    return {"downloads_dir": DOWNLOADS_DIR}


@app.get("/api/version")
def get_version() -> dict:
    return {"version": APP_VERSION}


@app.get("/api/check-updates")
def check_updates() -> dict:
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Couldn't reach GitHub: {e}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="No releases have been published yet.")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub returned an unexpected response ({resp.status_code}).")

    data = resp.json()
    latest_tag = data.get("tag_name") or ""
    current_parsed = _parse_version(APP_VERSION)
    latest_parsed = _parse_version(latest_tag)

    if current_parsed is not None and latest_parsed is not None:
        update_available = latest_parsed > current_parsed
    else:
        # Fallback for a non-numeric tag - at least flag a difference.
        update_available = latest_tag.lstrip("vV") != APP_VERSION.lstrip("vV")

    return {
        "current_version": APP_VERSION,
        "latest_version": latest_tag,
        "update_available": update_available,
        "release_url": data.get("html_url"),
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/start")
def start_job(req: StartRequest) -> dict:
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")

    if req.poll_interval < 3:
        raise HTTPException(status_code=400, detail="poll_interval must be at least 3 seconds.")

    started = _try_start_job(req.url, req.poll_interval)
    if not started:
        raise HTTPException(status_code=409, detail="A monitoring job is already running. Stop it first.")

    return state.to_dict()


@app.post("/api/stop")
def stop_job() -> dict:
    if state.stop_event is not None:
        state.stop_event.set()
    return state.to_dict()


@app.get("/api/status")
def get_status() -> dict:
    return state.to_dict()


@app.post("/api/queue/add")
def add_to_queue(req: QueueAddRequest) -> dict:
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) URL.")

    queue_state.add(req.url)
    _maybe_start_next_from_queue()
    return queue_state.to_dict()


@app.get("/api/queue/status")
def get_queue_status() -> dict:
    return queue_state.to_dict()


@app.post("/api/queue/resume")
def resume_queue() -> dict:
    queue_state.set_queue_paused(False)
    _maybe_start_next_from_queue()
    return queue_state.to_dict()


@app.post("/api/queue/clear")
def clear_queue() -> dict:
    queue_state.clear()
    return queue_state.to_dict()


@app.post("/api/queue/{item_id}/retry")
def retry_queue_item(item_id: str) -> dict:
    retried = queue_state.retry(item_id)
    if not retried:
        raise HTTPException(status_code=404, detail="No errored queue item with that id.")
    _maybe_start_next_from_queue()
    return queue_state.to_dict()


@app.delete("/api/queue/{item_id}")
def remove_from_queue(item_id: str) -> dict:
    queue_state.remove(item_id)
    return queue_state.to_dict()


@app.post("/api/overlay-items")
def start_overlay_items(req: OverlayItemsRequest) -> dict:
    parsed = urlparse(req.event_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) event URL.")

    started = _try_start_overlay_items(req.event_url)
    if not started:
        raise HTTPException(status_code=409, detail="Already loading auctions for an event. Wait for it to finish.")

    return overlay_items_state.to_dict()


@app.get("/api/overlay-items/status")
def get_overlay_items_status() -> dict:
    return overlay_items_state.to_dict()


@app.post("/api/overlay-items/stop")
def stop_overlay_items() -> dict:
    if overlay_items_state.stop_event is not None:
        overlay_items_state.stop_event.set()
    return overlay_items_state.to_dict()


@app.post("/api/overlay-items/clear")
def clear_overlay_items() -> dict:
    if overlay_items_state.is_running():
        raise HTTPException(status_code=409, detail="Still loading auctions. Wait for it to finish before clearing.")
    overlay_items_state.clear()
    return overlay_items_state.to_dict()


@app.post("/api/overlay-export")
def start_overlay_export(req: OverlayExportRequest) -> dict:
    parsed = urlparse(req.event_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) event URL.")

    listing_id = req.listing_id.strip()
    if not listing_id:
        raise HTTPException(status_code=400, detail="Please select which auction to export.")

    if req.time_scale is not None and req.time_scale <= 0:
        raise HTTPException(status_code=400, detail="time_scale must be positive.")

    filename = _find_download_for_event(req.event_url)
    started = _try_start_overlay_export(filename, req.event_url, listing_id, req.time_offset, req.time_scale)
    if not started:
        raise HTTPException(status_code=409, detail="An overlay export is already running. Wait for it to finish.")

    return overlay_export_state.to_dict()


@app.get("/api/overlay-export/status")
def get_overlay_export_status() -> dict:
    return overlay_export_state.to_dict()


@app.post("/api/overlay-export/stop")
def stop_overlay_export() -> dict:
    if overlay_export_state.stop_event is not None:
        overlay_export_state.stop_event.set()
    return overlay_export_state.to_dict()


@app.post("/api/overlay-export/clear")
def clear_overlay_export() -> dict:
    if overlay_export_state.is_running():
        raise HTTPException(status_code=409, detail="An overlay export is running. Wait for it to finish before clearing.")
    overlay_export_state.clear()
    return overlay_export_state.to_dict()


@app.post("/api/clip-export")
def start_clip_export(req: ClipExportRequest) -> dict:
    parsed = urlparse(req.event_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Please provide a valid http(s) event URL.")

    if not req.clips:
        raise HTTPException(status_code=400, detail="Please provide at least one clip (start and end timestamps).")

    clip_requests = []
    for i, clip in enumerate(req.clips, start=1):
        try:
            start_seconds = clips.parse_timestamp_to_seconds(clip.start)
            end_seconds = clips.parse_timestamp_to_seconds(clip.end)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Clip {i}: {e}")
        if end_seconds <= start_seconds:
            raise HTTPException(status_code=400, detail=f"Clip {i}: end time must be later than the start time.")
        clip_requests.append({
            "start": clip.start, "end": clip.end,
            "start_seconds": start_seconds, "end_seconds": end_seconds,
        })

    started = _try_start_clip_export(req.event_url, clip_requests)
    if not started:
        raise HTTPException(status_code=409, detail="A clip export is already running. Wait for it to finish.")

    return clip_export_state.to_dict()


@app.get("/api/clip-export/status")
def get_clip_export_status() -> dict:
    return clip_export_state.to_dict()


@app.post("/api/clip-export/clear")
def clear_clip_export() -> dict:
    if clip_export_state.is_running():
        raise HTTPException(status_code=409, detail="A clip export is running. Wait for it to finish before clearing.")
    clip_export_state.clear()
    return clip_export_state.to_dict()


# --- Chat and Auction Monitor (live overlay) ---------------------------------

@app.post("/api/live/events")
async def start_live_event(req: LiveStartRequest) -> dict:
    event_id = extract_event_id(req.url)
    if not event_id:
        raise HTTPException(status_code=400, detail="Could not find an event id in that URL.")
    session = _live_get_or_start(event_id, req.url)
    return {"event_id": session.event_id, "overlay_url": f"/overlay/{session.event_id}"}


@app.get("/api/live/events")
async def list_live_events() -> dict:
    return {"events": [session.to_dict() for session in LIVE_EVENTS.values()]}


@app.post("/api/live/events/{event_id}/stop")
async def stop_live_event(event_id: str) -> dict:
    session = LIVE_EVENTS.pop(event_id, None)
    if session is None:
        raise HTTPException(status_code=404, detail="No such event is being watched.")
    session.stop()
    return {"stopped": event_id}


@app.get("/api/live/events/{event_id}/unknown-frames")
async def live_event_unknown_frames(event_id: str) -> dict:
    session = LIVE_EVENTS.get(event_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such event is being watched.")
    return {"frames": session.unknown_frames()}


@app.get("/api/live/events/{event_id}/logs")
async def live_event_logs(event_id: str) -> dict:
    session = LIVE_EVENTS.get(event_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such event is being watched.")
    return {"logs": list(session.logs)}


@app.get("/overlay/{event_id}")
async def live_overlay_page(event_id: str) -> FileResponse:
    _live_get_or_start(event_id, _live_canonical_url(event_id))
    return FileResponse(os.path.join(STATIC_DIR, "overlay.html"))


@app.get("/overlay/{event_id}/mobile")
async def live_overlay_mobile_page(event_id: str) -> FileResponse:
    _live_get_or_start(event_id, _live_canonical_url(event_id))
    return FileResponse(os.path.join(STATIC_DIR, "overlay_mobile.html"))


@app.websocket("/ws/{event_id}")
async def live_overlay_ws(websocket: WebSocket, event_id: str) -> None:
    session = _live_get_or_start(event_id, _live_canonical_url(event_id))
    await websocket.accept()
    session.broadcaster.add(websocket)
    try:
        for message in session.initial_messages():
            await websocket.send_json(message)
        while True:
            # The overlay page is receive-only; this just detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.broadcaster.discard(websocket)


@app.get("/api/tunnel-url")
def get_tunnel_url() -> dict:
    """Lazily starts (or reuses) a cloudflared tunnel to edge_app - see
    tunnel.py and desktop_launcher.py's second uvicorn server on
    tunnel.EDGE_PORT - so Talent View / OBS URL links work from anywhere,
    not just this machine. Waits up to tunnel.py's default 30s for a result
    (whether this request is the one starting it, or one already kicked off
    by the live-event "is it ready yet" gate) before giving up with a clear
    503 - never hangs indefinitely regardless of how long that startup
    actually takes under the hood."""
    try:
        return {"url": tunnel.get_or_start_tunnel_url()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/tunnel-status")
def get_tunnel_status() -> dict:
    """Non-blocking - for a UI indicator showing whether the tunnel
    (started at app launch - see _start_tunnel_at_launch) is still
    connecting or already up, without making the caller wait either way."""
    return tunnel.get_status()


# edge_app exposes only the overlay pages, their static assets, and the
# websocket - never the downloader, queue, or settings - since it's the only
# thing ever reachable through the tunnel above, which has no authentication.
edge_app = FastAPI()
edge_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
edge_app.get("/overlay/{event_id}")(live_overlay_page)
edge_app.get("/overlay/{event_id}/mobile")(live_overlay_mobile_page)
edge_app.websocket("/ws/{event_id}")(live_overlay_ws)


@app.on_event("startup")
def _start_edge_server() -> None:
    """Started here (rather than only in desktop_launcher.py) so edge_app
    comes up the same way whether the app is launched as a packaged build or
    run from source via `uvicorn app:app --reload` for development."""
    threading.Thread(
        target=lambda: uvicorn.run(edge_app, host="127.0.0.1", port=tunnel.EDGE_PORT, log_level="warning"),
        daemon=True,
    ).start()


@app.on_event("startup")
def _start_tunnel_at_launch() -> None:
    """Starts the sharing tunnel immediately at app launch rather than
    waiting for the first "Start watching" click, so cloudflared has the
    maximum possible head start - by the time anyone actually wants a
    Talent View / OBS URL link, it's usually already connected, instead of
    making that first click wait on it. live/session.py's own "is it ready
    yet" gate (EventSession._ensure_tunnel) still runs on every watched
    event too, as a correctness safety net for the rare case someone starts
    watching within seconds of launching the app - it'll just resolve
    near-instantly against the cached result in the common case now.

    Note this means the tunnel - and the public link it creates - stays up
    for as long as the app is running, not just while an event is being
    watched; see tunnel.py / README's note on treating that link as
    sensitive while it's live."""
    def _start() -> None:
        try:
            tunnel.get_or_start_tunnel_url()
        except Exception:
            pass  # a real "Start watching"/button click will retry and surface any failure then
    threading.Thread(target=_start, daemon=True).start()
