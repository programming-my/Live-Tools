from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import tunnel
from .broadcast import Broadcaster
from .listener import run_listener
from .meta import LiveMeta
from .state import AuctionState


class EventSession:
    """Wires together the listener, auction state, chat/unknown-frame log,
    and broadcaster for one watched event. Card visibility is entirely
    pin-driven (see AuctionState/live/listener.py's _apply_pin_event) - this
    class has no side-effect timers or lookups of its own."""

    def __init__(self, event_url: str, event_id: str) -> None:
        self.event_url = event_url
        self.event_id = event_id
        self.status = "starting"
        self.status_message = ""
        self.logs: deque = deque(maxlen=200)
        self.frame_log: deque = deque(maxlen=200)  # chat messages + unrecognized frames
        self.auction_state = AuctionState()
        self.live_meta = LiveMeta()
        self.broadcaster = Broadcaster()
        self.stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._tunnel_ready = False
        self._tunnel_task: Optional[asyncio.Task] = None
        self._pending_live_message: Optional[str] = None

    def log(self, message: str) -> None:
        self.logs.append({"ts": datetime.now(timezone.utc).isoformat(), "msg": message})

    def _on_status(self, status: str, message: str) -> None:
        if status == "live" and not self._tunnel_ready:
            # Hold off showing "live" until the sharing tunnel (see tunnel.py)
            # is also ready, so Open Talent View / Copy OBS URL are instantly
            # usable the moment this event does show live - no separate wait
            # on first click. Runs concurrently with the listener's own
            # connection, so it rarely adds to the wait at all.
            self._pending_live_message = message
            self.status = "connecting"
            self.status_message = "Waiting for the sharing tunnel..."
            return
        self._pending_live_message = None
        self.status = status
        self.status_message = message

    # Cloudflare's free "quick tunnels" come with no uptime/latency guarantee
    # and can occasionally take much longer than usual to become reachable.
    # This bounds how long "live" waits on it specifically - tunnel.py's own
    # (longer) internal timeout keeps trying in the background regardless,
    # so Open Talent View / Copy OBS URL still pick it up later if it
    # finishes after this gate gives up.
    _TUNNEL_GATE_TIMEOUT = 30.0

    async def _ensure_tunnel(self) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(tunnel.get_or_start_tunnel_url), timeout=self._TUNNEL_GATE_TIMEOUT)
        except asyncio.TimeoutError:
            self.log("Sharing tunnel is taking longer than usual - continuing without waiting further for it.")
        except Exception as e:
            self.log(f"Sharing tunnel unavailable ({e}); Talent View/OBS links will only work on this machine.")
        finally:
            self._tunnel_ready = True
            if self._pending_live_message is not None:
                self.status = "live"
                self.status_message = self._pending_live_message
                self._pending_live_message = None

    def start(self) -> None:
        self._tunnel_task = asyncio.create_task(self._ensure_tunnel())
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await run_listener(
                self.event_url, self.auction_state, self.live_meta, self.broadcaster, self.frame_log,
                self.log, self._on_status, self.stop_event,
            )
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self.stop_event.set()
        if self._task is not None:
            self._task.cancel()
        if self._tunnel_task is not None:
            self._tunnel_task.cancel()

    def initial_messages(self) -> list[dict]:
        """Sent to a browser client right after it connects, so it doesn't wait
        for the next state change to show whatever's already in progress -
        the current auction card, event/host identity, viewer count, and
        whichever chat message is currently pinned (if any)."""
        now = datetime.now(timezone.utc)
        messages: list[dict] = []

        auction_data = self.auction_state.snapshot(now)
        if auction_data is not None:
            messages.append({"type": "auction", "data": auction_data})

        meta = self.live_meta.snapshot_meta()
        if meta is not None:
            messages.append({"type": "meta", "data": meta})

        if self.live_meta.viewer_count is not None:
            messages.append({"type": "viewer_count", "data": {"count": self.live_meta.viewer_count}})

        if self.live_meta.featured_message is not None:
            # Re-delivered as a normal "chat" envelope (it already carries
            # featured=True) - a late-joining client pins it the same way it
            # would have live, no separate envelope type needed.
            messages.append({"type": "chat", "data": self.live_meta.featured_message})

        return messages

    def unknown_frames(self) -> list:
        return [entry for entry in self.frame_log if entry.get("kind") == "unknown"]

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_url": self.event_url,
            "status": self.status,
            "status_message": self.status_message,
        }
