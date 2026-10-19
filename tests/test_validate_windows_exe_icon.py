from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_windows_exe_icon.py"
SPEC = importlib.util.spec_from_file_location("validate_windows_exe_icon", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def test_source_ico_frames_are_embedded_in_current_installer_when_available() -> None:
    assert len(VALIDATOR._ico_frame_digests(ROOT / "assets" / "icons" / "app.ico")) == 10
    executables = sorted(
        (ROOT / "dist" / "windows").glob(
            "VoiceInputAssistantSetup*/VoiceInputAssistant-v*-Setup.exe"
        )
    )
    if not executables:
        return
    for executable in executables:
        assert VALIDATOR.validate(ROOT / "assets" / "icons" / "app.ico", executable) == 10
