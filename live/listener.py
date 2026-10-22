from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import websockets

from .auth import bootstrap_session, to_iso
from .broadcast import Broadcaster
from .chat import try_parse_chat, try_parse_chat_removal
from .config import (
    AUCTION_MESSAGE_TYPES,
    CATCHUP_WINDOW_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    KNOWN_NON_CHAT_TYPES,
    PIN_EVENT_TYPE,
    READ_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF_INITIAL,
    RECONNECT_BACKOFF_MAX,
    VIEWER_STATS_TYPE,
    WS_URL,
)
from .meta import LiveMeta
from .state import AuctionState

WINDOW_READ_TIMEOUT = 6
DEFAULT_PAGE_SIZE = 200
# Consecutive read timeouts (each READ_TIMEOUT_SECONDS) before treating the
# connection as dead rather than just quiet - well beyond the 8s/10s
# heartbeat cadence, so this only trips on a genuine hang.
MAX_CONSECUTIVE_READ_TIMEOUTS = 3

CATCHUP_MESSAGE_TYPES = AUCTION_MESSAGE_TYPES + [PIN_EVENT_TYPE]


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


def _split_frames(raw: str) -> list:
    return raw.split("\x00")[:-1] if raw.endswith("\x00") else [raw]


def _apply_pin_event(payload: dict, auction_state: AuctionState) -> bool:
    """EventListingsUpdated carries one or more listings, each with its own
    isPinnedInEvent flag - applies pin/unpin for each, returns True iff the
    visible card changed as a result of any of them."""
    changed = False
    for listing in payload.get("eventListings", []):
        listing_id = listing.get("listingId")
        if not listing_id:
            continue
        if listing.get("isPinnedInEvent"):
            title = listing.get("title")
            details = (listing.get("saleInfo") or {}).get("details") or {}
            price_info = details.get("startingPrice") or listing.get("originalPrice") or {}
            price_original = price_info.get("original") or {}
            if auction_state.apply_pin(
                listing_id, title, price_original.get("amount"), price_original.get("currencyCode", "USD"),
            ):
                changed = True
        else:
            if auction_state.apply_unpin(listing_id):
                changed = True
    return changed


async def _read_matching_response(ws, request_id: str, timeout: float) -> Optional[dict]:
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
        for frame_text in _split_frames(raw):
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
    ws, common_headers: dict, event_id: str, message_types: list,
    start_iso: str, end_iso: str, page_size: int = DEFAULT_PAGE_SIZE,
) -> list:
    """Fetches messages in one time window via cursor pagination (best-effort, no retry-halving -
    unlike the offline pipeline's multi-hour history fetch, the live catch-up window is short)."""
    messages: list = []
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
        if response is None or "Error" in response.get("typename", ""):
            return messages
        messages.extend(response.get("messages", []))
        cursor = response.get("nextCursor")
        if not response.get("hasMore"):
            return messages


async def _catch_up(
    ws, common_headers: dict, session: dict, auction_state: AuctionState,
    broadcaster: Broadcaster, on_log: Callable[[str], None], now: datetime,
) -> None:
    """
    Fetches a short trailing window of recent auction + pin events on
    connect, so joining mid-item shows the in-progress card immediately
    instead of a blank overlay until the next pin. Best-effort: any failure
    here just falls back to live-only (same gap as not having catch-up at
    all).
    """
    window_start = now - timedelta(seconds=CATCHUP_WINDOW_SECONDS)
    try:
        messages = await _fetch_window(
            ws, common_headers, session["event_id"], CATCHUP_MESSAGE_TYPES,
            to_iso(window_start), to_iso(now),
        )
    except Exception as e:
        on_log(f"Catch-up fetch failed ({e}); starting live-only.")
        return

    messages.sort(key=lambda e: e.get("serverCreatedAt", 0))
    changed = False
    for event in messages:
        if event.get("typename") == PIN_EVENT_TYPE:
            if _apply_pin_event(event, auction_state):
                changed = True
        elif auction_state.apply_event(event, now):
            changed = True
    on_log(f"Catch-up: applied {len(messages)} recent event(s).")
    if changed:
        await broadcaster.send({"type": "auction", "data": auction_state.snapshot(now)})


async def _dispatch(
    payload: dict, auction_state: AuctionState, live_meta: LiveMeta, broadcaster: Broadcaster,
    chat_log: deque, on_log: Callable[[str], None],
) -> None:
    typename = payload.get("typename")
    now = datetime.now(timezone.utc)

    if typename == PIN_EVENT_TYPE:
        if _apply_pin_event(payload, auction_state):
            await broadcaster.send({"type": "auction", "data": auction_state.snapshot(now)})
        return

    if typename in AUCTION_MESSAGE_TYPES:
        if auction_state.apply_event(payload, now):
            await broadcaster.send({"type": "auction", "data": auction_state.snapshot(now)})
        return

    if typename == VIEWER_STATS_TYPE:
        count = payload.get("concurrentViewers")
        if count is not None and live_meta.apply_viewer_count(count):
            await broadcaster.send({"type": "viewer_count", "data": {"count": count}})
        return

    chat_message = try_parse_chat(payload)
    if chat_message is not None:
        chat_log.append({"kind": "chat", "message": chat_message})
        if chat_message.get("featured"):
            live_meta.set_featured_message(chat_message)
        await broadcaster.send({"type": "chat", "data": chat_message})
        return

    chat_removal = try_parse_chat_removal(payload)
    if chat_removal is not None:
        chat_log.append({"kind": "chat_removed", "message": chat_removal})
        live_meta.clear_featured_message_if(chat_removal.get("id"))
        await broadcaster.send({"type": "chat_removed", "data": chat_removal})
        return

    if typename in KNOWN_NON_CHAT_TYPES:
        return  # recognized housekeeping/telemetry - not chat, not logged as unknown

    chat_log.append({"kind": "unknown", "typename": typename, "payload": payload})
    on_log(f"Unrecognized frame typename={typename}: {json.dumps(payload)[:400]}")


async def _live_read_loop(
    ws, auction_state: AuctionState, live_meta: LiveMeta, broadcaster: Broadcaster,
    chat_log: deque, on_log: Callable[[str], None], stop_event: asyncio.Event,
) -> None:
    consecutive_timeouts = 0
    while not stop_event.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            consecutive_timeouts += 1
            if consecutive_timeouts >= MAX_CONSECUTIVE_READ_TIMEOUTS:
                raise
            continue
        consecutive_timeouts = 0

        for frame_text in _split_frames(raw):
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
            await _dispatch(envelope.get("payload", {}), auction_state, live_meta, broadcaster, chat_log, on_log)


async def _heartbeat_loop(ws, interval: int = HEARTBEAT_INTERVAL_SECONDS) -> None:
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send("\n")
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def run_listener(
    event_url: str,
    auction_state: AuctionState,
    live_meta: LiveMeta,
    broadcaster: Broadcaster,
    chat_log: deque,
    on_log: Callable[[str], None],
    on_status: Callable[[str, str], None],
    stop_event: asyncio.Event,
) -> None:
    """
    Connects to the event's live STOMP feed and dispatches messages
    indefinitely, reconnecting (with a freshly bootstrapped token, since the
    old one may have expired) and backing off on any failure, until
    stop_event is set.
    """
    backoff = RECONNECT_BACKOFF_INITIAL
    while not stop_event.is_set():
        try:
            on_status("connecting", "Bootstrapping session...")
            session = await asyncio.to_thread(bootstrap_session, event_url)
            on_log(f"Session bootstrapped for event {session['event_id']}.")
            if live_meta.apply_bootstrap(
                session.get("event_title"), session.get("host_username"), session.get("host_avatar_url"),
            ):
                await broadcaster.send({"type": "meta", "data": live_meta.snapshot_meta()})

            async with websockets.connect(
                WS_URL, subprotocols=["v10.stomp", "v11.stomp", "v12.stomp"], ping_interval=None,
            ) as ws:
                common_headers = {
                    "X-EBAY-C-TRACKING": f"guid={session['guid']},pageId=0",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY-US",
                    "Accept-Language": "en-US",
                    "Authorization": session["token"],
                }
                await ws.send(_stomp_frame(
                    "CONNECT", {**common_headers, "accept-version": "1.2", "heart-beat": "10000,10000"},
                ))
                await ws.recv()
                await ws.send(_stomp_frame(
                    "SUBSCRIBE", {**common_headers, "id": "sub-0", "destination": f"/clients/{session['guid']}"},
                ))
                await ws.send(_stomp_frame(
                    "SUBSCRIBE", {**common_headers, "id": "sub-1", "destination": f"/liveevents/{session['event_id']}"},
                ))

                heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
                backoff = RECONNECT_BACKOFF_INITIAL
                on_status("live", "Connected.")
                try:
                    await _catch_up(
                        ws, common_headers, session, auction_state, broadcaster, on_log,
                        datetime.now(timezone.utc),
                    )
                    await _live_read_loop(ws, auction_state, live_meta, broadcaster, chat_log, on_log, stop_event)
                finally:
                    heartbeat_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if stop_event.is_set():
                break
            on_status("reconnecting", str(e))
            on_log(f"Listener disconnected ({e}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    on_status("stopped", "Stopped.")
