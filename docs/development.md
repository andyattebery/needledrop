# Development Guide

## Architecture

needledrop captures without a break, and everything else hangs off that one stream:

```
arecord (continuous) → 20 ms chunks ─┬─► level → playback detector → playback_started / playback_stopped ─┐
                                     ├─► recent window → Shazam while playing → de-duplicate → track_recognized ┤
                                     └─► audio outputs (owntone_audio, sendspin)                               │
                                                                     every output ◄────────────────────────────┘
```

- **Capture**: one `arecord -t raw` subprocess (S16_LE stereo, 20 ms periods, up to a 4 s ALSA buffer) read in 20 ms
  chunks. If it exits, playback counts as stopped and capture restarts after 2 s.
- **Playback detection**: each chunk's RMS level against `silence_threshold_dbfs`. Playback starts after 0.1 s of sound
  and stops after `silence_hold_seconds` of silence, matching the sox silence filter that used to feed OwnTone's pipe.
- **Recognition**: while a record plays, the latest `sample_duration_seconds` are written to `/run/needledrop/sample.wav`
  and `shazamio` identifies them. The first attempt comes once the window has filled, then one every `interval_seconds`.
  A failure, or no answer within 60 s, is logged and the next attempt goes ahead. A result that arrives after playback
  stopped is dropped.
- **De-duplication**: if the same (title, artist) was recognized within `min_track_change_seconds`, the result is dropped.
- **Dispatch**: the `Dispatcher` gives each output its own worker: a queue of events, and for outputs that take audio, a
  queue holding 2 s of audio. A slow or failing output delays or loses only its own deliveries. An audio output that
  falls behind loses its oldest chunks. Event calls are wrapped in `safe_call`. A failing audio output is logged at most
  once a minute, since it gets fifty chunks a second.

The capture task does only cheap work (level, window, enqueueing), so no output can make the capture overrun.

A single `aiohttp.ClientSession` is created in `run()` and shared across all output handlers that need HTTP access.

## Module map

```
needledrop/
├── __main__.py              CLI entrypoint (argparse, logging setup)
├── app.py                   Pipeline (run, capture_loop, recognition_loop, recognize, Dispatcher, build_outputs)
├── config.py                YAML config loading, typed dataclasses
├── models.py                TrackInfo (eq/hash on title+artist), AudioChunk
├── audio/
│   ├── capture.py           continuous arecord stream, recognition window, WAV writer
│   └── level.py             chunk level (dBFS) and the playback detector
├── recognition/
│   ├── base.py              Recognizer protocol
│   └── shazamio_recognizer.py  Shazam implementation
└── outputs/
    ├── base.py              OutputHandler, AudioOutput and ServiceOutput protocols
    ├── lastfm.py            Last.fm scrobble/now-playing (pylast via to_thread)
    ├── log.py               Structured logging
    ├── owntone_audio.py     The turntable into OwnTone's pipe while a record plays
    ├── owntone_metadata.py  FIFO metadata pipe (base64 XML items)
    ├── sendspin.py          Sendspin source (aiosendspin) for Music Assistant
    └── tuneshine.py         HTTP POST with 64x64 WebP artwork
```

## Key design decisions

- **One continuous capture** — the OwnTone pipe and the Sendspin source need the audio itself, and both need to know
  within a second when a record starts. A single stream serves the detector, recognition and the audio outputs, so
  nothing else has to share the input device.
- **Silence-based play detection** — needledrop detects whether audio is playing by measuring the level of what it
  captures, rather than querying OwnTone's API. This keeps it independent of the playback server.
- **De-duplication on (title, artist)** — `TrackInfo.__eq__` and `__hash__` use only title and artist. Album and artwork differences don't trigger a new dispatch.
- **A worker per output** — each output's calls are wrapped in try/except and delivered on that output's own tasks. One
  output failing or stalling (e.g. a network timeout to Tuneshine) doesn't delay the others, such as the OwnTone pipe
  that has to open when a record starts.
- **tmpfs for WAV** — samples are written to `/run/needledrop/sample.wav` (tmpfs) to avoid SD card wear on the Raspberry Pi.
- **Protocol-based outputs** — output handlers implement the protocols in `outputs/base.py` structurally (Python
  `Protocol`), not by inheritance. See [Adding a new output handler](#adding-a-new-output-handler).
- **Sendspin's signal goes through a private attribute** — aiosendspin reports a source's signal only on the connection
  object, which `SendspinClient` keeps private. `_admitted_connection()` in `outputs/sendspin.py` is the one place that
  reaches for it, and `pyproject.toml` bounds aiosendspin to 10.x. Check it on every major upgrade.
- **Sendspin sends PCM** — every server accepts it, and it needs no PyAV. FLAC would need the `aiosendspin[source]`
  extra.

## Running tests

Requires [uv](https://docs.astral.sh/uv/). `.python-version` pins Python 3.13, which uv downloads if it isn't installed:

```bash
uv sync
uv run pytest tests/ -v
```

Shazam, Last.fm, ALSA and HTTP are mocked. The OwnTone audio tests use real FIFOs in a temporary directory, and the
Sendspin tests run an aiosendspin server in-process on localhost. No hardware or network access is needed.

## Dependencies

`uv.lock` pins every dependency, and it is the only place versions are pinned. The Docker image, the CI `test` job and the Nix package (through uv2nix) all install from it, so all three run the same versions. `pyproject.toml` keeps loose requirements.

- After changing dependencies in `pyproject.toml`, run `uv lock` and commit both files. `--locked` makes CI and the Docker build fail if they disagree.
- Dependabot opens one PR a month for each of `uv.lock`, `flake.lock`, the GitHub Actions versions and the `Dockerfile`'s base images, and each PR runs CI.
- uv's own version comes from `uv.lock`, where the `uv` dependency group locks it for CI's setup-uv to read, and from the tag of the `Dockerfile`'s builder image. `pyproject.toml` bounds `uv_build` to the same minor version, so uv builds the project with its bundled backend. Dependabot updates all three: the lock and the bound in its uv PR, the image tag in its docker PR.
- Python 3.13 is pinned in `.python-version` (uv, locally and in CI), both of the `Dockerfile`'s base images and `nix/package.nix`. Change them together. Dependabot leaves it alone: it ignores Python minor and major updates and keeps the builder tag's `python3.13` suffix.

## CI

The GitHub Actions workflow (`.github/workflows/ci.yaml`) has four jobs:

- **test** — runs on every push and PR to `main`. Installs the locked environment and runs pytest.
- **nix** — runs on every push and PR to `main`, on x86_64 and arm64 runners. `nix flake check` builds the package and runs the test suite against it. On x86_64 it also boots the NixOS module in a VM, which arm64 runners can't do because they have no KVM.
- **image** — runs on every push and PR to `main`, on x86_64 and arm64 runners. Builds the Docker image and runs `needledrop --help` in it, without pushing, so a Dependabot bump of a base image is built before it reaches a release.
- **docker** — runs on semver tag pushes (e.g. `0.1.0`, `v0.1.0`), after tests pass. It first checks that `pyproject.toml`'s version matches the tag. It then builds multi-arch images (`linux/amd64`, `linux/arm64`), pushes them to `ghcr.io/andyattebery/needledrop:<version>`, and promotes `:latest` unless the version is a pre-release.

## Releasing

1. Set the version with `uv version`, e.g. `uv version --bump minor` or `uv version 0.2.0`. It updates `pyproject.toml` and `uv.lock` together. Editing `pyproject.toml` by hand leaves the lock behind, and `--locked` then fails.
2. Commit, tag the commit with the version (e.g. `0.2.0`), and push both.

The docker job refuses a tag that doesn't match `pyproject.toml`'s version.

## Nix packaging

- `flake.nix` — the package, the NixOS module and the checks, for `x86_64-linux` and `aarch64-linux`.
- `nix/package.nix` — builds the app with [uv2nix](https://github.com/pyproject-nix/uv2nix) from `uv.lock`, using the same wheels pip would install.
  - `arecord` comes from `alsa-utils` built without nixpkgs' ALSA plugin bundle (pipewire, gstreamer, ffmpeg, …), which takes it from 812 MiB to 90 MiB.
  - `hw`, `dsnoop` and `null` are built into alsa-lib, so capture doesn't need the plugins.
- `nix/module.nix` — the `services.needledrop` NixOS module.
- `nix/test.nix` — the NixOS VM test. It captures noise from a `snd-dummy` card, so playback starts and recognition
  runs (and fails, with no network). It checks the OwnTone pipe gets audio and the Sendspin source listens and advertises.

A release needs no change to the flake, which builds this repository's source and reads the version from `pyproject.toml`. Dependabot moves the flake's inputs in `flake.lock`. Moving to a new NixOS release branch is a manual edit of the `nixpkgs` URL in `flake.nix`.

## Adding a new output handler

1. Create `needledrop/outputs/your_output.py` with a class implementing the three events:

```python
class YourOutput:
    async def on_track_recognized(self, track: TrackInfo) -> None:
        ...

    async def on_playback_started(self) -> None:
        ...

    async def on_playback_stopped(self) -> None:
        ...
```

   Two more protocols in `outputs/base.py` are optional:

   - **`AudioOutput`** — add `async def on_audio(self, chunk: AudioChunk)` to receive every captured chunk: 20 ms of
     S16_LE stereo PCM at `audio.sample_rate`, with its capture time. It must not block for long; an output that falls 2 s
     behind loses its oldest chunks.
   - **`ServiceOutput`** — add `async def start(self)` and `async def stop(self)` for something that lives as long as the
     app, such as a network listener. They run before the first event and on shutdown.

2. Add a config dataclass in `needledrop/config.py`:

```python
@dataclass
class YourOutputConfig:
    enabled: bool = False
    # your fields here
```

Add it to `OutputsConfig` with `field(default_factory=YourOutputConfig)`, and parse it in `load_config`.

3. Register it in `build_outputs()` in `needledrop/app.py`:

```python
if cfg.your_output.enabled:
    outputs.append(YourOutput(...))
```

4. Add the new section to `config.example.yaml`.
