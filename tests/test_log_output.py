import logging

from needledrop.models import TrackInfo
from needledrop.outputs.log import LogOutput


async def test_on_track_recognized_logs(caplog, sample_track):
    output = LogOutput()
    with caplog.at_level(logging.INFO):
        await output.on_track_recognized(sample_track)
    assert "Artist" in caplog.text
    assert "Song" in caplog.text
    assert "Album" in caplog.text


async def test_on_playback_stopped_logs(caplog):
    output = LogOutput()
    with caplog.at_level(logging.INFO):
        await output.on_playback_stopped()
    assert "Playback stopped" in caplog.text


async def test_none_album_formats(caplog):
    output = LogOutput()
    track = TrackInfo(title="Song", artist="Artist", album=None)
    with caplog.at_level(logging.INFO):
        await output.on_track_recognized(track)
    assert "Song" in caplog.text
    assert "None" in caplog.text
