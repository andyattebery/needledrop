from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from needledrop.config import (
    AudioConfig,
    Config,
    LastFMConfig,
    LogConfig,
    OutputsConfig,
    OwnToneMetadataConfig,
    RecognitionConfig,
    TuneshineConfig,
)
from needledrop.models import TrackInfo


@pytest.fixture
def sample_track():
    return TrackInfo(
        title="Song",
        artist="Artist",
        album="Album",
        artwork_url="https://example.com/art.jpg",
    )


@pytest.fixture
def old_track():
    return TrackInfo(
        title="Old Song",
        artist="Old Artist",
        album="Old Album",
        recognized_at=datetime.now() - timedelta(seconds=60),
    )


@pytest.fixture
def sample_config():
    return Config(
        audio=AudioConfig(
            alsa_device="dsnoopusb",
            sample_rate=48000,
            sample_duration_seconds=12,
            silence_threshold_dbfs=-50.0,
        ),
        recognition=RecognitionConfig(
            interval_seconds=20,
            min_track_change_seconds=30,
        ),
        outputs=OutputsConfig(
            lastfm=LastFMConfig(
                enabled=True,
                api_key="key",
                api_secret="secret",
                username="user",
                password_hash="hash",
                scrobble_threshold_seconds=30,
            ),
            owntone_metadata=OwnToneMetadataConfig(
                enabled=True,
                metadata_fifo="/tmp/test.metadata",
            ),
            tuneshine=TuneshineConfig(
                enabled=True,
                host="192.168.1.120",
            ),
            log=LogConfig(enabled=True),
        ),
    )


@pytest.fixture
def mock_session():
    session = MagicMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=b"fake-image-data")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.get.return_value = ctx
    session.post = AsyncMock()
    session.delete = AsyncMock()
    return session
