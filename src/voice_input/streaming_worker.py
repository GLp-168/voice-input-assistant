"""Worker process entry point for isolated streaming recognition."""

from __future__ import annotations

import base64
import json
import queue
import sys
import threading
import traceback
from typing import Any

from .streaming import (
    SherpaOnnxStreamingRecognizer,
    StreamingRecognitionEvent,
    _create_stream,
    _format_hotwords,
    _to_float32_mono,
)


def main() -> int:
    try:
        first_line = sys.stdin.readline()
        if not first_line:
            _emit({"type": "error", "message": "流式识别子进程未收到启动参数。"})
            return 2
        config = json.loads(first_line)
        if not isinstance(config, dict) or config.get("type") not in {"start", "prepare"}:
            _emit({"type": "error", "message": "流式识别子进程启动参数无效。"})
            return 2
        if config.get("type") == "prepare":
            return _run_prepared_worker(config)

        stop_event = threading.Event()
        pause_event = threading.Event()
        control_thread = threading.Thread(
            target=_read_controls,
            args=(stop_event, pause_event),
            name="voice-input-streaming-worker-control",
            daemon=True,
        )
        control_thread.start()

        recognizer = SherpaOnnxStreamingRecognizer(
            str(config.get("model_dir", "")),
            sample_rate=int(config.get("sample_rate", 16000)),
            channels=int(config.get("channels", 1)),
            chunk_ms=int(config.get("chunk_ms", 100)),
            update_interval_ms=int(config.get("update_interval_ms", 120)),
            num_threads=int(config.get("num_threads", 2)),
            device=config.get("device"),
            decoding_method=str(config.get("decoding_method", "modified_beam_search")),
        )

        def on_event(event: StreamingRecognitionEvent) -> None:
            _emit(
                {
                    "type": "event",
                    "text": event.text,
                    "partial_text": event.partial_text,
                    "committed_text": event.committed_text,
                    "is_final": event.is_final,
                    "is_endpoint": event.is_endpoint,
                }
            )

        final_text = recognizer.transcribe_microphone(
            max_duration_seconds=float(config.get("max_duration_seconds", 7200)),
            stop_event=stop_event,
            pause_event=pause_event,
            on_event=on_event,
            hotwords=tuple(config.get("hotwords") or ()),
        )
        _emit({"type": "final", "text": final_text})
        return 0
    except Exception as exc:
        _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
        return 1


def _run_prepared_worker(config: dict[str, Any]) -> int:
    control_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    reader = threading.Thread(
        target=_read_queued_controls,
        args=(control_queue,),
        name="voice-input-streaming-prepared-control",
        daemon=True,
    )
    reader.start()
    try:
        runtime = _create_runtime(config)
        prepared_recognizer = runtime.create_prepared_recognizer()
        _emit({"type": "ready"})

        while True:
            message = control_queue.get()
            kind = message.get("type")
            if kind == "shutdown":
                return 0
            if kind not in {"start", "start_external"}:
                continue
            try:
                if kind == "start_external":
                    final_text = _run_external_audio_session(runtime, prepared_recognizer, message, control_queue)
                else:
                    final_text = _run_prepared_session(runtime, prepared_recognizer, message, control_queue)
                _emit({"type": "final", "text": final_text})
            except Exception as exc:
                _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
    except Exception as exc:
        _emit({"type": "error", "message": f"{exc}\n\n{traceback.format_exc(limit=5)}"})
        return 1


def _run_prepared_session(
    runtime: SherpaOnnxStreamingRecognizer,
    prepared_recognizer: Any,
    config: dict[str, Any],
    control_queue: queue.Queue[dict[str, Any]],
) -> str:
    stop_event = threading.Event()
    pause_event = threading.Event()
    session_done = threading.Event()
    control_thread = threading.Thread(
        target=_apply_session_controls,
        args=(control_queue, stop_event, pause_event, session_done),
        name="voice-input-streaming-session-control",
        daemon=True,
    )
    control_thread.start()

    last_text = ""
    last_committed = ""

    def on_event(event: StreamingRecognitionEvent) -> None:
        nonlocal last_text, last_committed
        committed_update = (
            {"committed_append": event.committed_text[len(last_committed):]}
            if event.committed_text.startswith(last_committed)
            else {"committed_reset": event.committed_text}
        )
        _emit(
            {
                "type": "event",
                "text_patch": _text_patch(last_text, event.text),
                "partial_text": event.partial_text,
                "is_final": event.is_final,
                "is_endpoint": event.is_endpoint,
                **committed_update,
            }
        )
        last_text = event.text
        last_committed = event.committed_text

    try:
        return runtime.transcribe_microphone_with_prepared_recognizer(
            prepared_recognizer,
            max_duration_seconds=float(config.get("max_duration_seconds", 7200)),
            stop_event=stop_event,
            pause_event=pause_event,
            on_event=on_event,
            hotwords=tuple(config.get("hotwords") or ()),
        )
    finally:
        session_done.set()


def _create_runtime(config: dict[str, Any]) -> SherpaOnnxStreamingRecognizer:
    return SherpaOnnxStreamingRecognizer(
        str(config.get("model_dir", "")),
        sample_rate=int(config.get("sample_rate", 16000)),
        channels=int(config.get("channels", 1)),
        chunk_ms=int(config.get("chunk_ms", 100)),
        update_interval_ms=int(config.get("update_interval_ms", 120)),
        num_threads=int(config.get("num_threads", 2)),
        device=config.get("device"),
        decoding_method=str(config.get("decoding_method", "modified_beam_search")),
    )


def _run_external_audio_session(
    runtime: SherpaOnnxStreamingRecognizer,
    prepared_recognizer: Any,
    config: dict[str, Any],
    control_queue: queue.Queue[dict[str, Any]],
) -> str:
    import numpy as np

    hotword_text = ""
    if runtime.supports_hotwords:
        hotword_text = _format_hotwords(tuple(config.get("hotwords") or ()))
    stream = _create_stream(prepared_recognizer, hotword_text)
    committed_parts: list[str] = []
    last_emitted = ""
    last_emitted_committed = ""

    def current_text(partial: str = "") -> str:
        parts = [part for part in (*committed_parts, partial.strip()) if part]
        return "".join(parts).strip()

    def emit(partial: str = "", *, final: bool = False, endpoint: bool = False, force: bool = False) -> None:
        nonlocal last_emitted, last_emitted_committed
        text = current_text(partial)
        if not force and not final and text == last_emitted:
            return
        committed = "".join(committed_parts)
        committed_update = (
            {"committed_append": committed[len(last_emitted_committed):]}
            if committed.startswith(last_emitted_committed)
            else {"committed_reset": committed}
        )
        patch = _text_patch(last_emitted, text)
        last_emitted = text
        last_emitted_committed = committed
        _emit(
            {
                "type": "event",
                "text_patch": patch,
                "partial_text": partial.strip(),
                "is_final": final,
                "is_endpoint": endpoint,
                **committed_update,
            }
        )

    def decode_pending(*, force_emit: bool = False) -> None:
        while prepared_recognizer.is_ready(stream):
            prepared_recognizer.decode_stream(stream)
        partial = prepared_recognizer.get_result(stream).strip()
        emit(partial, force=force_emit)
        if prepared_recognizer.is_endpoint(stream):
            endpoint_text = prepared_recognizer.get_result(stream).strip()
            if endpoint_text:
                committed_parts.append(endpoint_text)
                emit("", endpoint=True, force=True)
            prepared_recognizer.reset(stream)

    while True:
        message = control_queue.get()
        kind = message.get("type")
        if kind == "audio":
            raw = base64.b64decode(str(message.get("pcm", "")))
            if not raw:
                continue
            samples = np.frombuffer(raw, dtype=np.int16)
            channels = max(1, int(message.get("channels") or runtime.channels))
            if channels > 1 and samples.size >= channels:
                samples = samples[: samples.size - (samples.size % channels)].reshape(-1, channels)
            float_samples = _to_float32_mono(samples, np)
            if float_samples.size:
                stream.accept_waveform(runtime.sample_rate, float_samples)
                decode_pending()
        elif kind == "stop":
            tail = np.zeros(int(0.35 * runtime.sample_rate), dtype=np.float32)
            stream.accept_waveform(runtime.sample_rate, tail)
            stream.input_finished()
            decode_pending(force_emit=True)
            final_partial = prepared_recognizer.get_result(stream).strip()
            if final_partial:
                committed_parts.append(final_partial)
            final_text = "".join(committed_parts).strip()
            emit("", final=True, force=True)
            return final_text
        elif kind == "shutdown":
            return ""


def _read_queued_controls(control_queue: queue.Queue[dict[str, Any]]) -> None:
    for line in sys.stdin:
        try:
            message: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            control_queue.put(message)


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


def _apply_session_controls(
    control_queue: queue.Queue[dict[str, Any]],
    stop_event: threading.Event,
    pause_event: threading.Event,
    session_done: threading.Event,
) -> None:
    while not session_done.is_set():
        try:
            message = control_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        kind = message.get("type")
        if kind == "stop":
            stop_event.set()
            pause_event.clear()
            return
        if kind == "pause":
            pause_event.set()
        elif kind == "resume":
            pause_event.clear()


def _read_controls(stop_event: threading.Event, pause_event: threading.Event) -> None:
    for line in sys.stdin:
        try:
            message: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        kind = message.get("type")
        if kind == "stop":
            stop_event.set()
            pause_event.clear()
            return
        if kind == "pause":
            pause_event.set()
        elif kind == "resume":
            pause_event.clear()


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
