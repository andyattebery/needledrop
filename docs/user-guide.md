# User Guide

## Requirements

- Linux host with ALSA audio (e.g. Raspberry Pi 4)
- Docker and Docker Compose, or NixOS (see [NixOS setup](#nixos-setup))
- An audio input device (USB audio interface, sound card). If anything else reads it too, configure it as a shared ALSA
  `dsnoop` device. needledrop captures continuously and feeds OwnTone itself, so on its own it needs no sharing.

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
| `sample_duration_seconds` | int | *required* | Seconds of the latest audio sent to Shazam per recognition |
| `silence_threshold_dbfs` | float | `-50.0` | Audio below this dBFS level is treated as silence |
| `silence_hold_seconds` | float | `5.0` | Seconds of silence before playback counts as stopped |

needledrop captures without a break. Playback starts once there has been 0.1 s of sound, and stops after
`silence_hold_seconds` of silence, so the gap between two tracks doesn't stop it.

### recognition

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `interval_seconds` | int | *required* | Seconds between recognitions while a record plays. The first comes `sample_duration_seconds` after playback starts |
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

### outputs.owntone_audio

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable writing the turntable's audio to OwnTone |
| `audio_fifo` | str | | Path to OwnTone's audio pipe (e.g. `/media/Turntable`) |

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

### outputs.sendspin

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the Sendspin source |
| `name` | str | `Turntable` | Name the input appears under in Music Assistant |
| `port` | int | `8928` | Port the Sendspin server connects to |
| `state_dir` | str | `/var/lib/needledrop` | Directory for the device's key and pairing records. It must persist across restarts |
| `unpaired_access` | bool | `true` | Let a server use the source once its operator approves it, without pairing |

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
      - needledrop-state:/var/lib/needledrop
    tmpfs:
      - /run/needledrop
    restart: unless-stopped

volumes:
  needledrop-state:
```

Key settings:

- **`network_mode: host`** — required for accessing network services (OwnTone, Tuneshine) on the local network, and
  for the Sendspin source's mDNS advertisement and port
- **`ipc: host`** — required for ALSA `dsnoop` shared memory segments
- **`/dev/snd`** — audio device passthrough
- **`audio` group** — permissions for audio devices
- **`/etc/asound.conf`** — ALSA configuration with `dsnoop` device definition
- **`/media`** — mount point for OwnTone media directory (contains the audio and metadata FIFOs)
- **`needledrop-state`** — the Sendspin source's key and pairing records, which must survive a new container
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
      owntone_audio = {
        enabled = true;
        audio_fifo = "/media/Turntable";
      };
      owntone_metadata = {
        enabled = true;
        metadata_fifo = "/media/Turntable.metadata";
      };
      sendspin.enabled = true;
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

With the Sendspin source enabled, set `services.needledrop.openFirewall = true` unless you open the ports yourself. It
opens the source's port over TCP and mDNS on 5353/udp.

### Secrets

`settings` ends up in the Nix store, which every user can read, so the Last.fm credentials don't belong in it. Keep each one in a file of its own, as sops-nix and agenix provide them, list the files in `secretFiles` by name, and refer to them in `settings` as `$NAME`, as above.

At each start the service writes its config to `/run/needledrop` with the values substituted. That directory is memory-backed, and the file is readable only by the service. The files reach the service as systemd credentials, so the values never appear in the program's environment. A `$NAME` without a file stops the service from starting instead of becoming an empty string.

### What the module sets up

- A `needledrop` system user, and `needledrop.service`, which runs as that user with the `audio` group added.
- `/run/needledrop`, memory-backed like the container's tmpfs, for the samples and the rendered config.
- `/var/lib/needledrop`, readable by the service alone, for the Sendspin source's key and pairing records.
- `arecord` and `ffmpeg` on the program's `PATH`, as in the container. `arecord` comes without ALSA's external plugins; `hw`, `dsnoop` and the other built-in device types work.
- systemd sandboxing, which still leaves `/dev/snd` reachable and the host's IPC namespace shared, so a `dsnoop` device works.
  With the Sendspin source enabled, it also allows netlink sockets, which mDNS needs to list the network interfaces.

Two differences from the container:

- **The service doesn't run as root.** If the `dsnoop` device is shared with a reader that runs as another user, its IPC objects have to admit the `audio` group. ALSA's default, `0600`, admits only the user that opened the device first. Add to the device's `asound.conf` entry:

  ```
  ipc_perm 0660
  ipc_gid audio
  ```

- **The OwnTone FIFOs have to be writable by the `needledrop` user,** and can't be under `/home`, which the service can't see.

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

## OwnTone

OwnTone plays the named pipes it finds in its library. needledrop feeds two of them:

- **Audio** (`owntone_audio`): the turntable as raw S16_LE stereo PCM at `audio.sample_rate`, which has to match OwnTone's
  `pipe_sample_rate`. The pipe is open only while a record plays. needledrop opens it once playback starts, and OwnTone
  starts playing when the data arrives. needledrop closes it after `silence_hold_seconds` of silence, and OwnTone stops at
  the end of the stream. needledrop never waits on the pipe: while OwnTone isn't reading it, the audio is dropped and
  needledrop retries once a second.
- **Metadata** (`owntone_metadata`): track information, which OwnTone shows for the stream. Artwork is downloaded and
  written as a base64-encoded PICT item. OwnTone reads this pipe only while it plays the turntable, so needledrop skips
  the track information at other times instead of waiting for OwnTone.

Create both FIFOs in OwnTone's library (e.g. `/media/Turntable` and `/media/Turntable.metadata`) before OwnTone scans it.
The metadata pipe's name has to be the audio pipe's name with `.metadata` appended.

## Sendspin (Music Assistant)

The Sendspin output makes needledrop a [Sendspin](https://www.sendspin-audio.com/) source. [Music
Assistant](https://www.music-assistant.io/) 2.10 or later, with its Sendspin and Sendspin Source providers enabled, lists
it under Live Inputs by its `name`. Play it on any player or group from there.

- **Discovery.** needledrop advertises itself over mDNS and listens on `port`, and Music Assistant connects to it. The
  host's firewall has to let both in.
- **Access.** A new device stays unapproved until you act on it in Music Assistant. With `unpaired_access` on, you
  can approve it together with its input. Otherwise pair it: Music Assistant asks for a code, and needledrop logs it
  as `Sendspin pairing: enter code 123-456 on the server`.
- **Autostart.** needledrop reports when a record is playing. Choose an autostart target in the device's settings in
  Music Assistant, and putting the needle down starts playback there. Music Assistant stops it again after a minute
  without signal.
- **Audio only.** Sendspin's source role has no field for track information, so Music Assistant shows the input's name,
  not the track. The other outputs still get the recognized tracks.
- **State.** The device's key in `state_dir` is its identity. If the key is lost, Music Assistant sees a new device to
  approve or pair again.

The audio is PCM at `audio.sample_rate`, 16-bit stereo: about 1.5 Mbit/s at 48 kHz. needledrop sends it only while
Music Assistant is playing the input.

## Tuneshine

[Tuneshine](https://tuneshine.rocks/) is a small LED matrix display. When a track is recognized, needledrop downloads the album artwork, resizes it to 64x64 pixels, converts it to WebP, and POSTs it along with track metadata to the device's HTTP API. When playback stops, it sends a DELETE to clear the display.

If artwork download or conversion fails, metadata is still sent as a JSON-only POST.
