import math
import struct

import pytest

from needledrop.audio.level import PlaybackDetector, Transition, chunk_dbfs

RATE = 48000
CHUNK_FRAMES = 960  # 20 ms at 48 kHz


def stereo_pcm(amplitude: int, frames: int = CHUNK_FRAMES) -> bytes:
    """A square wave at ``amplitude`` on both channels, S16_LE."""
    samples = []
    for i in range(frames):
        value = amplitude if i % 2 == 0 else -amplitude
        samples += [value, value]
    return struct.pack(f"<{len(samples)}h", *samples)


def test_chunk_dbfs_of_silence_is_minus_infinity():
    assert chunk_dbfs(bytes(CHUNK_FRAMES * 4)) == -math.inf


def test_chunk_dbfs_of_full_scale_is_zero():
    assert chunk_dbfs(stereo_pcm(32767)) == pytest.approx(0.0, abs=0.01)


def test_chunk_dbfs_of_half_scale_is_minus_six():
    assert chunk_dbfs(stereo_pcm(16384)) == pytest.approx(-6.02, abs=0.01)


def make_detector() -> PlaybackDetector:
    return PlaybackDetector(
        threshold_dbfs=-50.0, sample_rate=RATE, start_seconds=0.1, stop_seconds=5.0
    )


def feed(detector: PlaybackDetector, dbfs: float, chunks: int) -> list[Transition]:
    transitions = []
    for _ in range(chunks):
        transition = detector.update(dbfs, CHUNK_FRAMES)
        if transition is not None:
            transitions.append(transition)
    return transitions


def test_starts_after_start_seconds_of_sound():
    detector = make_detector()
    assert feed(detector, -20.0, 4) == []
    assert detector.update(-20.0, CHUNK_FRAMES) is Transition.STARTED
    assert detector.playing is True


def test_brief_sound_does_not_start():
    detector = make_detector()
    assert feed(detector, -20.0, 4) == []
    assert feed(detector, -70.0, 1) == []
    assert feed(detector, -20.0, 4) == []
    assert detector.playing is False


def test_level_at_threshold_counts_as_sound():
    detector = make_detector()
    assert feed(detector, -50.0, 5) == [Transition.STARTED]


def test_stops_after_stop_seconds_of_silence():
    detector = make_detector()
    feed(detector, -20.0, 5)
    assert feed(detector, -70.0, 249) == []
    assert detector.update(-70.0, CHUNK_FRAMES) is Transition.STOPPED
    assert detector.playing is False


def test_gap_between_tracks_does_not_stop():
    detector = make_detector()
    feed(detector, -20.0, 5)
    assert feed(detector, -70.0, 200) == []  # 4 s
    assert feed(detector, -20.0, 1) == []
    assert feed(detector, -70.0, 249) == []
    assert detector.playing is True


def test_restarts_after_a_stop():
    detector = make_detector()
    feed(detector, -20.0, 5)
    feed(detector, -70.0, 250)
    assert feed(detector, -20.0, 5) == [Transition.STARTED]


def test_reset_reports_whether_it_was_playing():
    detector = make_detector()
    assert detector.reset() is False
    feed(detector, -20.0, 5)
    assert detector.reset() is True
    assert detector.playing is False
    assert feed(detector, -20.0, 4) == []
