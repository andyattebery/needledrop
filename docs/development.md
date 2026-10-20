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

Requires [uv](https://docs.astral.sh/uv/). `.python-version` pins Python 3.13, which uv downloads if it isn't installed:

```bash
uv sync
uv run pytest tests/ -v
```

All external dependencies (Shazam, Last.fm, ALSA, HTTP, FIFO) are mocked — no hardware or network access needed.

## Dependencies

`uv.lock` pins every dependency, and it is the only place versions are pinned. The Docker image, the CI `test` job and the Nix package (through uv2nix) all install from it, so all three run the same versions. `pyproject.toml` keeps loose requirements.

- After changing dependencies in `pyproject.toml`, run `uv lock` and commit both files. `--locked` makes CI and the Docker build fail if they disagree.
- Dependabot opens one PR a month for each of `uv.lock`, `flake.lock` and the GitHub Actions versions, and each PR runs CI.
- uv itself is pinned to 0.12.24 in the `Dockerfile` and `ci.yaml`. Change both together.
- Python 3.13 is pinned in `.python-version` (uv, locally and in CI), the `Dockerfile`'s base image and `nix/package.nix`. Change all three together.

## CI

The GitHub Actions workflow (`.github/workflows/ci.yaml`) has three jobs:

- **test** — runs on every push and PR to `main`. Installs the locked environment and runs pytest.
- **nix** — runs on every push and PR to `main`, on x86_64 and arm64 runners. `nix flake check` builds the package and runs the test suite against it. On x86_64 it also boots the NixOS module in a VM, which arm64 runners can't do because they have no KVM.
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
- `nix/test.nix` — the NixOS VM test. It records from a silent `/dev/zero` device, so no recognition and no network.

A release needs no change to the flake, which builds this repository's source and reads the version from `pyproject.toml`. Dependabot moves the flake's inputs in `flake.lock`. Moving to a new NixOS release branch is a manual edit of the `nixpkgs` URL in `flake.nix`.

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
