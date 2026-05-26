from datetime import datetime, timedelta

from needledrop.models import TrackInfo


def test_equal_same_title_artist():
    t1 = TrackInfo(title="Song", artist="Artist", album="Album A")
    t2 = TrackInfo(title="Song", artist="Artist", album="Album B")
    assert t1 == t2


def test_not_equal_different_title():
    t1 = TrackInfo(title="Song A", artist="Artist")
    t2 = TrackInfo(title="Song B", artist="Artist")
    assert t1 != t2


def test_not_equal_different_artist():
    t1 = TrackInfo(title="Song", artist="Artist A")
    t2 = TrackInfo(title="Song", artist="Artist B")
    assert t1 != t2


def test_hash_same_for_equal_tracks():
    t1 = TrackInfo(title="Song", artist="Artist", album="A")
    t2 = TrackInfo(title="Song", artist="Artist", album="B")
    assert hash(t1) == hash(t2)


def test_usable_in_set():
    t1 = TrackInfo(title="Song", artist="Artist", album="A")
    t2 = TrackInfo(title="Song", artist="Artist", album="B")
    assert len({t1, t2}) == 1


def test_recognized_at_defaults_to_now():
    before = datetime.now()
    t = TrackInfo(title="Song", artist="Artist")
    after = datetime.now()
    assert before <= t.recognized_at <= after


def test_optional_fields_default_to_none():
    t = TrackInfo(title="Song", artist="Artist")
    assert t.album is None
    assert t.artwork_url is None
    assert t.duration_seconds is None


def test_not_equal_to_non_trackinfo():
    t = TrackInfo(title="Song", artist="Artist")
    assert t != "not a track"
    assert t.__eq__("not a track") is NotImplemented
