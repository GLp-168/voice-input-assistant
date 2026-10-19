from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys

import pytest


if sys.platform != "win32":
    pytest.skip("Windows uninstaller tests require winreg", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_input_windows_uninstall",
    ROOT / "packaging" / "windows" / "uninstall.py",
)
assert SPEC is not None and SPEC.loader is not None
uninstall = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(uninstall)


def _installed_product(path: Path) -> Path:
    path.mkdir()
    (path / uninstall.APP_EXE_NAME).write_bytes(b"app")
    (path / "Uninstall.exe").write_bytes(b"uninstaller")
    (path / "assets").mkdir()
    (path / "assets" / "product.json").write_text("{}", encoding="utf-8")
    return path


def test_safe_install_dir_requires_all_product_markers(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / uninstall.APP_EXE_NAME).write_bytes(b"unrelated")

    assert uninstall._is_safe_install_dir(install_dir) is False
    (install_dir / "Uninstall.exe").write_bytes(b"uninstaller")
    (install_dir / "assets").mkdir()
    (install_dir / "assets" / "product.json").write_text("{}", encoding="utf-8")
    assert uninstall._is_safe_install_dir(install_dir) is True


def test_cleanup_rejects_direct_invocation_without_one_time_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = _installed_product(tmp_path / "install")
    errors: list[str] = []
    monkeypatch.setattr(uninstall, "_message_error", errors.append)

    result = uninstall._cleanup(
        argparse.Namespace(
            install_dir=str(install_dir),
            parent_pid=0,
            cleanup_token="forged",
        )
    )

    assert result == 1
    assert install_dir.exists()
    assert "无效或已过期" in errors[0]


def test_remove_shortcuts_only_uses_application_owned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    desktop = owned / "desktop.lnk"
    startup = owned / "startup.lnk"
    start_menu = owned / "start-menu"
    desktop.write_text("shortcut", encoding="utf-8")
    startup.write_text("shortcut", encoding="utf-8")
    start_menu.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(uninstall, "_desktop_shortcut", lambda _name: desktop)
    monkeypatch.setattr(uninstall, "_startup_shortcut", lambda _name: startup)
    monkeypatch.setattr(uninstall, "_default_start_menu_dir", lambda: start_menu)

    uninstall._remove_shortcuts()

    assert not desktop.exists()
    assert not startup.exists()
    assert not start_menu.exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"
