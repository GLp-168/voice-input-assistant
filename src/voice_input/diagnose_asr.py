"""Command-line ASR diagnostics for target-machine POC."""

from __future__ import annotations

import argparse
import contextlib
import sys

from .asr import ASREngineError, DummyASREngine, FunASROfflineEngine, WhisperCppEngine
from .audio import AudioChunk
from .funasr_compat import patch_funasr_runtime, should_retry_after_funasr_symbol_error
from .resources import (
    bundled_funasr_offline_model,
    bundled_funasr_punc_model,
    bundled_funasr_streaming_model,
    bundled_funasr_vad_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose offline ASR backend availability.")
    parser.add_argument("--backend", default="funasr", help="funasr, whisper.cpp, or dummy")
    parser.add_argument(
        "--model",
        default="",
        help="Optional whisper.cpp model file when --backend whisper.cpp is selected",
    )
    parser.add_argument(
        "--executable",
        default="whisper-cli",
        help="Path/name of whisper.cpp executable",
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Also validate FunASR streaming/VAD/punctuation model loading",
    )
    args = parser.parse_args(argv)

    backend = args.backend.strip().lower()
    if backend in {"funasr", "paraformer", "funasr-offline", "local"}:
        engine = FunASROfflineEngine(
            model=bundled_funasr_offline_model(),
            vad_model=bundled_funasr_vad_model(),
            punc_model=bundled_funasr_punc_model(),
        )
    elif backend in {"whisper", "whisper.cpp", "whisper-cpp"}:
        engine = WhisperCppEngine(model_path=args.model, executable=args.executable, timeout_seconds=30)
    else:
        engine = DummyASREngine()

    try:
        result = engine.transcribe(AudioChunk(pcm=b"\x00" * 3200), language="zh")
    except ASREngineError as exc:
        print(f"ASR diagnostic failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ASR diagnostic crashed: {exc}", file=sys.stderr)
        return 3

    print(f"engine={result.engine}")
    print(f"language={result.language or ''}")
    print(f"text={result.text}")
    if args.streaming:
        if not _diagnose_streaming():
            return 4
    return 0


def _diagnose_streaming() -> bool:
    try:
        with _redirect_stdout_to_stderr():
            from funasr import AutoModel
            patch_funasr_runtime()

        streaming_model = bundled_funasr_streaming_model()
        offline_model = bundled_funasr_offline_model()
        vad_model = bundled_funasr_vad_model()
        punc_model = bundled_funasr_punc_model()
        print("streaming_backend=funasr")
        print(f"streaming_model={streaming_model}")
        print(f"streaming_review_model={offline_model}")
        print(f"streaming_vad_model={vad_model}")
        print(f"streaming_punc_model={punc_model}")
        print("streaming_hotwords_supported=true")
        print("streaming_vad_supported=true")
        print("streaming_punctuation_supported=true")
        with _redirect_stdout_to_stderr():
            AutoModel(model=streaming_model, device="cpu", disable_update=True, disable_pbar=True, ncpu=1)
            try:
                AutoModel(
                    model=offline_model,
                    vad_model=vad_model,
                    punc_model=punc_model,
                    vad_kwargs={"max_single_segment_time": 60000},
                    device="cpu",
                    disable_update=True,
                    disable_pbar=True,
                    ncpu=1,
                )
            except Exception as exc:
                if not should_retry_after_funasr_symbol_error(exc):
                    raise
                patch_funasr_runtime()
                AutoModel(
                    model=offline_model,
                    vad_model=vad_model,
                    punc_model=punc_model,
                    vad_kwargs={"max_single_segment_time": 60000},
                    device="cpu",
                    disable_update=True,
                    disable_pbar=True,
                    ncpu=1,
                )
        print("streaming_ready=true")
        return True
    except Exception as exc:
        print("streaming_ready=false", file=sys.stderr)
        print(f"streaming_error={exc}", file=sys.stderr)
        return False


@contextlib.contextmanager
def _redirect_stdout_to_stderr():
    with contextlib.redirect_stdout(sys.stderr):
        yield


if __name__ == "__main__":
    raise SystemExit(main())
