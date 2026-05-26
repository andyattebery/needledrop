import asyncio
import base64
import logging

import aiohttp

from needledrop.models import TrackInfo

logger = logging.getLogger(__name__)

TYPE_CORE = "636f7265"
CODE_TITLE = "6d696e6d"
CODE_ARTIST = "61736172"
CODE_ALBUM = "6173616c"
TYPE_SSNC = "73736e63"
CODE_PICT = "50494354"

FIFO_TIMEOUT = 2.0


def _build_item(type_hex: str, code_hex: str, data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f"<item><type>{type_hex}</type><code>{code_hex}</code>"
        f"<length>{len(data)}</length>"
        f'<data encoding="base64">{encoded}</data></item>'
    )


class OwnToneMetadataOutput:
    def __init__(self, metadata_fifo: str, session: aiohttp.ClientSession) -> None:
        self._fifo = metadata_fifo
        self._session = session

    async def on_track_recognized(self, track: TrackInfo) -> None:
        await self._write_item(TYPE_CORE, CODE_TITLE, track.title.encode("utf-8"))
        await self._write_item(TYPE_CORE, CODE_ARTIST, track.artist.encode("utf-8"))
        if track.album:
            await self._write_item(
                TYPE_CORE, CODE_ALBUM, track.album.encode("utf-8")
            )
        if track.artwork_url:
            await self._write_artwork(track.artwork_url)

    async def on_playback_stopped(self) -> None:
        pass

    async def _write_artwork(self, url: str) -> None:
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return
                image_data = await resp.read()
        except Exception as e:
            logger.warning("Failed to download artwork: %s", e)
            return
        await self._write_item(TYPE_SSNC, CODE_PICT, image_data)

    async def _write_item(self, type_hex: str, code_hex: str, data: bytes) -> None:
        item = _build_item(type_hex, code_hex, data)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._write_to_fifo, item),
                timeout=FIFO_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("FIFO write timed out")
        except OSError as e:
            logger.warning("FIFO write failed: %s", e)

    def _write_to_fifo(self, item: str) -> None:
        with open(self._fifo, "w") as f:
            f.write(item)
