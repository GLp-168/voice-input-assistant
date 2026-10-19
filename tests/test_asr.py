from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from voice_input.asr import (
    ASREngineCancelled,
    ASREngineUnavailable,
    ASRResult,
    DummyASREngine,
    FunASROfflineEngine,
    SherpaOnnxEngine,
    WhisperCppEngine,
)
from voice_input.audio import AudioCaptureError, AudioChunk, MicrophoneRecorder, record_segment


def test_dummy_engine_transcribes_raw_pcm_bytes() -> None:
    engine = DummyASREngine()
    result = engine.transcribe(b"\x00" * 3200, sample_rate=16_000, language="zh")

    assert isinstance(result, ASRResult)
    assert result.engine == "dummy"
    assert result.language == "zh"
    assert result.confidence == 0.0
    assert "[ASR dummy] audio received" in result.text
    assert result.metadata["byte_count"] == 3200


def test_dummy_engine_uses_audio_chunk_duration() -> None:
    chunk = AudioChunk(pcm=b"\x00" * 32_000, sample_rate=16_000, channels=1, sample_width=2)

    result = DummyASREngine().transcribe(chunk)

    assert result.duration_seconds == 1.0
    assert "1.00s" in result.text


def test_funasr_offline_worker_thread_budget_overrides_parent_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdout = None
        stderr = None

        @staticmethod
        def poll():
            return None

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    monkeypatch.setenv("MKL_NUM_THREADS", "99")
    monkeypatch.setattr("voice_input.asr.subprocess.Popen", fake_popen)
    engine = FunASROfflineEngine(model="model", num_threads=3)

    engine._ensure_worker_unlocked()

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OMP_NUM_THREADS"] == "3"
    assert env["MKL_NUM_THREADS"] == "3"


def test_funasr_offline_engine_transcribes_with_hotwords(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    class FakeAutoModel:
        def __init__(self, **kwargs) -> None:
            calls.append({"init": kwargs})

        def generate(self, **kwargs):
            calls.append({"generate": kwargs})
            return [{"text": "会议纪要"}]

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    result = FunASROfflineEngine(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="campplus",
    ).transcribe(
        AudioChunk(pcm=b"\x00" * 3200),
        language="zh",
        hotwords=("会议纪要", "专项债"),
    )

    assert result.engine == "funasr"
    assert result.text == "会议纪要"
    assert calls[0]["init"]["model"] == "paraformer-zh"  # type: ignore[index]
    assert calls[0]["init"]["spk_model"] == "campplus"  # type: ignore[index]
    assert calls[1]["generate"]["hotword"] == "会议纪要 专项债"  # type: ignore[index]
    assert calls[1]["generate"]["language"] == "zh"  # type: ignore[index]


def test_funasr_offline_engine_rejects_missing_model_before_loading_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAutoModel:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("AutoModel should not be loaded without an offline model")

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FailingAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    with pytest.raises(ASREngineUnavailable, match="offline model is not configured"):
        FunASROfflineEngine(model="").transcribe(AudioChunk(pcm=b"\x00" * 3200))


def test_future_engines_report_clear_missing_model_errors(tmp_path: Path) -> None:
    whisper = WhisperCppEngine(model_path=tmp_path / "missing.bin")
    sherpa = SherpaOnnxEngine(model_dir=tmp_path / "missing-model")

    with pytest.raises(ASREngineUnavailable, match="model not found"):
        whisper.transcribe(b"")

    with pytest.raises(ASREngineUnavailable, match="model directory not found"):
        sherpa.transcribe(b"")


def test_whisper_cpp_engine_invokes_cli_and_parses_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"model")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        audio_path = Path(command[command.index("-f") + 1])
        assert audio_path.exists()
        assert audio_path.suffix == ".wav"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="[00:00:00.000 --> 00:00:01.000] 你好，测试\n",
            stderr="",
        )

    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr("voice_input.asr.subprocess.run", fake_run)

    result = WhisperCppEngine(model_path=model).transcribe(
        AudioChunk(pcm=b"\x00" * 3200),
        language="zh",
    )

    assert result.text == "你好，测试"
    assert result.engine == "whisper.cpp"
    assert result.language == "zh"
    assert calls[0][0][:4] == ["/usr/bin/whisper-cli", "-m", str(model), "-f"]
    assert calls[0][0][-2:] == ["-l", "zh"]


def test_whisper_cpp_engine_passes_existing_audio_file_to_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "ggml-tiny.bin"
    audio_path = tmp_path / "uploaded.wav"
    model.write_bytes(b"model")
    audio_path.write_bytes(b"audio")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="file text\n", stderr="")

    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr("voice_input.asr.subprocess.run", fake_run)

    result = WhisperCppEngine(model_path=model).transcribe(audio_path)

    assert result.text == "file text"
    assert Path(calls[0][0][calls[0][0].index("-f") + 1]) == audio_path


def test_whisper_cpp_engine_allows_unbounded_timeout_for_imported_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "ggml-tiny.bin"
    audio_path = tmp_path / "uploaded.wav"
    model.write_bytes(b"model")
    audio_path.write_bytes(b"audio")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="file text\n", stderr="")

    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr("voice_input.asr.subprocess.run", fake_run)

    WhisperCppEngine(model_path=model).transcribe(audio_path, timeout_seconds=None)

    assert calls[0][1]["timeout"] is None


def test_whisper_cpp_engine_cancels_running_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    model = tmp_path / "ggml-tiny.bin"
    audio_path = tmp_path / "uploaded.wav"
    model.write_bytes(b"model")
    audio_path.write_bytes(b"audio")
    cancel_event = threading.Event()

    class FakeProcess:
        returncode: int | None = None
        terminated = False

        def communicate(self, *, timeout: float):
            cancel_event.set()
            raise subprocess.TimeoutExpired(["whisper-cli"], timeout)

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def wait(self, *, timeout: float) -> int:
            assert timeout == 2
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            raise AssertionError("terminate should stop the fake process")

    process = FakeProcess()
    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr("voice_input.asr.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        "voice_input.asr.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cancellable recognition must use Popen")
        ),
    )

    with pytest.raises(ASREngineCancelled):
        WhisperCppEngine(model_path=model).transcribe(
            audio_path,
            timeout_seconds=None,
            cancel_event=cancel_event,
        )

    assert process.terminated is True


def test_funasr_offline_engine_honors_pre_cancelled_request() -> None:
    import threading

    cancel_event = threading.Event()
    cancel_event.set()
    engine = FunASROfflineEngine(model="model")

    with pytest.raises(ASREngineCancelled):
        engine.transcribe(b"audio", timeout_seconds=60, cancel_event=cancel_event)


def test_funasr_explicit_unbounded_timeout_still_uses_isolated_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FunASROfflineEngine(model="model")
    calls: list[tuple[float | None, object | None]] = []

    def fake_transcribe_isolated(
        _audio,
        *,
        sample_rate,
        language,
        timeout_seconds,
        cancel_event,
        **_options,
    ) -> ASRResult:
        calls.append((timeout_seconds, cancel_event))
        return ASRResult(text="ok", engine="funasr")

    monkeypatch.setattr(engine, "_transcribe_isolated", fake_transcribe_isolated)

    result = engine.transcribe(b"audio", timeout_seconds=None)

    assert result.text == "ok"
    assert calls == [(None, None)]


def test_funasr_offline_worker_timeout_stops_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    engine = FunASROfflineEngine(model="model")
    process = SimpleNamespace(poll=lambda: None)
    stopped: list[bool] = []
    monkeypatch.setattr(FunASROfflineEngine, "_ensure_worker_unlocked", lambda self: process)
    monkeypatch.setattr(FunASROfflineEngine, "_stop_worker_unlocked", lambda self: stopped.append(True))
    monkeypatch.setattr("voice_input.asr._write_worker_message", lambda _process, _message: None)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"wav")

    with pytest.raises(Exception, match="识别超时"):
        engine._request_worker(
            audio_path,
            language="zh",
            hotwords=(),
            timeout_seconds=0.01,
            cancel_event=None,
        )

    assert stopped == [True]


def test_funasr_file_input_is_read_and_recognized_in_bounded_chunks(tmp_path: Path) -> None:
    import wave

    class FakeModel:
        def __init__(self) -> None:
            self.sample_counts: list[int] = []

        def generate(self, *, input, **_kwargs):
            self.sample_counts.append(int(input.size))
            return [{"text": "片", "sentence_info": [{"text": "片", "start": 0, "end": 100}]}]

    audio_path = tmp_path / "long.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(1000)
        output.writeframes(b"\x00\x00" * 1000)

    model = FakeModel()
    engine = FunASROfflineEngine(model="model", file_chunk_seconds=0.25)
    engine._runtime = model
    result = engine._transcribe_local(audio_path, language="zh")

    assert result.text == "片片片片"
    assert model.sample_counts == [4000, 4000, 4000, 4000]
    assert result.duration_seconds == pytest.approx(1.0)
    assert [segment["start"] for segment in result.segments] == pytest.approx([0.0, 0.25, 0.5, 0.75])
    assert result.metadata["chunk_count"] == 4


def test_whisper_cpp_engine_can_auto_detect_language(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"model")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hello\n", stderr="")

    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr("voice_input.asr.subprocess.run", fake_run)

    result = WhisperCppEngine(model_path=model).transcribe(b"\x00" * 3200, language=None)

    assert result.text == "hello"
    assert result.language is None
    assert "-l" not in calls[0][0]


def test_whisper_cpp_engine_reports_missing_executable(tmp_path: Path) -> None:
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"model")

    with pytest.raises(ASREngineUnavailable, match="executable not found"):
        WhisperCppEngine(model_path=model, executable="missing-whisper-cli").transcribe(b"")


def test_whisper_cpp_engine_surfaces_cli_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = tmp_path / "ggml-tiny.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr("voice_input.asr.shutil.which", lambda executable: "/usr/bin/whisper-cli")
    monkeypatch.setattr(
        "voice_input.asr.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, stdout="", stderr="bad model"),
    )

    with pytest.raises(RuntimeError, match="bad model"):
        WhisperCppEngine(model_path=model).transcribe(b"\x00" * 3200)


def test_microphone_recorder_skips_chunks_while_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = MicrophoneRecorder(max_duration_seconds=0.2, sample_rate=1000)
    recorder.pause()

    class CallbackStop(Exception):
        pass

    class FakeBlock:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def copy(self):
            return self

        def tobytes(self) -> bytes:
            return self.payload

    class FakeInputStream:
        def __init__(self, **kwargs) -> None:
            self.callback = kwargs["callback"]

        def __enter__(self):
            self.callback(FakeBlock(b"paused"), 100, None, None)
            recorder.resume()
            try:
                self.callback(FakeBlock(b"recorded-1"), 100, None, None)
                self.callback(FakeBlock(b"recorded-2"), 100, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args) -> None:
            return None

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    chunk = recorder.record_until_stopped()

    assert chunk.pcm == b"recorded-1recorded-2"
    assert chunk.metadata["frames"] == 200


def test_record_segment_validates_arguments_before_optional_dependency() -> None:
    with pytest.raises(ValueError, match="duration_seconds"):
        record_segment(0)


def test_record_segment_has_clear_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(AudioCaptureError, match="sounddevice"):
        record_segment(0.01)
