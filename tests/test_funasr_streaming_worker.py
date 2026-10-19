from __future__ import annotations

import base64
import io
import json
import queue
import sys
import time
from types import ModuleType

import numpy as np

from voice_input.funasr_streaming_worker import _FunASRRuntime, _run_prepared_worker


def _runtime(streaming_model, *, sample_rate: int = 16000) -> _FunASRRuntime:
    return _FunASRRuntime(
        streaming_model=streaming_model,
        streaming_model_name="streaming",
        sample_rate=sample_rate,
        update_interval_ms=40,
        device="cpu",
        num_threads=1,
    )


def _audio_message(sample_count: int, *, value: int = 1000) -> dict[str, object]:
    pcm = np.full(sample_count, value, dtype=np.int16).tobytes()
    return {
        "type": "audio",
        "pcm": base64.b64encode(pcm).decode("ascii"),
        "channels": 1,
    }


def test_funasr_runtime_loads_only_online_model(monkeypatch, capsys) -> None:
    instances = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            print(f"loading {kwargs['model']}")
            self.kwargs = kwargs
            instances.append(self)

    fake_funasr = ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    runtime = _FunASRRuntime.from_config(
        {
            "streaming_model": "streaming",
            "offline_model": "offline",
            "vad_model": "vad",
            "punc_model": "punc",
            "speaker_model": "speaker",
            "num_threads": 2,
            "review_num_threads": 6,
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "loading streaming" in captured.err
    assert runtime.streaming_model is instances[0]
    assert [instance.kwargs["model"] for instance in instances] == ["streaming"]
    assert instances[0].kwargs["ncpu"] == 2


def test_streaming_runtime_propagates_text_hotwords_and_online_segments(monkeypatch, capsys) -> None:
    emitted: list[dict[str, object]] = []

    class StreamingModel:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def generate(self, input, **kwargs):
            print("generate noisy line")
            self.calls.append(kwargs)
            return [{"text": "" if kwargs["is_final"] else "实时文本"}]

    model = StreamingModel()
    monkeypatch.setattr("voice_input.funasr_streaming_worker._emit", emitted.append)
    runtime = _runtime(model)
    controls: queue.Queue[dict[str, object]] = queue.Queue()
    controls.put(_audio_message(9600))
    controls.put({"type": "stop"})

    assert runtime.run_external_audio_session(
        {"hotwords": ["政务服务", "会议纪要"], "language": "zh"},
        controls,
    ) == "实时文本"

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "generate noisy line" in captured.err
    assert model.calls[0]["hotword"] == "政务服务 会议纪要"
    assert model.calls[0]["hotwords"] == ["政务服务", "会议纪要"]
    assert model.calls[0]["language"] == "zh"
    preview = next(message for message in emitted if message.get("is_final") is False)
    assert preview["text"] == "实时文本"
    assert preview["text_patch"] == {
        "start": 0,
        "delete_count": 0,
        "text": "实时文本",
    }
    final = next(message for message in emitted if message.get("is_final") is True)
    assert final["text"] == "实时文本"
    assert final["segments"] == [
        {
            "id": "online-1",
            "text": "实时文本",
            "start": 0.0,
            "end": 0.6,
            "time_unit": "seconds",
            "origin": "online",
            "revision": 1,
        }
    ]


def test_streaming_runtime_flushes_short_final_chunk(monkeypatch) -> None:
    emitted: list[dict[str, object]] = []

    class StreamingModel:
        def generate(self, input, **kwargs):
            assert kwargs["is_final"] is True
            assert int(input.size) == 800
            return [{"text": "尾字"}]

    monkeypatch.setattr("voice_input.funasr_streaming_worker._emit", emitted.append)
    runtime = _runtime(StreamingModel())
    controls: queue.Queue[dict[str, object]] = queue.Queue()
    controls.put(_audio_message(800))
    controls.put({"type": "stop"})

    assert runtime.run_external_audio_session({}, controls) == "尾字"
    assert runtime.last_segments[0]["end"] == 0.05
    assert emitted[-1]["is_final"] is True


def test_prepared_worker_starts_control_reader_after_model_load(monkeypatch, capsys) -> None:
    order: list[str] = []

    class FakeRuntime:
        streaming_model_name = "streaming"

    def fake_from_config(_cls, _config):
        order.append("runtime")
        time.sleep(0.03)
        return FakeRuntime()

    def fake_read_controls(control_queue):
        order.append("reader")
        control_queue.put({"type": "shutdown"})

    protocol_stdout = io.StringIO()
    monkeypatch.setattr("voice_input.funasr_streaming_worker._PROTOCOL_STDOUT", protocol_stdout)
    monkeypatch.setattr(_FunASRRuntime, "from_config", classmethod(fake_from_config))
    monkeypatch.setattr("voice_input.funasr_streaming_worker._read_queued_controls", fake_read_controls)

    assert _run_prepared_worker({"type": "prepare", "offline_model": "offline"}) == 0

    capsys.readouterr()
    ready = json.loads(protocol_stdout.getvalue().splitlines()[0])
    assert ready["type"] == "ready"
    assert ready["offline_model"] == "offline"
    assert order == ["runtime", "reader"]


def test_prepared_worker_final_message_includes_online_segments(monkeypatch) -> None:
    class FakeRuntime:
        streaming_model_name = "streaming"
        last_segments = [{"text": "第一段", "start": 0.0, "end": 1.0, "origin": "online"}]

        def run_external_audio_session(self, _message, _control_queue):
            return "第一段"

    def fake_from_config(_cls, _config):
        return FakeRuntime()

    def fake_read_controls(control_queue):
        control_queue.put({"type": "start_external"})
        control_queue.put({"type": "shutdown"})

    protocol_stdout = io.StringIO()
    monkeypatch.setattr("voice_input.funasr_streaming_worker._PROTOCOL_STDOUT", protocol_stdout)
    monkeypatch.setattr(_FunASRRuntime, "from_config", classmethod(fake_from_config))
    monkeypatch.setattr("voice_input.funasr_streaming_worker._read_queued_controls", fake_read_controls)

    assert _run_prepared_worker({"type": "prepare"}) == 0

    messages = [json.loads(line) for line in protocol_stdout.getvalue().splitlines()]
    final = next(message for message in messages if message["type"] == "final")
    assert final == {
        "type": "final",
        "text": "第一段",
        "segments": [{"text": "第一段", "start": 0.0, "end": 1.0, "origin": "online"}],
    }
