import asyncio
import contextlib
import logging
import struct
import wave
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from needledrop.app import (
    Dispatcher,
    LoopState,
    build_outputs,
    capture_loop,
    recognize,
    run_pipeline,
    safe_call,
    start_outputs,
    stop_outputs,
)
from needledrop.audio.capture import CaptureError, RecentAudio
from needledrop.audio.level import PlaybackDetector
from needledrop.models import AudioChunk, TrackInfo
from needledrop.outputs.base import AudioOutput, ServiceOutput

LOUD = struct.pack("<2h", 3000, -3000) * 960  # one 20 ms chunk at 48 kHz, about -20.8 dBFS
QUIET = bytes(960 * 4)


class Recorder:
    """An events-only output that records what it receives, with loop timestamps."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.times: dict[str, float] = {}

    def _record(self, *call) -> None:
        self.calls.append(call)
        self.times.setdefault(call[0], asyncio.get_running_loop().time())

    async def on_track_recognized(self, track: TrackInfo) -> None:
        self._record("recognized", track)

    async def on_playback_started(self) -> None:
        self._record("started")

    async def on_playback_stopped(self) -> None:
        self._record("stopped")


class AudioRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[AudioChunk] = []

    async def on_audio(self, chunk: AudioChunk) -> None:
        self.chunks.append(chunk)


class Service(Recorder):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.lifecycle: list[str] = []

    async def start(self) -> None:
        self.lifecycle.append("start")
        if self.fail:
            raise OSError("port in use")

    async def stop(self) -> None:
        self.lifecycle.append("stop")


class Posts:
    """Stands in for the Dispatcher in tests of recognize()."""

    def __init__(self) -> None:
        self.posts: list[tuple] = []

    def post(self, method: str, *args) -> None:
        self.posts.append((method, *args))


async def idle(chunks):
    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)
    await asyncio.Event().wait()


async def fail(chunks):
    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)
    raise CaptureError("arecord exited with status 1: gone")


def capture_sequence(*runs):
    """A capture_stream stand-in that plays one run per call, then idles."""
    pending = list(runs)

    async def capture(config, chunk_frames):
        run = pending.pop(0) if pending else idle([])
        async for chunk in run:
            yield chunk

    return capture


async def run_until(coro, predicate, timeout: float = 3.0) -> None:
    task = asyncio.create_task(coro)
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                if task.done():
                    task.result()
                await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def recognizer_returning(*results):
    recognizer = AsyncMock()
    recognizer.recognize.side_effect = list(results)
    return recognizer


# --- build_outputs and safe_call ---


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_respects_enabled(mock_network, sample_config, mock_session):
    outputs = build_outputs(sample_config, mock_session)
    assert len(outputs) == 4


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_disabled(mock_network, sample_config, mock_session):
    sample_config.outputs.lastfm.enabled = False
    sample_config.outputs.tuneshine.enabled = False
    outputs = build_outputs(sample_config, mock_session)
    assert len(outputs) == 2


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_adds_owntone_audio_and_sendspin(mock_network, sample_config, mock_session):
    sample_config.outputs.owntone_audio.enabled = True
    sample_config.outputs.owntone_audio.audio_fifo = "/srv/media/Turntable"
    sample_config.outputs.sendspin.enabled = True
    outputs = build_outputs(sample_config, mock_session)
    assert [type(o).__name__ for o in outputs[-2:]] == ["OwnToneAudioOutput", "SendspinOutput"]
    assert isinstance(outputs[-1], ServiceOutput)
    assert all(isinstance(o, AudioOutput) for o in outputs[-2:])


@patch("needledrop.outputs.lastfm.pylast.LastFMNetwork")
async def test_build_outputs_adds_mp3_stream(mock_network, sample_config, mock_session):
    sample_config.outputs.mp3_stream.enabled = True
    outputs = build_outputs(sample_config, mock_session)
    assert type(outputs[-1]).__name__ == "Mp3StreamOutput"
    assert isinstance(outputs[-1], ServiceOutput)
    assert isinstance(outputs[-1], AudioOutput)


async def test_safe_call_catches_exceptions(caplog):
    async def failing():
        raise ValueError("boom")

    with caplog.at_level(logging.ERROR):
        await safe_call(failing)
    assert "Output handler error" in caplog.text


async def test_safe_call_passes_args():
    mock_fn = AsyncMock()
    await safe_call(mock_fn, "arg1", "arg2")
    mock_fn.assert_called_once_with("arg1", "arg2")


# --- Dispatcher ---


async def test_dispatcher_slow_output_does_not_delay_others():
    release = asyncio.Event()

    class Slow(Recorder):
        async def on_playback_started(self) -> None:
            await release.wait()

    fast = Recorder()
    dispatcher = Dispatcher([Slow(), fast], audio_queue_chunks=4)
    dispatcher.post("on_playback_started")
    await run_until(dispatcher.run(), lambda: fast.calls == [("started",)])
    release.set()


async def test_dispatcher_sends_audio_only_to_audio_outputs():
    events_only, listener = Recorder(), AudioRecorder()
    dispatcher = Dispatcher([events_only, listener], audio_queue_chunks=4)
    chunk = AudioChunk(pcm=LOUD, captured_at=1.0)
    dispatcher.post_audio(chunk)
    await run_until(dispatcher.run(), lambda: listener.chunks == [chunk])
    assert events_only.calls == []


async def test_dispatcher_drops_the_oldest_audio_when_an_output_falls_behind():
    release = asyncio.Event()

    class Stalled(AudioRecorder):
        async def on_audio(self, chunk: AudioChunk) -> None:
            await release.wait()
            await super().on_audio(chunk)

    output = Stalled()
    dispatcher = Dispatcher([output], audio_queue_chunks=2)
    chunks = [AudioChunk(pcm=bytes([n]) * 4, captured_at=float(n)) for n in range(4)]
    task = asyncio.create_task(dispatcher.run())
    dispatcher.post_audio(chunks[0])
    await asyncio.sleep(0.01)  # the output takes chunk 0 and stalls on it
    for chunk in chunks[1:]:
        dispatcher.post_audio(chunk)
    release.set()
    await run_until(asyncio.sleep(3600), lambda: len(output.chunks) == 3)
    task.cancel()
    assert [c.captured_at for c in output.chunks] == [0.0, 2.0, 3.0]


async def test_dispatcher_isolates_output_errors(caplog):
    class Broken(Recorder):
        async def on_playback_started(self) -> None:
            raise RuntimeError("boom")

    output = Broken()
    dispatcher = Dispatcher([output], audio_queue_chunks=4)
    dispatcher.post("on_playback_started")
    dispatcher.post("on_playback_stopped")
    with caplog.at_level(logging.ERROR):
        await run_until(dispatcher.run(), lambda: output.calls == [("stopped",)])
    assert "Output handler error" in caplog.text


# --- capture, level and playback events ---


async def test_pipeline_reports_playback_start_and_stop(sample_config):
    sample_config.audio.silence_hold_seconds = 0.1
    output = Recorder()
    capture = capture_sequence(idle([QUIET] * 3 + [LOUD] * 5 + [QUIET] * 5))
    await run_until(
        run_pipeline(sample_config, recognizer_returning(), [output], capture=capture),
        lambda: len(output.calls) >= 2,
    )
    assert output.calls == [("started",), ("stopped",)]


async def test_pipeline_delivers_every_chunk_to_audio_outputs(sample_config):
    output = AudioRecorder()
    chunks = [LOUD, QUIET, LOUD]
    await run_until(
        run_pipeline(sample_config, recognizer_returning(), [output], capture=capture_sequence(idle(chunks))),
        lambda: len(output.chunks) == 3,
    )
    assert [c.pcm for c in output.chunks] == chunks
    assert output.chunks[0].captured_at <= output.chunks[1].captured_at <= output.chunks[2].captured_at


@patch("needledrop.app.CAPTURE_RETRY_SECONDS", 0)
async def test_capture_failure_while_playing_stops_playback_and_retries(sample_config, caplog):
    output = Recorder()
    capture = capture_sequence(fail([LOUD] * 5), idle([LOUD] * 5))
    with caplog.at_level(logging.WARNING):
        await run_until(
            run_pipeline(sample_config, recognizer_returning(), [output], capture=capture),
            lambda: len(output.calls) >= 3,
        )
    assert output.calls == [("started",), ("stopped",), ("started",)]
    assert "Audio capture failed: arecord exited with status 1: gone" in caplog.text


# --- recognition ---


async def test_recognition_waits_for_a_full_window_then_repeats_every_interval(sample_config, tmp_path):
    sample_config.audio.sample_duration_seconds = 0.2
    sample_config.recognition.interval_seconds = 0.1
    sample_config.recognition.min_track_change_seconds = 0
    times: list[float] = []

    async def recognize_sample(path):
        times.append(asyncio.get_running_loop().time())
        return None

    recognizer = AsyncMock()
    recognizer.recognize.side_effect = recognize_sample
    output = Recorder()
    with patch("needledrop.app.SAMPLE_PATH", tmp_path / "sample.wav"):
        await run_until(
            run_pipeline(sample_config, recognizer, [output], capture=capture_sequence(idle([LOUD] * 5))),
            lambda: len(times) >= 3,
        )
    assert times[0] - output.times["started"] >= 0.2 - 0.02
    assert all(later - earlier >= 0.1 - 0.02 for earlier, later in zip(times, times[1:]))


async def test_no_recognition_while_silent(sample_config, tmp_path):
    sample_config.audio.sample_duration_seconds = 0.05
    sample_config.recognition.interval_seconds = 0.05
    recognizer = recognizer_returning()
    with patch("needledrop.app.SAMPLE_PATH", tmp_path / "sample.wav"):
        task = asyncio.create_task(
            run_pipeline(sample_config, recognizer, [Recorder()], capture=capture_sequence(idle([QUIET] * 50)))
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    recognizer.recognize.assert_not_called()


async def test_recognize_dispatches_a_new_track_from_the_window(sample_config, tmp_path):
    track = TrackInfo(title="Song", artist="Artist")
    recognizer = recognizer_returning(track)
    posts, state = Posts(), LoopState()
    recent = RecentAudio(max_frames=960)
    recent.append(LOUD)
    sample = tmp_path / "sample.wav"

    await recognize(sample_config, recognizer, posts, recent, state, state.generation, sample_path=sample)

    assert posts.posts == [("on_track_recognized", track)]
    assert state.last_track is track
    with wave.open(str(sample)) as wav:
        assert wav.readframes(wav.getnframes()) == LOUD


async def test_recognize_skips_a_repeat_within_min_track_change(sample_config, tmp_path):
    state = LoopState(last_track=TrackInfo(title="Song", artist="Artist", recognized_at=datetime.now()))
    posts = Posts()
    recognizer = recognizer_returning(TrackInfo(title="Song", artist="Artist"))
    await recognize(sample_config, recognizer, posts, RecentAudio(960), state, state.generation,
                    sample_path=tmp_path / "s.wav")
    assert posts.posts == []


async def test_recognize_dispatches_a_repeat_after_min_track_change(sample_config, tmp_path):
    state = LoopState(last_track=TrackInfo(
        title="Song", artist="Artist", recognized_at=datetime.now() - timedelta(seconds=60)
    ))
    posts = Posts()
    recognizer = recognizer_returning(TrackInfo(title="Song", artist="Artist"))
    await recognize(sample_config, recognizer, posts, RecentAudio(960), state, state.generation,
                    sample_path=tmp_path / "s.wav")
    assert len(posts.posts) == 1


async def test_recognize_ignores_no_match(sample_config, tmp_path):
    posts, state = Posts(), LoopState()
    await recognize(sample_config, recognizer_returning(None), posts, RecentAudio(960), state,
                    state.generation, sample_path=tmp_path / "s.wav")
    assert posts.posts == []


async def test_recognize_logs_a_shazam_failure_without_raising(sample_config, tmp_path, caplog):
    recognizer = AsyncMock()
    recognizer.recognize.side_effect = OSError("Cannot connect to host amp.shazam.com")
    posts, state = Posts(), LoopState()
    with caplog.at_level(logging.WARNING):
        await recognize(sample_config, recognizer, posts, RecentAudio(960), state, state.generation,
                        sample_path=tmp_path / "s.wav")
    assert posts.posts == []
    assert "Recognition failed: Cannot connect to host amp.shazam.com" in caplog.text


async def test_recognize_discards_a_result_that_arrives_after_playback_changed(sample_config, tmp_path):
    posts, state = Posts(), LoopState()

    async def stopped_meanwhile(path):
        state.generation += 1
        return TrackInfo(title="Song", artist="Artist")

    recognizer = AsyncMock()
    recognizer.recognize.side_effect = stopped_meanwhile
    await recognize(sample_config, recognizer, posts, RecentAudio(960), state, state.generation,
                    sample_path=tmp_path / "s.wav")
    assert posts.posts == []


# --- service outputs ---


async def test_start_outputs_starts_service_outputs_and_isolates_failures(caplog):
    broken, working, plain = Service(fail=True), Service(), Recorder()
    with caplog.at_level(logging.ERROR):
        await start_outputs([broken, working, plain])
    assert broken.lifecycle == ["start"]
    assert working.lifecycle == ["start"]
    assert "Output handler error" in caplog.text


async def test_stop_outputs_stops_service_outputs():
    service = Service()
    await stop_outputs([service, Recorder()])
    assert service.lifecycle == ["stop"]


@patch("needledrop.app.RECOGNITION_TIMEOUT_SECONDS", 0.05)
async def test_recognize_gives_up_on_a_recognition_that_never_answers(sample_config, tmp_path, caplog):
    async def never_answers(path):
        await asyncio.Event().wait()

    recognizer = AsyncMock()
    recognizer.recognize.side_effect = never_answers
    posts, state = Posts(), LoopState()
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            recognize(sample_config, recognizer, posts, RecentAudio(960), state, state.generation,
                      sample_path=tmp_path / "s.wav"),
            timeout=2,
        )
    assert posts.posts == []
    assert "Recognition failed: timed out" in caplog.text


class AudioPosts(Posts):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[AudioChunk] = []

    def post_audio(self, chunk: AudioChunk) -> None:
        self.chunks.append(chunk)


def burst_arrivals(start: float, chunks: int) -> list[float]:
    """When each 20 ms chunk becomes readable if audio arrives in 6000-frame (125 ms) periods."""
    return [start + ((i + 1) * 960 + 5999) // 6000 * 0.125 + 0.003 for i in range(chunks)]


def timed_capture(now: list[float], *runs: list[float]):
    """A capture_stream stand-in that delivers LOUD chunks at the given arrival times, one run per call."""
    pending = list(runs)

    async def capture(config, chunk_frames):
        if not pending:
            await asyncio.Event().wait()
        for arrival in pending.pop(0):
            now[0] = arrival
            yield LOUD
            await asyncio.sleep(0)
        raise CaptureError("arecord exited with status 1: gone")

    return capture


async def run_capture_loop(config, capture, now, posts, until):
    detector = PlaybackDetector(threshold_dbfs=-50.0, sample_rate=48000, start_seconds=0.1, stop_seconds=5.0)
    await run_until(
        capture_loop(config, capture, detector, RecentAudio(960), posts, LoopState(), asyncio.Queue(),
                     now=lambda: now[0]),
        until,
    )


async def test_capture_loop_stamps_bursty_chunks_by_their_samples(sample_config):
    now, posts = [0.0], AudioPosts()
    arrivals = burst_arrivals(0.0, 500)
    await run_capture_loop(sample_config, timed_capture(now, arrivals), now, posts,
                           lambda: len(posts.chunks) == 500)
    stamps = [chunk.captured_at for chunk in posts.chunks][50:]
    assert all(abs((b - a) - 0.02) < 1e-6 for a, b in zip(stamps, stamps[1:]))


@patch("needledrop.app.CAPTURE_RETRY_SECONDS", 0)
async def test_capture_loop_restarts_its_clock_with_the_capture(sample_config):
    now, posts = [0.0], AudioPosts()
    capture = timed_capture(now, burst_arrivals(0.0, 50), burst_arrivals(5.0, 50))
    await run_capture_loop(sample_config, capture, now, posts, lambda: len(posts.chunks) == 100)
    assert abs(posts.chunks[50].captured_at - 5.0) < 0.15
