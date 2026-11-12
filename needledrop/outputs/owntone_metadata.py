import asyncio
import base64
import errno
import logging
import os
import select
import time

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
        self._owntone_not_reading = False

    async def on_track_recognized(self, track: TrackInfo) -> None:
        await self._write_item(TYPE_CORE, CODE_TITLE, track.title.encode("utf-8"))
        await self._write_item(TYPE_CORE, CODE_ARTIST, track.artist.encode("utf-8"))
        if track.album:
            await self._write_item(
                TYPE_CORE, CODE_ALBUM, track.album.encode("utf-8")
            )
        if track.artwork_url:
            await self._write_artwork(track.artwork_url)

    async def on_playback_started(self) -> None:
        pass

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
            await asyncio.to_thread(self._write_to_fifo, item)
        except OSError as e:
            if e.errno == errno.ENXIO:
                # OwnTone reads the pipe only while it plays the turntable.
                if not self._owntone_not_reading:
                    logger.info("OwnTone isn't reading its metadata pipe %s; skipping track info", self._fifo)
                    self._owntone_not_reading = True
                return
            logger.warning("FIFO write failed: %s", e)
            return
        self._owntone_not_reading = False

    def _write_to_fifo(self, item: str) -> None:
        """Write one item, giving up on OwnTone after FIFO_TIMEOUT, so no thread is held longer.

        Opening without blocking fails at once (ENXIO) while nothing reads the pipe; a blocking open
        would hold its thread until OwnTone next plays the turntable.
        """
        fd = os.open(self._fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            remaining = memoryview(item.encode("utf-8"))
            deadline = time.monotonic() + FIFO_TIMEOUT
            while remaining:
                try:
                    remaining = remaining[os.write(fd, remaining) :]
                except BlockingIOError:
                    wait = deadline - time.monotonic()
                    if wait <= 0 or not select.select([], [fd], [], wait)[1]:
                        raise TimeoutError(f"OwnTone stopped reading {self._fifo}") from None
        finally:
            os.close(fd)
