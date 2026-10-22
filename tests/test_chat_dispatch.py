import asyncio
import json
from collections import deque
from datetime import datetime, timezone

from live import chat as chat_module
from live.broadcast import Broadcaster
from live.listener import _dispatch
from live.meta import LiveMeta
from live.state import AuctionState


class _FakeWebSocket:
    def __init__(self):
        self.sent: list = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _run(coro):
    return asyncio.run(coro)


def _dispatch_one(payload, state=None, live_meta=None):
    frame_log: deque = deque()
    logs: list = []
    ws = _FakeWebSocket()
    broadcaster = Broadcaster()
    broadcaster.add(ws)
    if state is None:
        state = AuctionState()
    if live_meta is None:
        live_meta = LiveMeta()

    _run(_dispatch(payload, state, live_meta, broadcaster, frame_log, logs.append))
    return frame_log, logs, ws, state


def test_unknown_typename_is_logged_and_not_broadcast():
    frame_log, logs, ws, _state = _dispatch_one({"typename": "TotallyUnknownThing", "foo": "bar"})

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "unknown"
    assert frame_log[0]["typename"] == "TotallyUnknownThing"
    assert any("Unrecognized frame" in msg for msg in logs)
    assert ws.sent == []


def test_registering_a_chat_parser_routes_the_same_payload_to_chat(monkeypatch):
    def _fake_parser(payload):
        return {"user": payload["userName"], "text": payload["text"], "ts": 0}

    monkeypatch.setitem(chat_module.CHAT_MESSAGE_PARSERS, "TotallyUnknownThing", _fake_parser)

    frame_log, logs, ws, _state = _dispatch_one(
        {"typename": "TotallyUnknownThing", "text": "hi", "userName": "dana"},
    )

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "chat"
    assert frame_log[0]["message"]["user"] == "dana"
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "chat"
    assert sent["data"]["user"] == "dana"


def test_real_chat_message_added_payload_is_parsed():
    # Captured verbatim (messageId/userId anonymized-format preserved) from a
    # real live event on 2026-08-31.
    payload = {
        "typename": "ChatMessageAdded",
        "chatMessage": {
            "messageId": "fa0a5a36-b80a-4e26-b94d-baae5b3c78bd",
            "createdAt": "2026-08-31T20:21:20.411Z",
            "text": "Nice",
            "userId": "gIcOSzYZQV-",
            "userName": "iwantgeetars",
            "messageType": "REGULAR",
            "memberId": "gIcOSzYZQV-",
            "memberName": "iwantgeetars",
            "mentions": [],
        },
        "userId": "gIcOSzYZQV-",
        "userName": "iwantgeetars",
        "serverCreatedAt": 1788207680489,
    }

    frame_log, logs, ws, _state = _dispatch_one(payload)

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "chat"
    assert frame_log[0]["message"] == {
        "id": "fa0a5a36-b80a-4e26-b94d-baae5b3c78bd", "user": "iwantgeetars", "text": "Nice", "ts": 1788207680411,
        "featured": False,
    }
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "chat"
    assert sent["data"]["user"] == "iwantgeetars"
    assert sent["data"]["text"] == "Nice"


def test_known_non_chat_types_are_dropped_silently():
    # LiveEventViewerStatsUpdatedV2 is deliberately NOT in this list anymore -
    # it's now actively handled (see test_viewer_stats_update_broadcasts_count
    # below) rather than silently dropped.
    for typename in [
        "ServerTimeBeacon", "StreamReactionReceived", "EventListingsUpdatedPatch",
        "StreamHealthNotice", "SubtitleCueReceived",
    ]:
        frame_log, logs, ws, _state = _dispatch_one({"typename": typename, "serverCreatedAt": 0, "concurrentViewers": 42})
        assert len(frame_log) == 0
        assert ws.sent == []


def test_featured_message_added_uses_the_same_chat_parser():
    # Captured verbatim from a real live event on 2026-08-31 - a
    # host/moderator-highlighted message, same chatMessage shape as regular chat.
    payload = {
        "typename": "FeaturedMessageAdded",
        "chatMessage": {
            "messageId": "93272854-53ca-4c50-b4e9-7861292c470d",
            "createdAt": "2026-08-31T20:57:43.334Z",
            "text": "lol one sec guys",
            "userId": "jhjzx95_tio",
            "userName": "centralhype",
            "messageType": "REGULAR",
            "memberId": "jhjzx95_tio",
            "memberName": "centralhype",
            "mentions": [],
        },
        "featuredAt": "2026-08-31T20:57:43.334Z",
        "serverCreatedAt": 1788209863535,
    }

    frame_log, logs, ws, _state = _dispatch_one(payload)

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "chat"
    assert frame_log[0]["message"]["user"] == "centralhype"
    assert frame_log[0]["message"]["text"] == "lol one sec guys"
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "chat"


def test_featured_message_removed_is_not_treated_as_a_deletion():
    # "FeaturedMessageRemoved" almost certainly means "un-highlighted", not
    # "deleted from the chat feed entirely" - no evidence either way, so it
    # must stay a no-op (routed to "unknown") rather than removing a message
    # that's still legitimately present in the regular chat feed.
    assert "FeaturedMessageRemoved" not in chat_module.CHAT_REMOVAL_PARSERS
    frame_log, logs, ws, _state = _dispatch_one({"typename": "FeaturedMessageRemoved", "messageId": "abc"})
    assert frame_log[0]["kind"] == "unknown"
    assert ws.sent == []


def test_real_chat_message_deleted_payload_is_parsed():
    # Captured verbatim from a real live event on 2026-09-08 - a comment
    # deleted via backend moderation.
    payload = {
        "typename": "ChatMessageDeleted",
        "messageId": "73ab8e1a-cae2-4e91-87f9-dc495c58e99f",
        "serverCreatedAt": 1788907235302,
    }

    frame_log, logs, ws, _state = _dispatch_one(payload)

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "chat_removed"
    assert frame_log[0]["message"] == {"id": "73ab8e1a-cae2-4e91-87f9-dc495c58e99f"}
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent == {"type": "chat_removed", "data": {"id": "73ab8e1a-cae2-4e91-87f9-dc495c58e99f"}}


def test_registering_a_chat_removal_parser_broadcasts_chat_removed(monkeypatch):
    def _fake_removal_parser(payload):
        return {"id": payload["messageId"]}

    monkeypatch.setitem(chat_module.CHAT_REMOVAL_PARSERS, "SomeMessageDeleted", _fake_removal_parser)

    frame_log, logs, ws, _state = _dispatch_one({"typename": "SomeMessageDeleted", "messageId": "abc123"})

    assert len(frame_log) == 1
    assert frame_log[0]["kind"] == "chat_removed"
    assert frame_log[0]["message"] == {"id": "abc123"}
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent == {"type": "chat_removed", "data": {"id": "abc123"}}


def test_known_auction_typename_still_dispatches_normally():
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    frame_log, logs, ws, _state = _dispatch_one({
        "typename": "AuctionStarted", "listingId": "1",
        "startedAt": now.isoformat(), "endingAt": (now + dt.timedelta(seconds=30)).isoformat(),
        "startingPrice": {"original": {"amount": 1.0, "currencyCode": "USD"}},
        "serverCreatedAt": int(now.timestamp() * 1000),
    })

    assert len(frame_log) == 0  # auction events don't go through the chat/unknown log
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "auction"
    assert sent["data"]["listing_id"] == "1"


def test_event_listings_updated_pin_shows_starting_soon():
    payload = {
        "typename": "EventListingsUpdated",
        "eventId": "evt1",
        "eventListings": [{
            "listingId": "555",
            "title": "Cool Watch",
            "saleInfo": {"typename": "AuctionSaleInfo", "details": {
                "typename": "ReadyAuctionDetails",
                "startingPrice": {"original": {"amount": 10.0, "currencyCode": "USD"}},
                "bidDurationSec": 15,
            }},
            "isPinnedInEvent": True,
        }],
        "serverCreatedAt": 0,
    }

    frame_log, logs, ws, _state = _dispatch_one(payload)

    assert len(frame_log) == 0  # pin events don't go through the chat/unknown log
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent["type"] == "auction"
    assert sent["data"]["phase"] == "pinned"
    assert sent["data"]["title"] == "Cool Watch"
    assert sent["data"]["price"] == 10.0


def test_event_listings_updated_unpin_clears_the_card():
    state = AuctionState()
    state.apply_pin("555", "Cool Watch", 10.0, "USD")

    payload = {
        "typename": "EventListingsUpdated",
        "eventId": "evt1",
        "eventListings": [{
            "listingId": "555",
            "title": "Cool Watch",
            "saleInfo": {"typename": "AuctionSaleInfo", "details": {
                "typename": "EndedAuctionDetails",
                "startedAt": "2026-08-31T20:00:00.000Z", "endedAt": "2026-08-31T20:00:20.000Z",
                "result": {"typename": "AuctionResultSuccess",
                           "winner": {"userName": "bob"},
                           "winningBid": {"original": {"amount": 30.0, "currencyCode": "USD"}}},
            }},
            "isPinnedInEvent": False,
        }],
        "serverCreatedAt": 0,
    }

    frame_log, logs, ws, _state = _dispatch_one(payload, state=state)

    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent == {"type": "auction", "data": None}
    assert state.snapshot(datetime.now(timezone.utc)) is None


def test_viewer_stats_update_broadcasts_count():
    live_meta = LiveMeta()
    payload = {"typename": "LiveEventViewerStatsUpdatedV2", "concurrentViewers": 42, "serverCreatedAt": 0}

    frame_log, logs, ws, _state = _dispatch_one(payload, live_meta=live_meta)

    assert len(frame_log) == 0  # not chat, not logged as unknown
    assert live_meta.viewer_count == 42
    assert len(ws.sent) == 1
    sent = json.loads(ws.sent[0])
    assert sent == {"type": "viewer_count", "data": {"count": 42}}


def test_viewer_stats_update_does_not_rebroadcast_unchanged_count():
    live_meta = LiveMeta()
    live_meta.apply_viewer_count(42)
    payload = {"typename": "LiveEventViewerStatsUpdatedV2", "concurrentViewers": 42, "serverCreatedAt": 0}

    frame_log, logs, ws, _state = _dispatch_one(payload, live_meta=live_meta)

    assert ws.sent == []


def test_featured_message_added_is_tracked_as_the_pinned_message():
    live_meta = LiveMeta()
    payload = {
        "typename": "FeaturedMessageAdded",
        "chatMessage": {
            "messageId": "pinned-1", "createdAt": "2026-08-31T20:57:43.334Z", "text": "Welcome!",
            "userId": "u1", "userName": "theHost", "messageType": "REGULAR",
            "memberId": "u1", "memberName": "theHost", "mentions": [],
        },
        "featuredAt": "2026-08-31T20:57:43.334Z",
        "serverCreatedAt": 1788209863535,
    }

    _dispatch_one(payload, live_meta=live_meta)

    assert live_meta.featured_message is not None
    assert live_meta.featured_message["id"] == "pinned-1"
    assert live_meta.featured_message["featured"] is True


def test_chat_message_deleted_clears_the_pinned_message_if_it_matches():
    live_meta = LiveMeta()
    live_meta.set_featured_message({"id": "pinned-1", "user": "theHost", "text": "Welcome!", "ts": 0, "featured": True})

    _dispatch_one({"typename": "ChatMessageDeleted", "messageId": "pinned-1", "serverCreatedAt": 0}, live_meta=live_meta)

    assert live_meta.featured_message is None


def test_chat_message_deleted_leaves_a_different_pinned_message_alone():
    live_meta = LiveMeta()
    live_meta.set_featured_message({"id": "pinned-1", "user": "theHost", "text": "Welcome!", "ts": 0, "featured": True})

    _dispatch_one({"typename": "ChatMessageDeleted", "messageId": "some-other-id", "serverCreatedAt": 0}, live_meta=live_meta)

    assert live_meta.featured_message is not None
    assert live_meta.featured_message["id"] == "pinned-1"
