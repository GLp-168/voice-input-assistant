from __future__ import annotations

from pathlib import Path
import queue
import sys
import threading
import time
from types import ModuleType
import wave

import pytest

from voice_input.asr import ASREngineCancelled, ASRResult
from voice_input.funasr_streaming_process import FunASRPreparedStreamingRecognizer


def _install_fake_sounddevice(monkeypatch, order: list[str] | None = None) -> None:
    class CallbackStop(Exception):
        pass

    class FakeInputStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def __enter__(self):
            if order is not None:
                order.append("input_enter")
            try:
                self.callback(b"\0\0" * 100, 100, None, None)
            except CallbackStop:
                pass
            return self

        def __exit__(self, *_args):
            if order is not None:
                order.append("input_exit")
            return None

    fake_sounddevice = ModuleType("sounddevice")
    fake_sounddevice.CallbackStop = CallbackStop
    fake_sounddevice.InputStream = FakeInputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)


def test_funasr_streaming_process_relays_events_hotwords_and_review_interval(tmp_path, monkeypatch):
    order: list[str] = []
    _install_fake_sounddevice(monkeypatch, order)
    worker = tmp_path / "fake_funasr_worker.py"
    marker = tmp_path / "worker_args.txt"
    shutdown_marker = tmp_path / "online_released.txt"
    review_paths: list[Path] = []
    review_calls: list[dict[str, object]] = []
    review_closes: list[bool] = []

    class FakeReviewEngine:
        def __init__(self, **kwargs):
            review_calls.append(kwargs)

        def transcribe(self, audio_path, **kwargs):
            path = Path(audio_path)
            review_paths.append(path)
            assert shutdown_marker.exists()
            assert path.is_file()
            with wave.open(str(path), "rb") as source:
                assert source.getnframes() == 100
                assert source.readframes(100) == b"\0\0" * 100
            review_calls.append(kwargs)
            return ASRResult(
                text="复核完成",
                engine="funasr",
                segments=({"text": "复核完成", "start": 0.0, "end": 1.0},),
            )

        def close(self):
            review_closes.append(True)

    monkeypatch.setattr("voice_input.funasr_streaming_process.FunASROfflineEngine", FakeReviewEngine)
    worker.write_text(
        f"""
import json
import sys
from pathlib import Path

marker = Path({str(marker)!r})
shutdown_marker = Path({str(shutdown_marker)!r})
first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
assert first["streaming_model"] == "streaming"
assert first["offline_model"] == "offline"
assert first["speaker_model"] == "speaker"
assert first["num_threads"] == 3
assert first["review_num_threads"] == 7
print(json.dumps({{"type": "ready", "backend": "funasr"}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "start_external":
        marker.write_text(json.dumps(message, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({{"type": "event", "text": "开始", "partial_text": "开始"}}), flush=True)
    elif message.get("type") == "audio":
        pass
    elif message.get("type") == "stop":
        print(json.dumps({{
            "type": "final",
            "text": "完成",
            "segments": [{{"text": "完成", "start": 0.0, "end": 1.25, "spk": 0}}],
        }}), flush=True)
    elif message.get("type") == "shutdown":
        shutdown_marker.write_text("released", encoding="utf-8")
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.funasr_streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="offline",
        vad_model="vad",
        punc_model="punc",
        speaker_model="speaker",
        num_threads=3,
        review_num_threads=7,
        language="zh",
        review_interval_seconds=60,
    )
    levels = []
    microphone_states = []
    recognizer.set_audio_monitor(levels.append, microphone_states.append)
    events: queue.Queue[str] = queue.Queue()
    stop_event = threading.Event()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda event: events.put(event.text),
                hotwords=("政务服务", "会议纪要"),
            ),
        )
    )

    thread.start()

    assert events.get(timeout=3) == "开始"
    stop_event.set()
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()
    assert order[0] == "input_enter"
    assert [state.status for state in microphone_states] == ["opening", "active", "idle"]
    assert levels
    assert levels[-1].level == 0.0
    assert result["text"] == "复核完成"
    assert recognizer.last_segments == (
        {"text": "复核完成", "start": 0.0, "end": 1.0},
    )
    assert shutdown_marker.exists()
    assert review_paths and not review_paths[0].exists()
    assert review_calls[0]["model"] == "offline"
    assert review_calls[0]["num_threads"] == 7
    assert review_calls[1]["hotwords"] == ("政务服务", "会议纪要")
    assert review_closes == [True]
    start_message = marker.read_text(encoding="utf-8")
    assert '"hotwords": ["政务服务", "会议纪要"]' in start_message
    assert '"language": "zh"' in start_message
    assert '"review_interval_seconds": 60.0' in start_message


def test_funasr_streaming_finalizing_heartbeats_extend_idle_timeout(tmp_path, monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "heartbeat_funasr_worker.py"
    worker.write_text(
        """
import json
import sys
import time

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
print(json.dumps({"type": "ready", "backend": "funasr"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        for index in range(4):
            time.sleep(0.035)
            print(json.dumps({"type": "finalizing", "completed_reviews": index, "total_reviews": 4}), flush=True)
        print(json.dumps({"type": "final", "text": "completed"}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "voice_input.funasr_streaming_process._worker_command",
        lambda: [sys.executable, str(worker)],
    )
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="",
        vad_model="vad",
        punc_model="punc",
    )
    recognizer.final_wait_timeout_seconds = 0.05
    finalization_progress: list[tuple[int, int]] = []
    callback_thread_ids: list[int] = []

    def on_finalization_progress(completed: int, total: int) -> None:
        finalization_progress.append((completed, total))
        callback_thread_ids.append(threading.get_ident())

    recognizer.set_finalization_monitor(on_finalization_progress)
    stop_event = threading.Event()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda _event: None,
            ),
        )
    )

    thread.start()
    assert recognizer.wait_until_ready(timeout=3)
    stop_event.set()
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()
    assert result["text"] == "completed"
    assert recognizer.last_completion_status == "completed"
    assert finalization_progress == [(0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
    assert recognizer.last_finalization_progress == (4, 4)
    assert set(callback_thread_ids) == {thread.ident}


def test_funasr_streaming_opens_microphone_before_worker_ready(tmp_path, monkeypatch):
    order: list[str] = []
    _install_fake_sounddevice(monkeypatch, order)
    ready_gate = tmp_path / "ready_gate"
    worker = tmp_path / "delayed_funasr_worker.py"
    worker.write_text(
        f"""
import json
import sys
import time
from pathlib import Path

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
gate = Path({str(ready_gate)!r})
while not gate.exists():
    time.sleep(0.02)
print(json.dumps({{"type": "ready", "backend": "funasr"}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "start_external":
        print(json.dumps({{"type": "event", "text": "已连接", "partial_text": "已连接"}}), flush=True)
    elif message.get("type") == "stop":
        print(json.dumps({{"type": "final", "text": "完成"}}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.funasr_streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="",
        vad_model="vad",
        punc_model="punc",
    )
    stop_event = threading.Event()
    events: queue.Queue[str] = queue.Queue()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda event: events.put(event.text),
            ),
        )
    )

    thread.start()
    deadline = time.monotonic() + 3
    while "input_enter" not in order and time.monotonic() < deadline:
        time.sleep(0.02)

    assert order == ["input_enter"]
    assert not recognizer.is_ready()

    ready_gate.write_text("ready", encoding="utf-8")
    assert events.get(timeout=3) == "已连接"
    stop_event.set()
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()
    assert result["text"] == "完成"


def test_funasr_streaming_stop_before_ready_releases_microphone(tmp_path, monkeypatch):
    order: list[str] = []
    _install_fake_sounddevice(monkeypatch, order)
    ready_gate = tmp_path / "ready_gate"
    worker = tmp_path / "delayed_stop_funasr_worker.py"
    worker.write_text(
        f"""
import json
import sys
import time
from pathlib import Path

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
gate = Path({str(ready_gate)!r})
while not gate.exists():
    time.sleep(0.02)
print(json.dumps({{"type": "ready", "backend": "funasr"}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        print(json.dumps({{"type": "final", "text": "提前结束"}}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.funasr_streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="",
        vad_model="vad",
        punc_model="punc",
    )
    stop_event = threading.Event()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda _event: None,
            ),
        )
    )

    thread.start()
    deadline = time.monotonic() + 3
    while "input_enter" not in order and time.monotonic() < deadline:
        time.sleep(0.02)
    stop_event.set()
    deadline = time.monotonic() + 3
    while "input_exit" not in order and time.monotonic() < deadline:
        time.sleep(0.02)

    assert order == ["input_enter", "input_exit"]

    ready_gate.write_text("ready", encoding="utf-8")
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()
    assert result["text"] == "提前结束"


def test_high_quality_retains_idle_online_and_review_workers_without_duplicate_text(
    tmp_path,
    monkeypatch,
):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "retained_online_worker.py"
    online_final = tmp_path / "online_final.txt"
    review_paths: list[Path] = []
    review_closes: list[bool] = []

    class FakeReviewEngine:
        def __init__(self, **_kwargs):
            pass

        def transcribe(self, audio_path, **_kwargs):
            review_paths.append(Path(audio_path))
            assert online_final.exists()
            assert recognizer._process is not None
            assert recognizer._process.poll() is None
            return ASRResult(text="online", engine="funasr")

        def close(self):
            review_closes.append(True)

    worker.write_text(
        f"""
import json
import sys
from pathlib import Path

online_final = Path({str(online_final)!r})
json.loads(sys.stdin.readline())
print(json.dumps({{"type": "ready", "backend": "funasr"}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        online_final.write_text("done", encoding="utf-8")
        print(json.dumps({{
            "type": "final",
            "text": "online",
            "segments": [{{"text": "online", "start": 0.0, "end": 1.0}}],
        }}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "voice_input.funasr_streaming_process._worker_command",
        lambda: [sys.executable, str(worker)],
    )
    monkeypatch.setattr(
        "voice_input.funasr_streaming_process.FunASROfflineEngine",
        FakeReviewEngine,
    )
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="offline",
        vad_model="vad",
        punc_model="punc",
        release_online_before_review=False,
        retain_review_worker=True,
    )
    stop_event = threading.Event()
    result: dict[str, str] = {}
    final_events = []
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda event: final_events.append(event) if event.is_final else None,
            ),
        )
    )

    thread.start()
    assert recognizer.wait_until_ready(timeout=3)
    stop_event.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result["text"] == "online"
    assert [event.text for event in final_events] == ["online"]
    assert recognizer._process is not None and recognizer._process.poll() is None
    assert recognizer._review_engine is not None
    assert review_closes == []
    assert review_paths and not review_paths[0].exists()

    recognizer.close()
    assert review_closes == [True]


@pytest.mark.parametrize("review_mode", ["empty", "duplicate", "failure"])
def test_review_empty_duplicate_or_failure_never_corrupts_online_final(
    tmp_path,
    review_mode,
):
    class FakeReviewEngine:
        def transcribe(self, _audio_path, **_kwargs):
            if review_mode == "failure":
                raise RuntimeError("review failed")
            return ASRResult(
                text="" if review_mode == "empty" else "online",
                engine="funasr",
            )

        def close(self):
            pass

    audio_path = tmp_path / "review.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 100)
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="offline",
        vad_model="vad",
        punc_model="punc",
    )
    recognizer._review_engine = FakeReviewEngine()
    fallback_segments = ({"text": "online", "start": 0.0, "end": 1.0},)
    progress: list[tuple[int, int]] = []
    recognizer.set_finalization_monitor(lambda completed, total: progress.append((completed, total)))

    text, segments = recognizer._review_recording(
        audio_path,
        hotwords=(),
        fallback_text="online",
        fallback_segments=fallback_segments,
    )

    assert text == "online"
    assert segments == fallback_segments
    assert progress == [(0, 1), (1, 1)]
    if review_mode == "failure":
        assert recognizer.last_completion_status == "incomplete"


def test_corrected_review_without_segments_replaces_stale_online_segment(tmp_path):
    class FakeReviewEngine:
        def transcribe(self, _audio_path, **_kwargs):
            return ASRResult(
                text="复核文本",
                engine="funasr",
                duration_seconds=1.25,
            )

        def close(self):
            pass

    audio_path = tmp_path / "review.wav"
    with wave.open(str(audio_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 100)
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="offline",
        vad_model="vad",
        punc_model="punc",
    )
    recognizer._review_engine = FakeReviewEngine()

    text, segments = recognizer._review_recording(
        audio_path,
        hotwords=(),
        fallback_text="在线文本",
        fallback_segments=({"text": "在线文本", "start": 0.0, "end": 1.0},),
    )

    assert text == "复核文本"
    assert segments == ({"text": "复核文本", "start": 0.0, "end": 1.25},)


def test_online_worker_thread_budget_overrides_parent_environment(monkeypatch):
    captured: dict[str, object] = {}

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    monkeypatch.setenv("MKL_NUM_THREADS", "99")
    monkeypatch.setattr("voice_input.funasr_streaming_process.subprocess.Popen", fake_popen)
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="",
        vad_model="",
        punc_model="",
        num_threads=3,
    )

    recognizer._start_worker()

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OMP_NUM_THREADS"] == "3"
    assert env["MKL_NUM_THREADS"] == "3"


def test_shutdown_cancels_review_and_removes_temporary_audio(tmp_path, monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "shutdown_review_worker.py"
    review_started = threading.Event()
    review_paths: list[Path] = []
    review_closes: list[bool] = []

    class FakeReviewEngine:
        def __init__(self, **_kwargs):
            pass

        def transcribe(self, audio_path, **kwargs):
            review_paths.append(Path(audio_path))
            review_started.set()
            cancel_event = kwargs["cancel_event"]
            assert cancel_event.wait(timeout=3)
            raise ASREngineCancelled("cancelled")

        def close(self):
            review_closes.append(True)

    worker.write_text(
        """
import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({"type": "ready", "backend": "funasr"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        print(json.dumps({"type": "final", "text": "online"}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "voice_input.funasr_streaming_process._worker_command",
        lambda: [sys.executable, str(worker)],
    )
    monkeypatch.setattr(
        "voice_input.funasr_streaming_process.FunASROfflineEngine",
        FakeReviewEngine,
    )
    recognizer = FunASRPreparedStreamingRecognizer(
        streaming_model="streaming",
        offline_model="offline",
        vad_model="vad",
        punc_model="punc",
    )
    stop_event = threading.Event()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=threading.Event(),
                on_event=lambda _event: None,
            ),
        )
    )

    thread.start()
    assert recognizer.wait_until_ready(timeout=3)
    stop_event.set()
    assert review_started.wait(timeout=3)
    recognizer.close()
    recognizer.close()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result["text"] == "online"
    assert recognizer.last_completion_status == "incomplete"
    assert review_closes == [True]
    assert review_paths and not review_paths[0].exists()
