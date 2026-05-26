import asyncio
import logging
from datetime import datetime

import pylast

from needledrop.models import TrackInfo

logger = logging.getLogger(__name__)


class LastFMOutput:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        username: str,
        password_hash: str,
        scrobble_threshold_seconds: int,
    ) -> None:
        self._network = pylast.LastFMNetwork(
            api_key=api_key,
            api_secret=api_secret,
            username=username,
            password_hash=password_hash,
        )
        self._scrobble_threshold = scrobble_threshold_seconds
        self._prev_track: TrackInfo | None = None
        self._prev_recognized_at: datetime | None = None

    async def on_track_recognized(self, track: TrackInfo) -> None:
        if self._prev_track and self._prev_track != track:
            await self._maybe_scrobble_previous()

        try:
            await asyncio.to_thread(
                self._network.update_now_playing,
                artist=track.artist,
                title=track.title,
                album=track.album,
            )
        except (pylast.WSError, pylast.NetworkError) as e:
            logger.warning("Last.fm now-playing failed: %s", e)

        self._prev_track = track
        self._prev_recognized_at = track.recognized_at

    async def on_playback_stopped(self) -> None:
        await self._maybe_scrobble_previous()
        self._prev_track = None
        self._prev_recognized_at = None

    async def _maybe_scrobble_previous(self) -> None:
        if not self._prev_track or not self._prev_recognized_at:
            return
        elapsed = (datetime.now() - self._prev_recognized_at).total_seconds()
        if elapsed < self._scrobble_threshold:
            return
        try:
            await asyncio.to_thread(
                self._network.scrobble,
                artist=self._prev_track.artist,
                title=self._prev_track.title,
                album=self._prev_track.album,
                timestamp=int(self._prev_recognized_at.timestamp()),
            )
        except (pylast.WSError, pylast.NetworkError) as e:
            logger.warning("Last.fm scrobble failed: %s", e)
