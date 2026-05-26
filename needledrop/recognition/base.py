from pathlib import Path
from typing import Protocol

from needledrop.models import TrackInfo


class Recognizer(Protocol):
    async def recognize(self, wav_path: Path) -> TrackInfo | None: ...
