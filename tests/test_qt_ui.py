from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QLabel, QPushButton

from voice_input.qt_ui import QtVoiceInputWindow
from voice_input.settings import AppSettings


class FakeApp:
    def __init__(self, settings=None):
        self.settings = settings or AppSettings()
        self.started = 0
        self.stopped = 0
        self.toggled = 0
        self.saved = None
        self.requested_close = 0
        self.opened_log = 0
        self.opened_help = 0
        self.opened_health = 0
        self.autostart = []
        self.exited = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def toggle(self):
        self.toggled += 1

    def update_settings(self, settings):
        self.saved = settings

    def request_close(self):
        self.requested_close += 1

    def open_log_file(self):
        self.opened_log += 1

    def open_help_file(self):
        self.opened_help += 1

    def open_health_report(self):
        self.opened_health += 1

    def set_startup_enabled(self, enabled):
        self.autostart.append(enabled)

    def exit_app(self):
        self.exited += 1


def test_qt_window_updates_button_state_and_appends_text():
    window = QtVoiceInputWindow(FakeApp())

    assert window.windowTitle() == "语音转文字"
    assert window.start_button.text() == "开始录制"
    assert window.stop_button.text() == "结束录制"
    assert window.backend_combo.isEditable() is False
    assert window.language_combo.parent() is None
    assert window.backend_combo.parent() is None
    assert window.backend_combo.currentText() == "funasr"
    assert window.language_menu.title() == "语言"
    assert not hasattr(window, "engine_menu")
    menu_titles = [action.text() for action in window.menuBar().actions()]
    assert "引擎" not in menu_titles
    assert "设置" in menu_titles
    assert menu_titles[-1] == "帮助"

    window.set_running(True)
    assert window.start_button.isEnabled() is False
    assert window.stop_button.isEnabled() is True
    assert window.import_button.isEnabled() is False
    assert window.start_button.text() == "处理中"
    assert window.stop_button.text() == "结束录制"

    window.set_recording(True)
    assert window.start_button.text() == "录制中"
    assert window.stop_button.text() == "结束录制"
    assert window.status_label.text() == "录制"

    window.set_running(False)
    assert window.start_button.isEnabled() is True
    assert window.stop_button.isEnabled() is False
    assert window.import_button.isEnabled() is True
    assert window.start_button.text() == "开始录制"
    assert window.stop_button.text() == "结束录制"

    assert window.copy_button.isEnabled() is False
    assert window.export_word_button.isEnabled() is False
    assert window.export_pdf_button.isEnabled() is False
    assert window.find_button.isEnabled() is False
    assert window.clear_button.isEnabled() is False
    window.append_text("hello")
    assert window.copy_button.isEnabled() is True
    assert window.export_word_button.isEnabled() is True
    assert window.export_pdf_button.isEnabled() is True
    assert window.find_button.isEnabled() is True
    assert window.clear_button.isEnabled() is True
    window.append_text("world")
    assert window.text_area.toPlainText() == "hello\n\nworld"
    assert window.editor_meta_label.text().endswith("字")
    assert window.editor_meta_label.text() != "0 字"
    assert window.footer_mode_label.text().startswith("已写入")
    window._clear_text()
    assert window.copy_button.isEnabled() is False
    assert window.footer_mode_label.text() == "词库优化已启用"
    window.close_window()


def test_help_menu_opens_help_not_log():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    help_action = window.menuBar().actions()[-1]
    help_menu = help_action.menu()
    assert help_menu is not None
    assert [action.text() for action in help_menu.actions() if not action.isSeparator()] == ["使用帮助", "系统诊断", "打开日志"]

    help_menu.actions()[0].trigger()
    help_menu.actions()[1].trigger()

    assert app.opened_help == 1
    assert app.opened_health == 1
    assert app.opened_log == 0
    window.close_window()


def test_qt_window_saves_settings():
    app = FakeApp()
    window = QtVoiceInputWindow(app)

    window.hotkey_input.setText("Ctrl+Shift+Space")
    window.topmost_check.setChecked(False)
    window.inject_check.setChecked(True)
    window.hotkey_check.setChecked(False)
    window.tray_check.setChecked(False)
    window.startup_check.setChecked(True)
    window.start_minimized_check.setChecked(True)
    window.editable_check.setChecked(True)
    window.streaming_check.setChecked(False)
    window.partial_check.setChecked(True)
    window._set_engine("funasr")
    window._set_language("en")
    window.model_input.setText("models/ggml-tiny.bin")
    window.executable_input.setText("bin/whisper-cli")
    window._save_settings()

    assert app.saved.hotkey == "Ctrl+Shift+Space"
    assert app.saved.start_minimized is True
    assert app.saved.floating_window is False
    assert app.saved.inject_text is True
    assert app.saved.enable_hotkey is False
    assert app.saved.minimize_to_tray is False
    assert app.saved.launch_on_startup is True
    assert app.saved.asr_backend == "funasr"
    assert app.saved.recognition_language == "en"
    assert app.saved.transcript_editable is True
    assert app.saved.streaming_transcription_enabled is False
    assert app.saved.partial_transcription_enabled is True
    assert app.saved.enabled_builtin_lexicons == window.enabled_builtin_lexicons
    assert app.saved.whisper_model_path == "models/ggml-tiny.bin"
    assert app.saved.whisper_executable == "bin/whisper-cli"
    assert app.autostart == [True]
    window.close_window()


def test_qt_after_dispatches_callbacks_from_worker_thread():
    window = QtVoiceInputWindow(FakeApp())
    called = threading.Event()

    worker = threading.Thread(target=lambda: window.after(0, called.set))
    worker.start()
    worker.join(timeout=1)

    deadline = time.monotonic() + 2.0
    while not called.is_set() and time.monotonic() < deadline:
        window.qt_app.processEvents()
        time.sleep(0.01)

    assert called.is_set()
    window.close_window()


def test_streaming_text_replaces_live_candidate_until_final_commit():
    window = QtVoiceInputWindow(FakeApp())
    window.append_text("已有内容")

    window.begin_streaming_text()
    window.set_streaming_text("你")
    assert window.footer_mode_label.text() == "实时更新 · 1 字"
    window.set_streaming_text("你好")
    assert window.footer_mode_label.text() == "实时更新 · 2 字"

    assert window.text_area.toPlainText() == "已有内容\n\n你好"

    window.commit_streaming_text("你好世界")

    assert window.text_area.toPlainText() == "已有内容\n\n你好世界"
    assert window.footer_mode_label.text().startswith("已写入")
    window.close_window()


def test_settings_dialog_keeps_state_engine_combo_in_place():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    state_parent = window.backend_combo.parent()

    window._open_settings()

    assert window.backend_combo.parent() is state_parent
    assert window._settings_dialog is not None
    assert window._settings_dialog.engine_combo is not window.backend_combo
    assert window._settings_dialog.engine_combo.parent() is None
    assert window._settings_dialog.engine_select_button is None
    assert [window._settings_dialog.engine_combo.itemText(index) for index in range(window._settings_dialog.engine_combo.count())] == [
        "funasr"
    ]
    assert window.backend_combo.currentText() == "funasr"
    window.close_window()


def test_language_is_selected_from_menu_actions():
    app = FakeApp()
    window = QtVoiceInputWindow(app)

    window.language_actions["en"].trigger()
    assert window.language_combo.currentData() == "en"
    assert window.language_actions["en"].isChecked()
    assert window.language_menu.title() == "语言"
    assert app.saved.recognition_language == "en"

    assert not hasattr(window, "engine_actions")
    window.close_window()


def test_find_replace_tools_operate_on_transcript_text():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    window.append_text("会议开始")
    window.append_text("会议结束")

    assert window._find_text("会议") is True
    assert window._replace_current_text("会议", "记录") is True
    assert window.text_area.toPlainText().startswith("记录开始")

    assert window._replace_all_text("会议", "纪要") == 1
    assert window.text_area.toPlainText() == "记录开始\n\n纪要结束"

    window._open_find_replace()
    assert window._find_dialog is not None
    window.close_window()


def test_export_menu_exposes_meeting_minutes_template():
    window = QtVoiceInputWindow(FakeApp())

    assert window.menuBar().actions()[0].text() == "文件"
    assert "导出会议纪要 Word" in [action.text() for action in window.file_menu.actions()]
    assert "导出会议纪要 PDF" in [action.text() for action in window.file_menu.actions()]
    assert "会议纪要 Word" in [action.text() for action in window.export_menu.actions()]
    assert "会议纪要 PDF" in [action.text() for action in window.export_menu.actions()]
    assert "公文 PDF" in [action.text() for action in window.export_menu.actions()]
    window.close_window()


def test_pdf_export_templates_are_distinct():
    window = QtVoiceInputWindow(FakeApp())

    official = window._official_pdf_html("第一段\n第二段")
    minutes = window._minutes_pdf_html("议题一\n讨论内容")

    assert "发文字号" in official
    assert "主送" in official
    assert "附件" in official
    assert "会议时间" in minutes
    assert "决议事项" in minutes
    assert "待办跟进" in minutes
    assert official != minutes
    window.close_window()


def test_partial_transcription_setting_is_hidden_but_preserved_when_saved():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    window._open_settings()

    window.streaming_check.setChecked(False)
    window.partial_check.setChecked(True)
    window._save_settings()

    assert app.saved.partial_transcription_enabled is True
    assert window.partial_check.text() == "分段转写（每 1 分钟）"
    assert not hasattr(window, "session_mode_value")
    assert window.footer_mode_label.text() == "分段待命"
    window.set_partial_progress(2)
    assert window.footer_mode_label.text() == "已分段 2 段"
    window.close_window()


def test_settings_dialog_removes_redundant_recognition_language_page():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    window._open_settings()

    dialog = window._settings_dialog
    assert dialog is not None
    assert window.language_combo.parent() is None
    assert window.backend_combo.parent() is None
    assert not hasattr(window, "tier_combo")
    assert dialog.engine_combo.parent() is None
    assert dialog.engine_select_button is None
    assert dialog.language_select_button is None
    assert [button.text() for button in dialog.nav_buttons] == ["常规", "快捷键", "词库"]
    assert "识别" not in [button.text() for button in dialog.nav_buttons]
    assert window.language_menu.title() == "语言"
    assert window.language_actions
    window.close_window()


def test_lexicon_manager_controls_builtin_sets_without_exposing_product_tier():
    app = FakeApp()
    window = QtVoiceInputWindow(app)
    window._open_lexicon_manager()

    dialog = window._lexicon_dialog
    assert dialog is not None
    assert set(dialog.builtin_checks) == {
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    }
    assert not hasattr(dialog, "tier_select_button")
    assert "产品档位" not in [label.text() for label in dialog.findChildren(QLabel)]

    dialog.lexicon_enabled_check.setChecked(True)
    dialog.builtin_checks["official_document"].setChecked(False)

    assert app.saved.product_tier == "enterprise"
    assert app.saved.lexicon_enabled is True
    assert "official_document" not in app.saved.enabled_builtin_lexicons
    assert "4" in window._lexicon_summary_text()
    assert "实时纠错" in window._lexicon_effect_text()
    assert dialog.summary_label.text() == window._lexicon_effect_text()
    window.close_window()


def test_lexicon_manager_filters_builtin_sets_by_packaged_tier():
    app = FakeApp(
        AppSettings(
            product_tier="basic",
            enabled_builtin_lexicons=(
                "government_digital",
                "official_document",
                "office_meeting",
                "finance_budget",
                "project_engineering",
            ),
        )
    )
    window = QtVoiceInputWindow(app)
    window._open_lexicon_manager()

    dialog = window._lexicon_dialog
    assert dialog is not None
    assert set(dialog.builtin_checks) == {
        "government_digital",
        "official_document",
        "office_meeting",
        "finance_budget",
        "project_engineering",
    }
    window.close_window()


def test_recording_clock_excludes_paused_time(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("voice_input.qt_ui.time.monotonic", lambda: now[0])
    app = FakeApp()
    app.is_running = True
    app.is_recording_paused = False
    window = QtVoiceInputWindow(app)

    app.started = 1
    window.set_running(True)
    window.set_recording(True)
    now[0] += 5
    window._sync_session_panel()
    assert window.session_duration_value.text() == "00:05"

    app.is_recording_paused = True
    window._sync_session_panel()
    now[0] += 20
    window._sync_session_panel()
    assert window.session_duration_value.text() == "00:05"

    app.is_recording_paused = False
    window._sync_session_panel()
    now[0] += 7
    window._sync_session_panel()
    assert window.session_duration_value.text() == "00:12"
    window.close_window()


def test_lexicon_dialog_does_not_steal_settings_controls():
    window = QtVoiceInputWindow(FakeApp())

    window._open_settings()
    settings_check_parent = window.lexicon_check.parent()
    settings_path_parent = window.lexicon_input.parent()
    window._open_lexicon_manager()

    assert window.lexicon_check.parent() is settings_check_parent
    assert window.lexicon_input.parent() is settings_path_parent
    assert window._lexicon_dialog is not None
    assert window._lexicon_dialog.lexicon_enabled_check is not window.lexicon_check
    assert window._lexicon_dialog.lexicon_path_input is not window.lexicon_input
    window.close_window()


def test_runtime_workbench_avoids_duplicate_settings_and_status_text():
    window = QtVoiceInputWindow(FakeApp())

    visible_settings_buttons = [
        button for button in window.findChildren(QPushButton) if button.text() == "设置" and button.isVisible()
    ]
    assert visible_settings_buttons == [window.settings_button]

    window.set_status("正在录制，请继续讲话")

    assert window.status_label.isVisible() is False
    assert window.status_label.text() != window.bottom_status_label.text()
    assert window.session_state_value.text() == window.status_label.text()
    assert window.bottom_status_label.text() == "正在录制，请继续讲话"
    assert window.footer_mode_label.text() == "词库优化已启用"
    window.close_window()


def test_footer_surfaces_lexicon_state_when_empty():
    window = QtVoiceInputWindow(FakeApp())

    window.lexicon_check.setChecked(True)
    window._sync_context_panel()

    assert window.footer_mode_label.text() == "词库优化已启用"
    window.close_window()
