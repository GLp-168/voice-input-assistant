from __future__ import annotations

import sys
from types import ModuleType
import subprocess

from voice_input.diagnose_asr import main


def test_diagnose_asr_dummy_backend(capsys) -> None:
    assert main(["--backend", "dummy"]) == 0
    out = capsys.readouterr().out
    assert "engine=dummy" in out


def test_diagnose_asr_defaults_to_funasr(monkeypatch, capsys) -> None:
    class FakeAutoModel:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def generate(self, **kwargs):
            return [{"text": "ok"}]

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_offline_model", lambda: "models/funasr/offline")
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_vad_model", lambda: "models/funasr/vad")
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_punc_model", lambda: "models/funasr/punc")

    assert main([]) == 0
    assert "engine=funasr" in capsys.readouterr().out


def test_diagnose_asr_whisper_backend(monkeypatch, tmp_path) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"model")

    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr(
        "voice_input.asr.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout="hello", stderr=""),
    )

    assert main(["--backend", "whisper.cpp", "--model", str(model)]) == 0


def test_diagnose_asr_streaming_probe(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "voice_input.diagnose_asr.DummyASREngine.transcribe",
        lambda self, audio, **kwargs: type("Result", (), {"engine": "dummy", "language": "zh", "text": ""})(),
    )

    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_streaming_model", lambda: "models/funasr/streaming")
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_offline_model", lambda: "models/funasr/offline")
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_vad_model", lambda: "models/funasr/vad")
    monkeypatch.setattr("voice_input.diagnose_asr.bundled_funasr_punc_model", lambda: "models/funasr/punc")

    assert main(["--backend", "dummy", "--streaming"]) == 0
    out = capsys.readouterr().out

    assert "streaming_backend=funasr" in out
    assert "streaming_hotwords_supported=true" in out
    assert "streaming_vad_supported=true" in out
    assert "streaming_punctuation_supported=true" in out
    assert "streaming_ready=true" in out
    assert [call["model"] for call in calls] == ["models/funasr/streaming", "models/funasr/offline"]
