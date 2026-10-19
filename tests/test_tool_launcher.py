from __future__ import annotations

import io
import importlib.util
from pathlib import Path
import subprocess
import sys


def _load_tool_launcher():
    path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "tool_launcher.py"
    spec = importlib.util.spec_from_file_location("tool_launcher_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_launcher_forwards_child_output(tmp_path, monkeypatch, capsys) -> None:
    module = _load_tool_launcher()
    monkeypatch.chdir(tmp_path)
    app_root = tmp_path / "VoiceInputAssistant"
    tool_dir = app_root / "tools" / "VoiceInputHealth"
    tool_dir.mkdir(parents=True)
    app_exe = app_root / "VoiceInputAssistant.exe"
    app_exe.write_bytes(b"exe")
    launcher = tool_dir / "VoiceInputHealth.exe"

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        return subprocess.CompletedProcess(command, 5, stdout="ok\n", stderr="warn\n")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [str(launcher), "--json"])

    assert module.main() == 5
    assert calls == [[str(app_exe), "--voice-input-health", "--json"]]

    captured = capsys.readouterr()
    assert captured.out == "ok\n"
    assert captured.err == "warn\n"


def test_tool_launcher_dispatches_streaming_evaluator(tmp_path, monkeypatch) -> None:
    module = _load_tool_launcher()
    monkeypatch.chdir(tmp_path)
    app_root = tmp_path / "VoiceInputAssistant"
    tool_dir = app_root / "tools" / "VoiceInputStreamEvaluate"
    tool_dir.mkdir(parents=True)
    app_exe = app_root / "VoiceInputAssistant.exe"
    app_exe.write_bytes(b"exe")
    launcher = tool_dir / "VoiceInputStreamEvaluate.exe"

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [str(launcher), "--json", "samples.csv"])

    assert module.main() == 0
    assert calls == [[str(app_exe), "--voice-input-stream-evaluate", "--json", "samples.csv"]]


def test_write_text_uses_utf8_for_redirected_windows_stream() -> None:
    module = _load_tool_launcher()
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp936")

    module._write_text(stream, "扬声器（Realtek® Audio）\n")

    assert raw.getvalue().decode("utf-8") == "扬声器（Realtek® Audio）\n"
