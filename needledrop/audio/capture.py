import asyncio
import logging
from pathlib import Path

from pydub import AudioSegment

from needledrop.config import AudioConfig

logger = logging.getLogger(__name__)

SAMPLE_PATH = Path("/run/needledrop/sample.wav")


class CaptureError(Exception):
    pass


async def capture_sample(config: AudioConfig) -> Path:
    proc = await asyncio.create_subprocess_exec(
        "arecord",
        "-D", config.alsa_device,
        "-d", str(config.sample_duration_seconds),
        "-f", "S16_LE",
        "-r", str(config.sample_rate),
        "-c", "2",
        "-t", "wav",
        str(SAMPLE_PATH),
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise CaptureError(stderr.decode(errors="replace").strip())
    return SAMPLE_PATH


def is_silent(wav_path: Path, threshold_dbfs: float) -> bool:
    audio = AudioSegment.from_wav(str(wav_path))
    return audio.dBFS < threshold_dbfs
