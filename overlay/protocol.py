from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import websockets
from playwright.sync_api import sync_playwright

import monitor

WS_URL = "wss://fanout.ebay.com/livecommerce/auction/v1/fastly/socket"
EVENT_ID_PATTERN = re.compile(r"/ebaylive/events/([A-Za-z0-9]+)", re.IGNORECASE)
START_TIME_PATTERN = re.compile(
    r'"__typename":"LiveEvent","id":"[^"]+".{0,600}?"startTime":"([^"]+)"', re.DOTALL
)
END_TIME_PATTERN = re.compile(r'"endTime":"([^"]+)"')

AUCTION_MESSAGE_TYPES = [
    "AuctionWillStart", "AuctionWillStartV2", "AuctionStarted", "AuctionStartedV2",
    "AuctionNewBidLeader", "BidTimeExtended", "AuctionReset", "AuctionResetV2",
    "AuctionEnded", "AuctionEndedV2", "AuctionBiddingClosed",
]

DEFAULT_CHUNK_SECONDS = 90
MIN_CHUNK_SECONDS = 20
WINDOW_READ_TIMEOUT = 6
# How many window requests may be in flight at once, shared across BOTH
# categories (auctions and pin updates run concurrently - see
# fetch_full_history) via one asyncio.Semaphore - not per-category, so the
# combined load on the connection/backend stays bounded by this one number
# regardless of how many categories are running. Previously every window
# was fetched strictly one at a time (send, await its own reply, only then
# send the next) even though they're otherwise entirely independent
# requests - that serialization, not any single slow request, was why a
# multi-hour event's fetch took minutes even when every window succeeded
# on its first try. Kept modest rather than cranked way up: eBay's backend
# has already shown real flakiness under purely sequential load (the
# PublishFailedException bug - see MIN_CHUNK_RETRY_ATTEMPTS), so there's a
# genuine risk more concurrent load makes that worse, not just faster.
MAX_CONCURRENT_WINDOWS = 10
# Marks a diagnostic string (from _Multiplexer.request/_fetch_window) as
# meaning the websocket itself closed, not just one request timing out -
# every other window/category sharing that connection is equally dead, so
# callers check for this prefix to abort immediately rather than retrying.
CONNECTION_CLOSED_PREFIX = "connection closed: "
# How many *distinct window positions* (across all categories) are allowed
# to fail on their first attempt before giving up on the rest of the event
# entirely, rather than mechanically retrying through however much of the
# event remains.
#
# Originally this also counted every minimum-chunk retry attempt, which
# caused a real regression: two adjacent stubborn-but-recoverable-elsewhere
# windows (each needing ~4 failures of its own just to recursively split
# down to MIN_CHUNK_SECONDS, before its own 6 retries + 1 padded attempt)
# alone crossed this threshold and aborted the entire rest of an otherwise
# healthy fetch - silently dropping the last ~46 minutes of a real event's
# auction data, even though the connection kept working fine both before
# and after that pair (confirmed: it had already fetched 8600+ real events
# by then). A genuinely dead *connection* (as opposed to one stubborn
# request) is caught immediately and directly via the connection-closed
# check instead of by counting timeouts - so this threshold only needs to
# catch the softer case of a connection that stays open but the server
# stops answering on it, and can afford to be generous about it.
#
# With concurrent window fetching, "in a row" means "in the order each
# concurrent attempt happens to complete", not the order they were sent -
# a reasonable enough proxy for "does this connection still work at all"
# without needing a more elaborate statistical model.
MAX_CONSECUTIVE_WINDOW_FAILURES = 20
# A window that still fails once chunked down to MIN_CHUNK_SECONDS used to be
# given up on immediately - but silently dropping that window's auction/bid
# messages isn't just a gap, it directly produces wrong output: a listing
# whose AuctionStarted or final high bid fell in a dropped window either
# vanishes from the render entirely or shows a stale, too-low final price.
# A short flat retry (originally 4 attempts, 5s apart - under 20s of total
# patience) turned out nowhere near enough on a real event where a handful
# of windows stayed unavailable well past that - so this retries with
# exponential backoff out to several minutes of total patience before truly
# giving up. Each retry failure still counts toward MAX_CONSECUTIVE_WINDOW_
# FAILURES above (see its own comment for why that threshold was raised to
# tolerate this).
MIN_CHUNK_RETRY_ATTEMPTS = 6
MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 5.0
MIN_CHUNK_RETRY_BACKOFF_FACTOR = 2.0
MIN_CHUNK_RETRY_MAX_DELAY_SECONDS = 45.0
# All MIN_CHUNK_RETRY_ATTEMPTS retries above query the exact same
# [start, end) boundaries every time. That's the right move against a
# transient outage (which the backoff gives real time to clear), but if a
# window instead fails for every single retry over several minutes, that's
# evidence against "transient" - and the exact-same-request retries would
# never help against, say, a boundary-specific bug in the backend's own
# windowed-history endpoint (as opposed to a genuine gap in its archive,
# which padding can't fix either, but costs only one extra request to rule
# out). So before truly giving up, try once more with the window padded
# on both sides, then filter the response back down to just the originally
# requested range - safe from duplicating anything, since adjacent windows
# are fetched separately and never include data outside their own bounds.
WINDOW_PAD_SECONDS = 8.0


class _FailureTracker:
    """Shared across every window fetch (both categories, every recursive
    split) within one fetch_full_history run, so a string of failures
    anywhere is recognized as the same ongoing outage rather than resetting
    per category or split."""

    def __init__(self, max_consecutive: int = MAX_CONSECUTIVE_WINDOW_FAILURES) -> None:
        self.consecutive = 0
        self.max_consecutive = max_consecutive
        self.aborted = False

    def record_failure(self) -> bool:
        """Returns True the first time this failure pushes us past the
        threshold (so the caller logs the abort exactly once)."""
        self.consecutive += 1
        if self.consecutive >= self.max_consecutive and not self.aborted:
            self.aborted = True
            return True
        return False

    def record_success(self) -> None:
        self.consecutive = 0


def extract_event_id(event_url: str) -> Optional[str]:
    match = EVENT_ID_PATTERN.search(event_url)
    return match.group(1) if match else None


def extract_event_times(page_content: str) -> tuple[Optional[str], Optional[str]]:
    start_match = START_TIME_PATTERN.search(page_content)
    end_match = END_TIME_PATTERN.search(page_content)
    return (
        start_match.group(1) if start_match else None,
        end_match.group(1) if end_match else None,
    )


def _parse_iso(ts: str) -> datetime:
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _is_connection_closed(error: Optional[str]) -> bool:
    return bool(error) and error.startswith(CONNECTION_CLOSED_PREFIX)


def _message_created_at(message: dict) -> Optional[datetime]:
    ts_ms = message.get("serverCreatedAt")
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def bootstrap_session(event_url: str) -> dict:
    """
    Loads the event's replay page just long enough to capture the STOMP auth
    token/guid from the WebSocket CONNECT frame, and the event's real
    start/end time. Returns a dict with token, guid, event_id, start_time,
    end_time (all as needed by fetch_full_history). No waiting on playback.
    """
    event_id = extract_event_id(event_url)
    if not event_id:
        raise ValueError(f"Could not extract event id from URL: {event_url}")

    captured: dict = {}

    def on_websocket(ws):
        def on_sent(payload):
            text = payload if isinstance(payload, str) else ""
            if text.startswith("CONNECT"):
                auth_match = re.search(r"Authorization:(Bearer [^\r\n]+)", text)
                guid_match = re.search(r"guid=([a-f0-9]+)", text)
                if auth_match:
                    captured["token"] = auth_match.group(1).strip()
                if guid_match:
                    captured["guid"] = guid_match.group(1)
        ws.on("framesent", on_sent)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=monitor.HEADERS["User-Agent"])
        page = context.new_page()
        page.on("websocket", on_websocket)
        try:
            page.goto(event_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        # The page loads a nested liveweb.ebay.com frame that relays the auth
        # token before the actual STOMP websocket ever opens - how long that
        # takes varies a lot with network conditions (observed anywhere from
        # ~2s to ~18s), so poll for it instead of a single fixed sleep, which
        # was cutting connections off before the token ever arrived (see
        # live/auth.py's bootstrap_session, which hit the same issue first).
        for _ in range(48):  # up to ~24s
            if "token" in captured:
                break
            page.wait_for_timeout(500)
        content = page.content()
        browser.close()

    if "token" not in captured:
        raise RuntimeError("Could not capture an auth token from the event page's WebSocket connection.")

    start_time, end_time = extract_event_times(content)
    if not start_time:
        raise RuntimeError("Could not find the event's startTime on its page.")

    return {
        "event_id": event_id,
        "token": captured["token"],
        "guid": captured.get("guid", str(uuid.uuid4())),
        "start_time": start_time,
        "end_time": end_time,
    }


def _stomp_frame(command: str, headers: dict, body: str = "") -> str:
    lines = [command]
    for key, value in headers.items():
        lines.append(f"{key}:{value}")
    return "\n".join(lines) + "\n\n" + body + "\x00"


def _parse_stomp_frame(raw: str) -> Optional[tuple[str, dict, str]]:
    text = raw.lstrip("\n")
    if not text:
        return None  # heartbeat
    header_part, _, body = text.partition("\n\n")
    lines = header_part.split("\n")
    command = lines[0]
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key] = value
    return command, headers, body


class _Multiplexer:
    """Wraps a single websocket connection so many coroutines can each send
    a request and concurrently await their own matching response, instead
    of one caller having to fully finish (send, then block reading until
    its own reply shows up) before the next caller can even send theirs.

    Only one coroutine may safely call ws.recv() on a given connection at a
    time - concurrent readers would race, nondeterministically stealing
    frames from each other. A single background task owns ws.recv() here
    and demultiplexes each incoming payload to whichever pending request is
    waiting for that requestId (via a dict of futures), so this is what
    actually makes "many windows fetched at once" safe rather than just
    "many windows requested at once and hoping for the best". Concurrent
    *sends* are a real hazard too (interleaved writes can corrupt the
    frame stream), so all sends - including the heartbeat loop's - go
    through the same lock.
    """

    def __init__(self, ws):
        self.ws = ws
        self._pending: dict = {}
        self._send_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._reader_task is None:
            return
        self._reader_task.cancel()
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def send_raw(self, data: str) -> None:
        async with self._send_lock:
            await self.ws.send(data)

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self.ws.recv()
                self._dispatch(raw)
        except websockets.ConnectionClosed as e:
            self._fail_all_pending(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._fail_all_pending(e)

    def _dispatch(self, raw: str) -> None:
        for frame_text in raw.split("\x00")[:-1] if raw.endswith("\x00") else [raw]:
            parsed = _parse_stomp_frame(frame_text)
            if not parsed:
                continue  # heartbeat
            _command, _headers, body = parsed
            if not body.strip():
                continue
            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                continue
            payload = envelope.get("payload", {})
            request_id = payload.get("requestId")
            future = self._pending.pop(request_id, None) if request_id else None
            # No match (an unsolicited/untargeted payload, or a response
            # that arrived after its own request already gave up and moved
            # on) is simply dropped - this used to be logged in rich detail
            # while tracking down a real eBay-side bug (a PublishFailedException
            # showing up as exactly this kind of unmatched frame), which is
            # already understood now and doesn't need per-frame forensics
            # going forward.
            if future is not None and not future.done():
                future.set_result(payload)

    def _fail_all_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def request(self, frame: str, request_id: str, timeout: float) -> tuple[Optional[dict], str]:
        """Sends `frame` and awaits the response matching request_id.
        Returns (payload_or_None, diagnostic) - diagnostic is "timeout" or
        a CONNECTION_CLOSED_PREFIX-ed string on failure, "" on success."""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await self.ws.send(frame)
        except websockets.ConnectionClosed as e:
            self._pending.pop(request_id, None)
            return None, f"{CONNECTION_CLOSED_PREFIX}{e!r}"
        try:
            payload = await asyncio.wait_for(future, timeout=timeout)
            return payload, ""
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return None, "timeout"
        except websockets.ConnectionClosed as e:
            return None, f"{CONNECTION_CLOSED_PREFIX}{e!r}"


async def _fetch_window(
    mux: _Multiplexer, common_headers: dict, event_id: str, message_types: list, keystone_field: Optional[str],
    start_iso: str, end_iso: str, page_size: int = 200,
) -> tuple[list, Optional[str]]:
    """Fetches all messages for one time window via cursor pagination
    (inherently sequential - each page's cursor is only known from the
    previous page's response). Returns (messages, error)."""
    messages = []
    cursor = None
    while True:
        request_id = f"fetch-{uuid.uuid4()}"
        payload = {
            "typename": "ReplayEventMessagesRequest",
            "messageTypes": message_types,
            "newestFirst": False,
            "eventId": f"/liveevents/{event_id}",
            "pageSize": page_size,
            "cursor": cursor,
            "requestId": request_id,
            "startTime": start_iso,
            "endTime": end_iso,
        }
        envelope = {"specVersion": "1.22.1", "payload": payload}
        frame = _stomp_frame(
            "SEND",
            {**common_headers, "destination": f"/liveevents/{event_id}", "content-type": "application/json"},
            json.dumps(envelope),
        )
        response, diagnostic = await mux.request(frame, request_id, timeout=WINDOW_READ_TIMEOUT)
        if response is None:
            return messages, diagnostic
        if "Error" in response.get("typename", ""):
            return messages, response.get("errorCode", response.get("typename"))

        messages.extend(response.get("messages", []))
        cursor = response.get("nextCursor")
        if not response.get("hasMore"):
            return messages, None


async def _fetch_one_window_with_recovery(
    mux: _Multiplexer, common_headers: dict, event_id: str, message_types: list,
    start: datetime, end: datetime, chunk_seconds: int,
    on_log: Callable[[str], None], tracker: _FailureTracker,
    should_stop: Optional[Callable[[], bool]], lost_ranges: Optional[list],
) -> list:
    """Fetches one window, attempting it once and - if that fails -
    recovering: split into smaller sub-windows and retry each (recursing
    into itself down to MIN_CHUNK_SECONDS), and once at that minimum size,
    retry with backoff and finally a padded attempt before truly giving up
    on it. Called once per top-level window by _fetch_category, each as
    its own concurrent task - this function handles that window's entire
    lifecycle on its own rather than splitting "first attempt" and
    "recovery" across two separate layers (which would otherwise redundantly
    repeat the first attempt)."""
    if tracker.aborted or (should_stop is not None and should_stop()):
        return []

    messages, error = await _fetch_window(mux, common_headers, event_id, message_types, None, _to_iso(start), _to_iso(end))
    if not error:
        tracker.record_success()
        return messages

    if _is_connection_closed(error):
        on_log(f"{error} - the connection itself closed, stopping here with whatever was gathered so far rather than retrying a dead connection.")
        tracker.aborted = True
        return []

    if tracker.record_failure():
        on_log(
            f"{tracker.consecutive} window fetches in a row failed to get a response - the "
            "connection appears to have stopped responding partway through the event. "
            "Stopping early with whatever was gathered so far instead of grinding through "
            "the rest of the event the same way."
        )
        return []

    if chunk_seconds > MIN_CHUNK_SECONDS:
        on_log(f"Window {start.isoformat()}..{end.isoformat()} failed ({error}), retrying smaller.")
        half = max(chunk_seconds // 2, MIN_CHUNK_SECONDS)
        sub_messages = []
        sub_start = start
        while sub_start < end:
            if tracker.aborted or (should_stop is not None and should_stop()):
                break
            sub_end = min(sub_start + timedelta(seconds=half), end)
            sub_messages.extend(await _fetch_one_window_with_recovery(
                mux, common_headers, event_id, message_types, sub_start, sub_end, half,
                on_log, tracker, should_stop, lost_ranges,
            ))
            sub_start = sub_end
        return sub_messages

    on_log(f"Window {start.isoformat()}..{end.isoformat()} failed ({error}) at the minimum chunk size - retrying with backoff before giving up on it.")
    delay = MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    total_waited = 0.0
    for retry_num in range(1, MIN_CHUNK_RETRY_ATTEMPTS + 1):
        if tracker.aborted:
            break
        if should_stop is not None and should_stop():
            break
        await asyncio.sleep(delay)
        total_waited += delay
        delay = min(delay * MIN_CHUNK_RETRY_BACKOFF_FACTOR, MIN_CHUNK_RETRY_MAX_DELAY_SECONDS)
        retry_messages, retry_error = await _fetch_window(mux, common_headers, event_id, message_types, None, _to_iso(start), _to_iso(end))
        if not retry_error:
            on_log(f"Window {start.isoformat()}..{end.isoformat()} recovered on retry {retry_num} (after {total_waited:.0f}s).")
            tracker.record_success()
            return retry_messages
        if _is_connection_closed(retry_error):
            on_log(f"{retry_error} - the connection itself closed, stopping here with whatever was gathered so far rather than retrying a dead connection.")
            tracker.aborted = True
            return []
        # Deliberately NOT tracker.record_failure() here - see
        # MAX_CONSECUTIVE_WINDOW_FAILURES's docstring for why a stubborn
        # window's own retries must never count toward "the whole
        # connection looks dead".

    if tracker.aborted:
        return []

    padded_start = start - timedelta(seconds=WINDOW_PAD_SECONDS)
    padded_end = end + timedelta(seconds=WINDOW_PAD_SECONDS)
    padded_messages, padded_error = await _fetch_window(mux, common_headers, event_id, message_types, None, _to_iso(padded_start), _to_iso(padded_end))
    if not padded_error:
        filtered = [
            m for m in padded_messages
            if (ts := _message_created_at(m)) is None or start <= ts < end
        ]
        on_log(
            f"Window {start.isoformat()}..{end.isoformat()} recovered via a padded "
            f"retry ({len(filtered)} message(s) within the original range, out of "
            f"{len(padded_messages)} fetched) - the exact-boundary request may have been hitting "
            "a boundary-specific issue rather than a true data gap."
        )
        tracker.record_success()
        return filtered
    if _is_connection_closed(padded_error):
        on_log(f"{padded_error} - the connection itself closed, stopping here with whatever was gathered so far rather than retrying a dead connection.")
        tracker.aborted = True
        return []

    on_log(
        f"Window {start.isoformat()}..{end.isoformat()} failed ({error}) even after "
        f"{MIN_CHUNK_RETRY_ATTEMPTS} retries over ~{total_waited:.0f}s and a padded retry - "
        "giving up on this slice. Auctions active during this time range may be missing from "
        "the result or show a stale final price."
    )
    if lost_ranges is not None:
        lost_ranges.append((start, end))
    return []


async def _fetch_category(
    mux: _Multiplexer, common_headers: dict, event_id: str, message_types: list,
    window_start: datetime, window_end: datetime, chunk_seconds: int,
    on_log: Callable[[str], None],
    tracker: _FailureTracker,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
    lost_ranges: Optional[list] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
    stop_when: Optional[Callable[[list], bool]] = None,
) -> list:
    """Fetches one category across the full duration, via persistent worker
    tasks pulling chunk_seconds-sized windows off a shared queue - rather
    than one window at a time for the whole category. Each worker handles
    a window's entire lifecycle itself - its first attempt, and if that
    fails, the full recursive-split + backoff-retry + padded-retry recovery
    (see _fetch_one_window_with_recovery). A worker stuck deep in one
    window's recovery isn't pulling new work meanwhile, but the other
    (typically healthy) workers keep going.

    How many windows actually fetch at once is bounded by `semaphore` (see
    MAX_CONCURRENT_WINDOWS), not by the worker count itself - the worker
    count just controls how many pull from the queue concurrently, while
    `semaphore` is what two categories running together (see
    fetch_full_history) share to cap their *combined* concurrent fetches.

    Stops early (returning whatever was gathered so far) if `tracker` has
    recorded too many failures (see MAX_CONSECUTIVE_WINDOW_FAILURES),
    `should_stop` reports the job was cancelled, or - the reason this is a
    worker pool pulling from a queue rather than every window launched
    upfront - `stop_when`, if given, returns True once called with the
    messages gathered so far: workers then stop pulling further NEW
    windows (already in-flight ones still finish naturally) instead of
    working through the rest of the queue regardless. Used by
    fetch_history_for_title to stop scanning pin/listing history the
    moment a title match is found, rather than needing the whole event's
    pin history first.

    on_progress, if given, is called with a 0.0-1.0 fraction of this
    category's total duration *fully resolved* so far (a window counts
    once its own recovery, if any, has finished) - a stubborn window's own
    slice of progress is delayed until then, but every other concurrently-
    completing window keeps the bar moving in the meantime.
    """
    total_seconds = (window_end - window_start).total_seconds() or 1.0
    boundaries: list = []
    cursor = window_start
    while cursor < window_end:
        end = min(cursor + timedelta(seconds=chunk_seconds), window_end)
        boundaries.append((cursor, end))
        cursor = end

    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)

    queue: asyncio.Queue = asyncio.Queue()
    for boundary in boundaries:
        queue.put_nowait(boundary)

    all_messages: list = []
    completed_seconds = 0.0
    found = False

    def report_progress() -> None:
        if on_progress is not None:
            on_progress(min(max(completed_seconds / total_seconds, 0.0), 1.0))

    async def worker() -> None:
        nonlocal completed_seconds, found
        while True:
            if found or tracker.aborted or (should_stop is not None and should_stop()):
                return
            try:
                start, end = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with semaphore:
                messages = await _fetch_one_window_with_recovery(
                    mux, common_headers, event_id, message_types, start, end, chunk_seconds,
                    on_log, tracker, should_stop, lost_ranges,
                )
            all_messages.extend(messages)
            completed_seconds += (end - start).total_seconds()
            report_progress()
            if stop_when is not None and stop_when(all_messages):
                found = True
                return

    num_workers = min(MAX_CONCURRENT_WINDOWS, len(boundaries)) or 1
    workers = [asyncio.ensure_future(worker()) for _ in range(num_workers)]
    await asyncio.gather(*workers)

    if should_stop is not None and should_stop():
        on_log("Stopped by user.")

    return all_messages


async def _fetch_keystone_snapshot(
    mux: _Multiplexer, common_headers: dict, event_id: str, keystone_event_type: str, message_types: list,
    start_iso: str, end_iso: str,
) -> list:
    """Best-effort snapshot fetch (e.g. viewer stats) - returns [] on any error rather than raising,
    since this endpoint has proven flaky for anything but the full event range."""
    request_id = f"fetch-{uuid.uuid4()}"
    payload = {
        "typename": "GetMessagesFromKeystoneEventRequest",
        "messageTypes": message_types,
        "keystoneEventType": keystone_event_type,
        "eventId": f"/liveevents/{event_id}",
        "pageSize": 200,
        "cursor": None,
        "requestId": request_id,
        "startTime": start_iso,
        "endTime": end_iso,
    }
    envelope = {"specVersion": "1.22.1", "payload": payload}
    frame = _stomp_frame(
        "SEND",
        {**common_headers, "destination": f"/liveevents/{event_id}", "content-type": "application/json"},
        json.dumps(envelope),
    )
    response, _diagnostic = await mux.request(frame, request_id, timeout=15)
    if response is None or "Error" in response.get("typename", ""):
        return []
    return response.get("messages", [])


def _monotonic_progress(progress_fn: Optional[Callable[[float], None]]) -> Optional[Callable[[float], None]]:
    """Wraps a progress callback so it never reports a value lower than one
    it already reported. Needed once auctions and pin-updates fetch
    concurrently (see fetch_full_history): each maps into its own slice of
    the overall 0.0-1.0 range, but with both running at once, whichever
    category's callback happens to fire last "wins" regardless of actual
    order - without this, the progress bar could visibly jump backward
    (e.g. pin-updates briefly reporting a later point than auctions)."""
    if progress_fn is None:
        return None
    state = {"max": 0.0}

    def wrapped(frac: float) -> None:
        if frac > state["max"]:
            state["max"] = frac
            progress_fn(frac)

    return wrapped


def fetch_full_history(
    event_url: str,
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> dict:
    """
    Fetches the complete auction/bid history for an event's replay.
    Returns {"event_id", "start_time", "end_time", "auctions": [...]}.

    should_stop, if given, is polled between window fetches so a cancelled
    job stops promptly instead of running to completion (or to the failure
    circuit breaker below) regardless.

    on_progress, if given, is called with a 0.0-1.0 fraction of this whole
    fetch as it proceeds (auctions and pin updates concurrently, then the
    viewer-count snapshot) - without this, a long or troubled fetch (see
    MAX_CONSECUTIVE_WINDOW_FAILURES) looks identical to a hung one, since
    the only feedback otherwise is scrolling log lines with no indication
    of how much of the event is actually left.
    """
    log = on_log or (lambda msg: None)
    tracker = _FailureTracker()
    lost_ranges: list = []
    on_progress = _monotonic_progress(on_progress)

    def _scaled_progress(base: float, weight: float) -> Optional[Callable[[float], None]]:
        if on_progress is None:
            return None
        return lambda frac: on_progress(base + frac * weight)

    log("Bootstrapping session (loading event page for auth token)...")
    session = bootstrap_session(event_url)
    event_id = session["event_id"]
    start_dt = _parse_iso(session["start_time"])
    end_dt = _parse_iso(session["end_time"]) if session["end_time"] else datetime.now(timezone.utc)
    log(f"Event window: {start_dt.isoformat()} .. {end_dt.isoformat()}")

    async def _heartbeat_loop(mux: _Multiplexer, interval=8):
        try:
            while True:
                await asyncio.sleep(interval)
                await mux.send_raw("\n")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _run():
        # Gathered incrementally into this dict (rather than only building
        # the return value at the very end) so that if something outside
        # _fetch_category's own handling still goes wrong - the initial
        # handshake, or the final best-effort viewer-stats call - whatever
        # was already fetched is still recovered below instead of being
        # discarded by an unhandled exception crashing the whole call (as
        # happened on a real event: a ConnectionClosed late in the run
        # wiped out over 10,000 already-fetched auction/bid events).
        gathered = {"auctions": [], "pin_updates": [], "viewer_stats": []}

        # ping_interval disabled: Fastly's relay doesn't answer WebSocket-protocol
        # pings, only STOMP-level heartbeats (which we send ourselves below) -
        # leaving the library's default keepalive on causes it to kill the
        # connection itself after ~20s thinking the server is unresponsive.
        try:
            async with websockets.connect(WS_URL, subprotocols=["v10.stomp", "v11.stomp", "v12.stomp"], ping_interval=None) as ws:
                common_headers = {
                    "X-EBAY-C-TRACKING": f"guid={session['guid']},pageId=0",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY-US",
                    "Accept-Language": "en-US",
                    "Authorization": session["token"],
                }
                await ws.send(_stomp_frame("CONNECT", {**common_headers, "accept-version": "1.2", "heart-beat": "10000,10000"}))
                await ws.recv()
                await ws.send(_stomp_frame("SUBSCRIBE", {**common_headers, "id": "sub-0", "destination": f"/clients/{session['guid']}"}))
                await ws.send(_stomp_frame("SUBSCRIBE", {**common_headers, "id": "sub-1", "destination": f"/liveevents/{event_id}"}))

                mux = _Multiplexer(ws)
                mux.start()
                semaphore = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)
                heartbeat_task = asyncio.create_task(_heartbeat_loop(mux))
                try:
                    log("Fetching auction/bid history and pin/listing history concurrently...")
                    auctions_task = asyncio.ensure_future(_fetch_category(
                        mux, common_headers, event_id, AUCTION_MESSAGE_TYPES, start_dt, end_dt, DEFAULT_CHUNK_SECONDS,
                        log, tracker, should_stop, _scaled_progress(0.0, 0.6), lost_ranges, semaphore,
                    ))
                    pin_updates_task = asyncio.ensure_future(_fetch_category(
                        mux, common_headers, event_id, ["EventListingsUpdated"], start_dt, end_dt, DEFAULT_CHUNK_SECONDS,
                        log, tracker, should_stop, _scaled_progress(0.6, 0.35), lost_ranges, semaphore,
                    ))
                    gathered["auctions"], gathered["pin_updates"] = await asyncio.gather(auctions_task, pin_updates_task)
                    log(f"Fetched {len(gathered['auctions'])} auction/bid events and {len(gathered['pin_updates'])} pin-update events.")

                    if tracker.aborted or (should_stop is not None and should_stop()):
                        log("Skipping viewer count history - stopping early.")
                    else:
                        log("Fetching viewer count history (best-effort)...")
                        gathered["viewer_stats"] = await _fetch_keystone_snapshot(
                            mux, common_headers, event_id, "LiveEventViewerStatsUpdatedV2", ["LiveEventViewerStatsUpdatedV2"],
                            _to_iso(start_dt), _to_iso(end_dt),
                        )
                        log(f"Fetched {len(gathered['viewer_stats'])} viewer-count data points.")
                    if on_progress is not None:
                        on_progress(1.0)
                finally:
                    heartbeat_task.cancel()
                    await mux.stop()
        except websockets.ConnectionClosed as e:
            log(f"The sharing connection closed unexpectedly ({e!r}) - continuing with whatever was already gathered ({len(gathered['auctions'])} auction/bid event(s), {len(gathered['pin_updates'])} pin-update event(s)) instead of losing it all.")

        return gathered["auctions"], gathered["pin_updates"], gathered["viewer_stats"]

    auctions, pin_updates, viewer_stats = asyncio.run(_run())

    if lost_ranges:
        total_lost_seconds = sum((b - a).total_seconds() for a, b in lost_ranges)
        log(
            f"WARNING: could not fetch complete auction/bid history for {len(lost_ranges)} time range(s) "
            f"totaling {total_lost_seconds:.0f}s, even after retrying - any auction active during one of "
            "these windows may be missing entirely from the result, or show an incomplete/stale final "
            "price:"
        )
        for lost_start, lost_end in lost_ranges:
            log(f"  - {lost_start.isoformat()} .. {lost_end.isoformat()}")

    return {
        "event_id": event_id,
        "start_time": session["start_time"],
        "end_time": session["end_time"],
        "auctions": auctions,
        "pin_updates": pin_updates,
        "viewer_stats": viewer_stats,
        "lost_ranges": [(a.isoformat(), b.isoformat()) for a, b in lost_ranges],
    }


def fetch_history_for_title(
    event_url: str,
    title_query: str,
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> Optional[dict]:
    """
    Like fetch_full_history, but targeted at exactly one auction by its
    listing title instead of the whole event - much faster when the match
    isn't near the very end of the event, since it never needs the whole
    event's data: scans pin/listing history only until a *complete* title
    match is found (see timeline.find_complete_segment_by_title), stopping
    there rather than continuing through the rest of the event (see
    _fetch_category's stop_when), then fetches auction/bid events only for
    that matched segment's own narrow time range - not the whole event's.

    Same cost as fetch_full_history in the worst case (title not found at
    all, or it's the very last thing pinned) - nothing can be known about
    where a title falls without having looked for it.

    Returns a history dict in the same shape fetch_full_history returns
    (plus "matched_listing_id", the title match's own listing_id, for
    overlay.timeline.find_listing_segment to locate it again after
    build_timeline), or None if no match was found anywhere in the event.
    """
    from .timeline import find_complete_segment_by_title  # local: timeline imports fetch_listing_title from here

    log = on_log or (lambda msg: None)
    tracker = _FailureTracker()
    on_progress = _monotonic_progress(on_progress)

    log("Bootstrapping session (loading event page for auth token)...")
    session = bootstrap_session(event_url)
    event_id = session["event_id"]
    start_dt = _parse_iso(session["start_time"])
    end_dt = _parse_iso(session["end_time"]) if session["end_time"] else datetime.now(timezone.utc)
    log(f"Event window: {start_dt.isoformat()} .. {end_dt.isoformat()}")

    async def _heartbeat_loop(mux: _Multiplexer, interval=8):
        try:
            while True:
                await asyncio.sleep(interval)
                await mux.send_raw("\n")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _run() -> Optional[dict]:
        matched_segment: dict = {"value": None}
        pin_updates_scanned: list = []

        def stop_when(messages_so_far: list) -> bool:
            segment = find_complete_segment_by_title(messages_so_far, title_query, start_dt)
            if segment is not None:
                matched_segment["value"] = segment
                return True
            return False

        try:
            async with websockets.connect(WS_URL, subprotocols=["v10.stomp", "v11.stomp", "v12.stomp"], ping_interval=None) as ws:
                common_headers = {
                    "X-EBAY-C-TRACKING": f"guid={session['guid']},pageId=0",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY-US",
                    "Accept-Language": "en-US",
                    "Authorization": session["token"],
                }
                await ws.send(_stomp_frame("CONNECT", {**common_headers, "accept-version": "1.2", "heart-beat": "10000,10000"}))
                await ws.recv()
                await ws.send(_stomp_frame("SUBSCRIBE", {**common_headers, "id": "sub-0", "destination": f"/clients/{session['guid']}"}))
                await ws.send(_stomp_frame("SUBSCRIBE", {**common_headers, "id": "sub-1", "destination": f"/liveevents/{event_id}"}))

                mux = _Multiplexer(ws)
                mux.start()
                heartbeat_task = asyncio.create_task(_heartbeat_loop(mux))
                try:
                    log(f"Searching pin/listing history for a title matching \"{title_query}\"...")
                    pin_updates_scanned = await _fetch_category(
                        mux, common_headers, event_id, ["EventListingsUpdated"], start_dt, end_dt, DEFAULT_CHUNK_SECONDS,
                        log, tracker, should_stop,
                        (lambda frac: on_progress(frac * 0.6)) if on_progress is not None else None,
                        None, None, stop_when,
                    )
                    segment = matched_segment["value"]
                    if segment is None:
                        log(f"No auction found with a title matching \"{title_query}\" after scanning the whole event.")
                        return None
                    log(
                        f"Found \"{segment['title']}\" ({segment['pinned_offset']:.0f}s..{segment['unpinned_offset']:.0f}s "
                        "into the event) - fetching just its auction/bid events..."
                    )

                    seg_start = start_dt + timedelta(seconds=segment["pinned_offset"])
                    seg_end = start_dt + timedelta(seconds=segment["unpinned_offset"])
                    auctions = await _fetch_category(
                        mux, common_headers, event_id, AUCTION_MESSAGE_TYPES, seg_start, seg_end, DEFAULT_CHUNK_SECONDS,
                        log, tracker, should_stop,
                        (lambda frac: on_progress(0.6 + frac * 0.4)) if on_progress is not None else None,
                    )
                    log(f"Fetched {len(auctions)} auction/bid event(s) for the matched listing.")
                    if on_progress is not None:
                        on_progress(1.0)
                    return {
                        "event_id": event_id,
                        "start_time": session["start_time"],
                        "end_time": session["end_time"],
                        "auctions": auctions,
                        "pin_updates": pin_updates_scanned,
                        "viewer_stats": [],
                        "lost_ranges": [],
                        "matched_listing_id": segment["listing_id"],
                    }
                finally:
                    heartbeat_task.cancel()
                    await mux.stop()
        except websockets.ConnectionClosed as e:
            log(f"The sharing connection closed unexpectedly ({e!r}).")
            return None

    return asyncio.run(_run())


def fetch_listing_title(listing_id: str, cache: Optional[dict] = None) -> str:
    """Fetches an item's title from its public listing page (no auth needed)."""
    if cache is not None and listing_id in cache:
        return cache[listing_id]
    import html as html_module
    import requests
    title = f"Item {listing_id}"
    try:
        response = requests.get(f"https://www.ebay.com/itm/{listing_id}", headers=monitor.HEADERS, timeout=10)
        match = re.search(r"<title>([^<]+)</title>", response.text)
        if match:
            title = html_module.unescape(match.group(1))
            if title.endswith(" | eBay"):
                title = title[: -len(" | eBay")]
    except Exception:
        pass
    if cache is not None:
        cache[listing_id] = title
    return title
