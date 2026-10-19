"""Small console launcher that reuses the packaged main runtime.

The package keeps the historical tool paths for shortcuts and diagnostics,
but the heavy Python/Torch/FunASR runtime lives only in VoiceInputAssistant.exe.
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


TOOL_FLAGS = {
    "voiceinputhealth.exe": "--voice-input-health",
    "voiceinputdiagnose.exe": "--voice-input-diagnose",
    "voiceinputevaluate.exe": "--voice-input-evaluate",
    "voiceinputstreamevaluate.exe": "--voice-input-stream-evaluate",
}


def _write_text(stream, text: str) -> None:
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8", errors="replace"))
        buffer.flush()
        return
    try:
        stream.write(text)
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", errors="backslashreplace").decode("ascii"))
    stream.flush()


def main() -> int:
    launcher = Path(sys.argv[0]).resolve()
    flag = TOOL_FLAGS.get(launcher.name.lower())
    if flag is None:
        print(f"Unknown VoiceInput tool launcher name: {launcher.name}", file=sys.stderr)
        return 2

    try:
        app_root = launcher.parents[2]
    except IndexError:
        print(f"Cannot locate VoiceInputAssistant.exe from {launcher}", file=sys.stderr)
        return 2

    app_exe = app_root / "VoiceInputAssistant.exe"
    if not app_exe.is_file():
        print(f"VoiceInputAssistant.exe not found: {app_exe}", file=sys.stderr)
        return 2

    command = [str(app_exe), flag, *sys.argv[1:]]
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            command,
            cwd=str(Path.cwd()),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        print(f"Failed to start VoiceInputAssistant.exe: {exc}", file=sys.stderr)
        return 2
    if completed.stderr:
        _write_text(sys.stderr, completed.stderr)
    if completed.stdout:
        _write_text(sys.stdout, completed.stdout)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
