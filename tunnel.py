from __future__ import annotations

import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Optional

import requests

EDGE_PORT = 8766

_CLOUDFLARED_NAME = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
if getattr(sys, "frozen", False):
    _bundled_cloudflared = os.path.join(sys._MEIPASS, _CLOUDFLARED_NAME)
    CLOUDFLARED_BIN = _bundled_cloudflared if os.path.exists(_bundled_cloudflared) else shutil.which("cloudflared")
else:
    CLOUDFLARED_BIN = shutil.which("cloudflared")

_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
# cloudflared prints the public URL as soon as it's assigned, but its own
# banner warns "it may take some time to be reachable" - this later log line
# is the real signal that the tunnel connection is actually registered.
_REGISTERED_RE = re.compile(r"Registered tunnel connection")

# How long the actual cloudflared startup attempt itself gets to succeed,
# regardless of how long any one caller of get_or_start_tunnel_url() is
# willing to wait for a result - see that function's docstring for why
# these two things used to be (wrongly) the same number.
_STARTUP_TIMEOUT = 90.0

_state_lock = threading.Lock()
_process: Optional[subprocess.Popen] = None
_tunnel_url: Optional[str] = None
_tunnel_error: Optional[str] = None
_start_thread: Optional[threading.Thread] = None
_start_done = threading.Event()


def _pump_lines_into_queue(stream, line_queue: "queue.Queue[str]") -> None:
    for line in stream:
        line_queue.put(line)


def _dns_resolves_raw(hostname: str, resolver: str = "1.1.1.1", timeout: float = 2.0) -> bool:
    """Queries a public resolver directly over raw UDP (hand-rolled DNS wire
    protocol, A-record query) instead of going through socket.getaddrinfo()/
    the OS's own stub resolver - the OS resolver (e.g. macOS's
    mDNSResponder) can cache an early failed lookup for a brand new
    *.trycloudflare.com subdomain for far longer than the record actually
    takes to become live, which `dig`/`host` don't hit (they bypass that
    cache) but `requests`/`socket.getaddrinfo` do. Querying this way first
    means the OS resolver's own first-ever lookup for this hostname only
    happens once the record is confirmed live elsewhere, so it isn't the one
    caching a stale negative result. (Not DNS-over-HTTPS - some networks
    block known DoH endpoints outright; this is identical in shape to any
    other DNS traffic.)"""
    transaction_id = os.urandom(2)
    header = transaction_id + b"\x01\x00" + b"\x00\x01\x00\x00\x00\x00\x00\x00"
    question = b"".join(bytes([len(part)]) + part.encode() for part in hostname.split(".")) + b"\x00"
    question += b"\x00\x01\x00\x01"  # QTYPE=A, QCLASS=IN
    packet = header + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (resolver, 53))
            response, _ = sock.recvfrom(512)
    except OSError:
        return False
    if len(response) < 12 or response[:2] != transaction_id:
        return False
    rcode = struct.unpack("!H", response[2:4])[0] & 0x000F
    ancount = struct.unpack("!H", response[6:8])[0]
    return rcode == 0 and ancount > 0


def _wait_until_reachable(url: str, timeout: float) -> bool:
    """Even after cloudflared reports its connection registered, the edge
    can take another moment to actually become resolvable/routable -
    anywhere from well under a second to tens of seconds in practice."""
    hostname = url.split("://", 1)[-1].split("/", 1)[0]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _dns_resolves_raw(hostname):
            break
        time.sleep(0.5)
    else:
        return False

    while time.monotonic() < deadline:
        try:
            requests.get(url, timeout=3)
            return True
        except requests.RequestException:
            time.sleep(0.5)
    return False


def _run_tunnel_startup() -> None:
    """Runs in its own always-background thread, entirely independent of
    whichever caller happened to trigger it - see get_or_start_tunnel_url's
    docstring. Writes the result to _tunnel_url/_tunnel_error and signals
    _start_done exactly once, however long it actually takes (up to
    _STARTUP_TIMEOUT), regardless of anyone's patience."""
    global _process, _tunnel_url, _tunnel_error
    try:
        if not CLOUDFLARED_BIN:
            _tunnel_error = (
                "cloudflared was not found. Install it (e.g. 'brew install cloudflared') "
                "to use shareable Talent View / OBS links."
            )
            return

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

        candidate_url: Optional[str] = None
        deadline = time.monotonic() + _STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = line_queue.get(timeout=max(remaining, 0))
            except queue.Empty:
                break
            if candidate_url is None:
                match = _URL_RE.search(line)
                if match:
                    candidate_url = match.group(0)
            if candidate_url and _REGISTERED_RE.search(line):
                remaining = deadline - time.monotonic()
                if _wait_until_reachable(candidate_url, timeout=max(remaining, 10.0)):
                    _tunnel_url = candidate_url
                    return
                break
            if _process.poll() is not None:
                break

        stop_tunnel()
        _tunnel_error = "Timed out waiting for the tunnel to start."
    finally:
        _start_done.set()


def get_or_start_tunnel_url(wait_timeout: float = 30.0) -> str:
    """Returns the cached tunnel URL if one's already up. If a startup
    attempt is already running (triggered by a concurrent caller), waits up
    to wait_timeout for it to finish rather than starting a second
    cloudflared process - and, critically, rather than blocking on a lock
    held for however long THAT attempt takes.

    This used to hold a single lock for the entire (up to 90s) startup
    process, so a slow start left every other caller stuck waiting for
    however long was left of it - including a direct button click well
    after "live" already showed (live/session.py's own gate gives up
    waiting after 30s and lets "live" show regardless, but the startup
    attempt it kicked off keeps running in the background) - which looked
    like the button being stuck on "Starting tunnel..." forever. Now every
    caller - the gate or a button click - waits only as long as it
    personally chooses to, on a shared result any of them may have kicked
    off, instead of waiting on each other.
    """
    with _state_lock:
        if _tunnel_url:
            return _tunnel_url
        global _start_thread
        if _start_thread is None or not _start_thread.is_alive():
            _start_done.clear()
            _start_thread = threading.Thread(target=_run_tunnel_startup, daemon=True)
            _start_thread.start()

    if not _start_done.wait(timeout=wait_timeout):
        raise RuntimeError("The sharing tunnel is still starting - try again in a few seconds.")

    with _state_lock:
        if _tunnel_url:
            return _tunnel_url
        raise RuntimeError(_tunnel_error or "Timed out waiting for the tunnel to start.")


def get_status() -> dict:
    """Non-blocking status check for a UI indicator - unlike
    get_or_start_tunnel_url(), never waits, and doesn't start anything
    (there's nothing to lazily start now that app.py kicks this off at
    launch - see _start_tunnel_at_launch). One of:
    - {"status": "idle"} - hasn't been started yet (shouldn't normally be
      seen once the app's finished launching).
    - {"status": "starting"} - in progress.
    - {"status": "connected", "url": "..."} - ready to use.
    - {"status": "error", "detail": "..."} - the last attempt failed; a
      real call to get_or_start_tunnel_url() will retry.
    """
    with _state_lock:
        if _tunnel_url:
            return {"status": "connected", "url": _tunnel_url}
        if _start_thread is not None and _start_thread.is_alive():
            return {"status": "starting"}
        if _tunnel_error:
            return {"status": "error", "detail": _tunnel_error}
        return {"status": "idle"}


def stop_tunnel() -> None:
    global _process, _tunnel_url
    if _process is not None:
        _process.terminate()
        _process = None
    _tunnel_url = None
