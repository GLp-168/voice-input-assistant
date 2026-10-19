"""Worker process entry point for FunASR online recognition."""

from __future__ import annotations

import base64
import contextlib
import json
import queue
import sys
import threading
import time
import traceback
from typing import Any

from .config import DEFAULT_SAMPLE_RATE
from .transcript_reconciliation import TranscriptLedger


_EMIT_LOCK = threading.Lock()
_PROTOCOL_STDOUT = sys.stdout
_STREAMING_CHUNK_SIZE = [0, 10, 5]
_ENCODER_CHUNK_LOOK_BACK = 4
_DECODER_CHUNK_LOOK_BACK = 1


def main() -> int:
    try:
        first_line = sys.stdin.readline()
        if not first_line:
            _emit({"type": "error", "message": "FunASR worker did not receive startup parameters."})
            return 2
        config = json.loads(first_line)
        if not isinstance(config, dict) or config.get("type") != "prepare":
            _emit({"type": "error", "message": "FunASR worker startup parameters are invalid."})
            return 2
        return _run_prepared_worker(config)
    except Exception as exc:
        _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
        return 1


def _run_prepared_worker(config: dict[str, Any]) -> int:
    control_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    try:
        runtime = _FunASRRuntime.from_config(config)
        threading.Thread(
            target=_read_queued_controls,
            args=(control_queue,),
            name="voice-input-funasr-control",
            daemon=True,
        ).start()
        _emit(
            {
                "type": "ready",
                "backend": "funasr",
                "streaming_model": runtime.streaming_model_name,
                # Kept as protocol metadata. This process never loads the review model.
                "offline_model": str(config.get("offline_model") or ""),
            }
        )
        while True:
            message = control_queue.get()
            kind = message.get("type")
            if kind == "shutdown":
                return 0
            if kind != "start_external":
                continue
            try:
                final_text = runtime.run_external_audio_session(message, control_queue)
                _emit(
                    {
                        "type": "final",
                        "text": final_text,
                        "segments": list(runtime.last_segments),
                    }
                )
            except Exception as exc:
                _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
    except Exception as exc:
        _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
        return 1


class _FunASRRuntime:
    def __init__(
        self,
        *,
        streaming_model: Any,
        streaming_model_name: str,
        sample_rate: int,
        update_interval_ms: int,
        device: str,
        num_threads: int,
    ) -> None:
        self.streaming_model = streaming_model
        self.streaming_model_name = streaming_model_name
        self.sample_rate = int(sample_rate)
        self.update_interval_ms = max(40, int(update_interval_ms))
        self.device = device
        self.num_threads = max(1, int(num_threads))
        self.last_segments: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "_FunASRRuntime":
        try:
            with _redirect_stdout_to_stderr():
                from funasr import AutoModel
                from .funasr_compat import patch_funasr_runtime

                patch_funasr_runtime()
        except Exception as exc:
            raise RuntimeError(
                "FunASR is not available. Install funasr, modelscope, torch, torchaudio and soundfile."
            ) from exc

        device = str(config.get("device") or "cpu")
        ncpu = max(1, int(config.get("num_threads") or 4))
        streaming_model_name = str(config.get("streaming_model") or "paraformer-zh-streaming")
        with _redirect_stdout_to_stderr():
            streaming_model = AutoModel(
                model=streaming_model_name,
                device=device,
                disable_update=True,
                disable_pbar=True,
                ncpu=ncpu,
            )
        return cls(
            streaming_model=streaming_model,
            streaming_model_name=streaming_model_name,
            sample_rate=int(config.get("sample_rate") or DEFAULT_SAMPLE_RATE),
            update_interval_ms=int(config.get("update_interval_ms") or 120),
            device=device,
            num_threads=ncpu,
        )

    def run_external_audio_session(
        self,
        config: dict[str, Any],
        control_queue: queue.Queue[dict[str, Any]],
    ) -> str:
        import numpy as np

        hotwords = _normalize_hotwords(config.get("hotwords") or ())
        language = _funasr_language(config.get("language"))
        stride_samples = int(_STREAMING_CHUNK_SIZE[1] * 960)
        cache: dict[str, Any] = {}
        pending = np.empty(0, dtype=np.float32)
        ledger = TranscriptLedger()
        stream_audio_frames = 0
        last_emitted_text = ""
        last_emit_at = 0.0
        self.last_segments = []

        def emit(*, final: bool = False, endpoint: bool = False, force: bool = False) -> None:
            nonlocal last_emitted_text, last_emit_at
            text = ledger.text.strip()
            now = time.monotonic()
            if not force and not final and text == last_emitted_text:
                return
            if not force and not final and (now - last_emit_at) * 1000 < self.update_interval_ms:
                return
            patch = _text_patch(last_emitted_text, text)
            last_emitted_text = text
            last_emit_at = now
            _emit(
                {
                    "type": "event",
                    "text": text,
                    "text_patch": patch,
                    "partial_text": "",
                    "committed_text": "",
                    "is_final": final,
                    "is_endpoint": endpoint,
                    **({"segments": self.last_segments} if final else {}),
                }
            )

        def decode_streaming_chunk(chunk: Any, *, is_final: bool = False) -> str:
            if chunk.size == 0 and not is_final:
                return ""
            kwargs: dict[str, Any] = {
                "cache": cache,
                "is_final": is_final,
                "chunk_size": _STREAMING_CHUNK_SIZE,
                "encoder_chunk_look_back": _ENCODER_CHUNK_LOOK_BACK,
                "decoder_chunk_look_back": _DECODER_CHUNK_LOOK_BACK,
            }
            if hotwords:
                kwargs["hotword"] = _format_funasr_hotwords(hotwords)
                kwargs["hotwords"] = hotwords
            if language is not None:
                kwargs["language"] = language
            with _redirect_stdout_to_stderr():
                result = self.streaming_model.generate(input=chunk, **kwargs)
            return _extract_result_text(result)

        while True:
            message = control_queue.get()
            kind = message.get("type")
            if kind == "audio":
                samples = _message_to_float32_mono(message, np)
                if samples.size == 0:
                    continue
                pending = np.concatenate((pending, samples))
                while pending.size >= stride_samples:
                    chunk = pending[:stride_samples]
                    pending = pending[stride_samples:]
                    text = decode_streaming_chunk(chunk)
                    if text:
                        ledger.append_online(
                            text,
                            start_frame=stream_audio_frames,
                            end_frame=stream_audio_frames + int(chunk.size),
                        )
                    stream_audio_frames += int(chunk.size)
                    emit()
            elif kind == "stop":
                if pending.size:
                    final_chunk = pending
                    pending = np.empty(0, dtype=np.float32)
                else:
                    final_chunk = np.zeros(int(0.35 * self.sample_rate), dtype=np.float32)
                text = decode_streaming_chunk(final_chunk, is_final=True)
                if text:
                    ledger.append_online(
                        text,
                        start_frame=stream_audio_frames,
                        end_frame=stream_audio_frames + int(final_chunk.size),
                    )
                self.last_segments = list(ledger.history_segments(sample_rate=self.sample_rate))
                emit(final=True, endpoint=True, force=True)
                return ledger.text.strip()
            elif kind == "shutdown":
                return ""


def _read_queued_controls(control_queue: queue.Queue[dict[str, Any]]) -> None:
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            control_queue.put(message)


def _message_to_float32_mono(message: dict[str, Any], np: Any) -> Any:
    raw = base64.b64decode(str(message.get("pcm", "")))
    if not raw:
        return np.empty(0, dtype=np.float32)
    samples = np.frombuffer(raw, dtype=np.int16)
    channels = max(1, int(message.get("channels") or 1))
    if channels > 1 and samples.size >= channels:
        samples = samples[: samples.size - (samples.size % channels)].reshape(-1, channels)[:, 0]
    return np.ascontiguousarray(samples.astype(np.float32) / 32768.0)


def _normalize_hotwords(value: Any) -> list[str]:
    words: list[str] = []
    if isinstance(value, str):
        candidates = value.replace("\n", ",").split(",")
    else:
        try:
            candidates = list(value)
        except TypeError:
            candidates = []
    for item in candidates:
        word = str(item).strip()
        if word and word not in words:
            words.append(word)
    return words[:300]


def _format_funasr_hotwords(hotwords: list[str]) -> str:
    return " ".join(word.strip() for word in hotwords if word.strip())


def _funasr_language(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none", "auto"}:
        return None
    if normalized in {"zh-hans", "zh-hant", "zh", "zh-cn", "zh_cn", "chinese"}:
        return "zh"
    if normalized in {"en", "english"}:
        return "en"
    return normalized


def _extract_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("text", "")).strip()
    if isinstance(result, (list, tuple)):
        parts = [_extract_result_text(item) for item in result]
        return "".join(part for part in parts if part).strip()
    return str(result or "").strip()


def _text_patch(previous: str, current: str) -> dict[str, Any]:
    limit = min(len(previous), len(current))
    start = 0
    while start < limit and previous[start] == current[start]:
        start += 1
    return {
        "start": start,
        "delete_count": len(previous) - start,
        "text": current[start:],
    }


@contextlib.contextmanager
def _redirect_stdout_to_stderr() -> Any:
    """Keep the parent/worker JSON protocol clean while native libraries log."""

    with contextlib.redirect_stdout(sys.stderr):
        yield


def _emit(message: dict[str, Any]) -> None:
    with _EMIT_LOCK:
        _PROTOCOL_STDOUT.write(json.dumps(message, ensure_ascii=True) + "\n")
        _PROTOCOL_STDOUT.flush()


if __name__ == "__main__":
    raise SystemExit(main())
