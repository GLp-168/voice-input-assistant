"""Capture every installer wizard page for visual regression review."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "packaging" / "windows" / "setup_bootstrap.py"


def _load_bootstrap() -> object:
    spec = importlib.util.spec_from_file_location("voice_input_installer_preview", BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load installer module: {BOOTSTRAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture(output_dir: Path, *, width: int = 980, height: int = 680) -> tuple[Path, ...]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtTest

    bootstrap = _load_bootstrap()
    output_dir.mkdir(parents=True, exist_ok=True)
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(ROOT)))
    wizard = bootstrap.InstallerWizard(
        payload=ROOT / "missing-preview-payload.zip",
        default_install_dir=local_app_data / "Programs" / "VoiceInputAssistant",
        default_desktop_shortcut=True,
        default_start_on_boot=False,
        default_launch_after_install=True,
    )
    wizard.window.resize(width, height)
    captures: list[Path] = []

    def save(name: str) -> None:
        QtTest.QTest.qWait(280)
        target = output_dir / name
        if not wizard.window.grab().save(str(target)):
            raise RuntimeError(f"unable to save installer preview: {target}")
        captures.append(target)

    wizard.window.show()
    save("installer-options.png")
    wizard.is_installing = False
    wizard.progress_value = 88
    wizard.progress_label.setText("正在完成安装校验")
    wizard._render_progress_page()
    save("installer-progress.png")
    wizard._render_finish_page()
    save("installer-finish.png")
    wizard._render_error_page(
        "安装目录中的文件正在被占用。请退出正在运行的语音转文字应用后重试。\n"
        "错误代码：FILE_IN_USE\n"
        "位置：C:\\Users\\Example\\AppData\\Local\\Programs\\VoiceInputAssistant"
    )
    save("installer-error.png")
    wizard.window.close()
    wizard.qt_app.processEvents()
    return tuple(captures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "installer-visual-check",
    )
    parser.add_argument("--width", type=int, default=980)
    parser.add_argument("--height", type=int, default=680)
    args = parser.parse_args()
    for path in capture(args.output.resolve(), width=args.width, height=args.height):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
