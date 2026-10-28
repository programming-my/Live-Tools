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
# How many window fetches in a row (across all categories and recursion
# depths within one fetch_full_history call) are allowed to fail before
# giving up on the rest of the event entirely, rather than mechanically
# halving/retrying through however much of the event remains - observed on a
# real event where eBay's backend stopped answering requests on an otherwise
# still-open connection partway through: every subsequent window, even a
# 1-2 second one, timed out, which without this would have ground through
# the remaining ~75 minutes of the event one ever-smaller sub-window at a
# time before naturally finishing.
MAX_CONSECUTIVE_WINDOW_FAILURES = 8


class _FailureTracker:
    """Shared across every _fetch_category call (and its recursive splits)
    within one fetch_full_history run, so a string of failures in one
    category - or across a split window boundary - is still recognized as
    the same ongoing outage rather than resetting per recursive call."""

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


async def _read_matching_response(ws, request_id: str, timeout: float) -> Optional[dict]:
    """Reads STOMP frames until one whose JSON payload.requestId matches, or timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        for frame_text in raw.split("\x00")[:-1] if raw.endswith("\x00") else [raw]:
            parsed = _parse_stomp_frame(frame_text)
            if not parsed:
                continue
            _command, _headers, body = parsed
            if not body.strip():
                continue
            try:
                envelope = json.loads(body)
            except json.JSONDecodeError:
                continue
            payload = envelope.get("payload", {})
            if payload.get("requestId") == request_id:
                return payload


async def _fetch_window(
    ws, common_headers: dict, event_id: str, message_types: list, keystone_field: Optional[str],
    start_iso: str, end_iso: str, page_size: int = 200,
) -> tuple[list, Optional[str]]:
    """Fetches all messages for one time window via cursor pagination. Returns (messages, error)."""
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
        await ws.send(frame)

        response = await _read_matching_response(ws, request_id, timeout=WINDOW_READ_TIMEOUT)
        if response is None:
            return messages, "timeout"
        if "Error" in response.get("typename", ""):
            return messages, response.get("errorCode", response.get("typename"))

        messages.extend(response.get("messages", []))
        cursor = response.get("nextCursor")
        if not response.get("hasMore"):
            return messages, None


async def _fetch_category(
    ws, common_headers: dict, event_id: str, message_types: list,
    window_start: datetime, window_end: datetime, chunk_seconds: int,
    on_log: Callable[[str], None],
    tracker: _FailureTracker,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list:
    """Fetches one category across the full duration, chunked, halving a
    chunk on error - unless `tracker` has already recorded too many
    consecutive failures (see MAX_CONSECUTIVE_WINDOW_FAILURES) or
    `should_stop` reports the job was cancelled, in which case this stops
    early and returns whatever was gathered so far."""
    all_messages = []
    cursor_time = window_start
    while cursor_time < window_end:
        if tracker.aborted:
            break
        if should_stop is not None and should_stop():
            on_log("Stopped by user.")
            break
        chunk_end = min(cursor_time + timedelta(seconds=chunk_seconds), window_end)
        messages, error = await _fetch_window(
            ws, common_headers, event_id, message_types, None,
            _to_iso(cursor_time), _to_iso(chunk_end),
        )
        if error:
            if tracker.record_failure():
                on_log(
                    f"{tracker.consecutive} window fetches in a row failed to get a response - the "
                    "connection appears to have stopped responding partway through the event. "
                    "Stopping early with whatever was gathered so far instead of grinding through "
                    "the rest of the event the same way."
                )
                break
            if chunk_seconds > MIN_CHUNK_SECONDS:
                on_log(f"Window {cursor_time.isoformat()}..{chunk_end.isoformat()} failed ({error}), retrying smaller.")
                half = max(chunk_seconds // 2, MIN_CHUNK_SECONDS)
                sub_messages = await _fetch_category(
                    ws, common_headers, event_id, message_types, cursor_time, chunk_end, half, on_log,
                    tracker, should_stop,
                )
                all_messages.extend(sub_messages)
            else:
                on_log(f"Window {cursor_time.isoformat()}..{chunk_end.isoformat()} failed ({error}), giving up on this slice.")
        else:
            tracker.record_success()
            all_messages.extend(messages)
        cursor_time = chunk_end
    return all_messages


async def _fetch_keystone_snapshot(
    ws, common_headers: dict, event_id: str, keystone_event_type: str, message_types: list,
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
    await ws.send(frame)
    response = await _read_matching_response(ws, request_id, timeout=15)
    if response is None or "Error" in response.get("typename", ""):
        return []
    return response.get("messages", [])


def fetch_full_history(
    event_url: str,
    on_log: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> dict:
    """
    Fetches the complete auction/bid history for an event's replay.
    Returns {"event_id", "start_time", "end_time", "auctions": [...]}.

    should_stop, if given, is polled between window fetches so a cancelled
    job stops promptly instead of running to completion (or to the failure
    circuit breaker below) regardless.
    """
    log = on_log or (lambda msg: None)
    tracker = _FailureTracker()

    log("Bootstrapping session (loading event page for auth token)...")
    session = bootstrap_session(event_url)
    event_id = session["event_id"]
    start_dt = _parse_iso(session["start_time"])
    end_dt = _parse_iso(session["end_time"]) if session["end_time"] else datetime.now(timezone.utc)
    log(f"Event window: {start_dt.isoformat()} .. {end_dt.isoformat()}")

    async def _heartbeat_loop(ws, interval=8):
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send("\n")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _run():
        # ping_interval disabled: Fastly's relay doesn't answer WebSocket-protocol
        # pings, only STOMP-level heartbeats (which we send ourselves below) -
        # leaving the library's default keepalive on causes it to kill the
        # connection itself after ~20s thinking the server is unresponsive.
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

            heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
            try:
                log("Fetching auction/bid history...")
                auctions = await _fetch_category(
                    ws, common_headers, event_id, AUCTION_MESSAGE_TYPES, start_dt, end_dt, DEFAULT_CHUNK_SECONDS,
                    log, tracker, should_stop,
                )
                log(f"Fetched {len(auctions)} auction/bid events.")

                log("Fetching pin/listing history...")
                pin_updates = await _fetch_category(
                    ws, common_headers, event_id, ["EventListingsUpdated"], start_dt, end_dt, DEFAULT_CHUNK_SECONDS,
                    log, tracker, should_stop,
                )
                log(f"Fetched {len(pin_updates)} pin-update events.")

                if tracker.aborted or (should_stop is not None and should_stop()):
                    log("Skipping viewer count history - stopping early.")
                    viewer_stats = []
                else:
                    log("Fetching viewer count history (best-effort)...")
                    viewer_stats = await _fetch_keystone_snapshot(
                        ws, common_headers, event_id, "LiveEventViewerStatsUpdatedV2", ["LiveEventViewerStatsUpdatedV2"],
                        _to_iso(start_dt), _to_iso(end_dt),
                    )
                    log(f"Fetched {len(viewer_stats)} viewer-count data points.")

                return auctions, pin_updates, viewer_stats
            finally:
                heartbeat_task.cancel()

    auctions, pin_updates, viewer_stats = asyncio.run(_run())

    return {
        "event_id": event_id,
        "start_time": session["start_time"],
        "end_time": session["end_time"],
        "auctions": auctions,
        "pin_updates": pin_updates,
        "viewer_stats": viewer_stats,
    }


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
