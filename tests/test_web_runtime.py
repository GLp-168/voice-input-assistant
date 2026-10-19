from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from voice_input.settings import AppSettings
from voice_input.web_runtime import (
    FlipGlassVoiceInputWindow,
    _bounded_window_sizes,
    validate_web_runtime,
    web_runtime_index,
)


class FakeApp:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings(start_minimized=True)
        self.started = 0
        self.stopped = 0
        self.requested_close = 0
        self.imported = None
        self.paused = False

    @property
    def is_running(self) -> bool:
        return self.started > self.stopped

    @property
    def is_recording_paused(self) -> bool:
        return self.paused

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def toggle(self) -> None:
        if self.is_running:
            self.stop()
        else:
            self.start()

    def toggle_recording_pause(self) -> bool:
        self.paused = not self.paused
        return True

    def update_settings(self, settings: AppSettings) -> None:
        self.settings = settings

    def transcribe_audio_file(self, path) -> bool:
        self.imported = path
        return True

    def request_close(self) -> None:
        self.requested_close += 1

    def show_window(self) -> None:
        return None

    def exit_app(self) -> None:
        return None


def close_window(window: FlipGlassVoiceInputWindow) -> None:
    window._closing = True
    window.close()
    window.qt_app.processEvents()
    window.deleteLater()
    window.qt_app.processEvents()


def test_web_runtime_assets_are_built() -> None:
    index = web_runtime_index()
    assert index.is_file()
    html = index.read_text(encoding="utf-8")
    assert "VOICE INPUT" in html
    assert "qrc:///qtwebchannel/qwebchannel.js" in html


def test_web_runtime_integrity_rejects_tampered_assets(tmp_path: Path) -> None:
    dist = tmp_path / "web_dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    files = {
        "index.html": b"<script src='assets/app.js'></script><link rel='stylesheet' href='assets/app.css'>",
        "assets/app.js": b"window.test = true;",
        "assets/app.css": b"body { color: black; }",
    }
    for relative, content in files.items():
        path = dist / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "schema_version": 1,
        "algorithm": "sha256",
        "files": [
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for relative, content in files.items()
        ],
    }
    (dist / "integrity.json").write_text(json.dumps(manifest), encoding="utf-8")

    validate_web_runtime(dist / "index.html")
    (assets / "app.js").write_text("window.test = false;", encoding="utf-8")

    with pytest.raises(RuntimeError, match="integrity (size|SHA-256) mismatch"):
        validate_web_runtime(dist / "index.html")


def test_web_runtime_ready_watchdog_falls_back_when_handshake_never_arrives(monkeypatch) -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        activated: list[Path] = []
        window._web_ready = False
        window._fallback_started = False
        window._closing = False
        window._web_ready_watchdog.stop()
        monkeypatch.setattr(window, "_activate_legacy_fallback", activated.append)

        window._handle_load_finished(True)

        assert window._web_ready_watchdog.isActive()
        window._handle_web_ready_timeout()
        assert activated == [web_runtime_index()]
    finally:
        window._web_ready_watchdog.stop()
        close_window(window)


@pytest.mark.parametrize("failure_mode", ("load_failed", "handshake_timeout"))
def test_web_runtime_failure_switches_to_legacy_with_current_state_and_is_idempotent(
    monkeypatch,
    failure_mode: str,
) -> None:
    import voice_input.qt_ui as qt_ui_module

    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    app.ui = window
    created: list[object] = []

    class FakeLegacyWindow:
        def __init__(self, owner: FakeApp) -> None:
            assert owner is app
            created.append(self)
            self.transcripts: list[str] = []
            self.running: list[bool] = []
            self.recording: list[bool] = []
            self.statuses: list[str] = []

        def append_text(self, text: str) -> None:
            self.transcripts.append(text)

        def set_running(self, running: bool) -> None:
            self.running.append(running)

        def set_recording(self, recording: bool) -> None:
            self.recording.append(recording)

        def set_status(self, text: str) -> None:
            self.statuses.append(text)

    monkeypatch.setattr(qt_ui_module, "QtVoiceInputWindow", FakeLegacyWindow)
    try:
        window._transcript_text = "已确认段落\n\n正在识别的段落"
        window._last_status_text = "正在录音并实时识别"
        window._running = True
        window._recording = True
        window._fallback_started = False
        window._closing = False
        window._web_ready = False
        window._web_ready_watchdog.stop()

        if failure_mode == "load_failed":
            def trigger_failure() -> None:
                window._handle_load_finished(False)
        else:
            trigger_failure = window._handle_web_ready_timeout

        trigger_failure()

        assert len(created) == 1
        fallback = created[0]
        assert app.ui is fallback
        assert fallback.transcripts == ["已确认段落\n\n正在识别的段落"]
        assert fallback.statuses == ["正在录音并实时识别"]
        assert fallback.running == [True]
        assert fallback.recording == [True]
        assert window._fallback_started is True
        assert window._closing is True
        assert not window._web_ready_watchdog.isActive()

        trigger_failure()

        assert created == [fallback]
        assert fallback.transcripts == ["已确认段落\n\n正在识别的段落"]
        assert fallback.statuses == ["正在录音并实时识别"]
        assert fallback.running == [True]
        assert fallback.recording == [True]
    finally:
        window._web_ready_watchdog.stop()
        if not window._closing:
            close_window(window)
        else:
            window.deleteLater()
            window.qt_app.processEvents()


def test_web_runtime_ready_handshake_cancels_watchdog() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = False
        window._fallback_started = False
        window._closing = False
        window._web_ready_watchdog.stop()
        window._handle_load_finished(True)
        assert window._web_ready_watchdog.isActive()

        window._handle_web_ready(incremental=True)

        assert window._web_ready is True
        assert not window._web_ready_watchdog.isActive()
    finally:
        close_window(window)


def test_web_runtime_state_and_streaming_text() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        payload = window._state_payload()
        assert payload["connected"] is True
        assert payload["status"]["phase"] == "ready"
        assert payload["options"]["lexicons"]

        window.append_text("第一段")
        window.begin_streaming_text()
        window.set_streaming_text("实时内容")
        assert window._state_payload()["transcript"]["text"] == "第一段\n\n实时内容"
        assert window._state_payload()["transcript"]["streaming"] is True

        window.commit_streaming_text("最终内容")
        transcript = window._state_payload()["transcript"]
        assert transcript["text"] == "第一段\n\n最终内容"
        assert transcript["streaming"] is False
    finally:
        close_window(window)


def test_paused_status_detail_reflects_suspended_microphone() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        app.started = 1
        window.set_running(True)
        window.set_recording(True)
        window.set_status("正在实时转写")

        window._toggle_pause()

        status = window._status_payload()
        assert app.paused is True
        assert status["phase"] == "paused"
        assert status["detail"] == "录音已暂停，继续后恢复麦克风输入和实时转写"
        assert "输入正常" not in str(status["detail"])
    finally:
        close_window(window)


def test_finalization_progress_is_additive_status_protocol() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window.set_running(True)
        window.set_recording(False)
        window.set_finalization_progress(2, 5)

        status = window._status_payload()
        assert status["phase"] == "transcribing"
        assert status["finalization"] == {
            "active": True,
            "completed_reviews": 2,
            "total_reviews": 5,
        }
        assert status["detail"] == "正在完成本地离线复核（2/5）"

        window.set_running(False)
        assert window._status_payload()["finalization"] == {
            "active": False,
            "completed_reviews": 0,
            "total_reviews": 0,
        }
    finally:
        close_window(window)


def test_window_sizes_fit_high_dpi_logical_work_area() -> None:
    assert _bounded_window_sizes(960, 540) == (760, 492, 912, 492)
    assert _bounded_window_sizes(800, 480) == (752, 432, 752, 432)
    assert _bounded_window_sizes(1920, 1080) == (760, 520, 1280, 780)


def test_web_ready_sends_one_complete_bootstrap_state() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._transcript_text = "会前准备记录"
        states: list[str] = []
        statuses: list[str] = []
        clocks: list[int] = []
        transcripts: list[str] = []
        settings: list[str] = []
        window.bridge.stateChanged.connect(states.append)
        window.bridge.statusChanged.connect(statuses.append)
        window.bridge.clockChanged.connect(clocks.append)
        window.bridge.transcriptChanged.connect(transcripts.append)
        window.bridge.settingsChanged.connect(settings.append)

        window._handle_web_ready(incremental=True)

        assert len(states) == 1
        payload = json.loads(states[0])
        assert set(payload) == {
            "connected", "status", "transcript", "settings", "options", "audio", "history", "capabilities"
        }
        assert payload["transcript"]["text"] == "会前准备记录"
        assert statuses == []
        assert clocks == []
        assert transcripts == []
        assert settings == []
    finally:
        close_window(window)


def test_elapsed_timer_emits_clock_without_serializing_state(monkeypatch) -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        window._transcript_text = "长" * 250_000
        states: list[str] = []
        statuses: list[str] = []
        clocks: list[int] = []
        transcripts: list[str] = []
        settings: list[str] = []
        window.bridge.stateChanged.connect(states.append)
        window.bridge.statusChanged.connect(statuses.append)
        window.bridge.clockChanged.connect(clocks.append)
        window.bridge.transcriptChanged.connect(transcripts.append)
        window.bridge.settingsChanged.connect(settings.append)
        monkeypatch.setattr(window, "_elapsed_seconds", lambda: 91)
        monkeypatch.setattr(
            window,
            "_state_payload",
            lambda: (_ for _ in ()).throw(AssertionError("clock update serialized the full state")),
        )

        window._elapsed_timer.timeout.emit()

        assert clocks == [91]
        assert states == []
        assert statuses == []
        assert transcripts == []
        assert settings == []
    finally:
        close_window(window)


def test_long_transcript_is_not_repeated_by_clock_updates(monkeypatch) -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        states: list[str] = []
        statuses: list[str] = []
        clocks: list[int] = []
        transcripts: list[str] = []
        settings: list[str] = []
        window.bridge.stateChanged.connect(states.append)
        window.bridge.statusChanged.connect(statuses.append)
        window.bridge.clockChanged.connect(clocks.append)
        window.bridge.transcriptChanged.connect(transcripts.append)
        window.bridge.settingsChanged.connect(settings.append)
        long_text = "会议记录。" * 50_000

        window.append_text(long_text)

        assert len(transcripts) == 1
        transcript = json.loads(transcripts[0])
        assert set(transcript) == {"text", "editable", "streaming", "session_id", "segments", "revision"}
        assert transcript["text"] == long_text
        assert transcript["streaming"] is False
        monkeypatch.setattr(window, "_elapsed_seconds", lambda: 123)
        for _ in range(20):
            window._elapsed_timer.timeout.emit()

        assert clocks == [123] * 20
        assert len(transcripts) == 1
        assert states == []
        assert statuses == []
        assert settings == []
    finally:
        close_window(window)


def test_streaming_commit_emits_one_final_transcript_update() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        window._transcript_text = "已有记录" * 10_000
        deltas: list[str] = []
        window.bridge.transcriptDeltaChanged.connect(deltas.append)
        window.begin_streaming_text()

        window.commit_streaming_text("最终内容")

        assert [json.loads(payload)["op"] for payload in deltas] == ["begin", "commit"]
        assert json.loads(deltas[-1])["text"] == "最终内容"
        transcript = window._state_payload()["transcript"]
        assert transcript["text"] == f"{'已有记录' * 10_000}\n\n最终内容"
        assert transcript["streaming"] is False
    finally:
        close_window(window)


def test_streaming_updates_emit_suffix_patches_instead_of_full_text() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        window._transcript_text = "existing " * 20_000
        deltas: list[str] = []
        window.bridge.transcriptDeltaChanged.connect(deltas.append)

        window.begin_streaming_text()
        window.set_streaming_text("hello")
        window.set_streaming_text("hello world")

        patches = [json.loads(payload) for payload in deltas]
        assert [patch["op"] for patch in patches] == ["begin", "patch", "patch"]
        assert patches[-1]["start"] == 5
        assert patches[-1]["text"] == " world"
        assert "existing " not in deltas[-1]
        assert window._transcript_text.endswith("hello world")
    finally:
        close_window(window)


def test_stale_editor_revision_cannot_overwrite_newer_transcript() -> None:
    app = FakeApp(AppSettings(start_minimized=True, transcript_editable=True))
    window = FlipGlassVoiceInputWindow(app)
    try:
        window.append_text("original")
        base_revision = window._transcript_revision
        window._set_transcript_revision_from_web(
            json.dumps({"base_revision": base_revision, "revision": base_revision + 1, "text": "edited"})
        )
        assert window._transcript_text == "edited"

        stale_revision = window._transcript_revision
        window._clear_text()
        window._set_transcript_revision_from_web(
            json.dumps({"base_revision": stale_revision, "revision": stale_revision + 1, "text": "stale"})
        )
        assert window._transcript_text == ""
    finally:
        close_window(window)


def test_legacy_frontend_receives_small_state_patches(monkeypatch) -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        states: list[str] = []
        statuses: list[str] = []
        clocks: list[int] = []
        transcripts: list[str] = []
        settings: list[str] = []
        window.bridge.stateChanged.connect(states.append)
        window.bridge.statusChanged.connect(statuses.append)
        window.bridge.clockChanged.connect(clocks.append)
        window.bridge.transcriptChanged.connect(transcripts.append)
        window.bridge.settingsChanged.connect(settings.append)
        long_text = "兼容记录。" * 20_000

        window.append_text(long_text)
        monkeypatch.setattr(window, "_elapsed_seconds", lambda: 45)
        window._elapsed_timer.timeout.emit()

        assert json.loads(states[0]) == {
            "transcript": {
                "text": long_text,
                "editable": False,
                "streaming": False,
                    "session_id": "",
                    "segments": [],
                    "revision": 1,
                }
        }
        assert json.loads(states[1]) == {"status": {"elapsed_seconds": 45}}
        assert statuses == []
        assert clocks == []
        assert transcripts == []
        assert settings == []
    finally:
        close_window(window)


def test_web_bridge_dispatches_runtime_commands() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window.bridge.startRecording()
        window.bridge.stopRecording()
        window.bridge.closeWindow()
        assert app.started == 1
        assert app.stopped == 1
        assert app.requested_close == 1
    finally:
        close_window(window)


def test_web_settings_payload_updates_app_settings() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._save_settings_payload(
            json.dumps(
                {
                    "recognition_language": "en",
                    "transcript_editable": True,
                    "streaming_transcription_enabled": False,
                    "enabled_builtin_lexicons": ["office_meeting"],
                }
            )
        )
        assert app.settings.recognition_language == "en"
        assert app.settings.transcript_editable is True
        assert app.settings.streaming_transcription_enabled is False
        assert app.settings.enabled_builtin_lexicons == ("office_meeting",)
    finally:
        close_window(window)


def test_web_editor_rejects_updates_when_editing_is_disabled() -> None:
    app = FakeApp(AppSettings(start_minimized=True, transcript_editable=False))
    window = FlipGlassVoiceInputWindow(app)
    try:
        window.append_text("原始文本")
        window._set_transcript_from_web("被修改")
        assert window._transcript_text == "原始文本"
    finally:
        close_window(window)


def test_web_editor_updates_do_not_echo_the_full_transcript() -> None:
    app = FakeApp(AppSettings(start_minimized=True, transcript_editable=True))
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        transcripts: list[str] = []
        window.bridge.transcriptChanged.connect(transcripts.append)

        window._set_transcript_from_web("用户编辑" * 50_000)

        assert window._transcript_text == "用户编辑" * 50_000
        assert transcripts == []
    finally:
        close_window(window)


def test_audio_monitor_updates_use_small_incremental_signals() -> None:
    app = FakeApp()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        states: list[str] = []
        levels: list[float] = []
        microphones: list[str] = []
        window.bridge.stateChanged.connect(states.append)
        window.bridge.audioLevelChanged.connect(levels.append)
        window.bridge.microphoneStateChanged.connect(microphones.append)

        window.set_audio_level(1.4)
        window.set_microphone_state(
            {"status": "active", "device": 2, "name": "会议麦克风", "message": "输入正常"}
        )

        assert levels == [1.0]
        assert json.loads(microphones[0]) == {
            "status": "active",
            "device": 2,
            "name": "会议麦克风",
            "message": "输入正常",
        }
        assert states == []
    finally:
        close_window(window)


def test_history_updates_and_loads_structured_session() -> None:
    app = FakeApp()
    detail = {
        "id": "session-1",
        "created_at": "2026-07-13T10:00:00Z",
        "source": {"kind": "microphone"},
        "text": "第一段\n\n第二段",
        "duration": 12.0,
        "paragraphs": ["第一段", "第二段"],
        "segments": [
            {"start": 0.0, "end": 5.0, "text": "第一段", "speaker": 0},
            {"start": 5.0, "end": 12.0, "text": "第二段", "speaker": "S2"},
        ],
    }

    class FakeHistoryStore:
        def get_session(self, session_id: str):
            return detail if session_id == "session-1" else None

    app.history_store = FakeHistoryStore()
    window = FlipGlassVoiceInputWindow(app)
    try:
        window._web_ready = True
        window._incremental_updates = True
        history_events: list[str] = []
        detail_events: list[str] = []
        transcript_events: list[str] = []
        window.bridge.historyChanged.connect(history_events.append)
        window.bridge.historySessionChanged.connect(detail_events.append)
        window.bridge.transcriptChanged.connect(transcript_events.append)
        sessions = [
            {
                "id": "session-1",
                "created_at": "2026-07-13T10:00:00Z",
                "source": {"kind": "microphone"},
                "duration": 12.0,
                "text_preview": "第一段 第二段",
                "paragraph_count": 2,
                "segment_count": 2,
            }
        ]

        window.set_session_history(sessions)
        window.bridge.requestSession("session-1")
        window.bridge.loadSession("session-1")

        assert json.loads(history_events[0]) == sessions
        assert json.loads(detail_events[0])["segments"][0]["speaker"] == 0
        transcript = json.loads(transcript_events[0])
        assert transcript["text"] == "第一段\n\n第二段"
        assert transcript["session_id"] == "session-1"
        assert transcript["segments"][1]["speaker"] == "S2"
    finally:
        close_window(window)
