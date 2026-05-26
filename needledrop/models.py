from dataclasses import dataclass, field
from datetime import datetime


@dataclass(eq=False)
class TrackInfo:
    title: str
    artist: str
    album: str | None = None
    artwork_url: str | None = None
    duration_seconds: int | None = None
    recognized_at: datetime = field(default_factory=datetime.now)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TrackInfo):
            return NotImplemented
        return (self.title, self.artist) == (other.title, other.artist)

    def __hash__(self) -> int:
        return hash((self.title, self.artist))
