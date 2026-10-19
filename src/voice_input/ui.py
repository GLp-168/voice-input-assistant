"""Tkinter user interface for the desktop voice input shell."""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Callable

from .config import DEFAULT_MAX_RECORDING_SECONDS
from .resources import bundled_app_icon
from .settings import DEFAULT_HOTKEY, AppSettings

if TYPE_CHECKING:
    from .app import VoiceInputApp


class FlatButton:
    """Small wrapper around tk.Button with ttk-like state() support."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        text: str,
        command: Callable[[], None],
        variant: str = "secondary",
        colors: dict[str, str],
        wide: bool = False,
    ) -> None:
        palettes = {
            "primary": {
                "bg": colors["accent"],
                "fg": "#ffffff",
                "hover": colors["accent_hover"],
                "disabled_bg": "#b8c7e6",
                "disabled_fg": "#eef2f7",
            },
            "danger": {
                "bg": colors["danger_soft"],
                "fg": colors["danger"],
                "hover": "#fecdca",
                "disabled_bg": "#f2f4f7",
                "disabled_fg": colors["muted"],
            },
            "quiet": {
                "bg": colors["panel"],
                "fg": colors["muted"],
                "hover": colors["panel_soft"],
                "disabled_bg": colors["panel"],
                "disabled_fg": colors["muted"],
            },
            "secondary": {
                "bg": colors["panel_soft"],
                "fg": colors["text"],
                "hover": "#eef2f7",
                "disabled_bg": "#f2f4f7",
                "disabled_fg": colors["muted"],
            },
        }
        self.palette = palettes.get(variant, palettes["secondary"])
        self._selected = False
        self.selected_bg = colors["accent_soft"]
        self.selected_fg = colors["accent"]
        self.widget = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.palette["bg"],
            fg=self.palette["fg"],
            activebackground=self.palette["hover"],
            activeforeground=self.palette["fg"],
            disabledforeground=self.palette["disabled_fg"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            padx=18 if wide else 14,
            pady=10 if wide else 8,
            font=("Microsoft YaHei UI", 10, "bold" if variant == "primary" else "normal"),
        )
        self.widget.bind("<Enter>", self._on_enter)
        self.widget.bind("<Leave>", self._on_leave)

    def grid(self, *args: object, **kwargs: object) -> object:
        return self.widget.grid(*args, **kwargs)

    def configure(self, **kwargs: object) -> None:
        self.widget.configure(**kwargs)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        if str(self.widget.cget("state")) == "normal":
            self.widget.configure(
                bg=self.selected_bg if selected else self.palette["bg"],
                fg=self.selected_fg if selected else self.palette["fg"],
            )

    def state(self, flags: list[str]) -> None:
        if "disabled" in flags:
            self.widget.configure(
                state="disabled",
                bg=self.palette["disabled_bg"],
                fg=self.palette["disabled_fg"],
                cursor="arrow",
            )
        if "!disabled" in flags:
            self.widget.configure(
                state="normal",
                bg=self.selected_bg if self._selected else self.palette["bg"],
                fg=self.selected_fg if self._selected else self.palette["fg"],
                cursor="hand2",
            )

    def _on_enter(self, _event: object) -> None:
        if str(self.widget.cget("state")) == "normal" and not self._selected:
            self.widget.configure(bg=self.palette["hover"])

    def _on_leave(self, _event: object) -> None:
        if str(self.widget.cget("state")) == "normal":
            self.widget.configure(bg=self.selected_bg if self._selected else self.palette["bg"])


class TkVoiceInputWindow:
    """Modern, focused runtime window for daily voice input work."""

    def __init__(self, app: "VoiceInputApp") -> None:
        self.app = app
        self.root = tk.Tk()
        self.root.title("语音转文字")
        self._set_window_icon()
        self.root.geometry("940x640")
        self.root.minsize(820, 560)
        if hasattr(self.root, "configure"):
            self.root.configure(bg="#f5f7fb")
        self.root.attributes("-topmost", app.settings.floating_window)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        settings = app.settings
        self.status_var = tk.StringVar(value="正在初始化")
        self.summary_var = tk.StringVar(value="本地识别 · FunASR")
        self.hotkey_var = tk.StringVar(value=settings.hotkey)
        self.topmost_var = tk.BooleanVar(value=settings.floating_window)
        self.inject_var = tk.BooleanVar(value=settings.inject_text)
        self.enable_hotkey_var = tk.BooleanVar(value=settings.enable_hotkey)
        self.minimize_to_tray_var = tk.BooleanVar(value=settings.minimize_to_tray)
        self.startup_var = tk.BooleanVar(value=settings.launch_on_startup)
        self.asr_backend_var = tk.StringVar(value=settings.asr_backend)
        self.whisper_model_var = tk.StringVar(value=settings.whisper_model_path)
        self.whisper_executable_var = tk.StringVar(value=settings.whisper_executable)

        self._build_styles()
        self._build_layout()
        self.set_running(False)
        self.set_recording(False)

        if settings.start_minimized:
            self.root.withdraw()

    def mainloop(self) -> None:
        self.root.mainloop()

    def _set_window_icon(self) -> None:
        try:
            icon = bundled_app_icon()
            if icon.is_file():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

    def after(self, delay_ms: int, callback: Callable[[], None]) -> None:
        self.root.after(delay_ms, callback)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def append_text(self, text: str) -> None:
        self.text_area.configure(state="normal")
        if self.text_area.index("end-1c") != "1.0":
            self.text_area.insert("end", "\n")
        self.text_area.insert("end", text)
        self.text_area.see("end")
        self.text_area.configure(state="disabled")

    def set_running(self, running: bool) -> None:
        if running:
            self.start_button.state(["disabled"])
            self.stop_button.state(["!disabled"])
            self.start_button.configure(text="正在识别")
            self.stop_button.configure(text="结束录制（开始识别）")
            self.summary_var.set("录制或识别中")
        else:
            self.start_button.state(["!disabled"])
            self.stop_button.state(["disabled"])
            self.start_button.configure(text="开始识别（开始录制）")
            self.stop_button.configure(text="结束录制")
            self.summary_var.set("本地识别 · FunASR")

    def set_recording(self, recording: bool) -> None:
        if recording:
            self.stop_button.state(["!disabled"])
            self.start_button.configure(text="正在录制")
            self.stop_button.configure(text="结束录制（开始识别）")
            self.summary_var.set(f"正在录制 · 最长 {DEFAULT_MAX_RECORDING_SECONDS} 秒")
        elif getattr(self.app, "is_running", False):
            self.stop_button.state(["disabled"])
            self.start_button.configure(text="正在识别")
            self.stop_button.configure(text="正在识别")
            self.summary_var.set("录制已结束 · 正在识别")

    def set_autostart(self, enabled: bool) -> None:
        self.startup_var.set(enabled)

    def show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self.root)

    def show_info(self, title: str, message: str) -> None:
        messagebox.showinfo(title, message, parent=self.root)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(120, lambda: self.root.attributes("-topmost", self.topmost_var.get()))
        except Exception:
            pass
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def close_window(self) -> None:
        self.root.destroy()

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        colors = {
            "bg": "#f5f7fb",
            "panel": "#ffffff",
            "panel_soft": "#f8fafc",
            "line": "#d6deea",
            "text": "#162033",
            "muted": "#667085",
            "accent": "#2563eb",
            "accent_hover": "#1d4ed8",
            "accent_soft": "#dbeafe",
            "success": "#0f766e",
            "danger": "#b42318",
            "danger_soft": "#fee4e2",
        }
        self.colors = colors
        font = ("Microsoft YaHei UI", 10)

        style.configure(".", background=colors["bg"], foreground=colors["text"], font=font)
        style.configure("TCheckbutton", background=colors["panel"], foreground=colors["text"], font=font)
        if hasattr(style, "map"):
            style.map("TCheckbutton", foreground=[("disabled", colors["muted"])])
        style.configure(
            "TEntry",
            fieldbackground=colors["panel_soft"],
            background=colors["panel_soft"],
            foreground=colors["text"],
            insertcolor=colors["text"],
            bordercolor=colors["line"],
            lightcolor=colors["line"],
            darkcolor=colors["line"],
            padding=8,
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["panel_soft"],
            background=colors["panel_soft"],
            foreground=colors["text"],
            bordercolor=colors["line"],
            padding=8,
        )

    def _build_layout(self) -> None:
        outer = tk.Frame(self.root, bg=self.colors["bg"], padx=24, pady=22)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        header = tk.Frame(outer, bg=self.colors["bg"])
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="语音转文字",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Microsoft YaHei UI", 22, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            textvariable=self.summary_var,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Microsoft YaHei UI", 10),
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=(4, 0),
        )
        tk.Label(
            header,
            textvariable=self.status_var,
            bg=self.colors["accent_soft"],
            fg=self.colors["accent"],
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=14,
            pady=7,
        ).grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="e",
            padx=(18, 0),
        )

        workspace = tk.Frame(outer, bg=self.colors["bg"])
        workspace.grid(row=1, column=0, sticky="nsew", pady=(20, 0))
        workspace.columnconfigure(0, weight=5, uniform="workspace")
        workspace.columnconfigure(1, weight=3, uniform="workspace")
        workspace.rowconfigure(0, weight=1)

        transcript = self._panel(workspace, padx=18, pady=18)
        transcript.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        transcript.columnconfigure(0, weight=1)
        transcript.rowconfigure(1, weight=1)

        transcript_header = tk.Frame(transcript, bg=self.colors["panel"])
        transcript_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        transcript_header.columnconfigure(0, weight=1)
        self._panel_title(transcript_header, "识别结果").grid(row=0, column=0, sticky="w")
        transcript_tools = tk.Frame(transcript_header, bg=self.colors["panel"])
        transcript_tools.grid(row=0, column=1, sticky="e")
        FlatButton(
            transcript_tools,
            text="日志",
            command=self.app.open_log_file,
            variant="quiet",
            colors=self.colors,
        ).grid(
            row=0,
            column=0,
            padx=(0, 8),
        )
        FlatButton(
            transcript_tools,
            text="清空",
            command=self._clear_text,
            variant="quiet",
            colors=self.colors,
        ).grid(
            row=0,
            column=1,
        )

        text_shell = tk.Frame(
            transcript,
            bg=self.colors["panel_soft"],
            highlightbackground=self.colors["line"],
            highlightcolor=self.colors["line"],
            highlightthickness=1,
        )
        text_shell.grid(row=1, column=0, sticky="nsew")
        text_shell.columnconfigure(0, weight=1)
        text_shell.rowconfigure(0, weight=1)
        self.text_area = tk.Text(
            text_shell,
            wrap="word",
            width=1,
            height=12,
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=16,
            bg=self.colors["panel_soft"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground="#bfdbfe",
            selectforeground=self.colors["text"],
            font=("Microsoft YaHei UI", 12),
            highlightthickness=0,
        )
        self.text_area.grid(row=0, column=0, sticky="nsew")
        self.text_area.configure(state="disabled")

        scroll = ttk.Scrollbar(text_shell, orient="vertical", command=self.text_area.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text_area.configure(yscrollcommand=scroll.set)

        side = tk.Frame(workspace, bg=self.colors["bg"])
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)

        controls = self._panel(side, padx=18, pady=18)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)
        self._panel_title(controls, "运行控制").grid(row=0, column=0, columnspan=2, sticky="w")
        self._muted_label(controls, variable=self.summary_var).grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(6, 14),
        )
        self.start_button = FlatButton(
            controls,
            text="开始识别（开始录制）",
            command=self.app.start,
            variant="primary",
            colors=self.colors,
            wide=True,
        )
        self.start_button.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.stop_button = FlatButton(
            controls,
            text="结束录制（开始识别）",
            command=self.app.stop,
            variant="danger",
            colors=self.colors,
        )
        self.stop_button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._muted_label(
            controls,
            text=f"单次最长录入 {DEFAULT_MAX_RECORDING_SECONDS} 秒，提前结束会立即开始识别。",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        settings = self._panel(side, padx=18, pady=18)
        settings.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
        self._build_settings_tabs(settings)

    def _build_settings_tabs(self, settings: tk.Frame) -> None:
        settings.columnconfigure(0, weight=1)
        settings.rowconfigure(2, weight=1)
        self._panel_title(settings, "设置").grid(row=0, column=0, sticky="w")

        tab_bar = tk.Frame(settings, bg=self.colors["panel"])
        tab_bar.grid(row=1, column=0, sticky="ew", pady=(12, 12))
        tab_bar.columnconfigure(0, weight=1)
        tab_bar.columnconfigure(1, weight=1)
        self.settings_tab_buttons = {
            "behavior": FlatButton(
                tab_bar,
                text="行为",
                command=lambda: self._show_settings_tab("behavior"),
                variant="secondary",
                colors=self.colors,
            ),
            "engine": FlatButton(
                tab_bar,
                text="引擎",
                command=lambda: self._show_settings_tab("engine"),
                variant="secondary",
                colors=self.colors,
            ),
        }
        self.settings_tab_buttons["behavior"].grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.settings_tab_buttons["engine"].grid(row=0, column=1, sticky="ew", padx=(6, 0))

        content = tk.Frame(settings, bg=self.colors["panel"])
        content.grid(row=2, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        behavior = tk.Frame(content, bg=self.colors["panel"])
        behavior.columnconfigure(0, weight=1)
        behavior.columnconfigure(1, weight=0)
        self._muted_label(behavior, text="快捷键").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(behavior, textvariable=self.hotkey_var, width=18).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        FlatButton(behavior, text="保存", command=self._save_settings, colors=self.colors).grid(
            row=1,
            column=1,
            sticky="ew",
            pady=(6, 0),
        )
        ttk.Checkbutton(behavior, text="窗口置顶", variable=self.topmost_var, command=self._save_settings).grid(
            row=2,
            column=0,
            sticky="w",
            pady=(12, 0),
        )
        ttk.Checkbutton(behavior, text="注入当前输入框", variable=self.inject_var, command=self._save_settings).grid(
            row=2,
            column=1,
            sticky="w",
            pady=(12, 0),
        )
        ttk.Checkbutton(behavior, text="全局快捷键", variable=self.enable_hotkey_var, command=self._save_settings).grid(
            row=3,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        ttk.Checkbutton(behavior, text="关闭到托盘", variable=self.minimize_to_tray_var, command=self._save_settings).grid(
            row=3,
            column=1,
            sticky="w",
            pady=(8, 0),
        )
        ttk.Checkbutton(behavior, text="开机自启", variable=self.startup_var, command=self._toggle_startup).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(8, 0),
        )

        engine = tk.Frame(content, bg=self.colors["panel"])
        engine.columnconfigure(1, weight=1)
        self._muted_label(engine, text="引擎").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            engine,
            textvariable=self.asr_backend_var,
            values=("funasr", "dummy"),
            width=12,
        ).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0))

        self._muted_label(engine, text="模型").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(engine, textvariable=self.whisper_model_var, width=18).grid(
            row=1,
            column=1,
            sticky="ew",
            padx=(12, 8),
            pady=(10, 0),
        )
        FlatButton(engine, text="选择", command=self._select_model_path, colors=self.colors).grid(
            row=1,
            column=2,
            sticky="ew",
            pady=(10, 0),
        )

        self._muted_label(engine, text="执行文件").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(engine, textvariable=self.whisper_executable_var, width=18).grid(
            row=2,
            column=1,
            sticky="ew",
            padx=(12, 8),
            pady=(10, 0),
        )
        FlatButton(engine, text="选择", command=self._select_executable_path, colors=self.colors).grid(
            row=2,
            column=2,
            sticky="ew",
            pady=(10, 0),
        )

        self.settings_tabs = {"behavior": behavior, "engine": engine}
        self._show_settings_tab("behavior")

    def _show_settings_tab(self, name: str) -> None:
        for tab_name, frame in self.settings_tabs.items():
            if tab_name == name:
                frame.grid(row=0, column=0, sticky="nsew")
            else:
                frame.grid_remove()
        for tab_name, button in self.settings_tab_buttons.items():
            if hasattr(button, "set_selected"):
                button.set_selected(tab_name == name)

    def _panel(self, parent: tk.Misc, *, padx: int, pady: int) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=self.colors["panel"],
            padx=padx,
            pady=pady,
            highlightbackground="#e4e7ec",
            highlightcolor="#e4e7ec",
            highlightthickness=1,
        )

    def _panel_title(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )

    def _muted_label(
        self,
        parent: tk.Misc,
        *,
        text: str | None = None,
        variable: tk.StringVar | None = None,
    ) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            textvariable=variable,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=("Microsoft YaHei UI", 10),
        )

    def _save_settings(self) -> None:
        settings = AppSettings(
            hotkey=self.hotkey_var.get().strip() or DEFAULT_HOTKEY,
            start_minimized=self.app.settings.start_minimized,
            floating_window=self.topmost_var.get(),
            inject_text=self.inject_var.get(),
            enable_hotkey=self.enable_hotkey_var.get(),
            minimize_to_tray=self.minimize_to_tray_var.get(),
            launch_on_startup=self.startup_var.get(),
            show_first_run_hint=self.app.settings.show_first_run_hint,
            asr_backend=self.asr_backend_var.get().strip() or "funasr",
            whisper_model_path=self.whisper_model_var.get().strip(),
            whisper_executable=self.whisper_executable_var.get().strip() or "whisper-cli",
        )
        self.root.attributes("-topmost", settings.floating_window)
        self.app.update_settings(settings)

    def _toggle_startup(self) -> None:
        self.app.set_startup_enabled(self.startup_var.get())

    def _clear_text(self) -> None:
        self.text_area.configure(state="normal")
        self.text_area.delete("1.0", "end")
        self.text_area.configure(state="disabled")

    def _on_close(self) -> None:
        self.app.request_close()

    def _select_model_path(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择 whisper.cpp 模型",
            filetypes=[("Model files", "*.bin *.gguf"), ("All files", "*.*")],
        )
        if path:
            self.whisper_model_var.set(path)
            self._save_settings()

    def _select_executable_path(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择 whisper.cpp 执行文件",
            filetypes=[("Executable files", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self.whisper_executable_var.set(path)
            self._save_settings()
