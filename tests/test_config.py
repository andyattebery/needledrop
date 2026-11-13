import pytest

from needledrop.config import (
    AudioConfig,
    Config,
    LastFMConfig,
    LogConfig,
    Mp3StreamConfig,
    OutputsConfig,
    OwnToneAudioConfig,
    OwnToneMetadataConfig,
    RecognitionConfig,
    SendspinConfig,
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


def test_audio_silence_hold_defaults_to_five_seconds():
    result = _build_dataclass(AudioConfig, {
        "alsa_device": "hw:0",
        "sample_rate": 48000,
        "sample_duration_seconds": 12,
    })
    assert result.silence_hold_seconds == 5.0


def test_load_config_defaults_new_outputs_to_disabled(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
audio:
  alsa_device: hw:0
  sample_rate: 48000
  sample_duration_seconds: 12
recognition:
  interval_seconds: 20
  min_track_change_seconds: 30
outputs:
  log:
    enabled: true
""")
    config = load_config(config_file)
    assert config.outputs.owntone_audio == OwnToneAudioConfig(enabled=False, audio_fifo="")
    assert config.outputs.sendspin == SendspinConfig(
        enabled=False,
        name="Turntable",
        port=8928,
        state_dir="/var/lib/needledrop",
        unpaired_access=True,
    )
    assert config.outputs.mp3_stream == Mp3StreamConfig(
        enabled=False,
        port=8000,
        path="/turntable.mp3",
        name="Turntable",
        bitrate=320,
    )


def test_load_config_parses_mp3_stream(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
audio:
  alsa_device: hw:0
  sample_rate: 48000
  sample_duration_seconds: 12
recognition:
  interval_seconds: 20
  min_track_change_seconds: 30
outputs:
  mp3_stream:
    enabled: true
    port: 8100
    path: /vinyl.mp3
    name: Record Player
    bitrate: 192
""")
    config = load_config(config_file)
    assert config.outputs.mp3_stream == Mp3StreamConfig(
        enabled=True,
        port=8100,
        path="/vinyl.mp3",
        name="Record Player",
        bitrate=192,
    )


def test_load_config_parses_owntone_audio_and_sendspin(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
audio:
  alsa_device: hw:0
  sample_rate: 48000
  sample_duration_seconds: 12
  silence_hold_seconds: 8
recognition:
  interval_seconds: 20
  min_track_change_seconds: 30
outputs:
  owntone_audio:
    enabled: true
    audio_fifo: /srv/media/Turntable
  sendspin:
    enabled: true
    name: Record Player
    port: 9000
    state_dir: /tmp/state
    unpaired_access: false
""")
    config = load_config(config_file)
    assert config.audio.silence_hold_seconds == 8
    assert config.outputs.owntone_audio == OwnToneAudioConfig(
        enabled=True, audio_fifo="/srv/media/Turntable"
    )
    assert config.outputs.sendspin == SendspinConfig(
        enabled=True,
        name="Record Player",
        port=9000,
        state_dir="/tmp/state",
        unpaired_access=False,
    )
