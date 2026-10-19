from __future__ import annotations

import io
import queue
import sys
import threading
import time
from types import ModuleType

import pytest

from voice_input.streaming import StreamingASRError
from voice_input.streaming_process import (
    PreparedSubprocessStreamingRecognizer,
    SubprocessStreamingRecognizer,
    _apply_worker_committed_update,
    _apply_worker_text_update,
    _read_text_lines,
)
from voice_input.streaming_worker import _text_patch


def test_sherpa_worker_thread_budget_overrides_parent_environment(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    monkeypatch.setenv("MKL_NUM_THREADS", "99")
    monkeypatch.setattr("voice_input.streaming_process.subprocess.Popen", fake_popen)
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path, num_threads=2)

    recognizer._start_worker()

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["MKL_NUM_THREADS"] == "2"


def test_worker_patch_protocol_transfers_only_changed_suffix() -> None:
    previous = "meeting notes " * 10_000
    current = f"{previous}next"
    message = {
        "text_patch": _text_patch(previous, current),
        "committed_append": "confirmed",
    }

    assert "text" not in message
    assert message["text_patch"]["text"] == "next"
    assert _apply_worker_text_update(previous, message) == current
    assert _apply_worker_committed_update("", message) == "confirmed"


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


def test_read_text_lines_drains_non_newline_stderr_chunks() -> None:
    stream = io.TextIOWrapper(
        io.BytesIO("loading runtime\rprogress 50%\rprogress 100%".encode("utf-8")),
        encoding="utf-8",
    )
    lines: list[str] = []

    _read_text_lines(stream, lines)

    assert lines == ["loading runtime", "progress 50%", "progress 100%"]


def test_subprocess_streaming_recognizer_relays_events_and_stop(tmp_path, monkeypatch):
    worker = tmp_path / "fake_streaming_worker.py"
    worker.write_text(
        """
import json
import sys

json.loads(sys.stdin.readline())
print(json.dumps({"type": "event", "text": "你", "partial_text": "你"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        print(json.dumps({"type": "final", "text": "你好"}), flush=True)
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = SubprocessStreamingRecognizer(tmp_path)
    stop_event = threading.Event()
    pause_event = threading.Event()
    events: queue.Queue[str] = queue.Queue()
    result: dict[str, str] = {}

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=pause_event,
                on_event=lambda event: events.put(event.text),
            ),
        )
    )
    thread.start()

    assert events.get(timeout=3) == "你"
    stop_event.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result["text"] == "你好"


def test_subprocess_streaming_recognizer_reports_worker_crash(tmp_path, monkeypatch):
    worker = tmp_path / "crashing_streaming_worker.py"
    worker.write_text(
        """
import sys
sys.stdin.readline()
sys.stderr.write("native crash simulated\\n")
sys.stderr.flush()
raise SystemExit(123)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = SubprocessStreamingRecognizer(tmp_path)

    with pytest.raises(StreamingASRError, match="退出码 123"):
        recognizer.transcribe_microphone(
            max_duration_seconds=10,
            stop_event=threading.Event(),
            pause_event=threading.Event(),
            on_event=lambda _event: None,
        )


def test_prepared_subprocess_streaming_recognizer_uses_ready_worker(tmp_path, monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "prepared_streaming_worker.py"
    worker.write_text(
        """
import json
import sys

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "start_external":
        print(json.dumps({"type": "event", "text": "开始", "partial_text": "开始"}), flush=True)
    elif message.get("type") == "stop":
        print(json.dumps({"type": "final", "text": "完成"}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path)
    recognizer.prepare()
    assert recognizer._ready.wait(timeout=3)
    assert recognizer.is_ready()

    stop_event = threading.Event()
    pause_event = threading.Event()
    events: queue.Queue[str] = queue.Queue()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.setdefault(
            "text",
            recognizer.transcribe_microphone(
                max_duration_seconds=10,
                stop_event=stop_event,
                pause_event=pause_event,
                on_event=lambda event: events.put(event.text),
            ),
        )
    )
    thread.start()

    assert events.get(timeout=3) == "开始"
    stop_event.set()
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()
    assert result["text"] == "完成"


def test_prepared_streaming_opens_microphone_before_worker_ready(tmp_path, monkeypatch):
    order: list[str] = []
    _install_fake_sounddevice(monkeypatch, order)
    ready_gate = tmp_path / "ready_gate"
    worker = tmp_path / "delayed_prepared_streaming_worker.py"
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
print(json.dumps({{"type": "ready"}}), flush=True)
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
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path)
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


def test_prepared_streaming_stop_before_ready_releases_microphone(tmp_path, monkeypatch):
    order: list[str] = []
    _install_fake_sounddevice(monkeypatch, order)
    ready_gate = tmp_path / "ready_gate"
    worker = tmp_path / "delayed_stop_prepared_streaming_worker.py"
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
print(json.dumps({{"type": "ready"}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "stop":
        print(json.dumps({{"type": "final", "text": "提前结束"}}), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path)
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


def test_prepared_streaming_decodes_ascii_escaped_chinese_events(tmp_path, monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "escaped_prepared_streaming_worker.py"
    worker.write_text(
        """
import json
import sys

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
print(json.dumps({"type": "ready"}, ensure_ascii=True), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "start_external":
        print(json.dumps({"type": "event", "text": "你好政务", "partial_text": "你好政务"}, ensure_ascii=True), flush=True)
    elif message.get("type") == "stop":
        print(json.dumps({"type": "final", "text": "你好政务"}, ensure_ascii=True), flush=True)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path)
    stop_event = threading.Event()
    events: queue.Queue[str] = queue.Queue()

    thread = threading.Thread(
        target=lambda: recognizer.transcribe_microphone(
            max_duration_seconds=10,
            stop_event=stop_event,
            pause_event=threading.Event(),
            on_event=lambda event: events.put(event.text),
        )
    )
    thread.start()

    assert events.get(timeout=3) == "你好政务"
    stop_event.set()
    thread.join(timeout=3)
    recognizer.close()

    assert not thread.is_alive()


def test_prepared_streaming_marks_preview_fallback_incomplete(tmp_path, monkeypatch):
    _install_fake_sounddevice(monkeypatch)
    worker = tmp_path / "incomplete_prepared_streaming_worker.py"
    worker.write_text(
        """
import json
import sys
import time

first = json.loads(sys.stdin.readline())
assert first["type"] == "prepare"
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "start_external":
        print(json.dumps({"type": "event", "text": "latest preview", "partial_text": "preview"}), flush=True)
    elif message.get("type") == "stop":
        time.sleep(30)
    elif message.get("type") == "shutdown":
        raise SystemExit(0)
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr("voice_input.streaming_process._worker_command", lambda: [sys.executable, str(worker)])
    recognizer = PreparedSubprocessStreamingRecognizer(tmp_path)
    recognizer.final_wait_timeout_seconds = 0.05
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
    assert events.get(timeout=3) == "latest preview"
    stop_event.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert result["text"] == "latest preview"
    assert recognizer.last_completion_status == "incomplete"
