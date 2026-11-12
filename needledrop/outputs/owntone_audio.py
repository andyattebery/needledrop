import logging
import math
import os
import select
import time

from needledrop.audio.capture import FRAME_BYTES
from needledrop.models import AudioChunk, TrackInfo

logger = logging.getLogger(__name__)

# A write of at most PIPE_BUF bytes is atomic: a full pipe refuses it whole, so no frame is ever split.
WRITE_BYTES = select.PIPE_BUF - select.PIPE_BUF % FRAME_BYTES
OPEN_RETRY_SECONDS = 1.0
FULL_WARNING_INTERVAL_SECONDS = 60.0


class OwnToneAudioOutput:
    """Writes the turntable into OwnTone's pipe while a record plays.

    OwnTone starts playing a pipe when data arrives and stops at EOF, so the pipe is held open only from
    playback start to playback stop, as the sox silence filter in front of it used to do. Writes never
    block: with no reader the pipe can't be opened, and audio that a full pipe refuses is dropped.
    """

    def __init__(self, audio_fifo: str) -> None:
        self._path = audio_fifo
        self._fd: int | None = None
        self._playing = False
        self._next_open = 0.0
        self._open_failing = False
        self._last_full_warning = -math.inf

    async def on_track_recognized(self, track: TrackInfo) -> None:
        pass

    async def on_playback_started(self) -> None:
        self._playing = True
        self._next_open = 0.0
        self._open()

    async def on_playback_stopped(self) -> None:
        self._playing = False
        self._close()

    async def on_audio(self, chunk: AudioChunk) -> None:
        if not self._playing or (self._fd is None and not self._open()):
            return
        pcm = chunk.pcm
        for start in range(0, len(pcm), WRITE_BYTES):
            try:
                os.write(self._fd, pcm[start : start + WRITE_BYTES])
            except BlockingIOError:
                self._warn_full()
                return
            except BrokenPipeError:
                logger.warning("OwnTone closed its pipe %s; reopening", self._path)
                self._close()
                self._next_open = time.monotonic() + OPEN_RETRY_SECONDS
                return

    def _open(self) -> bool:
        now = time.monotonic()
        if now < self._next_open:
            return False
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if not self._open_failing:
                logger.warning("Can't open OwnTone pipe %s: %s; retrying", self._path, e.strerror)
                self._open_failing = True
            self._next_open = now + OPEN_RETRY_SECONDS
            return False
        if self._open_failing:
            logger.info("Opened OwnTone pipe %s", self._path)
            self._open_failing = False
        return True

    def _close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _warn_full(self) -> None:
        now = time.monotonic()
        if now - self._last_full_warning >= FULL_WARNING_INTERVAL_SECONDS:
            self._last_full_warning = now
            logger.warning("OwnTone isn't reading fast enough; dropping audio")
