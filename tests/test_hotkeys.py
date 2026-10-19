from __future__ import annotations

import pytest

from voice_input.hotkeys import HotkeyError, normalize_hotkey, parse_hotkey
from voice_input.settings import DEFAULT_HOTKEY, AppSettings


def test_parse_hotkey_ctrl_alt_space() -> None:
    modifiers, key = parse_hotkey("Ctrl+Alt+Space")

    assert modifiers
    assert key == 0x20


def test_parse_hotkey_function_key() -> None:
    _modifiers, key = parse_hotkey("Ctrl+Shift+F8")

    assert key == 0x77


def test_default_hotkey_uses_low_conflict_function_key() -> None:
    assert DEFAULT_HOTKEY == "Ctrl+Shift+F8"
    assert AppSettings().hotkey == DEFAULT_HOTKEY


def test_legacy_default_hotkey_is_migrated() -> None:
    assert AppSettings.from_dict({"hotkey": "Ctrl+Alt+Space"}).hotkey == DEFAULT_HOTKEY


def test_parse_hotkey_rejects_unknown_key() -> None:
    with pytest.raises(HotkeyError):
        parse_hotkey("Ctrl+Moon")


def test_meta_modifier_is_normalized_to_windows_modifier() -> None:
    modifiers, key = parse_hotkey("Meta+K")

    assert modifiers != 0
    assert key == ord("K")
    assert normalize_hotkey("shift+meta+k") == "Shift+Win+K"


def test_parse_hotkey_rejects_bare_letter_and_multiple_keys() -> None:
    with pytest.raises(HotkeyError, match="require a modifier"):
        parse_hotkey("K")
    with pytest.raises(HotkeyError, match="multiple keys"):
        parse_hotkey("Ctrl+K+L")
