import logging
from pathlib import Path

from shazamio import Shazam

from needledrop.models import TrackInfo

logger = logging.getLogger(__name__)


class ShazamIORecognizer:
    def __init__(self) -> None:
        self._shazam = Shazam()

    async def recognize(self, wav_path: Path) -> TrackInfo | None:
        result = await self._shazam.recognize(str(wav_path))
        track = result.get("track")
        if not track:
            return None

        title = track.get("title")
        artist = track.get("subtitle")
        if not title or not artist:
            return None

        album = self._extract_album(track)
        artwork_url = track.get("images", {}).get("coverart")

        return TrackInfo(
            title=title,
            artist=artist,
            album=album,
            artwork_url=artwork_url,
        )

    def _extract_album(self, track: dict) -> str | None:
        for section in track.get("sections", []):
            if section.get("type") != "SONG":
                continue
            for item in section.get("metadata", []):
                if item.get("title") == "Album":
                    return item.get("text")
        return None
