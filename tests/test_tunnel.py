import threading
import time

import tunnel
from tunnel import _REGISTERED_RE, _URL_RE


def test_url_regex_matches_cloudflared_boxed_output():
    line = "2026-10-23T10:00:00Z |  https://random-words-here.trycloudflare.com  |"
    match = _URL_RE.search(line)
    assert match is not None
    assert match.group(0) == "https://random-words-here.trycloudflare.com"


def test_url_regex_ignores_unrelated_lines():
    assert _URL_RE.search("Thank you for trying Cloudflare Tunnel.") is None
    assert _URL_RE.search("https://dash.cloudflare.com/argotunnel") is None


def test_url_regex_matches_with_hyphens_and_digits():
    line = "https://abc-123-xyz9.trycloudflare.com"
    match = _URL_RE.search(line)
    assert match is not None
    assert match.group(0) == line


def test_registered_regex_matches_real_cloudflared_log_line():
    line = (
        "2026-10-22T23:31:19Z INF Registered tunnel connection "
        "connIndex=0 connection=c546ebdb-a341-48e8-bedc-1cc53f87b2ea event=0 "
        "ip=198.41.200.63 location=sjc08 protocol=http2"
    )
    assert _REGISTERED_RE.search(line) is not None


def test_registered_regex_ignores_the_url_banner_line():
    assert _REGISTERED_RE.search("Your quick Tunnel has been created! Visit it at...") is None


def _reset_tunnel_state():
    tunnel._tunnel_url = None
    tunnel._tunnel_error = None
    tunnel._start_thread = None
    tunnel._start_done.clear()


def test_a_caller_that_gives_up_does_not_block_a_later_caller(monkeypatch):
    """Reproduces the real bug this was fixed for: a caller with a short
    wait_timeout (like live/session.py's "is it ready yet" gate giving up
    after 30s) must not leave anything held that blocks a second caller
    (like a direct button click soon after) for however long the
    in-progress startup takes - that second caller should get the real
    result as soon as it's ready, not get stuck waiting on the first."""
    _reset_tunnel_state()
    try:
        def fake_slow_startup():
            time.sleep(0.5)
            tunnel._tunnel_url = "https://fake.trycloudflare.com"
            tunnel._start_done.set()

        monkeypatch.setattr(tunnel, "_run_tunnel_startup", fake_slow_startup)

        # First caller gives up quickly, like the live-event gate does.
        start = time.monotonic()
        try:
            tunnel.get_or_start_tunnel_url(wait_timeout=0.1)
            assert False, "expected a timeout error"
        except RuntimeError as e:
            assert "still starting" in str(e)
        assert time.monotonic() - start < 0.3  # gave up quickly, not stuck for the full 0.5s startup

        # Second caller (e.g. a button click) arrives while that startup is
        # still running in the background - it should pick up the SAME
        # in-progress attempt and get the real result once ready, not
        # restart it or block for its own full wait_timeout.
        start = time.monotonic()
        url = tunnel.get_or_start_tunnel_url(wait_timeout=5.0)
        assert url == "https://fake.trycloudflare.com"
        assert time.monotonic() - start < 1.0  # got the result once ready, not after waiting the full 5s
    finally:
        _reset_tunnel_state()


def test_concurrent_callers_do_not_start_duplicate_tunnels(monkeypatch):
    call_count = {"n": 0}

    _reset_tunnel_state()
    try:
        def fake_slow_startup():
            call_count["n"] += 1
            time.sleep(0.3)
            tunnel._tunnel_url = "https://fake.trycloudflare.com"
            tunnel._start_done.set()

        monkeypatch.setattr(tunnel, "_run_tunnel_startup", fake_slow_startup)

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(tunnel.get_or_start_tunnel_url(wait_timeout=5.0)))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert call_count["n"] == 1
        assert results == ["https://fake.trycloudflare.com"] * 5
    finally:
        _reset_tunnel_state()
