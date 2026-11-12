import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from needledrop.audio.capture import (
    FRAME_BYTES,
    SAMPLE_PATH,
    CaptureError,
    RecentAudio,
    capture_stream,
    write_wav,
)
from needledrop.audio.level import PlaybackDetector, Transition, chunk_dbfs
from needledrop.config import Config
from needledrop.models import AudioChunk, TrackInfo
from needledrop.outputs.base import AudioOutput, OutputHandler, ServiceOutput
from needledrop.outputs.lastfm import LastFMOutput
from needledrop.outputs.log import LogOutput
from needledrop.outputs.owntone_audio import OwnToneAudioOutput
from needledrop.outputs.owntone_metadata import OwnToneMetadataOutput
from needledrop.outputs.sendspin import SendspinOutput
from needledrop.outputs.tuneshine import TuneshineOutput
from needledrop.recognition.shazamio_recognizer import ShazamIORecognizer

logger = logging.getLogger(__name__)

# Capture is read in 20 ms chunks, the unit of level detection and of audio delivery.
CHUNK_SECONDS = 0.02
# Sound needed before playback counts as started, as the sox filter that fed OwnTone's pipe had it.
START_SECONDS = 0.1
# Audio buffered per audio output before its oldest chunks are dropped.
AUDIO_QUEUE_SECONDS = 2.0
CAPTURE_RETRY_SECONDS = 2.0
# A recognition that hasn't answered by now is abandoned, so the next one can go ahead. shazamio itself
# retries rate limits and server errors for minutes.
RECOGNITION_TIMEOUT_SECONDS = 60.0
AUDIO_ERROR_LOG_INTERVAL_SECONDS = 60.0


@dataclass
class LoopState:
    last_track: TrackInfo | None = field(default=None)
    # Bumped at every playback start and stop, so a recognition that outlives its playback is dropped.
    generation: int = 0


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

    if cfg.owntone_audio.enabled:
        outputs.append(OwnToneAudioOutput(audio_fifo=cfg.owntone_audio.audio_fifo))

    if cfg.sendspin.enabled:
        outputs.append(SendspinOutput(cfg.sendspin, sample_rate=config.audio.sample_rate))

    return outputs


async def safe_call(fn, *args) -> None:
    try:
        await fn(*args)
    except Exception:
        logger.exception("Output handler error in %s", fn)


class _Worker:
    """Delivers one output's events, and its audio if it takes audio, on tasks of its own."""

    def __init__(self, output: OutputHandler, audio_queue_chunks: int) -> None:
        self._output = output
        self._events: asyncio.Queue[tuple[str, tuple]] = asyncio.Queue()
        self._audio: asyncio.Queue[AudioChunk] | None = (
            asyncio.Queue(maxsize=audio_queue_chunks) if isinstance(output, AudioOutput) else None
        )
        self._last_audio_error = -math.inf

    def post(self, method: str, args: tuple) -> None:
        self._events.put_nowait((method, args))

    def post_audio(self, chunk: AudioChunk) -> None:
        if self._audio is None:
            return
        if self._audio.full():
            self._audio.get_nowait()
        self._audio.put_nowait(chunk)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._deliver_events())
            if self._audio is not None:
                tg.create_task(self._deliver_audio(self._audio))

    async def _deliver_events(self) -> None:
        while True:
            method, args = await self._events.get()
            await safe_call(getattr(self._output, method), *args)

    async def _deliver_audio(self, queue: asyncio.Queue[AudioChunk]) -> None:
        while True:
            chunk = await queue.get()
            try:
                await self._output.on_audio(chunk)
            except Exception:
                # Fifty chunks a second: log a failing output once a minute, not on every chunk.
                now = time.monotonic()
                if now - self._last_audio_error >= AUDIO_ERROR_LOG_INTERVAL_SECONDS:
                    self._last_audio_error = now
                    logger.exception("Audio output error in %s", self._output)


class Dispatcher:
    """Fans events and audio out to every output.

    Each output has its own worker, so a slow or failing output delays and drops only its own
    deliveries. An audio output that falls behind loses its oldest chunks.
    """

    def __init__(self, outputs: list[OutputHandler], audio_queue_chunks: int) -> None:
        self._workers = [_Worker(output, audio_queue_chunks) for output in outputs]

    def post(self, method: str, *args) -> None:
        for worker in self._workers:
            worker.post(method, args)

    def post_audio(self, chunk: AudioChunk) -> None:
        for worker in self._workers:
            worker.post_audio(chunk)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for worker in self._workers:
                tg.create_task(worker.run())


def _apply_transition(
    transition: Transition,
    dispatcher: Dispatcher,
    state: LoopState,
    transitions: asyncio.Queue[Transition],
) -> None:
    state.generation += 1
    if transition is Transition.STARTED:
        dispatcher.post("on_playback_started")
    else:
        state.last_track = None
        dispatcher.post("on_playback_stopped")
    transitions.put_nowait(transition)


async def capture_loop(
    config: Config,
    capture,
    detector: PlaybackDetector,
    recent: RecentAudio,
    dispatcher: Dispatcher,
    state: LoopState,
    transitions: asyncio.Queue[Transition],
) -> None:
    """Read the capture, detect playback, keep the recognition window, and feed audio outputs.

    Only cheap work happens here, so no output can make the capture fall behind.
    """
    rate = config.audio.sample_rate
    chunk_frames = round(rate * CHUNK_SECONDS)
    while True:
        try:
            async for pcm in capture(config.audio, chunk_frames):
                frames = len(pcm) // FRAME_BYTES
                captured_at = time.monotonic() - frames / rate
                transition = detector.update(chunk_dbfs(pcm), frames)
                if transition is not None:
                    _apply_transition(transition, dispatcher, state, transitions)
                recent.append(pcm)
                dispatcher.post_audio(AudioChunk(pcm=pcm, captured_at=captured_at))
        except CaptureError as e:
            logger.warning("Audio capture failed: %s", e)
        # Without capture there is no audio to play.
        if detector.reset():
            _apply_transition(Transition.STOPPED, dispatcher, state, transitions)
        await asyncio.sleep(CAPTURE_RETRY_SECONDS)


async def recognize(
    config: Config,
    recognizer,
    dispatcher,
    recent: RecentAudio,
    state: LoopState,
    generation: int,
    sample_path: Path | None = None,
) -> None:
    path = sample_path or SAMPLE_PATH
    try:
        write_wav(path, recent.snapshot(), config.audio.sample_rate)
        async with asyncio.timeout(RECOGNITION_TIMEOUT_SECONDS):
            track = await recognizer.recognize(path)
    except TimeoutError:
        logger.warning("Recognition failed: timed out")
        return
    except Exception as e:
        logger.warning("Recognition failed: %s", e)
        return

    if track is None or state.generation != generation:
        return

    last = state.last_track
    if (
        last
        and track.title == last.title
        and track.artist == last.artist
        and (track.recognized_at - last.recognized_at).total_seconds()
        < config.recognition.min_track_change_seconds
    ):
        return

    dispatcher.post("on_track_recognized", track)
    state.last_track = track


async def recognition_loop(
    config: Config,
    recognizer,
    dispatcher: Dispatcher,
    recent: RecentAudio,
    state: LoopState,
    transitions: asyncio.Queue[Transition],
) -> None:
    """While a record plays, recognize once the window has filled, then every interval."""
    loop = asyncio.get_running_loop()
    due: float | None = None
    task: asyncio.Task | None = None
    try:
        while True:
            timeout = None if due is None else max(0.0, due - loop.time())
            try:
                transition = await asyncio.wait_for(transitions.get(), timeout)
            except TimeoutError:
                if task is None or task.done():
                    task = asyncio.create_task(
                        recognize(config, recognizer, dispatcher, recent, state, state.generation)
                    )
                due = loop.time() + config.recognition.interval_seconds
                continue

            if task is not None:
                task.cancel()
                task = None
            if transition is Transition.STARTED:
                due = loop.time() + config.audio.sample_duration_seconds
            else:
                due = None
    finally:
        if task is not None:
            task.cancel()


async def run_pipeline(
    config: Config,
    recognizer,
    outputs: list[OutputHandler],
    capture=capture_stream,
) -> None:
    rate = config.audio.sample_rate
    dispatcher = Dispatcher(outputs, audio_queue_chunks=round(AUDIO_QUEUE_SECONDS / CHUNK_SECONDS))
    state = LoopState()
    transitions: asyncio.Queue[Transition] = asyncio.Queue()
    detector = PlaybackDetector(
        threshold_dbfs=config.audio.silence_threshold_dbfs,
        sample_rate=rate,
        start_seconds=START_SECONDS,
        stop_seconds=config.audio.silence_hold_seconds,
    )
    recent = RecentAudio(max_frames=round(rate * config.audio.sample_duration_seconds))

    async with asyncio.TaskGroup() as tg:
        tg.create_task(dispatcher.run())
        tg.create_task(
            capture_loop(config, capture, detector, recent, dispatcher, state, transitions)
        )
        tg.create_task(recognition_loop(config, recognizer, dispatcher, recent, state, transitions))


async def start_outputs(outputs: list[OutputHandler]) -> None:
    for output in outputs:
        if isinstance(output, ServiceOutput):
            await safe_call(output.start)


async def stop_outputs(outputs: list[OutputHandler]) -> None:
    for output in outputs:
        if isinstance(output, ServiceOutput):
            await safe_call(output.stop)


async def run(config: Config) -> None:
    recognizer = ShazamIORecognizer()

    async with aiohttp.ClientSession() as session:
        outputs = build_outputs(config, session)
        await start_outputs(outputs)
        logger.info("Started with %d output(s)", len(outputs))
        try:
            await run_pipeline(config, recognizer, outputs)
        finally:
            await stop_outputs(outputs)
