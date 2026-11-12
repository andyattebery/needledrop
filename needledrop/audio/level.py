import audioop
import math
from enum import Enum

# Full scale of a signed 16-bit sample, as pydub measured dBFS before this module replaced it.
FULL_SCALE = 32768


def chunk_dbfs(pcm: bytes) -> float:
    """RMS level of S16_LE PCM in dBFS; -inf for digital silence."""
    rms = audioop.rms(pcm, 2)
    if rms == 0:
        return -math.inf
    return 20 * math.log10(rms / FULL_SCALE)


class Transition(Enum):
    STARTED = "started"
    STOPPED = "stopped"


class PlaybackDetector:
    """Turns per-chunk levels into playback start and stop transitions.

    Playback starts after ``start_seconds`` of continuous sound and stops after ``stop_seconds`` of
    continuous silence, as the sox silence filter that fed OwnTone's pipe did. A level at the threshold
    counts as sound. Durations are counted in frames, so the thresholds are exact.
    """

    def __init__(
        self,
        threshold_dbfs: float,
        sample_rate: int,
        start_seconds: float,
        stop_seconds: float,
    ) -> None:
        self._threshold = threshold_dbfs
        self._start_frames = round(start_seconds * sample_rate)
        self._stop_frames = round(stop_seconds * sample_rate)
        self._run_frames = 0
        self.playing = False

    def update(self, dbfs: float, frames: int) -> Transition | None:
        loud = dbfs >= self._threshold
        if loud != self.playing:
            self._run_frames += frames
        else:
            self._run_frames = 0

        if not self.playing and self._run_frames >= self._start_frames:
            self.playing = True
            self._run_frames = 0
            return Transition.STARTED
        if self.playing and self._run_frames >= self._stop_frames:
            self.playing = False
            self._run_frames = 0
            return Transition.STOPPED
        return None

    def reset(self) -> bool:
        """Forget all state, returning whether playback was in progress."""
        was_playing = self.playing
        self.playing = False
        self._run_frames = 0
        return was_playing
