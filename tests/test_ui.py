from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from voice_input.settings import AppSettings
from voice_input.ui import TkVoiceInputWindow


@dataclass
class DummyText:
    value: str = ""
    state: str = "disabled"
    yscrollcommand: object | None = None

    def grid(self, *_args, **_kwargs):
        return None

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "yscrollcommand" in kwargs:
            self.yscrollcommand = kwargs["yscrollcommand"]

    def index(self, _marker):
        return "1.0" if not self.value else "2.0"

    def insert(self, _where, text):
        self.value += text

    def see(self, _where):
        return None

    def delete(self, *_args):
        self.value = ""

    def yview(self):
        return None


class DummyButton:
    def __init__(self, text=""):
        self.text = text
        self.disabled = False

    def grid(self, *_args, **_kwargs):
        return None

    def state(self, flags):
        if "disabled" in flags:
            self.disabled = True
        if "!disabled" in flags:
            self.disabled = False

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]

    def set_selected(self, _selected):
        return None


class DummyStyle:
    def theme_names(self):
        return []

    def theme_use(self, *_args):
        return None

    def configure(self, *_args, **_kwargs):
        return None

    def map(self, *_args, **_kwargs):
        return None


def dummy_widget(*_args, **_kwargs):
    return SimpleNamespace(grid=lambda *a, **k: None, grid_remove=lambda *a, **k: None)


def dummy_frame(*_args, **_kwargs):
    return SimpleNamespace(
        grid=lambda *a, **k: None,
        grid_remove=lambda *a, **k: None,
        columnconfigure=lambda *a, **k: None,
        rowconfigure=lambda *a, **k: None,
    )


class DummyScrollbar:
    def grid(self, *_args, **_kwargs):
        return None

    def set(self, *_args, **_kwargs):
        return None


class DummyRoot:
    def __init__(self):
        self.title_text = ""
        self.geometry_text = ""
        self.minsize_value = None
        self.topmost = None
        self.protocols = {}
        self.destroyed = False
        self.withdrawn = False

    def title(self, text):
        self.title_text = text

    def geometry(self, text):
        self.geometry_text = text

    def minsize(self, width, height):
        self.minsize_value = (width, height)

    def attributes(self, name, value):
        if name == "-topmost":
            self.topmost = value

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def columnconfigure(self, *_args, **_kwargs):
        return None

    def rowconfigure(self, *_args, **_kwargs):
        return None

    def withdraw(self):
        self.withdrawn = True

    def deiconify(self):
        self.withdrawn = False

    def lift(self):
        return None

    def focus_force(self):
        return None

    def destroy(self):
        self.destroyed = True

    def mainloop(self):
        return None

    def after(self, _delay_ms, callback):
        self.last_after = callback


class DummyStringVar:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value

    def get(self):
        return self.value


class DummyBooleanVar(DummyStringVar):
    def get(self):
        return bool(self.value)


class FakeApp:
    def __init__(self):
        self.settings = AppSettings()
        self.started = 0
        self.stopped = 0
        self.toggled = 0
        self.saved = None
        self.shutdown_called = 0
        self.closed = 0
        self.hidden = 0
        self.opened_log = 0
        self.requested_close = 0
        self.autostart = []

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def toggle(self):
        self.toggled += 1

    def update_settings(self, settings):
        self.saved = settings

    def shutdown(self):
        self.shutdown_called += 1

    def request_close(self):
        self.requested_close += 1

    def open_log_file(self):
        self.opened_log += 1

    def set_startup_enabled(self, enabled):
        self.autostart.append(enabled)


def test_window_updates_button_state_and_status(monkeypatch):
    monkeypatch.setattr("tkinter.Tk", DummyRoot)
    monkeypatch.setattr("tkinter.StringVar", DummyStringVar)
    monkeypatch.setattr("tkinter.BooleanVar", DummyBooleanVar)
    monkeypatch.setattr("voice_input.ui.tk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.tk.Label", dummy_widget)
    monkeypatch.setattr("tkinter.Text", lambda *args, **kwargs: DummyText())
    monkeypatch.setattr("voice_input.ui.FlatButton", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.Style", lambda _root: DummyStyle())
    monkeypatch.setattr("voice_input.ui.ttk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Label", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Button", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.LabelFrame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Scrollbar", lambda *args, **kwargs: DummyScrollbar())
    monkeypatch.setattr("voice_input.ui.ttk.Entry", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Combobox", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Checkbutton", dummy_widget)
    monkeypatch.setattr("voice_input.ui.messagebox.showerror", lambda *args, **kwargs: None)
    monkeypatch.setattr("voice_input.ui.messagebox.showinfo", lambda *args, **kwargs: None)

    window = TkVoiceInputWindow(FakeApp())

    window.set_running(True)
    assert window.start_button.disabled is True
    assert window.stop_button.disabled is False
    assert window.start_button.text == "正在识别"
    assert window.stop_button.text == "结束录制（开始识别）"

    window.set_running(False)
    assert window.start_button.disabled is False
    assert window.stop_button.disabled is True
    assert window.start_button.text == "开始识别（开始录制）"
    assert window.stop_button.text == "结束录制"
    assert window.status_var.get() == "正在初始化"


def test_window_appends_text_and_saves_settings(monkeypatch):
    monkeypatch.setattr("tkinter.Tk", DummyRoot)
    monkeypatch.setattr("tkinter.StringVar", DummyStringVar)
    monkeypatch.setattr("tkinter.BooleanVar", DummyBooleanVar)
    monkeypatch.setattr("voice_input.ui.tk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.tk.Label", dummy_widget)
    monkeypatch.setattr("tkinter.Text", lambda *args, **kwargs: DummyText())
    monkeypatch.setattr("voice_input.ui.FlatButton", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.Style", lambda _root: DummyStyle())
    monkeypatch.setattr("voice_input.ui.ttk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Label", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Button", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.LabelFrame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Scrollbar", lambda *args, **kwargs: DummyScrollbar())
    monkeypatch.setattr("voice_input.ui.ttk.Entry", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Combobox", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Checkbutton", dummy_widget)
    monkeypatch.setattr("voice_input.ui.messagebox.showerror", lambda *args, **kwargs: None)
    monkeypatch.setattr("voice_input.ui.messagebox.showinfo", lambda *args, **kwargs: None)

    app = FakeApp()
    window = TkVoiceInputWindow(app)

    window.append_text("hello")
    assert window.text_area.value == "hello"

    window.hotkey_var.set("Ctrl+Shift+Space")
    window.topmost_var.set(False)
    window.inject_var.set(True)
    window.enable_hotkey_var.set(False)
    window.minimize_to_tray_var.set(False)
    window.startup_var.set(True)
    window.asr_backend_var.set("funasr")
    window.whisper_model_var.set("models/ggml-tiny.bin")
    window.whisper_executable_var.set("bin/whisper-cli")
    window._save_settings()

    assert app.saved.hotkey == "Ctrl+Shift+Space"
    assert app.saved.floating_window is False
    assert app.saved.inject_text is True
    assert app.saved.enable_hotkey is False
    assert app.saved.minimize_to_tray is False
    assert app.saved.launch_on_startup is True
    assert app.saved.asr_backend == "funasr"
    assert app.saved.whisper_model_path == "models/ggml-tiny.bin"
    assert app.saved.whisper_executable == "bin/whisper-cli"
    assert app.autostart == []

    window._toggle_startup()
    assert app.autostart == [True]


def test_window_close_delegates_to_app(monkeypatch):
    monkeypatch.setattr("tkinter.Tk", DummyRoot)
    monkeypatch.setattr("tkinter.StringVar", DummyStringVar)
    monkeypatch.setattr("tkinter.BooleanVar", DummyBooleanVar)
    monkeypatch.setattr("voice_input.ui.tk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.tk.Label", dummy_widget)
    monkeypatch.setattr("tkinter.Text", lambda *args, **kwargs: DummyText())
    monkeypatch.setattr("voice_input.ui.FlatButton", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.Style", lambda _root: DummyStyle())
    monkeypatch.setattr("voice_input.ui.ttk.Frame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Label", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Button", lambda *args, **kwargs: DummyButton(text=kwargs.get("text", "")))
    monkeypatch.setattr("voice_input.ui.ttk.LabelFrame", dummy_frame)
    monkeypatch.setattr("voice_input.ui.ttk.Scrollbar", lambda *args, **kwargs: DummyScrollbar())
    monkeypatch.setattr("voice_input.ui.ttk.Entry", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Combobox", dummy_widget)
    monkeypatch.setattr("voice_input.ui.ttk.Checkbutton", dummy_widget)

    app = FakeApp()
    window = TkVoiceInputWindow(app)
    window._on_close()

    assert app.requested_close == 1
