import asyncio
import itertools
import logging
import re
from dataclasses import dataclass

from lameenc import Encoder

from needledrop.audio.capture import CHANNELS
from needledrop.config import Mp3StreamConfig
from needledrop.models import AudioChunk, TrackInfo

logger = logging.getLogger(__name__)

# The rates MP3 carries (MPEG-1, 2 and 2.5). LAME would resample any other, inline on the event loop.
MP3_RATES = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000}
# LAME's -q2 (0 is slowest, 9 fastest), the highest lameenc documents.
LAME_QUALITY = 2
# Audio bytes between ICY metadata blocks, Icecast's default. Clients read it from icy-metaint.
METAINT = 16000
# How far a listener may fall behind before it's dropped, as Icecast's queue size: about 13 s at 320 kbit/s.
CLIENT_QUEUE_LIMIT = 512 * 1024
HEADER_LIMIT = 8 * 1024
HEADER_TIMEOUT = 10.0
# A metadata block's length byte counts 16-byte units.
MAX_BLOCK = 255 * 16
# Music Assistant's User-Agent: "Lavf/60.16.100.MusicAssistant" up to 2.10, "Music Assistant/<version> (…)" after.
MUSIC_ASSISTANT = re.compile(r"music ?assistant", re.IGNORECASE)


def stream_title(track: TrackInfo | None, name: str, *, music_assistant: bool) -> str:
    """The StreamTitle for a track, or the stream's name while there's none.

    Music Assistant gets `"Title" by Artist from "Album"`, the one form it reads an album from; a " inside a quoted
    part would break its match. Everyone else gets the usual `Artist - Title`. A ' would end the title early for a
    client that looks for the closing "';", so it becomes ’.
    """
    if track is None:
        title = name
    elif music_assistant:
        title = f'"{_curly(track.title)}" by {track.artist}'
        if track.album:
            title += f' from "{_curly(track.album)}"'
    else:
        title = f"{track.artist} - {track.title}"
    return title.replace("'", "’")


def _curly(text: str) -> str:
    return text.replace('"', "”")


def icy_metadata_block(title: str, art_url: str | None) -> bytes:
    """An ICY metadata block: its length byte, then StreamTitle and StreamUrl, NUL-padded to whole 16-byte units.

    It always ends in a NUL, as Icecast's blocks do. One too long for its length byte loses StreamUrl, then the end
    of the title.
    """
    url = (art_url or "").replace("'", "%27")
    payload = f"StreamTitle='{title}';StreamUrl='{url}';".encode()
    if len(payload) >= MAX_BLOCK:
        room = MAX_BLOCK - 1 - len("StreamTitle='';StreamUrl='';")
        title = title.encode()[:room].decode(errors="ignore")
        payload = f"StreamTitle='{title}';StreamUrl='';".encode()
    units = len(payload) // 16 + 1
    return bytes([units]) + payload.ljust(units * 16, b"\0")


@dataclass(frozen=True)
class _Metadata:
    """The current title's metadata blocks, one per title form, and a version that changes with them."""

    version: int
    generic: bytes
    music_assistant: bytes


class _Listener:
    """A client playing the stream, and, if it asked for metadata, its place between metadata blocks."""

    def __init__(self, writer: asyncio.StreamWriter, *, metadata: bool, music_assistant: bool) -> None:
        self._transport = writer.transport
        self.peer = _peer(writer)
        self._metadata = metadata
        self._music_assistant = music_assistant
        self._until_block = METAINT
        self._sent_version: int | None = None

    @property
    def backlog(self) -> int:
        """Bytes written for this client that the kernel hasn't taken yet."""
        return self._transport.get_write_buffer_size()

    def send(self, mp3: bytes, metadata: _Metadata) -> None:
        # The transport takes everything and never blocks, so a slow client delays no one.
        if self._transport.is_closing():
            return
        if not self._metadata:
            self._transport.write(mp3)
            return
        parts, start = [], 0
        while len(mp3) - start >= self._until_block:
            end = start + self._until_block
            parts += (mp3[start:end], self._block(metadata))
            start, self._until_block = end, METAINT
        parts.append(mp3[start:])
        self._until_block -= len(mp3) - start
        self._transport.write(b"".join(parts))

    def drop(self) -> None:
        self._transport.abort()

    def _block(self, metadata: _Metadata) -> bytes:
        """The current block if this client hasn't had it yet, else the empty block."""
        if metadata.version == self._sent_version:
            return b"\0"
        self._sent_version = metadata.version
        return metadata.music_assistant if self._music_assistant else metadata.generic


class Mp3StreamOutput:
    """The turntable as an Icecast-compatible MP3 stream, which any player can open by URL.

    ICY metadata carries the recognized track's title and art to players that ask for it. The server is built in, as
    OwnTone's and MPD's are; docs/development.md compares how it answers with Icecast, MPD, OwnTone and Liquidsoap.
    Encoding runs only while someone listens, and between records the stream carries the turntable's silence.
    """

    def __init__(self, config: Mp3StreamConfig, sample_rate: int) -> None:
        if sample_rate not in MP3_RATES:
            rates = ", ".join(str(rate) for rate in sorted(MP3_RATES))
            raise ValueError(f"mp3_stream can't stream {sample_rate} Hz audio; MP3 carries {rates} Hz")
        self._config = config
        self._sample_rate = sample_rate
        self._server: asyncio.Server | None = None
        self._listeners: list[_Listener] = []
        self._encoder: Encoder | None = None
        self._versions = itertools.count()
        self._metadata = self._metadata_for(None)

    @property
    def clients(self) -> int:
        """Clients playing the stream. The encoder exists exactly while there are any."""
        return len(self._listeners)

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._serve, "0.0.0.0", self._config.port, limit=HEADER_LIMIT
        )
        logger.info("MP3 stream %r is on port %d at %s", self._config.name, self._config.port, self._config.path)

    async def stop(self) -> None:
        # Not waiting for the server to finish closing: a connection accepted as it closes can hang that wait.
        # needledrop exits next, closing whatever is left.
        if self._server is not None:
            self._server.close()
        for listener in self._listeners:
            listener.drop()

    async def on_track_recognized(self, track: TrackInfo) -> None:
        self._metadata = self._metadata_for(track)

    async def on_playback_started(self) -> None:
        self._metadata = self._metadata_for(None)

    async def on_playback_stopped(self) -> None:
        self._metadata = self._metadata_for(None)

    async def on_audio(self, chunk: AudioChunk) -> None:
        if not self._listeners:
            return
        mp3 = self._encoder.encode(chunk.pcm)
        if not mp3:
            return
        for listener in self._listeners:
            listener.send(mp3, self._metadata)
            if listener.backlog > CLIENT_QUEUE_LIMIT:
                logger.info(
                    "MP3 stream: dropping %s, which fell %d KiB behind", listener.peer, listener.backlog // 1024
                )
                listener.drop()

    def _metadata_for(self, track: TrackInfo | None) -> _Metadata:
        art = track.artwork_url if track else None
        name = self._config.name
        return _Metadata(
            version=next(self._versions),
            generic=icy_metadata_block(stream_title(track, name, music_assistant=False), art),
            music_assistant=icy_metadata_block(stream_title(track, name, music_assistant=True), art),
        )

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        listener = None
        try:
            request = await _read_request(reader)
            if request is None:
                return
            method, path, headers = request
            if path != self._config.path:
                writer.write(_refusal("404 Not Found"))
            elif method not in ("GET", "HEAD"):
                writer.write(_refusal("405 Method Not Allowed", "Allow: GET, HEAD"))
            else:
                metadata = headers.get("icy-metadata") == "1"
                writer.write(self._response_head(metadata))
                if method == "GET":
                    user_agent = headers.get("user-agent", "")
                    listener = _Listener(
                        writer, metadata=metadata, music_assistant=bool(MUSIC_ASSISTANT.search(user_agent))
                    )
                    self._add(listener, user_agent)
                    # Nothing more is read from a listener; this returns when it hangs up.
                    while await reader.read(4096):
                        pass
        except ConnectionError:
            pass
        except Exception:
            logger.exception("MP3 stream connection failed")
        finally:
            if listener is None:
                writer.close()
            else:
                self._remove(listener)
                # Audio still queued for a client that has gone is of no use to it.
                listener.drop()

    def _response_head(self, metadata: bool) -> bytes:
        lines = [
            "HTTP/1.1 200 OK",
            "Content-Type: audio/mpeg",
            "Connection: close",
            "Cache-Control: no-cache, no-store",
            "Pragma: no-cache",
            "Access-Control-Allow-Origin: *",
            "Access-Control-Expose-Headers: icy-br, icy-metaint, icy-name, icy-pub",
            f"icy-name: {self._config.name}",
            f"icy-br: {self._config.bitrate}",
            "icy-pub: 0",
        ]
        if metadata:
            lines.append(f"icy-metaint: {METAINT}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def _add(self, listener: _Listener, user_agent: str) -> None:
        if not self._listeners:
            self._encoder = self._new_encoder()
        self._listeners.append(listener)
        logger.info("MP3 stream: %s is listening (%s)", listener.peer, user_agent or "no User-Agent")

    def _remove(self, listener: _Listener) -> None:
        self._listeners.remove(listener)
        if not self._listeners:
            self._encoder = None
        logger.info("MP3 stream: %s stopped listening", listener.peer)

    def _new_encoder(self) -> Encoder:
        encoder = Encoder()
        encoder.set_bit_rate(self._config.bitrate)
        encoder.set_in_sample_rate(self._sample_rate)
        encoder.set_channels(CHANNELS)
        encoder.set_quality(LAME_QUALITY)
        encoder.silence()
        return encoder


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]] | None:
    """The request's method, path and headers (by lowercase name), or None for a request to drop.

    A request is dropped if it isn't complete within HEADER_TIMEOUT, runs past HEADER_LIMIT, or has no request line.
    """
    lines: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(HEADER_TIMEOUT):
            while (line := await reader.readline()) not in (b"\r\n", b"\n"):
                size += len(line)
                if not line.endswith(b"\n") or size > HEADER_LIMIT:
                    return None
                lines.append(line)
    except (TimeoutError, ValueError):
        # ValueError: a line longer than the reader's limit.
        return None
    request_line = lines[0].split() if lines else []
    if len(request_line) != 3:
        return None
    method, target, _version = (part.decode("latin-1") for part in request_line)
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.decode("latin-1").partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    # The path alone names the stream, as Icecast's mounts do; players may add a query to defeat caches.
    return method, target.partition("?")[0], headers


def _refusal(status: str, *headers: str) -> bytes:
    lines = (f"HTTP/1.1 {status}", "Content-Length: 0", "Connection: close", *headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def _peer(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    return peer[0] if peer else "a client"
