from typing import Protocol

from needledrop.models import TrackInfo


class OutputHandler(Protocol):
    async def on_track_recognized(self, track: TrackInfo) -> None: ...

    async def on_playback_stopped(self) -> None: ...
