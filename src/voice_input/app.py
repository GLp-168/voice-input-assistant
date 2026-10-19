"""Desktop application orchestration for the offline voice input assistant."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import Parameter, signature
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Protocol

from .audio import AudioLevel, AudioLevelCallback, MicrophoneState, MicrophoneStateCallback
from .config import (
    DEFAULT_MAX_IMPORT_AUDIO_SECONDS,
    DEFAULT_MAX_RECORDING_SECONDS,
    DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS,
)
from .hotkeys import GlobalHotkeyManager
from .language import LANGUAGE_OPTIONS, engine_language_code, normalize_transcript_text, punctuate_transcript_text
from .lexicon import Lexicon
from .logging_config import configure_logging, log_file
from .media import (
    AudioTranscodeCancelled,
    is_supported_audio_file,
    probe_audio_duration_seconds,
    requires_audio_transcode,
    supported_audio_format_names,
    transcode_audio_to_wav,
)
from .resources import (
    DEFAULT_FUNASR_COMPACT_PUNC_MODEL_DIR,
    DEFAULT_FUNASR_OFFLINE_MODEL_DIR,
    DEFAULT_FUNASR_PUNC_MODEL_DIR,
    DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
    DEFAULT_FUNASR_STREAMING_MODEL_DIR,
    DEFAULT_FUNASR_VAD_MODEL_DIR,
    bundled_funasr_offline_model,
    bundled_funasr_punc_model,
    bundled_funasr_speaker_model,
    bundled_funasr_streaming_model,
    bundled_funasr_vad_model,
    bundled_lexicon_catalog,
    bundled_lexicon_paths,
    bundled_sherpa_streaming_model_dir,
    has_bundled_funasr_review_models,
    has_bundled_funasr_streaming_model,
    has_bundled_sherpa_streaming_model,
    has_bundled_whisper,
    packaged_enabled_builtin_lexicons,
    packaged_model_profile,
    packaged_product_tier,
)
from .resource_policy import (
    ResourceDecision,
    SessionResourcePolicy,
    capture_system_resource_snapshot,
)
from .settings import AppSettings, SettingsStore
from .status import RunState, RuntimeStatus
from .transcript_history import TranscriptHistoryStore
from .tray import TrayController
from .windows_support import clear_run_on_startup, is_run_on_startup_enabled, set_run_on_startup


LOGGER = logging.getLogger(__name__)
_DEFAULT_TRANSCRIPTION_TIMEOUT = object()
_WORKER_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_STREAMING_UI_INTERVAL_SECONDS = 0.2
_LEXICON_CACHE_LOCK = threading.Lock()
_LEXICON_CACHE_KEY: tuple[object, ...] | None = None
_LEXICON_CACHE_VALUE: Lexicon | None = None


@dataclass(slots=True)
class _CycleControls:
    partial_stop: threading.Event = field(default_factory=threading.Event)
    streaming_stop: threading.Event = field(default_factory=threading.Event)
    streaming_pause: threading.Event = field(default_factory=threading.Event)
    recognition_cancel: threading.Event = field(default_factory=threading.Event)


class UiPort(Protocol):
    """Small interface implemented by the desktop runtime UI."""

    def set_status(self, text: str) -> None:
        ...

    def append_text(self, text: str) -> None:
        ...

    def begin_streaming_text(self) -> None:
        ...

    def set_streaming_text(self, text: str) -> None:
        ...

    def commit_streaming_text(self, text: str) -> None:
        ...

    def set_running(self, running: bool) -> None:
        ...

    def set_recording(self, recording: bool) -> None:
        ...

    def set_audio_level(self, level: float) -> None:
        ...

    def set_microphone_state(self, state: dict[str, object]) -> None:
        ...

    def set_session_history(self, sessions: list[dict[str, object]]) -> None:
        ...

    def set_transcript_session(self, session: dict[str, object]) -> None:
        ...

    def show_error(self, title: str, message: str) -> None:
        ...

    def after(self, delay_ms: int, callback: Callable[[], None]) -> None:
        ...

    def show_window(self) -> None:
        ...

    def hide_window(self) -> None:
        ...

    def close_window(self) -> None:
        ...

    def show_info(self, title: str, message: str) -> None:
        ...

    def set_autostart(self, enabled: bool) -> None:
        ...


class ASREnginePort(Protocol):
    """Runtime ASR interface accepted by the desktop shell."""

    def transcribe(self, audio: object, **options: object) -> object:
        ...


class UnavailableASREngine:
    """Friendly placeholder used when the ASR layer is not importable yet."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause

    def transcribe(self, audio: object, **options: object) -> object:
        raise RuntimeError(
            "ASR 模块尚未接入或初始化失败。桌面壳已启动，请等待 ASR worker "
            f"提供兼容的 transcribe 接口。原始错误：{self.cause}"
        ) from self.cause


class NullInjector:
    """Fallback injector used until platform injection is enabled."""

    def inject(self, text: str) -> None:
        return None


class FixedDurationRecorder:
    """Records one short utterance for the MVP cycle."""

    def __init__(
        self,
        duration_seconds: float = DEFAULT_MAX_RECORDING_SECONDS,
        *,
        allow_fallback_silence: bool = False,
    ) -> None:
        self.duration_seconds = duration_seconds
        self.allow_fallback_silence = allow_fallback_silence
        self._active_recorder: object | None = None
        self._completed_recorder: object | None = None
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._accept_early_stop = False
        self._recording_active = False
        self._lock = threading.Lock()
        self._audio_level_callback: AudioLevelCallback | None = None
        self._microphone_state_callback: MicrophoneStateCallback | None = None

    def set_audio_monitor(
        self,
        on_level: AudioLevelCallback | None,
        on_state: MicrophoneStateCallback | None = None,
    ) -> None:
        with self._lock:
            self._audio_level_callback = on_level
            self._microphone_state_callback = on_state
            recorder = self._active_recorder
        set_monitor = getattr(recorder, "set_audio_monitor", None)
        if callable(set_monitor):
            set_monitor(on_level, on_state)

    def prepare_recording(self) -> None:
        with self._lock:
            self._stop_requested.clear()
            self._pause_requested.clear()
            self._accept_early_stop = True
            self._recording_active = False
            self._completed_recorder = None

    def record_once(self) -> object:
        from .audio import AudioCaptureError, AudioChunk, MicrophoneRecorder

        recorder = MicrophoneRecorder(max_duration_seconds=self.duration_seconds)
        with self._lock:
            set_monitor = getattr(recorder, "set_audio_monitor", None)
            if callable(set_monitor):
                set_monitor(self._audio_level_callback, self._microphone_state_callback)
            if self._stop_requested.is_set():
                recorder.stop()
                self._stop_requested.clear()
            if self._pause_requested.is_set():
                pause = getattr(recorder, "pause", None)
                if callable(pause):
                    pause()
            self._active_recorder = recorder
            self._recording_active = True
        try:
            return recorder.record_until_stopped()
        except AudioCaptureError:
            if self.allow_fallback_silence:
                LOGGER.warning("audio capture failed, falling back to silent demo audio", exc_info=True)
                return AudioChunk(pcm=b"\x00" * 3200)
            raise
        finally:
            with self._lock:
                if self._active_recorder is recorder:
                    self._completed_recorder = recorder
                    self._active_recorder = None
                self._accept_early_stop = False
                self._recording_active = False
                self._stop_requested.clear()
                self._pause_requested.clear()

    def stop(self) -> None:
        with self._lock:
            recorder = self._active_recorder
            should_hold_stop = self._accept_early_stop and not self._recording_active
        stop = getattr(recorder, "stop", None)
        if callable(stop):
            stop()
            return
        if should_hold_stop:
            self._stop_requested.set()

    def pause(self) -> None:
        with self._lock:
            recorder = self._active_recorder
            should_hold_pause = self._accept_early_stop and not self._recording_active
        pause = getattr(recorder, "pause", None)
        if callable(pause):
            pause()
            return
        if should_hold_pause:
            self._pause_requested.set()

    def resume(self) -> None:
        with self._lock:
            recorder = self._active_recorder
            self._pause_requested.clear()
        resume = getattr(recorder, "resume", None)
        if callable(resume):
            resume()

    def snapshot_since_last(self) -> object | None:
        with self._lock:
            recorder = self._active_recorder or self._completed_recorder
        snapshot = getattr(recorder, "snapshot_since_last", None)
        if callable(snapshot):
            return snapshot()
        return None


def load_default_asr_engine() -> ASREnginePort:
    """Load the default ASR engine lazily so UI import stays conflict-free."""

    try:
        from .asr import DummyASREngine

        return DummyASREngine()
    except Exception as exc:
        return UnavailableASREngine(exc)


def build_asr_engine(settings: AppSettings) -> ASREnginePort:
    settings = resolve_runtime_settings(settings)
    backend = settings.asr_backend.strip().lower()
    if backend in {"funasr", "paraformer", "funasr-offline", "local"}:
        try:
            from .asr import FunASROfflineEngine

            return FunASROfflineEngine(
                model=settings.funasr_offline_model,
                vad_model=settings.funasr_vad_model,
                punc_model=settings.funasr_punc_model,
                spk_model=settings.funasr_speaker_model,
            )
        except Exception as exc:
            return UnavailableASREngine(exc)
    if backend in {"whisper", "whisper.cpp", "whisper-cpp"}:
        try:
            from .asr import WhisperCppEngine

            return WhisperCppEngine(
                model_path=settings.whisper_model_path,
                executable=settings.whisper_executable or "whisper-cli",
            )
        except Exception as exc:
            return UnavailableASREngine(exc)
    if has_bundled_funasr_review_models():
        try:
            from .asr import FunASROfflineEngine

            return FunASROfflineEngine(
                model=settings.funasr_offline_model,
                vad_model=settings.funasr_vad_model,
                punc_model=settings.funasr_punc_model,
                spk_model=settings.funasr_speaker_model,
            )
        except Exception:
            LOGGER.warning("bundled FunASR unavailable, falling back to legacy engines", exc_info=True)
    if has_bundled_whisper():
        try:
            from .asr import WhisperCppEngine

            from .resources import bundled_whisper_executable, bundled_whisper_model

            return WhisperCppEngine(
                model_path=str(bundled_whisper_model()),
                executable=str(bundled_whisper_executable()),
            )
        except Exception:
            LOGGER.warning("bundled whisper.cpp unavailable, falling back to dummy", exc_info=True)
    return load_default_asr_engine()


def resolve_runtime_settings(settings: AppSettings) -> AppSettings:
    """Prefer bundled offline ASR resources when they are available."""

    data = settings.to_dict()
    product_tier = packaged_product_tier()
    data["product_tier"] = product_tier
    allowed_builtin_lexicons = {
        item.id for item in bundled_lexicon_catalog(product_tier=product_tier)
    }
    allowed_builtin_lexicons &= set(packaged_enabled_builtin_lexicons())
    data["enabled_builtin_lexicons"] = tuple(
        item
        for item in _setting_values(data.get("enabled_builtin_lexicons", ()))
        if item in allowed_builtin_lexicons
    )
    backend = str(data.get("asr_backend", "")).strip().lower()
    model_path = str(data.get("whisper_model_path", "")).strip()
    executable = str(data.get("whisper_executable", "")).strip()
    streaming_model_dir = str(data.get("sherpa_streaming_model_dir", "")).strip()
    streaming_backend = str(data.get("streaming_backend", "")).strip().lower()

    if streaming_backend in {"", "sherpa", "sherpa-onnx", "online"}:
        data["streaming_backend"] = "funasr"
    funasr_root = str(data.get("funasr_model_root", "")).strip()
    data["funasr_streaming_model"] = _resolve_funasr_model_setting(
        str(data.get("funasr_streaming_model", "")).strip(),
        root=funasr_root,
        dirname=DEFAULT_FUNASR_STREAMING_MODEL_DIR,
        fallback=bundled_funasr_streaming_model(),
    )
    data["funasr_offline_model"] = _resolve_funasr_model_setting(
        str(data.get("funasr_offline_model", "")).strip(),
        root=funasr_root,
        dirname=DEFAULT_FUNASR_OFFLINE_MODEL_DIR,
        fallback=bundled_funasr_offline_model(),
    )
    data["funasr_vad_model"] = _resolve_funasr_model_setting(
        str(data.get("funasr_vad_model", "")).strip(),
        root=funasr_root,
        dirname=DEFAULT_FUNASR_VAD_MODEL_DIR,
        fallback=bundled_funasr_vad_model(),
    )
    data["funasr_punc_model"] = _resolve_funasr_model_setting(
        str(data.get("funasr_punc_model", "")).strip(),
        root=funasr_root,
        dirname=DEFAULT_FUNASR_PUNC_MODEL_DIR,
        fallback=bundled_funasr_punc_model(),
    )
    data["funasr_speaker_model"] = _resolve_funasr_model_setting(
        str(data.get("funasr_speaker_model", "")).strip(),
        root=funasr_root,
        dirname=DEFAULT_FUNASR_SPEAKER_MODEL_DIR,
        fallback=bundled_funasr_speaker_model(),
    )

    if has_bundled_funasr_review_models() and backend in {"", "dummy", "mock", "whisper", "whisper.cpp", "whisper-cpp"}:
        data["asr_backend"] = "funasr"
    elif has_bundled_whisper():
        from .resources import bundled_whisper_executable, bundled_whisper_model

        bundled_model = str(bundled_whisper_model())
        bundled_executable = str(bundled_whisper_executable())
        if backend in {"", "dummy", "mock"}:
            data["asr_backend"] = "whisper.cpp"
        if _should_use_bundled_model(model_path):
            data["whisper_model_path"] = bundled_model
        if _should_use_bundled_executable(executable):
            data["whisper_executable"] = bundled_executable

    if data["streaming_backend"] in {"sherpa", "sherpa-onnx"} and has_bundled_sherpa_streaming_model() and _should_use_bundled_sherpa_streaming_model(streaming_model_dir):
        data["sherpa_streaming_model_dir"] = str(bundled_sherpa_streaming_model_dir())

    return AppSettings.from_dict(data)


def _should_use_bundled_model(model_path: str) -> bool:
    if not model_path:
        return True
    normalized = model_path.replace("/", "\\").lower()
    if normalized in {"models\\ggml-tiny.bin", "models\\ggml-base.bin"}:
        return True
    path = Path(model_path).expanduser()
    if path.is_file():
        return False
    return path.name.lower() in {"ggml-tiny.bin", "ggml-base.bin"}


def _should_use_bundled_executable(executable: str) -> bool:
    if not executable:
        return True
    normalized = executable.replace("/", "\\").lower()
    if normalized in {"whisper-cli", "whisper-cli.exe", "bin\\whisper-cli", "bin\\whisper-cli.exe"}:
        return True
    path = Path(executable).expanduser()
    if path.is_file():
        return False
    return path.name.lower() in {"whisper-cli", "whisper-cli.exe"}


def _should_use_bundled_sherpa_streaming_model(model_dir: str) -> bool:
    if not model_dir:
        return True
    normalized = model_dir.replace("/", "\\").lower()
    if "sherpa-onnx-streaming-zipformer-zh-14m-2023-02-23" in normalized:
        return True
    if "sherpa-onnx-streaming-zipformer-ctc-zh-xlarge-int8-2025-06-30" in normalized:
        return not Path(model_dir).expanduser().is_dir()
    return not Path(model_dir).expanduser().is_dir()


def _funasr_model_from_root(root: str, dirname: str, fallback: str) -> str:
    if root:
        candidate = Path(root).expanduser() / dirname
        if candidate.is_dir():
            return str(candidate)
    return fallback


def _resolve_funasr_model_setting(value: str, *, root: str, dirname: str, fallback: str) -> str:
    if _should_use_bundled_funasr_model(value, dirname):
        return _funasr_model_from_root(root, dirname, fallback)
    return value


def _should_use_bundled_funasr_model(value: str, dirname: str) -> bool:
    if not value:
        return True
    normalized = value.replace("/", "\\").lower()
    known_names = {
        DEFAULT_FUNASR_STREAMING_MODEL_DIR.lower(),
        DEFAULT_FUNASR_OFFLINE_MODEL_DIR.lower(),
        DEFAULT_FUNASR_VAD_MODEL_DIR.lower(),
        DEFAULT_FUNASR_PUNC_MODEL_DIR.lower(),
        DEFAULT_FUNASR_COMPACT_PUNC_MODEL_DIR.lower(),
        DEFAULT_FUNASR_SPEAKER_MODEL_DIR.lower(),
    }
    if dirname.lower() in normalized or any(name in normalized for name in known_names):
        return not Path(value).expanduser().is_dir()
    return False


def _setting_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value)
    return ()


def extract_text(result: object) -> str:
    if isinstance(result, str):
        return result

    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text

    if isinstance(result, dict):
        value = result.get("text", "")
        return value if isinstance(value, str) else str(value)

    return str(result)


def _format_seconds(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, second = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return f"{second} 秒"


def normalize_recognition_text(text: str, settings: AppSettings) -> str:
    """Apply language normalization and optional lexicon optimization."""

    normalized = normalize_transcript_text(text.strip(), settings.recognition_language)
    if not normalized:
        return normalized

    lexicon = _load_runtime_lexicon(settings)
    if lexicon is None:
        return punctuate_transcript_text(normalized, close_sentence=True)
    corrected = lexicon.normalize(normalized, product_tier=settings.product_tier.strip() or None)
    return punctuate_transcript_text(corrected, close_sentence=True)


def normalize_streaming_text(
    text: str,
    settings: AppSettings,
    *,
    close_sentence: bool = False,
) -> str:
    """Normalize live streaming hypotheses without reloading lexicon files."""

    normalized = normalize_transcript_text(text.strip(), settings.recognition_language)
    if not normalized:
        return normalized

    lexicon = _load_runtime_lexicon(settings)
    if lexicon is not None:
        normalized = lexicon.normalize(normalized, product_tier=settings.product_tier.strip() or None)
    return punctuate_transcript_text(normalized, close_sentence=close_sentence)


def _log_recognition_completed(kind: str, text: str) -> None:
    LOGGER.info("%s recognition completed: characters=%d", kind, len(text))


def _load_runtime_lexicon(settings: AppSettings) -> Lexicon | None:
    if not settings.lexicon_enabled:
        return None

    bundled_paths = bundled_lexicon_paths(
        settings.enabled_builtin_lexicons,
        product_tier=settings.product_tier.strip() or None,
    )
    lexicon_path = settings.lexicon_path.strip()
    custom_path = Path(lexicon_path).expanduser() if lexicon_path else None
    cache_key = _runtime_lexicon_cache_key(settings, bundled_paths, custom_path)

    global _LEXICON_CACHE_KEY, _LEXICON_CACHE_VALUE
    with _LEXICON_CACHE_LOCK:
        if cache_key == _LEXICON_CACHE_KEY:
            return _LEXICON_CACHE_VALUE

        lexicons: list[Lexicon] = []
        if bundled_paths:
            try:
                lexicons.append(Lexicon.from_files(bundled_paths))
            except Exception:
                LOGGER.warning("failed to load bundled lexicons", exc_info=True)

        if custom_path is not None:
            try:
                lexicons.append(Lexicon.from_file(custom_path, weight=1000))
            except Exception:
                LOGGER.warning("failed to load lexicon: %s", custom_path, exc_info=True)

        merged = lexicons[0].merge(*lexicons[1:]) if lexicons else None
        _LEXICON_CACHE_KEY = cache_key
        _LEXICON_CACHE_VALUE = merged
        return merged


def _runtime_lexicon_cache_key(
    settings: AppSettings,
    bundled_paths: tuple[Path, ...],
    custom_path: Path | None,
) -> tuple[object, ...]:
    path_parts: list[tuple[str, int | None, int | None]] = []
    for path in (*bundled_paths, *((custom_path,) if custom_path is not None else ())):
        try:
            stat = path.stat()
            path_parts.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            path_parts.append((str(path), None, None))
    return (
        settings.lexicon_enabled,
        tuple(settings.enabled_builtin_lexicons),
        settings.product_tier.strip(),
        tuple(path_parts),
    )


def _create_history_store(settings_path: Path) -> TranscriptHistoryStore:
    database_path = settings_path.with_name("transcript_history.sqlite3")
    legacy_path = settings_path.with_name("transcript_history.json")
    if database_path.exists() or not legacy_path.exists():
        return TranscriptHistoryStore(database_path)

    legacy_store = TranscriptHistoryStore(legacy_path)
    database_store = TranscriptHistoryStore(database_path)
    for session in reversed(legacy_store.list_sessions(limit=legacy_store.max_sessions)):
        database_store.save_session(session)
    return database_store


class VoiceInputApp:
    """Coordinates settings, UI actions, ASR, and optional text injection."""

    def __init__(
        self,
        ui_factory: Callable[["VoiceInputApp"], UiPort],
        *,
        settings_store: SettingsStore | None = None,
        asr_engine: ASREnginePort | None = None,
        recorder: FixedDurationRecorder | None = None,
        injector: object | None = None,
        history_store: TranscriptHistoryStore | None = None,
    ) -> None:
        self.log_path = configure_logging()
        self.settings_store = settings_store or SettingsStore()
        loaded_settings = self.settings_store.load()
        self.settings: AppSettings = resolve_runtime_settings(loaded_settings)
        self.history_store = history_store or _create_history_store(self.settings_store.path)
        self._session_history = self.history_store.list_session_summaries(limit=50)
        if (
            has_bundled_whisper()
            or has_bundled_sherpa_streaming_model()
            or has_bundled_funasr_streaming_model()
            or has_bundled_funasr_review_models()
        ) and self.settings.to_dict() != loaded_settings.to_dict():
            self.settings_store.save(self.settings)
        LOGGER.info("starting voice input assistant")
        self.asr_engine = asr_engine or build_asr_engine(self.settings)
        self.recorder = recorder or FixedDurationRecorder(
            allow_fallback_silence=self._uses_dummy_backend(self.settings)
        )
        self.injector = injector or self._load_default_injector()
        self.status = RuntimeStatus()
        self._running = False
        self._recording = False
        self._recording_paused = False
        self._messages: queue.Queue[tuple[str, str, object]] = queue.Queue()
        self._streaming_message_lock = threading.Lock()
        self._latest_streaming_event: tuple[str, object] | None = None
        self._last_streaming_ui_update_at = 0.0
        self._audio_monitor_lock = threading.Lock()
        self._audio_level = 0.0
        self._audio_level_details: dict[str, object] = AudioLevel(
            level=0.0,
            rms=0.0,
            peak=0.0,
            rms_dbfs=-96.0,
            peak_dbfs=-96.0,
        ).to_dict()
        self._audio_level_message_pending = False
        self._microphone_state: dict[str, object] = MicrophoneState().to_dict()
        self._worker: threading.Thread | None = None
        self._partial_worker: threading.Thread | None = None
        self._partial_worker_done: tuple[str, threading.Event] | None = None
        self._health_worker: threading.Thread | None = None
        self._health_job_token: str | None = None
        self._lifecycle_lock = threading.RLock()
        self._cycle_controls = _CycleControls()
        # Compatibility aliases for integrations that inspect the active cycle.
        self._partial_stop_event = self._cycle_controls.partial_stop
        self._partial_segment_count = 0
        self._session_lock = threading.Lock()
        self._session_parts: list[str] = []
        self._session_started_at: float | None = None
        self._streaming_active = False
        self._streaming_stop_event = self._cycle_controls.streaming_stop
        self._streaming_pause_event = self._cycle_controls.streaming_pause
        self._recognition_cancel_event = self._cycle_controls.recognition_cancel
        self._prepared_streaming_recognizer: object | None = None
        self._active_streaming_recognizer: object | None = None
        self._active_streaming_cycle_token: str | None = None
        self._transcribe_lock = threading.Lock()
        self._cycle_token = uuid.uuid4().hex
        self._cycle_settings = self.settings
        self._cycle_asr_engine = self.asr_engine
        self._cycle_injector = self.injector
        self._retired_asr_engines: list[object] = []
        self._persisted_cycle_tokens: set[str] = set()
        self._pending_streaming_runtime_refresh = False
        self._resource_policy = SessionResourcePolicy()
        self._resource_decision: ResourceDecision = self._resource_policy.begin_session(
            capture_system_resource_snapshot()
        )
        LOGGER.info("selected ASR resource profile: %s", self._resource_decision.profile.value)
        self._hotkey_error: str | None = None
        self._tray_available = False
        self._startup_enabled = self.settings.launch_on_startup
        if asr_engine is None and recorder is None:
            self._prewarm_streaming_recognizer(wait=False)
        self.ui = ui_factory(self)
        self.hotkeys = GlobalHotkeyManager(
            self.settings.hotkey,
            lambda: self.ui.after(0, self.toggle),
            enabled=self.settings.enable_hotkey,
        )
        self.tray = TrayController(
            on_show=lambda: self.ui.after(0, self.show_window),
            on_toggle=lambda: self.ui.after(0, self.toggle),
            on_exit=lambda: self.ui.after(0, self.exit_app),
        )

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_recording_paused(self) -> bool:
        return self._recording_paused

    @property
    def audio_level(self) -> float:
        with self._audio_monitor_lock:
            return self._audio_level

    @property
    def audio_level_details(self) -> dict[str, object]:
        with self._audio_monitor_lock:
            return dict(self._audio_level_details)

    @property
    def microphone_state(self) -> dict[str, object]:
        with self._audio_monitor_lock:
            return dict(self._microphone_state)

    @property
    def session_history(self) -> list[dict[str, object]]:
        return [dict(session) for session in self._session_history]

    @property
    def resource_profile(self) -> str:
        return self._resource_decision.profile.value

    def run(self) -> None:
        self._tray_available = self._start_tray()
        self._sync_tray_running()
        self._hotkey_error = None if self.hotkeys.start() else self._describe_hotkey_error()
        actual_startup = is_run_on_startup_enabled("VoiceInputAssistant")
        self._startup_enabled = actual_startup
        if actual_startup != self.settings.launch_on_startup:
            self.settings = AppSettings.from_dict(
                {**self.settings.to_dict(), "launch_on_startup": actual_startup}
            )
            self.settings_store.save(self.settings)
        self._sync_autostart_ui()

        startup_status = "就绪"
        if self.settings.start_minimized:
            if self._tray_available:
                self.ui.hide_window()
                startup_status = "已启动到托盘"
            else:
                self.ui.show_window()
                self.ui.after(
                    0,
                    lambda: self.ui.show_info(
                        "提示",
                        "已启用“启动后最小化”，但系统托盘不可用，窗口保持显示。",
                    ),
                )
        self._set_status(RunState.IDLE, startup_status)
        if self._hotkey_error:
            self.ui.after(0, lambda: self.ui.show_error("快捷键不可用", self._hotkey_error or ""))
        if self.settings.show_first_run_hint:
            self.ui.after(300, self._show_first_run_hint)
        self._poll_messages()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            if self._health_worker is not None and self._health_worker.is_alive():
                self.ui.show_error("诊断进行中", "请等待系统诊断完成后再开始录音。")
                return

        streaming_active = self._should_use_streaming_transcription()
        if not streaming_active:
            unavailable_message = self._offline_recognition_unavailable_message(feature="recording")
            if unavailable_message:
                self.ui.show_error("当前安装包不支持结束后转写", unavailable_message)
                return
        elif not self._resource_decision.allow_parallel_workers:
            # An import may have left its independent offline worker resident.
            # Release it before the streaming online/review pipeline takes ownership.
            self._close_asr_engine(self.asr_engine)

        cycle_token = uuid.uuid4().hex
        controls = _CycleControls()
        cycle_settings = AppSettings.from_dict(self.settings.to_dict())
        try:
            prepare_recorder = getattr(self.recorder, "prepare_recording", None)
            if callable(prepare_recorder):
                prepare_recorder()

            if streaming_active:
                worker = threading.Thread(
                    target=self._run_streaming_worker,
                    args=(cycle_token, controls),
                    name="voice-input-streaming-cycle",
                    daemon=True,
                )
            else:
                worker = threading.Thread(
                    target=self._run_once_worker,
                    args=(cycle_token, controls),
                    name="voice-input-cycle",
                    daemon=True,
                )
            partial_worker: threading.Thread | None = None
            partial_done: threading.Event | None = None
            if cycle_settings.partial_transcription_enabled and not streaming_active:
                partial_done = threading.Event()
                partial_worker = threading.Thread(
                    target=self._run_partial_transcription_worker,
                    args=(cycle_token, controls, partial_done),
                    name="voice-input-partial-cycle",
                    daemon=True,
                )

            with self._lifecycle_lock:
                if self._running:
                    return
                self._cycle_token = cycle_token
                self._install_cycle_controls(controls)
                self._cycle_settings = cycle_settings
                self._cycle_asr_engine = self.asr_engine
                self._cycle_injector = self.injector
                self._partial_segment_count = 0
                self._streaming_active = streaming_active
                self._running = True
                self._recording = True
                self._recording_paused = False
                self._worker = worker
                self._partial_worker = partial_worker
                self._partial_worker_done = (
                    (cycle_token, partial_done) if partial_done is not None else None
                )
            self._clear_latest_streaming_event()
            with self._session_lock:
                self._session_parts = []
                self._session_started_at = time.monotonic()
            self._reset_audio_monitor_cycle()
            self._configure_audio_monitor(self.recorder, cycle_token)
            LOGGER.info("recognition cycle started")
            self.ui.set_running(True)
            self._set_recording_ui(True)
            self._sync_tray_running()
            if streaming_active:
                self._begin_streaming_text()
                self._set_status(RunState.LISTENING, "正在实时转写")
            else:
                self._set_status(RunState.LISTENING, "正在录制")
            worker.start()
            if partial_worker is not None:
                partial_worker.start()
        except Exception as exc:
            self._rollback_failed_cycle_start(cycle_token, controls, exc)

    def stop(self) -> None:
        if not self._running:
            self._cycle_token = uuid.uuid4().hex
            self._recording = False
            self._recording_paused = False
            self._set_audio_level_ui(0.0)
            self.ui.set_running(False)
            self._set_recording_ui(False)
            self._sync_tray_running()
            return
        if self._recording:
            self.stop_recording()
        else:
            self.cancel_cycle()

    def stop_recording(self) -> bool:
        with self._lifecycle_lock:
            if not self._running or not self._recording:
                return False
            controls = self._cycle_controls
            streaming_active = self._streaming_active
            self._recording = False
            self._recording_paused = False

        LOGGER.info("recording stopped by user")
        self._set_audio_level_ui(0.0)
        controls.partial_stop.set()
        controls.streaming_stop.set()
        if streaming_active:
            controls.streaming_pause.clear()
        else:
            stop_recorder = getattr(self.recorder, "stop", None)
            if callable(stop_recorder):
                stop_recorder()
        self._set_recording_ui(False)
        self._set_status(RunState.RECOGNIZING, "正在整理最终文本" if streaming_active else "录制已结束，正在识别")
        return True

    def cancel_cycle(self) -> bool:
        with self._lifecycle_lock:
            if not self._running:
                return False
            controls = self._cycle_controls
            cycle_token = self._cycle_token
            active_recognizer = (
                self._active_streaming_recognizer
                if self._active_streaming_cycle_token == cycle_token
                else None
            )
            self._recording = False
            self._recording_paused = False

        LOGGER.info("recognition cycle cancellation requested")
        controls.partial_stop.set()
        controls.streaming_stop.set()
        controls.streaming_pause.clear()
        controls.recognition_cancel.set()
        stop_recorder = getattr(self.recorder, "stop", None)
        if callable(stop_recorder):
            stop_recorder()
        cancel_recognizer = getattr(active_recognizer, "cancel", None)
        if callable(cancel_recognizer):
            try:
                cancel_recognizer()
            except Exception:
                LOGGER.warning("failed to cancel active streaming recognizer", exc_info=True)
        self._set_audio_level_ui(0.0)
        self._set_recording_ui(False)
        self._set_status(RunState.RECOGNIZING, "正在取消识别")
        return True

    def pause_recording(self) -> bool:
        if not self._running or not self._recording:
            return False
        if self._recording_paused:
            return True

        pause_recorder = getattr(self.recorder, "pause", None)
        if not self._streaming_active and not callable(pause_recorder):
            return False

        LOGGER.info("recording paused by user")
        if self._streaming_active:
            self._cycle_controls.streaming_pause.set()
        else:
            pause_recorder()
        self._recording_paused = True
        self._set_audio_level_ui(0.0)
        self._set_microphone_state_ui(self._microphone_state_with_status("paused"))
        self._set_status(RunState.LISTENING, "录音已暂停")
        return True

    def resume_recording(self) -> bool:
        if not self._running or not self._recording or not self._recording_paused:
            return False

        resume_recorder = getattr(self.recorder, "resume", None)
        if not self._streaming_active and not callable(resume_recorder):
            return False

        LOGGER.info("recording resumed by user")
        if self._streaming_active:
            self._cycle_controls.streaming_pause.clear()
        else:
            resume_recorder()
        self._recording_paused = False
        self._set_microphone_state_ui(self._microphone_state_with_status("active"))
        self._set_status(RunState.LISTENING, "正在实时转写" if self._streaming_active else "正在录制")
        return True

    def toggle_recording_pause(self) -> bool:
        if self._recording_paused:
            return self.resume_recording()
        return self.pause_recording()

    def toggle(self) -> None:
        if self._running:
            self.stop()
        else:
            self.start()

    def _install_cycle_controls(self, controls: _CycleControls) -> None:
        self._cycle_controls = controls
        self._partial_stop_event = controls.partial_stop
        self._streaming_stop_event = controls.streaming_stop
        self._streaming_pause_event = controls.streaming_pause
        self._recognition_cancel_event = controls.recognition_cancel

    def _rollback_failed_cycle_start(
        self,
        cycle_token: str,
        controls: _CycleControls,
        exc: Exception,
        *,
        title: str = "无法开始录音",
    ) -> None:
        controls.partial_stop.set()
        controls.streaming_stop.set()
        controls.streaming_pause.clear()
        controls.recognition_cancel.set()
        stop_recorder = getattr(self.recorder, "stop", None)
        if callable(stop_recorder):
            try:
                stop_recorder()
            except Exception:
                LOGGER.warning("failed to stop recorder after cycle start failure", exc_info=True)
        with self._lifecycle_lock:
            if self._cycle_token == cycle_token:
                self._cycle_token = uuid.uuid4().hex
                self._running = False
                self._recording = False
                self._recording_paused = False
                self._streaming_active = False
        self._clear_latest_streaming_event(cycle_token)
        self._set_audio_level_ui(0.0)
        try:
            self._set_recording_ui(False)
            self.ui.set_running(False)
            self._sync_tray_running()
            self._set_status(RunState.ERROR, str(exc))
            self.ui.show_error(title, self._friendly_error(exc))
        except Exception:
            LOGGER.exception("failed to restore UI after cycle start failure")
        LOGGER.exception("failed to start recognition cycle", exc_info=exc)

    def update_settings(self, settings: AppSettings) -> None:
        resolved = resolve_runtime_settings(settings)
        validation_error = self._validate_settings(resolved)
        if validation_error is not None:
            self.ui.show_error("设置未保存", validation_error)
            return

        previous = self.settings
        asr_runtime_changed = self._asr_runtime_settings_changed(previous, resolved)
        streaming_runtime_changed = self._streaming_runtime_settings_changed(previous, resolved)
        self.settings = resolved
        self.settings_store.save(resolved)
        if asr_runtime_changed:
            previous_engine = self.asr_engine
            self.asr_engine = build_asr_engine(resolved)
            if previous_engine is not self.asr_engine:
                if self._running:
                    self._retired_asr_engines.append(previous_engine)
                else:
                    self._close_asr_engine(previous_engine)
        if isinstance(self.recorder, FixedDurationRecorder) and not self._running:
            self.recorder.allow_fallback_silence = self._uses_dummy_backend(resolved)
        self.hotkeys.stop()
        self.hotkeys = GlobalHotkeyManager(
            resolved.hotkey,
            lambda: self.ui.after(0, self.toggle),
            enabled=resolved.enable_hotkey,
        )
        self._hotkey_error = None if self.hotkeys.start() else self._describe_hotkey_error()
        if self._hotkey_error:
            self.ui.show_error("快捷键不可用", self._hotkey_error)
        self.injector = self._load_default_injector()
        if streaming_runtime_changed:
            if self._running:
                self._pending_streaming_runtime_refresh = True
            else:
                self._close_prepared_streaming_recognizer()
                self._prewarm_streaming_recognizer(wait=False)
        else:
            if self._running:
                self._pending_streaming_runtime_refresh = True
            else:
                self._sync_prepared_streaming_session_settings(resolved)
        if resolved.launch_on_startup != self._startup_enabled:
            self.set_startup_enabled(resolved.launch_on_startup)
        else:
            self._sync_autostart_ui()
        self._set_status(RunState.IDLE, "设置已保存")

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            controls = self._cycle_controls
            active_recognizer = self._active_streaming_recognizer
            self._cycle_token = uuid.uuid4().hex
            self._health_job_token = None
            self._running = False
            self._recording = False
            self._recording_paused = False
            self._streaming_active = False
            self._active_streaming_recognizer = None
            self._active_streaming_cycle_token = None
        self._clear_latest_streaming_event()
        stop_recorder = getattr(self.recorder, "stop", None)
        if callable(stop_recorder):
            stop_recorder()
        controls.partial_stop.set()
        controls.streaming_stop.set()
        controls.streaming_pause.clear()
        controls.recognition_cancel.set()
        if active_recognizer is self._prepared_streaming_recognizer:
            self._prepared_streaming_recognizer = None
        cancel_active = getattr(active_recognizer, "cancel", None)
        if callable(cancel_active):
            try:
                cancel_active()
            except Exception:
                LOGGER.warning("failed to cancel active streaming recognizer", exc_info=True)
        close_active = getattr(active_recognizer, "close", None)
        if callable(close_active):
            try:
                close_active()
            except Exception:
                LOGGER.warning("failed to close active streaming recognizer", exc_info=True)
        self._join_cycle_workers(_WORKER_SHUTDOWN_TIMEOUT_SECONDS)
        self._close_offline_asr_engines()
        self.hotkeys.stop()
        self._close_prepared_streaming_recognizer()
        self._resource_policy.end_session()
        self._stop_tray()
        self._set_audio_level_ui(0.0)

    def _close_offline_asr_engines(self) -> None:
        seen: set[int] = set()
        engines = (self.asr_engine, self._cycle_asr_engine, *self._retired_asr_engines)
        self._retired_asr_engines.clear()
        for engine in engines:
            if id(engine) in seen:
                continue
            seen.add(id(engine))
            self._close_asr_engine(engine)

    def _close_retired_asr_engines(self) -> None:
        retired = self._retired_asr_engines
        self._retired_asr_engines = []
        for engine in retired:
            self._close_asr_engine(engine)

    @staticmethod
    def _close_asr_engine(engine: object) -> None:
        close = getattr(engine, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                LOGGER.warning("failed to close offline ASR engine", exc_info=True)

    def request_close(self) -> None:
        if self.settings.minimize_to_tray and self._is_tray_available():
            self.ui.hide_window()
            self._set_status(RunState.IDLE, "已隐藏到托盘")
            return
        if self.settings.minimize_to_tray and not self._is_tray_available():
            self.ui.show_info("提示", "系统托盘不可用，窗口将直接退出。")
        self.exit_app()

    def show_window(self) -> None:
        self.ui.show_window()

    def exit_app(self) -> None:
        self.shutdown()
        self.ui.close_window()

    def set_startup_enabled(self, enabled: bool) -> None:
        target, arguments = self._startup_launch_command()
        if enabled:
            set_run_on_startup("VoiceInputAssistant", target, arguments=arguments)
        else:
            clear_run_on_startup("VoiceInputAssistant")
        self._startup_enabled = enabled
        self.settings = AppSettings.from_dict({**self.settings.to_dict(), "launch_on_startup": enabled})
        self.settings_store.save(self.settings)
        self._sync_autostart_ui()

    def _startup_launch_command(self) -> tuple[str, str]:
        legacy_argument = " --legacy-ui" if "--legacy-ui" in sys.argv else ""
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).resolve()), legacy_argument.strip()
        return str(Path(sys.executable).resolve()), f"-m voice_input{legacy_argument}"

    def _sync_autostart_ui(self) -> None:
        if hasattr(self.ui, "set_autostart"):
            self.ui.after(0, lambda enabled=self._startup_enabled: self.ui.set_autostart(enabled))

    def _start_tray(self) -> bool:
        start_ui_tray = getattr(self.ui, "start_tray", None)
        if callable(start_ui_tray):
            try:
                if bool(start_ui_tray()):
                    self.tray.available = True
                    return True
            except Exception:
                LOGGER.exception("failed to start native UI tray")

        available = self.tray.start()
        self.tray.available = available
        return available

    def _stop_tray(self) -> None:
        stop_ui_tray = getattr(self.ui, "stop_tray", None)
        if callable(stop_ui_tray):
            try:
                stop_ui_tray()
            except Exception:
                LOGGER.exception("failed to stop native UI tray")
        self.tray.stop()
        self.tray.available = False
        self._tray_available = False

    def _is_tray_available(self) -> bool:
        return self._tray_available or bool(getattr(self.tray, "available", False))

    def _sync_tray_running(self) -> None:
        set_ui_tray_running = getattr(self.ui, "set_tray_running", None)
        if callable(set_ui_tray_running):
            try:
                set_ui_tray_running(self._running)
            except Exception:
                LOGGER.exception("failed to update native UI tray state")
        if hasattr(self.tray, "set_running"):
            try:
                self.tray.set_running(self._running)
            except Exception:
                LOGGER.exception("failed to update tray state")

    def _set_recording_ui(self, recording: bool) -> None:
        set_recording = getattr(self.ui, "set_recording", None)
        if callable(set_recording):
            try:
                set_recording(recording)
            except Exception:
                LOGGER.exception("failed to update recording UI state")

    def _configure_audio_monitor(self, target: object, cycle_token: str) -> None:
        set_monitor = getattr(target, "set_audio_monitor", None)
        if not callable(set_monitor):
            return
        set_monitor(
            lambda level: self._publish_audio_level(cycle_token, level),
            lambda state: self._publish_microphone_state(cycle_token, state),
        )

    def _configure_finalization_monitor(self, target: object, cycle_token: str) -> None:
        set_monitor = getattr(target, "set_finalization_monitor", None)
        if not callable(set_monitor):
            return
        set_monitor(
            lambda completed, total: self._messages.put(
                ("finalization_progress", cycle_token, (int(completed), int(total)))
            )
        )

    def _publish_audio_level(self, cycle_token: str, level: AudioLevel) -> None:
        if not self._is_current_cycle(cycle_token):
            return
        should_queue = False
        with self._audio_monitor_lock:
            self._audio_level = max(0.0, min(1.0, float(level.level)))
            self._audio_level_details = level.to_dict()
            if not self._audio_level_message_pending:
                self._audio_level_message_pending = True
                should_queue = True
        if should_queue:
            self._messages.put(("audio_level", cycle_token, None))

    def _publish_microphone_state(self, cycle_token: str, state: MicrophoneState) -> None:
        if not self._is_current_cycle(cycle_token):
            return
        self._messages.put(("microphone_state", cycle_token, state.to_dict()))

    def _reset_audio_monitor_cycle(self) -> None:
        silent = AudioLevel(
            level=0.0,
            rms=0.0,
            peak=0.0,
            rms_dbfs=-96.0,
            peak_dbfs=-96.0,
        )
        with self._audio_monitor_lock:
            self._audio_level = 0.0
            self._audio_level_details = silent.to_dict()
            self._audio_level_message_pending = False
        self._set_audio_level_ui(0.0)

    def _set_audio_level_ui(self, level: float) -> None:
        normalized = max(0.0, min(1.0, float(level)))
        with self._audio_monitor_lock:
            self._audio_level = normalized
            self._audio_level_details["level"] = normalized
            if normalized == 0.0:
                self._audio_level_details.update(
                    {
                        "rms": 0.0,
                        "peak": 0.0,
                        "rms_dbfs": -96.0,
                        "peak_dbfs": -96.0,
                        "clipping": False,
                    }
                )
        self._notify_audio_level_ui(normalized)

    def _notify_audio_level_ui(self, level: float) -> None:
        set_level = getattr(self.ui, "set_audio_level", None)
        if callable(set_level):
            try:
                set_level(level)
            except Exception:
                LOGGER.exception("failed to update microphone level UI")

    def _set_microphone_state_ui(self, state: dict[str, object]) -> None:
        with self._audio_monitor_lock:
            self._microphone_state = dict(state)
        set_state = getattr(self.ui, "set_microphone_state", None)
        if callable(set_state):
            try:
                set_state(dict(state))
            except Exception:
                LOGGER.exception("failed to update microphone state UI")

    def _set_finalization_progress_ui(self, completed: int, total: int) -> None:
        set_progress = getattr(self.ui, "set_finalization_progress", None)
        if not callable(set_progress):
            return
        try:
            set_progress(completed, total)
        except Exception:
            LOGGER.exception("failed to update finalization progress UI")

    def _microphone_state_with_status(self, status: str) -> dict[str, object]:
        with self._audio_monitor_lock:
            state = dict(self._microphone_state)
        state["status"] = status
        if status != "error":
            state["message"] = ""
        return state

    def open_log_file(self) -> None:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        try:
            self._open_path(path)
        except Exception as exc:
            self.ui.show_error("无法打开日志", str(exc))

    def open_help_file(self) -> None:
        path = self._help_file_path()
        try:
            self._open_path(path)
        except Exception as exc:
            self.ui.show_error("无法打开帮助", str(exc))

    def open_health_report(self) -> None:
        with self._lifecycle_lock:
            if self._running:
                self.ui.show_error("识别进行中", "请等待当前录音或识别任务完成后再运行系统诊断。")
                return
            if self._health_worker is not None and self._health_worker.is_alive():
                self.ui.show_info("系统诊断", "诊断报告正在生成，请稍候。")
                return
            job_token = uuid.uuid4().hex
            settings = AppSettings.from_dict(self.settings.to_dict())
            worker = threading.Thread(
                target=self._run_health_report_worker,
                args=(job_token, settings),
                name="voice-input-health-report",
                daemon=True,
            )
            self._health_job_token = job_token
            self._health_worker = worker
        self._set_status(RunState.RECOGNIZING, "正在生成诊断报告")
        try:
            worker.start()
        except Exception as exc:
            with self._lifecycle_lock:
                if self._health_job_token == job_token:
                    self._health_job_token = None
                    self._health_worker = None
            self._set_status(RunState.ERROR, str(exc))
            self.ui.show_error("诊断失败", self._friendly_error(exc))

    def _run_health_report_worker(self, job_token: str, settings: AppSettings) -> None:
        try:
            from .healthcheck import run_health_checks

            report = run_health_checks(settings)
            payload = {
                "status": report.status,
                "generated_at": report.generated_at,
                "items": [
                    {
                        "name": item.name,
                        "status": item.status,
                        "message": item.message,
                        "details": item.details or {},
                    }
                    for item in report.items
                ],
            }
            report_path = self.settings_store.path.parent / "health-report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._messages.put(("health_open_path", job_token, report_path))
            self._messages.put(("health_status", job_token, (RunState.IDLE, "诊断报告已生成", None)))
        except Exception as exc:
            self._messages.put(("health_error", job_token, exc))
        finally:
            self._messages.put(("health_done", job_token, None))

    def _open_path(self, path: str | Path) -> None:
        resolved = Path(path)
        if sys.platform.startswith("win"):
            os.startfile(resolved)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(resolved)])
        else:
            subprocess.Popen(["xdg-open", str(resolved)])

    def transcribe_audio_file(self, audio_path: str | Path) -> bool:
        with self._lifecycle_lock:
            if self._running:
                self.ui.show_error("识别进行中", "请等待当前录音或识别任务完成后再选择音频文件。")
                return False
            if self._health_worker is not None and self._health_worker.is_alive():
                self.ui.show_error("诊断进行中", "请等待系统诊断完成后再选择音频文件。")
                return False

        path = Path(audio_path).expanduser()
        if not path.is_file():
            self.ui.show_error("音频文件不可用", f"找不到可识别的音频文件：{path}")
            return False
        if not is_supported_audio_file(path):
            supported = supported_audio_format_names()
            self.ui.show_error("音频格式暂不支持", f"当前支持 {supported} 文件，请转换格式后再导入。")
            return False
        unavailable_message = self._audio_file_transcription_unavailable_message()
        if unavailable_message:
            self.ui.show_error("导入音频不可用", unavailable_message)
            return False
        duration_seconds = probe_audio_duration_seconds(path)
        if duration_seconds is not None and duration_seconds > DEFAULT_MAX_IMPORT_AUDIO_SECONDS:
            self.ui.show_error(
                "音频时长超出上限",
                (
                    f"导入音频最长支持 {_format_seconds(DEFAULT_MAX_IMPORT_AUDIO_SECONDS)}。"
                    f"当前文件约 {_format_seconds(duration_seconds)}，请先拆分后再导入。"
                ),
            )
            return False

        if not self._resource_decision.allow_parallel_workers:
            # Mode switches must not leave the streaming online/review pair
            # resident while the import-specific offline worker starts.
            self._close_prepared_streaming_recognizer()

        cycle_token = uuid.uuid4().hex
        controls = _CycleControls()
        cycle_settings = AppSettings.from_dict(self.settings.to_dict())
        worker = threading.Thread(
            target=self._run_audio_file_worker,
            args=(path, cycle_token, duration_seconds, controls),
            name="voice-input-file-cycle",
            daemon=True,
        )
        with self._lifecycle_lock:
            if self._running:
                return False
            self._cycle_token = cycle_token
            self._install_cycle_controls(controls)
            self._cycle_settings = cycle_settings
            self._cycle_asr_engine = self.asr_engine
            self._cycle_injector = self.injector
            self._running = True
            self._recording = False
            self._recording_paused = False
            self._streaming_active = False
            self._worker = worker
            self._partial_worker = None
            self._partial_worker_done = None
        self._clear_latest_streaming_event()
        with self._session_lock:
            self._session_parts = []
            self._session_started_at = time.monotonic()
        LOGGER.info(
            "audio file recognition started: format=%s duration_seconds=%s",
            path.suffix.lower() or "unknown",
            "unknown" if duration_seconds is None else f"{duration_seconds:.1f}",
        )
        try:
            self.ui.set_running(True)
            self._set_recording_ui(False)
            self._sync_tray_running()
            self._set_status(RunState.RECOGNIZING, "正在识别音频文件")
            worker.start()
            return True
        except Exception as exc:
            self._rollback_failed_cycle_start(
                cycle_token,
                controls,
                exc,
                title="无法开始识别音频",
            )
            return False

    def _run_once_worker(self, cycle_token: str, controls: _CycleControls) -> None:
        terminal_error: Exception | None = None
        recorded_audio: object | None = None
        try:
            recorded_audio = self.recorder.record_once()
            audio = recorded_audio
            if not self._is_current_cycle(cycle_token):
                return
            self._messages.put(("recording", cycle_token, False))
            settings = self._settings_for_cycle(cycle_token)
            if settings.partial_transcription_enabled:
                controls.partial_stop.set()
                self._wait_for_partial_worker(cycle_token)
                if not self._is_current_cycle(cycle_token):
                    return
                final_snapshot = self._partial_snapshot()
                if final_snapshot is None:
                    self._complete_partial_session(cycle_token, result=None)
                    return
                audio = final_snapshot
                self._messages.put(("status", cycle_token, (RunState.RECOGNIZING, "正在识别最后片段", None)))
            else:
                self._messages.put(("status", cycle_token, (RunState.RECOGNIZING, "正在识别", None)))
            result = self._transcribe(
                audio,
                settings=settings,
                asr_engine=self._asr_engine_for_cycle(cycle_token),
                cancel_event=controls.recognition_cancel,
            )
            self._raise_if_cancelled(controls.recognition_cancel)
            if not self._is_current_cycle(cycle_token):
                return
            text = normalize_recognition_text(extract_text(result), settings)
            if not text:
                if settings.partial_transcription_enabled:
                    self._complete_partial_session(cycle_token, result=result)
                else:
                    self._messages.put(("status", cycle_token, (RunState.IDLE, "没有识别到语音", None)))
                return

            if settings.inject_text:
                self._messages.put(("status", cycle_token, (RunState.INJECTING, "正在输入文字", text)))
                if not self._inject_cycle_text(cycle_token, text):
                    return

            if not self._is_current_cycle(cycle_token):
                return
            session_text = self._compose_session_text(text)
            record = self._persist_transcript_session(
                cycle_token,
                source={"kind": "microphone"},
                text=session_text,
                result=result,
                use_result_segments=not settings.partial_transcription_enabled,
            )
            self._messages.put(("text", cycle_token, text))
            if record is not None:
                self._messages.put(("session_record", cycle_token, record))
            self._messages.put(("status", cycle_token, (RunState.IDLE, "已完成", text)))
            _log_recognition_completed("microphone", text)
        except Exception as exc:
            if self._is_current_cycle(cycle_token):
                if self._is_recognition_cancelled(exc):
                    self._messages.put(("status", cycle_token, (RunState.IDLE, "识别已取消", None)))
                else:
                    LOGGER.exception("recognition failed")
                    terminal_error = exc
        finally:
            controls.partial_stop.set()
            self._release_audio(recorded_audio)
            self._messages.put(("cycle_terminal", cycle_token, terminal_error))

    def _run_partial_transcription_worker(
        self,
        cycle_token: str,
        controls: _CycleControls,
        done_event: threading.Event,
    ) -> None:
        try:
            settings = self._settings_for_cycle(cycle_token)
            interval = max(
                0.1,
                float(
                    settings.partial_transcription_interval_seconds
                    or DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS
                ),
            )
            while self._is_current_cycle(cycle_token) and self._recording:
                if controls.partial_stop.wait(interval):
                    break
                if not self._is_current_cycle(cycle_token) or not self._recording:
                    break
                if self._recording_paused:
                    self._messages.put(("status", cycle_token, (RunState.LISTENING, "录音已暂停", None)))
                    continue
                snapshot = self._partial_snapshot()
                if snapshot is None:
                    continue
                try:
                    self._messages.put(("status", cycle_token, (RunState.RECOGNIZING, "正在后台转写片段", None)))
                    result = self._transcribe(
                        snapshot,
                        settings=settings,
                        asr_engine=self._asr_engine_for_cycle(cycle_token),
                        cancel_event=controls.recognition_cancel,
                    )
                    self._raise_if_cancelled(controls.recognition_cancel)
                    if not self._is_current_cycle(cycle_token):
                        return
                    text = normalize_recognition_text(extract_text(result), settings)
                    if text:
                        self._partial_segment_count += 1
                        self._remember_session_part(text)
                        self._messages.put(("partial_text", cycle_token, text))
                        self._messages.put(("partial_progress", cycle_token, self._partial_segment_count))
                        self._messages.put(("status", cycle_token, (RunState.LISTENING, "正在录制 · 已更新片段", text)))
                    else:
                        self._messages.put(("status", cycle_token, (RunState.LISTENING, "正在录制", None)))
                except Exception:
                    if controls.recognition_cancel.is_set():
                        return
                    LOGGER.warning("partial transcription failed", exc_info=True)
                    self._messages.put(("status", cycle_token, (RunState.LISTENING, "片段转写失败，录制继续", None)))
        finally:
            done_event.set()

    def _run_streaming_worker(self, cycle_token: str, controls: _CycleControls) -> None:
        recognizer: object | None = None
        recognizer_installed = False
        terminal_error: Exception | None = None
        try:
            if not self._is_current_cycle(cycle_token):
                return
            settings = self._settings_for_cycle(cycle_token)
            recognizer = self._build_streaming_recognizer()
            if not self._is_current_cycle(cycle_token):
                return
            if (
                not self._resource_decision.release_online_after_recording
                and recognizer is not self._prepared_streaming_recognizer
            ):
                previous_prepared = self._prepared_streaming_recognizer
                self._prepared_streaming_recognizer = recognizer
                if previous_prepared is not None:
                    self._close_asr_engine(previous_prepared)
            with self._lifecycle_lock:
                if not self._is_current_cycle(cycle_token):
                    return
                self._active_streaming_recognizer = recognizer
                self._active_streaming_cycle_token = cycle_token
                recognizer_installed = True
            self._configure_audio_monitor(recognizer, cycle_token)
            self._configure_finalization_monitor(recognizer, cycle_token)
            hotwords = self._streaming_hotwords(settings)

            def on_event(event: object) -> None:
                if not self._is_current_cycle(cycle_token):
                    return
                with self._streaming_message_lock:
                    self._latest_streaming_event = (cycle_token, event)

            final_text = recognizer.transcribe_microphone(
                max_duration_seconds=DEFAULT_MAX_RECORDING_SECONDS,
                stop_event=controls.streaming_stop,
                pause_event=controls.streaming_pause,
                on_event=on_event,
                hotwords=hotwords,
            )
            self._raise_if_cancelled(controls.recognition_cancel)
            if not self._is_current_cycle(cycle_token):
                return
            text = normalize_recognition_text(final_text, settings)
            completion_status = str(getattr(recognizer, "last_completion_status", "completed"))
            incomplete = completion_status == "incomplete"
            self._messages.put(("recording", cycle_token, False))
            if text:
                if settings.inject_text:
                    self._messages.put(("status", cycle_token, (RunState.INJECTING, "正在输入文字", text)))
                    if not self._inject_cycle_text(cycle_token, text):
                        return
                self._messages.put(("streaming_final", cycle_token, text))
                record = self._persist_transcript_session(
                    cycle_token,
                    source={"kind": "microphone", "completion_status": completion_status},
                    text=text,
                    duration=self._current_session_duration(),
                    segments=getattr(recognizer, "last_segments", ()),
                )
                if record is not None:
                    self._messages.put(("session_record", cycle_token, record))
                status_text = "已停止，结果可能不完整" if incomplete else "已完成"
                self._messages.put(("status", cycle_token, (RunState.IDLE, status_text, text)))
                _log_recognition_completed("streaming", text)
            else:
                self._messages.put(("streaming_final", cycle_token, ""))
                self._messages.put(("status", cycle_token, (RunState.IDLE, "没有识别到语音", None)))
        except Exception as exc:
            if self._is_current_cycle(cycle_token):
                if self._is_recognition_cancelled(exc):
                    self._messages.put(("streaming_final", cycle_token, ""))
                    self._messages.put(("status", cycle_token, (RunState.IDLE, "识别已取消", None)))
                else:
                    LOGGER.exception("streaming recognition failed")
                    terminal_error = exc
        finally:
            should_close = False
            if recognizer is not None:
                with self._lifecycle_lock:
                    active_owned_by_new_cycle = (
                        self._active_streaming_recognizer is recognizer
                        and self._active_streaming_cycle_token not in {None, cycle_token}
                    )
                    if (
                        self._active_streaming_recognizer is recognizer
                        and self._active_streaming_cycle_token == cycle_token
                    ):
                        self._active_streaming_recognizer = None
                        self._active_streaming_cycle_token = None
                    if cycle_token == self._cycle_token:
                        self._streaming_active = False
                    if (
                        (self._resource_decision.release_online_after_recording or not recognizer_installed)
                        and not active_owned_by_new_cycle
                    ):
                        if recognizer is self._prepared_streaming_recognizer:
                            self._prepared_streaming_recognizer = None
                        should_close = True
            if recognizer is not None and should_close:
                close = getattr(recognizer, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        LOGGER.warning("failed to release streaming recognizer after recording", exc_info=True)
            self._messages.put(("cycle_terminal", cycle_token, terminal_error))

    def _partial_snapshot(self) -> object | None:
        snapshot = getattr(self.recorder, "snapshot_since_last", None)
        if not callable(snapshot):
            return None
        return snapshot()

    def _run_audio_file_worker(
        self,
        audio_path: Path,
        cycle_token: str,
        duration_seconds: float | None,
        controls: _CycleControls,
    ) -> None:
        temp_context: tempfile.TemporaryDirectory[str] | None = None
        terminal_error: Exception | None = None
        try:
            settings = self._settings_for_cycle(cycle_token)
            self._messages.put(("recording", cycle_token, False))
            source_path: Path = audio_path
            if requires_audio_transcode(audio_path):
                self._messages.put(("status", cycle_token, (RunState.RECOGNIZING, "正在准备音频文件", None)))
                temp_context = tempfile.TemporaryDirectory(prefix="voice-input-import-")
                source_path = transcode_audio_to_wav(
                    audio_path,
                    Path(temp_context.name) / "input.wav",
                    timeout_seconds=self._audio_file_transcode_timeout_seconds(duration_seconds),
                    cancel_event=controls.recognition_cancel,
                )
                self._raise_if_cancelled(controls.recognition_cancel)
            self._messages.put(("status", cycle_token, (RunState.RECOGNIZING, "正在识别音频文件", None)))
            result = self._transcribe(
                source_path,
                timeout_seconds=self._audio_file_timeout_seconds(duration_seconds),
                settings=settings,
                asr_engine=self._asr_engine_for_cycle(cycle_token),
                cancel_event=controls.recognition_cancel,
            )
            self._raise_if_cancelled(controls.recognition_cancel)
            if not self._is_current_cycle(cycle_token):
                return
            text = normalize_recognition_text(extract_text(result), settings)
            if not text:
                self._messages.put(("status", cycle_token, (RunState.IDLE, "没有识别到语音", None)))
                return

            if settings.inject_text:
                self._messages.put(("status", cycle_token, (RunState.INJECTING, "正在输入文字", text)))
                if not self._inject_cycle_text(cycle_token, text):
                    return

            if not self._is_current_cycle(cycle_token):
                return
            record = self._persist_transcript_session(
                cycle_token,
                source={"kind": "audio_file", "name": audio_path.name},
                text=text,
                result=result,
                duration=duration_seconds,
            )
            self._messages.put(("text", cycle_token, text))
            if record is not None:
                self._messages.put(("session_record", cycle_token, record))
            self._messages.put(("status", cycle_token, (RunState.IDLE, "已完成", text)))
            _log_recognition_completed("audio-file", text)
        except Exception as exc:
            if self._is_current_cycle(cycle_token):
                if self._is_recognition_cancelled(exc):
                    self._messages.put(("status", cycle_token, (RunState.IDLE, "识别已取消", None)))
                else:
                    LOGGER.exception("audio file recognition failed")
                    terminal_error = exc
        finally:
            if temp_context is not None:
                temp_context.cleanup()
            self._messages.put(("cycle_terminal", cycle_token, terminal_error))

    def _poll_messages(self) -> None:
        while True:
            try:
                kind, token, payload = self._messages.get_nowait()
            except queue.Empty:
                break

            if kind.startswith("health_"):
                self._handle_health_message(kind, token, payload)
                continue
            if token != self._cycle_token:
                continue

            if kind == "text":
                self.ui.append_text(str(payload))
            elif kind == "partial_text":
                self.ui.append_text(str(payload))
            elif kind == "streaming_text":
                self._update_streaming_text(payload)
            elif kind == "streaming_final":
                self._clear_latest_streaming_event(token)
                self._commit_streaming_text(str(payload))
            elif kind == "open_path":
                try:
                    self._open_path(Path(str(payload)))
                except Exception as exc:
                    self.ui.show_error("无法打开文件", str(exc))
            elif kind == "partial_progress":
                set_partial_progress = getattr(self.ui, "set_partial_progress", None)
                if callable(set_partial_progress):
                    set_partial_progress(int(payload))
            elif kind == "audio_level":
                with self._audio_monitor_lock:
                    level = self._audio_level
                    self._audio_level_message_pending = False
                self._notify_audio_level_ui(level)
            elif kind == "microphone_state":
                self._set_microphone_state_ui(dict(payload))  # type: ignore[arg-type]
            elif kind == "finalization_progress":
                completed, total = payload  # type: ignore[misc]
                self._set_finalization_progress_ui(int(completed), int(total))
            elif kind == "session_history":
                self._session_history = self.history_store.list_session_summaries(limit=50)
                set_history = getattr(self.ui, "set_session_history", None)
                if callable(set_history):
                    set_history(self.session_history)
            elif kind == "session_record":
                set_session = getattr(self.ui, "set_transcript_session", None)
                if callable(set_session) and isinstance(payload, dict):
                    set_session(dict(payload))
            elif kind == "recording":
                self._recording = bool(payload)
                if not self._recording:
                    self._recording_paused = False
                    self._set_audio_level_ui(0.0)
                self._set_recording_ui(self._recording)
            elif kind == "running":
                self._running = bool(payload)
                if not self._running:
                    self._recording = False
                    self._recording_paused = False
                    self._set_audio_level_ui(0.0)
                    self._set_recording_ui(False)
                    self._apply_pending_streaming_runtime_refresh()
                    self._close_retired_asr_engines()
                self.ui.set_running(self._running)
                self._sync_tray_running()
            elif kind == "cycle_terminal":
                exc = payload if isinstance(payload, BaseException) else None
                self._running = False
                self._recording = False
                self._recording_paused = False
                self._set_audio_level_ui(0.0)
                self._set_recording_ui(False)
                self._apply_pending_streaming_runtime_refresh()
                self._close_retired_asr_engines()
                self.ui.set_running(False)
                self._sync_tray_running()
                if exc is not None:
                    self._set_status(RunState.ERROR, str(exc))
                    self.ui.show_error("识别失败", self._friendly_error(exc))
            elif kind == "status":
                state, message, last_text = payload  # type: ignore[misc]
                self._set_status(state, message, last_text)
            elif kind == "error":
                exc = payload
                self._running = False
                self._recording = False
                self._recording_paused = False
                self._set_audio_level_ui(0.0)
                self._set_recording_ui(False)
                self.ui.set_running(False)
                self._sync_tray_running()
                self._apply_pending_streaming_runtime_refresh()
                self._close_retired_asr_engines()
                self._set_status(RunState.ERROR, str(exc))
                self.ui.show_error("识别失败", self._friendly_error(exc))

        self._poll_latest_streaming_event()
        self.ui.after(100, self._poll_messages)

    def _handle_health_message(self, kind: str, token: str, payload: object) -> None:
        if token != self._health_job_token:
            return
        if kind == "health_open_path":
            try:
                self._open_path(Path(str(payload)))
            except Exception as exc:
                self.ui.show_error("无法打开诊断报告", str(exc))
        elif kind == "health_status":
            state, message, last_text = payload  # type: ignore[misc]
            self._set_status(state, message, last_text)
        elif kind == "health_error":
            exc = payload
            self._set_status(RunState.ERROR, str(exc))
            self.ui.show_error("诊断失败", self._friendly_error(exc))
        elif kind == "health_done":
            with self._lifecycle_lock:
                if token == self._health_job_token:
                    self._health_job_token = None
                    self._health_worker = None

    def _remember_session_part(self, text: str) -> None:
        cleaned = str(text).strip()
        if not cleaned:
            return
        with self._session_lock:
            self._session_parts.append(cleaned)

    def _compose_session_text(self, final_text: str) -> str:
        cleaned = str(final_text).strip()
        with self._session_lock:
            parts = [*self._session_parts]
        if cleaned and (not parts or parts[-1] != cleaned):
            parts.append(cleaned)
        return "\n\n".join(part for part in parts if part)

    def _current_session_duration(self) -> float | None:
        with self._session_lock:
            started_at = self._session_started_at
        if started_at is None:
            return None
        return max(0.0, time.monotonic() - started_at)

    def _persist_transcript_session(
        self,
        cycle_token: str,
        *,
        source: dict[str, object],
        text: str,
        result: object | None = None,
        duration: float | None = None,
        use_result_segments: bool = True,
        segments: object | None = None,
    ) -> dict[str, object] | None:
        cleaned = str(text).strip()
        if not cleaned:
            return None
        result_duration = getattr(result, "duration_seconds", None) if result is not None else None
        resolved_duration = duration if duration is not None else result_duration
        result_segments = getattr(result, "segments", ()) if result is not None and use_result_segments else ()
        stored_segments = result_segments if segments is None else segments
        try:
            with self._lifecycle_lock:
                if not self._claim_cycle_persistence(cycle_token):
                    return None
                record = self.history_store.add_session(
                    source=source,
                    text=cleaned,
                    duration=resolved_duration,
                    segments=stored_segments,
                )
        except Exception:
            with self._session_lock:
                self._persisted_cycle_tokens.discard(cycle_token)
            LOGGER.exception("failed to persist transcript session")
            return None
        self._messages.put(("session_history", cycle_token, None))
        return dict(record)

    def _claim_cycle_persistence(self, cycle_token: str) -> bool:
        with self._session_lock:
            if cycle_token != self._cycle_token:
                return False
            if cycle_token in self._persisted_cycle_tokens:
                return False
            self._persisted_cycle_tokens.add(cycle_token)
            return True

    def _complete_partial_session(self, cycle_token: str, *, result: object | None) -> None:
        text = self._compose_session_text("")
        if text and self._is_current_cycle(cycle_token):
            record = self._persist_transcript_session(
                cycle_token,
                source={"kind": "microphone"},
                text=text,
                result=result,
                use_result_segments=False,
            )
            if record is not None:
                self._messages.put(("session_record", cycle_token, record))
            self._messages.put(("status", cycle_token, (RunState.IDLE, "已完成", text)))
            return
        self._messages.put(("status", cycle_token, (RunState.IDLE, "没有识别到语音", None)))

    def _wait_for_partial_worker(self, cycle_token: str) -> None:
        worker_done = self._partial_worker_done
        if worker_done is None or worker_done[0] != cycle_token:
            return
        worker_done[1].wait()

    def _settings_for_cycle(self, cycle_token: str) -> AppSettings:
        if cycle_token == self._cycle_token:
            return self._cycle_settings
        return self.settings

    def _asr_engine_for_cycle(self, cycle_token: str) -> object:
        if cycle_token == self._cycle_token:
            return self._cycle_asr_engine
        return self.asr_engine

    def _inject_cycle_text(self, cycle_token: str, text: str) -> bool:
        with self._lifecycle_lock:
            if not self._is_current_cycle(cycle_token):
                return False
            injector = self._cycle_injector
            inject = getattr(injector, "inject", None)
            if callable(inject):
                inject(text)
            return self._is_current_cycle(cycle_token)

    def _release_audio(self, audio: object | None) -> None:
        close = getattr(audio, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                LOGGER.debug("failed to release captured audio", exc_info=True)

    def _join_cycle_workers(self, timeout_seconds: float) -> None:
        current = threading.current_thread()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for worker in (self._partial_worker, self._worker, self._health_worker):
            if worker is None or worker is current or not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        alive = [
            worker.name
            for worker in (self._partial_worker, self._worker, self._health_worker)
            if worker and worker.is_alive()
        ]
        if alive:
            LOGGER.warning("workers did not stop before shutdown timeout: %s", ", ".join(alive))

    def _apply_pending_streaming_runtime_refresh(self) -> None:
        if isinstance(self.recorder, FixedDurationRecorder):
            self.recorder.allow_fallback_silence = self._uses_dummy_backend(self.settings)
        if not self._pending_streaming_runtime_refresh:
            return
        self._pending_streaming_runtime_refresh = False
        self._close_prepared_streaming_recognizer()
        self._prewarm_streaming_recognizer(wait=False)

    def _set_status(
        self,
        state: RunState,
        message: str,
        last_text: str | None = None,
    ) -> None:
        self.status = self.status.replace(state=state, message=message, last_text=last_text)
        self.ui.set_status(message)

    def _should_use_streaming_transcription(self) -> bool:
        if not self.settings.streaming_transcription_enabled:
            return False
        return self.settings.streaming_backend.strip().lower() in {"funasr", "paraformer", "sherpa-onnx", "sherpa", "online"}

    def _build_streaming_recognizer(self, settings: AppSettings | None = None) -> object:
        settings = resolve_runtime_settings(settings or (self._cycle_settings if self._running else self.settings))
        backend = settings.streaming_backend.strip().lower()
        if backend in {"sherpa-onnx", "sherpa", "online"}:
            from .streaming_process import PreparedSubprocessStreamingRecognizer

            model_dir = settings.sherpa_streaming_model_dir.strip() or str(bundled_sherpa_streaming_model_dir())
            prepared = self._prepared_streaming_recognizer
            if isinstance(prepared, PreparedSubprocessStreamingRecognizer):
                if (
                    str(prepared.model_dir) == model_dir
                    and prepared.chunk_ms == settings.streaming_chunk_ms
                    and prepared.update_interval_ms == settings.streaming_update_interval_ms
                    and prepared.num_threads == self._resource_decision.online_threads
                ):
                    return prepared
            return PreparedSubprocessStreamingRecognizer(
                model_dir,
                chunk_ms=settings.streaming_chunk_ms,
                update_interval_ms=settings.streaming_update_interval_ms,
                num_threads=self._resource_decision.online_threads,
            )

        from .funasr_streaming_process import FunASRPreparedStreamingRecognizer

        prepared = self._prepared_streaming_recognizer
        review_interval = (
            float(settings.partial_transcription_interval_seconds or DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS)
            if settings.partial_transcription_enabled
            else 0.0
        )
        language = engine_language_code(settings.recognition_language)
        if isinstance(prepared, FunASRPreparedStreamingRecognizer):
            if (
                prepared.streaming_model == settings.funasr_streaming_model
                and prepared.offline_model == settings.funasr_offline_model
                and prepared.vad_model == settings.funasr_vad_model
                and prepared.punc_model == settings.funasr_punc_model
                and prepared.speaker_model == settings.funasr_speaker_model
                and prepared.chunk_ms == settings.streaming_chunk_ms
                and prepared.update_interval_ms == settings.streaming_update_interval_ms
                and prepared.num_threads == self._resource_decision.online_threads
                and prepared.review_num_threads == self._resource_decision.review_threads
                and prepared.release_online_before_review
                == self._resource_decision.release_online_after_recording
                and prepared.retain_review_worker == self._resource_decision.retain_review_worker
                and prepared.language == language
                and prepared.review_interval_seconds == review_interval
            ):
                return prepared
        return FunASRPreparedStreamingRecognizer(
            streaming_model=settings.funasr_streaming_model,
            offline_model=settings.funasr_offline_model,
            vad_model=settings.funasr_vad_model,
            punc_model=settings.funasr_punc_model,
            speaker_model=settings.funasr_speaker_model,
            chunk_ms=settings.streaming_chunk_ms,
            update_interval_ms=settings.streaming_update_interval_ms,
            num_threads=self._resource_decision.online_threads,
            review_num_threads=self._resource_decision.review_threads,
            release_online_before_review=self._resource_decision.release_online_after_recording,
            retain_review_worker=self._resource_decision.retain_review_worker,
            language=language,
            review_interval_seconds=review_interval,
        )

    def _prewarm_streaming_recognizer(self, *, wait: bool = False) -> None:
        if not self._should_use_streaming_transcription():
            return
        if not self._resource_decision.online_prewarm:
            LOGGER.info("streaming recognizer prewarm disabled by resource profile")
            return
        if not self._can_prewarm_streaming_recognizer():
            LOGGER.info("streaming recognizer prewarm skipped until bundled/local FunASR models are available")
            return
        recognizer: object | None = None
        previous = self._prepared_streaming_recognizer
        try:
            recognizer = self._build_streaming_recognizer()
            prepare = getattr(recognizer, "prepare", None)
            if callable(prepare):
                prepare()
            if wait:
                wait_until_ready = getattr(recognizer, "wait_until_ready", None)
                if callable(wait_until_ready):
                    wait_until_ready(timeout=120)
            self._prepared_streaming_recognizer = recognizer
            if previous is not None and previous is not recognizer:
                self._close_asr_engine(previous)
            LOGGER.info("streaming recognizer prewarm started")
        except Exception:
            if recognizer is not None:
                if self._prepared_streaming_recognizer is recognizer:
                    self._prepared_streaming_recognizer = None
                self._close_asr_engine(recognizer)
            LOGGER.warning("failed to prewarm streaming recognizer", exc_info=True)

    def _can_prewarm_streaming_recognizer(self) -> bool:
        settings = resolve_runtime_settings(self.settings)
        backend = settings.streaming_backend.strip().lower()
        if backend in {"sherpa-onnx", "sherpa", "online"}:
            return has_bundled_sherpa_streaming_model() or bool(settings.sherpa_streaming_model_dir.strip())
        if backend not in {"funasr", "paraformer"}:
            return False
        if has_bundled_funasr_streaming_model():
            return True
        return all(
            Path(value).expanduser().is_dir()
            for value in (settings.funasr_streaming_model,)
        )

    def _asr_runtime_settings_changed(self, previous: AppSettings, current: AppSettings) -> bool:
        keys = (
            "asr_backend",
            "whisper_model_path",
            "whisper_executable",
            "funasr_model_root",
            "funasr_offline_model",
            "funasr_vad_model",
            "funasr_punc_model",
            "funasr_speaker_model",
        )
        return any(getattr(previous, key) != getattr(current, key) for key in keys)

    def _streaming_runtime_settings_changed(self, previous: AppSettings, current: AppSettings) -> bool:
        keys = (
            "streaming_transcription_enabled",
            "streaming_backend",
            "sherpa_streaming_model_dir",
            "funasr_streaming_model",
            "funasr_offline_model",
            "funasr_vad_model",
            "funasr_punc_model",
            "funasr_speaker_model",
            "streaming_chunk_ms",
            "streaming_update_interval_ms",
        )
        return any(getattr(previous, key) != getattr(current, key) for key in keys)

    def _sync_prepared_streaming_session_settings(self, settings: AppSettings) -> None:
        prepared = self._prepared_streaming_recognizer
        if prepared is None:
            return
        if hasattr(prepared, "language"):
            try:
                setattr(prepared, "language", engine_language_code(settings.recognition_language))
            except Exception:
                LOGGER.debug("failed to update prepared streaming language", exc_info=True)
        if hasattr(prepared, "review_interval_seconds"):
            review_interval = (
                float(settings.partial_transcription_interval_seconds or DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS)
                if settings.partial_transcription_enabled
                else 0.0
            )
            try:
                setattr(prepared, "review_interval_seconds", review_interval)
            except Exception:
                LOGGER.debug("failed to update prepared streaming review interval", exc_info=True)

    def _close_prepared_streaming_recognizer(self) -> None:
        prepared = self._prepared_streaming_recognizer
        self._prepared_streaming_recognizer = None
        close = getattr(prepared, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                LOGGER.warning("failed to close prepared streaming recognizer", exc_info=True)

    def _streaming_hotwords(self, settings: AppSettings | None = None) -> tuple[str, ...]:
        settings = settings or self.settings
        if not settings.lexicon_enabled:
            return ()
        lexicons: list[Lexicon] = []
        bundled_paths = bundled_lexicon_paths(
            settings.enabled_builtin_lexicons,
            product_tier=settings.product_tier.strip() or None,
        )
        if bundled_paths:
            try:
                lexicons.append(Lexicon.from_files(bundled_paths))
            except Exception:
                LOGGER.warning("failed to load bundled hotwords", exc_info=True)
        lexicon_path = settings.lexicon_path.strip()
        if lexicon_path:
            try:
                lexicons.append(Lexicon.from_file(lexicon_path, weight=1000))
            except Exception:
                LOGGER.warning("failed to load custom hotwords: %s", lexicon_path, exc_info=True)
        if not lexicons:
            return ()
        merged = lexicons[0].merge(*lexicons[1:])
        return merged.canonical_terms(product_tier=settings.product_tier.strip() or None, limit=200)

    def _begin_streaming_text(self) -> None:
        begin = getattr(self.ui, "begin_streaming_text", None)
        if callable(begin):
            begin()

    def _poll_latest_streaming_event(self) -> None:
        now = time.monotonic()
        if now - self._last_streaming_ui_update_at < _STREAMING_UI_INTERVAL_SECONDS:
            return
        with self._streaming_message_lock:
            latest = self._latest_streaming_event
            self._latest_streaming_event = None
        if latest is None:
            return
        token, event = latest
        if token != self._cycle_token:
            return
        self._last_streaming_ui_update_at = now
        self._update_streaming_text(event)

    def _clear_latest_streaming_event(self, cycle_token: str | None = None) -> None:
        with self._streaming_message_lock:
            if cycle_token is None or self._latest_streaming_event is None:
                self._latest_streaming_event = None
            elif self._latest_streaming_event[0] == cycle_token:
                self._latest_streaming_event = None
        self._last_streaming_ui_update_at = 0.0

    def _update_streaming_text(self, event: object) -> None:
        raw_text = getattr(event, "text", "")
        close_sentence = bool(getattr(event, "is_final", False) or getattr(event, "is_endpoint", False))
        settings = self._cycle_settings if self._running else self.settings
        text = normalize_streaming_text(str(raw_text).strip(), settings, close_sentence=close_sentence)
        update = getattr(self.ui, "set_streaming_text", None)
        if callable(update):
            update(text)
        elif text:
            self.ui.append_text(text)

    def _commit_streaming_text(self, text: str) -> None:
        commit = getattr(self.ui, "commit_streaming_text", None)
        if callable(commit):
            commit(text)
        elif text:
            self.ui.append_text(text)

    def _load_default_injector(self) -> object:
        if not self.settings.inject_text:
            return NullInjector()
        try:
            from .injector import create_injector

            return create_injector()
        except Exception:
            return NullInjector()

    def _transcribe(
        self,
        audio: object,
        *,
        timeout_seconds: float | None | object = _DEFAULT_TRANSCRIPTION_TIMEOUT,
        settings: AppSettings | None = None,
        asr_engine: object | None = None,
        cancel_event: threading.Event | None = None,
    ) -> object:
        with self._transcribe_lock:
            settings = settings or self.settings
            engine = asr_engine or self.asr_engine
            transcribe = engine.transcribe
            transcribe_audio = getattr(getattr(audio, "pcm", None), "audio_path", None) or audio
            try:
                parameters = signature(transcribe).parameters
            except (TypeError, ValueError):
                parameters = {}

            accepts_options = any(
                parameter.kind == Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            language = engine_language_code(settings.recognition_language)
            options: dict[str, object] = {}
            if timeout_seconds is _DEFAULT_TRANSCRIPTION_TIMEOUT:
                resolved_timeout = self._transcription_timeout_seconds(audio)
                if resolved_timeout is not None:
                    options["timeout_seconds"] = resolved_timeout
            else:
                options["timeout_seconds"] = timeout_seconds
            hotwords = self._streaming_hotwords(settings)
            if hotwords:
                options["hotwords"] = hotwords
            if accepts_options:
                options["cancel_event"] = cancel_event or self._recognition_cancel_event
                return transcribe(transcribe_audio, language=language, **options)
            if "language" in parameters:
                return transcribe(transcribe_audio, language=language)
            return transcribe(transcribe_audio)

    def _raise_if_cancelled(self, cancel_event: threading.Event) -> None:
        if not cancel_event.is_set():
            return
        from .asr import ASREngineCancelled

        raise ASREngineCancelled("recognition cancelled")

    def _transcription_timeout_seconds(self, audio: object) -> float | None:
        if isinstance(audio, (str, Path)):
            return None

        duration = getattr(audio, "duration_seconds", None)
        if duration is None:
            return None
        try:
            duration_seconds = float(duration)
        except (TypeError, ValueError):
            return None
        return max(120.0, duration_seconds * 4.0 + 60.0)

    def _audio_file_timeout_seconds(self, duration_seconds: float | None) -> float:
        if duration_seconds is None:
            duration_seconds = float(DEFAULT_MAX_IMPORT_AUDIO_SECONDS)
        return max(120.0, min(float(duration_seconds), float(DEFAULT_MAX_IMPORT_AUDIO_SECONDS)) * 4.0 + 60.0)

    def _audio_file_transcode_timeout_seconds(self, duration_seconds: float | None) -> float:
        if duration_seconds is None:
            return 30.0 * 60.0
        return max(120.0, min(30.0 * 60.0, float(duration_seconds) * 0.5 + 60.0))

    @staticmethod
    def _is_recognition_cancelled(exc: Exception) -> bool:
        from .asr import ASREngineCancelled

        return isinstance(exc, (ASREngineCancelled, AudioTranscodeCancelled))

    def _validate_settings(self, settings: AppSettings) -> str | None:
        hotkey = settings.hotkey.strip()
        if settings.enable_hotkey:
            try:
                from .hotkeys import parse_hotkey

                parse_hotkey(hotkey)
            except Exception as exc:
                return f"快捷键无效：{exc}"

        valid_languages = {value for value, _label in LANGUAGE_OPTIONS}
        if settings.recognition_language not in valid_languages:
            return "识别语言设置无效，请重新选择。"

        if settings.lexicon_enabled:
            lexicon_path = settings.lexicon_path.strip()
            if lexicon_path:
                try:
                    Lexicon.from_file(lexicon_path)
                except Exception as exc:
                    return f"词库文件不可用：{exc}"
            elif not bundled_lexicon_paths(settings.enabled_builtin_lexicons, product_tier=settings.product_tier.strip() or None):
                return "未找到内置词库，请选择词库文件。"

        backend = settings.asr_backend.strip().lower()
        if backend in {"funasr", "paraformer", "funasr-offline", "local"}:
            for label, value in (
                ("FunASR offline model", settings.funasr_offline_model),
                ("FunASR VAD model", settings.funasr_vad_model),
                ("FunASR punctuation model", settings.funasr_punc_model),
            ):
                text = str(value).strip()
                if not text:
                    return f"{label} is not configured."
                if any(separator in text for separator in ("/", "\\")) and not Path(text).expanduser().exists():
                    return f"{label} does not exist: {text}"
        elif backend in {"whisper", "whisper.cpp", "whisper-cpp"}:
            model_path = settings.whisper_model_path.strip()
            if not model_path:
                return "已选择 whisper.cpp 时，必须指定模型文件路径。"
            if not os.path.isfile(os.path.expanduser(model_path)):
                return f"模型文件不存在或不是文件：{settings.whisper_model_path}"
            executable = settings.whisper_executable.strip() or "whisper-cli"
            if not self._is_executable_available(executable):
                return f"找不到 whisper.cpp 执行文件：{executable}"
        return None

    def _uses_dummy_backend(self, settings: AppSettings) -> bool:
        return settings.asr_backend.strip().lower() in {"", "dummy", "mock"}

    def _is_executable_available(self, executable: str) -> bool:
        from pathlib import Path
        import shutil

        executable = executable.strip()
        if not executable:
            return False
        if Path(executable).expanduser().is_file():
            return True
        return shutil.which(executable) is not None

    def _is_current_cycle(self, cycle_token: str) -> bool:
        return cycle_token == self._cycle_token and self._running

    def _audio_file_transcription_unavailable_message(self) -> str | None:
        return self._offline_recognition_unavailable_message(feature="audio_import")

    def _offline_recognition_unavailable_message(self, *, feature: str) -> str | None:
        default_funasr_without_model = (
            self.asr_engine.__class__.__name__ == "FunASROfflineEngine"
            and not str(getattr(self.asr_engine, "model", "")).strip()
        )
        if not isinstance(self.asr_engine, UnavailableASREngine):
            if not default_funasr_without_model:
                return None
        stream_only_package = (
            packaged_model_profile() == "streaming"
            and has_bundled_funasr_streaming_model()
            and not has_bundled_funasr_review_models()
            and not has_bundled_whisper()
        )
        if stream_only_package:
            if feature == "audio_import":
                return (
                    "当前安装的是轻量实时包，仅内置实时录音转写模型，暂不支持导入音频文件离线识别。\n\n"
                    "请使用“开始录制”进行实时转写，或安装完整离线包后再导入音频。"
                )
            return (
                "当前安装的是轻量实时包，仅内置实时录音转写模型，暂不支持关闭实时转写后的结束后识别。\n\n"
                "请在设置中开启实时流式转写，或安装完整离线包后使用录完再识别。"
            )
        if default_funasr_without_model:
            return "FunASR 离线识别模型未配置，无法使用导入音频或录完再识别。请配置离线模型后重试。"
        return None

    def _describe_hotkey_error(self) -> str | None:
        error = self.hotkeys.last_error
        if error is None:
            return None
        return f"热键注册失败：{error}"

    def _friendly_error(self, exc: object) -> str:
        from .asr import ASREngineError, ASREngineUnavailable
        from .audio import AudioCaptureError
        from .injector import InjectionError

        if isinstance(exc, AudioCaptureError):
            return (
                f"{exc}\n\n"
                "请检查麦克风权限、默认输入设备和音频驱动。"
                "如果只是预览安装和界面流程，可先导入一段本地音频验证转写链路。"
            )
        if isinstance(exc, ASREngineUnavailable):
            return f"{exc}\n\n请检查 FunASR 模型文件、运行时依赖和词库配置。"
        if isinstance(exc, ASREngineError):
            return f"{exc}\n\n请先用开始菜单中的系统诊断工具验证 FunASR 模型、麦克风和 FFmpeg。"
        if isinstance(exc, InjectionError):
            return f"{exc}\n\n请确认目标输入框已有焦点，或暂时关闭“识别后注入到当前输入框”。"
        if isinstance(exc, RuntimeError):
            return str(exc)
        return f"{exc}\n\n调试信息:\n{traceback.format_exc(limit=3)}"

    def _help_file_path(self) -> Path:
        executable_dir = Path(sys.executable).resolve().parent
        repo_root = Path(__file__).resolve().parents[2]
        candidates = (
            executable_dir / "docs" / "使用帮助.md",
            executable_dir / "docs" / "验收清单.md",
            executable_dir / "README.md",
            repo_root / "docs" / "使用帮助.md",
            repo_root / "docs" / "验收清单.md",
            repo_root / "README.md",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate

        fallback = self.settings_store.path.parent / "使用帮助.md"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        fallback.write_text(
            "# 语音转文字使用帮助\n\n"
            "1. 点击“开始”开始录音。\n"
            "2. 点击“结束”停止录音并整理最终文本。\n"
            "3. 在“语言”菜单或“设置 > 识别”中切换识别语言。\n"
            "4. 在“设置”中开启分段转写、词库优化、快捷键和托盘选项。\n"
            "5. 通过“文件”菜单导入音频或导出 Word/PDF。\n",
            encoding="utf-8",
        )
        return fallback

    def _show_first_run_hint(self) -> None:
        if not self.settings.show_first_run_hint:
            return
        model_profile = packaged_model_profile()
        if model_profile == "streaming":
            message = (
                "当前安装的是轻量实时包，已内置 FunASR 流式模型，可直接进行实时录音转写。\n\n"
                "导入音频、结束后高精度复核、VAD 和标点模型未包含在此包中；需要这些能力时请安装完整离线包。"
            )
        elif has_bundled_funasr_review_models(require_punctuation=True):
            message = (
                "当前安装的是完整离线包，已内置流式识别和高精度复核模型。\n\n"
                "实时转写使用 FunASR 流式模型，结束后会用高精度模型、VAD、标点和词库热词复核。"
            )
        else:
            message = (
                "当前安装包标记为完整离线包，但复核模型资源不完整。\n\n"
                "实时转写仍可尝试使用；导入音频或结束后复核前，请先运行“帮助 > 系统诊断”检查模型文件。"
            )
        self.ui.show_info(
            "使用提示",
            message,
        )
        self.settings = AppSettings.from_dict(
            {**self.settings.to_dict(), "show_first_run_hint": False}
        )
        self.settings_store.save(self.settings)


def _legacy_ui_factory(app: "VoiceInputApp") -> UiPort:
    from .qt_ui import QtVoiceInputWindow

    return QtVoiceInputWindow(app)


def _preferred_ui_factory(app: "VoiceInputApp") -> UiPort:
    try:
        from .web_runtime import FlipGlassVoiceInputWindow, web_runtime_index

        index_path = web_runtime_index()
        if not index_path.is_file() or index_path.stat().st_size <= 0:
            raise FileNotFoundError(f"Flip Glass Web UI resource is missing or empty: {index_path}")
        return FlipGlassVoiceInputWindow(app)
    except Exception:
        LOGGER.exception("failed to create Flip Glass WebEngine UI; falling back to legacy Qt UI")
        return _legacy_ui_factory(app)


def main() -> None:
    if "--voice-input-health" in sys.argv:
        from .healthcheck import main as healthcheck_main

        flag_index = sys.argv.index("--voice-input-health")
        raise SystemExit(healthcheck_main(sys.argv[flag_index + 1 :]))

    if "--voice-input-diagnose" in sys.argv:
        from .diagnose_asr import main as diagnose_main

        flag_index = sys.argv.index("--voice-input-diagnose")
        raise SystemExit(diagnose_main(sys.argv[flag_index + 1 :]))

    if "--voice-input-evaluate" in sys.argv:
        from .accuracy_eval import main as evaluate_main

        flag_index = sys.argv.index("--voice-input-evaluate")
        raise SystemExit(evaluate_main(sys.argv[flag_index + 1 :]))

    if "--voice-input-stream-evaluate" in sys.argv:
        from .streaming_accuracy_eval import main as streaming_evaluate_main

        flag_index = sys.argv.index("--voice-input-stream-evaluate")
        raise SystemExit(streaming_evaluate_main(sys.argv[flag_index + 1 :]))

    if "--funasr-streaming-worker" in sys.argv:
        from .funasr_streaming_worker import main as funasr_streaming_worker_main

        raise SystemExit(funasr_streaming_worker_main())

    if "--funasr-offline-worker" in sys.argv:
        from .funasr_offline_worker import main as funasr_offline_worker_main

        raise SystemExit(funasr_offline_worker_main())

    if "--streaming-worker" in sys.argv:
        from .streaming_worker import main as streaming_worker_main

        raise SystemExit(streaming_worker_main())

    ui_factory = _legacy_ui_factory if "--legacy-ui" in sys.argv else _preferred_ui_factory

    app = VoiceInputApp(ui_factory=ui_factory)
    app.run()
    app.ui.mainloop()
