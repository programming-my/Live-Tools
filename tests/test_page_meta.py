import json

from live.auth import extract_page_meta


def _fake_page(apollo_data: dict) -> str:
    blob = json.dumps(apollo_data)
    return f"""
    <html><body>
    <script>window.__APOLLO_DATA__={blob};</script>
    </body></html>
    """


def test_extracts_title_host_username_and_avatar():
    page = _fake_page({
        "User:UsJdyOxlQau": {
            "__typename": "User", "id": "UsJdyOxlQau",
            "profilePictureUrl": "https://i.ebayimg.com/images/g/vYQAAeSwBC5pn9tV/s-l140.webp",
            "userAccountName": "matchfireauctions2c",
        },
        "LiveEvent:evt123": {
            "__typename": "LiveEvent", "id": "evt123",
            "title": "$1 Auctions, Penny Drops and More",
            "hosts": [{"__ref": "User:UsJdyOxlQau"}],
        },
    })

    meta = extract_page_meta(page, "evt123")

    assert meta == {
        "event_title": "$1 Auctions, Penny Drops and More",
        "host_username": "matchfireauctions2c",
        "host_avatar_url": "https://i.ebayimg.com/images/g/vYQAAeSwBC5pn9tV/s-l140.webp",
    }


def test_tolerates_a_null_avatar_url():
    page = _fake_page({
        "User:abc": {
            "__typename": "User", "id": "abc",
            "profilePictureUrl": None,
            "userAccountName": "someseller",
        },
        "LiveEvent:evt123": {
            "__typename": "LiveEvent", "id": "evt123",
            "title": "A stream",
            "hosts": [{"__ref": "User:abc"}],
        },
    })

    meta = extract_page_meta(page, "evt123")

    assert meta["host_username"] == "someseller"
    assert meta["host_avatar_url"] is None


def test_falls_back_to_the_seller_store_logo_when_no_profile_picture_is_set():
    # Confirmed against a real event (2026-09-25): a seller with no personal
    # profilePictureUrl but a real store logo image.
    page = _fake_page({
        "Image:logo1": {"__typename": "Image", "id": "logo1", "url": "https://i.ebayimg.com/logo.png"},
        "User:abc": {
            "__typename": "User", "id": "abc",
            "profilePictureUrl": None,
            "userAccountName": "mrwikd",
            "sellerProfile": {
                "__typename": "SellerProfile",
                "sellerStore": {"__typename": "SellerStore", "name": "MR WIKD", "logo": {"__ref": "Image:logo1"}},
            },
        },
        "LiveEvent:evt123": {
            "__typename": "LiveEvent", "id": "evt123", "title": "A stream",
            "hosts": [{"__ref": "User:abc"}],
        },
    })

    meta = extract_page_meta(page, "evt123")

    assert meta["host_username"] == "mrwikd"
    assert meta["host_avatar_url"] == "https://i.ebayimg.com/logo.png"


def test_falls_back_to_any_live_event_key_if_the_exact_id_is_missing():
    # Guards against an id-format mismatch between the URL-derived event_id
    # and however the page's cache actually keys it - cheap insurance since
    # this whole path is best-effort anyway.
    page = _fake_page({
        "User:abc": {"__typename": "User", "id": "abc", "profilePictureUrl": None, "userAccountName": "someseller"},
        "LiveEvent:someOtherId": {
            "__typename": "LiveEvent", "id": "someOtherId", "title": "A stream",
            "hosts": [{"__ref": "User:abc"}],
        },
    })

    meta = extract_page_meta(page, "evt123")

    assert meta["event_title"] == "A stream"
    assert meta["host_username"] == "someseller"


def test_returns_all_none_when_no_apollo_data_script_is_present():
    meta = extract_page_meta("<html><body>no apollo data here</body></html>", "evt123")

    assert meta == {"event_title": None, "host_username": None, "host_avatar_url": None}


def test_returns_all_none_on_malformed_json_without_raising():
    page = "<html><script>window.__APOLLO_DATA__={not valid json};</script></html>"

    meta = extract_page_meta(page, "evt123")

    assert meta == {"event_title": None, "host_username": None, "host_avatar_url": None}


def test_returns_all_none_when_event_has_no_hosts():
    page = _fake_page({
        "LiveEvent:evt123": {"__typename": "LiveEvent", "id": "evt123", "title": "A stream", "hosts": []},
    })

    meta = extract_page_meta(page, "evt123")

    assert meta == {"event_title": "A stream", "host_username": None, "host_avatar_url": None}
