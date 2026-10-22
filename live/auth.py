from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright

from .config import HEADERS

EVENT_ID_PATTERN = re.compile(r"/ebaylive/events/([A-Za-z0-9]+)", re.IGNORECASE)
START_TIME_PATTERN = re.compile(
    r'"__typename":"LiveEvent","id":"[^"]+".{0,600}?"startTime":"([^"]+)"', re.DOTALL
)
END_TIME_PATTERN = re.compile(r'"endTime":"([^"]+)"')

# Every event page embeds a flat Apollo-normalized cache as
# `window.__APOLLO_DATA__ = {"Type:id": {...fields, "someField": {"__ref": "OtherType:otherId"}}}`.
# Unlike START_TIME_PATTERN/END_TIME_PATTERN (small bounded-window field
# pulls), this needs the whole blob - it can run many KB - so this capture
# is intentionally unbounded up to the closing </script>.
APOLLO_DATA_PATTERN = re.compile(r"window\.__APOLLO_DATA__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL)


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


_EMPTY_PAGE_META = {"event_title": None, "host_username": None, "host_avatar_url": None}


def extract_page_meta(page_content: str, event_id: str) -> dict:
    """
    Best-effort extraction of the event's title and its host's username/avatar
    from the page's embedded Apollo cache (`window.__APOLLO_DATA__` - a flat
    `{"Type:id": {...fields, "someField": {"__ref": "OtherType:otherId"}}}`
    dump). This is supplementary UI polish, not core functionality (unlike
    the token/start_time bootstrap_session already requires) - any failure
    here (missing script, malformed JSON, unexpected shape, absent keys)
    must never raise; it just means the caller falls back to generic
    placeholders in the UI.
    """
    try:
        match = APOLLO_DATA_PATTERN.search(page_content)
        if not match:
            return dict(_EMPTY_PAGE_META)
        data = json.loads(match.group(1))

        live_event = data.get(f"LiveEvent:{event_id}")
        if live_event is None:
            # Cheap fallback in case the id format ever differs from the
            # cache key - scan for any LiveEvent entry rather than give up.
            live_event = next(
                (v for k, v in data.items() if k.startswith("LiveEvent:")), None,
            )
        if live_event is None:
            return dict(_EMPTY_PAGE_META)

        event_title = live_event.get("title")

        host_username = None
        host_avatar_url = None
        hosts = live_event.get("hosts") or []
        if hosts:
            host_ref = hosts[0].get("__ref")
            host_user = data.get(host_ref) if host_ref else None
            if host_user:
                host_username = host_user.get("userAccountName")
                host_avatar_url = host_user.get("profilePictureUrl")
                if not host_avatar_url:
                    # Not every seller has a personal profile picture set,
                    # but most stores have a logo - confirmed via a real
                    # event (2026-09-25) where profilePictureUrl was null
                    # but sellerProfile.sellerStore.logo resolved to a real
                    # image. Falls back to that rather than leaving the
                    # header with a generic placeholder unnecessarily.
                    seller_store = (host_user.get("sellerProfile") or {}).get("sellerStore") or {}
                    logo_ref = (seller_store.get("logo") or {}).get("__ref")
                    logo = data.get(logo_ref) if logo_ref else None
                    if logo:
                        host_avatar_url = logo.get("url")

        return {
            "event_title": event_title,
            "host_username": host_username,
            "host_avatar_url": host_avatar_url,
        }
    except Exception:
        return dict(_EMPTY_PAGE_META)


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def bootstrap_session(event_url: str) -> dict:
    """
    Loads the event's page just long enough to capture the STOMP auth
    token/guid from the WebSocket CONNECT frame, and the event's real
    start/end time. Returns a dict with token, guid, event_id, start_time,
    end_time, plus best-effort event_title/host_username/host_avatar_url
    (see extract_page_meta - these three are optional UI polish and may be
    None). Re-run on every (re)connect so a fresh token is always used -
    the token appears to be generated client-side and there's no known way
    to derive it without a browser.
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
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
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
        # was cutting connections off before the token ever arrived.
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

    page_meta = extract_page_meta(content, event_id)

    return {
        "event_id": event_id,
        "token": captured["token"],
        "guid": captured.get("guid", str(uuid.uuid4())),
        "start_time": start_time,
        "end_time": end_time,
        **page_meta,
    }
