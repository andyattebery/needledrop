import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from needledrop.models import TrackInfo
from needledrop.outputs.lastfm import LastFMOutput


@pytest.fixture
def lastfm_output():
    with patch("needledrop.outputs.lastfm.pylast.LastFMNetwork"):
        output = LastFMOutput(
            api_key="key",
            api_secret="secret",
            username="user",
            password_hash="hash",
            scrobble_threshold_seconds=30,
        )
    return output


async def test_first_track_calls_now_playing(lastfm_output, sample_track):
    await lastfm_output.on_track_recognized(sample_track)
    lastfm_output._network.update_now_playing.assert_called_once_with(
        artist="Artist", title="Song", album="Album"
    )


async def test_same_track_no_scrobble(lastfm_output, sample_track):
    await lastfm_output.on_track_recognized(sample_track)
    await lastfm_output.on_track_recognized(sample_track)
    lastfm_output._network.scrobble.assert_not_called()


async def test_different_track_scrobbles_previous_if_threshold_met(lastfm_output):
    old = TrackInfo(
        title="Old",
        artist="Old Artist",
        recognized_at=datetime.now() - timedelta(seconds=60),
    )
    new = TrackInfo(title="New", artist="New Artist")

    await lastfm_output.on_track_recognized(old)
    await lastfm_output.on_track_recognized(new)

    lastfm_output._network.scrobble.assert_called_once()
    call_kwargs = lastfm_output._network.scrobble.call_args[1]
    assert call_kwargs["artist"] == "Old Artist"
    assert call_kwargs["title"] == "Old"


async def test_different_track_no_scrobble_under_threshold(lastfm_output):
    old = TrackInfo(
        title="Old",
        artist="Old Artist",
        recognized_at=datetime.now() - timedelta(seconds=5),
    )
    new = TrackInfo(title="New", artist="New Artist")

    await lastfm_output.on_track_recognized(old)
    await lastfm_output.on_track_recognized(new)

    lastfm_output._network.scrobble.assert_not_called()


async def test_playback_stopped_scrobbles_if_threshold_met(lastfm_output):
    track = TrackInfo(
        title="Song",
        artist="Artist",
        recognized_at=datetime.now() - timedelta(seconds=60),
    )
    await lastfm_output.on_track_recognized(track)
    await lastfm_output.on_playback_stopped()

    lastfm_output._network.scrobble.assert_called_once()


async def test_playback_stopped_clears_state(lastfm_output, sample_track):
    await lastfm_output.on_track_recognized(sample_track)
    await lastfm_output.on_playback_stopped()

    assert lastfm_output._prev_track is None
    assert lastfm_output._prev_recognized_at is None


async def test_now_playing_error_logged(lastfm_output, sample_track, caplog):
    import pylast

    lastfm_output._network.update_now_playing.side_effect = pylast.WSError(
        lastfm_output._network, "error", "details"
    )
    with caplog.at_level(logging.WARNING):
        await lastfm_output.on_track_recognized(sample_track)
    assert "Last.fm now-playing failed" in caplog.text


async def test_scrobble_error_logged(lastfm_output, caplog):
    import pylast

    track = TrackInfo(
        title="Song",
        artist="Artist",
        recognized_at=datetime.now() - timedelta(seconds=60),
    )
    await lastfm_output.on_track_recognized(track)

    lastfm_output._network.scrobble.side_effect = pylast.NetworkError(
        None, "timeout"
    )
    new = TrackInfo(title="New", artist="New Artist")
    with caplog.at_level(logging.WARNING):
        await lastfm_output.on_track_recognized(new)
    assert "Last.fm scrobble failed" in caplog.text
