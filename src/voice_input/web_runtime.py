"""Qt WebEngine runtime shell powered by the bundled UI-Flip-glass web UI."""

from __future__ import annotations

import hashlib
from html import escape as escape_html
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QMouseEvent
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMenu, QMessageBox, QStyle, QSystemTrayIcon
from PySide6.QtGui import QTextDocument

from .config import DEFAULT_MAX_RECORDING_SECONDS, SUPPORTED_AUDIO_FILE_EXTENSIONS
from .exporters import export_meeting_minutes_docx, export_official_document_docx, export_transcript_docx
from .language import LANGUAGE_OPTIONS
from .resources import (
    bundled_app_icon,
    bundled_lexicon_catalog,
    bundled_product_profile,
    packaged_model_profile,
)
from .settings import AppSettings, DEFAULT_HOTKEY

if TYPE_CHECKING:
    from .app import VoiceInputApp


LOGGER = logging.getLogger(__name__)
WEB_INTEGRITY_MANIFEST = "integrity.json"
WEB_INTEGRITY_SCHEMA_VERSION = 1
DEFAULT_WINDOW_SIZE = (1280, 780)
DEFAULT_MINIMUM_WINDOW_SIZE = (760, 520)
WINDOW_SCREEN_MARGIN = 48


def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _bounded_window_sizes(available_width: int, available_height: int) -> tuple[int, int, int, int]:
    """Keep the initial and minimum sizes within the current logical-pixel work area."""

    usable_width = max(320, int(available_width) - WINDOW_SCREEN_MARGIN)
    usable_height = max(320, int(available_height) - WINDOW_SCREEN_MARGIN)
    minimum_width = min(DEFAULT_MINIMUM_WINDOW_SIZE[0], usable_width)
    minimum_height = min(DEFAULT_MINIMUM_WINDOW_SIZE[1], usable_height)
    initial_width = min(DEFAULT_WINDOW_SIZE[0], usable_width)
    initial_height = min(DEFAULT_WINDOW_SIZE[1], usable_height)
    return minimum_width, minimum_height, initial_width, initial_height


def web_runtime_index() -> Path:
    """Locate the built web runtime in source, wheel, or PyInstaller layouts."""

    module_dir = Path(__file__).resolve().parent
    candidates = [module_dir / "web_dist" / "index.html"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        frozen_root = Path(meipass)
        candidates.extend(
            (
                frozen_root / "voice_input" / "web_dist" / "index.html",
                frozen_root / "web_dist" / "index.html",
            )
        )
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            (
                executable_dir / "web_dist" / "index.html",
                executable_dir / "_internal" / "voice_input" / "web_dist" / "index.html",
                executable_dir / "_internal" / "web_dist" / "index.html",
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def validate_web_runtime(index_path: Path) -> None:
    """Validate the bundled Web UI before allowing WebEngine to load it."""

    dist_dir = index_path.resolve().parent
    manifest_path = dist_dir / WEB_INTEGRITY_MANIFEST
    if not index_path.is_file() or index_path.stat().st_size <= 0:
        raise RuntimeError(f"Web UI entry point is missing or empty: {index_path}")
    if not manifest_path.is_file():
        raise RuntimeError(f"Web UI integrity manifest is missing: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Web UI integrity manifest is invalid: {manifest_path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != WEB_INTEGRITY_SCHEMA_VERSION
        or payload.get("algorithm") != "sha256"
        or not isinstance(payload.get("files"), list)
    ):
        raise RuntimeError(f"Web UI integrity manifest is invalid: {manifest_path}")

    expected: dict[str, tuple[int, str]] = {}
    for item in payload["files"]:
        if not isinstance(item, dict):
            raise RuntimeError(f"Web UI integrity manifest is invalid: {manifest_path}")
        relative = str(item.get("path") or "").replace("\\", "/")
        relative_path = Path(relative)
        size = item.get("size")
        sha256 = str(item.get("sha256") or "").lower()
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in expected
            or not isinstance(size, int)
            or size <= 0
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeError(f"Web UI integrity manifest is invalid: {manifest_path}")
        expected[relative] = (size, sha256)

    actual = {
        path.relative_to(dist_dir).as_posix(): path
        for path in dist_dir.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        detail = missing[0] if missing else extra[0]
        raise RuntimeError(f"Web UI integrity file list mismatch: {detail}")
    for relative, path in actual.items():
        expected_size, expected_sha256 = expected[relative]
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"Web UI integrity size mismatch: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"Web UI integrity SHA-256 mismatch: {relative}")


def _app() -> QApplication:
    instance = QApplication.instance()
    if instance is not None:
        return instance
    created = QApplication(sys.argv[:1])
    created.setApplicationName("语音转文字")
    created.setOrganizationName("VoiceInput")
    created.setStyle("Fusion")
    _register_cjk_font()
    created.setFont(_ui_font(10))
    return created


def _ui_font(size: int = 10) -> QFont:
    available = set(QFontDatabase.families())
    preferred = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans SC",
        "HarmonyOS Sans SC",
        "DengXian",
        "Arial",
    )
    family = next((name for name in preferred if not available or name in available), preferred[0])
    return QFont(family, size)


def _register_cjk_font() -> None:
    if sys.platform != "win32":
        return
    font_dir = Path("C:/Windows/Fonts")
    for filename in ("msyh.ttc", "msyh.ttf", "NotoSansSC-VF.ttf", "Deng.ttf"):
        path = font_dir / filename
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) >= 0:
            return


class _UiCallbackDispatcher(QObject):
    requested = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.requested.connect(self._dispatch)

    @Slot(int, object)
    def _dispatch(self, delay_ms: int, callback: Callable[[], None]) -> None:
        QTimer.singleShot(max(0, int(delay_ms)), callback)


class VoiceWebBridge(QObject):
    """Narrow WebChannel surface exposed to the trusted local runtime page."""

    stateChanged = Signal(str)
    statusChanged = Signal(str)
    clockChanged = Signal(int)
    transcriptChanged = Signal(str)
    transcriptDeltaChanged = Signal(str)
    settingsChanged = Signal(str)
    audioLevelChanged = Signal(float)
    microphoneStateChanged = Signal(str)
    historyChanged = Signal(str)
    historySessionChanged = Signal(str)
    notificationRequested = Signal(str)

    def __init__(self, window: "FlipGlassVoiceInputWindow") -> None:
        super().__init__(window)
        self.window = window

    @Slot()
    def ready(self) -> None:
        self.window._handle_web_ready(incremental=False)

    @Slot()
    def readyIncremental(self) -> None:
        self.window._handle_web_ready(incremental=True)

    @Slot()
    def startRecording(self) -> None:
        self.window.app.start()

    @Slot()
    def stopRecording(self) -> None:
        self.window.app.stop()

    @Slot()
    def togglePause(self) -> None:
        self.window._toggle_pause()

    @Slot(str)
    def setTranscript(self, text: str) -> None:
        self.window._set_transcript_from_web(text)

    @Slot(str)
    def setTranscriptRevision(self, payload: str) -> None:
        self.window._set_transcript_revision_from_web(payload)

    @Slot(str)
    def requestSession(self, session_id: str) -> None:
        self.window._request_history_session(session_id)

    @Slot(str)
    def loadSession(self, session_id: str) -> None:
        self.window._load_history_session(session_id)

    @Slot()
    def copyTranscript(self) -> None:
        self.window._copy_text()

    @Slot()
    def clearTranscript(self) -> None:
        self.window._clear_text()

    @Slot()
    def importAudio(self) -> None:
        self.window._import_audio()

    @Slot(str)
    def exportDocument(self, kind: str) -> None:
        self.window._export_document(kind)

    @Slot(str)
    def saveSettings(self, payload: str) -> None:
        self.window._save_settings_payload(payload)

    @Slot()
    def selectCustomLexicon(self) -> None:
        self.window._select_custom_lexicon()

    @Slot()
    def openHelp(self) -> None:
        callback = getattr(self.window.app, "open_help_file", None)
        if callable(callback):
            callback()

    @Slot()
    def openHealth(self) -> None:
        callback = getattr(self.window.app, "open_health_report", None)
        if callable(callback):
            callback()

    @Slot()
    def openLog(self) -> None:
        callback = getattr(self.window.app, "open_log_file", None)
        if callable(callback):
            callback()

    @Slot()
    def minimizeWindow(self) -> None:
        self.window.showMinimized()

    @Slot()
    def toggleMaximize(self) -> None:
        self.window._toggle_maximize()

    @Slot()
    def closeWindow(self) -> None:
        self.window.app.request_close()

    @Slot()
    def startWindowMove(self) -> None:
        handle = self.window.windowHandle()
        if handle is not None:
            handle.startSystemMove()


class _RuntimePage(QWebEnginePage):
    """Keep the embedded page local and open clicked web links externally."""

    def acceptNavigationRequest(
        self,
        url: QUrl,
        navigation_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if navigation_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            if url.scheme().lower() in {"http", "https"}:
                QDesktopServices.openUrl(url)
            return False
        if is_main_frame and url.scheme().lower() not in {"file", "qrc", "data", "about"}:
            return False
        return super().acceptNavigationRequest(url, navigation_type, is_main_frame)


class _RuntimeWebView(QWebEngineView):
    """Provide reliable native dragging for the frameless HTML title bar."""

    TITLEBAR_HEIGHT = 58
    WINDOW_ACTIONS_WIDTH = 174

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self.TITLEBAR_HEIGHT
            and event.position().x() < self.width() - self.WINDOW_ACTIONS_WIDTH
        ):
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self.TITLEBAR_HEIGHT
            and event.position().x() < self.width() - self.WINDOW_ACTIONS_WIDTH
        ):
            window = self.window()
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FlipGlassVoiceInputWindow(QMainWindow):
    """Frameless WebEngine host implementing the app's UI port."""

    WEB_READY_TIMEOUT_MS = 10_000

    def __init__(self, app: "VoiceInputApp") -> None:
        self.qt_app = _app()
        super().__init__()
        self.app = app
        index_path = web_runtime_index()
        validate_web_runtime(index_path)
        self._callback_dispatcher = _UiCallbackDispatcher(self)
        self._closing = False
        self._fallback_started = False
        self._web_ready = False
        self._incremental_updates = False
        self._load_succeeded = False
        self._pending_notifications: list[dict[str, str]] = []
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_toggle_action: QAction | None = None
        self._transcript_text = ""
        self._transcript_revision = 0
        self._transcript_segments: list[dict[str, object]] = []
        self._current_session_id = ""
        self._streaming_base_text: str | None = None
        self._streaming_preview_text = ""
        self._running = False
        self._recording = False
        self._paused = False
        self._partial_segment_count = 0
        self._finalization_progress = {
            "active": False,
            "completed_reviews": 0,
            "total_reviews": 0,
        }
        self._audio_level = max(0.0, min(1.0, float(getattr(app, "audio_level", 0.0))))
        microphone_state = getattr(app, "microphone_state", {})
        self._microphone_state = dict(microphone_state) if isinstance(microphone_state, dict) else {}
        history = getattr(app, "session_history", [])
        self._session_history = [dict(item) for item in history if isinstance(item, dict)]
        self._last_status_text = "正在初始化"
        self._last_status_detail = "正在连接本地识别服务"
        self._phase_override: str | None = None
        self._recording_started_at: float | None = None
        self._recording_elapsed_seconds = 0.0

        self.setWindowTitle("语音转文字")
        self._window_icon = self._load_window_icon()
        if not self._window_icon.isNull():
            self.setWindowIcon(self._window_icon)
        screen = self.screen() or self.qt_app.primaryScreen()
        if screen is None:
            minimum_width, minimum_height = DEFAULT_MINIMUM_WINDOW_SIZE
            initial_width, initial_height = DEFAULT_WINDOW_SIZE
        else:
            available = screen.availableGeometry()
            minimum_width, minimum_height, initial_width, initial_height = _bounded_window_sizes(
                available.width(),
                available.height(),
            )
        self.resize(initial_width, initial_height)
        self.setMinimumSize(minimum_width, minimum_height)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self._apply_topmost(app.settings.floating_window, show=False)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._emit_clock)

        self._web_ready_watchdog = QTimer(self)
        self._web_ready_watchdog.setSingleShot(True)
        self._web_ready_watchdog.setInterval(self.WEB_READY_TIMEOUT_MS)
        self._web_ready_watchdog.timeout.connect(self._handle_web_ready_timeout)

        self.web_view = _RuntimeWebView(self)
        self.web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        page = _RuntimePage(self.web_view)
        page.setBackgroundColor(QColor("#07090c"))
        self.web_view.setPage(page)
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)

        self.bridge = VoiceWebBridge(self)
        self.channel = QWebChannel(page)
        self.channel.registerObject("voiceBridge", self.bridge)
        page.setWebChannel(self.channel)
        self.web_view.loadFinished.connect(self._handle_load_finished)
        self.setCentralWidget(self.web_view)

        self.web_view.setUrl(QUrl.fromLocalFile(str(index_path)))

        if app.settings.start_minimized:
            self.hide()
        else:
            self.show()

    def mainloop(self) -> None:
        self.qt_app.exec()

    def after(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._callback_dispatcher.requested.emit(int(delay_ms), callback)

    def set_status(self, text: str) -> None:
        self._last_status_text = str(text)
        if any(token in self._last_status_text for token in ("失败", "错误", "异常")):
            self._phase_override = "error"
        elif not self._running:
            self._phase_override = None
        self._last_status_detail = self._status_detail(self._last_status_text)
        self._emit_status()

    def append_text(self, text: str) -> None:
        cleaned = str(text).strip()
        if not cleaned:
            return
        if self._transcript_text.strip():
            self._transcript_text = f"{self._transcript_text.rstrip()}\n\n{cleaned}"
        else:
            self._transcript_text = cleaned
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._transcript_segments = []
        self._current_session_id = ""
        self._advance_transcript_revision()
        self._emit_transcript()

    def begin_streaming_text(self) -> None:
        self._streaming_base_text = self._transcript_text.strip()
        self._streaming_preview_text = ""
        self._transcript_segments = []
        self._current_session_id = ""
        revision = self._advance_transcript_revision()
        self._emit_transcript_delta({"op": "begin", "revision": revision, "streaming": True})

    def set_streaming_text(self, text: str) -> None:
        cleaned = str(text).strip()
        if self._streaming_base_text is None:
            self.begin_streaming_text()
        previous_preview = self._streaming_preview_text
        base_revision = self._transcript_revision
        base = self._streaming_base_text or ""
        self._transcript_text = f"{base}\n\n{cleaned}" if base and cleaned else cleaned or base
        self._streaming_preview_text = cleaned
        revision = self._advance_transcript_revision()
        prefix_length = _common_prefix_length(previous_preview, cleaned)
        self._emit_transcript_delta(
            {
                "op": "patch",
                "base_revision": base_revision,
                "revision": revision,
                "start": prefix_length,
                "delete_count": len(previous_preview) - prefix_length,
                "text": cleaned[prefix_length:],
                "streaming": True,
                **({"base_text": base} if not previous_preview else {}),
            }
        )

    def commit_streaming_text(self, text: str) -> None:
        cleaned = str(text).strip()
        previous_preview = self._streaming_preview_text
        base_revision = self._transcript_revision
        base = self._streaming_base_text
        if base is None:
            base = self._transcript_text.strip()
        self._transcript_text = f"{base}\n\n{cleaned}" if base and cleaned else cleaned or base
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        revision = self._advance_transcript_revision()
        prefix_length = _common_prefix_length(previous_preview, cleaned)
        self._emit_transcript_delta(
            {
                "op": "commit",
                "base_revision": base_revision,
                "revision": revision,
                "start": prefix_length,
                "delete_count": len(previous_preview) - prefix_length,
                "text": cleaned[prefix_length:],
                "streaming": False,
                **({"base_text": base} if not previous_preview else {}),
            }
        )

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        self._phase_override = None
        if self._running:
            self._reset_finalization_progress()
        if not self._running:
            self._paused = False
            self._reset_finalization_progress()
            self._pause_recording_clock()
            self._elapsed_timer.stop()
        self._emit_status()

    def set_recording(self, recording: bool) -> None:
        recording = bool(recording)
        if recording and not self._recording:
            self._recording_elapsed_seconds = 0.0
            self._recording_started_at = time.monotonic()
            self._paused = False
            self._elapsed_timer.start()
        elif not recording and self._recording:
            self._pause_recording_clock()
            self._elapsed_timer.stop()
            self._paused = False
        self._recording = recording
        self._emit_status()

    def set_autostart(self, enabled: bool) -> None:
        if self.app.settings.launch_on_startup != bool(enabled):
            self.app.settings = AppSettings.from_dict(
                {**self.app.settings.to_dict(), "launch_on_startup": bool(enabled)}
            )
        self._emit_settings()

    def set_partial_progress(self, segment_count: int) -> None:
        self._partial_segment_count = max(0, int(segment_count))
        self._emit_status()

    def set_finalization_progress(self, completed_reviews: int, total_reviews: int) -> None:
        total = max(0, int(total_reviews))
        completed = max(0, int(completed_reviews))
        if total > 0:
            completed = min(completed, total)
        else:
            completed = 0
        self._finalization_progress = {
            "active": total > 0,
            "completed_reviews": completed,
            "total_reviews": total,
        }
        self._emit_status()

    def set_audio_level(self, level: float) -> None:
        self._audio_level = max(0.0, min(1.0, float(level)))
        if not self._web_ready:
            return
        if self._incremental_updates:
            self.bridge.audioLevelChanged.emit(self._audio_level)
        else:
            self.bridge.stateChanged.emit(json.dumps({"audio": {"level": self._audio_level}}))

    def set_microphone_state(self, state: dict[str, object]) -> None:
        self._microphone_state = dict(state)
        if not self._web_ready:
            return
        if self._incremental_updates:
            self.bridge.microphoneStateChanged.emit(json.dumps(self._microphone_state, ensure_ascii=False))
        else:
            self.bridge.stateChanged.emit(
                json.dumps({"audio": {"microphone": self._microphone_state}}, ensure_ascii=False)
            )

    def set_session_history(self, sessions: list[dict[str, object]]) -> None:
        self._session_history = [dict(item) for item in sessions if isinstance(item, dict)]
        if not self._web_ready:
            return
        payload = json.dumps(self._session_history, ensure_ascii=False)
        if self._incremental_updates:
            self.bridge.historyChanged.emit(payload)
        else:
            self.bridge.stateChanged.emit(json.dumps({"history": self._session_history}, ensure_ascii=False))

    def set_transcript_session(self, session: dict[str, object]) -> None:
        if not isinstance(session, dict):
            return
        if not self._transcript_text.strip():
            self._transcript_text = str(session.get("text", ""))
        raw_segments = session.get("segments", [])
        self._transcript_segments = (
            [dict(segment) for segment in raw_segments if isinstance(segment, dict)]
            if isinstance(raw_segments, list)
            else []
        )
        self._current_session_id = str(session.get("id", ""))
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._advance_transcript_revision()
        self._emit_transcript()

    def show_error(self, title: str, message: str) -> None:
        self._phase_override = "error"
        self._notify("error", title, message)
        self._emit_status()

    def show_info(self, title: str, message: str) -> None:
        self._notify("info", title, message)

    def show_window(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def hide_window(self) -> None:
        self.hide()

    def close_window(self) -> None:
        self._closing = True
        self.close()

    def start_tray(self) -> bool:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        if self._tray_icon is not None and self._tray_icon.isVisible():
            return True
        self.qt_app.setQuitOnLastWindowClosed(False)
        tray_icon = QSystemTrayIcon(self._window_icon, self)
        tray_icon.setToolTip("语音转文字")
        menu = QMenu(self)
        show_action = QAction("显示主窗口", menu)
        show_action.triggered.connect(lambda _checked=False: self.app.show_window())
        self._tray_toggle_action = QAction("开始识别", menu)
        self._tray_toggle_action.triggered.connect(lambda _checked=False: self.app.toggle())
        exit_action = QAction("退出", menu)
        exit_action.triggered.connect(lambda _checked=False: self.app.exit_app())
        menu.addAction(show_action)
        menu.addAction(self._tray_toggle_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        tray_icon.setContextMenu(menu)
        tray_icon.activated.connect(self._handle_tray_activated)
        tray_icon.show()
        self._tray_icon = tray_icon
        self.set_tray_running(getattr(self.app, "is_running", False))
        return True

    def set_tray_running(self, running: bool) -> None:
        if self._tray_toggle_action is not None:
            self._tray_toggle_action.setText("停止识别" if running else "开始识别")

    def stop_tray(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.hide()
            self._tray_icon.deleteLater()
            self._tray_icon = None
        self._tray_toggle_action = None

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            self._elapsed_timer.stop()
            self._web_ready_watchdog.stop()
            should_quit = getattr(self.app, "ui", self) is self
            page = self.web_view.page()
            self.web_view.setPage(QWebEnginePage(self.web_view))
            page.deleteLater()
            self.web_view.deleteLater()
            event.accept()
            if should_quit:
                QTimer.singleShot(0, self.qt_app.quit)
            return
        event.ignore()
        self.app.request_close()

    @Slot(bool)
    def _handle_load_finished(self, succeeded: bool) -> None:
        self._load_succeeded = bool(succeeded)
        if succeeded:
            if not self._web_ready and not self._fallback_started and not self._closing:
                self._web_ready_watchdog.start()
            return
        self._web_ready_watchdog.stop()
        path = web_runtime_index()
        LOGGER.error("failed to load Flip Glass runtime page: %s", path)
        self._activate_legacy_fallback(path)

    def _handle_web_ready_timeout(self) -> None:
        if self._web_ready or self._fallback_started or self._closing:
            return
        path = web_runtime_index()
        LOGGER.error(
            "Flip Glass runtime page loaded but did not complete the WebChannel handshake within %d ms: %s",
            self.WEB_READY_TIMEOUT_MS,
            path,
        )
        self._activate_legacy_fallback(path)

    def _activate_legacy_fallback(self, failed_path: Path) -> None:
        if self._fallback_started or self._closing:
            return
        self._web_ready_watchdog.stop()
        self._fallback_started = True
        try:
            from .qt_ui import QtVoiceInputWindow

            fallback = QtVoiceInputWindow(self.app)
            if self._transcript_text:
                fallback.append_text(self._transcript_text)
            fallback.set_running(self._running)
            fallback.set_recording(self._recording)
            fallback.set_status(self._last_status_text)
        except Exception:
            self._fallback_started = False
            LOGGER.exception("failed to activate legacy Qt UI after WebEngine load failure")
            QMessageBox.critical(
                self,
                "界面加载失败",
                f"无法加载运行时界面：\n{failed_path}\n\n旧版界面也无法启动，请重新安装应用。",
            )
            return

        self.stop_tray()
        self.app.ui = fallback
        start_tray = getattr(self.app, "_start_tray", None)
        if callable(start_tray):
            self.app._tray_available = bool(start_tray())
        sync_tray = getattr(self.app, "_sync_tray_running", None)
        if callable(sync_tray):
            sync_tray()
        self._closing = True
        self.close()

    def _handle_web_ready(self, *, incremental: bool = False) -> None:
        if self._fallback_started or self._closing:
            return
        self._web_ready_watchdog.stop()
        self._incremental_updates = bool(incremental)
        self._web_ready = True
        self._emit_state()
        pending = list(self._pending_notifications)
        self._pending_notifications.clear()
        for notice in pending:
            self.bridge.notificationRequested.emit(json.dumps(notice, ensure_ascii=False))

    def _emit_state(self) -> None:
        if not self._web_ready:
            return
        self.bridge.stateChanged.emit(json.dumps(self._state_payload(), ensure_ascii=False))

    def _emit_status(self) -> None:
        if not self._web_ready:
            return
        payload = self._status_payload()
        if self._incremental_updates:
            self.bridge.statusChanged.emit(json.dumps(payload, ensure_ascii=False))
        else:
            self.bridge.stateChanged.emit(json.dumps({"status": payload}, ensure_ascii=False))

    def _emit_clock(self) -> None:
        if not self._web_ready:
            return
        elapsed_seconds = self._elapsed_seconds()
        if self._incremental_updates:
            self.bridge.clockChanged.emit(elapsed_seconds)
        else:
            self.bridge.stateChanged.emit(
                json.dumps({"status": {"elapsed_seconds": elapsed_seconds}}, ensure_ascii=False)
            )

    def _emit_transcript(self) -> None:
        if not self._web_ready:
            return
        payload = self._transcript_payload()
        if self._incremental_updates:
            self.bridge.transcriptChanged.emit(json.dumps(payload, ensure_ascii=False))
        else:
            self.bridge.stateChanged.emit(json.dumps({"transcript": payload}, ensure_ascii=False))

    def _emit_transcript_delta(self, payload: dict[str, object]) -> None:
        if not self._web_ready:
            return
        if self._incremental_updates:
            self.bridge.transcriptDeltaChanged.emit(json.dumps(payload, ensure_ascii=False))
        else:
            self._emit_transcript()

    def _emit_settings(self) -> None:
        if not self._web_ready:
            return
        payload = self._settings_payload()
        if self._incremental_updates:
            self.bridge.settingsChanged.emit(json.dumps(payload, ensure_ascii=False))
        else:
            self.bridge.stateChanged.emit(json.dumps(payload, ensure_ascii=False))

    def _state_payload(self) -> dict[str, object]:
        return {
            "connected": True,
            "status": self._status_payload(),
            "transcript": self._transcript_payload(),
            **self._settings_payload(),
            "audio": {
                "level": self._audio_level,
                "microphone": self._microphone_state,
            },
            "history": self._session_history,
            "capabilities": {
                "max_recording_seconds": DEFAULT_MAX_RECORDING_SECONDS,
                "runtime": self._runtime_label(),
                **self._product_capabilities(),
            },
        }

    def _status_payload(self) -> dict[str, object]:
        return {
            "phase": self._phase_text(),
            "message": self._last_status_text,
            "detail": self._status_detail(self._last_status_text),
            "running": self._running,
            "recording": self._recording,
            "paused": self._paused,
            "elapsed_seconds": self._elapsed_seconds(),
            "partial_segments": self._partial_segment_count,
            "finalization": dict(self._finalization_progress),
        }

    def _transcript_payload(self) -> dict[str, object]:
        return {
            "text": self._transcript_text,
            "editable": self.app.settings.transcript_editable,
            "streaming": bool(self._streaming_preview_text),
            "session_id": self._current_session_id,
            "segments": self._transcript_segments,
            "revision": self._transcript_revision,
        }

    def _product_capabilities(self) -> dict[str, object]:
        product = bundled_product_profile()
        declared = product.get("funasr_capabilities")
        capabilities = dict(declared) if isinstance(declared, dict) else {}
        profile = packaged_model_profile()
        offline_review = bool(capabilities.get("offline_review", profile != "streaming"))
        return {
            "model_profile": profile,
            "resource_profile": str(getattr(self.app, "resource_profile", "balanced")),
            "streaming": bool(capabilities.get("streaming", True)),
            "audio_import": offline_review,
            "offline_review": offline_review,
            "punctuation": bool(capabilities.get("punctuation", offline_review)),
            "speaker_recognition": bool(
                capabilities.get("speaker", False) or self.app.settings.funasr_speaker_model.strip()
            ),
            "structured_history": True,
        }

    def _settings_payload(self) -> dict[str, object]:
        settings = self.app.settings
        lexicons = [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
            }
            for item in bundled_lexicon_catalog(product_tier=settings.product_tier.strip() or None)
        ]
        return {
            "settings": settings.to_dict(),
            "options": {
                "languages": [
                    {"value": value, "label": label}
                    for value, label in LANGUAGE_OPTIONS
                ],
                "lexicons": lexicons,
            },
        }

    def _phase_text(self) -> str:
        if self._phase_override:
            return self._phase_override
        if self._paused:
            return "paused"
        if self._recording:
            return "recording"
        if self._running:
            return "transcribing"
        return "ready"

    def _status_detail(self, message: str) -> str:
        if self._paused:
            return "录音已暂停，继续后恢复麦克风输入和实时转写"
        if self._finalization_progress["active"]:
            completed = self._finalization_progress["completed_reviews"]
            total = self._finalization_progress["total_reviews"]
            return f"正在完成本地离线复核（{completed}/{total}）"
        if self._recording:
            return "麦克风输入正常，转写内容会持续更新"
        if self._running:
            return "本地识别模型正在处理当前内容"
        if "设置" in message:
            return "新的运行参数已经生效"
        if "托盘" in message:
            return "应用仍在后台保持运行"
        if "完成" in message or "就绪" in message:
            return "所有语音处理均在本机完成"
        return "本地语音运行时"

    def _runtime_label(self) -> str:
        backend = self.app.settings.asr_backend.strip().lower()
        if backend in {"funasr", "paraformer", "local"}:
            return "本地 FunASR"
        if backend in {"whisper", "whisper.cpp", "whisper-cpp"}:
            return "本地 Whisper"
        return "本地处理"

    def _elapsed_seconds(self) -> int:
        elapsed = self._recording_elapsed_seconds
        if self._recording_started_at is not None:
            elapsed += max(0.0, time.monotonic() - self._recording_started_at)
        return max(0, int(elapsed))

    def _pause_recording_clock(self) -> None:
        if self._recording_started_at is None:
            return
        self._recording_elapsed_seconds += max(0.0, time.monotonic() - self._recording_started_at)
        self._recording_started_at = None

    def _resume_recording_clock(self) -> None:
        if not self._recording or self._recording_started_at is not None:
            return
        self._recording_started_at = time.monotonic()
        if not self._elapsed_timer.isActive():
            self._elapsed_timer.start()

    def _reset_finalization_progress(self) -> None:
        self._finalization_progress = {
            "active": False,
            "completed_reviews": 0,
            "total_reviews": 0,
        }

    def _toggle_pause(self) -> None:
        if not getattr(self.app, "is_running", False):
            return
        if self.app.toggle_recording_pause():
            self._paused = bool(getattr(self.app, "is_recording_paused", False))
            if self._paused:
                self._pause_recording_clock()
            else:
                self._resume_recording_clock()
            self._emit_status()

    def _set_transcript_from_web(self, text: str) -> None:
        if not self.app.settings.transcript_editable:
            return
        self._transcript_text = str(text)
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._transcript_segments = []
        self._current_session_id = ""
        self._advance_transcript_revision()

    def _set_transcript_revision_from_web(self, payload: str) -> None:
        if not self.app.settings.transcript_editable or self._running:
            return
        try:
            request = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(request, dict):
            return
        try:
            base_revision = int(request.get("base_revision"))
        except (TypeError, ValueError):
            return
        if base_revision != self._transcript_revision:
            return
        self._transcript_text = str(request.get("text", ""))
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._transcript_segments = []
        self._current_session_id = ""
        self._advance_transcript_revision()

    def _copy_text(self) -> None:
        if self._transcript_text:
            QApplication.clipboard().setText(self._transcript_text)

    def _clear_text(self) -> None:
        self._transcript_text = ""
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._transcript_segments = []
        self._current_session_id = ""
        self._advance_transcript_revision()
        self._emit_transcript()

    def _request_history_session(self, session_id: str) -> None:
        record = self._history_session_by_id(session_id)
        if record is None:
            self._notify("error", "会话不存在", "该历史会话可能已被清理。")
            return
        if self._web_ready:
            self.bridge.historySessionChanged.emit(json.dumps(record, ensure_ascii=False))

    def _load_history_session(self, session_id: str) -> None:
        if self._running:
            self._notify("toast", "录制进行中", "请在当前识别结束后再载入历史会话")
            return
        record = self._history_session_by_id(session_id)
        if record is None:
            self._notify("error", "会话不存在", "该历史会话可能已被清理。")
            return
        self._transcript_text = str(record.get("text", ""))
        raw_segments = record.get("segments", [])
        self._transcript_segments = [
            dict(segment) for segment in raw_segments if isinstance(segment, dict)
        ] if isinstance(raw_segments, list) else []
        self._current_session_id = str(record.get("id", ""))
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._advance_transcript_revision()
        self._emit_transcript()
        self._notify("toast", "已载入历史会话", "转写稿和时间段信息已恢复")

    def _history_session_by_id(self, session_id: str) -> dict[str, object] | None:
        store = getattr(self.app, "history_store", None)
        get_session = getattr(store, "get_session", None)
        if callable(get_session):
            record = get_session(str(session_id))
            if isinstance(record, dict):
                return dict(record)
        return next(
            (dict(item) for item in self._session_history if str(item.get("id", "")) == str(session_id)),
            None,
        )

    def _advance_transcript_revision(self) -> int:
        self._transcript_revision += 1
        return self._transcript_revision

    def _import_audio(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "导入音频文件",
            "",
            f"Audio files ({' '.join(f'*{extension}' for extension in SUPPORTED_AUDIO_FILE_EXTENSIONS)});;All files (*.*)",
        )
        if path:
            self.app.transcribe_audio_file(Path(path))

    def _export_document(self, kind: str) -> None:
        exporters: dict[str, tuple[Callable[..., None], str, str]] = {
            "word": (export_transcript_docx, "导出 Word 文档", "识别结果.docx"),
            "official-word": (export_official_document_docx, "导出公文 Word", "公文转写.docx"),
            "minutes-word": (export_meeting_minutes_docx, "导出会议纪要 Word", "会议纪要.docx"),
        }
        if kind in exporters:
            exporter, title, default_name = exporters[kind]
            self._export_docx(exporter, title, default_name)
            return
        pdfs: dict[str, tuple[str, str, Callable[[str], str]]] = {
            "pdf": ("导出 PDF", "识别结果.pdf", self._regular_pdf_html),
            "official-pdf": ("导出公文 PDF", "公文转写.pdf", self._official_pdf_html),
            "minutes-pdf": ("导出会议纪要 PDF", "会议纪要.pdf", self._minutes_pdf_html),
        }
        if kind in pdfs:
            title, default_name, html_factory = pdfs[kind]
            self._export_pdf(title, default_name, html_factory)

    def _export_docx(self, exporter: Callable[..., None], title: str, default_name: str) -> None:
        text = self._transcript_text.strip()
        if not text:
            self.show_info("没有可导出的内容", "请先完成一次识别，或启用编辑后输入文本。")
            return
        path, _selected_filter = QFileDialog.getSaveFileName(self, title, default_name, "Word document (*.docx)")
        if not path:
            return
        if not path.lower().endswith(".docx"):
            path += ".docx"
        try:
            exporter(path, text)
        except Exception as exc:
            self.show_error("导出失败", str(exc))
            return
        self._notify("toast", "导出完成", f"文档已保存：{path}")

    def _export_pdf(self, title: str, default_name: str, html_factory: Callable[[str], str]) -> None:
        text = self._transcript_text.strip()
        if not text:
            self.show_info("没有可导出的内容", "请先完成一次识别，或启用编辑后输入文本。")
            return
        path, _selected_filter = QFileDialog.getSaveFileName(self, title, default_name, "PDF document (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(path)
        try:
            document = QTextDocument(self)
            document.setDefaultFont(_ui_font(10))
            document.setHtml(html_factory(text))
            document.print_(printer)
        except Exception as exc:
            self.show_error("导出 PDF 失败", str(exc))
            return
        self._notify("toast", "导出完成", f"PDF 已保存：{path}")

    def _save_settings_payload(self, payload: str) -> None:
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            self.show_error("设置未保存", "设置数据格式无效，请重新打开设置后再试。")
            return
        if not isinstance(data, dict):
            self.show_error("设置未保存", "设置数据格式无效。")
            return
        allowed = {
            "hotkey",
            "start_minimized",
            "floating_window",
            "inject_text",
            "enable_hotkey",
            "minimize_to_tray",
            "launch_on_startup",
            "recognition_language",
            "transcript_editable",
            "streaming_transcription_enabled",
            "lexicon_enabled",
            "lexicon_path",
            "enabled_builtin_lexicons",
        }
        patch = {key: value for key, value in data.items() if key in allowed}
        if "enabled_builtin_lexicons" in patch and isinstance(patch["enabled_builtin_lexicons"], list):
            patch["enabled_builtin_lexicons"] = tuple(str(item) for item in patch["enabled_builtin_lexicons"])
        if not str(patch.get("hotkey", self.app.settings.hotkey)).strip():
            patch["hotkey"] = DEFAULT_HOTKEY
        previous = self.app.settings
        requested = AppSettings.from_dict({**previous.to_dict(), **patch})
        self.app.update_settings(requested)
        current = self.app.settings
        self._apply_topmost(current.floating_window)
        if all(getattr(current, key) == getattr(requested, key) for key in patch):
            self._notify("toast", "设置已保存", "新的运行参数已经生效")
        self._emit_settings()

    def _select_custom_lexicon(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "选择词库文件",
            "",
            "Lexicon files (*.txt *.json *.csv);;All files (*.*)",
        )
        if not path:
            return
        settings = AppSettings.from_dict(
            {
                **self.app.settings.to_dict(),
                "lexicon_enabled": True,
                "lexicon_path": path,
            }
        )
        self.app.update_settings(settings)
        self._emit_settings()

    def _notify(self, kind: str, title: str, message: str) -> None:
        notice = {"kind": kind, "title": str(title), "message": str(message)}
        if not self._web_ready:
            self._pending_notifications.append(notice)
            return
        self.bridge.notificationRequested.emit(json.dumps(notice, ensure_ascii=False))

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _apply_topmost(self, enabled: bool, *, show: bool = True) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(enabled))
        if show:
            self.show()

    def _handle_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.app.show_window()

    def _load_window_icon(self) -> QIcon:
        icon = bundled_app_icon()
        if icon.is_file():
            loaded = QIcon(str(icon))
            if not loaded.isNull():
                return loaded
        return self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

    @staticmethod
    def _regular_pdf_html(text: str) -> str:
        body = "".join(f"<p>{escape_html(paragraph)}</p>" for paragraph in text.splitlines() or [""])
        return f'''<html><head><style>
body {{ font-family: "Microsoft YaHei UI", sans-serif; color: #1d2939; margin: 42px; }}
h1 {{ font-size: 22px; margin: 0 0 24px; }}
p {{ font-size: 13px; line-height: 1.75; margin: 0 0 10px; }}
</style></head><body><h1>识别结果</h1>{body}</body></html>'''

    @staticmethod
    def _official_pdf_html(text: str) -> str:
        paragraphs = "".join(
            f"<p class='body'>{escape_html(paragraph)}</p>"
            for paragraph in text.splitlines()
            if paragraph.strip()
        )
        return f'''<html><head><style>
body {{ font-family: "FangSong", "仿宋_GB2312", serif; color: #111827; margin: 56px 52px; }}
.red {{ color: #b42318; text-align: center; font-size: 18px; letter-spacing: 2px; border-bottom: 2px solid #b42318; padding-bottom: 8px; }}
h1 {{ font-family: "SimHei", "黑体", sans-serif; text-align: center; font-size: 24px; margin: 28px 0 20px; }}
.meta {{ font-size: 14px; line-height: 1.9; margin: 0; }}
.body {{ font-size: 15px; line-height: 2.05; text-indent: 2em; margin: 0 0 8px; }}
.tail {{ border-top: 1px solid #d0d5dd; margin-top: 28px; padding-top: 10px; font-size: 13px; }}
</style></head><body><div class="red">语音转文字</div><h1>公文转写</h1>
<p class="meta">发文字号：〔    〕  号</p><p class="meta">签发人：</p><p class="meta">主送：</p>
{paragraphs}<div class="tail">附件：<br/>联系人：　　　　联系电话：<br/>抄送：</div></body></html>'''

    @staticmethod
    def _minutes_pdf_html(text: str) -> str:
        paragraphs = [paragraph.strip() for paragraph in text.splitlines() if paragraph.strip()]
        topic = escape_html(paragraphs[0]) if paragraphs else "（请补充会议议题）"
        content = "".join(f"<p>{escape_html(paragraph)}</p>" for paragraph in paragraphs)
        return f'''<html><head><style>
body {{ font-family: "Microsoft YaHei UI", "SimSun", sans-serif; color: #1d2939; margin: 48px; }}
h1 {{ text-align: center; font-size: 22px; margin: 0 0 22px; }}
.meta {{ font-size: 13px; line-height: 1.8; margin: 0; }}
h2 {{ font-size: 15px; margin: 20px 0 8px; }}
p {{ font-size: 13px; line-height: 1.8; margin: 0 0 8px; }}
.action {{ background: #f2f4f7; padding: 8px 10px; border-left: 3px solid #2563eb; }}
</style></head><body><h1>会议纪要</h1><p class="meta">会议时间：</p><p class="meta">会议地点：</p>
<p class="meta">主持人：　　　　记录人：</p><p class="meta">参会人员：</p><h2>一、会议议题</h2><p>{topic}</p>
<h2>二、会议内容</h2>{content}<h2>三、决议事项</h2><p class="action">1. </p>
<h2>四、待办跟进</h2><p class="action">责任人：　　　　截止时间：　　　　事项：</p></body></html>'''


# Compatibility alias for code that expects a generic Qt window name.
QtVoiceInputWindow = FlipGlassVoiceInputWindow
