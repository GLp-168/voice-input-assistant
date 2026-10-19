"""Replaceable offline ASR engine abstraction.

The MVP ships with ``DummyASREngine`` so the rest of the app can integrate
without a real speech model. Production engines should implement
``BaseASREngine.transcribe`` and return the same ``ASRResult`` contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import contextlib
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from typing import Any, Mapping, Sequence

from .config import DEFAULT_DUMMY_TEXT, DEFAULT_SAMPLE_RATE


AudioInput = str | Path | bytes | bytearray | memoryview | Any
_OPTION_UNSET = object()


class ASREngineError(RuntimeError):
    """Base exception raised by ASR engines."""


class ASREngineUnavailable(ASREngineError):
    """Raised when an optional ASR backend or model is not available."""


class ASREngineCancelled(ASREngineError):
    """Raised when an active recognition request is cancelled."""


@dataclass(frozen=True)
class ASRResult:
    """Normalized transcription result returned by every ASR backend."""

    text: str
    engine: str
    language: str | None = None
    confidence: float | None = None
    duration_seconds: float | None = None
    segments: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)


class BaseASREngine(ABC):
    """Common interface for offline ASR backends."""

    name: str = "base"

    def is_available(self) -> bool:
        """Return whether this engine has the dependencies needed to run."""

        return True

    @abstractmethod
    def transcribe(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        """Transcribe an audio path, raw bytes, or an audio chunk object."""


@dataclass
class DummyASREngine(BaseASREngine):
    """Placeholder ASR engine used for UI and input-injection integration."""

    placeholder_text: str = DEFAULT_DUMMY_TEXT
    name: str = "dummy"

    def transcribe(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        sample_rate = sample_rate or getattr(audio, "sample_rate", None) or DEFAULT_SAMPLE_RATE
        duration_seconds = _duration_seconds(audio, sample_rate)
        byte_count = _byte_count(audio)

        details: list[str] = []
        if duration_seconds is not None:
            details.append(f"{duration_seconds:.2f}s")
        if byte_count is not None:
            details.append(f"{byte_count} bytes")

        text = self.placeholder_text
        if details:
            text = f"{text} ({', '.join(details)})"

        return ASRResult(
            text=text,
            engine=self.name,
            language=language,
            confidence=0.0,
            duration_seconds=duration_seconds,
            metadata={
                "backend": "dummy",
                "sample_rate": sample_rate,
                "byte_count": byte_count,
                "options": dict(options),
            },
        )


@dataclass
class WhisperCppEngine(BaseASREngine):
    """Adapter for the whisper.cpp command-line executable."""

    model_path: str | Path
    executable: str = "whisper-cli"
    timeout_seconds: float | None = 120.0
    extra_args: Sequence[str] = field(default_factory=tuple)
    name: str = "whisper.cpp"

    def is_available(self) -> bool:
        model_text = str(self.model_path).strip()
        if not model_text:
            return False
        return Path(model_text).expanduser().is_file() and _resolve_executable(self.executable) is not None

    def transcribe(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        model_text = str(self.model_path).strip()
        if not model_text:
            raise ASREngineUnavailable(
                "未配置 whisper.cpp 模型文件。请在配置中选择 .bin 或 .gguf 模型文件。"
            )

        model_path = Path(model_text).expanduser()
        if not model_path.exists():
            raise ASREngineUnavailable(
                f"whisper.cpp 模型文件不存在(model not found): {model_path}。请检查模型路径。"
            )
        if not model_path.is_file():
            raise ASREngineUnavailable(
                f"whisper.cpp 模型路径不是文件: {model_path}。请选择 .bin 或 .gguf 模型文件。"
            )
        executable = _resolve_executable(self.executable)
        if executable is None:
            raise ASREngineUnavailable(
                f"找不到 whisper.cpp 执行文件(executable not found): {self.executable}。"
                "请在配置中选择 whisper-cli.exe，或确认它已加入 PATH。"
            )

        sample_rate = sample_rate or getattr(audio, "sample_rate", None) or DEFAULT_SAMPLE_RATE
        timeout_option = options.get("timeout_seconds", self.timeout_seconds)
        timeout = None if timeout_option is None else float(timeout_option)
        cancel_event = options.get("cancel_event")
        if _event_is_set(cancel_event):
            raise ASREngineCancelled("识别已取消。")

        with tempfile.TemporaryDirectory(prefix="voice-input-whisper-") as temp_dir:
            audio_path = _materialize_audio_file(audio, Path(temp_dir), sample_rate=sample_rate)
            command = [
                executable,
                "-m",
                str(model_path),
                "-f",
                str(audio_path),
                *self.extra_args,
            ]
            if language:
                command.extend(["-l", language])
            try:
                if cancel_event is None:
                    completed = subprocess.run(
                        command,
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        **_hidden_subprocess_kwargs(),
                    )
                else:
                    completed = _run_cancellable_whisper(
                        command,
                        timeout_seconds=timeout,
                        cancel_event=cancel_event,
                    )
            except subprocess.TimeoutExpired as exc:
                raise ASREngineError(
                    f"whisper.cpp 识别超时，已超过 {timeout:.0f} 秒。"
                    "请尝试更小模型，或检查目标机器性能。"
                ) from exc
            except OSError as exc:
                raise ASREngineError(f"无法启动 whisper.cpp 执行文件: {executable}。{exc}") from exc

        combined_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode != 0:
            details = combined_output.strip() or "无输出"
            raise ASREngineError(
                f"whisper.cpp 执行失败，退出码 {completed.returncode}。"
                f"请检查模型、执行文件和音频格式。输出: {details[:2000]}"
            )

        text = _parse_whisper_output(completed.stdout)
        return ASRResult(
            text=text,
            engine=self.name,
            language=language,
            duration_seconds=_duration_seconds(audio, sample_rate),
            metadata={
                "backend": "whisper.cpp",
                "command": command,
                "returncode": completed.returncode,
                "stderr": completed.stderr,
            },
        )


@dataclass
class FunASROfflineEngine(BaseASREngine):
    """High-accuracy local FunASR offline recognizer with VAD, punctuation and hotwords."""

    model: str | Path
    vad_model: str | Path = "fsmn-vad"
    punc_model: str | Path = "ct-punc"
    spk_model: str | Path = ""
    device: str = "cpu"
    num_threads: int = 4
    batch_size_s: int = 300
    file_chunk_seconds: float = 60.0
    name: str = "funasr"
    _runtime: Any = field(default=None, init=False, repr=False)
    _runtime_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _worker_process: subprocess.Popen[str] | None = field(default=None, init=False, repr=False)
    _worker_output: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue, init=False, repr=False)
    _worker_stderr: list[str] = field(default_factory=list, init=False, repr=False)
    _worker_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def is_available(self) -> bool:
        if not _is_model_reference_available(self.model):
            return False
        if self.vad_model and not _is_model_reference_available(self.vad_model):
            return False
        if self.punc_model and not _is_model_reference_available(self.punc_model):
            return False
        if self.spk_model and not _is_model_reference_available(self.spk_model):
            return False
        try:
            __import__("funasr")
        except ImportError:
            return False
        return True

    def transcribe(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        timeout_option = options.pop("timeout_seconds", _OPTION_UNSET)
        cancel_option = options.pop("cancel_event", _OPTION_UNSET)
        if timeout_option is not _OPTION_UNSET or cancel_option is not _OPTION_UNSET:
            timeout_seconds = None if timeout_option is _OPTION_UNSET else timeout_option
            cancel_event = None if cancel_option is _OPTION_UNSET else cancel_option
            return self._transcribe_isolated(
                audio,
                sample_rate=sample_rate,
                language=language,
                timeout_seconds=(
                    None if timeout_seconds is None else float(timeout_seconds)
                ),
                cancel_event=cancel_event,
                **options,
            )
        return self._transcribe_local(
            audio,
            sample_rate=sample_rate,
            language=language,
            **options,
        )

    def _transcribe_local(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        self._validate_model_configuration()
        if isinstance(audio, (str, Path)):
            return self._transcribe_file_chunked(
                Path(audio),
                language=language,
                **options,
            )
        samples, source_sample_rate = _audio_to_float32_mono(
            audio,
            target_sample_rate=DEFAULT_SAMPLE_RATE,
            source_sample_rate=sample_rate,
        )
        if getattr(samples, "size", 0) == 0:
            return ASRResult(
                text="",
                engine=self.name,
                language=language,
                duration_seconds=0.0,
                metadata={"backend": "funasr", "sample_rate": DEFAULT_SAMPLE_RATE},
            )

        hotwords = _normalize_hotwords(options.get("hotwords") or ())
        kwargs: dict[str, Any] = {"batch_size_s": self.batch_size_s}
        if hotwords:
            kwargs["hotword"] = _format_funasr_hotwords(hotwords)
            kwargs["hotwords"] = hotwords
        if language is not None:
            kwargs["language"] = language

        model = self._load_runtime()
        with _redirect_stdout_to_stderr():
            result = model.generate(input=samples, **kwargs)
        text = _extract_funasr_text(result)
        return ASRResult(
            text=text,
            engine=self.name,
            language=language,
            duration_seconds=_duration_seconds(audio, source_sample_rate) or (float(samples.size) / DEFAULT_SAMPLE_RATE),
            segments=_extract_funasr_segments(result),
            metadata={
                "backend": "funasr",
                "model": str(self.model),
                "vad_model": str(self.vad_model),
                "punc_model": str(self.punc_model),
                "spk_model": str(self.spk_model),
                "sample_rate": DEFAULT_SAMPLE_RATE,
                "source_sample_rate": source_sample_rate,
                "hotword_count": len(hotwords),
            },
        )

    def _transcribe_file_chunked(
        self,
        audio_path: Path,
        *,
        language: str | None,
        **options: Any,
    ) -> ASRResult:
        if not audio_path.is_file():
            raise ASREngineError(f"audio file not found: {audio_path}")
        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise ASREngineUnavailable("NumPy and soundfile are required to read imported audio.") from exc

        hotwords = _normalize_hotwords(options.get("hotwords") or ())
        kwargs: dict[str, Any] = {"batch_size_s": self.batch_size_s}
        if hotwords:
            kwargs["hotword"] = _format_funasr_hotwords(hotwords)
            kwargs["hotwords"] = hotwords
        if language is not None:
            kwargs["language"] = language

        model = self._load_runtime()
        texts: list[str] = []
        segments: list[Mapping[str, Any]] = []
        chunk_count = 0
        total_frames = 0
        with sf.SoundFile(str(audio_path), mode="r") as source:
            source_sample_rate = int(source.samplerate)
            if source_sample_rate <= 0:
                raise ASREngineError(f"invalid audio sample rate: {source_sample_rate}")
            frames_per_chunk = max(
                1,
                int(round(source_sample_rate * max(0.1, float(self.file_chunk_seconds)))),
            )
            while True:
                data = source.read(frames_per_chunk, dtype="float32", always_2d=True)
                if getattr(data, "size", 0) == 0:
                    break
                frame_count = int(data.shape[0])
                chunk_start_seconds = total_frames / float(source_sample_rate)
                total_frames += frame_count
                samples = data[:, 0] if data.ndim == 2 else data
                samples = _resample_float32(samples, source_sample_rate, DEFAULT_SAMPLE_RATE)
                samples = np.ascontiguousarray(samples, dtype=np.float32)
                if samples.size == 0:
                    continue
                with _redirect_stdout_to_stderr():
                    chunk_result = model.generate(input=samples, **kwargs)
                chunk_text = _extract_funasr_text(chunk_result)
                if chunk_text:
                    texts.append(chunk_text)
                segments.extend(
                    _offset_funasr_segments(
                        _extract_funasr_segments(chunk_result),
                        chunk_start_seconds,
                    )
                )
                chunk_count += 1

        duration_seconds = total_frames / float(source_sample_rate)
        return ASRResult(
            text="".join(texts).strip(),
            engine=self.name,
            language=language,
            duration_seconds=duration_seconds,
            segments=tuple(segments),
            metadata={
                "backend": "funasr",
                "model": str(self.model),
                "vad_model": str(self.vad_model),
                "punc_model": str(self.punc_model),
                "spk_model": str(self.spk_model),
                "sample_rate": DEFAULT_SAMPLE_RATE,
                "source_sample_rate": source_sample_rate,
                "hotword_count": len(hotwords),
                "file_chunk_seconds": self.file_chunk_seconds,
                "chunk_count": chunk_count,
            },
        )

    def _transcribe_isolated(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None,
        language: str | None,
        timeout_seconds: float | None,
        cancel_event: object | None,
        **options: Any,
    ) -> ASRResult:
        if _event_is_set(cancel_event):
            raise ASREngineCancelled("识别已取消。")
        with tempfile.TemporaryDirectory(prefix="voice-input-funasr-request-") as temp_dir:
            audio_path = _materialize_audio_file(
                audio,
                Path(temp_dir),
                sample_rate=sample_rate or getattr(audio, "sample_rate", None) or DEFAULT_SAMPLE_RATE,
            )
            return self._request_worker(
                audio_path,
                language=language,
                hotwords=_normalize_hotwords(options.get("hotwords") or ()),
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )

    def _request_worker(
        self,
        audio_path: Path,
        *,
        language: str | None,
        hotwords: Sequence[str],
        timeout_seconds: float | None,
        cancel_event: object | None,
    ) -> ASRResult:
        with self._worker_lock:
            if _event_is_set(cancel_event):
                raise ASREngineCancelled("识别已取消。")
            process = self._ensure_worker_unlocked()
            request_id = uuid.uuid4().hex
            request = {
                "type": "transcribe",
                "request_id": request_id,
                "config": {
                    "model": str(self.model),
                    "vad_model": str(self.vad_model),
                    "punc_model": str(self.punc_model),
                    "spk_model": str(self.spk_model),
                    "device": self.device,
                    "num_threads": self.num_threads,
                    "batch_size_s": self.batch_size_s,
                    "file_chunk_seconds": self.file_chunk_seconds,
                },
                "audio_path": str(audio_path),
                "language": language,
                "hotwords": list(hotwords),
            }
            _write_worker_message(process, request)
            deadline = (
                None
                if timeout_seconds is None
                else time.monotonic() + max(0.0, float(timeout_seconds))
            )
            while True:
                if _event_is_set(cancel_event):
                    self._stop_worker_unlocked()
                    raise ASREngineCancelled("识别已取消。")
                if deadline is not None and time.monotonic() >= deadline:
                    self._stop_worker_unlocked()
                    raise ASREngineError(f"FunASR 识别超时，已超过 {float(timeout_seconds):.0f} 秒。")
                try:
                    message = self._worker_output.get(timeout=0.05)
                except queue.Empty:
                    if process.poll() is not None:
                        self._stop_worker_unlocked()
                        raise ASREngineError(_format_offline_worker_failure(process.returncode, self._worker_stderr))
                    continue
                if str(message.get("request_id", "")) != request_id:
                    continue
                kind = str(message.get("type", ""))
                if kind == "error":
                    raise ASREngineError(str(message.get("message", "FunASR 识别子进程失败")))
                if kind != "result":
                    continue
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ASREngineError("FunASR 识别子进程返回了无效结果。")
                return ASRResult(
                    text=str(result.get("text", "")),
                    engine=str(result.get("engine", self.name)),
                    language=result.get("language") if isinstance(result.get("language"), str) else None,
                    confidence=_optional_float(result.get("confidence")),
                    duration_seconds=_optional_float(result.get("duration_seconds")),
                    segments=tuple(
                        dict(segment)
                        for segment in result.get("segments", ())
                        if isinstance(segment, Mapping)
                    ),
                    metadata=dict(result.get("metadata", {})) if isinstance(result.get("metadata"), Mapping) else {},
                )

    def _ensure_worker_unlocked(self) -> subprocess.Popen[str]:
        process = self._worker_process
        if process is not None and process.poll() is None:
            return process
        self._stop_worker_unlocked()
        creationflags = 0
        if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env["PYTHONIOENCODING"] = "utf-8"
        env["OMP_NUM_THREADS"] = str(max(1, int(self.num_threads)))
        env["MKL_NUM_THREADS"] = str(max(1, int(self.num_threads)))
        process = subprocess.Popen(
            _offline_worker_command(),
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
        self._worker_process = process
        self._worker_output = queue.Queue()
        self._worker_stderr = []
        threading.Thread(
            target=_read_worker_json_lines,
            args=(process.stdout, self._worker_output),
            name="voice-input-funasr-offline-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=_read_worker_text_lines,
            args=(process.stderr, self._worker_stderr),
            name="voice-input-funasr-offline-stderr",
            daemon=True,
        ).start()
        return process

    def close(self) -> None:
        with self._worker_lock:
            self._stop_worker_unlocked()

    def _stop_worker_unlocked(self) -> None:
        process = self._worker_process
        self._worker_process = None
        if process is None:
            return
        if process.poll() is None:
            _write_worker_message(process, {"type": "shutdown"})
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _load_runtime(self) -> Any:
        with self._runtime_lock:
            if self._runtime is not None:
                return self._runtime
            self._validate_model_configuration()
            try:
                with _redirect_stdout_to_stderr():
                    from funasr import AutoModel
                    from .funasr_compat import patch_funasr_runtime, should_retry_after_funasr_symbol_error

                    patch_funasr_runtime()
            except Exception as exc:
                raise ASREngineUnavailable(
                    "FunASR is not available. Install funasr, modelscope, torch, torchaudio and soundfile."
                ) from exc

            common_kwargs = {
                "device": self.device,
                "disable_update": True,
                "disable_pbar": True,
                "ncpu": max(1, int(self.num_threads)),
            }
            model_kwargs = {
                "model": str(self.model),
                "vad_model": str(self.vad_model) if self.vad_model else None,
                "punc_model": str(self.punc_model) if self.punc_model else None,
                "spk_model": str(self.spk_model) if self.spk_model else None,
                "vad_kwargs": {"max_single_segment_time": 60000},
                **common_kwargs,
            }
            model_kwargs = {key: value for key, value in model_kwargs.items() if value is not None}
            try:
                with _redirect_stdout_to_stderr():
                    self._runtime = AutoModel(**model_kwargs)
            except Exception as exc:
                if not should_retry_after_funasr_symbol_error(exc):
                    raise ASREngineUnavailable(f"FunASR offline model failed to load: {exc}") from exc
                with _redirect_stdout_to_stderr():
                    patch_funasr_runtime()
                    self._runtime = AutoModel(**model_kwargs)
            return self._runtime

    def _validate_model_configuration(self) -> None:
        if not str(self.model).strip():
            raise ASREngineUnavailable(
                "FunASR offline model is not configured. Install a package with offline review models "
                "or configure a local FunASR offline model."
            )


@dataclass
class SherpaOnnxEngine(BaseASREngine):
    """Future adapter for sherpa-onnx offline recognizers."""

    model_dir: str | Path
    name: str = "sherpa-onnx"

    def is_available(self) -> bool:
        if not Path(self.model_dir).exists():
            return False
        try:
            __import__("sherpa_onnx")
        except ImportError:
            return False
        return True

    def transcribe(
        self,
        audio: AudioInput,
        *,
        sample_rate: int | None = None,
        language: str | None = None,
        **options: Any,
    ) -> ASRResult:
        if not Path(self.model_dir).exists():
            raise ASREngineUnavailable(
                f"sherpa-onnx model directory not found: {Path(self.model_dir)}"
            )
        try:
            __import__("sherpa_onnx")
        except ImportError as exc:
            raise ASREngineUnavailable(
                "sherpa-onnx is not installed. Install sherpa-onnx before "
                "using SherpaOnnxEngine."
            ) from exc
        raise NotImplementedError(
            "SherpaOnnxEngine is a backend skeleton. Create the offline "
            "recognizer here without changing callers."
        )


def _duration_seconds(audio: AudioInput, sample_rate: int) -> float | None:
    duration = getattr(audio, "duration_seconds", None)
    if duration is not None:
        return float(duration)

    byte_count = _byte_count(audio)
    sample_width = getattr(audio, "sample_width", None)
    channels = getattr(audio, "channels", None)
    if byte_count is None or not sample_width or not channels or sample_rate <= 0:
        return None
    return byte_count / float(sample_rate * sample_width * channels)


def _byte_count(audio: AudioInput) -> int | None:
    data = getattr(audio, "pcm", None)
    if data is None:
        data = getattr(audio, "samples", None)
    if data is not None:
        return len(data)

    if isinstance(audio, (bytes, bytearray, memoryview)):
        return len(audio)

    if isinstance(audio, (str, Path)):
        path = Path(audio)
        if path.exists() and path.is_file():
            return path.stat().st_size
    return None


def _resolve_executable(executable: str | Path) -> str | None:
    executable_text = str(executable).strip()
    if not executable_text:
        return None
    executable_path = Path(executable_text).expanduser()
    if executable_path.is_file():
        return str(executable_path)
    resolved = shutil.which(executable_text)
    return resolved


def _materialize_audio_file(audio: AudioInput, temp_dir: Path, *, sample_rate: int) -> Path:
    if isinstance(audio, (str, Path)):
        path = Path(audio)
        if path.exists() and path.is_file():
            return path
        raise ASREngineError(f"audio file not found: {path}")

    pcm = getattr(audio, "pcm", None)
    if pcm is None:
        pcm = getattr(audio, "samples", None)
    if pcm is None and isinstance(audio, (bytes, bytearray, memoryview)):
        pcm = bytes(audio)
    if pcm is None:
        raise ASREngineError("unsupported audio input for whisper.cpp")

    channels = int(getattr(audio, "channels", 1) or 1)
    sample_width = int(getattr(audio, "sample_width", 2) or 2)
    output_path = temp_dir / "input.wav"
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm))
    return output_path


def _audio_to_float32_mono(
    audio: AudioInput,
    *,
    target_sample_rate: int,
    source_sample_rate: int | None = None,
) -> tuple[Any, int]:
    try:
        import numpy as np
    except ImportError as exc:
        raise ASREngineUnavailable("NumPy is required by FunASR offline recognition.") from exc

    if isinstance(audio, (str, Path)):
        path = Path(audio)
        if not path.exists() or not path.is_file():
            raise ASREngineError(f"audio file not found: {path}")
        try:
            import soundfile as sf
        except ImportError as exc:
            raise ASREngineUnavailable("soundfile is required to read imported audio.") from exc
        data, file_sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        samples = data[:, 0] if data.ndim == 2 else data
        return _resample_float32(samples, int(file_sample_rate), target_sample_rate), int(file_sample_rate)

    sample_rate = int(source_sample_rate or getattr(audio, "sample_rate", None) or DEFAULT_SAMPLE_RATE)
    channels = max(1, int(getattr(audio, "channels", 1) or 1))
    sample_width = int(getattr(audio, "sample_width", 2) or 2)

    pcm = getattr(audio, "pcm", None)
    if pcm is None:
        pcm = getattr(audio, "samples", None)
    if pcm is None and isinstance(audio, (bytes, bytearray, memoryview)):
        pcm = bytes(audio)
    if pcm is None:
        raise ASREngineError("unsupported audio input for FunASR")

    if sample_width != 2:
        with tempfile.TemporaryDirectory(prefix="voice-input-funasr-") as temp_dir:
            wav_path = _materialize_audio_file(audio, Path(temp_dir), sample_rate=sample_rate)
            return _audio_to_float32_mono(wav_path, target_sample_rate=target_sample_rate)

    samples = np.frombuffer(bytes(pcm), dtype=np.int16)
    if channels > 1 and samples.size >= channels:
        samples = samples[: samples.size - (samples.size % channels)].reshape(-1, channels)[:, 0]
    float_samples = samples.astype(np.float32) / 32768.0
    return _resample_float32(float_samples, sample_rate, target_sample_rate), sample_rate


def _resample_float32(samples: Any, source_sample_rate: int, target_sample_rate: int) -> Any:
    import numpy as np

    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if samples.size == 0 or source_sample_rate == target_sample_rate:
        return samples
    try:
        import soxr
    except ImportError as exc:
        raise ASREngineUnavailable("soxr is required to resample audio for FunASR.") from exc
    return np.ascontiguousarray(
        soxr.resample(samples, source_sample_rate, target_sample_rate, quality="HQ"),
        dtype=np.float32,
    )


def _is_model_reference_available(value: str | Path) -> bool:
    text = str(value).strip()
    if not text:
        return False
    path = Path(text).expanduser()
    if path.exists():
        return path.is_dir() or path.is_file()
    return not any(separator in text for separator in ("/", "\\"))


def _normalize_hotwords(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    else:
        try:
            candidates = list(value)
        except TypeError:
            candidates = []
    words: list[str] = []
    for item in candidates:
        word = str(item).strip()
        if word and word not in words:
            words.append(word)
    return words[:500]


def _format_funasr_hotwords(hotwords: Sequence[str]) -> str:
    return " ".join(word.strip() for word in hotwords if word.strip())


def _extract_funasr_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, Mapping):
        return str(result.get("text", "")).strip()
    if isinstance(result, (list, tuple)):
        parts = [_extract_funasr_text(item) for item in result]
        return "".join(part for part in parts if part).strip()
    return str(result or "").strip()


def _extract_funasr_segments(result: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(result, Mapping):
        items = (result,)
    elif isinstance(result, (list, tuple)):
        items = tuple(item for item in result if isinstance(item, Mapping))
    else:
        return ()

    segments: list[Mapping[str, Any]] = []
    for item in items:
        sentence_info = item.get("sentence_info")
        if isinstance(sentence_info, (list, tuple)):
            sentence_segment_count = len(segments)
            for sentence in sentence_info:
                if isinstance(sentence, Mapping):
                    segment = _normalize_funasr_segment(sentence)
                    if segment:
                        segments.append(segment)
            if len(segments) > sentence_segment_count:
                continue

        timestamp = item.get("timestamp")
        if isinstance(timestamp, (list, tuple)) and timestamp:
            segment = _normalize_funasr_segment(item)
            if segment:
                segments.append(segment)
    return tuple(segments)


def _normalize_funasr_segment(value: Mapping[str, Any]) -> dict[str, Any]:
    segment: dict[str, Any] = {}
    if "text" in value:
        segment["text"] = str(value.get("text") or "")
    for key in ("speaker", "spk"):
        if key in value:
            segment[key] = value[key]
    for key in ("start", "end"):
        if key in value:
            segment[key] = _funasr_milliseconds_to_seconds(value[key])

    timestamp = value.get("timestamp")
    if isinstance(timestamp, (list, tuple)):
        segment["timestamp"] = [
            [
                _funasr_milliseconds_to_seconds(item[0]),
                _funasr_milliseconds_to_seconds(item[1]),
            ]
            for item in timestamp
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]
        bounds = _funasr_timestamp_bounds(timestamp)
        if bounds is not None:
            segment.setdefault("start", _funasr_milliseconds_to_seconds(bounds[0]))
            segment.setdefault("end", _funasr_milliseconds_to_seconds(bounds[1]))
    segment["time_unit"] = "seconds"
    return segment


def _offset_funasr_segments(
    segments: Sequence[Mapping[str, Any]],
    offset_seconds: float,
) -> tuple[Mapping[str, Any], ...]:
    if offset_seconds == 0:
        return tuple(dict(segment) for segment in segments)
    adjusted: list[Mapping[str, Any]] = []
    for source in segments:
        segment = dict(source)
        for key in ("start", "end"):
            if key in segment:
                segment[key] = _add_seconds_offset(segment[key], offset_seconds)
        timestamp = segment.get("timestamp")
        if isinstance(timestamp, (list, tuple)):
            segment["timestamp"] = [
                [
                    _add_seconds_offset(item[0], offset_seconds),
                    _add_seconds_offset(item[1], offset_seconds),
                ]
                for item in timestamp
                if isinstance(item, (list, tuple)) and len(item) >= 2
            ]
        adjusted.append(segment)
    return tuple(adjusted)


def _add_seconds_offset(value: Any, offset_seconds: float) -> Any:
    if isinstance(value, bool):
        return value
    try:
        return float(value) + offset_seconds
    except (TypeError, ValueError):
        return value


def _funasr_timestamp_bounds(timestamp: Sequence[Any]) -> tuple[Any, Any] | None:
    pairs = [
        item
        for item in timestamp
        if isinstance(item, (list, tuple)) and len(item) >= 2
    ]
    if not pairs:
        return None
    return pairs[0][0], pairs[-1][1]


def _funasr_milliseconds_to_seconds(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return value


@contextlib.contextmanager
def _redirect_stdout_to_stderr() -> Any:
    import sys

    with contextlib.redirect_stdout(sys.stderr):
        yield


def _parse_whisper_output(stdout: str) -> str:
    lines: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        if not line:
            continue
        if line.lower().startswith(("whisper_", "system_info:", "main:")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _run_cancellable_whisper(
    command: list[str],
    *,
    timeout_seconds: float | None,
    cancel_event: object,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_subprocess_kwargs(),
    )
    deadline = (
        None
        if timeout_seconds is None
        else time.monotonic() + max(0.0, timeout_seconds)
    )
    try:
        while True:
            if _event_is_set(cancel_event):
                _terminate_subprocess(process)
                raise ASREngineCancelled("识别已取消。")
            if deadline is not None and time.monotonic() >= deadline:
                _terminate_subprocess(process)
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                return subprocess.CompletedProcess(
                    command,
                    process.returncode,
                    stdout,
                    stderr,
                )
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            _terminate_subprocess(process)


def _terminate_subprocess(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _offline_worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--funasr-offline-worker"]
    return [sys.executable, "-m", "voice_input.funasr_offline_worker"]


def _write_worker_message(process: subprocess.Popen[str], message: Mapping[str, Any]) -> None:
    if process.stdin is None or process.poll() is not None:
        raise ASREngineError("FunASR 识别子进程不可用。")
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise ASREngineError("无法向 FunASR 识别子进程发送请求。") from exc


def _read_worker_json_lines(stream: Any, output: queue.Queue[dict[str, Any]]) -> None:
    if stream is None:
        return
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            output.put(message)


def _read_worker_text_lines(stream: Any, lines: list[str]) -> None:
    if stream is None:
        return
    for line in stream:
        cleaned = line.strip()
        if cleaned:
            lines.append(cleaned)
            del lines[:-40]


def _format_offline_worker_failure(return_code: int | None, stderr_lines: Sequence[str]) -> str:
    detail = "\n".join(stderr_lines[-8:]).strip()
    code = return_code if return_code is not None else "unknown"
    message = f"FunASR 识别子进程异常退出，退出码 {code}。"
    return f"{message}\n\n{detail}" if detail else message


def _event_is_set(event: object | None) -> bool:
    is_set = getattr(event, "is_set", None)
    return bool(callable(is_set) and is_set())


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hidden_subprocess_kwargs() -> dict[str, object]:
    if not sys_platform_is_windows():
        return {}
    kwargs: dict[str, object] = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    kwargs["startupinfo"] = startupinfo
    return kwargs


def sys_platform_is_windows() -> bool:
    import sys

    return sys.platform.startswith("win")
