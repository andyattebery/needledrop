import asyncio
import logging
import wave
from unittest.mock import patch

import pytest

from needledrop.audio.capture import (
    FRAME_BYTES,
    CaptureClock,
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


RATE = 48000
CHUNK = 960  # 20 ms


def bursty_arrivals(seconds: float, period_frames: int = 6000, ppm: float = 0.0, latency: float = 0.003):
    """(true capture time, arrival time) for each 20 ms chunk when audio arrives a period at a time.

    A period becomes readable once its last frame is captured, plus some latency; a chunk can be read
    once the period that completes it has arrived. ``ppm`` makes the capture clock run slow.
    """
    frame_seconds = (1 + ppm / 1_000_000) / RATE
    chunks = []
    for i in range(int(seconds * RATE / CHUNK)):
        completing_period = ((i + 1) * CHUNK + period_frames - 1) // period_frames
        arrival = completing_period * period_frames * frame_seconds + latency
        chunks.append((i * CHUNK * frame_seconds, arrival))
    return chunks


def test_capture_clock_spaces_bursty_arrivals_by_their_samples():
    clock = CaptureClock(RATE)
    chunks = bursty_arrivals(seconds=30)
    stamps = [clock.stamp(CHUNK, arrival) for _, arrival in chunks]
    settled = stamps[50:]  # after the first second
    assert all(abs((b - a) - 0.02) < 1e-6 for a, b in zip(settled, settled[1:]))
    assert all(abs(stamp - true) < 0.005 for stamp, (true, _) in zip(stamps[50:], chunks[50:]))


def test_capture_clock_never_stamps_a_chunk_later_than_it_could_have_been_captured():
    clock = CaptureClock(RATE)
    for _, arrival in bursty_arrivals(seconds=5):
        assert clock.stamp(CHUNK, arrival) <= arrival - 0.02 + 1e-9


def test_capture_clock_follows_a_slow_capture_clock():
    clock = CaptureClock(RATE)
    chunks = bursty_arrivals(seconds=120, ppm=200)
    stamps = [clock.stamp(CHUNK, arrival) for _, arrival in chunks]
    assert all(abs(stamp - true) < 0.005 for stamp, (true, _) in zip(stamps[50:], chunks[50:]))


def test_capture_clock_ignores_a_late_read():
    clock = CaptureClock(RATE)
    chunks = bursty_arrivals(seconds=10)
    stalled = [(true, arrival + (0.3 if 200 <= i < 210 else 0.0)) for i, (true, arrival) in enumerate(chunks)]
    stamps = [clock.stamp(CHUNK, arrival) for _, arrival in stalled]
    assert all(abs(stamp - true) < 0.005 for stamp, (true, _) in zip(stamps[50:], stalled[50:]))
