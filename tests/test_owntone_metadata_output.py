import asyncio
import base64
import logging
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


async def test_fifo_timeout_logged(mock_session, caplog):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(title="Song", artist="Artist", album=None, artwork_url=None)

    with patch(
        "needledrop.outputs.owntone_metadata.asyncio.to_thread",
        side_effect=asyncio.TimeoutError,
    ), patch(
        "needledrop.outputs.owntone_metadata.asyncio.wait_for",
        side_effect=asyncio.TimeoutError,
    ), caplog.at_level(logging.WARNING):
        await output.on_track_recognized(track)
    assert "FIFO write timed out" in caplog.text


async def test_fifo_oserror_logged(mock_session, caplog):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    track = TrackInfo(title="Song", artist="Artist", album=None, artwork_url=None)

    with patch.object(
        output, "_write_to_fifo", side_effect=OSError("broken pipe")
    ), patch(
        "needledrop.outputs.owntone_metadata.asyncio.wait_for",
        side_effect=OSError("broken pipe"),
    ), caplog.at_level(logging.WARNING):
        await output.on_track_recognized(track)
    assert "FIFO write failed" in caplog.text


async def test_on_playback_stopped_is_noop(mock_session):
    output = OwnToneMetadataOutput(metadata_fifo="/tmp/fifo", session=mock_session)
    await output.on_playback_stopped()
