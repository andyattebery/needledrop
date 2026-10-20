# User Guide

## Requirements

- Linux host with ALSA audio (e.g. Raspberry Pi 4)
- Docker and Docker Compose, or NixOS (see [NixOS setup](#nixos-setup))
- An audio input device (USB audio interface, sound card) configured as a shared ALSA `dsnoop` device

## Installation

Pull the pre-built image:

```bash
docker pull ghcr.io/andyattebery/needledrop:latest
```

Or build from source:

```bash
git clone https://github.com/andyattebery/needledrop.git
cd needledrop
docker build -t needledrop .
```

## Configuration

Create a `config.yaml` based on [config.example.yaml](../config.example.yaml). All configuration is in a single YAML file passed via `--config`.

### audio

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `alsa_device` | str | *required* | ALSA device name (e.g. `dsnoopusb`) |
| `sample_rate` | int | *required* | Sample rate in Hz (e.g. `48000`) |
| `sample_duration_seconds` | int | *required* | Seconds of audio to capture per cycle |
| `silence_threshold_dbfs` | float | `-50.0` | Audio below this dBFS level is treated as silence |

### recognition

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `interval_seconds` | int | *required* | Seconds between recognition cycles |
| `min_track_change_seconds` | int | *required* | Ignore duplicate track detections within this window |

### outputs.lastfm

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable Last.fm scrobbling |
| `api_key` | str | | Last.fm API key |
| `api_secret` | str | | Last.fm API secret |
| `username` | str | | Last.fm username |
| `password_hash` | str | | MD5 hash of Last.fm password |
| `scrobble_threshold_seconds` | int | `30` | Minimum seconds a track must play before scrobbling |

### outputs.owntone_metadata

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable OwnTone metadata pipe |
| `metadata_fifo` | str | | Path to OwnTone metadata FIFO (e.g. `/media/Turntable.metadata`) |

### outputs.tuneshine

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable Tuneshine display updates |
| `host` | str | | Tuneshine device IP or hostname |

### outputs.log

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable structured log output of recognized tracks |

## Docker setup

The `docker-compose.yml` configures the container for audio access:

```yaml
services:
  needledrop:
    image: ghcr.io/andyattebery/needledrop:latest
    network_mode: host
    ipc: host
    devices:
      - /dev/snd:/dev/snd
    group_add:
      - audio
    volumes:
      - /etc/asound.conf:/etc/asound.conf:ro
      - /etc/needledrop/config.yaml:/etc/needledrop/config.yaml:ro
      - /media:/media
    tmpfs:
      - /run/needledrop
    restart: unless-stopped
```

Key settings:

- **`network_mode: host`** — required for accessing network services (OwnTone, Tuneshine) on the local network
- **`ipc: host`** — required for ALSA `dsnoop` shared memory segments
- **`/dev/snd`** — audio device passthrough
- **`audio` group** — permissions for audio devices
- **`/etc/asound.conf`** — ALSA configuration with `dsnoop` device definition
- **`/media`** — mount point for OwnTone media directory (contains the metadata FIFO)
- **`tmpfs` at `/run/needledrop`** — temporary WAV files are written here to avoid SD card wear

## NixOS setup

The repository is a flake. It provides the package and a NixOS module, `services.needledrop`, for `x86_64-linux` and `aarch64-linux`.

Add it as an input and import the module:

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    needledrop = {
      url = "github:andyattebery/needledrop";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, needledrop, ... }: {
    nixosConfigurations.turntable = nixpkgs.lib.nixosSystem {
      modules = [
        needledrop.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Then configure it. `settings` is `config.yaml` written in Nix, with the keys from [Configuration](#configuration):

```nix
services.needledrop = {
  enable = true;
  secretFiles = {
    LASTFM_API_KEY = "/run/secrets/lastfm-api-key";
    LASTFM_API_SECRET = "/run/secrets/lastfm-api-secret";
    LASTFM_PASSWORD_HASH = "/run/secrets/lastfm-password-hash";
  };
  settings = {
    audio.alsa_device = "dsnoopusb";
    outputs = {
      log.enabled = true;
      owntone_metadata = {
        enabled = true;
        metadata_fifo = "/media/Turntable.metadata";
      };
      tuneshine = {
        enabled = true;
        host = "192.168.1.120";
      };
      lastfm = {
        enabled = true;
        api_key = "$LASTFM_API_KEY";
        api_secret = "$LASTFM_API_SECRET";
        username = "your-username";
        password_hash = "$LASTFM_PASSWORD_HASH";
      };
    };
  };
};
```

`audio.alsa_device` is required. Unless you set them, the module uses `sample_rate` 48000, `sample_duration_seconds` 12, `interval_seconds` 20 and `min_track_change_seconds` 30.

### Secrets

`settings` ends up in the Nix store, which every user can read, so the Last.fm credentials don't belong in it. Keep each one in a file of its own, as sops-nix and agenix provide them, list the files in `secretFiles` by name, and refer to them in `settings` as `$NAME`, as above.

At each start the service writes its config to `/run/needledrop` with the values substituted. That directory is memory-backed, and the file is readable only by the service. The files reach the service as systemd credentials, so the values never appear in the program's environment. A `$NAME` without a file stops the service from starting instead of becoming an empty string.

### What the module sets up

- A `needledrop` system user, and `needledrop.service`, which runs as that user with the `audio` group added.
- `/run/needledrop`, memory-backed like the container's tmpfs, for the samples and the rendered config.
- `arecord` and `ffmpeg` on the program's `PATH`, as in the container. `arecord` comes without ALSA's external plugins; `hw`, `dsnoop` and the other built-in device types work.
- systemd sandboxing, which still leaves `/dev/snd` reachable and the host's IPC namespace shared, so a `dsnoop` device works.

Two differences from the container:

- **The service doesn't run as root.** If the `dsnoop` device is shared with a reader that runs as another user, such as an ALSA pipe service feeding OwnTone, its IPC objects have to admit the `audio` group. ALSA's default, `0600`, admits only the user that opened the device first. Add to the device's `asound.conf` entry:

  ```
  ipc_perm 0660
  ipc_gid audio
  ```

- **The OwnTone metadata FIFO has to be writable by the `needledrop` user,** and can't be under `/home`, which the service can't see.

## Last.fm setup

1. Create an API account at https://www.last.fm/api/account/create
2. Note the API key and secret
3. Generate your password hash:

```bash
echo -n "your-password" | md5sum
```

Or with Python:

```bash
python -c "import hashlib; print(hashlib.md5(b'your-password').hexdigest())"
```

4. Add the credentials to your `config.yaml` under `outputs.lastfm`. On NixOS, see [Secrets](#secrets).

Tracks are scrobbled when a new track is detected or playback stops, provided the previous track played for at least `scrobble_threshold_seconds`. Now-playing is updated immediately on each new track detection.

## OwnTone metadata

The OwnTone metadata output writes track information to a named pipe (FIFO) that OwnTone reads to display metadata for the audio stream. This is used when audio is piped to OwnTone via an ALSA pipe service — the FIFO lets OwnTone show the currently playing track even though the audio arrives as a raw stream.

The FIFO path (e.g. `/media/Turntable.metadata`) must match the pipe configuration in OwnTone. Artwork is downloaded and written as a base64-encoded PICT item.

## Tuneshine

[Tuneshine](https://tuneshine.rocks/) is a small LED matrix display. When a track is recognized, needledrop downloads the album artwork, resizes it to 64x64 pixels, converts it to WebP, and POSTs it along with track metadata to the device's HTTP API. When playback stops, it sends a DELETE to clear the display.

If artwork download or conversion fails, metadata is still sent as a JSON-only POST.
