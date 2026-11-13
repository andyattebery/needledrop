import asyncio
import json
import logging
import stat
import struct
import time
from unittest.mock import patch

import pytest
from aiosendspin.noise import Identity
from aiosendspin.noise.trust_store import InMemoryServerPairingStore
from aiosendspin.server import (
    SignalState,
    SourceSignalChangedEvent,
    SourceStreamEndedEvent,
    SourceStreamStartedEvent,
)
from aiosendspin.server.server import SendspinServer

from helpers import free_port, wait_for
from needledrop.config import SendspinConfig
from needledrop.models import AudioChunk
from needledrop.outputs.sendspin import IDENTITY_FILE, SendspinOutput, load_identity


def test_load_identity_creates_an_owner_only_key_and_reuses_it(tmp_path):
    path = tmp_path / IDENTITY_FILE
    first = load_identity(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_identity(path).peer_id == first.peer_id


def test_load_identity_replaces_an_unreadable_key(tmp_path, caplog):
    path = tmp_path / IDENTITY_FILE
    path.write_text("not json")
    with caplog.at_level(logging.WARNING):
        identity = load_identity(path)
    assert "Replacing unreadable Sendspin identity" in caplog.text
    assert json.loads(path.read_text())["private_key"]
    assert load_identity(path).peer_id == identity.peer_id


async def test_events_and_audio_before_start_are_ignored(tmp_path):
    output = SendspinOutput(SendspinConfig(state_dir=str(tmp_path)), sample_rate=48000)
    await output.on_playback_started()
    await output.on_playback_stopped()
    await output.on_track_recognized(None)
    await output.on_audio(AudioChunk(pcm=bytes(3840), captured_at=time.monotonic()))


@pytest.fixture
async def server():
    server = SendspinServer(
        loop=asyncio.get_running_loop(),
        identity=Identity.generate(),
        server_name="test-server",
        pairing_store=InMemoryServerPairingStore(),
    )
    yield server
    await server.close()


async def test_streams_the_turntable_to_a_sendspin_server(tmp_path, server):
    port = free_port()
    output = SendspinOutput(
        SendspinConfig(enabled=True, name="Turntable", port=port, state_dir=str(tmp_path)),
        sample_rate=48000,
        advertise_mdns=False,
    )
    await output.start()
    try:
        assert (tmp_path / IDENTITY_FILE).exists()
        await server.trust_unpaired(output.client_id, approved_roles={"source"})
        server.connect_to_client(f"ws://127.0.0.1:{port}/sendspin")
        await wait_for(
            lambda: (c := server.get_client(output.client_id)) is not None
            and "source@v1" in c.active_role_ids
        )
        server_client = server.get_client(output.client_id)
        events = []
        server_client.add_event_listener(lambda _client, event: events.append(event))

        await output.on_playback_started()
        await wait_for(
            lambda: any(
                isinstance(e, SourceSignalChangedEvent) and e.signal is SignalState.PRESENT
                for e in events
            )
        )

        server_client.role("source@v1").request_start()
        await wait_for(lambda: any(isinstance(e, SourceStreamStartedEvent) for e in events))
        handle = next(e for e in events if isinstance(e, SourceStreamStartedEvent)).handle

        sent = b"".join(struct.pack("<2h", n, -n) for n in range(1, 960 * 5 + 1))
        for start in range(0, len(sent), 3840):
            await output.on_audio(AudioChunk(pcm=sent[start : start + 3840], captured_at=time.monotonic()))

        received = b""
        async with asyncio.timeout(10):
            async for pcm, _timestamp in handle:
                received += pcm
                if len(received) >= len(sent):
                    break
        assert received[: len(sent)] == sent

        server_client.role("source@v1").request_stop()
        await wait_for(lambda: any(isinstance(e, SourceStreamEndedEvent) for e in events))
        await output.on_audio(AudioChunk(pcm=sent[:3840], captured_at=time.monotonic()))

        await output.on_playback_stopped()
        await wait_for(
            lambda: [
                e.signal for e in events if isinstance(e, SourceSignalChangedEvent)
            ][-1] is SignalState.ABSENT
        )
    finally:
        await output.stop()


async def test_a_signal_change_during_a_report_is_reported_next(tmp_path):
    release = asyncio.Event()
    sent = []

    class Connection:
        connected = True

        async def send_source_signal(self, signal):
            sent.append(signal)
            if len(sent) == 1:
                await release.wait()

    class Client:
        _admitted_connection = Connection()
        server_info = None

    output = SendspinOutput(SendspinConfig(state_dir=str(tmp_path)), sample_rate=48000)
    output._client = Client()
    first = asyncio.create_task(output._sync_signal())  # reports ABSENT and waits on the server
    await asyncio.sleep(0)
    output._signal = SignalState.PRESENT  # changes before that report completes
    release.set()
    await first
    await output._sync_signal()
    assert sent == [SignalState.ABSENT, SignalState.PRESENT]


async def test_a_server_connecting_while_the_listener_starts_reaches_the_client(tmp_path, caplog):
    attached = []

    class Client:
        _admitted_connection = None
        server_info = None

        def __init__(self, *args, **kwargs):
            pass

        def add_server_command_listener(self, callback):
            pass

        def add_disconnect_listener(self, callback):
            pass

        async def attach_websocket(self, ws):
            attached.append(ws)

        async def disconnect(self):
            pass

    class EagerListener:
        """A listener a server connects to before start() has returned, as mDNS registration takes a while."""

        def __init__(self, client_id, on_connection, **kwargs):
            self._on_connection = on_connection

        async def start(self):
            await self._on_connection("ws")

        async def stop(self):
            pass

    output = SendspinOutput(SendspinConfig(state_dir=str(tmp_path)), sample_rate=48000)
    with (
        patch("needledrop.outputs.sendspin.SendspinClient", Client),
        patch("needledrop.outputs.sendspin.ClientListener", EagerListener),
        caplog.at_level(logging.WARNING),
    ):
        await output.start()
        await output.stop()
    assert attached == ["ws"]
    assert "Sendspin connection failed" not in caplog.text
