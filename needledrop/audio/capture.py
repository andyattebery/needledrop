import asyncio
import logging
import wave
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

from needledrop.config import AudioConfig

logger = logging.getLogger(__name__)

# Recognition writes its sample here, on tmpfs, to spare the Pi's SD card.
SAMPLE_PATH = Path("/run/needledrop/sample.wav")

# Capture is always S16_LE stereo, which OwnTone's pipe and Shazam both take.
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH

# arecord writes one period at a time, so 20 ms periods keep the audio arriving steadily: playback is
# detected promptly and capture timestamps stay even. Without a period of its own arecord takes a quarter
# of the buffer, a second here. The 4 s buffer is what the OwnTone pipe service asked for, so a stall in
# this process doesn't overrun; a device that can't hold that much clamps it.
ALSA_PERIOD_US = 20_000
ALSA_BUFFER_US = 4_000_000


class CaptureError(Exception):
    pass


def arecord_args(config: AudioConfig) -> list[str]:
    return [
        "arecord", "-q",
        "-D", config.alsa_device,
        "-f", "S16_LE",
        "-r", str(config.sample_rate),
        "-c", str(CHANNELS),
        "-t", "raw",
        f"--period-time={ALSA_PERIOD_US}",
        f"--buffer-time={ALSA_BUFFER_US}",
    ]


async def _log_stderr(stream: asyncio.StreamReader, last_line: list[str]) -> None:
    async for raw in stream:
        line = raw.decode(errors="replace").strip()
        if line:
            logger.warning("arecord: %s", line)
            last_line[:] = [line]


async def capture_stream(config: AudioConfig, chunk_frames: int) -> AsyncIterator[bytes]:
    """Yield PCM from a continuous arecord in chunks of ``chunk_frames`` frames.

    Raises CaptureError when arecord exits. Closing the iterator terminates arecord.
    """
    proc = await asyncio.create_subprocess_exec(
        *arecord_args(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    last_line: list[str] = []
    stderr_task = asyncio.create_task(_log_stderr(proc.stderr, last_line))
    try:
        while True:
            try:
                chunk = await proc.stdout.readexactly(chunk_frames * FRAME_BYTES)
            except asyncio.IncompleteReadError:
                break
            yield chunk
        returncode = await proc.wait()
        await stderr_task
        detail = last_line[0] if last_line else "no error output"
        raise CaptureError(f"arecord exited with status {returncode}: {detail}")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        stderr_task.cancel()


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


class RecentAudio:
    """The most recent ``max_frames`` frames of captured PCM."""

    def __init__(self, max_frames: int) -> None:
        self._max_bytes = max_frames * FRAME_BYTES
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, pcm: bytes) -> None:
        self._chunks.append(pcm)
        self._size += len(pcm)
        while self._size - len(self._chunks[0]) >= self._max_bytes:
            self._size -= len(self._chunks.popleft())

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)[-self._max_bytes :]
