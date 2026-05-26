import logging

from needledrop.models import TrackInfo

logger = logging.getLogger(__name__)


class LogOutput:
    async def on_track_recognized(self, track: TrackInfo) -> None:
        logger.info("Recognized: %s - %s [%s]", track.artist, track.title, track.album)

    async def on_playback_stopped(self) -> None:
        logger.info("Playback stopped")
