import asyncio
import json
import logging
import os
import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from aiohttp import web
from aiosendspin.client import ClientListener, PairingSupport, SendspinClient, SourceCapture
from aiosendspin.models.core import DeviceInfo, ServerCommandPayload
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.types import AudioCodec, Roles, SignalState
from aiosendspin.noise import FileClientPairingStore, Identity, b64url_decode, b64url_encode

from needledrop.audio.capture import CHANNELS, SAMPLE_WIDTH
from needledrop.config import SendspinConfig
from needledrop.models import AudioChunk, TrackInfo

logger = logging.getLogger(__name__)

IDENTITY_FILE = "sendspin-identity.json"
PAIRING_FILE = "sendspin-pairing.json"
SIGNAL_SYNC_SECONDS = 1.0


def load_identity(path: Path) -> Identity:
    """Load this device's Sendspin key, creating it, readable by its owner only, on first use.

    The key's public half is the client ID that the server knows the device by, so it must survive
    restarts: a new key is a new device to pair or approve.
    """
    try:
        data = json.loads(path.read_text())
        return Identity.from_private_bytes(b64url_decode(data["private_key"]))
    except FileNotFoundError:
        pass
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("Replacing unreadable Sendspin identity %s: %s", path, e)
        path.unlink()
    identity = Identity.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"private_key": b64url_encode(identity.private_bytes)}, f)
    return identity


def _admitted_connection(client: SendspinClient):
    """The connection the client is using, or None.

    aiosendspin reports a source's signal only through the connection, which it keeps private. This is
    the one place that reaches for it; pyproject.toml bounds aiosendspin to 10.x for it.
    """
    return client._admitted_connection  # noqa: SLF001


class SendspinOutput:
    """A Sendspin source, through which a server such as Music Assistant plays the turntable.

    It advertises itself over mDNS and waits for a server to connect. The signal it reports follows
    needledrop's playback events, and audio flows only while the server has asked for it. The source
    role has no place for track metadata, so recognized tracks are not sent.
    """

    def __init__(
        self,
        config: SendspinConfig,
        sample_rate: int,
        *,
        advertise_mdns: bool = True,
    ) -> None:
        self._config = config
        self._format = SupportedAudioFormat(
            codec=AudioCodec.PCM,
            channels=CHANNELS,
            sample_rate=sample_rate,
            bit_depth=SAMPLE_WIDTH * 8,
        )
        self._advertise_mdns = advertise_mdns
        self._signal = SignalState.ABSENT
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._capture: SourceCapture | None = None
        # The connection and signal last reported, so each connection hears each change once.
        self._reported: tuple[object, SignalState] | None = None
        self._tasks: set[asyncio.Task] = set()
        self.client_id: str | None = None

    async def start(self) -> None:
        state_dir = Path(self._config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        identity = await asyncio.to_thread(load_identity, state_dir / IDENTITY_FILE)
        store = await FileClientPairingStore.open(state_dir / PAIRING_FILE)
        pairing = await store.get_pairing_config()
        if pairing.unpaired_access_enabled != self._config.unpaired_access:
            await store.store_pairing_config(
                replace(pairing, unpaired_access_enabled=self._config.unpaired_access)
            )

        client = SendspinClient(
            identity,
            self._config.name,
            [Roles.SOURCE],
            pairing_store=store,
            device_info=DeviceInfo(product_name="needledrop", software_version=version("needledrop")),
            source_support=ClientHelloSourceSupport(
                features=ClientHelloSourceFeatures(line_sense=True)
            ),
            pairing_support=PairingSupport(pairing_code_display=self._show_pairing_code),
        )
        client.add_server_command_listener(self._on_server_command)
        client.add_disconnect_listener(self._on_disconnect)
        # Ready before the listener starts: a server that knows this device can connect while the
        # mDNS registration inside start() is still under way.
        self._client, self.client_id = client, identity.peer_id
        listener = ClientListener(
            client_id=identity.peer_id,
            on_connection=self._on_connection,
            port=self._config.port,
            client_name=self._config.name,
            advertise_mdns=self._advertise_mdns,
        )
        await listener.start()

        self._listener = listener
        self._spawn(self._sync_signal_forever())
        logger.info(
            "Sendspin source %r is waiting for a server on port %d (client ID %s)",
            self._config.name,
            self._config.port,
            identity.peer_id,
        )

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._capture = None
        if self._client is not None:
            await self._client.disconnect()
        if self._listener is not None:
            await self._listener.stop()

    async def on_track_recognized(self, track: TrackInfo) -> None:
        pass

    async def on_playback_started(self) -> None:
        self._signal = SignalState.PRESENT
        await self._sync_signal()

    async def on_playback_stopped(self) -> None:
        self._signal = SignalState.ABSENT
        await self._sync_signal()

    async def on_audio(self, chunk: AudioChunk) -> None:
        capture, client = self._capture, self._client
        if capture is None or client is None:
            return
        age_us = round((time.monotonic() - chunk.captured_at) * 1_000_000)
        try:
            await capture.feed(chunk.pcm, client.now_us() - age_us)
        except RuntimeError:
            # The server stopped the stream, removed the source role, or went away.
            if self._capture is capture:
                self._capture = None

    async def _on_connection(self, ws: web.WebSocketResponse) -> None:
        try:
            await self._client.attach_websocket(ws)
        except Exception as e:
            logger.warning("Sendspin connection failed: %s", e)

    def _on_server_command(self, payload: ServerCommandPayload) -> None:
        if payload.source is None:
            return
        if payload.source.command == "start":
            self._spawn(self._begin_stream())
        else:
            # The SDK has already ended the stream.
            self._capture = None
            logger.info("Sendspin server stopped the stream")

    def _on_disconnect(self) -> None:
        self._capture = None
        self._reported = None

    async def _begin_stream(self) -> None:
        client = self._client
        try:
            capture = client.create_source_capture(self._format)
            await capture.start()
        except RuntimeError as e:
            logger.warning("Couldn't start the Sendspin stream: %s", e)
            return
        self._capture = capture
        server = client.server_info.name if client.server_info else "the server"
        logger.info("Streaming the turntable to %s", server)

    async def _sync_signal(self) -> None:
        client = self._client
        if client is None:
            return
        connection = _admitted_connection(client)
        if connection is None or not connection.connected:
            return
        signal = self._signal
        if self._reported == (connection, signal):
            return
        try:
            await connection.send_source_signal(signal)
        except RuntimeError:
            # The source role isn't active yet, as while the server awaits approval. Retried.
            return
        if self._reported is None or self._reported[0] is not connection:
            server = client.server_info.name if client.server_info else "a server"
            logger.info("Connected to Sendspin server %s", server)
        self._reported = (connection, signal)

    async def _sync_signal_forever(self) -> None:
        while True:
            await self._sync_signal()
            await asyncio.sleep(SIGNAL_SYNC_SECONDS)

    async def _show_pairing_code(self, pairing_code: str | None, *, grouped: str | None) -> None:
        if grouped is not None:
            logger.info("Sendspin pairing: enter code %s on the server", grouped)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
