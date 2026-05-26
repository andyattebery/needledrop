# needledrop — Python App Spec

Modular audio fingerprinting service for identifying vinyl records playing on a turntable. Runs on a Raspberry Pi 4 inside Docker. Captures audio from an ALSA device, identifies tracks via Shazam, and dispatches results to pluggable output handlers (Last.fm scrobbler, OwnTone metadata, remote metadata fallback, logging).

## Dependencies

```
shazamio       # async Shazam client (uses reverse-engineered API, no API key needed)
pylast         # Last.fm API (scrobbling, now-playing)
pyyaml         # config file parsing
aiohttp        # async HTTP client (OwnTone API, remote metadata)
```

Pre-built ARM64 wheels exist for all deps including `shazamio-core` (Rust/pyo3). No compilation toolchain needed.

Runtime system packages: `alsa-utils` (provides `arecord`), `ffmpeg` (pydub audio decoding for ShazamIO).

## Project structure

```
needledrop/
  pyproject.toml
  Dockerfile
  needledrop/
    __init__.py
    __main__.py
    config.py
    app.py
    models.py
    audio/
      __init__.py
      capture.py
    recognition/
      __init__.py
      base.py
      shazamio_recognizer.py
    outputs/
      __init__.py
      base.py
      lastfm.py
      owntone_metadata.py
      remote_metadata.py
      log.py
```

## Dockerfile

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    alsa-utils ffmpeg && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN pip install --no-cache-dir /src && rm -rf /src
ENTRYPOINT ["python", "-m", "needledrop"]
CMD ["--config", "/etc/needledrop/config.yaml"]
```

The container runs with:
- `network_mode: host` (access OwnTone API at localhost:3689)
- `ipc: host` (share ALSA dsnoop shared memory with host)
- `/dev/snd` device passthrough + `audio` group
- `/etc/asound.conf` bind-mounted from host (dsnoop config)
- `/media` bind-mounted from host (OwnTone media dir, contains `.metadata` FIFO)
- `/run/needledrop` as tmpfs (temp WAV files, avoids SD card writes)

## Config file

Path: `/etc/needledrop/config.yaml`

```yaml
audio:
  alsa_device: dsnoopusb        # ALSA device name (dsnoop virtual device)
  sample_rate: 48000            # must match dsnoop slave config
  sample_duration_seconds: 12   # length of audio sample for fingerprinting

recognition:
  interval_seconds: 20          # seconds between recognition attempts
  min_track_change_seconds: 30  # ignore duplicate detections within this window

outputs:
  lastfm:
    enabled: true
    api_key: "..."
    api_secret: "..."
    username: "..."
    password_hash: "..."              # MD5 of Last.fm password (use pylast.md5())
    scrobble_threshold_seconds: 30    # scrobble after track plays this long
  owntone_metadata:
    enabled: true
    metadata_fifo: /media/Turntable.metadata      # container path to OwnTone .metadata FIFO
    owntone_api_url: http://localhost:3689/api     # OwnTone JSON API
  remote_metadata:
    enabled: true
    host: "192.168.1.120"     # Tuneshine device (fallback when fingerprinting fails)
  log:
    enabled: true
```

## Module specs

### `models.py`

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class TrackInfo:
    title: str
    artist: str
    album: str | None = None
    artwork_url: str | None = None
    duration_seconds: int | None = None
    recognized_at: datetime = field(default_factory=datetime.now)
```

Equality for de-duplication is based on `(title, artist)` — not album or artwork.

### `config.py`

Load YAML config from path. Validate required fields. Return a typed config object (dataclass or similar). No environment variable overrides — all config comes from the YAML file.

Entry: `load_config(path: Path) -> Config`

### `__main__.py`

```python
import argparse, asyncio
from needledrop.config import load_config
from needledrop.app import run

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
args = parser.parse_args()
config = load_config(Path(args.config))
asyncio.run(run(config))
```

---

### `audio/capture.py`

Records a short audio sample from the ALSA device using `arecord` as a subprocess.

Entry: `async capture_sample(config: AudioConfig) -> Path`

Behavior:
1. Run: `arecord -D {alsa_device} -d {sample_duration_seconds} -f S16_LE -r {sample_rate} -c 2 -t wav /run/needledrop/sample.wav`
2. Wait for subprocess to complete
3. Return the path to the WAV file
4. If `arecord` fails (device busy, not found), raise a specific exception — the main loop handles retry

The WAV file is written to `/run/needledrop/` (tmpfs) to avoid SD card wear. The file is overwritten each cycle.

---

### `recognition/base.py`

```python
from typing import Protocol
from pathlib import Path
from needledrop.models import TrackInfo

class Recognizer(Protocol):
    async def recognize(self, wav_path: Path) -> TrackInfo | None:
        """Return TrackInfo if a song was identified, None otherwise."""
        ...
```

### `recognition/shazamio_recognizer.py`

Wraps `shazamio.Shazam`.

Entry: `ShazamIORecognizer` implementing `Recognizer`

Behavior:
1. `shazam = Shazam()` (no API key, no config)
2. `result = await shazam.recognize(str(wav_path))`
3. Parse the response: extract `track.title`, `track.subtitle` (artist), `track.sections[0].metadata` for album, `track.images.coverart` for artwork URL
4. If the response has no matches, return `None`
5. ShazamIO handles user-agent rotation and exponential backoff on 429/5xx automatically

---

### `outputs/base.py`

```python
from typing import Protocol
from needledrop.models import TrackInfo

class OutputHandler(Protocol):
    async def on_track_recognized(self, track: TrackInfo) -> None:
        """Called when a new (different) track is detected."""
        ...

    async def on_playback_stopped(self) -> None:
        """Called when OwnTone reports the pipe is no longer playing."""
        ...
```

### `outputs/lastfm.py`

Wraps `pylast`.

**Constructor**: takes `api_key`, `api_secret`, `username`, `password_hash`, `scrobble_threshold_seconds`. Creates `pylast.LastFMNetwork(...)`.

**`on_track_recognized(track)`**:
1. Call `network.update_now_playing(artist=track.artist, title=track.title, album=track.album)`
2. Record `track.recognized_at` and track identity for scrobble timing

**Scrobble timing**: the scrobble happens when a DIFFERENT track is recognized or playback stops. At that point, check if the previous track played longer than `scrobble_threshold_seconds`. If yes, call `network.scrobble(artist=prev.artist, title=prev.title, album=prev.album, timestamp=prev.recognized_at)`.

**`on_playback_stopped()`**:
1. If a track was playing and threshold was met, scrobble it
2. Clear state

**Error handling**: `pylast` calls are synchronous — run in `asyncio.to_thread()`. Catch `pylast.WSError` and log, don't crash.

### `outputs/owntone_metadata.py`

Writes track metadata to OwnTone's pipe metadata FIFO. This is a port of the existing shell script at `ansible/roles/owntone_alsa_pipe/templates/owntone-alsa-pipe-metadata.sh.j2`.

**Constructor**: takes `metadata_fifo` (path to FIFO), `owntone_api_url`.

**OwnTone pipe metadata protocol**:

The FIFO accepts XML-like items. Each item has a type, code, length, and base64-encoded data:

```
<item><type>{type_hex}</type><code>{code_hex}</code><length>{byte_length}</length><data encoding="base64">{base64_data}</data></item>
```

Type and code constants (hex strings):
- Core metadata type: `636f7265`
  - Track title code: `6d696e6d`
  - Artist code: `61736172`
  - Album code: `6173616c`
- Artwork sync type: `73736e63`
  - Picture code: `50494354`

For core metadata, `length` is the byte length of the original string (not the base64). `data` is the string value base64-encoded.

For artwork, `length` is the byte length of the image binary. `data` is the image binary base64-encoded. The image is downloaded from `track.artwork_url`.

**`on_track_recognized(track)`**:
1. Write title item to FIFO (if title present)
2. Write artist item to FIFO (if artist present)
3. Write album item to FIFO (if album present)
4. If `artwork_url` is present, download the image with `aiohttp`, write artwork item to FIFO

Writing to FIFO: open in write mode, write the item string, close. Use a 2-second timeout on each write (the FIFO blocks if OwnTone isn't reading). On timeout, log and continue — don't crash.

**`on_playback_stopped()`**: no-op (OwnTone clears metadata when pipe stops).

### `outputs/remote_metadata.py`

Fallback: polls an external HTTP device for track metadata when audio fingerprinting fails.

**Constructor**: takes `host` (IP/hostname of the Tuneshine device).

**This output is special** — it's not dispatched on `on_track_recognized`. Instead, it's called by the main loop when recognition returns `None`. It produces a `TrackInfo` that gets dispatched to the other outputs.

Entry: `async fetch_metadata(self) -> TrackInfo | None`

Behavior:
1. GET `http://{host}/state`
2. Parse JSON response. Expected structure:
   ```json
   {
     "remoteMetadata": {
       "idle": false,
       "trackName": "...",
       "artistName": "...",
       "albumName": "...",
       "imageUrl": "..."
     }
   }
   ```
3. If `idle` is `true` or fields are empty, return `None`
4. Return `TrackInfo(title=trackName, artist=artistName, album=albumName, artwork_url=imageUrl)`

**Error handling**: timeout after 5 seconds, return `None` on any HTTP or parse error.

**`on_track_recognized(track)`**: no-op (this output doesn't consume track events, it produces them).

**`on_playback_stopped()`**: no-op.

Note: because this module has a different role (source vs sink), it should NOT implement `OutputHandler`. Keep it as a separate interface used directly by the main loop.

### `outputs/log.py`

Logs recognized tracks to stdout (which goes to Docker logs / journald).

**`on_track_recognized(track)`**: log `Recognized: {artist} - {title} [{album}]` at INFO level.

**`on_playback_stopped()`**: log `Playback stopped` at INFO level.

---

### `app.py` — Main loop

Entry: `async run(config: Config) -> None`

```python
async def run(config: Config):
    recognizer = ShazamIORecognizer()
    outputs = build_outputs(config)  # list of enabled OutputHandler instances
    remote = RemoteMetadata(config.outputs.remote_metadata.host) if config.outputs.remote_metadata.enabled else None

    last_track: TrackInfo | None = None
    was_playing = False

    while True:
        await asyncio.sleep(config.recognition.interval_seconds)

        # 1. Check if OwnTone is playing from the pipe
        playing = await check_owntone_pipe_playing(config.outputs.owntone_metadata.owntone_api_url)

        if not playing:
            if was_playing:
                for output in outputs:
                    await safe_call(output.on_playback_stopped)
                last_track = None
            was_playing = False
            continue

        was_playing = True

        # 2. Capture audio sample
        try:
            wav_path = await capture_sample(config.audio)
        except CaptureError:
            logger.warning("Audio capture failed")
            continue

        # 3. Recognize
        track = await recognizer.recognize(wav_path)

        # 4. Fallback to remote metadata if recognition failed
        if track is None and remote is not None:
            track = await remote.fetch_metadata()

        if track is None:
            continue

        # 5. De-duplicate
        if last_track and track.title == last_track.title and track.artist == last_track.artist:
            if (track.recognized_at - last_track.recognized_at).total_seconds() < config.recognition.min_track_change_seconds:
                continue

        # 6. Dispatch to outputs
        for output in outputs:
            await safe_call(output.on_track_recognized, track)

        last_track = track
```

**`check_owntone_pipe_playing(api_url)`**: async function that:
1. GET `{api_url}/player` — parse JSON, check `state == "play"` and `item_id != 0`
2. GET `{api_url}/queue` — find the item with matching `id`, check `data_kind == "pipe"`
3. Return `True` if both conditions met, `False` otherwise
4. On HTTP error, return `False`

This matches the logic in the existing shell script's `pipe_is_playing()` function.

**`safe_call(fn, *args)`**: wraps output handler calls in try/except. Log exceptions, don't crash. One failing output doesn't block others.

**`build_outputs(config)`**: returns a list of enabled `OutputHandler` instances based on config. Always includes `LogOutput` when `log.enabled` is true. Includes `LastFMOutput` when `lastfm.enabled` is true. Includes `OwnToneMetadataOutput` when `owntone_metadata.enabled` is true. Does NOT include `RemoteMetadata` — it's handled separately in the main loop since it's a source, not a sink.

## Testing

### Local development (any machine)

- Record a 12-second WAV of music from any source
- Pass it to `ShazamIORecognizer.recognize()` — verify correct identification
- Unit test config parsing, de-duplication logic, metadata protocol encoding
- Mock the FIFO and OwnTone API for output handler tests

### On-device verification

- `docker compose logs -f needledrop` — watch recognition logs
- Play a record → recognition should appear within ~30 seconds
- Check OwnTone web UI for metadata (title/artist/album/artwork)
- Check Last.fm profile for now-playing and scrobble history
- `docker stats needledrop` — CPU should spike briefly during recognition, idle otherwise
- Stop needledrop container → OwnTone audio continues unaffected (dsnoop independence)
- Kill network → recognition fails gracefully, remote metadata fallback attempted, no crash
