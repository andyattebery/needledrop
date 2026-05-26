import io
import json
import logging

import aiohttp
from PIL import Image

from needledrop.models import TrackInfo

logger = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=5)


class TuneshineOutput:
    def __init__(self, host: str, session: aiohttp.ClientSession) -> None:
        self._url = f"http://{host}"
        self._session = session

    async def on_track_recognized(self, track: TrackInfo) -> None:
        metadata = {
            "trackName": track.title,
            "artistName": track.artist,
        }
        if track.album:
            metadata["albumName"] = track.album

        if track.artwork_url:
            image_bytes = await self._download_and_convert(track.artwork_url)
            if image_bytes:
                await self._post_multipart(image_bytes, metadata)
                return

        await self._post_json(metadata)

    async def on_playback_stopped(self) -> None:
        try:
            await self._session.delete(
                f"{self._url}/image", timeout=TIMEOUT
            )
        except Exception as e:
            logger.warning("Tuneshine DELETE /image failed: %s", e)

    async def _download_and_convert(self, url: str) -> bytes | None:
        try:
            async with self._session.get(url, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
        except Exception as e:
            logger.warning("Artwork download failed: %s", e)
            return None

        try:
            img = Image.open(io.BytesIO(raw))
            img = img.resize((64, 64), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="WebP")
            return buf.getvalue()
        except Exception as e:
            logger.warning("Artwork conversion failed: %s", e)
            return None

    async def _post_multipart(self, image: bytes, metadata: dict) -> None:
        data = aiohttp.FormData()
        data.add_field("image", image, content_type="image/webp")
        data.add_field("metadata", json.dumps(metadata))
        try:
            await self._session.post(
                f"{self._url}/image", data=data, timeout=TIMEOUT
            )
        except Exception as e:
            logger.warning("Tuneshine POST /image (multipart) failed: %s", e)

    async def _post_json(self, metadata: dict) -> None:
        try:
            await self._session.post(
                f"{self._url}/image", json=metadata, timeout=TIMEOUT
            )
        except Exception as e:
            logger.warning("Tuneshine POST /image (json) failed: %s", e)
