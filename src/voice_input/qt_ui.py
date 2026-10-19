"""Qt/PySide6 runtime user interface for the desktop voice input shell."""

from __future__ import annotations

from html import escape as escape_html
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCloseEvent,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QStyle,
    QVBoxLayout,
    QWidget,
    QLineEdit,
)

from .config import (
    DEFAULT_MAX_RECORDING_SECONDS,
    DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS,
    SUPPORTED_AUDIO_FILE_EXTENSIONS,
)
from .exporters import export_meeting_minutes_docx, export_official_document_docx, export_transcript_docx
from .language import LANGUAGE_OPTIONS, language_label
from .media import supported_audio_format_names
from .resources import bundled_app_icon, bundled_lexicon_catalog
from .settings import DEFAULT_HOTKEY, AppSettings

if TYPE_CHECKING:
    from .app import VoiceInputApp

ENGINE_OPTIONS: tuple[tuple[str, str], ...] = (("funasr", "高精度本地识别"),)


def _app() -> QApplication:
    instance = QApplication.instance()
    if instance is not None:
        return instance
    created = QApplication(sys.argv[:1])
    created.setApplicationName("语音转文字")
    created.setStyle("Fusion")
    _register_cjk_font()
    created.setFont(_ui_font(10))
    return created


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时" if not minutes and not seconds else f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


def _ui_font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    available = set(QFontDatabase.families())
    preferred = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans SC",
        "DengXian",
        "SimHei",
        "SimSun",
        "Arial",
    )
    family = next((name for name in preferred if not available or name in available), preferred[0])
    font = QFont(family, size)
    font.setWeight(weight)
    return font


def _register_cjk_font() -> None:
    if sys.platform != "win32":
        return
    font_dir = Path("C:/Windows/Fonts")
    for filename in ("msyh.ttc", "msyh.ttf", "NotoSansSC-VF.ttf", "Deng.ttf", "simhei.ttf", "simsun.ttc"):
        path = font_dir / filename
        if path.exists() and QFontDatabase.addApplicationFont(str(path)) >= 0:
            return


class ModernCheckBox(QCheckBox):
    """A crisp painted checkbox so Qt stylesheets do not fall back to color blocks."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 42, max(30, metrics.height() + 10))

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        box_size = 20
        box_x = 1
        box_y = (self.height() - box_size) / 2
        box = QRectF(box_x, box_y, box_size, box_size)
        enabled = self.isEnabled()
        checked = self.isChecked()

        if checked and enabled:
            fill = QColor("#2f6fed")
            border = QColor("#2f6fed")
            text = QColor("#263446")
        elif checked:
            fill = QColor("#e6e9ef")
            border = QColor("#d2d8e2")
            text = QColor("#98a2b3")
        else:
            fill = QColor("#ffffff")
            border = QColor("#98a2b3" if enabled else "#d2d8e2")
            text = QColor("#263446" if enabled else "#98a2b3")

        painter.setPen(QPen(border, 1.8))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 5.5, 5.5)

        if checked and enabled:
            check = QPainterPath()
            check.moveTo(box_x + 5.0, box_y + 10.5)
            check.lineTo(box_x + 8.4, box_y + 14.0)
            check.lineTo(box_x + 15.5, box_y + 6.2)
            painter.setPen(QPen(QColor("#ffffff"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(check)

        painter.setPen(text)
        painter.setFont(self.font())
        painter.drawText(QRectF(32, 0, self.width() - 32, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())


class HotkeyEdit(QLineEdit):
    """Capture a keyboard shortcut instead of asking users to type one."""

    hotkeyCaptured = Signal(str)

    _MODIFIER_KEYS = {
        int(Qt.Key.Key_Control),
        int(Qt.Key.Key_Shift),
        int(Qt.Key.Key_Alt),
        int(Qt.Key.Key_Meta),
    }
    _NAMED_KEYS = {
        int(Qt.Key.Key_Space): "Space",
        int(Qt.Key.Key_Tab): "Tab",
        int(Qt.Key.Key_Return): "Enter",
        int(Qt.Key.Key_Enter): "Enter",
        int(Qt.Key.Key_Escape): "Esc",
        int(Qt.Key.Key_Backspace): "Backspace",
        int(Qt.Key.Key_Delete): "Delete",
        int(Qt.Key.Key_Insert): "Insert",
        int(Qt.Key.Key_Home): "Home",
        int(Qt.Key.Key_End): "End",
        int(Qt.Key.Key_PageUp): "PageUp",
        int(Qt.Key.Key_PageDown): "PageDown",
        int(Qt.Key.Key_Up): "Up",
        int(Qt.Key.Key_Down): "Down",
        int(Qt.Key.Key_Left): "Left",
        int(Qt.Key.Key_Right): "Right",
    }

    def __init__(self, hotkey: str) -> None:
        super().__init__(hotkey)
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setPlaceholderText("点击后按下快捷键")
        self.setToolTip("点击这里，然后直接按下想使用的快捷键。")

    def keyPressEvent(self, event: object) -> None:
        key = int(event.key())  # type: ignore[attr-defined]
        if key in self._MODIFIER_KEYS:
            return
        if key in {int(Qt.Key.Key_Backspace), int(Qt.Key.Key_Delete)}:
            self.clear()
            self.hotkeyCaptured.emit("")
            return
        if key == int(Qt.Key.Key_Escape):
            self.clearFocus()
            return

        key_name = self._key_name(key)
        if not key_name:
            return

        modifiers = event.modifiers()  # type: ignore[attr-defined]
        parts: list[str] = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        parts.append(key_name)
        hotkey = "+".join(parts)
        self.setText(hotkey)
        self.hotkeyCaptured.emit(hotkey)

    def mousePressEvent(self, event: object) -> None:
        super().mousePressEvent(event)  # type: ignore[arg-type]
        self.selectAll()

    @classmethod
    def _key_name(cls, key: int) -> str:
        if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
            return chr(key)
        if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
            return chr(key)
        if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
            return f"F{key - int(Qt.Key.Key_F1) + 1}"
        return cls._NAMED_KEYS.get(key, "")


class _UiCallbackDispatcher(QObject):
    """Run callbacks on the Qt UI thread, including requests from hotkey threads."""

    requested = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.requested.connect(self._dispatch)

    @Slot(int, object)
    def _dispatch(self, delay_ms: int, callback: Callable[[], None]) -> None:
        QTimer.singleShot(max(0, int(delay_ms)), callback)


class SettingsDialog(QDialog):
    """Product-style settings dialog with grouped controls."""

    def __init__(self, parent: "QtVoiceInputWindow") -> None:
        super().__init__(parent)
        self.window = parent
        self.engine_combo = QComboBox()
        self.engine_combo.addItems([value for value, _label in ENGINE_OPTIONS])
        self.engine_combo.setEditable(False)
        engine_index = self.engine_combo.findText(parent.backend_combo.currentText())
        self.engine_combo.setCurrentIndex(max(engine_index, 0))
        self.engine_combo.currentTextChanged.connect(parent._apply_settings_engine)
        self.engine_select_button: QPushButton | None = None
        self.engine_select_actions: dict[str, QAction] = {}
        self.language_select_button: QPushButton | None = None
        self.language_select_actions: dict[str, QAction] = {}
        self.setWindowTitle("设置")
        self.setModal(True)
        self.resize(780, 560)
        self.setMinimumSize(720, 500)
        self.setStyleSheet(parent.styleSheet())
        self._build_layout()

    def _build_layout(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)

        nav = QVBoxLayout()
        nav.setSpacing(8)
        root.addLayout(nav)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        pages = [
            ("常规", self._general_page()),
            ("快捷键", self._input_page()),
            ("词库", self._lexicon_page()),
        ]
        self.nav_buttons: list[QPushButton] = []
        for index, (title, page) in enumerate(pages):
            button = self.window._button(title, "nav")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, i=index: self._show_page(i))
            nav.addWidget(button)
            self.nav_buttons.append(button)
            self.stack.addWidget(page)
        nav.addStretch(1)
        self._show_page(0)

    def _show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for button_index, button in enumerate(self.nav_buttons):
            button.setChecked(button_index == index)

    def _page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("DialogPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        return page

    def _general_page(self) -> QWidget:
        page = self._page()
        layout = page.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.addWidget(self.window._section_title("常规"))
        for checkbox in (
            self.window.topmost_check,
            self.window.tray_check,
            self.window.startup_check,
            self.window.start_minimized_check,
        ):
            checkbox.stateChanged.connect(self.window._save_settings)
            layout.addWidget(checkbox)
        layout.addStretch(1)
        return page

    def _input_page(self) -> QWidget:
        page = self._page()
        layout = page.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.addWidget(self.window._section_title("快捷键"))
        layout.addWidget(self.window._field_label("按键"))
        row = QHBoxLayout()
        row.addWidget(self.window.hotkey_input, 1)
        save_button = self.window._button("保存", "secondary")
        save_button.clicked.connect(self.window._save_settings)
        row.addWidget(save_button)
        layout.addLayout(row)
        for checkbox in (self.window.hotkey_check, self.window.inject_check):
            checkbox.stateChanged.connect(self.window._save_settings)
            layout.addWidget(checkbox)
        layout.addStretch(1)
        return page

    def _lexicon_page(self) -> QWidget:
        page = self._page()
        layout = page.layout()
        assert isinstance(layout, QVBoxLayout)
        layout.addWidget(self.window._section_title("词库"))
        self.window.lexicon_check.stateChanged.connect(self.window._save_settings)
        layout.addWidget(self.window.lexicon_check)
        self.window.lexicon_summary_label = QLabel(self.window._lexicon_summary_text())
        self.window.lexicon_summary_label.setObjectName("MutedText")
        self.window.lexicon_summary_label.setWordWrap(True)
        layout.addWidget(self.window.lexicon_summary_label)
        self.window.lexicon_effect_label = QLabel(self.window._lexicon_effect_text())
        self.window.lexicon_effect_label.setObjectName("MutedText")
        self.window.lexicon_effect_label.setWordWrap(True)
        layout.addWidget(self.window.lexicon_effect_label)
        manage_button = self.window._button("管理词库", "secondary")
        manage_button.clicked.connect(self.window._open_lexicon_manager)
        row = QHBoxLayout()
        row.addWidget(manage_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _path_row(self, line_edit: QLineEdit, callback: Callable[[], None]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(line_edit, 1)
        button = self.window._button("选择", "secondary")
        button.clicked.connect(callback)
        row.addWidget(button)
        return row

    def _setting_select_row(self, title: str, control: QPushButton) -> QFrame:
        frame = QFrame()
        frame.setObjectName("SettingRow")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(12)
        label = QLabel(title)
        label.setObjectName("SettingTitle")
        layout.addWidget(label, 1)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return frame

    def _select_button(
        self,
        options: list[tuple[str, str]],
        current_value: str,
        callback: Callable[[str], None],
    ) -> tuple[QPushButton, dict[str, QAction]]:
        button = self.window._button("", "select")
        button.setMinimumWidth(154)
        button.setMaximumWidth(220)
        menu = QMenu(button)
        group = QActionGroup(menu)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for value, label in options:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(value == current_value)
            action.triggered.connect(lambda _checked=False, selected=value: callback(selected))
            group.addAction(action)
            menu.addAction(action)
            actions[value] = action
        button.setMenu(menu)
        self._sync_select_button(button, actions, current_value)
        return button, actions

    def _sync_select_button(self, button: QPushButton | None, actions: dict[str, QAction], value: str) -> None:
        if button is None:
            return
        selected_text = value
        for action_value, action in actions.items():
            action.blockSignals(True)
            action.setChecked(action_value == value)
            action.blockSignals(False)
            if action_value == value:
                selected_text = action.text()
        button.setText(f"{selected_text}  ▾")

    def sync_engine_selector(self, value: str) -> None:
        self._sync_select_button(self.engine_select_button, self.engine_select_actions, value)

    def sync_language_selector(self, value: str) -> None:
        self._sync_select_button(self.language_select_button, self.language_select_actions, value)


class FindReplaceDialog(QDialog):
    """Small text tool for transcript review without crowding the workbench."""

    def __init__(self, parent: "QtVoiceInputWindow") -> None:
        super().__init__(parent)
        self.window = parent
        self.setWindowTitle("查找替换")
        self.setModal(False)
        self.resize(460, 210)
        self.setMinimumWidth(420)
        self.setStyleSheet(parent.styleSheet())
        self._build_layout()

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        root.addWidget(self.window._field_label("查找"))
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("输入要查找的文本")
        root.addWidget(self.find_input)

        root.addWidget(self.window._field_label("替换为"))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("输入替换文本")
        root.addWidget(self.replace_input)

        row = QHBoxLayout()
        row.setSpacing(10)
        find_button = self.window._button("查找下一个", "secondary")
        find_button.clicked.connect(self._find_next)
        replace_button = self.window._button("替换当前", "secondary")
        replace_button.clicked.connect(self._replace_current)
        replace_all_button = self.window._button("全部替换", "primary")
        replace_all_button.clicked.connect(self._replace_all)
        row.addWidget(find_button)
        row.addWidget(replace_button)
        row.addWidget(replace_all_button)
        root.addLayout(row)

    def _find_next(self) -> None:
        query = self.find_input.text()
        if not self.window._find_text(query):
            self.window.show_info("未找到", "当前文本中没有匹配内容。")

    def _replace_current(self) -> None:
        query = self.find_input.text()
        replacement = self.replace_input.text()
        if not self.window._replace_current_text(query, replacement):
            self.window.show_info("未替换", "请先输入要查找的文本。")

    def _replace_all(self) -> None:
        query = self.find_input.text()
        replacement = self.replace_input.text()
        count = self.window._replace_all_text(query, replacement)
        self.window.show_info("替换完成", f"已替换 {count} 处。")


class LexiconManagerDialog(QDialog):
    """Focused lexicon configuration without crowding the main settings panel."""

    def __init__(self, parent: "QtVoiceInputWindow") -> None:
        super().__init__(parent)
        self.window = parent
        self.builtin_checks: dict[str, ModernCheckBox] = {}
        self.lexicon_enabled_check = ModernCheckBox("启用词库优化")
        self.lexicon_enabled_check.setChecked(parent.lexicon_check.isChecked())
        self.lexicon_path_input = QLineEdit(parent.lexicon_input.text())
        self.setWindowTitle("词库管理")
        self.setModal(True)
        self.resize(640, 520)
        self.setMinimumSize(580, 460)
        self.setStyleSheet(parent.styleSheet())
        self._build_layout()

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(14)
        root.addWidget(self.window._section_title("词库优化"))

        self.lexicon_enabled_check.stateChanged.connect(self._handle_changed)
        root.addWidget(self.lexicon_enabled_check)

        self.summary_label = QLabel(self.window._lexicon_effect_text())
        self.summary_label.setObjectName("MutedText")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        root.addWidget(self.window._field_label("内置词库"))
        for item in bundled_lexicon_catalog(product_tier=self.window.product_tier):
            checkbox = ModernCheckBox(item.title)
            checkbox.setToolTip(item.description)
            checkbox.setChecked(item.id in self.window.enabled_builtin_lexicons)
            checkbox.stateChanged.connect(self._handle_changed)
            self.builtin_checks[item.id] = checkbox
            root.addWidget(checkbox)

        root.addWidget(self.window._field_label("自定义词库"))
        root.addLayout(self._path_row(self.lexicon_path_input, self._select_custom_lexicon_path))
        self.custom_status_label = QLabel(self.window._custom_lexicon_text())
        self.custom_status_label.setObjectName("MutedText")
        self.custom_status_label.setWordWrap(True)
        root.addWidget(self.custom_status_label)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = self.window._button("完成", "primary")
        close_button.clicked.connect(self.close)
        close_row.addWidget(close_button)
        root.addLayout(close_row)
        root.addStretch(1)

    def _path_row(self, line_edit: QLineEdit, callback: Callable[[], None]) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(line_edit, 1)
        button = self.window._button("选择", "secondary")
        button.clicked.connect(callback)
        row.addWidget(button)
        return row

    def sync_builtin_checks(self) -> None:
        for key, checkbox in self.builtin_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(key in self.window.enabled_builtin_lexicons)
            checkbox.blockSignals(False)
        self.sync_summary()

    def sync_controls(self) -> None:
        self.lexicon_enabled_check.blockSignals(True)
        self.lexicon_enabled_check.setChecked(self.window.lexicon_check.isChecked())
        self.lexicon_enabled_check.blockSignals(False)
        self.lexicon_path_input.blockSignals(True)
        self.lexicon_path_input.setText(self.window.lexicon_input.text())
        self.lexicon_path_input.blockSignals(False)
        self.sync_builtin_checks()

    def sync_summary(self) -> None:
        if hasattr(self, "summary_label"):
            self.summary_label.setText(self.window._lexicon_effect_text())
        if hasattr(self, "custom_status_label"):
            self.custom_status_label.setText(self.window._custom_lexicon_text())

    def _select_custom_lexicon_path(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(self, "选择词库文件", "", "Lexicon files (*.txt *.json *.csv);;All files (*.*)")
        if path:
            self.lexicon_path_input.setText(path)
            self.lexicon_enabled_check.setChecked(True)
            self._handle_changed()

    def _handle_changed(self) -> None:
        self.window.enabled_builtin_lexicons = tuple(
            key for key, checkbox in self.builtin_checks.items() if checkbox.isChecked()
        )
        self.window.lexicon_check.blockSignals(True)
        self.window.lexicon_check.setChecked(self.lexicon_enabled_check.isChecked())
        self.window.lexicon_check.blockSignals(False)
        self.window.lexicon_input.setText(self.lexicon_path_input.text().strip())
        self.window._save_settings()
        self.sync_summary()


class QtVoiceInputWindow(QMainWindow):
    """Fluent-inspired transcript workbench for runtime use."""

    def __init__(self, app: "VoiceInputApp") -> None:
        self.qt_app = _app()
        super().__init__()
        self.setFont(_ui_font(10))
        self.app = app
        self._callback_dispatcher = _UiCallbackDispatcher(self)
        self._closing = False
        self._tray_icon: QSystemTrayIcon | None = None
        self._tray_toggle_action: QAction | None = None
        self._settings_dialog: SettingsDialog | None = None
        self._lexicon_dialog: LexiconManagerDialog | None = None
        self._find_dialog: FindReplaceDialog | None = None
        self._partial_segment_count = 0
        self._streaming_base_text: str | None = None
        self._streaming_preview_text = ""
        self._recording_started_at: float | None = None
        self._recording_elapsed_seconds = 0.0
        self._recording_clock_active = False
        self._last_status_text = "正在初始化"
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._sync_session_panel)

        self.setWindowTitle("语音转文字")
        self._window_icon = self._load_window_icon()
        if not self._window_icon.isNull():
            self.setWindowIcon(self._window_icon)
        self.resize(1240, 760)
        self.setMinimumSize(1020, 680)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, app.settings.floating_window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAutoFillBackground(True)
        self.setStyleSheet(STYLE_SHEET)

        self._init_controls(app.settings)
        self._build_menus()
        self._build_layout()
        self.set_running(False)
        self.set_recording(False)
        if app.settings.start_minimized:
            self.hide()
        else:
            self.show()

    def _init_controls(self, settings: AppSettings) -> None:
        self.hotkey_input = HotkeyEdit(settings.hotkey)
        self.hotkey_input.hotkeyCaptured.connect(lambda _hotkey: self._save_settings())
        self.topmost_check = ModernCheckBox("窗口置顶")
        self.topmost_check.setChecked(settings.floating_window)
        self.inject_check = ModernCheckBox("识别完成后自动粘贴")
        self.inject_check.setChecked(settings.inject_text)
        self.hotkey_check = ModernCheckBox("启用快捷键")
        self.hotkey_check.setChecked(settings.enable_hotkey)
        self.tray_check = ModernCheckBox("关闭到托盘")
        self.tray_check.setChecked(settings.minimize_to_tray)
        self.startup_check = ModernCheckBox("开机自启")
        self.startup_check.setChecked(settings.launch_on_startup)
        self.start_minimized_check = ModernCheckBox("启动后最小化")
        self.start_minimized_check.setChecked(settings.start_minimized)
        self.editable_check = ModernCheckBox("启用编辑")
        self.editable_check.setChecked(settings.transcript_editable)
        self.lexicon_check = ModernCheckBox("自动修正专业词")
        self.lexicon_check.setChecked(settings.lexicon_enabled)
        self.streaming_check = ModernCheckBox("低延迟流式输出")
        self.streaming_check.setChecked(settings.streaming_transcription_enabled)
        self.enabled_builtin_lexicons = tuple(settings.enabled_builtin_lexicons)
        self.product_tier = settings.product_tier.strip() or "standard"
        self.partial_check = ModernCheckBox("分段转写（每 1 分钟）")
        self.partial_check.setChecked(settings.partial_transcription_enabled)
        self.startup_check.stateChanged.connect(self._toggle_startup)

        self.backend_combo = QComboBox()
        self.backend_combo.addItems([value for value, _label in ENGINE_OPTIONS])
        self.backend_combo.setEditable(False)
        backend_index = self.backend_combo.findText(settings.asr_backend)
        self.backend_combo.setCurrentIndex(max(backend_index, 0))
        self.backend_combo.currentTextChanged.connect(self._handle_toolbar_engine_changed)

        self.language_combo = QComboBox()
        self.language_combo.setMinimumWidth(156)
        for value, label in LANGUAGE_OPTIONS:
            self.language_combo.addItem(label, value)
        language_index = self.language_combo.findData(settings.recognition_language)
        self.language_combo.setCurrentIndex(max(language_index, 0))
        self.language_combo.currentIndexChanged.connect(lambda _value: self._handle_language_combo_changed())

        self.model_input = QLineEdit(settings.whisper_model_path)
        self.executable_input = QLineEdit(settings.whisper_executable)
        self.lexicon_input = QLineEdit(settings.lexicon_path)

    def _build_menus(self) -> None:
        self.copy_action: QAction | None = None
        self.export_word_action: QAction | None = None
        self.export_official_word_action: QAction | None = None
        self.export_minutes_word_action: QAction | None = None
        self.export_pdf_action: QAction | None = None
        self.export_official_pdf_action: QAction | None = None
        self.export_minutes_pdf_action: QAction | None = None

        self.file_menu = self.menuBar().addMenu("文件")
        file_menu = self.file_menu
        import_action = file_menu.addAction("导入音频", self._import_audio)
        import_action.setToolTip(f"选择 {supported_audio_format_names()} 文件进行转写。")
        file_menu.addSeparator()
        self.export_word_action = file_menu.addAction("导出 Word", self._export_word)
        self.export_official_word_action = file_menu.addAction("导出公文 Word", self._export_official_word)
        self.export_minutes_word_action = file_menu.addAction("导出会议纪要 Word", self._export_minutes_word)
        self.export_pdf_action = file_menu.addAction("导出 PDF", self._export_pdf)
        self.export_official_pdf_action = file_menu.addAction("导出公文 PDF", self._export_official_pdf)
        self.export_minutes_pdf_action = file_menu.addAction("导出会议纪要 PDF", self._export_minutes_pdf)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.app.exit_app)

        self.edit_menu = self.menuBar().addMenu("编辑")
        edit_menu = self.edit_menu
        self.copy_action = edit_menu.addAction("复制", self._copy_text)
        edit_menu.addAction("查找替换", self._open_find_replace)
        edit_menu.addAction("清空", self._clear_text)
        edit_action = QAction("启用编辑", self)
        edit_action.setCheckable(True)
        edit_action.setChecked(self.editable_check.isChecked())
        edit_action.triggered.connect(lambda checked: self.editable_check.setChecked(checked))
        self.editable_check.stateChanged.connect(lambda state: edit_action.setChecked(bool(state)))
        edit_menu.addAction(edit_action)

        self.recog_menu = self.menuBar().addMenu("识别")
        recog_menu = self.recog_menu
        recog_menu.addAction("开始录制", self.app.start)
        recog_menu.addAction("结束录制并识别", self.app.stop)
        recog_menu.addAction("暂停/继续录制", self._toggle_pause)

        self.language_menu = self.menuBar().addMenu("语言")
        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions: dict[str, QAction] = {}
        for value, label in LANGUAGE_OPTIONS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(value)
            action.triggered.connect(lambda _checked=False, language=value: self._set_language(language))
            self.language_group.addAction(action)
            self.language_menu.addAction(action)
            self.language_actions[value] = action

        settings_menu = self.menuBar().addMenu("设置")
        settings_menu.addAction("打开设置", self._open_settings)

        help_menu = self.menuBar().addMenu("帮助")
        help_callback = getattr(self.app, "open_help_file", None)
        if not callable(help_callback):
            def show_help_fallback() -> None:
                self.show_info(
                    "使用帮助",
                    "请通过“设置”调整识别、快捷键、词库与托盘选项。",
                )

            help_callback = show_help_fallback
        help_menu.addAction("使用帮助", help_callback)
        health_callback = getattr(self.app, "open_health_report", None)
        if callable(health_callback):
            help_menu.addAction("系统诊断", health_callback)
        log_callback = getattr(self.app, "open_log_file", None)
        if callable(log_callback):
            help_menu.addSeparator()
            help_menu.addAction("打开日志", log_callback)
        self._sync_menu_actions()

    def mainloop(self) -> None:
        self.qt_app.exec()

    def after(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self._callback_dispatcher.requested.emit(int(delay_ms), callback)

    def set_status(self, text: str) -> None:
        self._last_status_text = text
        self.status_label.setText(self._phase_text())
        self.status_label.setToolTip(text)
        if hasattr(self, "bottom_status_label"):
            self.bottom_status_label.setText(text)
        if hasattr(self, "session_state_value"):
            self.session_state_value.setText(self._phase_text())
        if hasattr(self, "footer_mode_label"):
            self.footer_mode_label.setText(self._footer_mode_text())

    def append_text(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        if self.text_area.toPlainText().strip():
            cursor = self.text_area.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_area.setTextCursor(cursor)
            self.text_area.insertPlainText(f"\n\n{cleaned}")
        else:
            self.text_area.setPlainText(cleaned)
        self._sync_text_actions()
        self.text_area.verticalScrollBar().setValue(self.text_area.verticalScrollBar().maximum())

    def begin_streaming_text(self) -> None:
        self._streaming_base_text = self.text_area.toPlainText().strip()
        self._streaming_preview_text = ""

    def set_streaming_text(self, text: str) -> None:
        cleaned = text.strip()
        if self._streaming_base_text is None:
            self.begin_streaming_text()
        base = self._streaming_base_text or ""
        if base and cleaned:
            rendered = f"{base}\n\n{cleaned}"
        else:
            rendered = cleaned or base
        if rendered != self.text_area.toPlainText():
            self.text_area.setPlainText(rendered)
            self.text_area.verticalScrollBar().setValue(self.text_area.verticalScrollBar().maximum())
        self._streaming_preview_text = cleaned
        self._sync_text_actions()

    def commit_streaming_text(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            self.set_streaming_text(cleaned)
        elif self._streaming_base_text is not None:
            self.text_area.setPlainText(self._streaming_base_text)
        self._streaming_base_text = None
        self._streaming_preview_text = ""
        self._sync_text_actions()

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.pause_button.setEnabled(running and getattr(self.app, "is_running", False))
        self.import_button.setEnabled(not running)
        self.start_button.setText("处理中" if running else "开始录制")
        self.stop_button.setText("结束录制")
        self.status_label.setProperty("active", running)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        if hasattr(self, "session_state_value"):
            self.session_state_value.setProperty("active", running)
            self.session_state_value.style().unpolish(self.session_state_value)
            self.session_state_value.style().polish(self.session_state_value)
        if not running:
            self._elapsed_timer.stop()
            self._recording_started_at = None
            self._recording_elapsed_seconds = 0.0
            self._recording_clock_active = False
        self.status_label.setText(self._phase_text())
        self._sync_context_panel()

    def set_recording(self, recording: bool) -> None:
        self.stop_button.setEnabled(recording)
        self.pause_button.setEnabled(recording)
        if recording:
            self._partial_segment_count = 0
            self._recording_elapsed_seconds = 0.0
            self._recording_started_at = time.monotonic()
            self._recording_clock_active = True
            self._elapsed_timer.start()
            self.start_button.setText("录制中")
            self.stop_button.setText("结束录制")
            self.pause_button.setText("暂停")
        elif getattr(self.app, "is_running", False):
            self._pause_recording_clock()
            self._recording_clock_active = False
            self.start_button.setText("正在识别")
            self.stop_button.setText("正在识别")
            self.pause_button.setEnabled(False)
            self._elapsed_timer.stop()
        self.status_label.setText(self._phase_text())
        self._sync_context_panel()

    def set_autostart(self, enabled: bool) -> None:
        self.startup_check.blockSignals(True)
        self.startup_check.setChecked(enabled)
        self.startup_check.blockSignals(False)

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def show_info(self, title: str, message: str) -> None:
        QMessageBox.information(self, title, message)

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.topmost_check.isChecked():
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.show()

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

    def _handle_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.app.show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closing:
            event.accept()
            self.qt_app.quit()
            return
        event.ignore()
        self.app.request_close()

    def _build_layout(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root.setAutoFillBackground(True)
        self.setCentralWidget(root)

        page = QVBoxLayout(root)
        page.setContentsMargins(22, 16, 22, 18)
        page.setSpacing(10)

        toolbar = self._toolbar()
        page.addWidget(toolbar)

        workspace = QHBoxLayout()
        workspace.setSpacing(14)
        page.addLayout(workspace, 1)

        transcript = self._panel("TranscriptPanel")
        transcript_layout = QVBoxLayout(transcript)
        transcript_layout.setContentsMargins(0, 0, 0, 0)
        transcript_layout.setSpacing(0)

        editor_bar = QFrame()
        editor_bar.setObjectName("EditorBar")
        editor_bar_layout = QHBoxLayout(editor_bar)
        editor_bar_layout.setContentsMargins(16, 12, 12, 10)
        editor_bar_layout.setSpacing(10)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.editor_title_label = QLabel("转写稿")
        self.editor_title_label.setObjectName("EditorTitle")
        self.editor_meta_label = QLabel("0 字")
        self.editor_meta_label.setObjectName("EditorMeta")
        title_block.addWidget(self.editor_title_label)
        title_block.addWidget(self.editor_meta_label)
        editor_bar_layout.addLayout(title_block, 1)
        self.copy_button = self._button("复制", "ghost")
        self.copy_button.setToolTip("复制当前转写文本。")
        self.copy_button.clicked.connect(self._copy_text)
        editor_bar_layout.addWidget(self.copy_button)
        self.find_button = self._button("查找", "ghost")
        self.find_button.setToolTip("打开查找替换。")
        self.find_button.clicked.connect(self._open_find_replace)
        editor_bar_layout.addWidget(self.find_button)
        self.clear_button = self._button("清空", "ghost")
        self.clear_button.setToolTip("清空当前转写内容。")
        self.clear_button.clicked.connect(self._clear_text)
        editor_bar_layout.addWidget(self.clear_button)
        self.editable_check.stateChanged.connect(self._toggle_editable)
        self.editable_check.setToolTip("开启后可以直接在转写区编辑文本。")
        editor_bar_layout.addWidget(self.editable_check)
        transcript_layout.addWidget(editor_bar)

        self.text_area = QPlainTextEdit()
        self.text_area.setObjectName("TranscriptText")
        self.text_area.setReadOnly(not self.editable_check.isChecked())
        self.text_area.setPlaceholderText("开始录制或导入音频后，转写内容会出现在这里。")
        self.text_area.textChanged.connect(self._sync_text_actions)
        transcript_layout.addWidget(self.text_area, 1)
        workspace.addWidget(transcript, 1)

        session = self._panel("SessionPanel")
        session.setMinimumWidth(260)
        session.setMaximumWidth(320)
        session_layout = QVBoxLayout(session)
        session_layout.setContentsMargins(16, 16, 16, 16)
        session_layout.setSpacing(10)
        self.session_title_label = QLabel("会话概览")
        self.session_title_label.setObjectName("SidebarTitle")
        session_layout.addWidget(self.session_title_label)

        session_hero = QFrame()
        session_hero.setObjectName("SessionHero")
        hero_layout = QVBoxLayout(session_hero)
        hero_layout.setContentsMargins(14, 12, 14, 12)
        hero_layout.setSpacing(8)
        hero_top = QHBoxLayout()
        hero_top.setSpacing(8)
        hero_caption = QLabel("当前")
        hero_caption.setObjectName("SessionLabel")
        hero_top.addWidget(hero_caption)
        hero_top.addStretch(1)
        self.session_state_value = QLabel("正在初始化")
        self.session_state_value.setObjectName("SessionBadge")
        self.session_state_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_top.addWidget(self.session_state_value)
        hero_layout.addLayout(hero_top)
        self.session_duration_value = QLabel("00:00")
        self.session_duration_value.setObjectName("SessionClock")
        hero_layout.addWidget(self.session_duration_value)
        hero_hint = QLabel("单次录制最长 2 小时")
        hero_hint.setObjectName("SessionHint")
        hero_layout.addWidget(hero_hint)
        session_layout.addWidget(session_hero)

        session_grid = QGridLayout()
        session_grid.setContentsMargins(0, 2, 0, 0)
        session_grid.setHorizontalSpacing(8)
        session_grid.setVerticalSpacing(8)
        self.session_language_value = self._session_tile(
            session_grid,
            0,
            0,
            "语言",
            language_label(str(self.language_combo.currentData() or "zh-Hans")),
        )
        self.session_lexicon_value = self._session_tile(
            session_grid,
            0,
            1,
            "词库",
            "已启用" if self.lexicon_check.isChecked() else "未启用",
        )
        session_layout.addLayout(session_grid)
        session_layout.addStretch(1)
        workspace.addWidget(session, 0)

        footer = QFrame()
        footer.setObjectName("FooterBar")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12, 8, 12, 8)
        footer_layout.setSpacing(10)
        self.status_label = QLabel(self._phase_text(), footer)
        self.status_label.setObjectName("StatusDot")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.hide()
        self.bottom_status_label = QLabel(self._last_status_text)
        self.bottom_status_label.setObjectName("FooterText")
        footer_layout.addWidget(self.bottom_status_label)
        footer_layout.addStretch(1)
        self.footer_mode_label = QLabel(self._footer_mode_text())
        self.footer_mode_label.setObjectName("FooterText")
        footer_layout.addWidget(self.footer_mode_label)
        page.addWidget(footer)

        self._sync_text_actions()
        self._sync_session_panel()

    def _toolbar(self) -> QFrame:
        frame = self._panel("Toolbar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.start_button = self._button("开始录制", "primary")
        self.start_button.setMinimumWidth(118)
        self.start_button.setToolTip("开始录制语音。录制过程中可以暂停，结束后开始识别。")
        self.start_button.clicked.connect(self.app.start)
        layout.addWidget(self.start_button)
        self.pause_button = self._button("暂停", "secondary")
        self.pause_button.setMinimumWidth(76)
        self.pause_button.setToolTip("暂停或继续当前录音。")
        self.pause_button.clicked.connect(self._toggle_pause)
        layout.addWidget(self.pause_button)
        self.stop_button = self._button("结束录制", "secondary")
        self.stop_button.setMinimumWidth(104)
        self.stop_button.setToolTip("结束当前录制并开始转写。单次录制最长 2 小时。")
        self.stop_button.clicked.connect(self.app.stop)
        layout.addWidget(self.stop_button)

        layout.addWidget(self._divider())

        self.import_button = self._button("导入", "secondary")
        self.import_button.setToolTip(f"导入 {supported_audio_format_names()} 文件进行识别。")
        self.import_button.clicked.connect(self._import_audio)
        layout.addWidget(self.import_button)

        layout.addStretch(1)

        self.export_button = self._button("导出", "secondary")
        self.export_menu = QMenu(self)
        self.export_menu.addAction("常规 Word", self._export_word)
        self.export_menu.addAction("公文 Word", self._export_official_word)
        self.export_menu.addAction("会议纪要 Word", self._export_minutes_word)
        self.export_menu.addSeparator()
        self.export_menu.addAction("常规 PDF", self._export_pdf)
        self.export_menu.addAction("公文 PDF", self._export_official_pdf)
        self.export_menu.addAction("会议纪要 PDF", self._export_minutes_pdf)
        self.export_button.setMenu(self.export_menu)
        layout.addWidget(self.export_button)
        self.export_word_button = self.export_button
        self.export_pdf_button = self.export_button
        self.settings_button = self._button("设置", "ghost")
        self.settings_button.setToolTip("打开设置。")
        self.settings_button.clicked.connect(self._open_settings)
        layout.addWidget(self.settings_button)
        return frame

    def _session_row(self, parent_layout: QVBoxLayout, title: str, value: str) -> QLabel:
        frame = QFrame()
        frame.setObjectName("SessionRow")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("SessionLabel")
        value_label = QLabel(value)
        value_label.setObjectName("SessionValue")
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_label.setWordWrap(True)
        layout.addWidget(label, 0)
        layout.addWidget(value_label, 1)
        frame.setLayout(layout)
        parent_layout.addWidget(frame)
        return value_label

    def _session_tile(self, parent_layout: QGridLayout, row: int, column: int, title: str, value: str) -> QLabel:
        frame = QFrame()
        frame.setObjectName("SessionTile")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(4)
        label = QLabel(title)
        label.setObjectName("SessionLabel")
        value_label = QLabel(value)
        value_label.setObjectName("SessionValue")
        value_label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(value_label)
        parent_layout.addWidget(frame, row, column)
        return value_label

    def _divider(self) -> QFrame:
        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedWidth(1)
        return divider

    def _open_settings(self) -> None:
        if self._settings_dialog is None:
            self._settings_dialog = SettingsDialog(self)
        self._sync_settings_engine(self.backend_combo.currentText())
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _open_lexicon_manager(self) -> None:
        if self._lexicon_dialog is None:
            self._lexicon_dialog = LexiconManagerDialog(self)
        self._lexicon_dialog.sync_controls()
        self._lexicon_dialog.sync_summary()
        self._lexicon_dialog.show()
        self._lexicon_dialog.raise_()
        self._lexicon_dialog.activateWindow()

    def _save_settings(self) -> None:
        settings = AppSettings(
            hotkey=self.hotkey_input.text().strip() or DEFAULT_HOTKEY,
            start_minimized=self.start_minimized_check.isChecked(),
            floating_window=self.topmost_check.isChecked(),
            inject_text=self.inject_check.isChecked(),
            enable_hotkey=self.hotkey_check.isChecked(),
            minimize_to_tray=self.tray_check.isChecked(),
            launch_on_startup=self.startup_check.isChecked(),
            show_first_run_hint=self.app.settings.show_first_run_hint,
            asr_backend=self.backend_combo.currentText().strip() or "funasr",
            whisper_model_path=self.model_input.text().strip(),
            whisper_executable=self.executable_input.text().strip() or "whisper-cli",
            recognition_language=str(self.language_combo.currentData() or "zh-Hans"),
            transcript_editable=self.editable_check.isChecked(),
            streaming_transcription_enabled=self.streaming_check.isChecked(),
            streaming_backend=self.app.settings.streaming_backend,
            sherpa_streaming_model_dir=self.app.settings.sherpa_streaming_model_dir,
            funasr_model_root=self.app.settings.funasr_model_root,
            funasr_streaming_model=self.app.settings.funasr_streaming_model,
            funasr_offline_model=self.app.settings.funasr_offline_model,
            funasr_vad_model=self.app.settings.funasr_vad_model,
            funasr_punc_model=self.app.settings.funasr_punc_model,
            streaming_chunk_ms=self.app.settings.streaming_chunk_ms,
            streaming_update_interval_ms=self.app.settings.streaming_update_interval_ms,
            partial_transcription_enabled=self.partial_check.isChecked(),
            partial_transcription_interval_seconds=DEFAULT_PARTIAL_TRANSCRIPTION_INTERVAL_SECONDS,
            lexicon_enabled=self.lexicon_check.isChecked(),
            lexicon_path=self.lexicon_input.text().strip(),
            enabled_builtin_lexicons=tuple(self.enabled_builtin_lexicons),
            product_tier=self.product_tier,
        )
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, settings.floating_window)
        self.show()
        self.app.update_settings(settings)
        self._sync_menu_actions()
        self._sync_context_panel()

    def _sync_settings_engine(self, value: str) -> None:
        if self._settings_dialog is None:
            return
        combo = self._settings_dialog.engine_combo
        index = combo.findText(value)
        if index >= 0 and combo.currentIndex() != index:
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self._settings_dialog.sync_engine_selector(value)

    def _sync_settings_language(self, value: str) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.sync_language_selector(value)

    def _handle_toolbar_engine_changed(self, value: str) -> None:
        self._set_engine(value)

    def _apply_settings_engine(self, value: str) -> None:
        self._set_engine(value)

    def _handle_language_combo_changed(self) -> None:
        self._set_language(str(self.language_combo.currentData() or "zh-Hans"))

    def _set_language(self, value: str) -> None:
        index = self.language_combo.findData(value)
        if index >= 0 and self.language_combo.currentIndex() != index:
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(index)
            self.language_combo.blockSignals(False)
        self._sync_menu_actions()
        self._sync_settings_language(value)
        self._save_settings()

    def _set_engine(self, value: str) -> None:
        index = self.backend_combo.findText(value)
        if index >= 0 and self.backend_combo.currentIndex() != index:
            self.backend_combo.blockSignals(True)
            self.backend_combo.setCurrentIndex(index)
            self.backend_combo.blockSignals(False)
        self._sync_settings_engine(value)
        self._sync_menu_actions()
        self._save_settings()

    def _sync_menu_actions(self) -> None:
        language = str(self.language_combo.currentData() or "zh-Hans")
        if hasattr(self, "language_actions"):
            for value, action in self.language_actions.items():
                action.blockSignals(True)
                action.setChecked(value == language)
                action.blockSignals(False)
            self.language_menu.setTitle("语言")

    def _toggle_startup(self) -> None:
        self.app.set_startup_enabled(self.startup_check.isChecked())

    def _clear_text(self) -> None:
        self.text_area.clear()
        self._sync_text_actions()

    def _copy_text(self) -> None:
        text = self.text_area.toPlainText()
        if text:
            QApplication.clipboard().setText(text)

    def _toggle_editable(self) -> None:
        self.text_area.setReadOnly(not self.editable_check.isChecked())
        self.text_area.setFocus()
        self._save_settings()

    def _toggle_pause(self) -> None:
        if not getattr(self.app, "is_running", False):
            return
        if self.app.toggle_recording_pause():
            paused = getattr(self.app, "is_recording_paused", False)
            if paused:
                self._pause_recording_clock()
            else:
                self._resume_recording_clock()
            self.pause_button.setText("继续" if paused else "暂停")
            self._sync_context_panel()

    def _sync_text_actions(self) -> None:
        text = self.text_area.toPlainText()
        has_text = bool(text.strip())
        self.copy_button.setEnabled(has_text)
        self.find_button.setEnabled(has_text)
        self.clear_button.setEnabled(has_text)
        self.export_button.setEnabled(has_text)
        if hasattr(self, "editor_meta_label"):
            self.editor_meta_label.setText(f"{len(text.strip())} 字" if has_text else "0 字")
        if hasattr(self, "footer_mode_label"):
            self.footer_mode_label.setText(self._footer_mode_text())
        for action in (
            getattr(self, "copy_action", None),
            getattr(self, "export_word_action", None),
            getattr(self, "export_official_word_action", None),
            getattr(self, "export_minutes_word_action", None),
            getattr(self, "export_pdf_action", None),
            getattr(self, "export_official_pdf_action", None),
            getattr(self, "export_minutes_pdf_action", None),
        ):
            if action is not None:
                action.setEnabled(has_text)

    def _sync_context_panel(self) -> None:
        self._sync_session_panel()

    def _sync_session_panel(self) -> None:
        self._sync_recording_clock_pause_state()
        if hasattr(self, "session_duration_value"):
            self.session_duration_value.setText(self._elapsed_text())
        if hasattr(self, "session_state_value"):
            self.session_state_value.setText(self._phase_text())
        if hasattr(self, "session_language_value"):
            self.session_language_value.setText(language_label(str(self.language_combo.currentData() or "zh-Hans")))
        if hasattr(self, "session_mode_value"):
            self.session_mode_value.setText(self._recognition_mode_text())
        if hasattr(self, "session_lexicon_value"):
            self.session_lexicon_value.setText("已启用" if self.lexicon_check.isChecked() else "未启用")
        if hasattr(self, "footer_mode_label"):
            self.footer_mode_label.setText(self._footer_mode_text())
        if hasattr(self, "lexicon_summary_label"):
            self.lexicon_summary_label.setText(self._lexicon_summary_text())
        if hasattr(self, "lexicon_effect_label"):
            self.lexicon_effect_label.setText(self._lexicon_effect_text())
        if self._lexicon_dialog is not None:
            self._lexicon_dialog.sync_summary()

    def _open_find_replace(self) -> None:
        if self._find_dialog is None:
            self._find_dialog = FindReplaceDialog(self)
        self._find_dialog.show()
        self._find_dialog.raise_()
        self._find_dialog.activateWindow()

    def _find_text(self, query: str) -> bool:
        if not query:
            return False
        document = self.text_area.document()
        cursor = document.find(query, self.text_area.textCursor())
        if cursor.isNull():
            cursor = document.find(query)
        if cursor.isNull():
            return False
        self.text_area.setTextCursor(cursor)
        self.text_area.setFocus()
        return True

    def _replace_current_text(self, query: str, replacement: str) -> bool:
        if not query:
            return False
        cursor = self.text_area.textCursor()
        if not cursor.hasSelection() or cursor.selectedText() != query:
            if not self._find_text(query):
                return False
            cursor = self.text_area.textCursor()
        cursor.insertText(replacement)
        self._sync_text_actions()
        return True

    def _replace_all_text(self, query: str, replacement: str) -> int:
        if not query:
            return 0
        text = self.text_area.toPlainText()
        count = text.count(query)
        if not count:
            return 0
        self.text_area.setPlainText(text.replace(query, replacement))
        self._sync_text_actions()
        return count

    def _import_audio(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "导入音频文件",
            "",
            f"Audio files ({' '.join(f'*{extension}' for extension in SUPPORTED_AUDIO_FILE_EXTENSIONS)});;All files (*.*)",
        )
        if path:
            self.app.transcribe_audio_file(Path(path))

    def _export_word(self) -> None:
        self._export_docx(export_transcript_docx, "导出 Word 文档", "识别结果.docx")

    def _export_official_word(self) -> None:
        self._export_docx(export_official_document_docx, "导出公文 Word", "公文转写.docx")

    def _export_minutes_word(self) -> None:
        self._export_docx(export_meeting_minutes_docx, "导出会议纪要 Word", "会议纪要.docx")

    def _export_docx(self, exporter: Callable[..., None], title: str, default_name: str) -> None:
        text = self.text_area.toPlainText().strip()
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
        self.show_info("导出完成", f"文档已保存：\n{path}")

    def _export_pdf(self) -> None:
        self._export_pdf_document("导出 PDF", "识别结果.pdf", self._regular_pdf_html)

    def _export_official_pdf(self) -> None:
        self._export_pdf_document("导出公文 PDF", "公文转写.pdf", self._official_pdf_html)

    def _export_minutes_pdf(self) -> None:
        self._export_pdf_document("导出会议纪要 PDF", "会议纪要.pdf", self._minutes_pdf_html)

    def _export_pdf_document(self, title: str, default_name: str, html_factory: Callable[[str], str]) -> None:
        text = self.text_area.toPlainText().strip()
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
            document.setDefaultFont(QFont("Microsoft YaHei UI", 10))
            document.setHtml(html_factory(text))
            document.print_(printer)
        except Exception as exc:
            self.show_error("导出 PDF 失败", str(exc))
            return
        self.show_info("导出完成", f"PDF 已保存：\n{path}")

    def _regular_pdf_html(self, text: str) -> str:
        body = "".join(f"<p>{escape_html(paragraph)}</p>" for paragraph in text.splitlines() or [""])
        return f"""
<html><head><style>
body {{ font-family: "Microsoft YaHei UI", sans-serif; color: #1d2939; margin: 42px; }}
h1 {{ font-size: 22px; margin: 0 0 24px; }}
p {{ font-size: 13px; line-height: 1.75; margin: 0 0 10px; }}
</style></head><body><h1>识别结果</h1>{body}</body></html>
"""

    def _official_pdf_html(self, text: str) -> str:
        paragraphs = "".join(f"<p class='body'>{escape_html(paragraph)}</p>" for paragraph in text.splitlines() if paragraph.strip())
        return f"""
<html><head><style>
body {{ font-family: "FangSong", "仿宋_GB2312", serif; color: #111827; margin: 56px 52px; }}
.red {{ color: #b42318; text-align: center; font-size: 18px; letter-spacing: 2px; border-bottom: 2px solid #b42318; padding-bottom: 8px; }}
h1 {{ font-family: "SimHei", "黑体", sans-serif; text-align: center; font-size: 24px; margin: 28px 0 20px; }}
.meta {{ font-size: 14px; line-height: 1.9; margin: 0; }}
.body {{ font-size: 15px; line-height: 2.05; text-indent: 2em; margin: 0 0 8px; }}
.tail {{ border-top: 1px solid #d0d5dd; margin-top: 28px; padding-top: 10px; font-size: 13px; }}
</style></head><body>
<div class="red">语音转文字</div>
<h1>公文转写</h1>
<p class="meta">发文字号：〔    〕  号</p>
<p class="meta">签发人：</p>
<p class="meta">主送：</p>
{paragraphs}
<div class="tail">附件：<br/>联系人：　　　　联系电话：<br/>抄送：</div>
</body></html>
"""

    def _minutes_pdf_html(self, text: str) -> str:
        paragraphs = [paragraph.strip() for paragraph in text.splitlines() if paragraph.strip()]
        topic = escape_html(paragraphs[0]) if paragraphs else "（请补充会议议题）"
        content = "".join(f"<p>{escape_html(paragraph)}</p>" for paragraph in paragraphs)
        return f"""
<html><head><style>
body {{ font-family: "Microsoft YaHei UI", "SimSun", sans-serif; color: #1d2939; margin: 48px; }}
h1 {{ text-align: center; font-size: 22px; margin: 0 0 22px; }}
.meta {{ font-size: 13px; line-height: 1.8; margin: 0; }}
h2 {{ font-size: 15px; margin: 20px 0 8px; }}
p {{ font-size: 13px; line-height: 1.8; margin: 0 0 8px; }}
.action {{ background: #f2f4f7; padding: 8px 10px; border-left: 3px solid #2563eb; }}
</style></head><body>
<h1>会议纪要</h1>
<p class="meta">会议时间：</p>
<p class="meta">会议地点：</p>
<p class="meta">主持人：　　　　记录人：</p>
<p class="meta">参会人员：</p>
<h2>一、会议议题</h2>
<p>{topic}</p>
<h2>二、会议内容</h2>
{content}
<h2>三、决议事项</h2>
<p class="action">1. </p>
<h2>四、待办跟进</h2>
<p class="action">责任人：　　　　截止时间：　　　　事项：</p>
</body></html>
"""

    def _select_model_path(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(self, "选择 whisper.cpp 模型", "", "Model files (*.bin *.gguf);;All files (*.*)")
        if path:
            self.model_input.setText(path)
            self._save_settings()

    def _select_executable_path(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(self, "选择 whisper.cpp 执行文件", "", "Executable files (*.exe);;All files (*.*)")
        if path:
            self.executable_input.setText(path)
            self._save_settings()

    def _select_lexicon_path(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(self, "选择词库文件", "", "Lexicon files (*.txt *.json *.csv);;All files (*.*)")
        if path:
            self.lexicon_input.setText(path)
            self.lexicon_check.setChecked(True)
            self._save_settings()
            if self._lexicon_dialog is not None:
                self._lexicon_dialog.sync_controls()
                self._lexicon_dialog.sync_summary()

    def _panel(self, name: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName(name)
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return frame

    def _button(self, text: str, variant: str) -> QPushButton:
        button = QPushButton(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setProperty("variant", variant)
        heights = {"primary": 40, "secondary": 38, "ghost": 34, "nav": 38, "select": 38}
        button.setMinimumHeight(heights.get(variant, 38))
        button.setIconSize(QSize(16, 16))
        return button

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        return label

    def set_partial_progress(self, segment_count: int) -> None:
        self._partial_segment_count = max(0, segment_count)
        self._sync_context_panel()

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _load_window_icon(self) -> QIcon:
        icon = bundled_app_icon()
        if icon.is_file():
            loaded_icon = QIcon(str(icon))
            if not loaded_icon.isNull():
                return loaded_icon
        return self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)

    def _runtime_summary(self) -> str:
        language = language_label(str(self.language_combo.currentData() or "zh-Hans"))
        if self.streaming_check.isChecked():
            live = "实时流式输出"
        elif self.partial_check.isChecked():
            live = "分段转写"
        else:
            live = "结束后转写"
        return f"{language} · {live} · 最长 {_format_duration(DEFAULT_MAX_RECORDING_SECONDS)}"

    def _recognition_mode_text(self) -> str:
        if self.streaming_check.isChecked():
            return "实时流式"
        if self.partial_check.isChecked():
            if self._partial_segment_count:
                return f"每分钟转写 · {self._partial_segment_count} 段"
            return "每分钟转写"
        return "结束后转写"

    def _footer_mode_text(self) -> str:
        text = self.text_area.toPlainText().strip() if hasattr(self, "text_area") else ""
        if self._streaming_preview_text:
            return f"实时更新 · {len(self._streaming_preview_text)} 字"
        if self.partial_check.isChecked():
            if self._partial_segment_count:
                return f"已分段 {self._partial_segment_count} 段"
            return "分段待命"
        if text:
            return f"已写入 {len(text)} 字"
        if self.lexicon_check.isChecked():
            return "词库优化已启用"
        return "等待录制"

    def _phase_text(self) -> str:
        if getattr(self.app, "is_recording_paused", False):
            return "暂停"
        if self._recording_clock_active:
            return "录制"
        if getattr(self.app, "is_running", False):
            return "转写"
        return "就绪"

    def _elapsed_text(self) -> str:
        elapsed = self._recording_elapsed_seconds
        if self._recording_started_at is not None:
            elapsed += max(0.0, time.monotonic() - self._recording_started_at)
        if elapsed <= 0:
            return "00:00"
        elapsed = max(0, int(elapsed))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _pause_recording_clock(self) -> None:
        if self._recording_started_at is None:
            return
        self._recording_elapsed_seconds += max(0.0, time.monotonic() - self._recording_started_at)
        self._recording_started_at = None

    def _resume_recording_clock(self) -> None:
        if not self._recording_clock_active or self._recording_started_at is not None:
            return
        self._recording_started_at = time.monotonic()
        if not self._elapsed_timer.isActive():
            self._elapsed_timer.start()

    def _sync_recording_clock_pause_state(self) -> None:
        if not self._recording_clock_active:
            return
        if getattr(self.app, "is_recording_paused", False):
            self._pause_recording_clock()
        else:
            self._resume_recording_clock()

    def _partial_mode_text(self) -> str:
        if self.streaming_check.isChecked():
            return "实时流式 · 约 100ms"
        if self.partial_check.isChecked():
            return "分段转写 · 每分钟"
        return "关闭"

    def _partial_progress_text(self) -> str:
        if self.streaming_check.isChecked():
            return "实时输出"
        if not self.partial_check.isChecked():
            return "未启用"
        if self._partial_segment_count:
            return f"已完成 {self._partial_segment_count} 段"
        return "等待首段"

    def _lexicon_summary_text(self) -> str:
        if not self.lexicon_check.isChecked():
            return "未启用"
        builtin_count = len(self.enabled_builtin_lexicons)
        custom = "已叠加自定义" if self.lexicon_input.text().strip() else "未叠加自定义"
        return f"内置 {builtin_count} 套 · {custom}"

    def _custom_lexicon_text(self) -> str:
        path = self.lexicon_input.text().strip()
        if not path:
            return "未导入自定义词库。支持 TXT、JSON、CSV。"
        return f"已导入：{Path(path).name}"

    def _lexicon_effect_text(self) -> str:
        if not self.lexicon_check.isChecked():
            return "词库优化关闭：识别结果只做语言规范化。"
        selected = [
            item.title
            for item in bundled_lexicon_catalog(product_tier=self.product_tier)
            if item.id in self.enabled_builtin_lexicons
        ]
        if not selected and not self.lexicon_input.text().strip():
            return "词库优化已开启，但当前没有启用任何词库。"
        parts = []
        if selected:
            parts.append("内置：" + "、".join(selected))
        if self.lexicon_input.text().strip():
            parts.append("自定义：" + Path(self.lexicon_input.text().strip()).name)
        return "流式识别时实时纠错，识别完成后再次整理术语：" + "；".join(parts)


STYLE_SHEET = """
QWidget#Root {
    background: #edf3f8;
    color: #1d2939;
}

QMenuBar {
    background: #f6f9fc;
    color: #344054;
    padding: 2px 8px;
}

QMenuBar::item:selected,
QMenu::item:selected {
    background: #eaf2ff;
    color: #175cd3;
}

QMenu {
    background: #ffffff;
    border: 1px solid #d9e2ef;
    padding: 6px;
}

QLabel#MutedText,
QLabel#FieldLabel,
QLabel#FooterText,
QLabel#EditorMeta,
QLabel#SessionLabel {
    color: #667085;
    font-size: 13px;
}

QLabel#FieldLabel {
    font-weight: 700;
}

QLabel#SectionTitle {
    color: #101828;
    font-size: 17px;
    font-weight: 800;
}

QLabel#EditorTitle,
QLabel#SidebarTitle {
    color: #101828;
    font-size: 16px;
    font-weight: 800;
}

QLabel#StatusDot {
    background: #eaf2ff;
    color: #175cd3;
    border: 1px solid #c7d7fe;
    border-radius: 12px;
    padding: 4px 10px;
    font-weight: 800;
    font-size: 12px;
}

QLabel#StatusDot[active="true"] {
    background: #ecfdf3;
    color: #027a48;
    border-color: #abefc6;
}

QLabel#SessionBadge {
    background: #eef4ff;
    color: #175cd3;
    border: 1px solid #c7d7fe;
    border-radius: 11px;
    padding: 3px 9px;
    font-size: 12px;
    font-weight: 800;
}

QLabel#SessionBadge[active="true"] {
    background: #ecfdf3;
    color: #027a48;
    border-color: #abefc6;
}

QLabel#SessionClock {
    color: #101828;
    font-size: 28px;
    font-weight: 900;
}

QLabel#SessionHint {
    color: #667085;
    font-size: 12px;
}

QFrame#Toolbar,
QFrame#TranscriptPanel,
QFrame#SessionPanel,
QFrame#FooterBar,
QWidget#DialogPage {
    background: #ffffff;
    border: 1px solid #d9e4ef;
    border-radius: 8px;
}

QFrame#Toolbar {
    background: #fdfefe;
}

QFrame#EditorBar {
    background: #ffffff;
    border-bottom: 1px solid #e5edf6;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}

QFrame#SessionPanel {
    background: #f7fbff;
}

QFrame#SessionHero {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1, stop: 0 #ffffff, stop: 1 #edf5ff);
    border: 1px solid #d8e7fb;
    border-radius: 8px;
}

QFrame#FooterBar {
    background: transparent;
    border: 0;
}

QFrame#SessionRow {
    background: transparent;
    border: 0;
    border-bottom: 1px solid #e6edf5;
}

QFrame#SessionTile {
    background: #ffffff;
    border: 1px solid #e2ebf6;
    border-radius: 8px;
}

QFrame#SettingRow {
    background: #f8fafc;
    border: 1px solid #e8eef7;
    border-radius: 8px;
}

QLabel#SettingTitle {
    color: #344054;
    font-size: 14px;
    font-weight: 700;
}

QLabel#SessionValue {
    color: #182230;
    font-size: 14px;
    font-weight: 800;
}

QFrame#Divider {
    background: #dde5f0;
    border: 0;
}

QPlainTextEdit#TranscriptText {
    background: #ffffff;
    color: #182230;
    border: 0;
    border-bottom-left-radius: 8px;
    border-bottom-right-radius: 8px;
    padding: 16px;
    font-size: 17px;
    selection-background-color: #c7d7fe;
}

QLineEdit,
QComboBox {
    background: #fbfdff;
    color: #182230;
    border: 1px solid #d0d9e6;
    border-radius: 8px;
    padding: 7px 11px;
    min-height: 23px;
}

QComboBox {
    padding-right: 34px;
    selection-background-color: #eaf2ff;
    selection-color: #175cd3;
}

QComboBox::drop-down {
    background: #eef4ff;
    border: 0;
    border-left: 1px solid #d8e3f5;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    width: 32px;
}

QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #2f6fed;
}

QComboBox QAbstractItemView {
    background: #ffffff;
    color: #182230;
    border: 1px solid #c7d7fe;
    border-radius: 8px;
    padding: 6px;
    outline: 0;
    selection-background-color: #eaf2ff;
    selection-color: #175cd3;
}

QLineEdit:focus,
QComboBox:focus {
    border: 1px solid #2f6fed;
}

QPlainTextEdit#TranscriptText:focus {
    border: 0;
}

QPushButton {
    border: 0;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 700;
}

QPushButton[variant="primary"] {
    background: #2563eb;
    color: white;
}

QPushButton[variant="primary"]:hover {
    background: #1d4ed8;
}

QPushButton[variant="primary"]:disabled {
    background: #b8c7e6;
    color: #eef2f7;
}

QPushButton[variant="secondary"] {
    background: #eef4ff;
    color: #175cd3;
}

QPushButton[variant="secondary"]:hover {
    background: #dbeafe;
}

QPushButton[variant="select"] {
    background: #ffffff;
    color: #182230;
    border: 1px solid #d0d9e6;
    text-align: left;
    padding: 8px 12px;
}

QPushButton[variant="select"]:hover {
    background: #f8fbff;
    border-color: #adc8ff;
    color: #175cd3;
}

QPushButton[variant="select"]::menu-indicator {
    image: none;
    width: 0;
}

QPushButton[variant="secondary"]:disabled,
QPushButton[variant="ghost"]:disabled {
    background: #f2f4f7;
    color: #98a2b3;
}

QPushButton[variant="ghost"] {
    background: transparent;
    color: #667085;
}

QPushButton[variant="ghost"]:hover {
    background: #f2f4f7;
    color: #344054;
}

QPushButton[variant="nav"] {
    background: transparent;
    color: #475467;
    text-align: left;
}

QPushButton[variant="nav"]:checked {
    background: #eaf2ff;
    color: #175cd3;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px;
}

QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 48px;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
"""
