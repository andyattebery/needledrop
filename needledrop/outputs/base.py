from typing import Protocol, runtime_checkable

from needledrop.models import AudioChunk, TrackInfo


class OutputHandler(Protocol):
    """What every output implements: the playback and recognition events."""

    async def on_track_recognized(self, track: TrackInfo) -> None: ...

    async def on_playback_started(self) -> None: ...

    async def on_playback_stopped(self) -> None: ...


@runtime_checkable
class AudioOutput(Protocol):
    """An output that also takes the captured audio, chunk by chunk."""

    async def on_audio(self, chunk: AudioChunk) -> None: ...


@runtime_checkable
class ServiceOutput(Protocol):
    """An output with something to run for the app's lifetime, such as a network listener."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
