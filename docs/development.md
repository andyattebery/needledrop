# Development Guide

## Architecture

needledrop runs a single async loop:

```
capture audio → check silence → recognize via Shazam → de-duplicate → dispatch to outputs
```

- **Capture**: `arecord` subprocess writes a WAV sample to tmpfs
- **Silence detection**: `pydub` checks if the sample's dBFS is below the configured threshold. If silent and was previously playing, dispatches `on_playback_stopped` to all outputs. No recognition attempt is made on silent samples.
- **Recognition**: `shazamio` identifies the track from the WAV file
- **De-duplication**: if the same (title, artist) was recognized within `min_track_change_seconds`, the result is dropped
- **Dispatch**: `on_track_recognized` is called on every enabled output handler, each wrapped in `safe_call` so one failure doesn't block the others

A single `aiohttp.ClientSession` is created in `run()` and shared across all output handlers that need HTTP access.

## Module map

```
needledrop/
├── __main__.py              CLI entrypoint (argparse, logging setup)
├── app.py                   Main loop (run, tick, build_outputs, LoopState)
├── config.py                YAML config loading, typed dataclasses
├── models.py                TrackInfo dataclass (eq/hash on title+artist)
├── audio/
│   └── capture.py           arecord subprocess, silence detection (pydub)
├── recognition/
│   ├── base.py              Recognizer protocol
│   └── shazamio_recognizer.py  Shazam implementation
└── outputs/
    ├── base.py              OutputHandler protocol
    ├── lastfm.py            Last.fm scrobble/now-playing (pylast via to_thread)
    ├── log.py               Structured logging
    ├── owntone_metadata.py  FIFO metadata pipe (base64 XML items)
    └── tuneshine.py         HTTP POST with 64x64 WebP artwork
```

## Key design decisions

- **Silence-based play detection** — needledrop detects whether audio is playing by measuring dBFS of captured samples, rather than querying OwnTone's API. This keeps it independent of the playback server.
- **De-duplication on (title, artist)** — `TrackInfo.__eq__` and `__hash__` use only title and artist. Album and artwork differences don't trigger a new dispatch.
- **safe_call wrapping** — each output handler call is wrapped in try/except. One output failing (e.g. network timeout to Tuneshine) doesn't prevent other outputs (e.g. Last.fm) from running.
- **tmpfs for WAV** — samples are written to `/run/needledrop/sample.wav` (tmpfs) to avoid SD card wear on the Raspberry Pi.
- **Protocol-based outputs** — output handlers implement `OutputHandler` via structural subtyping (Python `Protocol`), not inheritance. Any class with `on_track_recognized` and `on_playback_stopped` async methods works.

## Running tests

Requires Python 3.12+:

```bash
pip install ".[test]"
pytest tests/ -v
```

All external dependencies (Shazam, Last.fm, ALSA, HTTP, FIFO) are mocked — no hardware or network access needed.

## CI

The GitHub Actions workflow (`.github/workflows/ci.yaml`) has two jobs:

- **test** — runs on every push and PR to `main`. Sets up Python 3.12, installs test deps, runs pytest.
- **docker** — runs on semver tag pushes (e.g. `0.1.0`, `v0.1.0`), after tests pass. Builds multi-arch images (`linux/amd64`, `linux/arm64`), pushes to `ghcr.io/andyattebery/needledrop:<version>`, and promotes `:latest` for non-prerelease tags.

## Adding a new output handler

1. Create `needledrop/outputs/your_output.py` with a class implementing two async methods:

```python
class YourOutput:
    async def on_track_recognized(self, track: TrackInfo) -> None:
        ...

    async def on_playback_stopped(self) -> None:
        ...
```

2. Add a config dataclass in `needledrop/config.py`:

```python
@dataclass
class YourOutputConfig:
    enabled: bool = False
    # your fields here
```

Add it to `OutputsConfig`.

3. Register it in `build_outputs()` in `needledrop/app.py`:

```python
if cfg.your_output.enabled:
    outputs.append(YourOutput(...))
```

4. Add the new section to `config.example.yaml`.
