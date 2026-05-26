import pytest

from needledrop.config import (
    AudioConfig,
    Config,
    LastFMConfig,
    LogConfig,
    OutputsConfig,
    OwnToneMetadataConfig,
    RecognitionConfig,
    TuneshineConfig,
    _build_dataclass,
    load_config,
)


def test_build_dataclass_filters_unknown_keys():
    result = _build_dataclass(AudioConfig, {
        "alsa_device": "hw:0",
        "sample_rate": 48000,
        "sample_duration_seconds": 12,
        "unknown_key": "ignored",
    })
    assert result.alsa_device == "hw:0"
    assert not hasattr(result, "unknown_key")


def test_build_dataclass_handles_none():
    result = _build_dataclass(LogConfig, None)
    assert result.enabled is False


def test_build_dataclass_applies_defaults():
    result = _build_dataclass(AudioConfig, {
        "alsa_device": "hw:0",
        "sample_rate": 48000,
        "sample_duration_seconds": 12,
    })
    assert result.silence_threshold_dbfs == -50.0


def test_build_dataclass_missing_required_raises():
    with pytest.raises(TypeError):
        _build_dataclass(AudioConfig, {"sample_rate": 48000})


def test_load_config_parses_valid_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
audio:
  alsa_device: dsnoopusb
  sample_rate: 48000
  sample_duration_seconds: 12

recognition:
  interval_seconds: 20
  min_track_change_seconds: 30

outputs:
  lastfm:
    enabled: false
  owntone_metadata:
    enabled: true
    metadata_fifo: /media/Turntable.metadata
  tuneshine:
    enabled: false
  log:
    enabled: true
""")
    config = load_config(config_file)
    assert config.audio.alsa_device == "dsnoopusb"
    assert config.audio.sample_rate == 48000
    assert config.recognition.interval_seconds == 20
    assert config.outputs.owntone_metadata.enabled is True
    assert config.outputs.owntone_metadata.metadata_fifo == "/media/Turntable.metadata"
    assert config.outputs.lastfm.enabled is False
    assert config.outputs.log.enabled is True


def test_load_config_disabled_outputs_parse(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
audio:
  alsa_device: hw:0
  sample_rate: 44100
  sample_duration_seconds: 10

recognition:
  interval_seconds: 15
  min_track_change_seconds: 20

outputs:
  lastfm:
    enabled: false
  owntone_metadata:
    enabled: false
  tuneshine:
    enabled: false
  log:
    enabled: false
""")
    config = load_config(config_file)
    assert config.outputs.lastfm.enabled is False
    assert config.outputs.tuneshine.host == ""
