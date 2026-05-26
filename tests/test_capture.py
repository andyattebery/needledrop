from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from needledrop.audio.capture import CaptureError, capture_sample, is_silent
from needledrop.config import AudioConfig


@patch("needledrop.audio.capture.AudioSegment")
def test_is_silent_below_threshold(mock_audio_segment):
    mock_audio = MagicMock()
    mock_audio.dBFS = -60.0
    mock_audio_segment.from_wav.return_value = mock_audio
    assert is_silent(Path("/tmp/test.wav"), -50.0) is True


@patch("needledrop.audio.capture.AudioSegment")
def test_is_silent_above_threshold(mock_audio_segment):
    mock_audio = MagicMock()
    mock_audio.dBFS = -30.0
    mock_audio_segment.from_wav.return_value = mock_audio
    assert is_silent(Path("/tmp/test.wav"), -50.0) is False


@patch("needledrop.audio.capture.AudioSegment")
def test_is_silent_at_threshold(mock_audio_segment):
    mock_audio = MagicMock()
    mock_audio.dBFS = -50.0
    mock_audio_segment.from_wav.return_value = mock_audio
    assert is_silent(Path("/tmp/test.wav"), -50.0) is False


@patch("asyncio.create_subprocess_exec")
async def test_capture_sample_success(mock_exec):
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")
    mock_exec.return_value = mock_proc

    config = AudioConfig(
        alsa_device="dsnoopusb",
        sample_rate=48000,
        sample_duration_seconds=12,
    )
    result = await capture_sample(config)
    assert result == Path("/run/needledrop/sample.wav")

    args = mock_exec.call_args[0]
    assert args[0] == "arecord"
    assert "-D" in args
    assert "dsnoopusb" in args
    assert "-d" in args
    assert "12" in args
    assert "-r" in args
    assert "48000" in args


@patch("asyncio.create_subprocess_exec")
async def test_capture_sample_failure_raises(mock_exec):
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"device not found")
    mock_exec.return_value = mock_proc

    config = AudioConfig(
        alsa_device="dsnoopusb",
        sample_rate=48000,
        sample_duration_seconds=12,
    )
    with pytest.raises(CaptureError, match="device not found"):
        await capture_sample(config)
