from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

EDGE_PORT = 8766

_CLOUDFLARED_NAME = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
if getattr(sys, "frozen", False):
    _bundled_cloudflared = os.path.join(sys._MEIPASS, _CLOUDFLARED_NAME)
    CLOUDFLARED_BIN = _bundled_cloudflared if os.path.exists(_bundled_cloudflared) else shutil.which("cloudflared")
else:
    CLOUDFLARED_BIN = shutil.which("cloudflared")

_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None
_tunnel_url: Optional[str] = None


def _pump_lines_into_queue(stream, line_queue: "queue.Queue[str]") -> None:
    for line in stream:
        line_queue.put(line)


def get_or_start_tunnel_url(timeout: float = 20.0) -> str:
    """Starts a cloudflared quick tunnel to the edge server (see app.py's
    edge_app / desktop_launcher.py's EDGE_PORT server) on first call, and
    returns its public https://*.trycloudflare.com URL - cached and reused
    for every call after the first, for the lifetime of this process."""
    global _process, _tunnel_url
    with _lock:
        if _tunnel_url:
            return _tunnel_url
        if not CLOUDFLARED_BIN:
            raise RuntimeError(
                "cloudflared was not found. Install it (e.g. 'brew install cloudflared') "
                "to use shareable Talent View / OBS links."
            )

        _process = subprocess.Popen(
            [CLOUDFLARED_BIN, "tunnel", "--protocol", "http2", "--url", f"http://127.0.0.1:{EDGE_PORT}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        # Read cloudflared's output from a background thread into a queue so
        # we can enforce a real timeout - a direct blocking readline() loop
        # here could hang indefinitely if cloudflared never prints the URL.
        line_queue: "queue.Queue[str]" = queue.Queue()
        reader = threading.Thread(target=_pump_lines_into_queue, args=(_process.stdout, line_queue), daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = line_queue.get(timeout=max(remaining, 0))
            except queue.Empty:
                break
            match = _URL_RE.search(line)
            if match:
                _tunnel_url = match.group(0)
                return _tunnel_url
            if _process.poll() is not None:
                break

        stop_tunnel()
        raise RuntimeError("Timed out waiting for the tunnel to start.")


def stop_tunnel() -> None:
    global _process, _tunnel_url
    if _process is not None:
        _process.terminate()
        _process = None
    _tunnel_url = None
