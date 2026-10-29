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
