"""Settings storage for the desktop shell.

The MVP keeps configuration intentionally small and dependency free.  Values are
stored as JSON under the user's config directory so the ASR and packaging layers
can evolve independently from the UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from .resources import packaged_enabled_builtin_lexicons


APP_NAME = "voice_input"
DEFAULT_HOTKEY = "Ctrl+Shift+F8"
LEGACY_DEFAULT_HOTKEYS = {"ctrl+alt+space"}
_INVALID_SETTING = object()
_POSITIVE_NUMBER_SETTINGS = {
    "streaming_chunk_ms",
    "streaming_update_interval_ms",
    "partial_transcription_interval_seconds",
}


def default_config_dir() -> Path:
    """Return a user-specific config directory for the current platform."""

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


@dataclass(slots=True)
class AppSettings:
    """User-editable settings used by the desktop shell."""

    hotkey: str = DEFAULT_HOTKEY
    start_minimized: bool = False
    floating_window: bool = True
    inject_text: bool = False
    enable_hotkey: bool = True
    minimize_to_tray: bool = True
    launch_on_startup: bool = False
    show_first_run_hint: bool = True
    asr_backend: str = "funasr"
    whisper_model_path: str = ""
    whisper_executable: str = "whisper-cli"
    recognition_language: str = "zh-Hans"
    microphone_device: str = ""
    transcript_editable: bool = False
    streaming_transcription_enabled: bool = True
    streaming_backend: str = "funasr"
    sherpa_streaming_model_dir: str = ""
    funasr_model_root: str = ""
    funasr_streaming_model: str = ""
    funasr_offline_model: str = ""
    funasr_vad_model: str = ""
    funasr_punc_model: str = ""
    funasr_speaker_model: str = ""
    streaming_chunk_ms: int = 100
    streaming_update_interval_ms: int = 180
    partial_transcription_enabled: bool = False
    partial_transcription_interval_seconds: float = 60.0
    lexicon_enabled: bool = True
    lexicon_path: str = ""
    enabled_builtin_lexicons: tuple[str, ...] = field(default_factory=packaged_enabled_builtin_lexicons)
    product_tier: str = "enterprise"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        defaults = cls()
        values: dict[str, Any] = {}
        for setting_field in fields(cls):
            name = setting_field.name
            if name not in data:
                continue
            value = _validated_setting_value(
                name,
                data[name],
                default=getattr(defaults, name),
            )
            if value is not _INVALID_SETTING:
                values[name] = value

        hotkey = values.get("hotkey")
        if isinstance(hotkey, str) and hotkey.strip().lower() in LEGACY_DEFAULT_HOTKEYS:
            values["hotkey"] = DEFAULT_HOTKEY
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    """Read and write settings from a small JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_config_dir() / "settings.json"

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()

        try:
            with self.path.open("r", encoding="utf-8") as settings_file:
                data = json.load(settings_file)
        except (OSError, json.JSONDecodeError):
            return AppSettings()

        if not isinstance(data, dict):
            return AppSettings()
        return AppSettings.from_dict(data)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as settings_file:
                temporary_path = Path(settings_file.name)
                json.dump(settings.to_dict(), settings_file, indent=2, ensure_ascii=False)
                settings_file.write("\n")
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def _validated_setting_value(name: str, value: Any, *, default: Any) -> Any:
    if name == "enabled_builtin_lexicons":
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)) and all(
            isinstance(item, str) for item in value
        ):
            return tuple(value)
        return _INVALID_SETTING
    if isinstance(default, bool):
        return value if isinstance(value, bool) else _INVALID_SETTING
    if isinstance(default, str):
        return value if isinstance(value, str) else _INVALID_SETTING
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            return _INVALID_SETTING
        if name in _POSITIVE_NUMBER_SETTINGS and value <= 0:
            return _INVALID_SETTING
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return _INVALID_SETTING
        number = float(value)
        if not math.isfinite(number):
            return _INVALID_SETTING
        if name in _POSITIVE_NUMBER_SETTINGS and number <= 0:
            return _INVALID_SETTING
        return number
    return _INVALID_SETTING
