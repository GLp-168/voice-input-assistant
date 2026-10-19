"""Runtime health checks for installed VoiceInput deployments."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .audio import AudioCaptureError, list_microphones, record_segment
from .config import DEFAULT_SAMPLE_RATE
from .funasr_compat import (
    funasr_runtime_diagnostics,
    patch_funasr_runtime,
    should_retry_after_funasr_symbol_error,
)
from .lexicon import Lexicon
from .logging_config import log_file
from .media import find_ffmpeg_executable
from .resources import (
    DEFAULT_FUNASR_OFFLINE_MODEL_DIR,
    DEFAULT_FUNASR_PUNC_MODEL_DIR,
    DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
    DEFAULT_FUNASR_STREAMING_MODEL_DIR,
    DEFAULT_FUNASR_VAD_MODEL_DIR,
    bundled_funasr_model_root,
    bundled_funasr_offline_model,
    bundled_funasr_punc_model,
    bundled_funasr_speaker_model,
    bundled_funasr_streaming_model,
    bundled_funasr_vad_model,
    bundled_lexicon_catalog,
    bundled_lexicon_paths,
    funasr_model_file_status,
    has_bundled_funasr_models,
    has_bundled_funasr_review_models,
    has_bundled_funasr_speaker_model,
    has_bundled_funasr_streaming_model,
    packaged_funasr_model_dirs,
    packaged_funasr_model_roles,
    packaged_enabled_builtin_lexicons,
    packaged_model_profile,
    packaged_product_tier,
)
from .settings import AppSettings, SettingsStore


@dataclass(frozen=True, slots=True)
class HealthCheckItem:
    name: str
    status: str
    message: str = ""
    details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str
    generated_at: float
    items: tuple[HealthCheckItem, ...]


CheckFn = Callable[[AppSettings, bool], HealthCheckItem]


def run_health_checks(settings: AppSettings | None = None, *, microphone_smoke: bool = False) -> HealthReport:
    active_settings = settings or SettingsStore().load()
    checks: tuple[CheckFn, ...] = (
        _check_runtime,
        _check_offline_bundle,
        _check_streaming_bundle,
        _check_audio_io,
        _check_streaming_load,
        _check_microphones,
        _check_ffmpeg,
        _check_lexicons,
        _check_logs,
    )
    items = tuple(_safe_check(check, active_settings, microphone_smoke) for check in checks)
    status = "ok"
    if any(item.status == "error" for item in items):
        status = "error"
    elif any(item.status == "warning" for item in items):
        status = "warning"
    return HealthReport(status=status, generated_at=time.time(), items=items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check VoiceInput runtime health.")
    parser.add_argument("--settings", help="Optional settings.json path")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--microphone-smoke", action="store_true", help="Record a 0.2s microphone smoke test")
    parser.add_argument("--output", help="Write the report to this file instead of stdout")
    parser.add_argument("--open", action="store_true", help="Open the output file after writing it")
    args = parser.parse_args(argv)

    settings = SettingsStore(Path(args.settings)).load() if args.settings else SettingsStore().load()
    report = run_health_checks(settings, microphone_smoke=args.microphone_smoke)
    if args.json:
        content = json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2) + "\n"
    else:
        lines = [f"status={report.status}"]
        for item in report.items:
            line = f"{item.status.upper()}\t{item.name}"
            if item.message:
                line += f"\t{item.message}"
            lines.append(line)
        content = "\n".join(lines) + "\n"
    if args.output:
        output_path = Path(os.path.expandvars(args.output)).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        if args.open:
            _open_path(output_path)
    else:
        _write_stdout(content)
    return 0 if report.status in {"ok", "warning"} else 2


def _safe_check(check: CheckFn, settings: AppSettings, microphone_smoke: bool) -> HealthCheckItem:
    try:
        return check(settings, microphone_smoke)
    except Exception as exc:
        details: dict[str, object] = {"traceback": traceback.format_exc(limit=8)}
        if check.__name__ == "_check_streaming_load":
            details["funasr_runtime"] = funasr_runtime_diagnostics()
        return HealthCheckItem(
            check.__name__.removeprefix("_check_"),
            "error",
            str(exc),
            details,
        )


def _check_runtime(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    return HealthCheckItem(
        "runtime",
        "ok",
        f"Python {platform.python_version()} on {platform.system()} {platform.release()}",
        {
            "executable": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "model_profile": packaged_model_profile(),
        },
    )


def _check_offline_bundle(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    model_root = bundled_funasr_model_root()
    roles = packaged_funasr_model_roles()
    offline_dir = roles.get("offline", "")
    vad_dir = roles.get("vad", "")
    punc_dir = roles.get("punc", "")
    speaker_dir = roles.get("speaker", "")
    offline_model = model_root / (offline_dir or DEFAULT_FUNASR_OFFLINE_MODEL_DIR)
    vad_model = model_root / (vad_dir or DEFAULT_FUNASR_VAD_MODEL_DIR)
    punc_model = model_root / (punc_dir or DEFAULT_FUNASR_PUNC_MODEL_DIR)
    speaker_model = model_root / (speaker_dir or DEFAULT_FUNASR_SPEAKER_MODEL_DIR)
    advertised = bool(offline_dir or vad_dir or punc_dir or speaker_dir)
    review_ok = has_bundled_funasr_review_models(require_punctuation=bool(punc_dir))
    speaker_ok = not speaker_dir or has_bundled_funasr_speaker_model()
    ok = review_ok and speaker_ok
    status = "ok" if ok else "warning" if not advertised else "error"
    return HealthCheckItem(
        "offline_bundle",
        status,
        "bundled FunASR offline/VAD/punctuation/speaker models are available"
        if ok
        else "this package does not include offline review resources"
        if not advertised
        else "missing bundled FunASR offline resources",
        {
            "backend": "funasr",
            "model_profile": packaged_model_profile(),
            "model_root": str(model_root),
            "offline_model": {
                "path": str(offline_model),
                "exists": offline_model.is_dir(),
                "required_files": funasr_model_file_status(offline_model, offline_dir or DEFAULT_FUNASR_OFFLINE_MODEL_DIR),
            },
            "vad_model": {
                "path": str(vad_model),
                "exists": vad_model.is_dir(),
                "required_files": funasr_model_file_status(vad_model, vad_dir or DEFAULT_FUNASR_VAD_MODEL_DIR),
            },
            "punc_model": {
                "path": str(punc_model),
                "exists": punc_model.is_dir(),
                "required_files": funasr_model_file_status(punc_model, punc_dir or DEFAULT_FUNASR_PUNC_MODEL_DIR),
            },
            "speaker_model": {
                "path": str(speaker_model),
                "exists": speaker_model.is_dir(),
                "required_files": funasr_model_file_status(
                    speaker_model,
                    speaker_dir or DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
                ),
            },
        },
    )


def _check_streaming_bundle(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    model_root = bundled_funasr_model_root()
    model_dirs = {dirname: model_root / dirname for dirname in packaged_funasr_model_dirs()}
    ok = has_bundled_funasr_models()
    return HealthCheckItem(
        "streaming_bundle",
        "ok" if ok else "error",
        "bundled FunASR package models are available" if ok else "missing bundled FunASR models",
        {
            "backend": "funasr",
            "model_profile": packaged_model_profile(),
            "model_root": str(model_root),
            "model_root_exists": model_root.is_dir(),
            "streaming_model": packaged_funasr_model_roles().get("streaming", DEFAULT_FUNASR_STREAMING_MODEL_DIR),
            "offline_model": packaged_funasr_model_roles().get("offline", DEFAULT_FUNASR_OFFLINE_MODEL_DIR),
            "vad_model": packaged_funasr_model_roles().get("vad", DEFAULT_FUNASR_VAD_MODEL_DIR),
            "punc_model": packaged_funasr_model_roles().get("punc", DEFAULT_FUNASR_PUNC_MODEL_DIR),
            "speaker_model": packaged_funasr_model_roles().get("speaker", DEFAULT_FUNASR_SPEAKER_MODEL_DIR),
            "model_dirs": {
                name: {
                    "path": str(path),
                    "exists": path.is_dir(),
                    "required_files": funasr_model_file_status(path, name),
                }
                for name, path in model_dirs.items()
            },
        },
    )


def _check_streaming_load(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    import numpy as np

    with _redirect_stdout_to_stderr():
        from funasr import AutoModel
        patch_funasr_runtime()

    streaming_model = bundled_funasr_streaming_model()
    offline_model = bundled_funasr_offline_model()
    vad_model = bundled_funasr_vad_model()
    punc_model = bundled_funasr_punc_model()
    speaker_model = bundled_funasr_speaker_model()
    if not has_bundled_funasr_streaming_model():
        return HealthCheckItem(
            "streaming_load",
            "error",
            "missing bundled FunASR streaming model",
            {"backend": "funasr", "streaming_model": streaming_model},
        )
    supports_review = has_bundled_funasr_review_models()
    supports_punctuation = has_bundled_funasr_review_models(require_punctuation=True)
    supports_speaker = has_bundled_funasr_speaker_model()
    speaker_clustering_smoke = False
    started = time.perf_counter()
    offline_runtime = None
    with _redirect_stdout_to_stderr():
        streaming_runtime = AutoModel(
            model=streaming_model,
            device="cpu",
            disable_update=True,
            disable_pbar=True,
            ncpu=1,
        )
        if supports_review:
            offline_kwargs = {
                "model": offline_model,
                "vad_model": vad_model or None,
                "punc_model": punc_model or None,
                "spk_model": speaker_model or None,
                "vad_kwargs": {"max_single_segment_time": 60000},
                "device": "cpu",
                "disable_update": True,
                "disable_pbar": True,
                "ncpu": 1,
            }
            offline_kwargs = {key: value for key, value in offline_kwargs.items() if value is not None}
            try:
                offline_runtime = AutoModel(**offline_kwargs)
            except Exception as exc:
                if not should_retry_after_funasr_symbol_error(exc):
                    raise
                patch_funasr_runtime()
                offline_runtime = AutoModel(**offline_kwargs)

        silence = np.zeros(9600, dtype=np.float32)
        streaming_runtime.generate(
            input=silence,
            cache={},
            is_final=True,
            chunk_size=[0, 10, 5],
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        if offline_runtime is not None:
            offline_runtime.generate(input=silence, batch_size_s=1)
        if supports_speaker:
            cluster_backend = getattr(offline_runtime, "cb_model", None)
            if cluster_backend is None:
                raise RuntimeError("speaker model loaded without a clustering backend")
            first_speaker = np.tile(np.asarray([[1.0, 0.0]], dtype=np.float32), (12, 1))
            second_speaker = np.tile(np.asarray([[0.0, 1.0]], dtype=np.float32), (12, 1))
            labels = np.asarray(cluster_backend(np.vstack((first_speaker, second_speaker)))).reshape(-1)
            if labels.size != 24 or np.unique(labels).size < 2:
                raise RuntimeError("speaker clustering smoke test did not separate synthetic speakers")
            speaker_clustering_smoke = True
    elapsed = time.perf_counter() - started
    return HealthCheckItem(
        "streaming_load",
        "ok",
        f"loaded and exercised FunASR streaming{' and review' if supports_review else ''} models in {elapsed:.2f}s",
        {
            "backend": "funasr",
            "streaming_model": streaming_model,
            "offline_model": offline_model,
            "vad_model": vad_model,
            "punc_model": punc_model,
            "speaker_model": speaker_model,
            "supports_native_hotwords": True,
            "supports_vad": supports_review,
            "supports_punctuation": supports_punctuation,
            "supports_speaker": supports_speaker,
            "speaker_clustering_smoke": speaker_clustering_smoke,
            "supports_background_review": supports_review,
            "smoke_inference": True,
            "elapsed_seconds": elapsed,
        },
    )


def _check_audio_io(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    import numpy as np
    import soundfile as sf

    from .asr import _resample_float32

    samples = np.linspace(-0.25, 0.25, 1600, dtype=np.float32)
    with tempfile.TemporaryDirectory(prefix="voice-input-health-audio-") as temp_dir:
        path = Path(temp_dir) / "roundtrip.wav"
        sf.write(str(path), samples, DEFAULT_SAMPLE_RATE, format="WAV", subtype="PCM_16")
        decoded, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    decoded_array = np.asarray(decoded, dtype=np.float32).reshape(-1)
    if int(sample_rate) != DEFAULT_SAMPLE_RATE or decoded_array.size != samples.size:
        raise RuntimeError("SoundFile WAV round-trip returned unexpected audio metadata")
    if not np.isfinite(decoded_array).all() or float(np.max(np.abs(decoded_array))) <= 0.0:
        raise RuntimeError("SoundFile WAV round-trip returned invalid samples")
    formats = sf.available_formats()
    if "WAV" not in formats:
        raise RuntimeError("libsndfile does not report WAV support")
    source_44k = np.linspace(-0.25, 0.25, 4410, dtype=np.float32)
    resampled = _resample_float32(source_44k, 44100, DEFAULT_SAMPLE_RATE)
    if not 1590 <= int(resampled.size) <= 1610 or not np.isfinite(resampled).all():
        raise RuntimeError("SoXR audio resampling returned invalid samples")
    return HealthCheckItem(
        "audio_io",
        "ok",
        "SoundFile WAV read/write round-trip passed",
        {
            "soundfile_version": str(getattr(sf, "__version__", "")),
            "libsndfile_version": str(getattr(sf, "__libsndfile_version__", "")),
            "sample_rate": int(sample_rate),
            "frames": int(decoded_array.size),
            "resampled_frames": int(resampled.size),
        },
    )


def _check_microphones(_settings: AppSettings, microphone_smoke: bool) -> HealthCheckItem:
    try:
        devices = list_microphones()
    except AudioCaptureError as exc:
        return HealthCheckItem("microphones", "error", str(exc))
    details: dict[str, object] = {
        "count": len(devices),
        "devices": [asdict(device) for device in devices[:10]],
    }
    if not devices:
        return HealthCheckItem("microphones", "warning", "no input devices found", details)
    if microphone_smoke:
        chunk = record_segment(0.2, sample_rate=DEFAULT_SAMPLE_RATE)
        details["smoke_duration_seconds"] = chunk.duration_seconds
        details["smoke_bytes"] = len(chunk.pcm)
    return HealthCheckItem("microphones", "ok", f"{len(devices)} input device(s)", details)


def _check_ffmpeg(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    if packaged_model_profile() == "streaming":
        return HealthCheckItem(
            "ffmpeg",
            "ok",
            "not required by this streaming-only package",
            {"path": "", "required": False},
        )
    ffmpeg = find_ffmpeg_executable()
    return HealthCheckItem(
        "ffmpeg",
        "ok" if ffmpeg else "warning",
        ffmpeg or "ffmpeg not found; some imported formats may be unavailable",
        {"path": ffmpeg or ""},
    )


def _check_lexicons(settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    product_tier = packaged_product_tier()
    available = {
        item.id for item in bundled_lexicon_catalog(product_tier=product_tier)
    } & set(packaged_enabled_builtin_lexicons())
    selected = tuple(item for item in settings.enabled_builtin_lexicons if item in available)
    paths = bundled_lexicon_paths(selected, product_tier=product_tier)
    custom = settings.lexicon_path.strip()
    details: dict[str, object] = {
        "enabled": settings.lexicon_enabled,
        "product_tier": product_tier,
        "builtin_selected": list(selected),
        "builtin_available": [
            item.id
            for item in bundled_lexicon_catalog(product_tier=product_tier)
            if item.id in available
        ],
        "builtin_paths": [str(path) for path in paths],
        "custom_path": custom,
    }
    if settings.lexicon_enabled and not paths and not custom:
        return HealthCheckItem("lexicons", "warning", "lexicon enabled but no lexicon files are selected", details)
    if custom:
        try:
            lexicon = Lexicon.from_file(custom)
            details["custom_rules"] = len(lexicon.rules)
        except Exception as exc:
            return HealthCheckItem("lexicons", "error", f"custom lexicon failed: {exc}", details)
    return HealthCheckItem("lexicons", "ok", f"{len(paths)} bundled lexicon file(s)", details)


def _check_logs(_settings: AppSettings, _microphone_smoke: bool) -> HealthCheckItem:
    path = log_file()
    return HealthCheckItem(
        "logs",
        "ok" if path.parent.exists() else "warning",
        str(path),
        {"path": str(path), "exists": path.is_file()},
    )


def _report_to_dict(report: HealthReport) -> dict[str, object]:
    return {
        "status": report.status,
        "generated_at": report.generated_at,
        "items": [asdict(item) for item in report.items],
    }


def _write_stdout(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@contextlib.contextmanager
def _redirect_stdout_to_stderr():
    with contextlib.redirect_stdout(sys.stderr):
        yield


if __name__ == "__main__":
    raise SystemExit(main())
