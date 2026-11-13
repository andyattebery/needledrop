import asyncio
import logging
import socket
from unittest.mock import patch

import lameenc
import pytest

from helpers import free_port, wait_for
from needledrop.config import Mp3StreamConfig
from needledrop.models import AudioChunk, TrackInfo
from needledrop.outputs.mp3_stream import Mp3StreamOutput, icy_metadata_block, stream_title

SILENCE = AudioChunk(pcm=bytes(3840), captured_at=0.0)  # 20 ms of S16_LE stereo at 48 kHz
# At 48 kHz and 320 kbit/s every MPEG-1 Layer III frame is 144 * 320000 / 48000 bytes, with no padding.
FRAME_BYTES = 960
MA_2_10 = "Lavf/60.16.100.MusicAssistant"
MA_DEV = "Music Assistant/2.11.0 (https://music-assistant.io)"
VLC = "VLC/3.0.21 LibVLC/3.0.21"


@pytest.fixture
def port():
    return free_port()


@pytest.fixture
async def stream(port):
    output = Mp3StreamOutput(Mp3StreamConfig(enabled=True, port=port), sample_rate=48000)
    await output.start()
    yield output
    await output.stop()


async def request(port: int, *lines: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connects and sends the request lines, then the blank line that ends them."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write("".join(f"{line}\r\n" for line in lines).encode() + b"\r\n")
    await writer.drain()
    return reader, writer


async def response_head(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    head = (await reader.readuntil(b"\r\n\r\n")).decode()
    status, *lines = head.removesuffix("\r\n\r\n").split("\r\n")
    fields = (line.partition(":") for line in lines)
    return status, {name.lower(): value.strip() for name, _, value in fields}


async def closed(reader: asyncio.StreamReader) -> bool:
    """Whether the server closes the connection without sending anything more.

    A server that closes with part of the request unread resets the connection instead.
    """
    async with asyncio.timeout(2):
        try:
            return await reader.read() == b""
        except ConnectionResetError:
            return True


def frames_at_every_frame_boundary(body: bytes) -> bool:
    return all(
        body[i] == 0xFF and body[i + 1] & 0xE0 == 0xE0
        for i in range(0, len(body) - 1, FRAME_BYTES)
    )


def test_refuses_a_rate_mp3_cannot_carry():
    with pytest.raises(ValueError, match="96000"):
        Mp3StreamOutput(Mp3StreamConfig(enabled=True), sample_rate=96000)
    Mp3StreamOutput(Mp3StreamConfig(enabled=True), sample_rate=44100)


async def test_a_client_gets_mp3_with_the_expected_headers(stream, port):
    reader, writer = await request(port, "GET /turntable.mp3 HTTP/1.1", "Host: pi", "User-Agent: VLC/3.0.21")
    status, headers = await response_head(reader)
    assert status == "HTTP/1.1 200 OK"
    # Exactly these: no icy-metaint, as metadata wasn't asked for, and no Transfer-Encoding or Content-Length,
    # so the body runs until the connection closes.
    assert headers == {
        "content-type": "audio/mpeg",
        "connection": "close",
        "cache-control": "no-cache, no-store",
        "pragma": "no-cache",
        "access-control-allow-origin": "*",
        "access-control-expose-headers": "icy-br, icy-metaint, icy-name, icy-pub",
        "icy-name": "Turntable",
        "icy-br": "320",
        "icy-pub": "0",
    }

    await wait_for(lambda: stream.clients == 1)
    for _ in range(50):
        await stream.on_audio(SILENCE)
    async with asyncio.timeout(5):
        body = await reader.readexactly(30 * FRAME_BYTES)
    # Unbroken MP3: a frame header at every frame boundary, with nothing between the frames.
    assert frames_at_every_frame_boundary(body)
    writer.close()


async def test_only_the_stream_path_is_served(stream, port):
    for target in ("/turntable.mp3", "/turntable.mp3?nocache=1"):
        reader, writer = await request(port, f"GET {target} HTTP/1.1", "Host: pi")
        status, _ = await response_head(reader)
        assert status == "HTTP/1.1 200 OK", target
        writer.close()

    for line, refusal in (
        ("GET / HTTP/1.1", "HTTP/1.1 404 Not Found"),
        ("GET /other.mp3 HTTP/1.1", "HTTP/1.1 404 Not Found"),
        ("POST /turntable.mp3 HTTP/1.1", "HTTP/1.1 405 Method Not Allowed"),
    ):
        reader, writer = await request(port, line, "Host: pi", "Content-Length: 0")
        status, headers = await response_head(reader)
        assert status == refusal, line
        assert await closed(reader), line
        writer.close()
    assert headers["allow"] == "GET, HEAD"
    await wait_for(lambda: stream.clients == 0)


async def test_head_returns_headers_then_closes(stream, port):
    reader, writer = await request(port, "HEAD /turntable.mp3 HTTP/1.1", "Host: pi")
    status, headers = await response_head(reader)
    assert status == "HTTP/1.1 200 OK"
    assert headers["content-type"] == "audio/mpeg"
    assert headers["icy-name"] == "Turntable"
    assert await closed(reader)
    assert stream.clients == 0


@patch("needledrop.outputs.mp3_stream.HEADER_TIMEOUT", 0.2)
async def test_a_request_without_a_blank_line_is_dropped(stream, port):
    for sent in (
        b"GET /turntable.mp3 HTTP/1.1\r\nHost: pi\r\n",  # never finishes
        b"GET /turntable.mp3 HTTP/1.1\r\n" + b"X-Filler: " + b"a" * 9000 + b"\r\n\r\n",  # one oversized line
        b"GET /turntable.mp3 HTTP/1.1\r\n" + b"X-Filler: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\r\n" * 200 + b"\r\n",
        b"garbage\r\n\r\n",  # no method, path and version
    ):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(sent)
        assert await closed(reader), sent[:40]
        writer.close()
    assert stream.clients == 0


async def test_encoding_runs_only_while_someone_listens(stream, port):
    created, encoded = [], []

    class CountingEncoder:
        """lameenc's encoder, counting what it's asked to do."""

        def __init__(self):
            self._encoder = lameenc.Encoder()
            created.append(self)

        def __getattr__(self, name):
            return getattr(self._encoder, name)

        def encode(self, pcm):
            encoded.append(len(pcm))
            return self._encoder.encode(pcm)

    with patch("needledrop.outputs.mp3_stream.Encoder", CountingEncoder):
        for _ in range(3):
            await stream.on_audio(SILENCE)
        assert (stream.clients, created, encoded) == (0, [], [])

        reader, writer = await request(port, "GET /turntable.mp3 HTTP/1.1", "Host: pi")
        await response_head(reader)
        await wait_for(lambda: stream.clients == 1)
        for _ in range(3):
            await stream.on_audio(SILENCE)
        assert (len(created), len(encoded)) == (1, 3)

        writer.close()
        await wait_for(lambda: stream.clients == 0)
        for _ in range(3):
            await stream.on_audio(SILENCE)
        assert (len(created), len(encoded)) == (1, 3)

        # The next listener gets a fresh encoder, so its stream starts on a frame.
        reader, writer = await request(port, "GET /turntable.mp3 HTTP/1.1", "Host: pi")
        await response_head(reader)
        await wait_for(lambda: stream.clients == 1)
        for _ in range(50):
            await stream.on_audio(SILENCE)
        assert len(created) == 2
        async with asyncio.timeout(5):
            assert frames_at_every_frame_boundary(await reader.readexactly(30 * FRAME_BYTES))
        writer.close()


async def test_stop_disconnects_clients(port):
    output = Mp3StreamOutput(Mp3StreamConfig(enabled=True, port=port), sample_rate=48000)
    await output.start()
    reader, writer = await request(port, "GET /turntable.mp3 HTTP/1.1", "Host: pi")
    await response_head(reader)
    await wait_for(lambda: output.clients == 1)

    await output.stop()
    assert await closed(reader)
    await wait_for(lambda: output.clients == 0)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    writer.close()


# --- ICY metadata ---


async def feed(stream, chunks: int) -> None:
    for _ in range(chunks):
        await stream.on_audio(SILENCE)


async def read_block(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Reads 16000 bytes of audio and the metadata block after them: the audio, and the block without its length."""
    async with asyncio.timeout(5):
        audio = await reader.readexactly(16000)
        length = (await reader.readexactly(1))[0]
        return audio, await reader.readexactly(length * 16)


async def next_title(stream, reader: asyncio.StreamReader) -> bytes:
    """Feeds the stream until the client gets a metadata block that isn't empty; that block, its NULs stripped."""
    async with asyncio.timeout(5):
        while True:
            await feed(stream, 25)  # about 19 KB of MP3, more than the 16000 bytes read
            _, block = await read_block(reader)
            if block:
                return block.rstrip(b"\0")


async def listen(port: int, user_agent: str, icy_header: str = "Icy-MetaData: 1"):
    reader, writer = await request(port, "GET /turntable.mp3 HTTP/1.1", "Host: pi", icy_header, f"User-Agent: {user_agent}")
    _, headers = await response_head(reader)
    return reader, writer, headers


def test_stream_title_formats():
    track = TrackInfo(title="Song", artist="Artist", album="Album")
    assert stream_title(track, "Turntable", music_assistant=False) == "Artist - Song"
    assert stream_title(track, "Turntable", music_assistant=True) == '"Song" by Artist from "Album"'
    for no_album in (None, ""):
        untitled = TrackInfo(title="Song", artist="Artist", album=no_album)
        assert stream_title(untitled, "Turntable", music_assistant=True) == '"Song" by Artist'
    assert stream_title(None, "Turntable", music_assistant=False) == "Turntable"
    assert stream_title(None, "Turntable", music_assistant=True) == "Turntable"

    quotes = TrackInfo(title='Don\'t Say "Yes"', artist="Guns N' Roses", album='Live "\'88"')
    assert stream_title(quotes, "Turntable", music_assistant=False) == 'Guns N’ Roses - Don’t Say "Yes"'
    assert stream_title(quotes, "Turntable", music_assistant=True) == (
        '"Don’t Say ”Yes”" by Guns N’ Roses from "Live ”’88”"'
    )
    assert stream_title(None, "Andy's Turntable", music_assistant=False) == "Andy’s Turntable"


def test_icy_metadata_block_format_and_limits():
    # 30 bytes of payload, padded to 32: two 16-byte units.
    assert icy_metadata_block("T", "U") == b"\x02StreamTitle='T';StreamUrl='U';\0\0"
    assert icy_metadata_block("T", None) == b"\x02StreamTitle='T';StreamUrl='';\0\0\0"
    # 32 bytes of payload fill two units exactly, so a third holds the terminating NUL.
    assert icy_metadata_block("T", "UUU") == b"\x03StreamTitle='T';StreamUrl='UUU';" + bytes(16)
    # A ' in the URL would end it early; percent-encoded, it still names the same file.
    assert icy_metadata_block("T", "https://x/a'b.jpg") == (
        b"\x04StreamTitle='T';StreamUrl='https://x/a%27b.jpg';" + bytes(16)
    )

    # Too long for the length byte: the URL goes first, then the end of the title, on a character boundary.
    title = "é" * 2500  # 5000 bytes in UTF-8
    block = icy_metadata_block(title, "https://example.com/art.jpg")
    assert len(block) == 1 + 255 * 16 and block[0] == 255
    payload = block[1:].rstrip(b"\0")
    assert payload.startswith(b"StreamTitle='\xc3\xa9") and payload.endswith(b"\xc3\xa9';StreamUrl='';")
    kept = payload.removeprefix(b"StreamTitle='").removesuffix(b"';StreamUrl='';").decode()
    assert kept == "é" * 2025  # 4050 bytes: all that fits with the rest and a NUL in 4080
    # A short title with a long URL loses only the URL.
    assert icy_metadata_block("Song", "https://x/" + "a" * 4100 + ".jpg") == (
        b"\x03StreamTitle='Song';StreamUrl='';" + bytes(16)
    )


async def test_metadata_arrives_every_16000_bytes(stream, port, sample_track):
    reader, writer, headers = await listen(port, VLC)
    assert headers["icy-metaint"] == "16000"
    await wait_for(lambda: stream.clients == 1)

    await feed(stream, 50)  # about 39 KB: two blocks' worth
    audio, block = await read_block(reader)
    assert frames_at_every_frame_boundary(audio)
    assert block == b"StreamTitle='Turntable';StreamUrl='';" + bytes(11)
    _, block = await read_block(reader)
    assert block == b""  # no change: the length byte alone, 0

    await stream.on_track_recognized(sample_track)
    await feed(stream, 50)
    _, block = await read_block(reader)
    assert block == b"StreamTitle='Artist - Song';StreamUrl='https://example.com/art.jpg';" + bytes(12)
    _, block = await read_block(reader)
    assert block == b""
    writer.close()


async def test_music_assistant_gets_the_quoted_title(stream, port, sample_track):
    await stream.on_track_recognized(sample_track)
    listeners = {
        agent: await listen(port, agent, icy_header)
        for agent, icy_header in ((MA_2_10, "Icy-MetaData: 1"), (MA_DEV, "icy-metadata:1"), (VLC, "ICY-METADATA:  1 "))
    }
    await wait_for(lambda: stream.clients == 3)
    titles = {}
    for agent, (reader, writer, headers) in listeners.items():
        assert headers["icy-metaint"] == "16000", agent
        titles[agent] = await next_title(stream, reader)
        writer.close()
    art = b"StreamUrl='https://example.com/art.jpg';"
    assert titles == {
        MA_2_10: b"StreamTitle='\"Song\" by Artist from \"Album\"';" + art,
        MA_DEV: b"StreamTitle='\"Song\" by Artist from \"Album\"';" + art,
        VLC: b"StreamTitle='Artist - Song';" + art,
    }


async def test_playback_start_and_stop_reset_the_title(stream, port, sample_track):
    reader, writer, _ = await listen(port, VLC)
    await wait_for(lambda: stream.clients == 1)
    assert await next_title(stream, reader) == b"StreamTitle='Turntable';StreamUrl='';"
    for reset in (stream.on_playback_stopped, stream.on_playback_started):
        await stream.on_track_recognized(sample_track)
        assert await next_title(stream, reader) == (
            b"StreamTitle='Artist - Song';StreamUrl='https://example.com/art.jpg';"
        )
        await reset()
        assert await next_title(stream, reader) == b"StreamTitle='Turntable';StreamUrl='';", reset.__name__
    writer.close()


@patch("needledrop.outputs.mp3_stream.CLIENT_QUEUE_LIMIT", 64 * 1024)
async def test_a_slow_client_is_disconnected(stream, port, caplog):
    # This client stops reading after its request. Once the kernel's buffers between it and the server are full,
    # what the server writes queues up in its transport.
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    sock.setblocking(False)
    slow_reader, slow_writer = await asyncio.open_connection(sock=sock)
    slow_writer.write(b"GET /turntable.mp3 HTTP/1.1\r\nHost: pi\r\n\r\n")
    reader, writer, _ = await listen(port, VLC)
    await wait_for(lambda: stream.clients == 2)

    received = 0

    async def keep_reading():
        nonlocal received
        while data := await reader.read(65536):
            received += len(data)

    reading = asyncio.create_task(keep_reading())
    one_second = AudioChunk(pcm=bytes(48000 * 4), captured_at=0.0)
    with caplog.at_level(logging.INFO, logger="needledrop.outputs.mp3_stream"):
        async with asyncio.timeout(30):
            while stream.clients == 2:
                await stream.on_audio(one_second)
                await asyncio.sleep(0)
            # The other listener streams on.
            before = received
            await stream.on_audio(one_second)
            await wait_for(lambda: received > before + 30000)
    assert stream.clients == 1
    assert len([r for r in caplog.records if "behind" in r.getMessage()]) == 1

    # The slow client gets what the kernel had buffered for it, then the end.
    async with asyncio.timeout(10):
        try:
            while await slow_reader.read(65536):
                pass
        except ConnectionResetError:
            pass
    reading.cancel()
    writer.close()
    slow_writer.close()
