from tunnel import _URL_RE


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
