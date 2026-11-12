import asyncio
import logging
import wave
from unittest.mock import patch

import pytest

from needledrop.audio.capture import (
    FRAME_BYTES,
    CaptureError,
    RecentAudio,
    arecord_args,
    capture_stream,
    write_wav,
)
from needledrop.config import AudioConfig


@pytest.fixture
def audio_config():
    return AudioConfig(alsa_device="dsnoopusb", sample_rate=48000, sample_duration_seconds=12)


class FakeProcess:
    """An arecord stand-in whose stdout and stderr the test feeds."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.terminated = False

    def finish(self, returncode: int) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = returncode

    def terminate(self) -> None:
        self.terminated = True
        self.finish(-15)

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


def test_arecord_args_capture_raw_s16_stereo_in_20ms_periods(audio_config):
    # A period of its own: without one, arecord takes a quarter of the buffer, delivers audio in 1 s
    # bursts, and on a device whose maximum period equals its maximum buffer refuses to start.
    assert arecord_args(audio_config) == [
        "arecord", "-q", "-D", "dsnoopusb", "-f", "S16_LE", "-r", "48000", "-c", "2",
        "-t", "raw", "--period-time=20000", "--buffer-time=4000000",
    ]


async def test_capture_stream_yields_whole_chunks(audio_config):
    proc = FakeProcess()
    proc.stdout.feed_data(bytes(range(256)) * 3)  # 768 bytes: two chunks of 96 frames
    with patch("needledrop.audio.capture.asyncio.create_subprocess_exec", return_value=proc):
        stream = capture_stream(audio_config, chunk_frames=96)
        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()
    assert len(first) == 96 * FRAME_BYTES
    assert first + second == (bytes(range(256)) * 3)[: 2 * 96 * FRAME_BYTES]


async def test_capture_stream_raises_with_arecord_error_when_it_exits(audio_config):
    proc = FakeProcess()
    proc.stderr.feed_data(b"arecord: main:850: audio open error: No such device\n")
    proc.finish(1)
    with patch("needledrop.audio.capture.asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(CaptureError, match="status 1: arecord: main:850: audio open error"):
            await anext(capture_stream(audio_config, chunk_frames=96))


async def test_capture_stream_logs_overruns_as_warnings(audio_config, caplog):
    proc = FakeProcess()
    proc.stderr.feed_data(b"overrun!!! (at least 12.345 ms long)\n")
    proc.stdout.feed_data(bytes(96 * FRAME_BYTES))
    with patch("needledrop.audio.capture.asyncio.create_subprocess_exec", return_value=proc):
        stream = capture_stream(audio_config, chunk_frames=96)
        with caplog.at_level(logging.WARNING):
            await anext(stream)
            await asyncio.sleep(0)
        await stream.aclose()
    assert "arecord: overrun!!! (at least 12.345 ms long)" in caplog.text


async def test_capture_stream_terminates_arecord_when_closed(audio_config):
    proc = FakeProcess()
    proc.stdout.feed_data(bytes(96 * FRAME_BYTES))
    with patch("needledrop.audio.capture.asyncio.create_subprocess_exec", return_value=proc):
        stream = capture_stream(audio_config, chunk_frames=96)
        await anext(stream)
        await stream.aclose()
    assert proc.terminated is True


def test_write_wav_writes_stereo_s16(tmp_path):
    path = tmp_path / "sample.wav"
    pcm = bytes(range(256)) * 4  # 256 frames
    write_wav(path, pcm, sample_rate=44100)
    with wave.open(str(path)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (2, 2, 44100)
        assert wav.readframes(wav.getnframes()) == pcm


def test_recent_audio_keeps_only_the_last_max_frames():
    recent = RecentAudio(max_frames=150)
    chunks = [bytes([n]) * (100 * FRAME_BYTES) for n in range(3)]
    for chunk in chunks:
        recent.append(chunk)
    snapshot = recent.snapshot()
    assert len(snapshot) == 150 * FRAME_BYTES
    assert snapshot == (chunks[1] + chunks[2])[-150 * FRAME_BYTES :]


def test_recent_audio_snapshot_is_shorter_before_it_fills():
    recent = RecentAudio(max_frames=150)
    recent.append(bytes(10 * FRAME_BYTES))
    assert len(recent.snapshot()) == 10 * FRAME_BYTES
