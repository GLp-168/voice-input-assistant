"""Global hotkey support with a Windows native implementation."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import logging
import sys
import threading


LOGGER = logging.getLogger(__name__)


class HotkeyError(RuntimeError):
    """Raised when a hotkey cannot be registered."""


class GlobalHotkeyManager:
    """Register a global hotkey when the platform supports it."""

    def __init__(self, hotkey: str, on_trigger: Callable[[], None], *, enabled: bool = True) -> None:
        self.hotkey = hotkey
        self.on_trigger = on_trigger
        self.enabled = enabled
        self._impl: _WindowsHotkeyLoop | None = None
        self.last_error: Exception | None = None
        self.is_registered = False

    def start(self) -> bool:
        self.last_error = None
        self.is_registered = False
        if not self.enabled:
            return False
        self.stop()
        if not sys.platform.startswith("win"):
            LOGGER.info("global hotkey skipped on unsupported platform: %s", sys.platform)
            return False
        try:
            self._impl = _WindowsHotkeyLoop(self.hotkey, self.on_trigger)
            self._impl.start()
            self.is_registered = True
            LOGGER.info("registered global hotkey: %s", self.hotkey)
            return True
        except Exception:
            self.last_error = sys.exc_info()[1]
            LOGGER.exception("failed to register global hotkey: %s", self.hotkey)
            self._impl = None
            return False

    def stop(self) -> None:
        if self._impl is not None:
            self._impl.stop()
            self._impl = None
        self.is_registered = False


class _WindowsHotkeyLoop:
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    HOTKEY_ID = 0x5649

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000
    ERROR_HOTKEY_ALREADY_REGISTERED = 1409
    ERROR_ACCESS_DENIED = 5

    KEY_CODES = {
        "SPACE": 0x20,
        "TAB": 0x09,
        "ENTER": 0x0D,
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "BACKSPACE": 0x08,
        "DELETE": 0x2E,
        "INSERT": 0x2D,
        "HOME": 0x24,
        "END": 0x23,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
        "UP": 0x26,
        "DOWN": 0x28,
        "LEFT": 0x25,
        "RIGHT": 0x27,
    }

    def __init__(self, hotkey: str, on_trigger: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.on_trigger = on_trigger
        self.modifiers, self.key_code = parse_hotkey(hotkey)
        self.thread_id = 0
        self.error: HotkeyError | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="voice-input-hotkey", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=3)
        if self.thread_id == 0:
            raise HotkeyError(f"hotkey thread did not start: {self.hotkey}")
        if self.error is not None:
            raise self.error

    def stop(self) -> None:
        self._stop.set()
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
        self._thread.join(timeout=2)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.thread_id = kernel32.GetCurrentThreadId()
        modifiers = self.modifiers | self.MOD_NOREPEAT
        registered_modifiers = self._register_hotkey(user32, modifiers)
        if registered_modifiers is None:
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    try:
                        self.on_trigger()
                    except Exception:
                        LOGGER.exception("global hotkey callback failed")
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def _register_hotkey(self, user32: ctypes.WinDLL, modifiers: int) -> int | None:
        ctypes.set_last_error(0)
        if user32.RegisterHotKey(None, self.HOTKEY_ID, modifiers, self.key_code):
            return modifiers
        first_error = ctypes.get_last_error()
        fallback_modifiers = self.modifiers
        if modifiers != fallback_modifiers:
            ctypes.set_last_error(0)
            if user32.RegisterHotKey(None, self.HOTKEY_ID, fallback_modifiers, self.key_code):
                LOGGER.warning("registered global hotkey without MOD_NOREPEAT fallback: %s", self.hotkey)
                return fallback_modifiers
            fallback_error = ctypes.get_last_error()
        else:
            fallback_error = first_error
        self.error = HotkeyError(self._format_register_error(fallback_error or first_error))
        return None

    def _format_register_error(self, error_code: int) -> str:
        if error_code == self.ERROR_HOTKEY_ALREADY_REGISTERED:
            reason = "快捷键已被系统或其他程序占用"
        elif error_code == self.ERROR_ACCESS_DENIED:
            reason = "权限不足或目标会话不允许注册全局快捷键"
        elif error_code:
            reason = f"Windows 错误码 {error_code}"
        else:
            reason = "Windows 未返回明确错误码"
        return f"RegisterHotKey failed for {self.hotkey}: {reason}"


def parse_hotkey(hotkey: str) -> tuple[int, int]:
    modifiers, key_name, _canonical = _parse_hotkey(hotkey)

    if len(key_name) == 1 and ("A" <= key_name <= "Z" or "0" <= key_name <= "9"):
        key_code = ord(key_name)
    elif key_name.startswith("F") and key_name[1:].isdigit():
        number = int(key_name[1:])
        if not 1 <= number <= 24:
            raise HotkeyError(f"unsupported function key: {key_name}")
        key_code = 0x70 + number - 1
    else:
        key_code = _WindowsHotkeyLoop.KEY_CODES.get(key_name)
        if key_code is None:
            raise HotkeyError(f"unsupported hotkey key: {key_name}")

    return modifiers, key_code


def normalize_hotkey(hotkey: str) -> str:
    """Return the canonical cross-UI spelling for a supported global hotkey."""

    _modifiers, _key_name, canonical = _parse_hotkey(hotkey)
    return canonical


def _parse_hotkey(hotkey: str) -> tuple[int, str, str]:
    parts = [part.strip().upper() for part in hotkey.replace("-", "+").split("+") if part.strip()]
    if not parts:
        raise HotkeyError("hotkey is empty")

    modifiers = 0
    key_name = ""
    canonical_modifiers: set[str] = set()
    for part in parts:
        if part in {"CTRL", "CONTROL"}:
            modifiers |= _WindowsHotkeyLoop.MOD_CONTROL
            canonical_modifiers.add("Ctrl")
        elif part == "ALT":
            modifiers |= _WindowsHotkeyLoop.MOD_ALT
            canonical_modifiers.add("Alt")
        elif part == "SHIFT":
            modifiers |= _WindowsHotkeyLoop.MOD_SHIFT
            canonical_modifiers.add("Shift")
        elif part in {"WIN", "WINDOWS", "SUPER", "META"}:
            modifiers |= _WindowsHotkeyLoop.MOD_WIN
            canonical_modifiers.add("Win")
        else:
            if key_name:
                raise HotkeyError(f"hotkey has multiple keys: {hotkey}")
            key_name = part

    if not key_name:
        raise HotkeyError(f"hotkey missing key: {hotkey}")

    if len(key_name) == 1 and ("A" <= key_name <= "Z" or "0" <= key_name <= "9"):
        if not modifiers:
            raise HotkeyError("letter and number hotkeys require a modifier")
        canonical_key = key_name
    elif key_name.startswith("F") and key_name[1:].isdigit():
        number = int(key_name[1:])
        if not 1 <= number <= 24:
            raise HotkeyError(f"unsupported function key: {key_name}")
        canonical_key = f"F{number}"
    else:
        if key_name not in _WindowsHotkeyLoop.KEY_CODES:
            raise HotkeyError(f"unsupported hotkey key: {key_name}")
        canonical_key = "Esc" if key_name in {"ESC", "ESCAPE"} else key_name.title()

    modifier_order = ("Ctrl", "Alt", "Shift", "Win")
    canonical = "+".join([*(name for name in modifier_order if name in canonical_modifiers), canonical_key])
    return modifiers, key_name, canonical
