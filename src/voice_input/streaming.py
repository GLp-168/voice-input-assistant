"""Low-latency streaming ASR support for microphone recognition."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import queue
import threading
import time
from typing import Any

from .audio import AudioCaptureError
from .config import DEFAULT_CHANNELS, DEFAULT_SAMPLE_RATE
from .resources import SHERPA_STREAMING_CTC_MODEL_FILES, SHERPA_STREAMING_TRANSDUCER_MODEL_FILES


class StreamingASRError(RuntimeError):
    """Raised when a streaming ASR session cannot run."""


class StreamingASRUnavailable(StreamingASRError):
    """Raised when the streaming backend or model files are not available."""


@dataclass(frozen=True, slots=True)
class StreamingRecognitionEvent:
    """A live recognition update.

    ``text`` is the complete transcript accumulated in the current recording
    cycle. During recording, ASR partial hypotheses can still be revised; the
    final event is emitted after the user stops recording.
    """

    text: str
    partial_text: str = ""
    committed_text: str = ""
    is_final: bool = False
    is_endpoint: bool = False
    segments: tuple[dict[str, Any], ...] = ()


class SherpaOnnxStreamingRecognizer:
    """Microphone streaming recognizer backed by sherpa-onnx OnlineRecognizer."""

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
        self.decoding_method = _normalize_decoding_method(decoding_method)
        self.model_kind = _detect_model_kind(self.model_dir)

    def is_available(self) -> bool:
        if not _has_required_model_files(self.model_dir):
            return False
        try:
            __import__("sherpa_onnx")
            __import__("numpy")
            __import__("sounddevice")
        except ImportError:
            return False
        return True

    def transcribe_microphone(
        self,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event: Callable[[StreamingRecognitionEvent], None],
        hotwords: Iterable[str] = (),
    ) -> str:
        """Capture microphone audio and emit live streaming recognition events."""

        return self._transcribe_microphone(
            max_duration_seconds=max_duration_seconds,
            stop_event=stop_event,
            pause_event=pause_event,
            on_event=on_event,
            hotwords=hotwords,
            prepared_recognizer=None,
        )

    def create_prepared_recognizer(self) -> Any:
        """Load and return a sherpa recognizer without opening the microphone."""

        if not _has_required_model_files(self.model_dir):
            raise StreamingASRUnavailable(f"sherpa-onnx streaming model is incomplete: {self.model_dir}")

        try:
            import sherpa_onnx
        except ImportError as exc:
            raise StreamingASRUnavailable(
                "低延迟流式识别需要 sherpa-onnx、numpy 和 sounddevice。"
            ) from exc

        return self._create_recognizer(sherpa_onnx)

    def transcribe_microphone_with_prepared_recognizer(
        self,
        recognizer: Any,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event: Callable[[StreamingRecognitionEvent], None],
        hotwords: Iterable[str] = (),
    ) -> str:
        """Capture microphone audio using an already loaded sherpa recognizer."""

        return self._transcribe_microphone(
            max_duration_seconds=max_duration_seconds,
            stop_event=stop_event,
            pause_event=pause_event,
            on_event=on_event,
            hotwords=hotwords,
            prepared_recognizer=recognizer,
        )

    def _transcribe_microphone(
        self,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event: Callable[[StreamingRecognitionEvent], None],
        hotwords: Iterable[str],
        prepared_recognizer: Any | None,
    ) -> str:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be greater than 0")
        if not _has_required_model_files(self.model_dir):
            raise StreamingASRUnavailable(f"sherpa-onnx streaming model is incomplete: {self.model_dir}")

        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise StreamingASRUnavailable(
                "低延迟流式识别需要 sherpa-onnx、numpy 和 sounddevice。"
            ) from exc

        committed_parts: list[str] = []
        last_emitted = ""
        last_emit_at = 0.0
        audio_queue: queue.Queue[Any] = queue.Queue()
        error_queue: queue.Queue[Exception] = queue.Queue()
        done_event = threading.Event()
        max_frames = int(round(max_duration_seconds * self.sample_rate))
        blocksize = max(320, int(self.sample_rate * self.chunk_ms / 1000))
        captured_frames = 0
        stop_drain_deadline: float | None = None

        def current_text(partial: str = "") -> str:
            parts = [part for part in (*committed_parts, partial.strip()) if part]
            return "".join(parts).strip()

        def emit(partial: str = "", *, final: bool = False, endpoint: bool = False, force: bool = False) -> None:
            nonlocal last_emitted, last_emit_at
            text = current_text(partial)
            now = time.monotonic()
            if not force and not final and text == last_emitted:
                return
            if not force and not final and (now - last_emit_at) * 1000 < self.update_interval_ms:
                return
            last_emitted = text
            last_emit_at = now
            on_event(
                StreamingRecognitionEvent(
                    text=text,
                    partial_text=partial.strip(),
                    committed_text="".join(committed_parts),
                    is_final=final,
                    is_endpoint=endpoint,
                )
            )

        def callback(indata: object, frame_count: int, _time: object, status: object) -> None:
            nonlocal captured_frames
            if status:
                error_queue.put(RuntimeError(str(status)))
            if stop_event.is_set():
                done_event.set()
                raise sd.CallbackStop()
            if pause_event.is_set():
                return
            captured_frames += int(frame_count)
            try:
                audio_queue.put(indata.copy(), block=False)  # type: ignore[attr-defined]
            except AttributeError:
                audio_queue.put(indata, block=False)
            except queue.Full:
                return
            if captured_frames >= max_frames:
                stop_event.set()
                done_event.set()
                raise sd.CallbackStop()

        try:
            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                device=self.device,
                blocksize=blocksize,
                callback=callback,
            ):
                if prepared_recognizer is None:
                    try:
                        import sherpa_onnx
                    except ImportError as exc:
                        raise StreamingASRUnavailable(
                            "低延迟流式识别需要 sherpa-onnx、numpy 和 sounddevice。"
                        ) from exc
                    recognizer = self._create_recognizer(sherpa_onnx)
                else:
                    recognizer = prepared_recognizer
                hotword_text = _format_hotwords(hotwords) if self.supports_hotwords else ""
                stream = _create_stream(recognizer, hotword_text)

                def decode_pending(*, force_emit: bool = False) -> None:
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    partial = recognizer.get_result(stream).strip()
                    emit(partial, force=force_emit)
                    if recognizer.is_endpoint(stream):
                        endpoint_text = recognizer.get_result(stream).strip()
                        if endpoint_text:
                            committed_parts.append(endpoint_text)
                            emit("", endpoint=True, force=True)
                        recognizer.reset(stream)

                while not done_event.is_set() or not audio_queue.empty():
                    if stop_event.is_set():
                        done_event.set()
                        if stop_drain_deadline is None:
                            stop_drain_deadline = time.monotonic() + 1.0
                        if audio_queue.empty() or time.monotonic() >= stop_drain_deadline:
                            break
                    try:
                        block = audio_queue.get(timeout=0.03)
                    except queue.Empty:
                        _raise_capture_error_if_any(error_queue)
                        continue
                    samples = _to_float32_mono(block, np)
                    if samples.size == 0:
                        continue
                    stream.accept_waveform(self.sample_rate, samples)
                    decode_pending()
                    _raise_capture_error_if_any(error_queue)

                tail = np.zeros(int(0.35 * self.sample_rate), dtype=np.float32)
                stream.accept_waveform(self.sample_rate, tail)
                stream.input_finished()
                decode_pending(force_emit=True)
                final_partial = recognizer.get_result(stream).strip()
        except AudioCaptureError:
            raise
        except StreamingASRUnavailable:
            raise
        except Exception as exc:  # pragma: no cover - depends on local audio host
            raise AudioCaptureError(f"failed to stream microphone audio: {exc}") from exc

        if final_partial:
            committed_parts.append(final_partial)
        final_text = "".join(committed_parts).strip()
        emit("", final=True, force=True)
        return final_text

    def _create_recognizer(self, sherpa_onnx: Any) -> Any:
        if self.model_kind == "zipformer2_ctc":
            return sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
                tokens=str(self.model_dir / "tokens.txt"),
                model=str(self.model_dir / "model.int8.onnx"),
                num_threads=self.num_threads,
                sample_rate=self.sample_rate,
                decoding_method="greedy_search",
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.0,
                rule2_min_trailing_silence=0.8,
                rule3_min_utterance_length=16.0,
                ctc_max_active=5000,
                provider="cpu",
            )
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self.model_dir / "tokens.txt"),
            encoder=str(self.model_dir / "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=str(self.model_dir / "decoder-epoch-99-avg-1.onnx"),
            joiner=str(self.model_dir / "joiner-epoch-99-avg-1.int8.onnx"),
            num_threads=self.num_threads,
            sample_rate=self.sample_rate,
            decoding_method=self.decoding_method,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.0,
            rule2_min_trailing_silence=0.8,
            rule3_min_utterance_length=16.0,
            max_active_paths=4,
            hotwords_score=1.5,
            model_type="zipformer",
            modeling_unit="cjkchar",
            provider="cpu",
        )

    @property
    def supports_hotwords(self) -> bool:
        return self.model_kind == "transducer" and self.decoding_method == "modified_beam_search"


def _has_required_model_files(model_dir: Path) -> bool:
    return _detect_model_kind(model_dir) is not None


def _detect_model_kind(model_dir: Path) -> str | None:
    if not model_dir.is_dir():
        return None
    if all((model_dir / filename).is_file() for filename in SHERPA_STREAMING_CTC_MODEL_FILES):
        return "zipformer2_ctc"
    if all((model_dir / filename).is_file() for filename in SHERPA_STREAMING_TRANSDUCER_MODEL_FILES):
        return "transducer"
    return None


def _to_float32_mono(block: object, np: Any) -> Any:
    samples = np.asarray(block)
    if samples.ndim > 1:
        samples = samples[:, 0]
    if samples.dtype == np.float32:
        return np.ascontiguousarray(samples)
    return np.ascontiguousarray(samples.astype(np.float32) / 32768.0)


def _raise_capture_error_if_any(error_queue: queue.Queue[Exception]) -> None:
    try:
        error = error_queue.get_nowait()
    except queue.Empty:
        return
    raise AudioCaptureError(f"failed to stream microphone audio: {error}") from error


def _format_hotwords(hotwords: Iterable[str]) -> str:
    cleaned: list[str] = []
    for item in hotwords:
        word = str(item).strip()
        formatted = _format_hotword_phrase(word)
        if formatted and formatted not in cleaned:
            cleaned.append(formatted)
    return "\n".join(cleaned)


def _format_hotword_phrase(word: str) -> str:
    if not word:
        return ""
    tokens: list[str] = []
    ascii_buffer: list[str] = []

    def flush_ascii() -> None:
        if ascii_buffer:
            tokens.append("".join(ascii_buffer))
            ascii_buffer.clear()

    for char in word:
        if char.isspace():
            flush_ascii()
        elif _is_cjk_char(char):
            flush_ascii()
            tokens.append(char)
        else:
            ascii_buffer.append(char)
    flush_ascii()
    return " ".join(tokens)


def _is_cjk_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
    )


def _normalize_decoding_method(value: str) -> str:
    method = str(value or "").strip().lower().replace("-", "_")
    if method in {"modified_beam_search", "greedy_search"}:
        return method
    return "modified_beam_search"


def _create_stream(recognizer: Any, hotwords: str) -> Any:
    if not hotwords:
        return recognizer.create_stream()
    try:
        return recognizer.create_stream(hotwords)
    except Exception:
        return recognizer.create_stream()
