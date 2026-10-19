"""Windows-specific helpers for hidden shell work and startup integration."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from typing import Sequence


def is_windows() -> bool:
    return sys.platform.startswith("win")


def desktop_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return home / "Desktop"


def start_menu_programs_dir(app_name: str) -> Path:
    appdata = Path(os.environ.get("APPDATA", str(Path.home())))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / app_name


def startup_run_key() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Run"


def uninstall_key_path(app_id: str) -> str:
    return rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{app_id}"


def run_hidden(
    command: Sequence[str],
    *,
    check: bool = True,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "check": check,
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if is_windows():
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = creationflags
    return subprocess.run(list(command), **kwargs)


def set_run_on_startup(
    app_name: str,
    target: Path | str,
    *,
    arguments: str = "",
) -> None:
    if not is_windows():
        return

    import winreg

    key_path = startup_run_key()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        command = f'"{target}"'
        if arguments:
            command += f" {arguments}"
        winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)


def clear_run_on_startup(app_name: str) -> None:
    if not is_windows():
        return

    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, startup_run_key()) as key:
            winreg.DeleteValue(key, app_name)
    except FileNotFoundError:
        return
    except OSError:
        return


def is_run_on_startup_enabled(app_name: str) -> bool:
    if not is_windows():
        return False

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup_run_key()) as key:
            winreg.QueryValueEx(key, app_name)
    except OSError:
        return False
    return True
