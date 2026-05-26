import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from needledrop.models import TrackInfo
from needledrop.outputs.tuneshine import TuneshineOutput


@pytest.fixture
def tuneshine(mock_session):
    return TuneshineOutput(host="192.168.1.120", session=mock_session)


async def test_track_with_artwork_posts_multipart(tuneshine, mock_session):
    track = TrackInfo(
        title="Song", artist="Artist", album="Album",
        artwork_url="https://example.com/art.jpg",
    )
    with patch("needledrop.outputs.tuneshine.Image") as mock_image:
        mock_img = MagicMock()
        mock_image.open.return_value = mock_img
        mock_img.resize.return_value = mock_img
        mock_img.save = MagicMock(side_effect=lambda buf, **kw: buf.write(b"webp"))

        await tuneshine.on_track_recognized(track)

    mock_session.post.assert_called_once()
    call_kwargs = mock_session.post.call_args[1]
    assert "data" in call_kwargs


async def test_track_without_artwork_posts_json(tuneshine, mock_session):
    track = TrackInfo(title="Song", artist="Artist", album="Album", artwork_url=None)
    await tuneshine.on_track_recognized(track)

    mock_session.post.assert_called_once()
    call_kwargs = mock_session.post.call_args[1]
    assert "json" in call_kwargs
    assert call_kwargs["json"]["trackName"] == "Song"
    assert call_kwargs["json"]["artistName"] == "Artist"
    assert call_kwargs["json"]["albumName"] == "Album"


async def test_metadata_keys(tuneshine, mock_session):
    track = TrackInfo(title="Song", artist="Artist", album="Album", artwork_url=None)
    await tuneshine.on_track_recognized(track)

    json_body = mock_session.post.call_args[1]["json"]
    assert "trackName" in json_body
    assert "artistName" in json_body
    assert "albumName" in json_body


async def test_no_album_omits_key(tuneshine, mock_session):
    track = TrackInfo(title="Song", artist="Artist", album=None, artwork_url=None)
    await tuneshine.on_track_recognized(track)

    json_body = mock_session.post.call_args[1]["json"]
    assert "albumName" not in json_body


async def test_on_playback_stopped_deletes(tuneshine, mock_session):
    await tuneshine.on_playback_stopped()
    mock_session.delete.assert_called_once()
    assert "/image" in mock_session.delete.call_args[0][0]


async def test_image_resized_to_64x64(tuneshine, mock_session):
    track = TrackInfo(
        title="Song", artist="Artist",
        artwork_url="https://example.com/art.jpg",
    )
    with patch("needledrop.outputs.tuneshine.Image") as mock_image:
        mock_img = MagicMock()
        mock_image.open.return_value = mock_img
        mock_img.resize.return_value = mock_img
        mock_img.save = MagicMock(side_effect=lambda buf, **kw: buf.write(b"webp"))
        mock_image.LANCZOS = 1

        await tuneshine.on_track_recognized(track)

    mock_img.resize.assert_called_once_with((64, 64), 1)


async def test_artwork_download_failure_falls_back_to_json(tuneshine, mock_session):
    mock_session.get.return_value.__aenter__.side_effect = Exception("fail")
    track = TrackInfo(
        title="Song", artist="Artist",
        artwork_url="https://example.com/art.jpg",
    )
    await tuneshine.on_track_recognized(track)

    call_kwargs = mock_session.post.call_args[1]
    assert "json" in call_kwargs


async def test_image_conversion_failure_falls_back_to_json(tuneshine, mock_session):
    track = TrackInfo(
        title="Song", artist="Artist",
        artwork_url="https://example.com/art.jpg",
    )
    with patch("needledrop.outputs.tuneshine.Image") as mock_image:
        mock_image.open.side_effect = Exception("bad image")

        await tuneshine.on_track_recognized(track)

    call_kwargs = mock_session.post.call_args[1]
    assert "json" in call_kwargs


async def test_post_failure_logged(tuneshine, mock_session, caplog):
    mock_session.post.side_effect = Exception("connection refused")
    track = TrackInfo(title="Song", artist="Artist", artwork_url=None)

    with caplog.at_level(logging.WARNING):
        await tuneshine.on_track_recognized(track)
    assert "Tuneshine POST /image" in caplog.text


async def test_delete_failure_logged(tuneshine, mock_session, caplog):
    mock_session.delete.side_effect = Exception("connection refused")

    with caplog.at_level(logging.WARNING):
        await tuneshine.on_playback_stopped()
    assert "Tuneshine DELETE /image" in caplog.text
