import asyncio
from datetime import datetime, timedelta, timezone

from overlay.protocol import (
    MAX_CONSECUTIVE_WINDOW_FAILURES,
    MIN_CHUNK_SECONDS,
    _FailureTracker,
    _fetch_category,
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
            ws=None, common_headers={}, event_id="evt", message_types=["X"],
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
    # Bounded well below what unrestrained recursive halving across 70
    # minutes (down to MIN_CHUNK_SECONDS granularity) would have produced.
    assert call_count["n"] <= MAX_CONSECUTIVE_WINDOW_FAILURES + 5


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
            ws=None, common_headers={}, event_id="evt", message_types=["X"],
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
            ws=None, common_headers={}, event_id="evt", message_types=["X"],
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
            ws=None, common_headers={}, event_id="evt", message_types=["X"],
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
