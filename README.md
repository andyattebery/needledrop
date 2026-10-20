![ci](https://github.com/andyattebery/needledrop/actions/workflows/ci.yaml/badge.svg)

# needledrop

Audio fingerprinting service for vinyl record identification.

Captures audio from an ALSA device, identifies tracks via [Shazam](https://www.shazam.com/), and dispatches results to pluggable output handlers:

- [Last.fm](https://www.last.fm/) scrobbling and now-playing
- [OwnTone](https://owntone.github.io/owntone-server/) metadata pipe
- [Tuneshine](https://tuneshine.rocks/) LED matrix display
- Structured logging

Designed to run on a Raspberry Pi alongside a vinyl playback setup, in Docker or as a NixOS service.

## Quick start

See the [User Guide](docs/user-guide.md) for installation, configuration, and Docker or NixOS setup.

## Development

See the [Development Guide](docs/development.md) for architecture, testing, and contributing.
