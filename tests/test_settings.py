from __future__ import annotations

import json

import pytest

from voice_input.settings import AppSettings, DEFAULT_HOTKEY, SettingsStore


def test_from_dict_rejects_invalid_field_types_individually() -> None:
    defaults = AppSettings()

    settings = AppSettings.from_dict(
        {
            "hotkey": 123,
            "start_minimized": "false",
            "recognition_language": "en",
            "microphone_device": "2:Conference microphone",
            "streaming_chunk_ms": True,
            "streaming_update_interval_ms": -1,
            "partial_transcription_interval_seconds": float("nan"),
            "enabled_builtin_lexicons": ["office_meeting", 42],
        }
    )

    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings.start_minimized is defaults.start_minimized
    assert settings.recognition_language == "en"
    assert settings.microphone_device == "2:Conference microphone"
    assert settings.streaming_chunk_ms == defaults.streaming_chunk_ms
    assert settings.streaming_update_interval_ms == defaults.streaming_update_interval_ms
    assert (
        settings.partial_transcription_interval_seconds
        == defaults.partial_transcription_interval_seconds
    )
    assert settings.enabled_builtin_lexicons == defaults.enabled_builtin_lexicons


def test_from_dict_normalizes_supported_legacy_values() -> None:
    settings = AppSettings.from_dict(
        {
            "hotkey": "Ctrl+Alt+Space",
            "partial_transcription_interval_seconds": 90,
            "enabled_builtin_lexicons": "office_meeting",
        }
    )

    assert settings.hotkey == DEFAULT_HOTKEY
    assert settings.partial_transcription_interval_seconds == 90.0
    assert settings.enabled_builtin_lexicons == ("office_meeting",)


def test_store_loads_valid_fields_when_other_json_types_are_invalid(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "inject_text": "yes",
                "floating_window": False,
                "streaming_chunk_ms": "100",
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.inject_text is AppSettings().inject_text
    assert settings.floating_window is False
    assert settings.streaming_chunk_ms == AppSettings().streaming_chunk_ms


def test_atomic_save_preserves_existing_settings_when_replace_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.save(AppSettings(recognition_language="en"))
    original = path.read_bytes()

    def fail_replace(_source, _target) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("voice_input.settings.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.save(AppSettings(recognition_language="zh-Hant"))

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".settings.json.*.tmp")) == []
