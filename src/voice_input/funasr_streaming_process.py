"""Process-isolated FunASR streaming recognizer."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Iterable
from typing import Any

from .asr import FunASROfflineEngine
from .audio import AudioLevelCallback, MicrophoneStateCallback
from .config import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE
from .streaming import StreamingASRError, StreamingRecognitionEvent
from .streaming_process import (
    _ParentMicrophoneCapture,
    _drain_capture_blocks,
    _apply_worker_committed_update,
    _apply_worker_text_update,
    _read_json_lines,
    _read_text_lines,
    _terminate_process,
    _write_worker_message,
)


FinalizationProgressCallback = Callable[[int, int], None]
_REVIEW_CHUNK_SECONDS = 60
LOGGER = logging.getLogger(__name__)


class FunASRPreparedStreamingRecognizer:
    """Reusable online recognizer with process-isolated final review."""

    backend = "funasr"
    supports_hotwords = True

    def __init__(
        self,
        *,
        streaming_model: str,
        offline_model: str,
        vad_model: str,
        punc_model: str,
        speaker_model: str = "",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        chunk_ms: int = 100,
        update_interval_ms: int = 120,
        num_threads: int | None = None,
        review_num_threads: int | None = None,
        device: str | None = None,
        language: str | None = None,
        review_interval_seconds: float = 0.0,
        release_online_before_review: bool = True,
        retain_review_worker: bool = False,
    ) -> None:
        self.streaming_model = str(streaming_model)
        self.offline_model = str(offline_model)
        self.vad_model = str(vad_model)
        self.punc_model = str(punc_model)
        self.speaker_model = str(speaker_model)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.chunk_ms = max(40, int(chunk_ms))
        self.update_interval_ms = max(40, int(update_interval_ms))
        self.num_threads = _default_funasr_threads() if num_threads is None else max(1, int(num_threads))
        self.review_num_threads = (
            self.num_threads if review_num_threads is None else max(1, int(review_num_threads))
        )
        self.device = str(device or "cpu")
        self.language = language
        self.review_interval_seconds = max(0.0, float(review_interval_seconds or 0.0))
        self.release_online_before_review = bool(release_online_before_review)
        self.retain_review_worker = bool(retain_review_worker)
        self.final_wait_timeout_seconds = 300.0
        self.last_completion_status = "idle"
        self.last_segments: tuple[dict[str, Any], ...] = ()
        self.last_finalization_progress = (0, 0)

        self._process: subprocess.Popen[str] | None = None
        self._output: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._ready = threading.Event()
        self._failed_message: str | None = None
        self._prepare_started = False
        self._lock = threading.Lock()
        self._audio_level_callback: AudioLevelCallback | None = None
        self._microphone_state_callback: MicrophoneStateCallback | None = None
        self._finalization_progress_callback: FinalizationProgressCallback | None = None
        self._review_engine: FunASROfflineEngine | None = None
        self._review_cancel_event = threading.Event()

    def set_audio_monitor(
        self,
        on_level: AudioLevelCallback | None,
        on_state: MicrophoneStateCallback | None = None,
    ) -> None:
        self._audio_level_callback = on_level
        self._microphone_state_callback = on_state

    def set_finalization_monitor(
        self,
        on_progress: FinalizationProgressCallback | None,
    ) -> None:
        """Register a final-review callback run synchronously by the transcription thread."""

        self._finalization_progress_callback = on_progress

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
                name="voice-input-funasr-stdout",
                daemon=True,
            ).start()
            threading.Thread(
                target=_read_text_lines,
                args=(self._process.stderr, self._stderr_lines),
                name="voice-input-funasr-stderr",
                daemon=True,
            ).start()
            _write_worker_message(self._process, self._prepare_message())
            threading.Thread(
                target=self._wait_until_ready,
                name="voice-input-funasr-ready",
                daemon=True,
            ).start()

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._failed_message is None and self._process is not None and self._process.poll() is None

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout=timeout) and self.is_ready()

    def close(self) -> None:
        self.cancel()
        process = self._process
        self._process = None
        self._prepare_started = False
        self._ready.clear()
        if process is not None:
            if process.poll() is None:
                _write_worker_message(process, {"type": "shutdown"})
                _terminate_process(process, graceful_timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        self._close_review_engine()

    def cancel(self) -> None:
        """Cancel final review without tearing down a reusable online worker."""

        self._review_cancel_event.set()

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
        self.last_segments = ()
        self.last_finalization_progress = (0, 0)
        self.prepare()
        self._review_cancel_event.clear()
        capture = _ParentMicrophoneCapture(
            sample_rate=self.sample_rate,
            channels=self.channels,
            chunk_ms=self.chunk_ms,
            device=None,
            on_level=self._audio_level_callback,
            on_state=self._microphone_state_callback,
        )
        capture.start(
            stop_event=stop_event,
            pause_event=pause_event,
            max_duration_seconds=max_duration_seconds,
        )
        review_audio = (
            _ReviewAudioFile(sample_rate=self.sample_rate, channels=self.channels)
            if self.offline_model.strip()
            else None
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
        final_wait_timeout = max(0.05, float(self.final_wait_timeout_seconds))
        final_activity_at: float | None = None

        try:
            ready_deadline = time.monotonic() + 120
            while not self._ready.is_set():
                if stop_event.is_set() and not capture.done_event.is_set():
                    capture.close()
                capture.raise_error_if_any()
                process = self._process
                return_code = None if process is None else process.poll()
                if return_code is not None:
                    raise StreamingASRError(_format_funasr_worker_failure(return_code, self._stderr_lines))
                if time.monotonic() >= ready_deadline:
                    raise StreamingASRError("FunASR 引擎启动超时，请稍后重试或运行健康检查。")
                time.sleep(0.03)

            capture.raise_error_if_any()
            if self._failed_message is not None:
                raise StreamingASRError(self._failed_message)
            process = self._process
            if process is None or process.poll() is not None:
                raise StreamingASRError(_format_funasr_worker_failure(-1, self._stderr_lines))

            _write_worker_message(
                process,
                {
                    "type": "start_external",
                    "max_duration_seconds": float(max_duration_seconds),
                    "hotwords": list(hotwords),
                    "language": self.language,
                    "review_interval_seconds": self.review_interval_seconds,
                },
            )
            session_started = True

            while True:
                _drain_capture_blocks(
                    process,
                    capture,
                    on_block=None if review_audio is None else review_audio.append,
                )

                if (stop_event.is_set() or capture.done_event.is_set()) and not stop_sent:
                    capture.close()
                    _drain_capture_blocks(
                        process,
                        capture,
                        on_block=None if review_audio is None else review_audio.append,
                    )
                    _write_worker_message(process, {"type": "stop"})
                    stop_sent = True
                    stop_sent_at = time.monotonic()
                    final_activity_at = stop_sent_at

                while True:
                    try:
                        message = self._output.get_nowait()
                    except queue.Empty:
                        break
                    if stop_sent_at is not None:
                        final_activity_at = time.monotonic()
                    kind = str(message.get("type", ""))
                    if kind == "event":
                        latest_text = _apply_worker_text_update(latest_text, message)
                        latest_committed_text = _apply_worker_committed_update(
                            latest_committed_text,
                            message,
                        )
                        event_segments = _worker_segments(message.get("segments"))
                        if event_segments or "segments" in message:
                            self.last_segments = event_segments
                        review_pending = review_audio is not None and review_audio.frame_count > 0
                        worker_final = bool(message.get("is_final", False))
                        on_event(
                            StreamingRecognitionEvent(
                                text=latest_text,
                                partial_text=str(message.get("partial_text", "")),
                                committed_text=latest_committed_text,
                                is_final=worker_final and not review_pending,
                                is_endpoint=bool(message.get("is_endpoint", False)) and not review_pending,
                                segments=event_segments,
                            )
                        )
                    elif kind == "final":
                        final_text = str(message.get("text", ""))
                        final_segments = _worker_segments(message.get("segments"))
                        if final_segments or "segments" in message:
                            self.last_segments = final_segments
                        completed_reviews, total_reviews = self.last_finalization_progress
                        if total_reviews > 0 and completed_reviews < total_reviews:
                            self._publish_finalization_progress(total_reviews, total_reviews)
                        final_seen = True
                    elif kind == "finalizing":
                        completed_reviews, total_reviews = _worker_finalization_progress(message)
                        self._publish_finalization_progress(completed_reviews, total_reviews)
                    elif kind == "error":
                        error_message = str(message.get("message", "FunASR worker failed."))

                return_code = process.poll()
                if error_message:
                    raise StreamingASRError(error_message)
                if final_seen:
                    if review_audio is not None and review_audio.frame_count > 0:
                        review_path = review_audio.seal()
                        if self.release_online_before_review:
                            self._release_online_worker()
                        final_text, review_segments = self._review_recording(
                            review_path,
                            hotwords=tuple(hotwords),
                            fallback_text=final_text or latest_text,
                            fallback_segments=self.last_segments,
                        )
                        self.last_segments = review_segments
                        latest_text = final_text
                        latest_committed_text = final_text
                        on_event(
                            StreamingRecognitionEvent(
                                text=final_text,
                                partial_text="",
                                committed_text=final_text,
                                is_final=True,
                                is_endpoint=True,
                                segments=review_segments,
                            )
                        )
                    if self.last_completion_status == "running":
                        self.last_completion_status = "completed"
                    return final_text
                if return_code is not None:
                    raise StreamingASRError(_format_funasr_worker_failure(return_code, self._stderr_lines))
                if final_activity_at is not None and time.monotonic() - final_activity_at > final_wait_timeout:
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
                _drain_capture_blocks(
                    process,
                    capture,
                    on_block=None if review_audio is None else review_audio.append,
                )
                _write_worker_message(process, {"type": "stop"})
            if review_audio is not None:
                review_audio.close()

    def _review_recording(
        self,
        audio_path: Path,
        *,
        hotwords: tuple[str, ...],
        fallback_text: str,
        fallback_segments: tuple[dict[str, Any], ...],
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        engine = self._ensure_review_engine()
        self._publish_finalization_progress(0, 1)
        try:
            result = engine.transcribe(
                audio_path,
                language=self.language,
                hotwords=hotwords,
                timeout_seconds=self.final_wait_timeout_seconds,
                cancel_event=self._review_cancel_event,
            )
        except Exception:
            LOGGER.warning("FunASR offline review failed; keeping the online result", exc_info=True)
            self.last_completion_status = "incomplete"
            return fallback_text, fallback_segments
        finally:
            if not self.retain_review_worker:
                self._close_review_engine()
            self._publish_finalization_progress(1, 1)
        review_text = str(result.text).strip()
        text = review_text or fallback_text
        if review_text and result.segments:
            segments = tuple(dict(segment) for segment in result.segments)
        elif review_text and review_text != fallback_text:
            segments = (_whole_recording_segment(review_text, result.duration_seconds),)
        else:
            segments = fallback_segments
        return text, segments

    def _ensure_review_engine(self) -> FunASROfflineEngine:
        engine = self._review_engine
        if engine is None:
            engine = FunASROfflineEngine(
                model=self.offline_model,
                vad_model=self.vad_model,
                punc_model=self.punc_model,
                spk_model=self.speaker_model,
                device=self.device,
                num_threads=self.review_num_threads,
                file_chunk_seconds=float(_REVIEW_CHUNK_SECONDS),
            )
            self._review_engine = engine
        return engine

    def _close_review_engine(self) -> None:
        engine = self._review_engine
        self._review_engine = None
        if engine is not None:
            engine.close()

    def _prepare_message(self) -> dict[str, Any]:
        return {
            "type": "prepare",
            "streaming_model": self.streaming_model,
            "offline_model": self.offline_model,
            "vad_model": self.vad_model,
            "punc_model": self.punc_model,
            "speaker_model": self.speaker_model,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "chunk_ms": self.chunk_ms,
            "update_interval_ms": self.update_interval_ms,
            "num_threads": self.num_threads,
            "review_num_threads": self.review_num_threads,
            "device": self.device,
        }

    def _wait_until_ready(self) -> None:
        process = self._process
        while process is not None:
            try:
                message = self._output.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None:
                    self._failed_message = _format_funasr_worker_failure(process.returncode or -1, self._stderr_lines)
                    self._ready.set()
                    return
                continue
            kind = str(message.get("type", ""))
            if kind == "ready":
                self._ready.set()
                return
            if kind == "error":
                self._failed_message = str(message.get("message", "FunASR worker failed."))
                self._ready.set()
                return

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

    def _terminate_current_worker(self) -> None:
        process = self._process
        self._process = None
        self._prepare_started = False
        self._ready.clear()
        if process is None:
            return
        _terminate_process(process)

    def _release_online_worker(self) -> None:
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

    def _publish_finalization_progress(self, completed: int, total: int) -> None:
        progress = (completed, total)
        self.last_finalization_progress = progress
        callback = self._finalization_progress_callback
        if callback is not None:
            callback(*progress)


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--funasr-streaming-worker"]
    return [sys.executable, "-m", "voice_input.funasr_streaming_worker"]


class _ReviewAudioFile:
    """Session-owned WAV file removed after review on every exit path."""

    def __init__(self, *, sample_rate: int, channels: int) -> None:
        temporary = tempfile.NamedTemporaryFile(
            prefix="voice-input-funasr-review-",
            suffix=".wav",
            delete=False,
        )
        self.path = Path(temporary.name)
        temporary.close()
        self.frame_count = 0
        self._writer: wave.Wave_write | None = wave.open(str(self.path), "wb")
        self._writer.setnchannels(max(1, int(channels)))
        self._writer.setsampwidth(2)
        self._writer.setframerate(max(1, int(sample_rate)))

    def append(self, pcm: bytes, frames: int) -> None:
        writer = self._writer
        if writer is None:
            raise RuntimeError("review audio file is sealed")
        writer.writeframesraw(pcm)
        self.frame_count += max(0, int(frames))

    def seal(self) -> Path:
        writer = self._writer
        if writer is not None:
            self._writer = None
            writer.close()
        return self.path

    def close(self) -> None:
        try:
            self.seal()
        finally:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def _default_funasr_threads() -> int:
    cpu_count = os.cpu_count() or 4
    return max(2, min(4, cpu_count // 2 or 1))


def _worker_segments(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _whole_recording_segment(text: str, duration_seconds: float | None) -> dict[str, Any]:
    try:
        duration = max(0.0, float(duration_seconds or 0.0))
    except (TypeError, ValueError):
        duration = 0.0
    return {"text": text, "start": 0.0, "end": duration}


def _worker_finalization_progress(message: dict[str, Any]) -> tuple[int, int]:
    try:
        total = max(0, int(message.get("total_reviews", 0)))
    except (TypeError, ValueError):
        total = 0
    try:
        completed = max(0, int(message.get("completed_reviews", 0)))
    except (TypeError, ValueError):
        completed = 0
    if total > 0:
        completed = min(completed, total)
    else:
        completed = 0
    return completed, total


def _format_funasr_worker_failure(return_code: int, stderr_lines: list[str]) -> str:
    detail = "\n".join(stderr_lines[-8:]).strip()
    base = f"FunASR worker exited unexpectedly with code {return_code}."
    if sys.platform.startswith("win") and return_code in {-1073741819, 3221225477}:
        base += " This usually indicates a native audio or model runtime crash."
    if detail:
        return f"{base}\n\n{detail}"
    return base
