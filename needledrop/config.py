from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AudioConfig:
    alsa_device: str
    sample_rate: int
    sample_duration_seconds: int
    silence_threshold_dbfs: float = -50.0
    silence_hold_seconds: float = 5.0


@dataclass
class RecognitionConfig:
    interval_seconds: int
    min_track_change_seconds: int


@dataclass
class LastFMConfig:
    enabled: bool = False
    api_key: str = ""
    api_secret: str = ""
    username: str = ""
    password_hash: str = ""
    scrobble_threshold_seconds: int = 30


@dataclass
class OwnToneMetadataConfig:
    enabled: bool = False
    metadata_fifo: str = ""


@dataclass
class OwnToneAudioConfig:
    enabled: bool = False
    audio_fifo: str = ""


@dataclass
class TuneshineConfig:
    enabled: bool = False
    host: str = ""


@dataclass
class LogConfig:
    enabled: bool = False


@dataclass
class SendspinConfig:
    enabled: bool = False
    name: str = "Turntable"
    port: int = 8928
    state_dir: str = "/var/lib/needledrop"
    unpaired_access: bool = True


@dataclass
class Mp3StreamConfig:
    enabled: bool = False
    port: int = 8000
    path: str = "/turntable.mp3"
    # The station name (icy-name), and the title while no track is known.
    name: str = "Turntable"
    # kbit/s, constant.
    bitrate: int = 320


@dataclass
class OutputsConfig:
    lastfm: LastFMConfig
    owntone_metadata: OwnToneMetadataConfig
    tuneshine: TuneshineConfig
    log: LogConfig
    owntone_audio: OwnToneAudioConfig = field(default_factory=OwnToneAudioConfig)
    sendspin: SendspinConfig = field(default_factory=SendspinConfig)
    mp3_stream: Mp3StreamConfig = field(default_factory=Mp3StreamConfig)


@dataclass
class Config:
    audio: AudioConfig
    recognition: RecognitionConfig
    outputs: OutputsConfig


def _build_dataclass(cls, data: dict):
    if data is None:
        data = {}
    fields = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in data.items() if k in fields})


def load_config(path: Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    outputs_raw = raw.get("outputs", {})
    outputs = OutputsConfig(
        lastfm=_build_dataclass(LastFMConfig, outputs_raw.get("lastfm")),
        owntone_metadata=_build_dataclass(
            OwnToneMetadataConfig, outputs_raw.get("owntone_metadata")
        ),
        tuneshine=_build_dataclass(TuneshineConfig, outputs_raw.get("tuneshine")),
        log=_build_dataclass(LogConfig, outputs_raw.get("log")),
        owntone_audio=_build_dataclass(OwnToneAudioConfig, outputs_raw.get("owntone_audio")),
        sendspin=_build_dataclass(SendspinConfig, outputs_raw.get("sendspin")),
        mp3_stream=_build_dataclass(Mp3StreamConfig, outputs_raw.get("mp3_stream")),
    )

    return Config(
        audio=_build_dataclass(AudioConfig, raw.get("audio")),
        recognition=_build_dataclass(RecognitionConfig, raw.get("recognition")),
        outputs=outputs,
    )
