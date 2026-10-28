import asyncio
import time
from datetime import datetime, timedelta, timezone

import websockets

from overlay.protocol import (
    CONNECTION_CLOSED_PREFIX,
    MAX_CONCURRENT_WINDOWS,
    MAX_CONSECUTIVE_WINDOW_FAILURES,
    MIN_CHUNK_SECONDS,
    _FailureTracker,
    _Multiplexer,
    _fetch_category,
    _is_connection_closed,
)


def test_failure_tracker_aborts_once_past_threshold():
    tracker = _FailureTracker(max_consecutive=3)
    assert tracker.record_failure() is False
    assert tracker.record_failure() is False
    assert tracker.record_failure() is True  # crosses the threshold here
    assert tracker.aborted is True
    # Doesn't keep re-announcing the abort on every subsequent failure.
    assert tracker.record_failure() is False


def test_failure_tracker_resets_on_success():
    tracker = _FailureTracker(max_consecutive=3)
    tracker.record_failure()
    tracker.record_failure()
    tracker.record_success()
    assert tracker.consecutive == 0
    assert tracker.record_failure() is False
    assert tracker.record_failure() is False


def _run_fetch_category(fetch_window_impl, window_minutes=70, chunk_seconds=90, tracker=None, should_stop=None):
    import overlay.protocol as protocol

    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    protocol._fetch_window = fetch_window_impl
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0  # don't actually wait in tests
    try:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=window_minutes)
        logs = []
        tracker = tracker or _FailureTracker()
        messages = asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=chunk_seconds,
            on_log=logs.append, tracker=tracker, should_stop=should_stop,
        ))
        return messages, logs, tracker
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay


def test_fetch_category_stops_early_after_sustained_failures():
    """Every window times out (simulating eBay's backend going silent
    partway through a long event, as seen in production) - the circuit
    breaker should cut this off well before grinding through every
    ever-smaller sub-window across the full ~70 minute range."""
    call_count = {"n": 0}

    async def always_times_out(*args, **kwargs):
        call_count["n"] += 1
        return [], "timeout"

    messages, logs, tracker = _run_fetch_category(always_times_out, window_minutes=70)

    assert messages == []
    assert tracker.aborted is True
    assert any("connection appears to have stopped responding" in msg for msg in logs)
    # Bounded well below what unrestrained recursive halving across the full
    # 70 minutes (down to MIN_CHUNK_SECONDS granularity, each with its own
    # full retry+pad budget) would cost - retries no longer count toward
    # the threshold itself (see MAX_CONSECUTIVE_WINDOW_FAILURES's docstring),
    # so more distinct window positions get tried before tripping than
    # before, but it's still a small fraction of the whole event.
    assert call_count["n"] < 300


def test_fetch_category_recovers_from_a_few_isolated_failures():
    """A handful of scattered failures that each then succeed on the next
    window should NOT trip the circuit breaker - only a sustained run of
    consecutive failures should."""
    calls = {"n": 0}

    async def fails_then_succeeds(*args, **kwargs):
        calls["n"] += 1
        # Fail once every 4th call, succeed otherwise - never MAX_CONSECUTIVE
        # failures in a row.
        if calls["n"] % 4 == 0:
            return [], "timeout"
        return [{"ok": True}], None

    messages, logs, tracker = _run_fetch_category(fails_then_succeeds, window_minutes=20, chunk_seconds=300)

    assert tracker.aborted is False
    assert len(messages) > 0


def test_fetch_category_reports_increasing_progress():
    """Progress should move forward even while individual windows are
    failing and getting split smaller - that's exactly the scenario (a
    troubled stretch of an event) where it previously looked indistinguishable
    from a genuine hang."""
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        # Every 3rd call fails (and gets split/retried), rest succeed.
        if calls["n"] % 3 == 0:
            return [], "timeout"
        return [{"ok": True}], None

    progress_values = []
    # The _run_fetch_category helper above doesn't thread on_progress
    # through, so call _fetch_category directly here instead.
    import overlay.protocol as protocol
    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    protocol._fetch_window = flaky
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0
    try:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=20)
        asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=300,
            on_log=lambda m: None, tracker=_FailureTracker(), should_stop=None,
            on_progress=progress_values.append,
        ))
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay

    assert len(progress_values) > 1
    assert progress_values[-1] == 1.0
    # Monotonically non-decreasing - progress should never go backwards.
    assert all(a <= b for a, b in zip(progress_values, progress_values[1:]))


def test_fetch_category_recovers_a_minimum_chunk_window_via_retry():
    """A window that fails even after being split all the way down to
    MIN_CHUNK_SECONDS should still get a few extra retries before being
    given up on - and if one of those succeeds, its messages must be kept
    and it must NOT be recorded as a lost range (this is the fix for a real
    render where a dropped window caused a whole auction to vanish and
    another to show a stale, too-low final price)."""
    calls = {"n": 0}

    async def fails_twice_then_recovers(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return [], "timeout"
        return [{"ok": True}], None

    lost_ranges: list = []
    import overlay.protocol as protocol
    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    protocol._fetch_window = fails_twice_then_recovers
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0
    try:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(seconds=protocol.MIN_CHUNK_SECONDS)
        logs = []
        messages = asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=protocol.MIN_CHUNK_SECONDS,
            on_log=logs.append, tracker=_FailureTracker(), lost_ranges=lost_ranges,
        ))
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay

    assert messages == [{"ok": True}]
    assert lost_ranges == []
    assert any("recovered on retry" in msg for msg in logs)


def test_fetch_category_records_lost_range_when_retries_all_fail():
    """A window that still fails every retry should be recorded in
    lost_ranges exactly once, with a clear log message - not silently
    dropped (the pre-existing behavior that caused real data loss)."""
    import overlay.protocol as protocol
    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    original_attempts = protocol.MIN_CHUNK_RETRY_ATTEMPTS

    async def always_times_out(*args, **kwargs):
        return [], "timeout"

    protocol._fetch_window = always_times_out
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0
    protocol.MIN_CHUNK_RETRY_ATTEMPTS = 2  # keep the tracker's consecutive count low
    try:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(seconds=protocol.MIN_CHUNK_SECONDS)
        logs = []
        lost_ranges: list = []
        tracker = _FailureTracker(max_consecutive=100)  # isolate from the circuit breaker for this test
        messages = asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=protocol.MIN_CHUNK_SECONDS,
            on_log=logs.append, tracker=tracker, lost_ranges=lost_ranges,
        ))
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay
        protocol.MIN_CHUNK_RETRY_ATTEMPTS = original_attempts

    assert messages == []
    assert lost_ranges == [(start, end)]
    assert any("giving up on this slice" in msg for msg in logs)


def test_fetch_category_recovers_via_padded_retry_when_exact_window_persistently_fails():
    """A window that fails every exact-boundary retry (simulating a
    boundary-specific backend issue rather than a transient one - a real
    event showed a window fail every retry over 165s, twice, and still
    never recover) should still recover via one final padded-window
    attempt, with the response correctly filtered back down to only
    messages inside the originally-requested range (not the padding)."""
    import overlay.protocol as protocol

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=protocol.MIN_CHUNK_SECONDS)

    def _ms(dt):
        return int(dt.timestamp() * 1000)

    async def fails_exact_succeeds_padded(*args, **kwargs):
        start_iso, end_iso = args[5], args[6]
        requested_start = protocol._parse_iso(start_iso)
        requested_end = protocol._parse_iso(end_iso)
        span = (requested_end - requested_start).total_seconds()
        if span <= protocol.MIN_CHUNK_SECONDS + 0.001:
            return [], "timeout"
        # A padded (wider) request - return one message inside the
        # originally-requested range and one in the padding on each side,
        # to verify the response gets filtered back down correctly.
        return [
            {"serverCreatedAt": _ms(start - timedelta(seconds=4)), "typename": "InPadding"},
            {"serverCreatedAt": _ms(start + timedelta(seconds=5)), "typename": "InRange"},
            {"serverCreatedAt": _ms(end + timedelta(seconds=4)), "typename": "InPadding"},
        ], None

    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    original_attempts = protocol.MIN_CHUNK_RETRY_ATTEMPTS
    protocol._fetch_window = fails_exact_succeeds_padded
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0
    protocol.MIN_CHUNK_RETRY_ATTEMPTS = 2
    try:
        logs = []
        lost_ranges: list = []
        messages = asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=protocol.MIN_CHUNK_SECONDS,
            on_log=logs.append, tracker=_FailureTracker(), lost_ranges=lost_ranges,
        ))
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay
        protocol.MIN_CHUNK_RETRY_ATTEMPTS = original_attempts

    assert len(messages) == 1
    assert messages[0]["typename"] == "InRange"
    assert lost_ranges == []
    assert any("recovered via a padded retry" in msg for msg in logs)


def test_is_connection_closed_recognizes_the_prefix():
    assert _is_connection_closed(f"{CONNECTION_CLOSED_PREFIX}ConnectionClosedError(...)") is True
    assert _is_connection_closed("timeout - no frames of any kind arrived") is False
    assert _is_connection_closed(None) is False
    assert _is_connection_closed("") is False


def test_fetch_category_stops_immediately_on_connection_closed_without_retrying():
    """A real event crashed the whole fetch_full_history call (losing over
    10,000 already-fetched events) when the websocket itself closed deep
    into a retry sequence, because the resulting exception was never
    caught. _fetch_window now reports this as a CONNECTION_CLOSED_PREFIX-ed
    error instead of raising - _fetch_category must recognize it and abort
    immediately (not retry/split/pad a connection that's gone), returning
    whatever was already gathered."""
    calls = {"n": 0}

    async def succeeds_then_connection_closes(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"ok": True}], None
        return [], f"{CONNECTION_CLOSED_PREFIX}ConnectionClosedError(...)"

    messages, logs, tracker = _run_fetch_category(succeeds_then_connection_closes, window_minutes=70, chunk_seconds=90)

    assert messages == [{"ok": True}]
    assert tracker.aborted is True
    assert any("the connection itself closed" in msg for msg in logs)
    # Didn't keep calling _fetch_window over and over after the connection closed.
    assert calls["n"] <= 3


def test_two_adjacent_stubborn_windows_dont_abort_the_rest_of_a_healthy_fetch():
    """The actual regression this encodes: a real export lost the last ~46
    minutes of an otherwise perfectly healthy event because two adjacent
    windows that were genuinely unrecoverable (a confirmed eBay-side bug,
    not a dead connection) each needed enough retries that their combined
    failure count alone crossed the circuit breaker's old threshold,
    aborting everything after them. Two always-failing windows followed by
    hours of otherwise-successful ones must NOT trip the breaker - both bad
    windows should land in lost_ranges, and everything after them must
    still be fetched."""
    import overlay.protocol as protocol

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(hours=4)
    stubborn_start = start + timedelta(minutes=190)  # near the far end of the range
    stubborn_end = stubborn_start + timedelta(seconds=2 * protocol.MIN_CHUNK_SECONDS)

    async def fails_only_in_the_stubborn_range(*args, **kwargs):
        start_iso, end_iso = args[5], args[6]
        req_start = protocol._parse_iso(start_iso)
        if stubborn_start <= req_start < stubborn_end:
            return [], "timeout"
        return [{"ok": True}], None

    original = protocol._fetch_window
    original_delay = protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS
    protocol._fetch_window = fails_only_in_the_stubborn_range
    protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = 0
    try:
        logs = []
        lost_ranges: list = []
        tracker = _FailureTracker()
        messages = asyncio.run(_fetch_category(
            mux=None, common_headers={}, event_id="evt", message_types=["X"],
            window_start=start, window_end=end, chunk_seconds=90,
            on_log=logs.append, tracker=tracker, lost_ranges=lost_ranges,
        ))
    finally:
        protocol._fetch_window = original
        protocol.MIN_CHUNK_RETRY_INITIAL_DELAY_SECONDS = original_delay

    assert tracker.aborted is False
    assert len(lost_ranges) >= 1
    # The bulk of a 4-hour event (minus ~40s of genuinely-lost data) must
    # still come through, including everything AFTER the stubborn pair.
    assert len(messages) > 100


def test_fetch_category_respects_should_stop():
    calls = {"n": 0}

    async def always_succeeds(*args, **kwargs):
        calls["n"] += 1
        return [{"ok": True}], None

    def should_stop():
        return calls["n"] >= 2

    messages, logs, tracker = _run_fetch_category(
        always_succeeds, window_minutes=70, chunk_seconds=90, should_stop=should_stop,
    )

    assert any("Stopped by user" in msg for msg in logs)
    # Stopped well short of covering the full 70 minutes at 90s/window (~47 windows).
    assert calls["n"] < 10


# ---------------------------------------------------------------------------
# _Multiplexer: the concurrent request/response dispatcher. A single fake
# websocket (fake send/recv, no real network) stands in for the real
# connection, with full control over when/what "arrives" so these can
# directly exercise out-of-order responses, unmatched frames, and
# ConnectionClosed - the exact hazards concurrent requests on one shared
# connection actually have to handle correctly.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 (kept local to this section, used only here)


def _message_frame(request_id: str, **extra_payload_fields) -> str:
    payload = {"requestId": request_id, "typename": "Result", **extra_payload_fields}
    envelope = {"specVersion": "1.22.1", "payload": payload}
    return f"MESSAGE\n\n{_json.dumps(envelope)}\x00"


class _FakeWebSocket:
    """Fake matching just the send/recv surface _Multiplexer actually uses."""

    def __init__(self):
        self.sent: list = []
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def push(self, frame_text: str) -> None:
        await self._incoming.put(frame_text)

    async def close_with(self, exc: Exception) -> None:
        await self._incoming.put(exc)


async def _with_multiplexer(body):
    ws = _FakeWebSocket()
    mux = _Multiplexer(ws)
    mux.start()
    try:
        return await body(ws, mux)
    finally:
        await mux.stop()


def test_multiplexer_routes_responses_to_the_right_caller_regardless_of_arrival_order():
    """The whole point of the multiplexer: two concurrent requests on the
    same connection must each get their OWN response even when the
    responses arrive in the opposite order from how the requests were sent
    - this is exactly what made naive concurrent fetching unsafe before
    (each caller previously did its own blocking send+recv on the shared
    connection, which would have raced)."""

    async def body(ws, mux):
        task_a = asyncio.ensure_future(mux.request("frame-a", "a", timeout=2))
        task_b = asyncio.ensure_future(mux.request("frame-b", "b", timeout=2))
        await asyncio.sleep(0)  # let both requests actually reach the fake send
        await ws.push(_message_frame("b", result="B"))
        await ws.push(_message_frame("a", result="A"))
        return await task_a, await task_b

    (payload_a, diag_a), (payload_b, diag_b) = asyncio.run(_with_multiplexer(body))
    assert payload_a["result"] == "A" and diag_a == ""
    assert payload_b["result"] == "B" and diag_b == ""


def test_multiplexer_ignores_frames_for_other_or_unknown_request_ids():
    async def body(ws, mux):
        req_task = asyncio.ensure_future(mux.request("frame", "real-id", timeout=2))
        await asyncio.sleep(0)
        await ws.push(_message_frame("someone-elses-id", result="not for us"))
        await ws.push(_message_frame("real-id", result="ours"))
        return await req_task

    payload, diagnostic = asyncio.run(_with_multiplexer(body))
    assert payload["result"] == "ours"
    assert diagnostic == ""


def test_multiplexer_times_out_cleanly_when_nothing_matches():
    async def body(ws, mux):
        return await mux.request("frame", "id", timeout=0.05)

    payload, diagnostic = asyncio.run(_with_multiplexer(body))
    assert payload is None
    assert diagnostic == "timeout"


def test_multiplexer_fails_all_pending_requests_on_connection_closed():
    """A closed connection must fail EVERY outstanding request, not just
    the next one to try reading - otherwise a concurrent caller could hang
    forever waiting for a reply that can now never arrive."""

    async def body(ws, mux):
        task_a = asyncio.ensure_future(mux.request("frame-a", "a", timeout=5))
        task_b = asyncio.ensure_future(mux.request("frame-b", "b", timeout=5))
        await asyncio.sleep(0)
        await ws.close_with(websockets.ConnectionClosed(None, None))
        return await task_a, await task_b

    (payload_a, diag_a), (payload_b, diag_b) = asyncio.run(_with_multiplexer(body))
    assert payload_a is None and _is_connection_closed(diag_a)
    assert payload_b is None and _is_connection_closed(diag_b)


# ---------------------------------------------------------------------------
# _fetch_category's actual concurrency: bounded in-flight count, and a real
# (small) wall-clock speedup over doing the same work sequentially.
# ---------------------------------------------------------------------------

def test_fetch_category_bounds_concurrent_in_flight_requests():
    """Never more than MAX_CONCURRENT_WINDOWS requests in flight at once,
    even with many more windows than that queued up."""
    in_flight = {"current": 0, "max_seen": 0}

    async def tracked(*args, **kwargs):
        in_flight["current"] += 1
        in_flight["max_seen"] = max(in_flight["max_seen"], in_flight["current"])
        await asyncio.sleep(0.01)
        in_flight["current"] -= 1
        return [{"ok": True}], None

    # Many more windows (20 min / 10s = 120) than MAX_CONCURRENT_WINDOWS.
    _run_fetch_category(tracked, window_minutes=20, chunk_seconds=10)

    assert in_flight["max_seen"] <= MAX_CONCURRENT_WINDOWS
    assert in_flight["max_seen"] > 1  # actually ran concurrently, not one at a time


def test_fetch_category_concurrent_fetch_is_faster_than_sequential_would_be():
    """With a fixed per-window delay, fetching N windows concurrently
    (bounded by MAX_CONCURRENT_WINDOWS) must take meaningfully less wall
    time than N * delay (what strictly one-at-a-time fetching, the
    previous design, would cost)."""
    delay = 0.05
    window_minutes = 20
    chunk_seconds = 10
    num_windows = (window_minutes * 60) // chunk_seconds  # 120

    async def delayed_success(*args, **kwargs):
        await asyncio.sleep(delay)
        return [{"ok": True}], None

    start_time = time.monotonic()
    messages, _logs, _tracker = _run_fetch_category(delayed_success, window_minutes=window_minutes, chunk_seconds=chunk_seconds)
    elapsed = time.monotonic() - start_time

    sequential_estimate = num_windows * delay
    assert len(messages) == num_windows
    # Comfortably faster than sequential - not a tight bound, just proof
    # real concurrency is happening rather than one request at a time.
    assert elapsed < sequential_estimate / 2


def test_fetch_full_history_runs_both_categories_concurrently():
    """Integration-level proof (not just _fetch_category in isolation) that
    fetch_full_history's own wiring - creating the _Multiplexer, starting
    it, running auctions and pin-updates via asyncio.gather, stopping it -
    actually runs the two categories concurrently rather than one after the
    other, and doesn't hang or deadlock doing so. bootstrap_session and the
    websocket connection itself are faked out (no real network/browser);
    only _fetch_category is faked, with an artificial delay standing in for
    real network latency."""
    import overlay.protocol as protocol

    class _FakeConnectCtx:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *_args):
            return False

    class _HandshakeThenBlockWS:
        """Returns a dummy reply to the one handshake recv(), then blocks
        (really yields, not busy-spins) forever after - nothing else needs
        to arrive for this test, since _fetch_category itself is faked."""

        def __init__(self):
            self._queue: asyncio.Queue = asyncio.Queue()
            self._recv_count = 0

        async def send(self, data):
            pass

        async def recv(self):
            self._recv_count += 1
            if self._recv_count == 1:
                return "CONNECTED\n\n"
            return await self._queue.get()

    delay = 0.1
    original_bootstrap = protocol.bootstrap_session
    original_connect = protocol.websockets.connect
    original_fetch_category = protocol._fetch_category
    original_keystone = protocol._fetch_keystone_snapshot

    async def fake_fetch_category(mux, common_headers, event_id, message_types, *args, **kwargs):
        await asyncio.sleep(delay)
        return [{"typename": "fake", "category": message_types}]

    async def fake_keystone(*args, **kwargs):
        return []

    protocol.bootstrap_session = lambda event_url: {
        "event_id": "evt", "token": "tok", "guid": "g",
        "start_time": "2025-01-01T00:00:00Z", "end_time": "2025-01-01T00:01:00Z",
    }
    protocol.websockets.connect = lambda *a, **k: _FakeConnectCtx(_HandshakeThenBlockWS())
    protocol._fetch_category = fake_fetch_category
    protocol._fetch_keystone_snapshot = fake_keystone
    try:
        start_time = time.monotonic()
        result = protocol.fetch_full_history("https://www.ebay.com/ebaylive/events/evt/stream")
        elapsed = time.monotonic() - start_time
    finally:
        protocol.bootstrap_session = original_bootstrap
        protocol.websockets.connect = original_connect
        protocol._fetch_category = original_fetch_category
        protocol._fetch_keystone_snapshot = original_keystone

    assert len(result["auctions"]) == 1
    assert len(result["pin_updates"]) == 1
    # One category's worth of delay, not two - proves asyncio.gather is
    # actually running them concurrently rather than one after the other.
    assert elapsed < delay * 1.8
