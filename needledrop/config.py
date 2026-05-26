from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class AudioConfig:
    alsa_device: str
    sample_rate: int
    sample_duration_seconds: int
    silence_threshold_dbfs: float = -50.0


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
class TuneshineConfig:
    enabled: bool = False
    host: str = ""


@dataclass
class LogConfig:
    enabled: bool = False


@dataclass
class OutputsConfig:
    lastfm: LastFMConfig
    owntone_metadata: OwnToneMetadataConfig
    tuneshine: TuneshineConfig
    log: LogConfig


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
    )

    return Config(
        audio=_build_dataclass(AudioConfig, raw.get("audio")),
        recognition=_build_dataclass(RecognitionConfig, raw.get("recognition")),
        outputs=outputs,
    )
