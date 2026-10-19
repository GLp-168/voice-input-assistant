"""Persistent process worker for cancellable FunASR offline recognition."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback
from typing import Any, Mapping

from .asr import ASRResult, FunASROfflineEngine


def main() -> int:
    engine: FunASROfflineEngine | None = None
    config_signature = ""
    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        if request.get("type") == "shutdown":
            return 0
        if request.get("type") != "transcribe":
            continue

        request_id = str(request.get("request_id", ""))
        try:
            config = request.get("config")
            if not isinstance(config, dict):
                raise ValueError("FunASR worker configuration is invalid")
            next_signature = json.dumps(config, sort_keys=True, ensure_ascii=True)
            if engine is None or next_signature != config_signature:
                engine = FunASROfflineEngine(
                    model=str(config.get("model", "")),
                    vad_model=str(config.get("vad_model", "")),
                    punc_model=str(config.get("punc_model", "")),
                    spk_model=str(config.get("spk_model", "")),
                    device=str(config.get("device", "cpu")),
                    num_threads=max(1, int(config.get("num_threads", 4))),
                    batch_size_s=max(1, int(config.get("batch_size_s", 300))),
                    file_chunk_seconds=max(0.1, float(config.get("file_chunk_seconds", 60.0))),
                )
                config_signature = next_signature
            audio_path = Path(str(request.get("audio_path", "")))
            if not audio_path.is_file():
                raise FileNotFoundError(f"audio file not found: {audio_path}")
            hotwords = request.get("hotwords")
            result = engine._transcribe_local(
                audio_path,
                language=request.get("language") if isinstance(request.get("language"), str) else None,
                hotwords=hotwords if isinstance(hotwords, list) else (),
            )
            _emit(
                {
                    "type": "result",
                    "request_id": request_id,
                    "result": _result_payload(result),
                }
            )
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            _emit({"type": "error", "request_id": request_id, "message": str(exc)})
    return 0


def _result_payload(result: ASRResult) -> dict[str, Any]:
    return {
        "text": result.text,
        "engine": result.engine,
        "language": result.language,
        "confidence": result.confidence,
        "duration_seconds": result.duration_seconds,
        "segments": _json_safe(result.segments),
        "metadata": _json_safe(result.metadata),
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    return str(value)


def _emit(message: Mapping[str, Any]) -> None:
    print(json.dumps(dict(message), ensure_ascii=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
