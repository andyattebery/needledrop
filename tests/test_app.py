import logging
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from needledrop.app import LoopState, build_outputs, safe_call, tick
from needledrop.audio.capture import CaptureError
from needledrop.models import TrackInfo


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_respects_enabled(mock_network, sample_config, mock_session):
    outputs = build_outputs(sample_config, mock_session)
    assert len(outputs) == 4


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_disabled(mock_network, sample_config, mock_session):
    sample_config.outputs.lastfm.enabled = False
    sample_config.outputs.tuneshine.enabled = False
    outputs = build_outputs(sample_config, mock_session)
    assert len(outputs) == 2


async def test_safe_call_catches_exceptions(caplog):
    async def failing():
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR):
        await safe_call(failing)
    assert "Output handler error" in caplog.text


async def test_safe_call_passes_args():
    mock_fn = AsyncMock()
    await safe_call(mock_fn, "arg1", "arg2")
    mock_fn.assert_called_once_with("arg1", "arg2")


@patch("needledrop.app.is_silent", return_value=True)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_silence_dispatches_playback_stopped(
    mock_capture, mock_silent, sample_config
):
    mock_capture.return_value = Path("/tmp/test.wav")
    output = AsyncMock()
    state = LoopState(was_playing=True)

    await tick(sample_config, AsyncMock(), [output], state)

    output.on_playback_stopped.assert_called_once()
    assert state.was_playing is False
    assert state.last_track is None


@patch("needledrop.app.is_silent", return_value=True)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_continued_silence_no_repeated_stop(
    mock_capture, mock_silent, sample_config
):
    mock_capture.return_value = Path("/tmp/test.wav")
    output = AsyncMock()
    state = LoopState(was_playing=False)

    await tick(sample_config, AsyncMock(), [output], state)

    output.on_playback_stopped.assert_not_called()


@patch("needledrop.app.is_silent", return_value=False)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_recognition_dispatches(mock_capture, mock_silent, sample_config):
    mock_capture.return_value = Path("/tmp/test.wav")
    recognizer = AsyncMock()
    track = TrackInfo(title="Song", artist="Artist")
    recognizer.recognize.return_value = track
    output = AsyncMock()
    state = LoopState()

    await tick(sample_config, recognizer, [output], state)

    output.on_track_recognized.assert_called_once_with(track)
    assert state.last_track == track
    assert state.was_playing is True


@patch("needledrop.app.is_silent", return_value=False)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_recognition_none_no_dispatch(mock_capture, mock_silent, sample_config):
    mock_capture.return_value = Path("/tmp/test.wav")
    recognizer = AsyncMock()
    recognizer.recognize.return_value = None
    output = AsyncMock()
    state = LoopState()

    await tick(sample_config, recognizer, [output], state)

    output.on_track_recognized.assert_not_called()


@patch("needledrop.app.is_silent", return_value=False)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_duplicate_within_window_no_dispatch(
    mock_capture, mock_silent, sample_config
):
    mock_capture.return_value = Path("/tmp/test.wav")
    recognizer = AsyncMock()
    track = TrackInfo(title="Song", artist="Artist")
    recognizer.recognize.return_value = track
    output = AsyncMock()
    state = LoopState(
        last_track=TrackInfo(
            title="Song", artist="Artist", recognized_at=datetime.now()
        )
    )

    await tick(sample_config, recognizer, [output], state)

    output.on_track_recognized.assert_not_called()


@patch("needledrop.app.is_silent", return_value=False)
@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_duplicate_after_window_dispatches(
    mock_capture, mock_silent, sample_config
):
    mock_capture.return_value = Path("/tmp/test.wav")
    recognizer = AsyncMock()
    track = TrackInfo(title="Song", artist="Artist")
    recognizer.recognize.return_value = track
    output = AsyncMock()
    state = LoopState(
        last_track=TrackInfo(
            title="Song",
            artist="Artist",
            recognized_at=datetime.now() - timedelta(seconds=60),
        )
    )

    await tick(sample_config, recognizer, [output], state)

    output.on_track_recognized.assert_called_once()


@patch("needledrop.app.capture_sample", new_callable=AsyncMock)
async def test_capture_error_continues(mock_capture, sample_config, caplog):
    mock_capture.side_effect = CaptureError("device busy")
    output = AsyncMock()
    state = LoopState()

    with caplog.at_level(logging.WARNING):
        await tick(sample_config, AsyncMock(), [output], state)

    output.on_track_recognized.assert_not_called()
    assert "Audio capture failed" in caplog.text
