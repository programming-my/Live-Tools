import threading

import pytest
from fastapi import HTTPException

import app


def test_overlay_export_enabled_by_default_when_not_frozen():
    """Export Auction Overlay is dev-server-only for now (see
    app.OVERLAY_EXPORT_ENABLED's own comment) - running from source (as
    these tests do) should leave it enabled."""
    assert app.OVERLAY_EXPORT_ENABLED is True


def test_require_overlay_export_enabled_passes_when_enabled():
    original = app.OVERLAY_EXPORT_ENABLED
    app.OVERLAY_EXPORT_ENABLED = True
    try:
        app._require_overlay_export_enabled()  # must not raise
    finally:
        app.OVERLAY_EXPORT_ENABLED = original


def test_require_overlay_export_enabled_raises_404_when_disabled():
    """This is what actually keeps the feature out of a packaged build,
    not just the UI hiding its section - the route itself refuses to work
    regardless of what hits it."""
    original = app.OVERLAY_EXPORT_ENABLED
    app.OVERLAY_EXPORT_ENABLED = False
    try:
        with pytest.raises(HTTPException) as exc_info:
            app._require_overlay_export_enabled()
        assert exc_info.value.status_code == 404
    finally:
        app.OVERLAY_EXPORT_ENABLED = original


def test_all_overlay_export_routes_are_guarded_when_disabled():
    original = app.OVERLAY_EXPORT_ENABLED
    app.OVERLAY_EXPORT_ENABLED = False
    try:
        with pytest.raises(HTTPException) as exc_info:
            app.start_overlay_export(app.OverlayExportRequest(
                event_url="https://www.ebay.com/ebaylive/events/x/stream", title_query="test",
            ))
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            app.get_overlay_export_status()
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            app.stop_overlay_export()
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            app.clear_overlay_export()
        assert exc_info.value.status_code == 404
    finally:
        app.OVERLAY_EXPORT_ENABLED = original


def test_version_endpoint_reports_the_flag():
    result = app.get_version()
    assert result["overlay_export_enabled"] == app.OVERLAY_EXPORT_ENABLED


@pytest.fixture
def clean_job_state():
    """_run_job mutates the module-level app.state singleton directly (it's
    meant to run in a background thread) - these tests call it synchronously,
    so reset the singleton before and after to avoid bleeding into other
    tests."""
    app.state.reset("https://www.ebay.com/ebaylive/events/x/stream")
    yield
    app.state.reset("https://www.ebay.com/ebaylive/events/x/stream")
    app.state.thread = None


def test_run_job_downloads_when_event_has_ended_with_a_replay(monkeypatch, tmp_path):
    """The core behavior change this test locks in: _run_job no longer polls
    in a loop waiting for the event to end (see monitor.py's now-removed
    monitor_loop) - it checks once, and if the replay is already ready, goes
    straight to downloading it."""
    monkeypatch.setattr(app, "DOWNLOADS_DIR", str(tmp_path))
    monkeypatch.setattr(app, "check_event_status", lambda url: ("ENDED", "https://cdn.example.com/replay.m3u8", None))
    monkeypatch.setattr(app, "fetch_event_start_date", lambda url: None)
    monkeypatch.setattr(app, "download_replay_with_progress", lambda m3u8_url, output_path, on_progress, should_stop: True)

    app.state.reset("https://www.ebay.com/ebaylive/events/x/stream")
    app._run_job("https://www.ebay.com/ebaylive/events/x/stream", threading.Event())

    result = app.state.to_dict()
    assert result["status"] == "DONE"
    assert result["download_filename"]


def test_run_job_sets_not_ended_when_event_has_ended_but_no_replay_yet(monkeypatch, clean_job_state):
    monkeypatch.setattr(app, "check_event_status", lambda url: ("ENDED", None, None))
    download_called = []
    monkeypatch.setattr(
        app, "download_replay_with_progress",
        lambda *a, **k: download_called.append(True) or True,
    )

    app._run_job("https://www.ebay.com/ebaylive/events/x/stream", threading.Event())

    assert app.state.to_dict()["status"] == "NOT_ENDED"
    assert download_called == []


def test_run_job_sets_not_ended_when_event_still_live(monkeypatch, clean_job_state):
    monkeypatch.setattr(app, "check_event_status", lambda url: ("LIVE", None, None))

    app._run_job("https://www.ebay.com/ebaylive/events/x/stream", threading.Event())

    assert app.state.to_dict()["status"] == "NOT_ENDED"


def test_run_job_sets_error_when_check_event_status_fails(monkeypatch, clean_job_state):
    monkeypatch.setattr(app, "check_event_status", lambda url: ("ERROR", None, "connection refused"))

    app._run_job("https://www.ebay.com/ebaylive/events/x/stream", threading.Event())

    assert app.state.to_dict()["status"] == "ERROR"


def test_start_request_no_longer_takes_a_poll_interval():
    """Locks in the removal of the poll-interval concept entirely (see
    README.md/static/index.html) - StartRequest only needs a URL now."""
    assert "poll_interval" not in app.StartRequest.model_fields
    req = app.StartRequest(url="https://www.ebay.com/ebaylive/events/x/stream")
    assert req.url == "https://www.ebay.com/ebaylive/events/x/stream"


def test_clear_job_resets_a_stopped_jobs_status_and_log(monkeypatch, clean_job_state):
    monkeypatch.setattr(app, "check_event_status", lambda url: ("LIVE", None, None))
    app._run_job("https://www.ebay.com/ebaylive/events/x/stream", threading.Event())
    assert app.state.to_dict()["status"] == "NOT_ENDED"

    result = app.clear_job()

    assert result["status"] == "IDLE"
    assert result["url"] is None
    assert result["logs"] == []


def test_clear_job_refuses_while_a_download_is_running(clean_job_state):
    class FakeRunningThread:
        def is_alive(self):
            return True

    app.state.thread = FakeRunningThread()
    try:
        with pytest.raises(HTTPException) as exc_info:
            app.clear_job()
        assert exc_info.value.status_code == 409
    finally:
        app.state.thread = None
