"""Text injection helpers for desktop voice input."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class InjectionError(RuntimeError):
    """Raised when text injection cannot be completed."""


@runtime_checkable
class TextInjector(Protocol):
    """Minimal protocol for text injection backends."""

    def inject(self, text: str) -> None:
        """Insert text into the active field."""


@dataclass(slots=True)
class _WindowsClipboardSnapshot:
    """OLE clipboard object retained until injection finishes."""

    data_object: int | None
    sequence_number: int
    ole_initialized: bool
    _closed: bool = False

    def restore(self) -> None:
        if self._closed:
            return
        try:
            if self.data_object is None:
                _clear_windows_clipboard()
                return
            import ctypes

            ole32 = ctypes.windll.ole32
            _configure_ole_clipboard_api(ctypes, ole32)
            result = ole32.OleSetClipboard(ctypes.c_void_p(self.data_object))
            if result < 0:
                raise OSError(f"OleSetClipboard failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")
            result = ole32.OleFlushClipboard()
            if result < 0:
                raise OSError(f"OleFlushClipboard failed with HRESULT 0x{result & 0xFFFFFFFF:08X}")
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.data_object is not None:
                _release_com_pointer(self.data_object)
        finally:
            if self.ole_initialized:
                try:
                    import ctypes

                    ctypes.windll.ole32.OleUninitialize()
                except Exception:
                    pass


@dataclass(frozen=True, slots=True)
class _LinuxClipboardSnapshot:
    """Best available Linux clipboard representation captured before injection."""

    mime_type: str | None
    data: bytes = b""


_LINUX_CLIPBOARD_TIMEOUT_SECONDS = 5.0
_LINUX_CLIPBOARD_META_TYPES = {
    "ATOM",
    "INTEGER",
    "MULTIPLE",
    "SAVE_TARGETS",
    "TARGETS",
    "TIMESTAMP",
}
_LINUX_CLIPBOARD_TYPE_PRIORITY = (
    "image/png",
    "image/webp",
    "image/jpeg",
    "text/html",
    "text/rtf",
    "application/rtf",
    "text/uri-list",
    "text/plain;charset=utf-8",
    "UTF8_STRING",
    "text/plain",
    "STRING",
)


def create_injector(*, dry_run: bool = False, platform_name: str | None = None) -> TextInjector:
    """Create the default desktop text injector.

    The MVP uses a clipboard-paste strategy on both Windows and Linux.
    ``dry_run`` keeps the control flow testable without touching the system.
    """

    return ClipboardInjector(platform_name=platform_name, dry_run=dry_run)


@dataclass(slots=True)
class ClipboardInjector:
    """Clipboard-driven injector with platform-specific paste hooks."""

    platform_name: str | None = None
    dry_run: bool = False
    restore_clipboard: bool = True
    paste_delay_seconds: float = 0.05

    def __post_init__(self) -> None:
        self.platform_name = _normalize_platform(self.platform_name)

    def inject(self, text: str) -> None:
        if self.dry_run or not text:
            return

        if self.platform_name.startswith(("win", "cygwin", "msys")):
            self._inject_windows(text)
            return

        if self.platform_name.startswith("linux"):
            self._inject_linux(text)
            return

        raise InjectionError(f"Unsupported platform for text injection: {self.platform_name}")

    def _inject_windows(self, text: str) -> None:
        snapshot: _WindowsClipboardSnapshot | None = None
        owned_sequence: int | None = None
        try:
            snapshot = self._snapshot_windows_clipboard()
            self._set_clipboard_text(text, expected_sequence=snapshot.sequence_number)
            owned_sequence = self._clipboard_sequence_number()
            self._paste_hotkey_windows()
            if self.paste_delay_seconds > 0:
                time.sleep(self.paste_delay_seconds)
        except Exception as exc:  # Best effort cleanup; the controller handles the error.
            if snapshot is not None and owned_sequence is None:
                current_sequence = self._clipboard_sequence_number()
                if current_sequence != snapshot.sequence_number:
                    owned_sequence = current_sequence
            raise InjectionError(f"Windows text injection failed: {exc}") from exc
        finally:
            self._restore_windows_clipboard_best_effort(snapshot, owned_sequence)

    def _inject_linux(self, text: str) -> None:
        tools = self._linux_tools()
        snapshot = self._snapshot_linux_clipboard(tools)
        owns_clipboard = False
        try:
            self._set_linux_clipboard(text, tools)
            owns_clipboard = True
            self._paste_hotkey_linux(tools)
            if self.paste_delay_seconds > 0:
                time.sleep(self.paste_delay_seconds)
        except Exception as exc:  # Best effort cleanup; the controller handles the error.
            if not owns_clipboard:
                owns_clipboard = self._linux_clipboard_matches_text(text, tools)
            raise InjectionError(f"Linux text injection failed: {exc}") from exc
        finally:
            self._restore_linux_clipboard_best_effort(
                snapshot,
                expected_text=text if owns_clipboard else None,
                tools=tools,
            )

    def _restore_windows_clipboard_best_effort(
        self,
        snapshot: _WindowsClipboardSnapshot | None,
        owned_sequence: int | None,
    ) -> None:
        if snapshot is None:
            return
        if not self.restore_clipboard or owned_sequence is None:
            snapshot.close()
            return
        try:
            if self._clipboard_sequence_number() == owned_sequence:
                snapshot.restore()
            else:
                snapshot.close()
        except Exception:
            snapshot.close()

    def _restore_linux_clipboard_best_effort(
        self,
        snapshot: _LinuxClipboardSnapshot | None,
        *,
        expected_text: str | None,
        tools: tuple[str, str, str],
    ) -> None:
        if not self.restore_clipboard or snapshot is None or expected_text is None:
            return
        try:
            if not self._linux_clipboard_matches_text(expected_text, tools):
                return
            if snapshot.mime_type is None:
                self._clear_linux_clipboard(tools)
            else:
                self._write_linux_clipboard_data(
                    snapshot.data,
                    mime_type=snapshot.mime_type,
                    tools=tools,
                )
        except Exception:
            pass

    def _snapshot_windows_clipboard(self) -> _WindowsClipboardSnapshot:
        try:
            import ctypes
        except Exception:
            raise InjectionError("Windows clipboard APIs are unavailable")

        if not hasattr(ctypes, "windll"):
            raise InjectionError("Windows clipboard APIs are unavailable")

        user32 = ctypes.windll.user32
        ole32 = ctypes.windll.ole32
        _configure_ole_clipboard_api(ctypes, ole32)
        user32.CountClipboardFormats.argtypes = []
        user32.CountClipboardFormats.restype = ctypes.c_int
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint

        initialize_result = ole32.OleInitialize(None)
        ole_initialized = initialize_result in {0, 1}
        if initialize_result < 0:
            raise InjectionError(
                f"Unable to initialize OLE clipboard access (HRESULT 0x{initialize_result & 0xFFFFFFFF:08X})"
            )

        sequence_number = int(user32.GetClipboardSequenceNumber())
        try:
            if user32.CountClipboardFormats() == 0:
                return _WindowsClipboardSnapshot(None, sequence_number, ole_initialized)
            data_object = ctypes.c_void_p()
            result = ole32.OleGetClipboard(ctypes.byref(data_object))
            if result < 0 or not data_object.value:
                raise InjectionError(
                    f"Unable to snapshot the Windows clipboard (HRESULT 0x{result & 0xFFFFFFFF:08X})"
                )
            return _WindowsClipboardSnapshot(int(data_object.value), sequence_number, ole_initialized)
        except Exception:
            if ole_initialized:
                ole32.OleUninitialize()
            raise

    def _clipboard_sequence_number(self) -> int:
        import ctypes

        if not hasattr(ctypes, "windll"):
            raise InjectionError("Windows clipboard APIs are unavailable")
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.argtypes = []
        user32.GetClipboardSequenceNumber.restype = ctypes.c_uint
        return int(user32.GetClipboardSequenceNumber())

    def _set_clipboard_text(self, text: str, *, expected_sequence: int | None = None) -> None:
        import ctypes
        from ctypes import wintypes

        if not hasattr(ctypes, "windll"):
            raise InjectionError("Windows clipboard APIs are unavailable")

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        _configure_clipboard_api(ctypes, wintypes, user32, kernel32)

        payload = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(payload)

        if not user32.OpenClipboard(None):
            raise InjectionError("Unable to open Windows clipboard")
        try:
            if expected_sequence is not None and self._clipboard_sequence_number() != expected_sequence:
                raise InjectionError("Windows clipboard changed before text injection")
            if not user32.EmptyClipboard():
                raise InjectionError("Unable to clear Windows clipboard")
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise InjectionError("Unable to allocate Windows clipboard buffer")
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                raise InjectionError("Unable to lock Windows clipboard buffer")
            try:
                ctypes.memmove(locked, payload, size)
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                raise InjectionError("Unable to publish Windows clipboard text")
        finally:
            user32.CloseClipboard()

    def _paste_hotkey_windows(self) -> None:
        import ctypes

        if not hasattr(ctypes, "windll"):
            raise InjectionError("Windows input APIs are unavailable")

        user32 = ctypes.windll.user32
        VK_CONTROL = 0x11
        VK_V = 0x56
        KEYEVENTF_KEYUP = 0x0002

        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _linux_tools(self) -> tuple[str, str, str]:
        wl_copy = shutil.which("wl-copy")
        wl_paste = shutil.which("wl-paste")
        xclip = shutil.which("xclip")
        paste_hotkey = shutil.which("xdotool")

        missing = []
        if wl_copy is not None and wl_paste is not None:
            copy_tool, read_tool = wl_copy, wl_paste
        elif xclip is not None:
            copy_tool = read_tool = xclip
        else:
            copy_tool = read_tool = None
            missing.append("wl-copy + wl-paste or xclip")
        if paste_hotkey is None:
            missing.append("xdotool")
        if missing:
            raise InjectionError(
                "Linux text injection requires these commands: " + ", ".join(missing)
            )
        assert copy_tool is not None and read_tool is not None and paste_hotkey is not None
        return copy_tool, read_tool, paste_hotkey

    def _snapshot_linux_clipboard(
        self,
        tools: tuple[str, str, str],
    ) -> _LinuxClipboardSnapshot | None:
        _, read_tool, _ = tools
        read_name = _tool_name(read_tool)
        try:
            mime_types = self._linux_clipboard_types(read_tool, read_name)
            if not mime_types:
                return _LinuxClipboardSnapshot(None)
            mime_type = _preferred_linux_clipboard_type(mime_types)
            if mime_type is None:
                return None
            data = self._read_linux_clipboard_data(
                read_tool,
                read_name=read_name,
                mime_type=mime_type,
            )
            return _LinuxClipboardSnapshot(mime_type=mime_type, data=data)
        except Exception:
            return None

    def _linux_clipboard_types(self, read_tool: str, read_name: str) -> tuple[str, ...]:
        if read_name == "wl-paste":
            command = [read_tool, "--list-types"]
        elif read_name == "xclip":
            command = [
                read_tool,
                "-selection",
                "clipboard",
                "-out",
                "-target",
                "TARGETS",
            ]
        else:
            raise InjectionError(f"Unsupported Linux clipboard reader: {read_tool}")
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
        )
        return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())

    def _read_linux_clipboard_data(
        self,
        read_tool: str,
        *,
        read_name: str,
        mime_type: str | None = None,
    ) -> bytes:
        if read_name == "wl-paste":
            command = [read_tool, "--no-newline"]
            if mime_type:
                command.extend(["--type", mime_type])
        elif read_name == "xclip":
            command = [read_tool, "-selection", "clipboard", "-out"]
            if mime_type:
                command.extend(["-target", mime_type])
        else:
            raise InjectionError(f"Unsupported Linux clipboard reader: {read_tool}")
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
        )
        return completed.stdout

    def _set_linux_clipboard(self, text: str, tools: tuple[str, str, str]) -> None:
        self._write_linux_clipboard_data(
            text.encode("utf-8"),
            mime_type="text/plain;charset=utf-8",
            tools=tools,
        )

    def _write_linux_clipboard_data(
        self,
        data: bytes,
        *,
        mime_type: str,
        tools: tuple[str, str, str],
    ) -> None:
        copy_tool, _, _ = tools
        copy_name = _tool_name(copy_tool)
        if copy_name == "wl-copy":
            subprocess.run(
                [copy_tool, "--type", mime_type],
                input=data,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
            )
            return
        if copy_name == "xclip":
            subprocess.run(
                [
                    copy_tool,
                    "-selection",
                    "clipboard",
                    "-in",
                    "-target",
                    mime_type,
                ],
                input=data,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
            )
            return
        raise InjectionError(f"Unsupported Linux clipboard tool: {copy_tool}")

    def _linux_clipboard_matches_text(
        self,
        expected_text: str,
        tools: tuple[str, str, str],
    ) -> bool:
        _, read_tool, _ = tools
        try:
            current = self._read_linux_clipboard_data(
                read_tool,
                read_name=_tool_name(read_tool),
            )
        except Exception:
            return False
        return current == expected_text.encode("utf-8")

    def _clear_linux_clipboard(self, tools: tuple[str, str, str]) -> None:
        copy_tool, _, _ = tools
        copy_name = _tool_name(copy_tool)
        if copy_name == "wl-copy":
            subprocess.run(
                [copy_tool, "--clear"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
            )
            return
        if copy_name == "xclip":
            self._write_linux_clipboard_data(
                b"",
                mime_type="text/plain;charset=utf-8",
                tools=tools,
            )
            return
        raise InjectionError(f"Unsupported Linux clipboard tool: {copy_tool}")

    def _paste_hotkey_linux(self, tools: tuple[str, str, str]) -> None:
        _, _, paste_hotkey = tools
        subprocess.run(
            [paste_hotkey, "key", "--clearmodifiers", "ctrl+v"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_LINUX_CLIPBOARD_TIMEOUT_SECONDS,
        )


def _normalize_platform(platform_name: str | None) -> str:
    if platform_name:
        return platform_name.lower()
    return sys.platform.lower()


def _tool_name(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).name


def _preferred_linux_clipboard_type(mime_types: tuple[str, ...]) -> str | None:
    by_lower = {mime_type.lower(): mime_type for mime_type in mime_types}
    for preferred in _LINUX_CLIPBOARD_TYPE_PRIORITY:
        match = by_lower.get(preferred.lower())
        if match is not None:
            return match
    for mime_type in mime_types:
        if mime_type.upper() not in _LINUX_CLIPBOARD_META_TYPES:
            return mime_type
    return None


def _configure_clipboard_api(ctypes, wintypes, user32, kernel32) -> None:
    """Declare Win32 signatures so 64-bit clipboard handles are not truncated."""

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _configure_ole_clipboard_api(ctypes, ole32) -> None:
    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = ctypes.c_long
    ole32.OleUninitialize.argtypes = []
    ole32.OleUninitialize.restype = None
    ole32.OleGetClipboard.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    ole32.OleGetClipboard.restype = ctypes.c_long
    ole32.OleSetClipboard.argtypes = [ctypes.c_void_p]
    ole32.OleSetClipboard.restype = ctypes.c_long
    ole32.OleFlushClipboard.argtypes = []
    ole32.OleFlushClipboard.restype = ctypes.c_long


def _release_com_pointer(address: int) -> None:
    import ctypes

    pointer = ctypes.c_void_p(address)
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release_address = ctypes.cast(vtable[2], ctypes.c_void_p).value
    if release_address is None:
        return
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release_address)
    release(pointer)


def _clear_windows_clipboard() -> None:
    import ctypes
    from ctypes import wintypes

    if not hasattr(ctypes, "windll"):
        raise InjectionError("Windows clipboard APIs are unavailable")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _configure_clipboard_api(ctypes, wintypes, user32, kernel32)
    if not user32.OpenClipboard(None):
        raise InjectionError("Unable to open Windows clipboard")
    try:
        if not user32.EmptyClipboard():
            raise InjectionError("Unable to clear Windows clipboard")
    finally:
        user32.CloseClipboard()
