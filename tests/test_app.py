from __future__ import annotations

from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from voice_input import app as app_module
from voice_input.audio import AudioLevel, MicrophoneState
from voice_input.app import (
    FixedDurationRecorder,
    VoiceInputApp,
    build_asr_engine,
    normalize_recognition_text,
    normalize_streaming_text,
    normalize_transcript_text,
    resolve_runtime_settings,
)
from voice_input.asr import ASREngineCancelled, ASRResult, DummyASREngine, FunASROfflineEngine, WhisperCppEngine
from voice_input.config import DEFAULT_MAX_RECORDING_SECONDS
from voice_input.resource_policy import (
    ResourceProfile,
    SystemResourceSnapshot,
    build_resource_decision,
)
from voice_input.settings import AppSettings, SettingsStore
from voice_input.transcript_history import TranscriptHistoryStore


def test_main_dispatches_packaged_tool_subcommands(monkeypatch) -> None:
    cases = (
        ("--voice-input-health", "voice_input.healthcheck"),
        ("--voice-input-diagnose", "voice_input.diagnose_asr"),
        ("--voice-input-evaluate", "voice_input.accuracy_eval"),
        ("--voice-input-stream-evaluate", "voice_input.streaming_accuracy_eval"),
    )
    for flag, module_name in cases:
        module = __import__(module_name, fromlist=["main"])
        calls: list[tuple[str, ...]] = []

        def fake_main(argv: list[str] | None = None, *, _calls: list[tuple[str, ...]] = calls) -> int:
            _calls.append(tuple(argv or ()))
            return 17

        monkeypatch.setattr(module, "main", fake_main)
        monkeypatch.setattr(sys, "argv", ["VoiceInputAssistant.exe", flag, "--json", "sample.csv"])

        with pytest.raises(SystemExit) as exc:
            app_module.main()

        assert exc.value.code == 17
        assert calls == [("--json", "sample.csv")]


def test_main_dispatches_funasr_offline_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    import voice_input.funasr_offline_worker as worker_module

    calls: list[bool] = []
    monkeypatch.setattr(worker_module, "main", lambda: calls.append(True) or 0)
    monkeypatch.setattr(sys, "argv", ["VoiceInputAssistant.exe", "--funasr-offline-worker"])

    with pytest.raises(SystemExit) as exc:
        app_module.main()

    assert exc.value.code == 0
    assert calls == [True]


def test_main_legacy_ui_flag_selects_qt_factory(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeVoiceInputApp:
        def __init__(self, *, ui_factory) -> None:
            calls["factory"] = ui_factory
            self.ui = SimpleNamespace(mainloop=lambda: calls.setdefault("mainloop", True))

        def run(self) -> None:
            calls["run"] = True

    monkeypatch.setattr(app_module, "VoiceInputApp", FakeVoiceInputApp)
    monkeypatch.setattr(sys, "argv", ["VoiceInputAssistant.exe", "--legacy-ui"])

    app_module.main()

    assert calls["factory"] is app_module._legacy_ui_factory
    assert calls["run"] is True
    assert calls["mainloop"] is True


def test_preferred_ui_factory_falls_back_when_web_assets_are_missing(monkeypatch, tmp_path: Path) -> None:
    import voice_input.qt_ui as qt_ui_module
    import voice_input.web_runtime as web_runtime_module

    sentinel = object()
    monkeypatch.setattr(web_runtime_module, "web_runtime_index", lambda: tmp_path / "missing.html")
    monkeypatch.setattr(qt_ui_module, "QtVoiceInputWindow", lambda _app: sentinel)

    assert app_module._preferred_ui_factory(object()) is sentinel


@pytest.mark.parametrize("manifest_case", ("missing", "invalid_json", "missing_entry"))
def test_preferred_ui_factory_falls_back_when_web_manifest_is_invalid(
    monkeypatch,
    tmp_path: Path,
    manifest_case: str,
) -> None:
    import hashlib
    import json

    import voice_input.qt_ui as qt_ui_module
    import voice_input.web_runtime as web_runtime_module

    dist = tmp_path / manifest_case
    dist.mkdir()
    index_path = dist / "index.html"
    index_content = b"<html><script src='assets/app.js'></script></html>"
    index_path.write_bytes(index_content)
    manifest_path = dist / "integrity.json"
    if manifest_case == "invalid_json":
        manifest_path.write_text("{not valid json", encoding="utf-8")
    elif manifest_case == "missing_entry":
        asset_path = dist / "assets" / "app.js"
        asset_path.parent.mkdir()
        asset_path.write_bytes(b"window.voiceInput = true;")
        manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "files": [
                        {
                            "path": "index.html",
                            "size": len(index_content),
                            "sha256": hashlib.sha256(index_content).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    sentinel = object()
    legacy_apps: list[object] = []

    def create_legacy(app: object) -> object:
        legacy_apps.append(app)
        return sentinel

    def validate_then_create_web(_app: object) -> object:
        web_runtime_module.validate_web_runtime(index_path)
        return object()

    target_app = object()
    monkeypatch.setattr(web_runtime_module, "web_runtime_index", lambda: index_path)
    monkeypatch.setattr(web_runtime_module, "FlipGlassVoiceInputWindow", validate_then_create_web)
    monkeypatch.setattr(qt_ui_module, "QtVoiceInputWindow", create_legacy)

    assert app_module._preferred_ui_factory(target_app) is sentinel
    assert legacy_apps == [target_app]


def test_default_recorder_allows_two_hour_sessions() -> None:
    assert DEFAULT_MAX_RECORDING_SECONDS == 2 * 60 * 60
    assert FixedDurationRecorder().duration_seconds == 2 * 60 * 60


def test_recognition_completion_log_does_not_include_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_text = "confidential customer transcript"

    with caplog.at_level("INFO", logger="voice_input.app"):
        app_module._log_recognition_completed("microphone", sensitive_text)

    assert sensitive_text not in caplog.text
    assert "characters=32" in caplog.text


def test_fixed_duration_recorder_keeps_completed_snapshot_source(monkeypatch) -> None:
    class FakeMicrophoneRecorder:
        def __init__(self, *, max_duration_seconds: float) -> None:
            self.max_duration_seconds = max_duration_seconds
            self.snapshot_calls = 0

        def record_until_stopped(self) -> bytes:
            return b"full-audio"

        def snapshot_since_last(self) -> str:
            self.snapshot_calls += 1
            return "last-segment"

    monkeypatch.setattr("voice_input.audio.MicrophoneRecorder", FakeMicrophoneRecorder)

    recorder = FixedDurationRecorder(duration_seconds=12)
    assert recorder.record_once() == b"full-audio"

    assert recorder.snapshot_since_last() == "last-segment"


def test_fixed_duration_recorder_forwards_audio_monitor(monkeypatch) -> None:
    class FakeMicrophoneRecorder:
        def __init__(self, *, max_duration_seconds: float) -> None:
            self.max_duration_seconds = max_duration_seconds
            self.on_level = None
            self.on_state = None

        def set_audio_monitor(self, on_level, on_state) -> None:
            self.on_level = on_level
            self.on_state = on_state

        def record_until_stopped(self) -> bytes:
            self.on_level(AudioLevel(0.75, 0.25, 0.5, -12.0, -6.0))
            self.on_state(MicrophoneState(status="active", name="Test microphone"))
            return b"audio"

    monkeypatch.setattr("voice_input.audio.MicrophoneRecorder", FakeMicrophoneRecorder)
    levels = []
    states = []
    recorder = FixedDurationRecorder(duration_seconds=12)
    recorder.set_audio_monitor(levels.append, states.append)

    assert recorder.record_once() == b"audio"
    assert levels[0].level == 0.75
    assert states[0].name == "Test microphone"


def _patch_bundled_funasr(monkeypatch) -> None:
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: True)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: True)
    monkeypatch.setattr("voice_input.app.bundled_funasr_streaming_model", lambda: "models/funasr/streaming")
    monkeypatch.setattr("voice_input.app.bundled_funasr_offline_model", lambda: "models/funasr/offline")
    monkeypatch.setattr("voice_input.app.bundled_funasr_vad_model", lambda: "models/funasr/vad")
    monkeypatch.setattr("voice_input.app.bundled_funasr_punc_model", lambda: "models/funasr/punc")
    monkeypatch.setattr("voice_input.app.bundled_funasr_speaker_model", lambda: "models/funasr/speaker")


def _patch_no_bundled_asr(monkeypatch) -> None:
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_whisper", lambda: False)


def test_build_asr_engine_defaults_to_bundled_funasr(monkeypatch) -> None:
    _patch_bundled_funasr(monkeypatch)

    engine = build_asr_engine(AppSettings(asr_backend="dummy"))

    assert isinstance(engine, FunASROfflineEngine)
    assert Path(engine.model) == Path("models/funasr/offline")


def test_build_asr_engine_can_use_dummy_when_selected_without_bundle(monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)

    engine = build_asr_engine(AppSettings(asr_backend="dummy"))

    assert isinstance(engine, DummyASREngine)


def test_build_asr_engine_can_select_whisper_cpp_without_bundle(monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)

    engine = build_asr_engine(
        AppSettings(
            asr_backend="whisper.cpp",
            whisper_model_path="models/ggml-tiny.bin",
            whisper_executable="bin/whisper-cli",
        )
    )

    assert isinstance(engine, WhisperCppEngine)
    assert Path(engine.model_path) == Path("models/ggml-tiny.bin")
    assert engine.executable == "bin/whisper-cli"


def test_resolve_runtime_settings_fills_bundled_paths(monkeypatch) -> None:
    _patch_bundled_funasr(monkeypatch)

    settings = resolve_runtime_settings(
        AppSettings(
            asr_backend="dummy",
            whisper_model_path="",
            whisper_executable="whisper-cli",
        )
    )

    assert settings.asr_backend == "funasr"
    assert settings.funasr_streaming_model == "models/funasr/streaming"
    assert settings.funasr_offline_model == "models/funasr/offline"
    assert settings.funasr_vad_model == "models/funasr/vad"
    assert settings.funasr_punc_model == "models/funasr/punc"
    assert settings.funasr_speaker_model == "models/funasr/speaker"
    assert settings.product_tier == "enterprise"


def test_resolve_runtime_settings_repairs_stale_funasr_model_paths(monkeypatch, tmp_path: Path) -> None:
    current_root = tmp_path / "current" / "models" / "funasr"
    current_punc = current_root / "punc_ct-transformer_cn-en-common-vocab471067-large"
    current_punc.mkdir(parents=True)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: True)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: True)
    monkeypatch.setattr("voice_input.app.bundled_funasr_streaming_model", lambda: "models/funasr/streaming")
    monkeypatch.setattr("voice_input.app.bundled_funasr_offline_model", lambda: "models/funasr/offline")
    monkeypatch.setattr("voice_input.app.bundled_funasr_vad_model", lambda: "models/funasr/vad")
    monkeypatch.setattr("voice_input.app.bundled_funasr_punc_model", lambda: str(current_punc))

    settings = resolve_runtime_settings(
        AppSettings(
            asr_backend="funasr",
            funasr_punc_model=str(
                tmp_path
                / "old"
                / "models"
                / "funasr"
                / "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
            ),
        )
    )

    assert settings.funasr_punc_model == str(current_punc)


def test_resolve_runtime_settings_promotes_hidden_tier_for_selected_builtin_lexicons(monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    monkeypatch.setattr("voice_input.app.has_bundled_sherpa_streaming_model", lambda: False)

    settings = resolve_runtime_settings(
        AppSettings(
            product_tier="standard",
            enabled_builtin_lexicons=("government_digital", "official_document", "office_meeting"),
        )
    )

    assert settings.product_tier == "enterprise"


def test_resolve_runtime_settings_caps_builtin_lexicons_to_packaged_tier(monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    monkeypatch.setattr("voice_input.app.has_bundled_sherpa_streaming_model", lambda: False)
    monkeypatch.setattr("voice_input.app.packaged_product_tier", lambda: "basic")

    settings = resolve_runtime_settings(
        AppSettings(
            product_tier="enterprise",
            enabled_builtin_lexicons=("government_digital", "official_document", "office_meeting"),
        )
    )

    assert settings.product_tier == "basic"
    assert settings.enabled_builtin_lexicons == ("government_digital", "official_document", "office_meeting")


def test_resolve_runtime_settings_respects_packaged_enabled_builtin_lexicons(monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    monkeypatch.setattr("voice_input.app.has_bundled_sherpa_streaming_model", lambda: False)
    monkeypatch.setattr("voice_input.app.packaged_product_tier", lambda: "standard")
    monkeypatch.setattr("voice_input.app.packaged_enabled_builtin_lexicons", lambda: ("government_digital",))

    settings = resolve_runtime_settings(
        AppSettings(
            product_tier="standard",
            enabled_builtin_lexicons=("government_digital", "official_document"),
        )
    )

    assert settings.product_tier == "standard"
    assert settings.enabled_builtin_lexicons == ("government_digital",)


def test_resolve_runtime_settings_repairs_stale_bundled_absolute_paths(monkeypatch, tmp_path: Path) -> None:
    bundled_model = tmp_path / "current" / "models" / "ggml-base.bin"
    bundled_executable = tmp_path / "current" / "engines" / "whispercpp" / "bin" / "whisper-cli.exe"
    bundled_model.parent.mkdir(parents=True)
    bundled_executable.parent.mkdir(parents=True)
    bundled_model.write_bytes(b"model")
    bundled_executable.write_bytes(b"exe")
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_whisper", lambda: True)
    monkeypatch.setattr("voice_input.resources.bundled_whisper_model", lambda: bundled_model)
    monkeypatch.setattr("voice_input.resources.bundled_whisper_executable", lambda: bundled_executable)

    settings = resolve_runtime_settings(
        AppSettings(
            asr_backend="whisper.cpp",
            whisper_model_path=str(tmp_path / "old" / "models" / "ggml-base.bin"),
            whisper_executable=str(tmp_path / "old" / "engines" / "whispercpp" / "bin" / "whisper-cli.exe"),
        )
    )

    assert Path(settings.whisper_model_path) == bundled_model
    assert Path(settings.whisper_executable) == bundled_executable


def test_resolve_runtime_settings_keeps_existing_custom_paths(monkeypatch, tmp_path: Path) -> None:
    bundled_model = tmp_path / "current" / "models" / "ggml-base.bin"
    bundled_executable = tmp_path / "current" / "engines" / "whispercpp" / "bin" / "whisper-cli.exe"
    custom_model = tmp_path / "custom" / "my-model.gguf"
    custom_executable = tmp_path / "custom" / "my-whisper.exe"
    for path in (bundled_model, bundled_executable, custom_model, custom_executable):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_whisper", lambda: True)
    monkeypatch.setattr("voice_input.resources.bundled_whisper_model", lambda: bundled_model)
    monkeypatch.setattr("voice_input.resources.bundled_whisper_executable", lambda: bundled_executable)

    settings = resolve_runtime_settings(
        AppSettings(
            asr_backend="whisper.cpp",
            whisper_model_path=str(custom_model),
            whisper_executable=str(custom_executable),
        )
    )

    assert Path(settings.whisper_model_path) == custom_model
    assert Path(settings.whisper_executable) == custom_executable


def test_update_settings_uses_bundled_funasr_when_available(monkeypatch, tmp_path: Path) -> None:
    _patch_bundled_funasr(monkeypatch)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.update_settings(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))

    assert app.settings.asr_backend == "funasr"
    assert isinstance(app.asr_engine, FunASROfflineEngine)


def test_set_startup_enabled_updates_settings_and_ui(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    app.ui.set_autostart = Mock()
    monkeypatch.setattr("voice_input.app.set_run_on_startup", lambda *args, **kwargs: None)
    monkeypatch.setattr("voice_input.app.clear_run_on_startup", lambda *args, **kwargs: None)

    app.set_startup_enabled(True)

    assert app.settings.launch_on_startup is True
    assert store.load().launch_on_startup is True
    app.ui.set_autostart.assert_called_with(True)


def test_startup_launch_command_preserves_legacy_ui(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    monkeypatch.setattr("voice_input.app.sys.argv", ["voice-input", "--legacy-ui"])
    monkeypatch.setattr("voice_input.app.sys.frozen", False, raising=False)

    _target, arguments = app._startup_launch_command()

    assert arguments == "-m voice_input --legacy-ui"


def test_help_file_uses_packaged_docs_when_available(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app_dir = tmp_path / "app"
    help_path = app_dir / "docs" / "使用帮助.md"
    help_path.parent.mkdir(parents=True)
    help_path.write_text("help", encoding="utf-8")
    fake_exe = app_dir / "VoiceInputAssistant.exe"
    fake_exe.write_text("exe", encoding="utf-8")
    monkeypatch.setattr("voice_input.app.sys.executable", str(fake_exe))

    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    assert app._help_file_path() == help_path


def test_open_health_report_writes_json(tmp_path: Path, monkeypatch) -> None:
    from voice_input.healthcheck import HealthCheckItem, HealthReport

    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    opened: list[Path] = []
    monkeypatch.setattr(
        "voice_input.healthcheck.run_health_checks",
        lambda _settings: HealthReport(
            status="ok",
            generated_at=1.0,
            items=(HealthCheckItem("runtime", "ok", "ready"),),
        ),
    )
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    monkeypatch.setattr(app, "_open_path", lambda path: opened.append(Path(path)))

    app.open_health_report()
    deadline = time.time() + 2
    while time.time() < deadline and not opened:
        app._poll_messages()
        time.sleep(0.01)

    report_path = tmp_path / "health-report.json"
    assert opened == [report_path]
    assert '"status": "ok"' in report_path.read_text(encoding="utf-8")


class FakeUI:
    def __init__(self, app: VoiceInputApp) -> None:
        self.app = app
        self.status = []
        self.hidden = 0
        self.closed = 0
        self.info = []
        self.errors = []
        self.running = []
        self.text = []
        self.audio_levels: list[float] = []
        self.microphone_states: list[dict[str, object]] = []
        self.session_histories: list[list[dict[str, object]]] = []

    def set_status(self, text: str) -> None:
        self.status.append(text)

    def append_text(self, text: str) -> None:
        self.text.append(text)

    def begin_streaming_text(self) -> None:
        self.text.append("<stream-begin>")

    def set_streaming_text(self, text: str) -> None:
        self.text.append(f"<stream:{text}>")

    def commit_streaming_text(self, text: str) -> None:
        self.text.append(f"<stream-final:{text}>")

    def set_running(self, running: bool) -> None:
        self.running.append(running)

    def set_audio_level(self, level: float) -> None:
        self.audio_levels.append(level)

    def set_microphone_state(self, state: dict[str, object]) -> None:
        self.microphone_states.append(dict(state))

    def set_session_history(self, sessions: list[dict[str, object]]) -> None:
        self.session_histories.append([dict(session) for session in sessions])

    def show_error(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def show_info(self, title: str, message: str) -> None:
        self.info.append((title, message))

    def after(self, _delay_ms: int, callback) -> None:
        if getattr(callback, "__name__", "") != "_poll_messages":
            callback()

    def show_window(self) -> None:
        pass

    def hide_window(self) -> None:
        self.hidden += 1

    def close_window(self) -> None:
        self.closed += 1


class FakeUIWithTray(FakeUI):
    def __init__(self, app: VoiceInputApp) -> None:
        super().__init__(app)
        self.tray_started = 0
        self.tray_stopped = 0
        self.tray_running = []

    def start_tray(self) -> bool:
        self.tray_started += 1
        return True

    def set_tray_running(self, running: bool) -> None:
        self.tray_running.append(running)

    def stop_tray(self) -> None:
        self.tray_stopped += 1


def test_audio_monitor_updates_are_coalesced_and_dispatched_on_ui_poll(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    token = app._cycle_token
    app._running = True

    app._publish_audio_level(token, AudioLevel(0.25, 0.1, 0.2, -20.0, -14.0))
    app._publish_audio_level(token, AudioLevel(0.8, 0.5, 0.7, -6.0, -3.0))
    app._publish_microphone_state(
        token,
        MicrophoneState(status="active", device=3, name="Conference microphone"),
    )

    assert app._messages.qsize() == 2
    app._poll_messages()

    assert app.audio_level == 0.8
    assert app.audio_level_details["rms_dbfs"] == -6.0
    assert app.ui.audio_levels[-1] == 0.8
    assert app.microphone_state["status"] == "active"
    assert app.ui.microphone_states[-1]["name"] == "Conference microphone"


def test_completed_transcript_session_is_persisted_and_published(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, history_store=history_store)
    token = app._cycle_token
    result = ASRResult(
        text="第一段\n\n第二段",
        engine="test",
        duration_seconds=12.0,
        segments=(
            {"start": 0.0, "end": 5.0, "text": "第一段", "speaker": "S1"},
            {"start": 5.0, "end": 12.0, "text": "第二段", "speaker": "S2"},
        ),
    )

    app._persist_transcript_session(
        token,
        source={"kind": "microphone"},
        text=result.text,
        result=result,
    )
    app._poll_messages()

    sessions = history_store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["paragraphs"] == ["第一段", "第二段"]
    assert sessions[0]["segments"][1]["speaker"] == "S2"
    assert app.ui.session_histories[-1][0]["id"] == sessions[0]["id"]


def test_native_ui_tray_handles_close_to_tray(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(minimize_to_tray=True, show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUIWithTray, settings_store=store)

    app._tray_available = app._start_tray()
    app.request_close()
    app.shutdown()

    assert app.ui.tray_started == 1
    assert app.ui.hidden == 1
    assert app.ui.closed == 0
    assert app.ui.tray_stopped == 1


def test_request_close_minimizes_to_tray(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(minimize_to_tray=True, show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    app.tray.available = True

    app.request_close()

    assert app.ui.hidden == 1
    assert app.ui.closed == 0


def test_startup_minimized_keeps_window_visible_without_tray(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(start_minimized=True, show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    app.tray.start = lambda: False  # type: ignore[method-assign]

    app.run()

    assert app.ui.hidden == 0
    assert app.ui.closed == 0
    assert app.ui.info


def test_request_close_exits_when_tray_disabled(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(minimize_to_tray=False, show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.request_close()

    assert app.ui.closed == 1


def test_request_close_exits_when_tray_unavailable(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(minimize_to_tray=True, show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.request_close()

    assert app.ui.hidden == 0
    assert app.ui.closed == 1
    assert app.ui.info


def test_first_run_hint_is_saved_after_run(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.run()

    assert app.ui.info
    assert store.load().show_first_run_hint is False


def test_first_run_hint_describes_streaming_profile(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=True))
    monkeypatch.setattr("voice_input.app.packaged_model_profile", lambda: "streaming")
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app._show_first_run_hint()

    assert "轻量实时包" in app.ui.info[-1][1]
    assert "完整离线包" in app.ui.info[-1][1]


def test_first_run_hint_warns_when_full_profile_models_are_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=True))
    monkeypatch.setattr("voice_input.app.packaged_model_profile", lambda: "full")
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda **_kwargs: False)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app._show_first_run_hint()

    assert "资源不完整" in app.ui.info[-1][1]
    assert "系统诊断" in app.ui.info[-1][1]


class FakeInjector:
    def __init__(self) -> None:
        self.injected = []

    def inject(self, text: str) -> None:
        self.injected.append(text)


class ManualRecorder:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.stop_calls = 0

    def prepare_recording(self) -> None:
        self.stopped.clear()

    def record_once(self) -> bytes:
        self.started.set()
        self.stopped.wait(timeout=2)
        return b"\x00" * 3200

    def stop(self) -> None:
        self.stop_calls += 1
        self.stopped.set()


class PausableRecorder(ManualRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1


class SnapshotRecorder(ManualRecorder):
    def __init__(self) -> None:
        super().__init__()
        self.snapshots: queue.Queue[object] = queue.Queue()
        self.paused = False

    def snapshot_since_last(self) -> object | None:
        try:
            return self.snapshots.get_nowait()
        except queue.Empty:
            return None

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class StaticASR:
    def transcribe(self, _audio: object, **_options: object) -> str:
        return "手动结束录制"


class EchoASR:
    def __init__(self) -> None:
        self.audio_inputs: list[object] = []

    def transcribe(self, audio: object, **_options: object) -> str:
        self.audio_inputs.append(audio)
        return str(audio)


class FakeStreamingRecognizer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.pause_seen = False
        self.hotwords = ()
        self.last_segments = ()

    def transcribe_microphone(
        self,
        *,
        max_duration_seconds: float,
        stop_event: threading.Event,
        pause_event: threading.Event,
        on_event,
        hotwords=(),
    ) -> str:
        from voice_input.streaming import StreamingRecognitionEvent

        assert max_duration_seconds == DEFAULT_MAX_RECORDING_SECONDS
        self.hotwords = tuple(hotwords)
        self.started.set()
        on_event(StreamingRecognitionEvent(text="你", partial_text="你"))
        on_event(StreamingRecognitionEvent(text="你好", partial_text="你好"))
        deadline = time.time() + 2
        while time.time() < deadline and not stop_event.is_set():
            self.pause_seen = self.pause_seen or pause_event.is_set()
            time.sleep(0.01)
        self.last_segments = (
            {"text": "你好世界", "start": 0.0, "end": 1.5, "spk": 0, "time_unit": "seconds"},
        )
        self.finished.set()
        return "你好世界"


class FinalizationStreamingRecognizer(FakeStreamingRecognizer):
    def __init__(self) -> None:
        super().__init__()
        self.on_finalization_progress = None

    def set_finalization_monitor(self, callback) -> None:
        self.on_finalization_progress = callback

    def transcribe_microphone(self, **kwargs) -> str:
        callback = self.on_finalization_progress
        assert callback is not None
        callback(0, 3)
        callback(2, 3)
        callback(3, 3)
        return super().transcribe_microphone(**kwargs)


class CloseableStreamingRecognizer(FakeStreamingRecognizer):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FinalizationUI(FakeUI):
    def __init__(self, app: VoiceInputApp) -> None:
        super().__init__(app)
        self.finalization_progress: list[tuple[int, int]] = []
        self.finalization_thread_ids: list[int] = []

    def set_finalization_progress(self, completed: int, total: int) -> None:
        self.finalization_progress.append((completed, total))
        self.finalization_thread_ids.append(threading.get_ident())


def test_stop_ends_recording_and_then_recognizes(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    recorder = ManualRecorder()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=StaticASR(),
    )

    app.start()
    assert recorder.started.wait(timeout=2)
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and not app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert recorder.stop_calls == 1
    assert app.ui.text == ["手动结束录制。"]
    assert app.ui.running[-1] is False


def test_start_explains_stream_only_package_when_streaming_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    recorder = ManualRecorder()
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: True)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_whisper", lambda: False)
    monkeypatch.setattr("voice_input.app.packaged_model_profile", lambda: "streaming")
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=FunASROfflineEngine(model=""),
    )

    app.start()

    assert app.is_running is False
    assert recorder.started.is_set() is False
    assert app.ui.errors
    assert app.ui.errors[-1][0] == "当前安装包不支持结束后转写"
    assert "轻量实时包" in app.ui.errors[-1][1]
    assert "完整离线包" in app.ui.errors[-1][1]


def test_pause_and_resume_are_forwarded_to_active_recorder(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    recorder = PausableRecorder()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=StaticASR(),
    )

    app.start()
    assert recorder.started.wait(timeout=2)

    assert app.pause_recording() is True
    assert app.is_recording_paused is True
    assert recorder.pause_calls == 1

    assert app.resume_recording() is True
    assert app.is_recording_paused is False
    assert recorder.resume_calls == 1

    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)
    assert app.ui.running[-1] is False


def test_streaming_transcription_updates_text_before_stop(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = FakeStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()
    assert recognizer.started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and "<stream:你好>" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert "<stream-begin>" in app.ui.text
    assert "<stream:你>" not in app.ui.text
    assert "<stream:你好>" in app.ui.text
    assert app.is_running is True

    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and "<stream-final:你好世界>" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert recognizer.finished.is_set()
    assert app.ui.text[-1] == "<stream-final:你好世界。>"
    assert app.ui.running[-1] is False
    sessions = app.history_store.list_sessions(limit=1)
    assert sessions[0]["segments"] == [
        {"text": "你好世界", "start": 0.0, "end": 1.5, "spk": 0, "time_unit": "seconds"},
    ]


def test_streaming_finalization_progress_is_dispatched_by_ui_poll(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = FinalizationStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FinalizationUI, settings_store=store, asr_engine=StaticASR())
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)
    ui_thread_id = threading.get_ident()

    app.start()
    assert recognizer.started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and len(app.ui.finalization_progress) < 3:
        app._poll_messages()
        time.sleep(0.01)

    assert app.ui.finalization_progress == [(0, 3), (2, 3), (3, 3)]
    assert set(app.ui.finalization_thread_ids) == {ui_thread_id}

    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)


def test_finalization_progress_from_stale_cycle_is_ignored(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    app = VoiceInputApp(ui_factory=FinalizationUI, settings_store=store, asr_engine=StaticASR())
    recognizer = FinalizationStreamingRecognizer()
    stale_token = app._cycle_token
    app._configure_finalization_monitor(recognizer, stale_token)
    app._cycle_token = "current-cycle"

    assert recognizer.on_finalization_progress is not None
    recognizer.on_finalization_progress(1, 4)
    app._poll_messages()

    assert app.ui.finalization_progress == []


def test_balanced_profile_releases_prepared_streaming_worker_after_recording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = CloseableStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    app._resource_decision = build_resource_decision(
        ResourceProfile.BALANCED,
        SystemResourceSnapshot(logical_cpu_count=8),
    )
    app._prepared_streaming_recognizer = recognizer
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()
    assert recognizer.started.wait(timeout=2)
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)

    assert recognizer.closed is True
    assert app._prepared_streaming_recognizer is None


def test_constrained_profile_disables_streaming_prewarm(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    app._resource_decision = build_resource_decision(
        ResourceProfile.CONSTRAINED,
        SystemResourceSnapshot(logical_cpu_count=4),
    )
    build = Mock(side_effect=AssertionError("constrained profile must not prewarm"))
    monkeypatch.setattr(app, "_build_streaming_recognizer", build)

    app._prewarm_streaming_recognizer()

    build.assert_not_called()


def test_high_quality_profile_tracks_worker_when_prewarm_was_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = CloseableStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    app._resource_decision = build_resource_decision(
        ResourceProfile.HIGH_QUALITY,
        SystemResourceSnapshot(logical_cpu_count=16),
    )
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()
    assert recognizer.started.wait(timeout=2)
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)

    assert recognizer.closed is False
    assert app._prepared_streaming_recognizer is recognizer

    app.shutdown()
    assert recognizer.closed is True


def test_streaming_mode_releases_idle_import_worker_before_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class CloseableASR(StaticASR):
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    offline = CloseableASR()
    recognizer = CloseableStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=offline)
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()

    assert offline.closed == 1
    assert recognizer.started.wait(timeout=2)
    app.stop()


def test_failed_streaming_prewarm_closes_partial_worker(tmp_path: Path, monkeypatch) -> None:
    class FailedRecognizer:
        def __init__(self) -> None:
            self.closed = 0

        def prepare(self) -> None:
            raise RuntimeError("prepare failed")

        def close(self) -> None:
            self.closed += 1

    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = FailedRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    monkeypatch.setattr(app, "_can_prewarm_streaming_recognizer", lambda: True)
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app._prewarm_streaming_recognizer()

    assert recognizer.closed == 1
    assert app._prepared_streaming_recognizer is None


def test_sherpa_recognizer_uses_resource_thread_budget(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=True,
            streaming_backend="sherpa-onnx",
            sherpa_streaming_model_dir=str(tmp_path),
        )
    )
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    app._resource_decision = build_resource_decision(
        ResourceProfile.CONSTRAINED,
        SystemResourceSnapshot(logical_cpu_count=4),
    )

    recognizer = app._build_streaming_recognizer()

    assert recognizer.num_threads == 2


def test_shutdown_ends_resource_policy_session(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())

    app.shutdown()

    assert app._resource_policy.active_decision is None


def test_streaming_preview_applies_lexicon_correction(tmp_path: Path, monkeypatch) -> None:
    lexicon_path = tmp_path / "lexicon.txt"
    lexicon_path.write_text("政屋=>政务\n", encoding="utf-8")
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=True,
            lexicon_enabled=True,
            lexicon_path=str(lexicon_path),
            enabled_builtin_lexicons=(),
        )
    )

    class LexiconStreamingRecognizer(FakeStreamingRecognizer):
        def transcribe_microphone(self, *, max_duration_seconds, stop_event, pause_event, on_event, hotwords=()):
            from voice_input.streaming import StreamingRecognitionEvent

            self.started.set()
            on_event(StreamingRecognitionEvent(text="政屋服务", partial_text="政屋服务"))
            deadline = time.time() + 2
            while time.time() < deadline and not stop_event.is_set():
                time.sleep(0.01)
            self.finished.set()
            return "政屋服务"

    recognizer = LexiconStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()
    assert recognizer.started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and "<stream:政务服务>" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert "<stream:政务服务>" in app.ui.text

    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and "<stream-final:政务服务。>" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert app.ui.text[-1] == "<stream-final:政务服务。>"


def test_streaming_pause_and_resume_control_capture_event(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    recognizer = FakeStreamingRecognizer()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())
    monkeypatch.setattr(app, "_build_streaming_recognizer", lambda: recognizer)

    app.start()
    assert recognizer.started.wait(timeout=2)
    assert app.pause_recording() is True
    deadline = time.time() + 2
    while time.time() < deadline and not recognizer.pause_seen:
        app._poll_messages()
        time.sleep(0.01)

    assert recognizer.pause_seen is True
    assert app.resume_recording() is True
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)
    assert app.ui.running[-1] is False


def test_partial_transcription_runs_while_recording_continues(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
            partial_transcription_enabled=True,
            partial_transcription_interval_seconds=0.1,
        )
    )
    recorder = SnapshotRecorder()
    asr = EchoASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=asr,
    )

    app.start()
    assert recorder.started.wait(timeout=2)
    recorder.snapshots.put("partial-1")
    deadline = time.time() + 2
    while time.time() < deadline and "partial-1" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert "partial-1" in app.ui.text
    assert recorder.stop_calls == 0
    assert app.is_running is True

    recorder.snapshots.put("final-tail")
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and (
        "final-tail" not in app.ui.text or not app.ui.running or app.ui.running[-1] is not False
    ):
        app._poll_messages()
        time.sleep(0.01)

    assert asr.audio_inputs[:2] == ["partial-1", "final-tail"]
    assert app.ui.running[-1] is False


def test_partial_transcription_skips_snapshots_while_paused(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
            partial_transcription_enabled=True,
            partial_transcription_interval_seconds=0.1,
        )
    )
    recorder = SnapshotRecorder()
    asr = EchoASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=asr,
    )

    app.start()
    assert recorder.started.wait(timeout=2)
    assert app.pause_recording() is True
    recorder.snapshots.put("paused-segment")
    time.sleep(0.25)
    app._poll_messages()

    assert asr.audio_inputs == []
    assert app.ui.status[-1] == "录音已暂停"

    assert app.resume_recording() is True
    deadline = time.time() + 2
    while time.time() < deadline and "paused-segment" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    assert asr.audio_inputs == ["paused-segment"]
    app.stop()


def test_partial_transcription_uses_final_snapshot_after_recording_stops(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
            partial_transcription_enabled=True,
            partial_transcription_interval_seconds=0.1,
        )
    )
    recorder = SnapshotRecorder()
    recorder.snapshots.put("final-only")
    asr = EchoASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=asr,
    )

    app.start()
    assert recorder.started.wait(timeout=2)
    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and (
        "final-only" not in app.ui.text or not app.ui.running or app.ui.running[-1] is not False
    ):
        app._poll_messages()
        time.sleep(0.01)

    assert asr.audio_inputs == ["final-only"]
    assert b"\x00" * 3200 not in asr.audio_inputs
    assert app.ui.text == ["final-only"]


def test_partial_transcription_persists_existing_parts_when_final_snapshot_is_empty(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
            partial_transcription_enabled=True,
            partial_transcription_interval_seconds=0.1,
        )
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    recorder = SnapshotRecorder()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        history_store=history_store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=EchoASR(),
    )

    app.start()
    assert recorder.started.wait(timeout=2)
    recorder.snapshots.put("kept-part")
    deadline = time.time() + 2
    while time.time() < deadline and "kept-part" not in app.ui.text:
        app._poll_messages()
        time.sleep(0.01)

    app.stop()
    deadline = time.time() + 2
    while time.time() < deadline and app.is_running:
        app._poll_messages()
        time.sleep(0.01)

    sessions = history_store.list_sessions()
    assert [session["text"] for session in sessions] == ["kept-part"]


def test_stop_waits_for_in_flight_partial_before_persisting_session(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
            partial_transcription_enabled=True,
            partial_transcription_interval_seconds=0.1,
        )
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    recorder = SnapshotRecorder()

    class BlockingTailASR:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def transcribe(self, audio: object, **_options: object) -> str:
            if audio == "in-flight-tail":
                self.started.set()
                self.release.wait()
            return str(audio)

    asr = BlockingTailASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        history_store=history_store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=asr,
    )

    try:
        app.start()
        assert recorder.started.wait(timeout=2)
        recorder.snapshots.put("in-flight-tail")
        assert asr.started.wait(timeout=2)

        app.stop()
        worker = app._worker
        assert worker is not None
        worker.join(timeout=0.2)

        assert worker.is_alive()
        assert history_store.list_sessions() == []

        asr.release.set()
        deadline = time.time() + 2
        while time.time() < deadline and app.is_running:
            app._poll_messages()
            time.sleep(0.01)
        worker.join(timeout=1)

        assert not worker.is_alive()
        sessions = history_store.list_sessions()
        assert [session["text"] for session in sessions] == ["in-flight-tail"]
    finally:
        asr.release.set()
        app.shutdown()


def test_partial_transcription_persists_each_cycle_at_most_once(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, history_store=history_store)
    token = app._cycle_token
    app._running = True

    app._persist_transcript_session(token, source={"kind": "microphone"}, text="first")
    app._persist_transcript_session(token, source={"kind": "microphone"}, text="duplicate")

    assert [session["text"] for session in history_store.list_sessions()] == ["first"]


def test_shutdown_invalidates_cycle_waits_and_blocks_late_injection_or_history(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            inject_text=True,
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )
    history_store = TranscriptHistoryStore(tmp_path / "history.json")
    recorder = ManualRecorder()
    injector = FakeInjector()

    class BlockingASR:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def transcribe(self, _audio: object, **_options: object) -> str:
            self.started.set()
            self.release.wait(timeout=2)
            return "late transcript"

    asr = BlockingASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        history_store=history_store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=asr,
        injector=injector,
    )
    app.start()
    assert recorder.started.wait(timeout=2)
    app.stop()
    assert asr.started.wait(timeout=2)

    release_thread = threading.Thread(target=lambda: (time.sleep(0.05), asr.release.set()))
    release_thread.start()
    app.shutdown()
    release_thread.join(timeout=1)

    assert app._worker is not None and not app._worker.is_alive()
    assert injector.injected == []
    assert history_store.list_sessions() == []


def test_runtime_settings_changes_are_deferred_until_recording_finishes(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    initial = AppSettings(
        asr_backend="dummy",
        enable_hotkey=False,
        show_first_run_hint=False,
        streaming_transcription_enabled=False,
    )
    store.save(initial)
    recorder = ManualRecorder()
    original_asr = StaticASR()
    replacement_asr = StaticASR()
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        recorder=recorder,  # type: ignore[arg-type]
        asr_engine=original_asr,
    )
    monkeypatch.setattr("voice_input.app.build_asr_engine", lambda _settings: replacement_asr)

    app.start()
    assert recorder.started.wait(timeout=2)
    app.update_settings(
        AppSettings(
            asr_backend="dummy",
            funasr_model_root="next-cycle-model-root",
            enable_hotkey=False,
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )

    assert app.asr_engine is replacement_asr
    assert app._asr_engine_for_cycle(app._cycle_token) is original_asr
    assert app._settings_for_cycle(app._cycle_token).funasr_model_root == ""
    app.stop()


def test_idle_runtime_settings_change_closes_replaced_asr_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseableASR(StaticASR):
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            asr_backend="dummy",
            enable_hotkey=False,
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )
    original = CloseableASR()
    replacement = CloseableASR()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=original)
    monkeypatch.setattr("voice_input.app.build_asr_engine", lambda _settings: replacement)

    app.update_settings(
        AppSettings(
            asr_backend="dummy",
            funasr_model_root="next-model-root",
            enable_hotkey=False,
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )

    assert app.asr_engine is replacement
    assert original.closed == 1


class FileASR:
    def __init__(self) -> None:
        self.audio_inputs: list[object] = []
        self.options: list[dict[str, object]] = []

    def transcribe(self, audio: object, **options: object) -> str:
        self.audio_inputs.append(audio)
        self.options.append(dict(options))
        return "file transcript"


def test_audio_file_recognition_can_be_cancelled_after_it_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellableASR:
        def __init__(self) -> None:
            self.started = threading.Event()

        def transcribe(self, _audio: object, **options: object) -> str:
            cancel_event = options["cancel_event"]
            self.started.set()
            assert cancel_event.wait(timeout=2)  # type: ignore[union-attr]
            raise ASREngineCancelled("cancelled")

    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"audio")
    asr = CancellableASR()
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: 10.0)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=asr)

    assert app.transcribe_audio_file(audio_path) is True
    assert asr.started.wait(timeout=2)
    app.stop()

    deadline = time.time() + 2
    while time.time() < deadline and (not app.ui.running or app.ui.running[-1] is not False):
        app._poll_messages()
        time.sleep(0.01)

    assert "识别已取消" in app.ui.status
    assert app.ui.errors == []


def test_transcribe_audio_file_runs_recognition_cycle(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"audio")
    asr = FileASR()
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: 180.0)
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        asr_engine=asr,
    )

    assert app.transcribe_audio_file(audio_path) is True
    deadline = time.time() + 2
    while time.time() < deadline and (
        not app.ui.text or not app.ui.running or app.ui.running[-1] is not False
    ):
        app._poll_messages()
        time.sleep(0.01)

    assert asr.audio_inputs == [audio_path]
    assert asr.options[0]["timeout_seconds"] == 180 * 4 + 60
    assert app.ui.text == ["file transcript"]
    assert app.ui.running[-1] is False


def test_audio_import_releases_retained_streaming_workers(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"audio")
    asr = FileASR()
    prepared = CloseableStreamingRecognizer()
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: 10.0)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=asr)
    app._prepared_streaming_recognizer = prepared

    assert app.transcribe_audio_file(audio_path) is True

    assert prepared.closed is True
    assert app._prepared_streaming_recognizer is None


def test_transcribe_audio_file_transcodes_common_container_format(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"audio")
    converted_path = tmp_path / "converted.wav"
    converted_path.write_bytes(b"wav")
    asr = FileASR()
    transcode_calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: 240.0)

    def fake_transcode(source: Path, target: Path, **options: object) -> Path:
        transcode_calls.append((source, target))
        assert options["timeout_seconds"] == pytest.approx(180.0)
        assert options["cancel_event"] is app._recognition_cancel_event
        target.write_bytes(b"wav")
        return target

    monkeypatch.setattr("voice_input.app.transcode_audio_to_wav", fake_transcode)
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        asr_engine=asr,
    )

    assert app.transcribe_audio_file(audio_path) is True
    deadline = time.time() + 2
    while time.time() < deadline and (
        not app.ui.text or not app.ui.running or app.ui.running[-1] is not False
    ):
        app._poll_messages()
        time.sleep(0.01)

    assert transcode_calls
    assert transcode_calls[0][0] == audio_path
    assert Path(asr.audio_inputs[0]).suffix == ".wav"
    assert asr.options[0]["timeout_seconds"] == 240 * 4 + 60
    assert app.ui.text == ["file transcript"]


def test_transcribe_audio_file_reports_missing_transcoder(tmp_path: Path, monkeypatch) -> None:
    from voice_input.media import AudioTranscodeUnavailable

    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "meeting.wma"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: None)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=FileASR())

    def fake_transcode(_source: Path, _target: Path, **_options: object) -> Path:
        raise AudioTranscodeUnavailable("需要 FFmpeg")

    monkeypatch.setattr("voice_input.app.transcode_audio_to_wav", fake_transcode)

    assert app.transcribe_audio_file(audio_path) is True
    deadline = time.time() + 2
    while time.time() < deadline and not app.ui.errors:
        app._poll_messages()
        time.sleep(0.01)

    assert app.ui.errors
    assert "FFmpeg" in app.ui.errors[-1][1]
    assert app.ui.running[-1] is False


def test_transcribe_audio_file_rejects_files_over_two_hours(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "meeting.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", lambda _path: 2 * 60 * 60 + 1)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=FileASR())

    assert app.transcribe_audio_file(audio_path) is False
    assert app.ui.errors
    assert "最长支持 2 小时" in app.ui.errors[-1][1]
    assert app.is_running is False


def test_transcribe_audio_file_rejects_missing_file(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())

    assert app.transcribe_audio_file(tmp_path / "missing.wav") is False
    assert app.ui.errors


def test_transcribe_audio_file_rejects_unsupported_format(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    audio_path = tmp_path / "meeting.xyz"
    audio_path.write_bytes(b"audio")
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())

    assert app.transcribe_audio_file(audio_path) is False
    assert app.ui.errors
    assert app.is_running is False


def test_transcribe_audio_file_explains_stream_only_package_without_offline_model(
    tmp_path: Path, monkeypatch
) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=True))
    audio_path = tmp_path / "meeting.wav"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_streaming_model", lambda: True)
    monkeypatch.setattr("voice_input.app.has_bundled_funasr_review_models", lambda: False)
    monkeypatch.setattr("voice_input.app.has_bundled_whisper", lambda: False)
    monkeypatch.setattr("voice_input.app.packaged_model_profile", lambda: "streaming")
    monkeypatch.setattr("voice_input.app.probe_audio_duration_seconds", Mock())
    app = VoiceInputApp(
        ui_factory=FakeUI,
        settings_store=store,
        asr_engine=FunASROfflineEngine(model=""),
    )

    assert app.transcribe_audio_file(audio_path) is False

    assert app.ui.errors
    assert app.ui.errors[-1][0] == "导入音频不可用"
    assert "轻量实时包" in app.ui.errors[-1][1]
    assert "完整离线包" in app.ui.errors[-1][1]
    assert app.is_running is False
    voice_input_probe = app_module.probe_audio_duration_seconds
    assert isinstance(voice_input_probe, Mock)
    voice_input_probe.assert_not_called()


def test_transcription_timeout_is_dynamic_for_recorded_audio(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    asr = FileASR()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=asr)

    class AudioWithDuration:
        duration_seconds = 3600

    assert app._transcribe(AudioWithDuration()) == "file transcript"

    assert asr.options[0]["timeout_seconds"] == 3600 * 4 + 60


def test_update_settings_rebuilds_injector(tmp_path: Path, monkeypatch) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(inject_text=False, show_first_run_hint=False, streaming_transcription_enabled=False))
    injector = FakeInjector()
    monkeypatch.setattr("voice_input.injector.create_injector", lambda: injector)
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.update_settings(AppSettings(inject_text=True, show_first_run_hint=False))

    assert app.injector is injector


def test_update_settings_does_not_rebuild_asr_for_hotkey_or_lexicon_changes(tmp_path: Path, monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            asr_backend="dummy",
            enable_hotkey=False,
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )
    asr = StaticASR()
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=asr)
    monkeypatch.setattr("voice_input.app.build_asr_engine", Mock(side_effect=AssertionError("ASR should not rebuild")))

    app.update_settings(
        AppSettings(
            asr_backend="dummy",
            hotkey="Ctrl+Shift+F8",
            enable_hotkey=False,
            lexicon_enabled=True,
            enabled_builtin_lexicons=("government_digital",),
            show_first_run_hint=False,
            streaming_transcription_enabled=False,
        )
    )

    assert app.asr_engine is asr
    assert not app.ui.errors


def test_update_settings_does_not_restart_streaming_worker_for_lexicon_changes(tmp_path: Path, monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(
        AppSettings(
            asr_backend="dummy",
            enable_hotkey=False,
            show_first_run_hint=False,
            streaming_transcription_enabled=True,
        )
    )
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR(), recorder=SnapshotRecorder())

    class Prepared:
        language = "zh"
        review_interval_seconds = 0.0
        closed = False

        def close(self) -> None:
            self.closed = True

    prepared = Prepared()
    app._prepared_streaming_recognizer = prepared
    monkeypatch.setattr(app, "_prewarm_streaming_recognizer", Mock(side_effect=AssertionError("streaming should not prewarm")))

    app.update_settings(
        AppSettings(
            asr_backend="dummy",
            enable_hotkey=False,
            lexicon_enabled=True,
            enabled_builtin_lexicons=("government_digital",),
            show_first_run_hint=False,
            streaming_transcription_enabled=True,
        )
    )

    assert prepared.closed is False
    assert app._prepared_streaming_recognizer is prepared


def test_update_settings_rejects_missing_whisper_model(tmp_path: Path, monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    original_engine = app.asr_engine

    app.update_settings(
        AppSettings(
            asr_backend="whisper.cpp",
            whisper_model_path=str(tmp_path / "missing.bin"),
            whisper_executable="whisper-cli",
            show_first_run_hint=False,
        )
    )

    assert app.ui.errors
    assert app.asr_engine is original_engine


def test_update_settings_accepts_whisper_model_and_path_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model = tmp_path / "model.bin"
    executable = tmp_path / "whisper-cli.exe"
    model.write_bytes(b"model")
    executable.write_bytes(b"exe")
    _patch_no_bundled_asr(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)

    app.update_settings(
        AppSettings(
            asr_backend="whisper.cpp",
            whisper_model_path=str(model),
            whisper_executable=str(executable),
            enable_hotkey=False,
            show_first_run_hint=False,
        )
    )

    assert not app.ui.errors
    assert isinstance(app.asr_engine, WhisperCppEngine)


def test_update_settings_accepts_builtin_lexicons_without_custom_file(tmp_path: Path, monkeypatch) -> None:
    _patch_no_bundled_asr(monkeypatch)
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store, asr_engine=StaticASR())

    app.update_settings(
        AppSettings(
            asr_backend="dummy",
            enable_hotkey=False,
            lexicon_enabled=True,
            lexicon_path="",
            show_first_run_hint=False,
        )
    )

    assert not app.ui.errors
    assert app.settings.lexicon_enabled is True
    assert app.settings.lexicon_path == ""


def test_stale_cycle_messages_are_ignored(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    stale_token = app._cycle_token
    app.stop()

    app._messages.put(("text", stale_token, "过期文本"))
    app._messages.put(("running", stale_token, False))
    app._poll_messages()

    assert app.ui.text == []
    assert app.ui.running[-1] is False


def test_current_cycle_messages_are_processed(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.save(AppSettings(show_first_run_hint=False, streaming_transcription_enabled=False))
    app = VoiceInputApp(ui_factory=FakeUI, settings_store=store)
    app._running = True
    token = app._cycle_token

    app._messages.put(("text", token, "新文本"))
    app._messages.put(("running", token, False))
    app._poll_messages()

    assert app.ui.text == ["新文本"]
    assert app.ui.running[-1] is False


def test_transcript_normalization_converts_default_chinese_to_simplified() -> None:
    assert normalize_transcript_text("財務預算科目\n報表 識別結果", "zh-Hans") == "财务预算科目\n报表 识别结果"


def test_normalize_recognition_text_applies_enabled_lexicon(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "lexicon.txt"
    lexicon_path.write_text("政屋=>政务\n", encoding="utf-8")

    text = normalize_recognition_text(
        "政屋服務",
        AppSettings(
            recognition_language="zh-Hans",
            lexicon_enabled=True,
            lexicon_path=str(lexicon_path),
            product_tier="standard",
        ),
    )

    assert text == "政务服务。"


def test_normalize_streaming_text_applies_lexicon_before_final_commit(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "lexicon.txt"
    lexicon_path.write_text("政屋=>政务\n", encoding="utf-8")

    text = normalize_streaming_text(
        "政屋服务",
        AppSettings(
            recognition_language="zh-Hans",
            lexicon_enabled=True,
            lexicon_path=str(lexicon_path),
            enabled_builtin_lexicons=(),
            product_tier="standard",
        ),
        close_sentence=False,
    )

    assert text == "政务服务"


def test_normalize_recognition_text_applies_builtin_lexicons() -> None:
    text = normalize_recognition_text(
        "政屋服務和会义纪要",
        AppSettings(recognition_language="zh-Hans", lexicon_enabled=True, product_tier="standard"),
    )

    assert text == "政务服务和会议纪要。"


def test_custom_lexicon_overrides_builtin_rule(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "lexicon.txt"
    lexicon_path.write_text("政屋=>政务大厅\n", encoding="utf-8")

    text = normalize_recognition_text(
        "政屋服務",
        AppSettings(
            recognition_language="zh-Hans",
            lexicon_enabled=True,
            lexicon_path=str(lexicon_path),
            product_tier="standard",
        ),
    )

    assert text == "政务大厅服务。"


def test_builtin_lexicon_selection_limits_applied_rules() -> None:
    text = normalize_recognition_text(
        "政屋服務和会义纪要",
        AppSettings(
            recognition_language="zh-Hans",
            lexicon_enabled=True,
            enabled_builtin_lexicons=("government_digital",),
            product_tier="standard",
        ),
    )

    assert text == "政务服务和会义纪要。"


def test_builtin_lexicons_are_available_in_all_product_tiers() -> None:
    basic = normalize_recognition_text(
        "发文自号和会义纪要",
        AppSettings(recognition_language="zh-Hans", lexicon_enabled=True, product_tier="basic"),
    )
    standard = normalize_recognition_text(
        "发文自号和会义纪要",
        AppSettings(recognition_language="zh-Hans", lexicon_enabled=True, product_tier="standard"),
    )
    enterprise = normalize_recognition_text(
        "发文自号和会义纪要",
        AppSettings(recognition_language="zh-Hans", lexicon_enabled=True, product_tier="enterprise"),
    )

    assert basic == "发文字号和会议纪要。"
    assert standard == "发文字号和会议纪要。"
    assert enterprise == "发文字号和会议纪要。"


def test_basic_tier_enables_meeting_lexicon() -> None:
    text = normalize_recognition_text(
        "会义纪要和参会人元",
        AppSettings(recognition_language="zh-Hans", lexicon_enabled=True, product_tier="basic"),
    )

    assert text == "会议纪要和参会人员。"
