#!/usr/bin/env python3
"""
Replays a captured or hand-authored auction-event sequence through
AuctionState, printing each state change as it would be broadcast - for
eyeballing the push sequence during development without needing a live
event.

Accepts either the hand-authored fixture shape ({"auctions": [...]}) or a
real captured history JSON in the same shape (e.g. one produced by the
old ebay-live-downloader project's overlay/protocol.py: fetch_full_history).

Usage:
    python3 scripts/replay_fixture.py --file tests/fixtures/sample_auction_events.json
    python3 scripts/replay_fixture.py --file /path/to/history2.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live.config import PIN_EVENT_TYPE  # noqa: E402
from live.listener import _apply_pin_event  # noqa: E402
from live.state import AuctionState  # noqa: E402


def event_time(event: dict) -> datetime:
    server_created_at = event.get("serverCreatedAt")
    if server_created_at is not None:
        return datetime.fromtimestamp(server_created_at / 1000, tz=timezone.utc)
    return datetime.now(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="Path to a JSON file with an 'auctions' list")
    args = parser.parse_args()

    with open(args.file) as f:
        data = json.load(f)
    events = data.get("auctions", []) if isinstance(data, dict) else data
    events = sorted(events, key=lambda e: e.get("serverCreatedAt", 0))
    print(f"Replaying {len(events)} events from {args.file}...\n")

    state = AuctionState()
    for event in events:
        now = event_time(event)
        if event.get("typename") == PIN_EVENT_TYPE:
            changed = _apply_pin_event(event, state)
        else:
            changed = state.apply_event(event, now)
        if changed:
            snapshot = state.snapshot(now)
            print(f"[{now.isoformat()}] {event.get('typename')} -> {json.dumps(snapshot)}")


if __name__ == "__main__":
    main()
