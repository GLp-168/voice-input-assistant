from __future__ import annotations

import pytest

from voice_input.injector import (
    ClipboardInjector,
    InjectionError,
    TextInjector,
    _LinuxClipboardSnapshot,
    _WindowsClipboardSnapshot,
    _preferred_linux_clipboard_type,
    create_injector,
)


def test_create_injector_returns_protocol_compatible_object() -> None:
    injector = create_injector(dry_run=True, platform_name="win32")

    assert isinstance(injector, TextInjector)
    assert isinstance(injector, ClipboardInjector)


def test_dry_run_does_not_touch_system(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = ClipboardInjector(platform_name="win32", dry_run=True)
    monkeypatch.setattr(
        ClipboardInjector,
        "_inject_windows",
        lambda self, text: (_ for _ in ()).throw(AssertionError("system access should not happen in dry_run")),
    )

    injector.inject("hello")

    assert True


def test_windows_injection_surfaces_clear_error_when_clipboard_api_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = ClipboardInjector(platform_name="win32", dry_run=False)
    snapshot = _WindowsClipboardSnapshot(None, 10, False)
    monkeypatch.setattr(ClipboardInjector, "_snapshot_windows_clipboard", lambda self: snapshot)
    monkeypatch.setattr(ClipboardInjector, "_clipboard_sequence_number", lambda self: 10)
    monkeypatch.setattr(
        ClipboardInjector,
        "_set_clipboard_text",
        lambda self, text, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
    )

    with pytest.raises(InjectionError, match="Windows text injection failed"):
        injector.inject("hello")

    assert snapshot._closed is True


def test_windows_injection_restores_full_snapshot_when_clipboard_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = ClipboardInjector(platform_name="win32", paste_delay_seconds=0)
    snapshot = _WindowsClipboardSnapshot(123, 10, False)
    restored: list[bool] = []
    monkeypatch.setattr(ClipboardInjector, "_snapshot_windows_clipboard", lambda self: snapshot)
    monkeypatch.setattr(ClipboardInjector, "_set_clipboard_text", lambda self, text, **_kwargs: None)
    monkeypatch.setattr(ClipboardInjector, "_paste_hotkey_windows", lambda self: None)
    monkeypatch.setattr(ClipboardInjector, "_clipboard_sequence_number", lambda self: 11)
    monkeypatch.setattr(_WindowsClipboardSnapshot, "restore", lambda self: restored.append(True))

    injector.inject("hello")

    assert restored == [True]


def test_windows_injection_does_not_overwrite_newer_clipboard_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = ClipboardInjector(platform_name="win32", paste_delay_seconds=0)
    snapshot = _WindowsClipboardSnapshot(123, 10, False)
    sequence_numbers = iter((11, 12))
    restored: list[bool] = []
    monkeypatch.setattr(ClipboardInjector, "_snapshot_windows_clipboard", lambda self: snapshot)
    monkeypatch.setattr(ClipboardInjector, "_set_clipboard_text", lambda self, text, **_kwargs: None)
    monkeypatch.setattr(ClipboardInjector, "_paste_hotkey_windows", lambda self: None)
    monkeypatch.setattr(ClipboardInjector, "_clipboard_sequence_number", lambda self: next(sequence_numbers))
    monkeypatch.setattr(_WindowsClipboardSnapshot, "restore", lambda self: restored.append(True))
    monkeypatch.setattr(_WindowsClipboardSnapshot, "close", lambda self: setattr(self, "_closed", True))

    injector.inject("hello")

    assert restored == []
    assert snapshot._closed is True


def test_linux_injection_requires_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = ClipboardInjector(platform_name="linux", dry_run=False)
    monkeypatch.setattr("voice_input.injector.shutil.which", lambda name: None)

    with pytest.raises(InjectionError, match="requires these commands"):
        injector.inject("hello")


def test_linux_dry_run_skips_tool_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    injector = ClipboardInjector(platform_name="linux", dry_run=True)

    monkeypatch.setattr("voice_input.injector.shutil.which", lambda name: (_ for _ in ()).throw(AssertionError("should not probe tools")))

    injector.inject("hello")


def test_linux_snapshot_prefers_and_preserves_rich_clipboard_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = ClipboardInjector(platform_name="linux")
    tools = ("/usr/bin/wl-copy", "/usr/bin/wl-paste", "/usr/bin/xdotool")
    payload = b"\x89PNG\r\n"
    monkeypatch.setattr(
        ClipboardInjector,
        "_linux_clipboard_types",
        lambda self, _tool, _name: ("TARGETS", "text/plain", "image/png"),
    )

    def read_data(self, _tool, *, read_name, mime_type=None):
        assert read_name == "wl-paste"
        assert mime_type == "image/png"
        return payload

    monkeypatch.setattr(ClipboardInjector, "_read_linux_clipboard_data", read_data)

    snapshot = injector._snapshot_linux_clipboard(tools)

    assert snapshot == _LinuxClipboardSnapshot("image/png", payload)
    assert _preferred_linux_clipboard_type(("TARGETS", "text/html", "text/plain")) == "text/html"


def test_linux_injection_restores_snapshot_only_while_clipboard_is_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = ClipboardInjector(platform_name="linux", paste_delay_seconds=0)
    tools = ("/usr/bin/wl-copy", "/usr/bin/wl-paste", "/usr/bin/xdotool")
    snapshot = _LinuxClipboardSnapshot("image/png", b"png-data")
    restored: list[tuple[bytes, str]] = []
    monkeypatch.setattr(ClipboardInjector, "_linux_tools", lambda self: tools)
    monkeypatch.setattr(
        ClipboardInjector,
        "_snapshot_linux_clipboard",
        lambda self, _tools: snapshot,
    )
    monkeypatch.setattr(ClipboardInjector, "_set_linux_clipboard", lambda self, text, _tools: None)
    monkeypatch.setattr(ClipboardInjector, "_paste_hotkey_linux", lambda self, _tools: None)
    monkeypatch.setattr(
        ClipboardInjector,
        "_linux_clipboard_matches_text",
        lambda self, text, _tools: text == "hello",
    )
    monkeypatch.setattr(
        ClipboardInjector,
        "_write_linux_clipboard_data",
        lambda self, data, *, mime_type, tools: restored.append((data, mime_type)),
    )

    injector.inject("hello")

    assert restored == [(b"png-data", "image/png")]


def test_linux_injection_does_not_overwrite_newer_clipboard_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injector = ClipboardInjector(platform_name="linux", paste_delay_seconds=0)
    tools = ("/usr/bin/xclip", "/usr/bin/xclip", "/usr/bin/xdotool")
    restored: list[bytes] = []
    monkeypatch.setattr(ClipboardInjector, "_linux_tools", lambda self: tools)
    monkeypatch.setattr(
        ClipboardInjector,
        "_snapshot_linux_clipboard",
        lambda self, _tools: _LinuxClipboardSnapshot("text/html", b"<b>old</b>"),
    )
    monkeypatch.setattr(ClipboardInjector, "_set_linux_clipboard", lambda self, text, _tools: None)
    monkeypatch.setattr(ClipboardInjector, "_paste_hotkey_linux", lambda self, _tools: None)
    monkeypatch.setattr(
        ClipboardInjector,
        "_linux_clipboard_matches_text",
        lambda self, _text, _tools: False,
    )
    monkeypatch.setattr(
        ClipboardInjector,
        "_write_linux_clipboard_data",
        lambda self, data, **_kwargs: restored.append(data),
    )

    injector.inject("hello")

    assert restored == []


def test_linux_tool_discovery_requires_a_compatible_clipboard_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = {
        "wl-copy": "/usr/bin/wl-copy",
        "wl-paste": None,
        "xclip": "/usr/bin/xclip",
        "xdotool": "/usr/bin/xdotool",
    }
    monkeypatch.setattr("voice_input.injector.shutil.which", paths.get)

    assert ClipboardInjector(platform_name="linux")._linux_tools() == (
        "/usr/bin/xclip",
        "/usr/bin/xclip",
        "/usr/bin/xdotool",
    )
