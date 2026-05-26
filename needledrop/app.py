import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

from needledrop.audio.capture import CaptureError, capture_sample, is_silent
from needledrop.config import Config
from needledrop.models import TrackInfo
from needledrop.outputs.base import OutputHandler
from needledrop.outputs.lastfm import LastFMOutput
from needledrop.outputs.log import LogOutput
from needledrop.outputs.owntone_metadata import OwnToneMetadataOutput
from needledrop.outputs.tuneshine import TuneshineOutput
from needledrop.recognition.shazamio_recognizer import ShazamIORecognizer

logger = logging.getLogger(__name__)


@dataclass
class LoopState:
    last_track: TrackInfo | None = field(default=None)
    was_playing: bool = False


def build_outputs(config: Config, session: aiohttp.ClientSession) -> list[OutputHandler]:
    outputs: list[OutputHandler] = []
    cfg = config.outputs

    if cfg.lastfm.enabled:
        outputs.append(
            LastFMOutput(
                api_key=cfg.lastfm.api_key,
                api_secret=cfg.lastfm.api_secret,
                username=cfg.lastfm.username,
                password_hash=cfg.lastfm.password_hash,
                scrobble_threshold_seconds=cfg.lastfm.scrobble_threshold_seconds,
            )
        )

    if cfg.owntone_metadata.enabled:
        outputs.append(
            OwnToneMetadataOutput(
                metadata_fifo=cfg.owntone_metadata.metadata_fifo,
                session=session,
            )
        )

    if cfg.tuneshine.enabled:
        outputs.append(
            TuneshineOutput(
                host=cfg.tuneshine.host,
                session=session,
            )
        )

    if cfg.log.enabled:
        outputs.append(LogOutput())

    return outputs


async def safe_call(fn, *args) -> None:
    try:
        await fn(*args)
    except Exception:
        logger.exception("Output handler error in %s", fn)


async def tick(config: Config, recognizer, outputs: list, state: LoopState) -> None:
    try:
        wav_path = await capture_sample(config.audio)
    except CaptureError as e:
        logger.warning("Audio capture failed: %s", e)
        return

    if is_silent(wav_path, config.audio.silence_threshold_dbfs):
        if state.was_playing:
            for output in outputs:
                await safe_call(output.on_playback_stopped)
            state.last_track = None
        state.was_playing = False
        return

    state.was_playing = True

    track = await recognizer.recognize(wav_path)
    if track is None:
        return

    if (
        state.last_track
        and track.title == state.last_track.title
        and track.artist == state.last_track.artist
        and (track.recognized_at - state.last_track.recognized_at).total_seconds()
        < config.recognition.min_track_change_seconds
    ):
        return

    for output in outputs:
        await safe_call(output.on_track_recognized, track)

    state.last_track = track


async def run(config: Config) -> None:
    recognizer = ShazamIORecognizer()

    async with aiohttp.ClientSession() as session:
        outputs = build_outputs(config, session)
        state = LoopState()
        logger.info("Started with %d output(s)", len(outputs))

        while True:
            await asyncio.sleep(config.recognition.interval_seconds)
            await tick(config, recognizer, outputs, state)
