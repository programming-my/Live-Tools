from clips import parse_timestamp_to_seconds, seconds_to_timecode


def test_parse_timestamp_bare_seconds():
    assert parse_timestamp_to_seconds("90") == 90
    assert parse_timestamp_to_seconds("90.5") == 90.5


def test_parse_timestamp_mm_ss():
    assert parse_timestamp_to_seconds("1:02") == 62
    assert parse_timestamp_to_seconds("00:00:30".split(":", 1)[1]) == 30  # "00:30"


def test_parse_timestamp_hh_mm_ss():
    assert parse_timestamp_to_seconds("00:00:30") == 30
    assert parse_timestamp_to_seconds("2:03:04") == 7384


def test_parse_timestamp_rejects_invalid():
    for bad in ["invalid", "1:60", "1:2:60", "-5", ""]:
        try:
            parse_timestamp_to_seconds(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_seconds_to_timecode():
    assert seconds_to_timecode(0) == "00-00-00"
    assert seconds_to_timecode(65) == "00-01-05"
    assert seconds_to_timecode(3661) == "01-01-01"
    assert seconds_to_timecode(90.9) == "00-01-30"
