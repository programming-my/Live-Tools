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
