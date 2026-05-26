from pathlib import Path
from unittest.mock import AsyncMock, patch

from needledrop.recognition.shazamio_recognizer import ShazamIORecognizer

SHAZAM_RESPONSE = {
    "track": {
        "title": "Let Your Love Grow Tall",
        "subtitle": "Passion Pit",
        "sections": [
            {"type": "SONG", "metadata": [{"title": "Album", "text": "Manners"}]}
        ],
        "images": {"coverart": "https://example.com/art.jpg"},
    }
}


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_successful_recognition(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = SHAZAM_RESPONSE
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))

    assert track is not None
    assert track.title == "Let Your Love Grow Tall"
    assert track.artist == "Passion Pit"
    assert track.album == "Manners"
    assert track.artwork_url == "https://example.com/art.jpg"


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_no_track_in_response(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = {}
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))
    assert track is None


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_missing_title(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = {
        "track": {"subtitle": "Artist", "sections": [], "images": {}}
    }
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))
    assert track is None


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_missing_artist(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = {
        "track": {"title": "Song", "sections": [], "images": {}}
    }
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))
    assert track is None


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_no_song_section_album_is_none(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = {
        "track": {
            "title": "Song",
            "subtitle": "Artist",
            "sections": [{"type": "OTHER"}],
            "images": {"coverart": "https://example.com/art.jpg"},
        }
    }
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))
    assert track is not None
    assert track.album is None


@patch("needledrop.recognition.shazamio_recognizer.Shazam")
async def test_missing_images_artwork_is_none(mock_shazam_cls):
    mock_shazam = AsyncMock()
    mock_shazam.recognize.return_value = {
        "track": {
            "title": "Song",
            "subtitle": "Artist",
            "sections": [],
        }
    }
    mock_shazam_cls.return_value = mock_shazam

    recognizer = ShazamIORecognizer()
    track = await recognizer.recognize(Path("/tmp/test.wav"))
    assert track is not None
    assert track.artwork_url is None
