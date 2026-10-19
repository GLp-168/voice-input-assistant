"""Windowed uninstaller entry point for the Windows package."""

from __future__ import annotations

import argparse
import ctypes
import hmac
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import winreg


PRODUCT_NAME = "语音转文字"
APP_EXE_NAME = "VoiceInputAssistant.exe"
RUN_VALUE_NAME = "VoiceInputAssistant"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceInputAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_PRODUCT_NAMES = ("离线语音输入助手",)
CLEANUP_TOKEN_NAME = "cleanup.token"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Uninstall {PRODUCT_NAME}.")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--install-dir", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--cleanup-token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.cleanup:
        return _cleanup(args)
    return _start_cleanup(args)


def _start_cleanup(args: argparse.Namespace) -> int:
    install_dir = _resolve_install_dir(args.install_dir)
    if not _is_safe_install_dir(install_dir):
        _message_error(f"安装目录异常，已取消卸载：\n{install_dir}")
        return 1

    if _is_app_running():
        _message_info(f"请先退出正在运行的 {PRODUCT_NAME}，再卸载。")
        return 1

    if not _confirm(f"确定要卸载 {PRODUCT_NAME} 吗？"):
        return 0

    cleanup_exe, cleanup_token = _copy_self_to_temp()
    command = [
        str(cleanup_exe),
        "--cleanup",
        "--install-dir",
        str(install_dir),
        "--parent-pid",
        str(os.getpid()),
        "--cleanup-token",
        cleanup_token,
    ]
    subprocess.Popen(
        command,
        cwd=str(cleanup_exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=_detached_flags(),
    )
    return 0


def _cleanup(args: argparse.Namespace) -> int:
    install_dir = _resolve_install_dir(args.install_dir)
    try:
        if not _consume_cleanup_token(str(args.cleanup_token)):
            raise RuntimeError("卸载清理请求无效或已过期。")
        _wait_for_parent(args.parent_pid)
        if _is_app_running():
            _message_info(f"请先退出正在运行的 {PRODUCT_NAME}，再卸载。")
            return 1
        if not _is_safe_install_dir(install_dir):
            raise RuntimeError(f"安装目录异常，已取消卸载：{install_dir}")

        _clear_startup()
        _remove_uninstall_registry()
        _remove_shortcuts()
        _remove_install_dir(install_dir)
    except Exception as exc:
        _message_error(f"卸载失败：\n{exc}")
        return 1

    _message_info(f"{PRODUCT_NAME} 已卸载。")
    return 0


def _resolve_install_dir(value: str) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(sys.executable).resolve().parent


def _copy_self_to_temp() -> tuple[Path, str]:
    temp_dir = Path(tempfile.mkdtemp(prefix="VoiceInputAssistantUninstall-"))
    target = temp_dir / "UninstallCleanup.exe"
    shutil.copy2(Path(sys.executable), target)
    cleanup_token = secrets.token_urlsafe(32)
    (temp_dir / CLEANUP_TOKEN_NAME).write_text(cleanup_token, encoding="ascii")
    return target, cleanup_token


def _consume_cleanup_token(provided_token: str) -> bool:
    token_path = Path(sys.executable).resolve().with_name(CLEANUP_TOKEN_NAME)
    try:
        expected_token = token_path.read_text(encoding="ascii").strip()
        token_path.unlink()
    except (OSError, UnicodeError):
        return False
    return bool(provided_token) and hmac.compare_digest(provided_token, expected_token)


def _wait_for_parent(pid: int) -> None:
    if pid <= 0:
        time.sleep(0.5)
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if handle:
            kernel32.WaitForSingleObject(handle, 15_000)
            kernel32.CloseHandle(handle)
            return
    except Exception:
        pass
    time.sleep(1.0)


def _is_app_running() -> bool:
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {APP_EXE_NAME}", "/NH"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="mbcs",
            errors="ignore",
            creationflags=_hidden_flags(),
        )
    except Exception:
        return False
    return APP_EXE_NAME.lower() in completed.stdout.lower()


def _is_safe_install_dir(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    if not resolved.exists() or not resolved.is_dir():
        return False
    if str(resolved) == resolved.anchor:
        return False

    blocked = {
        Path.home().resolve(),
        Path(os.environ.get("USERPROFILE", str(Path.home()))).resolve(),
        (Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop").resolve(),
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))).resolve(),
        (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs").resolve(),
        Path(os.environ.get("APPDATA", str(Path.home()))).resolve(),
    }
    if resolved in blocked:
        return False

    return (
        (resolved / APP_EXE_NAME).is_file()
        and (resolved / "Uninstall.exe").is_file()
        and (resolved / "assets" / "product.json").is_file()
    )


def _clear_startup() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_uninstall_registry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_shortcuts() -> None:
    _remove_path(_desktop_shortcut(PRODUCT_NAME))
    _remove_path(_startup_shortcut(PRODUCT_NAME))
    _remove_path(_default_start_menu_dir())
    for legacy_name in LEGACY_PRODUCT_NAMES:
        _remove_path(_desktop_shortcut(legacy_name))
        _remove_path(_startup_shortcut(legacy_name))
        _remove_path(_start_menu_dir(legacy_name))


def _remove_install_dir(install_dir: Path) -> None:
    last_error: Exception | None = None
    for _attempt in range(40):
        try:
            shutil.rmtree(install_dir)
            return
        except FileNotFoundError:
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error is not None:
        raise last_error


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _default_start_menu_dir() -> Path:
    return _start_menu_dir(PRODUCT_NAME)


def _start_menu_dir(product_name: str) -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / product_name
    )


def _desktop_shortcut(product_name: str) -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / f"{product_name}.lnk"


def _startup_shortcut(product_name: str) -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{product_name}.lnk"


def _message_info(message: str) -> None:
    _message_box(message, 0x40)


def _message_error(message: str) -> None:
    _message_box(message, 0x10)


def _confirm(message: str) -> bool:
    try:
        result = ctypes.windll.user32.MessageBoxW(None, message, PRODUCT_NAME, 0x24)
        return result == 6
    except Exception:
        return True


def _message_box(message: str, flags: int) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, PRODUCT_NAME, flags)
    except Exception:
        print(message)


def _hidden_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _detached_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)


if __name__ == "__main__":
    raise SystemExit(main())
