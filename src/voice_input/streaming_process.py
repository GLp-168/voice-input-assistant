"""Process-isolated streaming ASR runner.

The sherpa/sounddevice stack uses native code. Running it in a child process
keeps the Qt shell alive if the local audio host or model runtime aborts.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .audio import (
    AudioCaptureError,
    AudioLevel,
    AudioLevelCallback,
    AudioLevelMeter,
    MicrophoneState,
    MicrophoneStateCallback,
    describe_microphone,
)
from .config import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE
from .streaming import StreamingASRError, StreamingRecognitionEvent


class SubprocessStreamingRecognizer:
    """Streaming recognizer facade backed by a worker subprocess."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        chunk_ms: int = 100,
        update_interval_ms: int = 120,
        num_threads: int = 2,
        device: int | str | None = None,
        decoding_method: str = "modified_beam_search",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk_ms = max(40, int(chunk_ms))
        self.update_interval_ms = max(40, int(update_interval_ms))
        self.num_threads = max(1, int(num_threads))
        self.device = device
        self.decoding_method = str(decoding_method or "modified_beam_search")
        self._audio_level_callback: AudioLevelCallback | None = None
        self._microphone_state_callback: MicrophoneStateCallback | None = None

    def set_audio_monitor(
        self,
        on_level: AudioLevelCallback | None,
        on_state: MicrophoneStateCallback | None = None,
    ) -> None:
        self._audio_level_callback = on_level
        self._microphone_state_callback = on_state

    def transcribe_microphone(
        self,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event: Callable[[StreamingRecognitionEvent], None],
        hotwords: Iterable[str] = (),
    ) -> str:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be greater than 0")

        process = self._start_worker()
        output: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_lines: list[str] = []
        stdout_reader = threading.Thread(
            target=_read_json_lines,
            args=(process.stdout, output),
            name="voice-input-streaming-worker-stdout",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=_read_text_lines,
            args=(process.stderr, stderr_lines),
            name="voice-input-streaming-worker-stderr",
            daemon=True,
        )
        stdout_reader.start()
        stderr_reader.start()

        start_message = {
            "type": "start",
            "model_dir": str(self.model_dir),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "chunk_ms": self.chunk_ms,
            "update_interval_ms": self.update_interval_ms,
            "num_threads": self.num_threads,
            "device": self.device,
            "decoding_method": self.decoding_method,
            "max_duration_seconds": float(max_duration_seconds),
            "hotwords": list(hotwords),
        }
        _write_worker_message(process, start_message)

        stop_sent = False
        last_pause_state = pause_event.is_set()
        if last_pause_state:
            _write_worker_message(process, {"type": "pause"})
        final_text = ""
        final_seen = False
        error_message: str | None = None
        latest_text = ""
        latest_committed_text = ""

        try:
            while True:
                if stop_event.is_set() and not stop_sent:
                    _write_worker_message(process, {"type": "stop"})
                    stop_sent = True

                pause_state = pause_event.is_set()
                if pause_state != last_pause_state:
                    _write_worker_message(process, {"type": "pause" if pause_state else "resume"})
                    last_pause_state = pause_state

                while True:
                    try:
                        message = output.get_nowait()
                    except queue.Empty:
                        break
                    kind = str(message.get("type", ""))
                    if kind == "event":
                        latest_text = _apply_worker_text_update(latest_text, message)
                        latest_committed_text = _apply_worker_committed_update(
                            latest_committed_text,
                            message,
                        )
                        on_event(
                            StreamingRecognitionEvent(
                                text=latest_text,
                                partial_text=str(message.get("partial_text", "")),
                                committed_text=latest_committed_text,
                                is_final=bool(message.get("is_final", False)),
                                is_endpoint=bool(message.get("is_endpoint", False)),
                            )
                        )
                    elif kind == "final":
                        final_text = str(message.get("text", ""))
                        final_seen = True
                    elif kind == "error":
                        error_message = str(message.get("message", "流式识别子进程失败"))

                return_code = process.poll()
                if return_code is not None:
                    while True:
                        try:
                            message = output.get_nowait()
                        except queue.Empty:
                            break
                        if message.get("type") == "final":
                            final_text = str(message.get("text", ""))
                            final_seen = True
                        elif message.get("type") == "error":
                            error_message = str(message.get("message", "流式识别子进程失败"))
                    if error_message:
                        raise StreamingASRError(error_message)
                    if return_code == 0 or final_seen:
                        return final_text
                    raise StreamingASRError(_format_worker_failure(return_code, stderr_lines))

                if error_message:
                    raise StreamingASRError(error_message)
                time.sleep(0.03)
        finally:
            if process.poll() is None:
                if not stop_sent:
                    _write_worker_message(process, {"type": "stop"})
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    def _start_worker(self) -> subprocess.Popen[str]:
        creationflags = 0
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env["PYTHONIOENCODING"] = "utf-8"
        env["OMP_NUM_THREADS"] = str(self.num_threads)
        env["MKL_NUM_THREADS"] = str(self.num_threads)
        return subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
            env=env,
        )


class PreparedSubprocessStreamingRecognizer(SubprocessStreamingRecognizer):
    """Reusable worker that preloads sherpa before recording starts."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.final_wait_timeout_seconds = 15.0
        self.last_completion_status = "idle"
        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._ready = threading.Event()
        self._failed_message: str | None = None
        self._prepare_started = False
        self._lock = threading.Lock()

    def prepare(self) -> None:
        with self._lock:
            if self._prepare_started and self._process is not None and self._process.poll() is None:
                return
            self.close()
            self._process = self._start_worker()
            self._output = queue.Queue()
            self._stderr_lines = []
            self._ready.clear()
            self._failed_message = None
            self._prepare_started = True
            threading.Thread(
                target=_read_json_lines,
                args=(self._process.stdout, self._output),
                name="voice-input-prepared-streaming-stdout",
                daemon=True,
            ).start()
            threading.Thread(
                target=_read_text_lines,
                args=(self._process.stderr, self._stderr_lines),
                name="voice-input-prepared-streaming-stderr",
                daemon=True,
            ).start()
            _write_worker_message(self._process, self._prepare_message())
            threading.Thread(
                target=self._wait_until_ready,
                name="voice-input-prepared-streaming-ready",
                daemon=True,
            ).start()

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._failed_message is None and self._process is not None and self._process.poll() is None

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout=timeout) and self.is_ready()

    def close(self) -> None:
        process = self._process
        self._process = None
        self._prepare_started = False
        self._ready.clear()
        if process is None:
            return
        if process.poll() is None:
            _write_worker_message(process, {"type": "shutdown"})
            _terminate_process(process, graceful_timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def transcribe_microphone(
        self,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event: Callable[[StreamingRecognitionEvent], None],
        hotwords: Iterable[str] = (),
    ) -> str:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be greater than 0")

        self.last_completion_status = "running"
        self.prepare()
        capture = _ParentMicrophoneCapture(
            sample_rate=self.sample_rate,
            channels=self.channels,
            chunk_ms=self.chunk_ms,
            device=self.device,
            on_level=self._audio_level_callback,
            on_state=self._microphone_state_callback,
        )
        capture.start(
            stop_event=stop_event,
            pause_event=pause_event,
            max_duration_seconds=max_duration_seconds,
        )

        process: subprocess.Popen[str] | None = None
        session_started = False
        stop_sent = False
        final_text = ""
        final_seen = False
        error_message: str | None = None
        latest_text = ""
        latest_committed_text = ""
        stop_sent_at: float | None = None

        try:
            ready_deadline = time.monotonic() + 45
            while not self._ready.is_set():
                if stop_event.is_set() and not capture.done_event.is_set():
                    capture.close()
                capture.raise_error_if_any()
                process = self._process
                return_code = None if process is None else process.poll()
                if return_code is not None:
                    raise StreamingASRError(_format_worker_failure(return_code, self._stderr_lines))
                if time.monotonic() >= ready_deadline:
                    raise StreamingASRError("流式识别引擎仍在启动，请稍后再试。")
                time.sleep(0.03)

            capture.raise_error_if_any()
            if self._failed_message is not None:
                raise StreamingASRError(self._failed_message)
            process = self._process
            if process is None or process.poll() is not None:
                raise StreamingASRError(_format_worker_failure(-1, self._stderr_lines))

            _write_worker_message(
                process,
                {
                    "type": "start_external",
                    "max_duration_seconds": float(max_duration_seconds),
                    "hotwords": list(hotwords),
                },
            )
            session_started = True

            while True:
                _drain_capture_blocks(process, capture)

                if (stop_event.is_set() or capture.done_event.is_set()) and not stop_sent:
                    capture.close()
                    _drain_capture_blocks(process, capture)
                    _write_worker_message(process, {"type": "stop"})
                    stop_sent = True
                    stop_sent_at = time.monotonic()

                while True:
                    try:
                        message = self._output.get_nowait()
                    except queue.Empty:
                        break
                    kind = str(message.get("type", ""))
                    if kind == "event":
                        latest_text = _apply_worker_text_update(latest_text, message)
                        latest_committed_text = _apply_worker_committed_update(
                            latest_committed_text,
                            message,
                        )
                        on_event(
                            StreamingRecognitionEvent(
                                text=latest_text,
                                partial_text=str(message.get("partial_text", "")),
                                committed_text=latest_committed_text,
                                is_final=bool(message.get("is_final", False)),
                                is_endpoint=bool(message.get("is_endpoint", False)),
                            )
                        )
                    elif kind == "final":
                        final_text = str(message.get("text", ""))
                        final_seen = True
                    elif kind == "error":
                        error_message = str(message.get("message", "流式识别子进程失败"))

                return_code = process.poll()
                if error_message:
                    raise StreamingASRError(error_message)
                if final_seen:
                    self.last_completion_status = "completed"
                    return final_text
                if return_code is not None:
                    raise StreamingASRError(_format_worker_failure(return_code, self._stderr_lines))
                if (
                    stop_sent_at is not None
                    and time.monotonic() - stop_sent_at > self.final_wait_timeout_seconds
                ):
                    self._terminate_current_worker()
                    self.last_completion_status = "incomplete"
                    return latest_text
                if not stop_sent:
                    capture.raise_error_if_any()
                time.sleep(0.03)
        except Exception:
            self.last_completion_status = "failed"
            raise
        finally:
            capture.close()
            if session_started and process is not None and process.poll() is None and not stop_sent:
                _drain_capture_blocks(process, capture)
                _write_worker_message(process, {"type": "stop"})

    def _prepare_message(self) -> dict[str, Any]:
        return {
            "type": "prepare",
            "model_dir": str(self.model_dir),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "chunk_ms": self.chunk_ms,
            "update_interval_ms": self.update_interval_ms,
            "num_threads": self.num_threads,
            "device": self.device,
            "decoding_method": self.decoding_method,
        }

    def _wait_until_ready(self) -> None:
        process = self._process
        while process is not None:
            try:
                message = self._output.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None:
                    self._failed_message = _format_worker_failure(process.returncode or -1, self._stderr_lines)
                    self._ready.set()
                    return
                continue
            kind = str(message.get("type", ""))
            if kind == "ready":
                self._ready.set()
                return
            if kind == "error":
                self._failed_message = str(message.get("message", "流式识别子进程失败"))
                self._ready.set()
                return

    def _terminate_current_worker(self) -> None:
        process = self._process
        self._process = None
        self._prepare_started = False
        self._ready.clear()
        if process is None:
            return
        _terminate_process(process)


class _ParentMicrophoneCapture:
    """Capture microphone PCM in the parent process and queue it for the worker."""

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        chunk_ms: int,
        device: int | str | None,
        on_level: AudioLevelCallback | None = None,
        on_state: MicrophoneStateCallback | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk_ms = int(chunk_ms)
        self.device = device
        self.blocksize = max(320, int(self.sample_rate * self.chunk_ms / 1000))
        self.blocks: queue.Queue[tuple[bytes, int]] = queue.Queue(maxsize=4096)
        self.errors: queue.Queue[Exception] = queue.Queue()
        self.done_event = threading.Event()
        self._captured_frames = 0
        self._max_frames = 0
        self._stream: Any | None = None
        self._sd: Any | None = None
        self._audio_level_callback = on_level
        self._microphone_state_callback = on_state
        self._level_meter = AudioLevelMeter()
        self._device_state = MicrophoneState(device=device)
        self._capture_failed = False

    def start(
        self,
        *,
        stop_event: threading.Event,
        pause_event: threading.Event,
        max_duration_seconds: float,
    ) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise AudioCaptureError(
                "audio capture requires the optional dependency 'sounddevice'. "
                "Install it and ensure PortAudio/audio drivers are available."
            ) from exc

        self._sd = sd
        self._max_frames = int(round(max_duration_seconds * self.sample_rate))
        self._level_meter.reset()
        self._capture_failed = False
        self._device_state = describe_microphone(sd, self.device, status="opening")
        self._notify_state(self._device_state)

        def callback(indata: object, frame_count: int, _time: object, status: object) -> None:
            if status:
                error = RuntimeError(str(status))
                self.errors.put(error)
                self._report_error(str(error))
            if stop_event.is_set():
                self.done_event.set()
                raise sd.CallbackStop()
            if pause_event.is_set():
                return

            remaining = self._max_frames - self._captured_frames
            if remaining <= 0:
                stop_event.set()
                self.done_event.set()
                raise sd.CallbackStop()

            usable_frames = min(int(frame_count), remaining)
            payload = _pcm_bytes_from_callback_input(indata, usable_frames, int(frame_count))
            if payload:
                if self._audio_level_callback is not None:
                    try:
                        level = self._level_meter.process(payload, dtype="int16")
                    except (TypeError, ValueError):
                        level = None
                    if level is not None:
                        self._notify_level(level)
                try:
                    self.blocks.put_nowait((payload, usable_frames))
                except queue.Full:
                    error = AudioCaptureError("microphone audio queue is full")
                    self.errors.put(error)
                    self._report_error(str(error))
                    stop_event.set()
                    self.done_event.set()
                    raise sd.CallbackStop()
            self._captured_frames += usable_frames
            if self._captured_frames >= self._max_frames:
                stop_event.set()
                self.done_event.set()
                raise sd.CallbackStop()

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
                blocksize=self.blocksize,
                callback=callback,
            )
            self._stream.__enter__()
            self._notify_state(
                MicrophoneState(
                    status="active",
                    device=self._device_state.device,
                    name=self._device_state.name,
                )
            )
        except AudioCaptureError:
            raise
        except Exception as exc:  # pragma: no cover - depends on local audio host
            self._report_error(str(exc))
            raise AudioCaptureError(f"failed to start microphone audio: {exc}") from exc

    def close(self) -> None:
        self.done_event.set()
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover - depends on local audio host
            self.errors.put(AudioCaptureError(f"failed to close microphone audio: {exc}"))
            self._report_error(str(exc))
        self._notify_level(
            AudioLevel(level=0.0, rms=0.0, peak=0.0, rms_dbfs=-96.0, peak_dbfs=-96.0)
        )
        if not self._capture_failed:
            self._notify_state(
                MicrophoneState(
                    status="idle",
                    device=self._device_state.device,
                    name=self._device_state.name,
                )
            )

    def raise_error_if_any(self) -> None:
        try:
            error = self.errors.get_nowait()
        except queue.Empty:
            return
        if isinstance(error, AudioCaptureError):
            raise error
        raise AudioCaptureError(f"failed to stream microphone audio: {error}") from error

    def _notify_level(self, level: AudioLevel) -> None:
        callback = self._audio_level_callback
        if callback is None:
            return
        try:
            callback(level)
        except Exception:
            return

    def _notify_state(self, state: MicrophoneState) -> None:
        callback = self._microphone_state_callback
        if callback is None:
            return
        try:
            callback(state)
        except Exception:
            return

    def _report_error(self, message: str) -> None:
        self._capture_failed = True
        self._notify_state(
            MicrophoneState(
                status="error",
                device=self._device_state.device,
                name=self._device_state.name,
                message=message,
            )
        )


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--streaming-worker"]
    return [sys.executable, "-m", "voice_input.streaming_worker"]


def _pcm_bytes_from_callback_input(indata: object, usable_frames: int, frame_count: int) -> bytes:
    block = indata
    if usable_frames < frame_count:
        try:
            block = indata[:usable_frames]  # type: ignore[index]
        except (TypeError, AttributeError):
            block = indata
    try:
        return block.copy().tobytes()  # type: ignore[attr-defined]
    except AttributeError:
        return bytes(block)


def _drain_capture_blocks(
    process: subprocess.Popen[str],
    capture: _ParentMicrophoneCapture,
    *,
    on_block: Callable[[bytes, int], None] | None = None,
) -> None:
    while process.poll() is None:
        try:
            pcm, frames = capture.blocks.get_nowait()
        except queue.Empty:
            return
        if on_block is not None:
            on_block(pcm, frames)
        _write_worker_message(
            process,
            {
                "type": "audio",
                "pcm": base64.b64encode(pcm).decode("ascii"),
                "frames": frames,
                "sample_rate": capture.sample_rate,
                "channels": capture.channels,
            },
        )


def _terminate_process(process: subprocess.Popen[str], *, graceful_timeout: float = 0.5) -> None:
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=graceful_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        process.kill()


def _write_worker_message(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if process.stdin is None or process.poll() is not None:
        return
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        return


def _apply_worker_text_update(current: str, message: dict[str, Any]) -> str:
    patch = message.get("text_patch")
    if not isinstance(patch, dict):
        return str(message.get("text", ""))
    try:
        start = int(patch.get("start", 0))
        delete_count = int(patch.get("delete_count", 0))
    except (TypeError, ValueError):
        return str(message.get("text", current))
    if start < 0 or delete_count < 0 or start > len(current):
        return str(message.get("text", current))
    return f"{current[:start]}{str(patch.get('text', ''))}{current[start + delete_count:]}"


def _apply_worker_committed_update(current: str, message: dict[str, Any]) -> str:
    if "committed_reset" in message:
        return str(message.get("committed_reset", ""))
    if "committed_append" in message:
        return f"{current}{str(message.get('committed_append', ''))}"
    return str(message.get("committed_text", current))


def _read_json_lines(stream: Any, output: queue.Queue[dict[str, Any]]) -> None:
    if stream is None:
        return
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            output.put({"type": "error", "message": line})
            continue
        if isinstance(message, dict):
            output.put(message)


def _read_text_lines(stream: Any, lines: list[str]) -> None:
    if stream is None:
        return
    pending = ""
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in {"\n", "\r"}:
            _append_stderr_line(lines, pending)
            pending = ""
            continue
        pending += chunk
        if len(pending) >= 2048:
            _append_stderr_line(lines, pending)
            pending = ""
    _append_stderr_line(lines, pending)


def _append_stderr_line(lines: list[str], line: str) -> None:
    line = line.strip()
    if line:
        lines.append(line)
        del lines[:-40]


def _format_worker_failure(return_code: int, stderr_lines: list[str]) -> str:
    detail = "\n".join(stderr_lines[-8:]).strip()
    base = f"流式识别子进程异常退出，退出码 {return_code}。"
    if sys.platform.startswith("win") and return_code in {-1073741819, 3221225477}:
        base += " 这通常是音频驱动或原生识别库崩溃导致的。"
    if detail:
        return f"{base}\n\n{detail}"
    return base
