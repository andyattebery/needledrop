import asyncio
import base64
import logging
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from needledrop.models import TrackInfo
from needledrop.outputs.owntone_metadata import (
    CODE_ALBUM,
    CODE_ARTIST,
    CODE_PICT,
    CODE_TITLE,
    TYPE_CORE,
    TYPE_SSNC,
    OwnToneMetadataOutput,
    _build_item,
)


def test_build_item_correct_xml():
    data = b"Hello"
    item = _build_item(TYPE_CORE, CODE_TITLE, data)
    expected_b64 = base64.b64encode(data).decode("ascii")
    assert f"<type>{TYPE_CORE}</type>" in item
    assert f"<code>{CODE_TITLE}</code>" in item
    assert f"<length>{len(data)}</length>" in item
    assert f'<data encoding="base64">{expected_b64}</data>' in item


def test_build_item_length_is_byte_length():
    data = "héllo".encode("utf-8")
    item = _build_item(TYPE_CORE, CODE_TITLE, data)
    assert f"<length>{len(data)}</length>" in item
    assert len(data) == 6


def test_build_item_base64_roundtrips():
    data = b"test data with special chars: \xff\x00"
    item = _build_item(TYPE_CORE, CODE_TITLE, data)
    b64_start = item.index('base64">') + len('base64">')
    b64_end = item.index("</data>")
    decoded = base64.b64decode(item[b64_start:b64_end])
    assert decoded == data


async def test_writes_title_artist_album(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(title="Song", artist="Artist", album="Album")

    with patch.object(output, "_write_to_fifo") as mock_fifo:
        await output.on_track_recognized(track)

    assert mock_fifo.call_count == 3


async def test_skips_album_when_none(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(title="Song", artist="Artist", album=None)

    with patch.object(output, "_write_to_fifo") as mock_fifo:
        await output.on_track_recognized(track)

    assert mock_fifo.call_count == 2


async def test_downloads_and_writes_artwork(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(
        title="Song", artist="Artist", artwork_url="https://example.com/art.jpg"
    )

    with patch.object(output, "_write_to_fifo") as mock_fifo:
        await output.on_track_recognized(track)

    calls = mock_fifo.call_args_list
    artwork_call = calls[-1][0][0]
    assert TYPE_SSNC in artwork_call
    assert CODE_PICT in artwork_call


async def test_artwork_download_failure_logged(mock_session, caplog):
    mock_session.get.return_value.__aenter__.side_effect = Exception("network error")
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(
        title="Song", artist="Artist", artwork_url="https://example.com/art.jpg"
    )

    with patch.object(output, "_write_to_fifo"), caplog.at_level(logging.WARNING):
        await output.on_track_recognized(track)
    assert "Failed to download artwork" in caplog.text


@pytest.fixture
def fifo(tmp_path):
    path = tmp_path / "Turntable.metadata"
    os.mkfifo(path)
    return path


def read_all(fd: int) -> bytes:
    data = b""
    while True:
        try:
            block = os.read(fd, 1 << 20)
        except BlockingIOError:
            return data
        if not block:
            return data
        data += block


async def test_track_info_reaches_a_reading_owntone(mock_session, fifo):
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        output = OwnToneMetadataOutput(metadata_fifo=str(fifo), session=mock_session)
        await output.on_track_recognized(TrackInfo(title="Song", artist="Artist"))
        written = read_all(reader).decode()
    finally:
        os.close(reader)
    assert written == _build_item(TYPE_CORE, CODE_TITLE, b"Song") + _build_item(TYPE_CORE, CODE_ARTIST, b"Artist")


async def test_without_a_reader_writes_are_skipped_at_once(mock_session, fifo, caplog):
    output = OwnToneMetadataOutput(metadata_fifo=str(fifo), session=mock_session)
    track = TrackInfo(title="Song", artist="Artist", album="Album")
    threads_before = threading.active_count()
    start = time.monotonic()
    with caplog.at_level(logging.INFO):
        await output.on_track_recognized(track)
        await output.on_track_recognized(track)
    assert time.monotonic() - start < 0.5
    assert caplog.text.count("OwnTone isn't reading its metadata pipe") == 1
    assert "FIFO write failed" not in caplog.text
    assert threading.active_count() <= threads_before + 1


async def test_a_reader_that_stops_reading_times_out_without_holding_a_thread(mock_session, fifo, caplog):
    mock_session.get.return_value.__aenter__.return_value.read = AsyncMock(return_value=bytes(300_000))
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # open, but never read
    try:
        output = OwnToneMetadataOutput(metadata_fifo=str(fifo), session=mock_session)
        track = TrackInfo(title="Song", artist="Artist", artwork_url="https://example.com/art.jpg")
        start = time.monotonic()
        with patch("needledrop.outputs.owntone_metadata.FIFO_TIMEOUT", 0.2), caplog.at_level(logging.WARNING):
            await output.on_track_recognized(track)
        elapsed = time.monotonic() - start
    finally:
        os.close(reader)
    assert elapsed < 1.0
    assert "FIFO write failed: OwnTone stopped reading" in caplog.text


async def test_a_missing_pipe_is_logged(mock_session, tmp_path, caplog):
    output = OwnToneMetadataOutput(metadata_fifo=str(tmp_path / "missing"), session=mock_session)
    with caplog.at_level(logging.WARNING):
        await output.on_track_recognized(TrackInfo(title="Song", artist="Artist"))
    assert "FIFO write failed" in caplog.text


async def test_on_playback_stopped_is_noop(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    await output.on_playback_stopped()


async def test_on_playback_started_writes_nothing(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    with patch.object(output, "_write_item") as write_item:
        await output.on_playback_started()
    write_item.assert_not_called()
