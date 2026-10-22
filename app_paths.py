from __future__ import annotations

import os
import sys


def app_support_dir() -> str:
    """Where a packaged (PyInstaller) app keeps its own bookkeeping (queue
    state, settings, failed-job logs) - the frozen bundle's own directory
    isn't writable/persistent once installed, so this only ever engages
    inside a real build (sys.frozen is never set for a normal `uvicorn
    app:app` dev run, on any OS)."""
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "eBay Live Tools")
