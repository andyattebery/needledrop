import logging
import os

import pytest

from needledrop.models import AudioChunk
from needledrop.outputs.owntone_audio import OwnToneAudioOutput

CHUNK = AudioChunk(pcm=bytes(range(256)) * 15, captured_at=0.0)  # 960 frames


@pytest.fixture
def fifo(tmp_path):
    path = tmp_path / "Turntable"
    os.mkfifo(path)
    return path


@pytest.fixture
def reader(fifo):
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    yield fd
    os.close(fd)


def read_all(fd: int) -> bytes:
    data = b""
    while True:
        try:
            block = os.read(fd, 65536)
        except BlockingIOError:
            return data
        if not block:
            return data
        data += block


async def test_writes_audio_between_start_and_stop(fifo, reader):
    output = OwnToneAudioOutput(audio_fifo=str(fifo))
    await output.on_audio(CHUNK)
    await output.on_playback_started()
    await output.on_audio(CHUNK)
    assert read_all(reader) == CHUNK.pcm
    with pytest.raises(BlockingIOError):  # a writer still holds the pipe open
        os.read(reader, 1)

    await output.on_playback_stopped()
    await output.on_audio(CHUNK)
    assert os.read(reader, 1) == b""  # EOF: OwnTone stops


async def test_waits_for_a_reader_and_retries(fifo, caplog):
    output = OwnToneAudioOutput(audio_fifo=str(fifo))
    with caplog.at_level(logging.WARNING):
        await output.on_playback_started()
    assert "Can't open OwnTone pipe" in caplog.text

    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        await output.on_audio(CHUNK)  # within the retry interval: still waiting
        assert read_all(fd) == b""
        output._next_open = 0  # the interval has passed
        await output.on_audio(CHUNK)
        assert read_all(fd) == CHUNK.pcm
    finally:
        await output.on_playback_stopped()
        os.close(fd)


async def test_a_missing_pipe_is_logged_not_raised(tmp_path, caplog):
    output = OwnToneAudioOutput(audio_fifo=str(tmp_path / "missing"))
    with caplog.at_level(logging.WARNING):
        await output.on_playback_started()
        await output.on_audio(CHUNK)
    assert "Can't open OwnTone pipe" in caplog.text


async def test_a_full_pipe_drops_whole_frames(fifo, reader, caplog):
    output = OwnToneAudioOutput(audio_fifo=str(fifo))
    await output.on_playback_started()
    with caplog.at_level(logging.WARNING):
        for _ in range(200):  # far more than any pipe holds
            await output.on_audio(CHUNK)
    assert "OwnTone isn't reading fast enough" in caplog.text
    assert caplog.text.count("OwnTone isn't reading fast enough") == 1
    data = read_all(reader)
    assert len(data) % 4 == 0
    await output.on_playback_stopped()


async def test_reopens_after_owntone_closes_the_pipe(fifo, caplog):
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    output = OwnToneAudioOutput(audio_fifo=str(fifo))
    await output.on_playback_started()
    os.close(fd)
    with caplog.at_level(logging.WARNING):
        await output.on_audio(CHUNK)
    assert "OwnTone closed its pipe" in caplog.text

    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        output._next_open = 0
        await output.on_audio(CHUNK)
        assert read_all(fd) == CHUNK.pcm
    finally:
        await output.on_playback_stopped()
        os.close(fd)


async def test_track_recognized_is_ignored(fifo):
    output = OwnToneAudioOutput(audio_fifo=str(fifo))
    await output.on_track_recognized(None)
