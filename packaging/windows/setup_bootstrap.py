"""Single-file Windows installer bootstrap for Voice Input Assistant."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import winreg
import zipfile


PRODUCT_NAME = "语音转文字"
APP_FOLDER = "VoiceInputAssistant"
VERSION = "0.1.0"
PUBLISHER = "Project Team"
PAYLOAD_NAME = "VoiceInputAssistant.zip"
PAYLOAD_MANIFEST_NAME = "assets/payload-manifest.json"
PAYLOAD_MANIFEST_SCHEMA_VERSION = 1
MAX_PAYLOAD_FILES = 100_000
MAX_PAYLOAD_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024
APP_ICON_NAME = "app.ico"
RUN_VALUE_NAME = "VoiceInputAssistant"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceInputAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LEGACY_PRODUCT_NAMES = ("离线语音输入助手",)
CLSCTX_INPROC_SERVER = 0x1

FUNASR_REQUIRED_FILES = {
    "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ),
    "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ),
    "speech_fsmn_vad_zh-cn-16k-common-pytorch": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "am.mvn",
    ),
    "punc_ct-transformer_cn-en-common-vocab471067-large": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "jieba_usr_dict",
    ),
    "punc_ct-transformer_zh-cn-common-vocab272727-pytorch": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
    ),
    "speech_campplus_sv_zh-cn_16k-common": (
        "configuration.json",
        "config.yaml",
        "campplus_cn_common.bin",
    ),
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _raise_for_hresult(result: int, message: str) -> None:
    if result < 0:
        raise OSError(result, message)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(value: str) -> GUID:
    guid = GUID()
    result = ctypes.oledll.ole32.CLSIDFromString(value, ctypes.byref(guid))
    _raise_for_hresult(result, f"无法解析 COM GUID: {value}")
    return guid


CLSID_ShellLink = _guid("{00021401-0000-0000-C000-000000000046}")
IID_IShellLinkW = _guid("{000214F9-0000-0000-C000-000000000046}")
IID_IPersistFile = _guid("{0000010B-0000-0000-C000-000000000046}")


class IShellLinkW(ctypes.Structure):
    pass


class IPersistFile(ctypes.Structure):
    pass


STDMETHOD = ctypes.WINFUNCTYPE
HRESULT = ctypes.HRESULT
ULONG = ctypes.c_ulong


class IShellLinkWVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))),
        ("AddRef", STDMETHOD(ULONG, ctypes.POINTER(IShellLinkW))),
        ("Release", STDMETHOD(ULONG, ctypes.POINTER(IShellLinkW))),
        ("GetPath", ctypes.c_void_p),
        ("GetIDList", ctypes.c_void_p),
        ("SetIDList", ctypes.c_void_p),
        ("GetDescription", ctypes.c_void_p),
        ("SetDescription", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.c_wchar_p)),
        ("GetWorkingDirectory", ctypes.c_void_p),
        ("SetWorkingDirectory", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.c_wchar_p)),
        ("GetArguments", ctypes.c_void_p),
        ("SetArguments", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.c_wchar_p)),
        ("GetHotkey", ctypes.c_void_p),
        ("SetHotkey", ctypes.c_void_p),
        ("GetShowCmd", ctypes.c_void_p),
        ("SetShowCmd", ctypes.c_void_p),
        ("GetIconLocation", ctypes.c_void_p),
        ("SetIconLocation", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.c_wchar_p, ctypes.c_int)),
        ("SetRelativePath", ctypes.c_void_p),
        ("Resolve", ctypes.c_void_p),
        ("SetPath", STDMETHOD(HRESULT, ctypes.POINTER(IShellLinkW), ctypes.c_wchar_p)),
    ]


IShellLinkW._fields_ = [("lpVtbl", ctypes.POINTER(IShellLinkWVtbl))]


class IPersistFileVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", STDMETHOD(HRESULT, ctypes.POINTER(IPersistFile), ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))),
        ("AddRef", STDMETHOD(ULONG, ctypes.POINTER(IPersistFile))),
        ("Release", STDMETHOD(ULONG, ctypes.POINTER(IPersistFile))),
        ("GetClassID", ctypes.c_void_p),
        ("IsDirty", ctypes.c_void_p),
        ("Load", ctypes.c_void_p),
        ("Save", STDMETHOD(HRESULT, ctypes.POINTER(IPersistFile), ctypes.c_wchar_p, ctypes.c_bool)),
        ("SaveCompleted", ctypes.c_void_p),
        ("GetCurFile", ctypes.c_void_p),
    ]


IPersistFile._fields_ = [("lpVtbl", ctypes.POINTER(IPersistFileVtbl))]


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(description="Install Voice Input Assistant.")
    parser.add_argument("--check", action="store_true", help="Validate the embedded payload and exit.")
    parser.add_argument("--check-runtime", default="", help=argparse.SUPPRESS)
    parser.add_argument("--silent", action="store_true", help="Install with defaults and no wizard.")
    parser.add_argument("--extract-only", default="", help="Extract payload to this directory without installing.")
    parser.add_argument("--install-dir", default="", help="Override install directory.")

    desktop_group = parser.add_mutually_exclusive_group()
    desktop_group.add_argument("--desktop-shortcut", dest="desktop_shortcut", action="store_true")
    desktop_group.add_argument("--no-desktop-shortcut", dest="desktop_shortcut", action="store_false")

    startup_group = parser.add_mutually_exclusive_group()
    startup_group.add_argument("--start-on-boot", dest="start_on_boot", action="store_true")
    startup_group.add_argument("--no-start-on-boot", dest="start_on_boot", action="store_false")

    launch_group = parser.add_mutually_exclusive_group()
    launch_group.add_argument("--launch", dest="launch_after_install", action="store_true")
    launch_group.add_argument("--no-launch", dest="launch_after_install", action="store_false")

    parser.set_defaults(
        desktop_shortcut=True,
        start_on_boot=False,
        launch_after_install=None,
    )
    args = parser.parse_args(raw_argv)

    try:
        payload = _payload_path()
        if args.check:
            _validate_payload(payload)
            print(f"payload ok: {payload}")
            return 0

        if args.check_runtime:
            runtime_dir = Path(args.check_runtime).expanduser().resolve()
            _validate_payload(payload)
            _run_post_install_health_check(runtime_dir)
            print(f"payload and runtime ok: {runtime_dir}")
            return 0

        if args.extract_only:
            destination = Path(args.extract_only).expanduser().resolve()
            _extract_payload(payload, destination)
            print(f"extracted to {destination}")
            return 0

        install_dir = (
            Path(args.install_dir).expanduser().resolve()
            if args.install_dir
            else _default_install_dir()
        )

        if args.silent:
            app_exe = _install(
                payload,
                install_dir,
                create_desktop_shortcut=args.desktop_shortcut,
                start_on_boot=args.start_on_boot,
            )
            if args.launch_after_install:
                _launch_app(app_exe, app_exe.parent)
            return 0

        wizard = _create_installer_wizard(
            payload=payload,
            default_install_dir=install_dir,
            default_desktop_shortcut=args.desktop_shortcut,
            default_start_on_boot=args.start_on_boot,
            default_launch_after_install=(
                True if args.launch_after_install is None else args.launch_after_install
            ),
        )
        return wizard.run()
    except Exception as exc:
        if (
            "--silent" not in raw_argv
            and "--check" not in raw_argv
            and "--check-runtime" not in raw_argv
            and "--extract-only" not in raw_argv
        ):
            _show_error(str(exc))
        print(f"install failed: {exc}", file=sys.stderr)
        return 1


def _create_installer_wizard(**options: object) -> object:
    try:
        return InstallerWizard(**options)
    except ImportError:
        return _TkInstallerWizard(**options)


class _LegacyInstallerWizard:
    """Minimal product-style Windows installer wizard."""

    def __init__(
        self,
        *,
        payload: Path,
        default_install_dir: Path,
        default_desktop_shortcut: bool,
        default_start_on_boot: bool,
        default_launch_after_install: bool,
    ) -> None:
        import tkinter as tk
        from tkinter import font, ttk

        self.tk = tk
        self.font = font
        self.ttk = ttk
        self.payload = payload
        self.exit_code = 0
        self.is_installing = False
        self.app_exe: Path | None = None
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.colors = {
            "shell": "#17191d",
            "bg": "#202329",
            "glow": "#12394a",
            "glow_alt": "#122f3b",
            "surface": "#292d34",
            "surface_alt": "#333842",
            "field": "#f7fafc",
            "field_text": "#222833",
            "line": "#464b55",
            "text": "#f8fafc",
            "muted": "#b9c0ca",
            "subtle": "#88919e",
            "accent": "#f39800",
            "accent_hover": "#ffad2e",
            "accent_press": "#d77d00",
            "accent_soft": "#4a351d",
            "success": "#35d39a",
            "success_soft": "#183d31",
            "error": "#ff6f8a",
            "error_soft": "#3a1d27",
            "disabled": "#555a63",
        }
        self.font_family = self._choose_font_family()

        self.root = tk.Tk()
        self.root.title(f"{PRODUCT_NAME} 安装向导")
        self._set_window_icon()
        self.window_width = 960
        self.window_height = 660
        self.root.geometry(f"{self.window_width}x{self.window_height}")
        self.root.resizable(False, False)
        self.root.configure(bg=self.colors["shell"])
        try:
            self.root.overrideredirect(True)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _event: self._on_close())
        self.root.bind("<Map>", self._restore_custom_chrome)

        self.install_dir_var = tk.StringVar(value=str(default_install_dir))
        self.desktop_shortcut_var = tk.BooleanVar(value=default_desktop_shortcut)
        self.start_on_boot_var = tk.BooleanVar(value=default_start_on_boot)
        self.launch_after_install_var = tk.BooleanVar(value=default_launch_after_install)
        self.footer_hint_var = tk.StringVar(value="")

        self.primary_button: tk.Button | None = None
        self.secondary_button: tk.Button | None = None
        self.tertiary_button: tk.Button | None = None
        self.progress_bar: ttk.Progressbar | None = None
        self.progress_canvas: tk.Canvas | None = None
        self.progress_text_var = tk.StringVar(value="正在安装0%")
        self.progress_status_var = tk.StringVar(value="正在准备离线资源")
        self.progress_value = 0.0
        self.step_widgets: list[dict[str, object]] = []
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._minimized = False

        self._center_window()
        self._configure_styles()
        self._build_shell()
        self._fade_in()
        self._render_options_page()

    def run(self) -> int:
        self.root.mainloop()
        return self.exit_code

    def _set_window_icon(self) -> None:
        try:
            icon = _bundled_app_icon()
            if icon.is_file():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

    def _configure_styles(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Installer.Horizontal.TProgressbar",
            troughcolor=self.colors["surface_alt"],
            bordercolor=self.colors["surface_alt"],
            background=self.colors["accent"],
            lightcolor=self.colors["accent"],
            darkcolor=self.colors["accent"],
        )

    def _build_shell(self) -> None:
        tk = self.tk
        outer = tk.Frame(self.root, bg=self.colors["bg"])
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=self.colors["bg"])
        header.pack(fill="x", padx=30, pady=(24, 10))

        brand = tk.Frame(header, bg=self.colors["bg"])
        brand.pack(side="left", anchor="n")
        self._make_brand_icon(brand).pack(side="left", padx=(0, 14))
        brand_text = tk.Frame(brand, bg=self.colors["bg"])
        brand_text.pack(side="left")
        tk.Label(
            brand_text,
            text=PRODUCT_NAME,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Segoe UI", 23, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="离线识别引擎、默认模型和桌面入口会随安装包一次写入本机。",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(5, 0))

        chips = tk.Frame(header, bg=self.colors["bg"])
        chips.pack(side="right", anchor="n", pady=(6, 0))
        self._make_chip(chips, "离线安装", accent=True).pack(side="right", padx=(8, 0))
        self._make_chip(chips, "Windows").pack(side="right", padx=(8, 0))
        self._make_chip(chips, f"v{VERSION}").pack(side="right")

        self._build_stepper(outer)
        tk.Frame(outer, bg=self.colors["line"], height=1).pack(fill="x", padx=30, pady=(12, 0))

        self.content = tk.Frame(outer, bg=self.colors["bg"])
        self.content.pack(fill="both", expand=True, padx=30, pady=(18, 16))

        tk.Frame(outer, bg=self.colors["line"], height=1).pack(fill="x", padx=30)

        footer = tk.Frame(outer, bg=self.colors["bg"])
        footer.pack(fill="x", padx=30, pady=(12, 22))
        tk.Label(
            footer,
            textvariable=self.footer_hint_var,
            bg=self.colors["bg"],
            fg=self.colors["subtle"],
            font=("Segoe UI", 9),
            justify="left",
            wraplength=500,
        ).pack(side="left", anchor="w")

        footer_actions = tk.Frame(footer, bg=self.colors["bg"])
        footer_actions.pack(side="right")
        self.primary_button = self._make_button(footer_actions, "", lambda: None, variant="accent")
        self.primary_button.pack(side="right")
        self.secondary_button = self._make_button(footer_actions, "", lambda: None, variant="neutral")
        self.secondary_button.pack(side="right", padx=(10, 0))
        self.tertiary_button = self._make_button(footer_actions, "取消", self._on_close, variant="ghost")
        self.tertiary_button.pack(side="right", padx=(10, 0))

    def _build_stepper(self, parent: object) -> None:
        tk = self.tk
        self.step_widgets = []
        step_specs = [
            ("安装设置", "选择目录和启动方式"),
            ("写入文件", "部署离线引擎和模型"),
            ("完成", "确认入口并启动应用"),
        ]
        step_bar = tk.Frame(parent, bg=self.colors["bg"])
        step_bar.pack(fill="x", padx=30, pady=(8, 0))
        for index, (title, desc) in enumerate(step_specs):
            cell = tk.Frame(step_bar, bg=self.colors["bg"])
            cell.pack(side="left", fill="x", expand=True)
            row = tk.Frame(cell, bg=self.colors["bg"])
            row.pack(anchor="w")
            badge = tk.Canvas(row, width=28, height=28, bg=self.colors["bg"], highlightthickness=0, bd=0)
            badge.pack(side="left")
            labels = tk.Frame(row, bg=self.colors["bg"])
            labels.pack(side="left", padx=(10, 0))
            step_label = tk.Label(
                labels,
                text=f"步骤 {index + 1}",
                bg=self.colors["bg"],
                fg=self.colors["subtle"],
                font=("Segoe UI", 8, "bold"),
            )
            step_label.pack(anchor="w")
            title_label = tk.Label(
                labels,
                text=title,
                bg=self.colors["bg"],
                fg=self.colors["text"],
                font=("Segoe UI", 10, "bold"),
            )
            title_label.pack(anchor="w")
            desc_label = tk.Label(
                labels,
                text=desc,
                bg=self.colors["bg"],
                fg=self.colors["muted"],
                font=("Segoe UI", 9),
            )
            desc_label.pack(anchor="w")
            self.step_widgets.append(
                {
                    "badge": badge,
                    "step_label": step_label,
                    "title_label": title_label,
                    "desc_label": desc_label,
                }
            )

    def _set_stage(self, index: int) -> None:
        for step_index, item in enumerate(self.step_widgets):
            badge = item["badge"]
            step_label = item["step_label"]
            title_label = item["title_label"]
            desc_label = item["desc_label"]
            active = step_index == index
            done = step_index < index
            fill = self.colors["accent"] if active else self.colors["accent_soft"] if done else self.colors["surface_alt"]
            outline = self.colors["accent"] if active or done else self.colors["line"]
            number_color = self.colors["bg"] if active else self.colors["text"] if done else self.colors["muted"]
            title_color = self.colors["text"] if active or done else self.colors["muted"]
            desc_color = self.colors["muted"] if active or done else self.colors["subtle"]
            badge.delete("all")
            badge.create_oval(2, 2, 26, 26, fill=fill, outline=outline, width=1)
            badge.create_text(14, 14, text=str(step_index + 1), fill=number_color, font=("Segoe UI", 9, "bold"))
            step_label.configure(fg=title_color)
            title_label.configure(fg=title_color)
            desc_label.configure(fg=desc_color)

    def _render_options_page(self) -> None:
        self._clear_content()
        self.is_installing = False
        self._set_stage(0)
        self.footer_hint_var.set("安装过程不会联网下载模型；所有识别依赖已包含在安装包内。")
        self._set_footer(
            primary_text="开始安装",
            primary_command=self._begin_install,
            secondary_text="更改位置",
            secondary_command=self._browse_install_dir,
            tertiary_text="取消",
            tertiary_command=self._on_close,
        )

        tk = self.tk
        main = tk.Frame(self.content, bg=self.colors["bg"])
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg=self.colors["bg"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 20))
        right = tk.Frame(main, bg=self.colors["bg"], width=260)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        self._make_section_title(
            left,
            "准备安装",
            "确认安装位置和入口配置，然后开始写入应用、离线识别引擎和默认模型。",
        )

        path_card = self._make_card(left, padx=18, pady=16)
        path_card.pack(fill="x", pady=(20, 14))
        tk.Label(
            path_card,
            text="安装位置",
            bg=self.colors["surface"],
            fg=self.colors["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        path_row = tk.Frame(path_card, bg=self.colors["surface"])
        path_row.pack(fill="x", pady=(9, 0))
        entry = tk.Entry(
            path_row,
            textvariable=self.install_dir_var,
            bg=self.colors["field"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.colors["line"],
            highlightcolor=self.colors["accent"],
            font=("Segoe UI", 10),
        )
        entry.pack(side="left", fill="x", expand=True, ipady=9)
        self._make_button(path_row, "浏览...", self._browse_install_dir, variant="neutral").pack(side="left", padx=(10, 0))

        options_card = self._make_card(left, padx=18, pady=16)
        options_card.pack(fill="x")
        tk.Label(
            options_card,
            text="安装选项",
            bg=self.colors["surface"],
            fg=self.colors["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")
        self._make_checkbox(
            options_card,
            "添加桌面快捷方式",
            self.desktop_shortcut_var,
        ).pack(anchor="w", pady=(12, 9))
        self._make_checkbox(
            options_card,
            "开机后自动启动",
            self.start_on_boot_var,
        ).pack(anchor="w")

        self._make_fact_tile(right, "离线能力", "内置 FunASR 流式、VAD、标点与高精度复核模型。", accent=True).pack(fill="x")
        self._make_fact_tile(right, "系统入口", "安装后写入桌面、开始菜单和卸载入口。").pack(fill="x", pady=(12, 0))
        self._make_fact_tile(right, "运行方式", "应用启动后可通过右下角系统托盘重新打开。").pack(fill="x", pady=(12, 0))

    def _render_progress_page(self) -> None:
        self._clear_content()
        self._set_stage(1)
        self.footer_hint_var.set("正在部署本地离线资源，请不要关闭安装器。")
        self._set_footer(
            primary_text="正在安装...",
            primary_command=lambda: None,
            secondary_text="",
            secondary_command=lambda: None,
            tertiary_text="",
            tertiary_command=lambda: None,
        )
        self._set_button_enabled(self.primary_button, False)
        self._set_button_enabled(self.secondary_button, False)
        self._set_button_enabled(self.tertiary_button, False)

        tk = self.tk
        panel = tk.Frame(self.content, bg=self.colors["bg"])
        panel.pack(fill="both", expand=True)
        center = tk.Frame(panel, bg=self.colors["bg"])
        center.place(relx=0.5, rely=0.48, anchor="center", width=650)

        self._make_status_icon(center, "install").pack(anchor="center")
        tk.Label(
            center,
            text="正在安装",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Segoe UI", 22, "bold"),
        ).pack(anchor="center", pady=(16, 0))
        tk.Label(
            center,
            text="正在写入应用文件、离线识别引擎、模型文件、快捷方式和卸载入口。",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor="center", pady=(7, 24))

        self.progress_bar = self.ttk.Progressbar(
            center,
            style="Installer.Horizontal.TProgressbar",
            mode="indeterminate",
        )
        self.progress_bar.pack(fill="x")
        self.progress_bar.start(12)

    def _render_finish_page(self) -> None:
        self._clear_content()
        self.is_installing = False
        self.exit_code = 0
        self._set_stage(2)
        self.footer_hint_var.set("完成后可以从桌面、开始菜单或系统托盘再次打开应用。")
        self._set_footer(
            primary_text="完成并启动",
            primary_command=self._finish,
            secondary_text="仅启动应用",
            secondary_command=self._launch_installed_app,
            tertiary_text="稍后打开",
            tertiary_command=self._finish_without_launch,
        )
        self._set_button_enabled(self.primary_button, True)
        self._set_button_enabled(self.secondary_button, True)
        self._set_button_enabled(self.tertiary_button, True)

        tk = self.tk
        main = tk.Frame(self.content, bg=self.colors["bg"])
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg=self.colors["bg"])
        left.pack(side="left", fill="both", expand=True, padx=(0, 20))
        right = tk.Frame(main, bg=self.colors["bg"], width=260)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        self._make_status_icon(left, "success").pack(anchor="w")
        tk.Label(
            left,
            text="安装完成",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Segoe UI", 24, "bold"),
        ).pack(anchor="w", pady=(14, 0))
        tk.Label(
            left,
            text=f"{PRODUCT_NAME} 已准备就绪，点击完成即可启动应用。",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(7, 18))

        path_card = self._make_card(left, padx=18, pady=15)
        path_card.pack(fill="x")
        tk.Label(
            path_card,
            text="已安装到",
            bg=self.colors["surface"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            path_card,
            text=self.install_dir_var.get(),
            bg=self.colors["surface"],
            fg=self.colors["accent"],
            font=("Segoe UI", 10),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        self._make_checkbox(
            left,
            "完成后立即启动应用",
            self.launch_after_install_var,
        ).pack(anchor="w", pady=(18, 0))

        self._make_fact_tile(right, "启动入口", "桌面快捷方式、开始菜单和系统托盘均可唤起应用。", accent=True).pack(fill="x")
        self._make_fact_tile(right, "托盘操作", "托盘菜单提供显示主窗口、开始/停止识别和退出。").pack(fill="x", pady=(12, 0))
        self._make_fact_tile(right, "卸载入口", "可从开始菜单或 Windows 应用管理中卸载。").pack(fill="x", pady=(12, 0))

    def _render_error_page(self, message: str) -> None:
        self._clear_content()
        self.is_installing = False
        self.exit_code = 1
        self.footer_hint_var.set("修正问题后可重新安装，已写入的临时文件会在下一次安装时清理。")
        self._set_footer(
            primary_text="返回重试",
            primary_command=self._render_options_page,
            secondary_text="",
            secondary_command=lambda: None,
            tertiary_text="关闭",
            tertiary_command=self._on_close,
        )
        self._set_button_enabled(self.primary_button, True)
        self._set_button_enabled(self.secondary_button, False)
        self._set_button_enabled(self.tertiary_button, True)

        tk = self.tk
        panel = tk.Frame(self.content, bg=self.colors["bg"])
        panel.pack(fill="both", expand=True)
        self._make_status_icon(panel, "error").pack(anchor="w")
        tk.Label(
            panel,
            text="安装失败",
            bg=self.colors["bg"],
            fg="#ffd6df",
            font=("Segoe UI", 24, "bold"),
        ).pack(anchor="w", pady=(14, 0))
        error_card = self._make_card(panel, padx=18, pady=15, error=True)
        error_card.pack(fill="x", pady=(18, 0))
        tk.Label(
            error_card,
            text=message,
            bg=self.colors["surface"],
            fg="#ffb8c6",
            font=("Segoe UI", 10),
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

    def _begin_install(self) -> None:
        try:
            install_dir = _normalize_install_dir(self.install_dir_var.get())
            _validate_install_dir_choice(install_dir)
        except Exception as exc:
            self._message_error(str(exc))
            return

        self.install_dir_var.set(str(install_dir))
        self.is_installing = True
        self._render_progress_page()

        worker = threading.Thread(
            target=self._install_worker,
            args=(install_dir,),
            name="installer-worker",
            daemon=True,
        )
        worker.start()
        self.root.after(120, self._poll_install_result)

    def _install_worker(self, install_dir: Path) -> None:
        try:
            app_exe = _install(
                self.payload,
                install_dir,
                create_desktop_shortcut=self.desktop_shortcut_var.get(),
                start_on_boot=self.start_on_boot_var.get(),
            )
            self.result_queue.put(("success", app_exe))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))

    def _poll_install_result(self) -> None:
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_install_result)
            return

        if self.progress_bar is not None:
            self.progress_bar.stop()
        if kind == "success":
            self.app_exe = payload if isinstance(payload, Path) else Path(str(payload))
            self._render_finish_page()
            return
        self._render_error_page(str(payload))

    def _browse_install_dir(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择安装位置",
            initialdir=str(_existing_parent(Path(self.install_dir_var.get()).expanduser())),
            mustexist=False,
        )
        if selected:
            target = Path(selected)
            if target.name != APP_FOLDER:
                target = target / APP_FOLDER
            self.install_dir_var.set(str(target))

    def _launch_installed_app(self) -> None:
        if self.app_exe is None:
            return
        try:
            _launch_app(self.app_exe, self.app_exe.parent)
            self.launch_after_install_var.set(False)
            if self.secondary_button is not None:
                self.secondary_button.configure(text="已启动")
                self._set_button_enabled(self.secondary_button, False)
        except Exception as exc:
            self._message_error(f"无法启动应用：{exc}")

    def _finish(self) -> None:
        if self.launch_after_install_var.get():
            self._launch_installed_app()
        self.root.destroy()

    def _finish_without_launch(self) -> None:
        self.launch_after_install_var.set(False)
        self.root.destroy()

    def _on_close(self) -> None:
        if self.is_installing:
            self._message_error("安装正在进行，请稍候。")
            return
        self.root.destroy()

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    def _set_footer(
        self,
        *,
        primary_text: str,
        primary_command: object,
        secondary_text: str,
        secondary_command: object,
        tertiary_text: str,
        tertiary_command: object,
    ) -> None:
        if self.primary_button is not None:
            self.primary_button.configure(text=primary_text, command=primary_command)
            self.primary_button.pack_configure(side="right")
        if self.secondary_button is not None:
            self.secondary_button.configure(text=secondary_text, command=secondary_command)
            if secondary_text:
                self.secondary_button.pack_configure(side="right", padx=(10, 0))
            else:
                self.secondary_button.pack_forget()
        if self.tertiary_button is not None:
            self.tertiary_button.configure(text=tertiary_text, command=tertiary_command)
            if tertiary_text:
                self.tertiary_button.pack_configure(side="right", padx=(10, 0))
            else:
                self.tertiary_button.pack_forget()

    def _make_brand_icon(self, parent: object) -> object:
        tk = self.tk
        canvas = tk.Canvas(parent, width=48, height=48, bg=self.colors["bg"], highlightthickness=0, bd=0)
        canvas.create_oval(4, 4, 44, 44, fill=self.colors["surface_alt"], outline=self.colors["accent"], width=1)
        canvas.create_rectangle(20, 13, 28, 29, fill=self.colors["field"], outline=self.colors["accent"], width=2)
        canvas.create_line(24, 29, 24, 35, fill=self.colors["accent"], width=2)
        canvas.create_line(18, 35, 30, 35, fill=self.colors["accent"], width=2)
        canvas.create_arc(13, 11, 35, 33, start=205, extent=130, style="arc", outline=self.colors["accent"], width=2)
        return canvas

    def _make_status_icon(self, parent: object, kind: str) -> object:
        tk = self.tk
        canvas = tk.Canvas(parent, width=72, height=72, bg=self.colors["bg"], highlightthickness=0, bd=0)
        if kind == "success":
            canvas.create_oval(4, 4, 68, 68, fill=self.colors["success_soft"], outline=self.colors["success"], width=1)
            canvas.create_line(20, 37, 31, 48, fill=self.colors["success"], width=4)
            canvas.create_line(31, 48, 52, 25, fill=self.colors["success"], width=4)
        elif kind == "error":
            canvas.create_oval(4, 4, 68, 68, fill=self.colors["error_soft"], outline=self.colors["error"], width=1)
            canvas.create_line(24, 24, 48, 48, fill=self.colors["error"], width=4)
            canvas.create_line(48, 24, 24, 48, fill=self.colors["error"], width=4)
        else:
            canvas.create_oval(4, 4, 68, 68, fill=self.colors["accent_soft"], outline=self.colors["accent"], width=1)
            canvas.create_rectangle(30, 19, 42, 44, fill=self.colors["field"], outline=self.colors["accent"], width=2)
            canvas.create_line(36, 44, 36, 54, fill=self.colors["accent"], width=3)
            canvas.create_line(28, 54, 44, 54, fill=self.colors["accent"], width=3)
        return canvas

    def _make_section_title(self, parent: object, title: str, subtitle: str) -> None:
        tk = self.tk
        tk.Label(
            parent,
            text=title,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=("Segoe UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            parent,
            text=subtitle,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
            wraplength=560,
            justify="left",
        ).pack(anchor="w", pady=(7, 0))

    def _make_card(self, parent: object, *, padx: int, pady: int, error: bool = False) -> object:
        tk = self.tk
        frame = tk.Frame(parent, bg=self.colors["surface"], padx=padx, pady=pady)
        frame.configure(highlightthickness=1, highlightbackground=self.colors["error"] if error else self.colors["line"])
        return frame

    def _make_chip(self, parent: object, text: str, *, accent: bool = False) -> object:
        tk = self.tk
        bg = self.colors["accent_soft"] if accent else self.colors["surface_alt"]
        fg = self.colors["accent"] if accent else self.colors["text"]
        return tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=("Segoe UI", 9, "bold"),
            padx=10,
            pady=4,
        )

    def _make_fact_tile(self, parent: object, title: str, value: str, *, accent: bool = False) -> object:
        tk = self.tk
        frame = self._make_card(parent, padx=14, pady=12)
        title_color = self.colors["accent"] if accent else self.colors["muted"]
        tk.Label(
            frame,
            text=title,
            bg=self.colors["surface"],
            fg=title_color,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=value,
            bg=self.colors["surface"],
            fg=self.colors["text"],
            font=("Segoe UI", 10),
            wraplength=210,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))
        return frame

    def _make_button(
        self,
        parent: object,
        text: str,
        command: object,
        *,
        variant: str = "neutral",
    ) -> object:
        tk = self.tk
        styles = {
            "accent": (self.colors["accent"], "#06101a", self.colors["accent_hover"], "bold", 28, 11),
            "neutral": (self.colors["surface_alt"], self.colors["text"], "#1d3048", "normal", 18, 9),
            "ghost": (self.colors["bg"], self.colors["muted"], self.colors["surface_alt"], "normal", 18, 9),
        }
        bg, fg, active_bg, weight, padx, pady = styles.get(variant, styles["neutral"])
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            bd=0,
            relief="flat",
            cursor="hand2",
            padx=padx,
            pady=pady,
            font=("Segoe UI", 10, weight),
        )

    def _make_checkbox(self, parent: object, text: str, variable: object) -> object:
        tk = self.tk
        bg = parent.cget("bg") if hasattr(parent, "cget") else self.colors["bg"]
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=bg,
            fg=self.colors["text"],
            activebackground=bg,
            activeforeground=self.colors["accent"],
            selectcolor=self.colors["field"],
            font=("Segoe UI", 10),
            cursor="hand2",
        )

    def _set_button_enabled(self, button: object | None, enabled: bool) -> None:
        if button is None:
            return
        button.configure(state="normal" if enabled else "disabled")

    def _message_error(self, message: str) -> None:
        from tkinter import messagebox

        messagebox.showerror(PRODUCT_NAME, message, parent=self.root)


class _TkInstallerWizard:
    """Minimal product-style Windows installer wizard."""

    def __init__(
        self,
        *,
        payload: Path,
        default_install_dir: Path,
        default_desktop_shortcut: bool,
        default_start_on_boot: bool,
        default_launch_after_install: bool,
    ) -> None:
        import tkinter as tk
        from tkinter import font

        self.tk = tk
        self.font = font
        self.payload = payload
        self.exit_code = 0
        self.is_installing = False
        self.app_exe: Path | None = None
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.colors = {
            "shell": "#17191d",
            "bg": "#202329",
            "glow": "#12394a",
            "glow_alt": "#122f3b",
            "surface": "#292d34",
            "surface_alt": "#333842",
            "field": "#f7fafc",
            "field_text": "#222833",
            "line": "#464b55",
            "text": "#f8fafc",
            "muted": "#b9c0ca",
            "subtle": "#88919e",
            "accent": "#f39800",
            "accent_hover": "#ffad2e",
            "accent_press": "#d77d00",
            "accent_soft": "#4a351d",
            "success": "#35d39a",
            "success_soft": "#183d31",
            "error": "#ff6f8a",
            "error_soft": "#3a1d27",
            "disabled": "#555a63",
        }
        self.root = tk.Tk()
        self.font_family = self._choose_font_family()
        self.root.title(f"{PRODUCT_NAME} 安装向导")
        self._set_window_icon()
        self.window_width = 960
        self.window_height = 660
        self.root.geometry(f"{self.window_width}x{self.window_height}")
        self.root.resizable(False, False)
        self.root.configure(bg=self.colors["shell"])
        try:
            self.root.overrideredirect(True)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Escape>", lambda _event: self._on_close())
        self.root.bind("<Map>", self._restore_custom_chrome)

        self.install_dir_var = tk.StringVar(value=str(default_install_dir))
        self.desktop_shortcut_var = tk.BooleanVar(value=default_desktop_shortcut)
        self.start_on_boot_var = tk.BooleanVar(value=default_start_on_boot)
        self.launch_after_install_var = tk.BooleanVar(value=default_launch_after_install)
        self.progress_text_var = tk.StringVar(value="正在安装0%")
        self.progress_status_var = tk.StringVar(value="正在准备离线资源")

        self.primary_button: tk.Button | None = None
        self.secondary_button: tk.Button | None = None
        self.progress_canvas: tk.Canvas | None = None
        self.progress_value = 0.0
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._minimized = False

        self._center_window()
        self._build_shell()
        self._fade_in()
        self._render_options_page()

    def run(self) -> int:
        self.root.mainloop()
        return self.exit_code

    def _set_window_icon(self) -> None:
        try:
            icon = _bundled_app_icon()
            if icon.is_file():
                self.root.iconbitmap(str(icon))
        except Exception:
            pass

    def _choose_font_family(self) -> str:
        try:
            families = set(self.font.families())
        except Exception:
            families = set()
        for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
            if family in families:
                return family
        return "Arial"

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return (self.font_family, size, weight)

    def _center_window(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - self.window_width) // 2)
        y = max(0, (screen_h - self.window_height) // 2)
        self.root.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")

    def _fade_in(self) -> None:
        try:
            self.root.attributes("-alpha", 0.0)
        except Exception:
            return

        def step(alpha: float = 0.0) -> None:
            try:
                next_alpha = min(1.0, alpha + 0.08)
                self.root.attributes("-alpha", next_alpha)
                if next_alpha < 1.0:
                    self.root.after(12, lambda: step(next_alpha))
            except Exception:
                pass

        step()

    def _build_shell(self) -> None:
        tk = self.tk
        self.window = tk.Frame(
            self.root,
            bg=self.colors["bg"],
            highlightthickness=1,
            highlightbackground="#3b3f47",
        )
        self.window.pack(fill="both", expand=True, padx=10, pady=10)

        self.header = tk.Frame(self.window, bg=self.colors["bg"], height=76)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.header.bind("<ButtonPress-1>", self._start_drag)
        self.header.bind("<B1-Motion>", self._drag_window)

        brand = tk.Frame(self.header, bg=self.colors["bg"])
        brand.pack(side="left", padx=34, pady=20)
        self._make_brand_icon(brand).pack(side="left", padx=(0, 12))
        tk.Label(
            brand,
            text=PRODUCT_NAME,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=self._font(15, "bold"),
        ).pack(side="left")

        controls = tk.Frame(self.header, bg=self.colors["bg"])
        controls.pack(side="right", padx=26, pady=20)
        self._make_window_button(controls, "—", self._minimize_window).pack(side="left", padx=(0, 8))
        self._make_window_button(controls, "×", self._on_close).pack(side="left")

        self.content = tk.Frame(self.window, bg=self.colors["bg"])
        self.content.pack(fill="both", expand=True)

    def _start_drag(self, event: object) -> None:
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _drag_window(self, event: object) -> None:
        x = self.root.winfo_pointerx() - self._drag_start_x - 10
        y = self.root.winfo_pointery() - self._drag_start_y - 10
        self.root.geometry(f"+{x}+{y}")

    def _minimize_window(self) -> None:
        if self.is_installing:
            return
        self._minimized = True
        try:
            self.root.overrideredirect(False)
        except Exception:
            pass
        self.root.iconify()

    def _restore_custom_chrome(self, _event: object | None = None) -> None:
        if not self._minimized:
            return
        self._minimized = False
        try:
            self.root.after(10, lambda: self.root.overrideredirect(True))
        except Exception:
            pass

    def _render_options_page(self) -> None:
        self._clear_content()
        self.is_installing = False
        self.progress_canvas = None

        tk = self.tk
        page = self._make_center_page()
        self._make_hero(page, "install", size="small").pack(anchor="center", pady=(0, 8))
        tk.Label(
            page,
            text="准备安装",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=self._font(24, "bold"),
        ).pack(anchor="center")
        tk.Label(
            page,
            text="选择安装位置后即可开始",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=self._font(11),
        ).pack(anchor="center", pady=(5, 12))

        path_wrap = tk.Frame(page, bg=self.colors["bg"])
        path_wrap.pack(fill="x", padx=110)
        tk.Label(
            path_wrap,
            text="安装路径",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=self._font(10, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        path_row = tk.Frame(
            path_wrap,
            bg=self.colors["field"],
            highlightthickness=1,
            highlightbackground="#d9e1ea",
            highlightcolor=self.colors["accent"],
        )
        path_row.pack(fill="x")
        entry = tk.Entry(
            path_row,
            textvariable=self.install_dir_var,
            bg=self.colors["field"],
            fg=self.colors["field_text"],
            insertbackground=self.colors["field_text"],
            relief="flat",
            bd=0,
            font=self._font(10),
        )
        entry.pack(side="left", fill="x", expand=True, padx=(14, 8), ipady=11)
        entry.xview_moveto(0)
        self._make_button(
            path_row,
            "浏览",
            self._browse_install_dir,
            variant="neutral",
            compact=True,
        ).pack(side="left", padx=(0, 8), pady=8)

        option_row = tk.Frame(page, bg=self.colors["bg"])
        option_row.pack(anchor="center", pady=(10, 12))
        self._make_checkbox(
            option_row,
            "添加桌面快捷方式",
            self.desktop_shortcut_var,
        ).pack(side="left", padx=(0, 28))
        self._make_checkbox(
            option_row,
            "开机自动启动",
            self.start_on_boot_var,
        ).pack(side="left")

        self.primary_button = self._make_button(page, "立即安装", self._begin_install, variant="accent")
        self.primary_button.pack(anchor="center")

    def _render_progress_page(self) -> None:
        self._clear_content()
        self.progress_value = 0.0
        self.progress_text_var.set("正在安装0%")
        self.progress_status_var.set("正在准备离线资源")

        tk = self.tk
        page = self._make_center_page()
        self._make_hero(page, "progress").pack(anchor="center", pady=(10, 22))
        tk.Label(
            page,
            text="轻快运行，极低的资源占用",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=self._font(25, "bold"),
        ).pack(anchor="center", pady=(16, 0))
        tk.Label(
            page,
            textvariable=self.progress_status_var,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=self._font(11),
        ).pack(anchor="center", pady=(10, 28))

        self.progress_canvas = tk.Canvas(
            page,
            width=760,
            height=14,
            bg=self.colors["bg"],
            highlightthickness=0,
            bd=0,
        )
        self.progress_canvas.pack(anchor="center")
        tk.Label(
            page,
            textvariable=self.progress_text_var,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=self._font(11),
        ).pack(anchor="center", pady=(12, 0))
        self._draw_progress()
        self._animate_progress()

    def _animate_progress(self) -> None:
        if not self.is_installing or self.progress_canvas is None:
            return
        if self.progress_value < 88:
            increment = 1.6 if self.progress_value < 35 else 0.9 if self.progress_value < 70 else 0.35
            self.progress_value = min(88.0, self.progress_value + increment)
            if self.progress_value > 62:
                self.progress_status_var.set("正在创建快捷方式和系统入口")
            elif self.progress_value > 28:
                self.progress_status_var.set("正在写入识别引擎和模型")
            self._draw_progress()
        self.root.after(70, self._animate_progress)

    def _draw_progress(self) -> None:
        if self.progress_canvas is None:
            return
        width = 760
        height = 8
        y = 3
        self.progress_canvas.delete("all")
        self._rounded_rectangle(self.progress_canvas, 0, y, width, y + height, 4, fill="#575b65", outline="")
        fill_width = max(10, int(width * max(0, min(100, self.progress_value)) / 100))
        self._rounded_rectangle(self.progress_canvas, 0, y, fill_width, y + height, 4, fill=self.colors["accent"], outline="")
        self.progress_text_var.set(f"正在安装{int(self.progress_value)}%")

    def _finish_progress_success(self) -> None:
        self.progress_value = 100
        self.progress_status_var.set("安装完成")
        self._draw_progress()
        self.root.after(420, self._render_finish_page)

    def _render_finish_page(self) -> None:
        self._clear_content()
        self.is_installing = False
        self.exit_code = 0
        self.progress_canvas = None

        tk = self.tk
        page = self._make_center_page()
        self._make_hero(page, "success").pack(anchor="center", pady=(16, 18))
        tk.Label(
            page,
            text="安装完成",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            font=self._font(28, "bold"),
        ).pack(anchor="center")
        tk.Label(
            page,
            text=f"{PRODUCT_NAME} 已经准备就绪",
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=self._font(11),
        ).pack(anchor="center", pady=(8, 18))
        tk.Label(
            page,
            text=self.install_dir_var.get(),
            bg=self.colors["bg"],
            fg=self.colors["subtle"],
            font=self._font(9),
            wraplength=720,
        ).pack(anchor="center", pady=(0, 22))

        self.primary_button = self._make_button(page, "开始使用", self._start_using, variant="accent")
        self.primary_button.pack(anchor="center")

    def _render_error_page(self, message: str) -> None:
        self._clear_content()
        self.is_installing = False
        self.exit_code = 1
        self.progress_canvas = None

        tk = self.tk
        page = self._make_center_page()
        self._make_hero(page, "error").pack(anchor="center", pady=(16, 18))
        tk.Label(
            page,
            text="安装失败",
            bg=self.colors["bg"],
            fg="#ffd6df",
            font=self._font(27, "bold"),
        ).pack(anchor="center")
        tk.Label(
            page,
            text=message,
            bg=self.colors["bg"],
            fg="#ffb8c6",
            font=self._font(10),
            wraplength=700,
            justify="center",
        ).pack(anchor="center", pady=(14, 28))
        self.primary_button = self._make_button(page, "返回重试", self._render_options_page, variant="accent")
        self.primary_button.pack(anchor="center")

    def _begin_install(self) -> None:
        try:
            install_dir = _normalize_install_dir(self.install_dir_var.get())
            _validate_install_dir_choice(install_dir)
        except Exception as exc:
            self._message_error(str(exc))
            return

        self.install_dir_var.set(str(install_dir))
        self._show_install_path_start()
        self.is_installing = True
        self._set_button_enabled(self.primary_button, False)
        self._render_progress_page()

        worker = threading.Thread(
            target=self._install_worker,
            args=(install_dir,),
            name="installer-worker",
            daemon=True,
        )
        worker.start()
        self.root.after(120, self._poll_install_result)

    def _install_worker(self, install_dir: Path) -> None:
        try:
            app_exe = _install(
                self.payload,
                install_dir,
                create_desktop_shortcut=self.desktop_shortcut_var.get(),
                start_on_boot=self.start_on_boot_var.get(),
            )
            self.result_queue.put(("success", app_exe))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))

    def _poll_install_result(self) -> None:
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_install_result)
            return

        self.is_installing = False
        if kind == "success":
            self.app_exe = payload if isinstance(payload, Path) else Path(str(payload))
            self._finish_progress_success()
            return
        self._render_error_page(str(payload))

    def _browse_install_dir(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择安装位置",
            initialdir=str(_existing_parent(Path(self.install_dir_var.get()).expanduser())),
            mustexist=False,
        )
        if selected:
            target = Path(selected)
            if target.name != APP_FOLDER:
                target = target / APP_FOLDER
            self.install_dir_var.set(str(target))
            self._show_install_path_start()

    def _show_install_path_start(self) -> None:
        try:
            focused = self.root.focus_get()
            if hasattr(focused, "xview_moveto"):
                focused.xview_moveto(0)
        except Exception:
            pass

    def _launch_installed_app(self) -> None:
        if self.app_exe is None:
            return
        try:
            _launch_app(self.app_exe, self.app_exe.parent)
            self.launch_after_install_var.set(False)
            if self.secondary_button is not None:
                self.secondary_button.configure(text="已启动")
                self._set_button_enabled(self.secondary_button, False)
        except Exception as exc:
            self._message_error(f"无法启动应用：{exc}")

    def _finish(self) -> None:
        if self.launch_after_install_var.get():
            self._launch_installed_app()
        self.root.destroy()

    def _start_using(self) -> None:
        self._launch_installed_app()
        self.root.destroy()

    def _finish_without_launch(self) -> None:
        self.launch_after_install_var.set(False)
        self.root.destroy()

    def _on_close(self) -> None:
        if self.is_installing:
            self._message_error("安装正在进行，请稍候。")
            return
        self.root.destroy()

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()

    def _make_center_page(self) -> object:
        tk = self.tk
        page = tk.Frame(self.content, bg=self.colors["bg"])
        page.pack(fill="both", expand=True)
        page.bind("<ButtonPress-1>", self._start_drag)
        page.bind("<B1-Motion>", self._drag_window)
        center = tk.Frame(page, bg=self.colors["bg"])
        center.place(relx=0.5, y=0, anchor="n", width=820)
        return center

    def _make_brand_icon(self, parent: object) -> object:
        tk = self.tk
        canvas = tk.Canvas(parent, width=34, height=34, bg=self.colors["bg"], highlightthickness=0, bd=0)
        canvas.create_polygon(17, 2, 30, 8, 28, 23, 17, 32, 6, 23, 4, 8, fill="#f8fafc", outline="")
        canvas.create_polygon(17, 5, 26, 10, 24, 22, 17, 28, 10, 22, 8, 10, fill=self.colors["accent"], outline="")
        canvas.create_line(17, 10, 17, 23, fill="#ffffff", width=3, capstyle="round")
        canvas.create_arc(11, 9, 23, 24, start=205, extent=130, outline="#ffffff", width=2)
        return canvas

    def _make_window_button(self, parent: object, text: str, command: object) -> object:
        tk = self.tk
        button = tk.Button(
            parent,
            text=text,
            command=command,
            width=3,
            height=1,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            activebackground=self.colors["surface_alt"],
            activeforeground=self.colors["text"],
            bd=0,
            relief="flat",
            cursor="hand2",
            font=self._font(16),
        )
        button.bind("<Enter>", lambda _event: self._tween_button(button, self.colors["surface_alt"]))
        button.bind("<Leave>", lambda _event: self._tween_button(button, self.colors["bg"]))
        return button

    def _make_hero(self, parent: object, kind: str, *, size: str = "normal") -> object:
        tk = self.tk
        if size == "small":
            canvas = tk.Canvas(parent, width=220, height=130, bg=self.colors["bg"], highlightthickness=0, bd=0)
            scale = 0.72
            offset_x = 9
            offset_y = -14
        else:
            canvas = tk.Canvas(parent, width=280, height=190, bg=self.colors["bg"], highlightthickness=0, bd=0)
            scale = 1.0
            offset_x = 0
            offset_y = 0
        canvas.create_oval(
            int(52 * scale + offset_x),
            int(20 * scale + offset_y),
            int(228 * scale + offset_x),
            int(176 * scale + offset_y),
            fill=self.colors["glow"],
            outline="",
        )
        canvas.create_oval(
            int(82 * scale + offset_x),
            int(52 * scale + offset_y),
            int(198 * scale + offset_x),
            int(152 * scale + offset_y),
            fill=self.colors["glow_alt"],
            outline="",
        )
        if kind == "success":
            self._draw_success_hero(canvas)
        elif kind == "error":
            self._draw_error_hero(canvas)
        else:
            self._draw_install_hero(canvas, active=kind == "progress", scale=scale, offset_x=offset_x, offset_y=offset_y)
        return canvas

    def _draw_install_hero(self, canvas: object, *, active: bool, scale: float = 1.0, offset_x: int = 0, offset_y: int = 0) -> None:
        def p(value: int, axis: str = "x") -> int:
            return int(value * scale + (offset_x if axis == "x" else offset_y))

        canvas.create_oval(p(72), p(126, "y"), p(208), p(162, "y"), fill="#4f5668", outline="")
        canvas.create_oval(p(87), p(118, "y"), p(193), p(148, "y"), fill="#687086", outline="")
        canvas.create_oval(p(64), p(122, "y"), p(216), p(168, "y"), outline=self.colors["accent"], width=2)
        canvas.create_polygon(p(140), p(60, "y"), p(166), p(124, "y"), p(114), p(124, "y"), fill="#e5cf85", outline="")
        canvas.create_polygon(p(140), p(76, "y"), p(156), p(126, "y"), p(124), p(126, "y"), fill="#f1d989", outline="")
        canvas.create_oval(p(100), p(34, "y"), p(180), p(86, "y"), fill="#aeb4c8", outline="")
        canvas.create_oval(p(112), p(40, "y"), p(168), p(75, "y"), fill="#c9cedf", outline="")
        canvas.create_rectangle(p(112), p(58, "y"), p(168), p(83, "y"), fill="#9ca3b8", outline="")
        if active:
            canvas.create_arc(p(90), p(44, "y"), p(190), p(144, "y"), start=210, extent=120, outline="#ffffff", width=2, style="arc")
            canvas.create_arc(p(78), p(32, "y"), p(202), p(156, "y"), start=210, extent=120, outline=self.colors["accent"], width=2, style="arc")

    def _draw_success_hero(self, canvas: object) -> None:
        canvas.create_polygon(140, 32, 210, 62, 202, 128, 140, 164, 78, 128, 70, 62, fill="#f8fafc", outline="")
        canvas.create_polygon(140, 42, 198, 67, 190, 121, 140, 151, 90, 121, 82, 67, fill="#233039", outline="")
        canvas.create_line(112, 100, 132, 120, fill=self.colors["success"], width=8, capstyle="round")
        canvas.create_line(132, 120, 170, 78, fill=self.colors["success"], width=8, capstyle="round")
        canvas.create_oval(82, 130, 198, 160, outline=self.colors["accent"], width=2)

    def _draw_error_hero(self, canvas: object) -> None:
        canvas.create_oval(92, 42, 188, 138, fill=self.colors["error_soft"], outline=self.colors["error"], width=3)
        canvas.create_line(116, 66, 164, 114, fill=self.colors["error"], width=7, capstyle="round")
        canvas.create_line(164, 66, 116, 114, fill=self.colors["error"], width=7, capstyle="round")

    def _rounded_rectangle(self, canvas: object, x1: int, y1: int, x2: int, y2: int, radius: int, **kwargs: object) -> None:
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        canvas.create_polygon(points, smooth=True, **kwargs)

    def _make_button(
        self,
        parent: object,
        text: str,
        command: object,
        *,
        variant: str = "neutral",
        compact: bool = False,
    ) -> object:
        tk = self.tk
        if variant == "accent":
            bg = self.colors["accent"]
            fg = "#17191d"
            hover = self.colors["accent_hover"]
            press = self.colors["accent_press"]
            weight = "bold"
            padx = 44 if not compact else 20
            pady = 12 if not compact else 8
        elif variant == "ghost":
            bg = self.colors["bg"]
            fg = self.colors["muted"]
            hover = self.colors["surface_alt"]
            press = self.colors["surface"]
            weight = "normal"
            padx = 18
            pady = 9
        else:
            bg = "#edf2f7" if compact else self.colors["surface_alt"]
            fg = self.colors["field_text"] if compact else self.colors["text"]
            hover = "#ffffff" if compact else "#424855"
            press = "#dfe7ef" if compact else self.colors["surface"]
            weight = "bold" if compact else "normal"
            padx = 18 if compact else 22
            pady = 8 if compact else 10
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=press,
            activeforeground=fg,
            bd=0,
            relief="flat",
            cursor="hand2",
            padx=padx,
            pady=pady,
            font=self._font(11, weight),
        )
        button._normal_bg = bg  # type: ignore[attr-defined]
        button._normal_fg = fg  # type: ignore[attr-defined]
        button._hover_bg = hover  # type: ignore[attr-defined]
        button._press_bg = press  # type: ignore[attr-defined]
        button._tween_job = None  # type: ignore[attr-defined]
        button.bind("<Enter>", lambda _event: self._tween_button(button, button._hover_bg))  # type: ignore[attr-defined]
        button.bind("<Leave>", lambda _event: self._tween_button(button, button._normal_bg))  # type: ignore[attr-defined]
        button.bind("<ButtonPress-1>", lambda _event: self._tween_button(button, button._press_bg, steps=2))  # type: ignore[attr-defined]
        button.bind("<ButtonRelease-1>", lambda _event: self._tween_button(button, button._hover_bg, steps=3))  # type: ignore[attr-defined]
        return button

    def _tween_button(self, button: object, target: str, *, steps: int = 5) -> None:
        try:
            if str(button.cget("state")) == "disabled":
                return
            existing = getattr(button, "_tween_job", None)
            if existing:
                self.root.after_cancel(existing)
            start = button.cget("bg")
            start_rgb = self._hex_to_rgb(start)
            target_rgb = self._hex_to_rgb(target)

            def tick(index: int = 1) -> None:
                try:
                    mixed = self._rgb_to_hex(
                        tuple(
                            int(start_rgb[channel] + (target_rgb[channel] - start_rgb[channel]) * index / steps)
                            for channel in range(3)
                        )
                    )
                    button.configure(bg=mixed, activebackground=mixed)
                    if index < steps:
                        job = self.root.after(14, lambda: tick(index + 1))
                        button._tween_job = job  # type: ignore[attr-defined]
                except Exception:
                    pass

            tick()
        except Exception:
            try:
                button.configure(bg=target, activebackground=target)
            except Exception:
                pass

    def _hex_to_rgb(self, value: str) -> tuple[int, int, int]:
        value = str(value).strip()
        if value.startswith("#") and len(value) == 7:
            return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
        red, green, blue = self.root.winfo_rgb(value)
        return (red // 256, green // 256, blue // 256)

    def _rgb_to_hex(self, rgb: tuple[int, int, int]) -> str:
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    def _make_checkbox(self, parent: object, text: str, variable: object) -> object:
        tk = self.tk
        bg = parent.cget("bg") if hasattr(parent, "cget") else self.colors["bg"]
        check = tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            bg=bg,
            fg=self.colors["muted"],
            activebackground=bg,
            activeforeground=self.colors["text"],
            selectcolor=self.colors["surface_alt"],
            font=self._font(10),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        check.bind("<Enter>", lambda _event: check.configure(fg=self.colors["text"]))
        check.bind("<Leave>", lambda _event: check.configure(fg=self.colors["muted"]))
        return check

    def _set_button_enabled(self, button: object | None, enabled: bool) -> None:
        if button is None:
            return
        try:
            button.configure(state="normal" if enabled else "disabled")
            if enabled:
                button.configure(cursor="hand2", fg=getattr(button, "_normal_fg", self.colors["text"]))
                self._tween_button(button, getattr(button, "_normal_bg", self.colors["surface_alt"]))
            else:
                button.configure(cursor="arrow", bg=self.colors["disabled"], fg=self.colors["muted"])
        except Exception:
            pass

    def _message_error(self, message: str) -> None:
        from tkinter import messagebox

        messagebox.showerror(PRODUCT_NAME, message, parent=self.root)


class InstallerWizard:
    """Qt installer wizard sharing the runtime UI visual language."""

    def __init__(
        self,
        *,
        payload: Path,
        default_install_dir: Path,
        default_desktop_shortcut: bool,
        default_start_on_boot: bool,
        default_launch_after_install: bool,
    ) -> None:
        from PySide6 import QtCore, QtGui, QtWidgets

        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.payload = payload
        self.model_profile = _payload_model_profile_from_path(payload)
        self.exit_code = 0
        self.is_installing = False
        self.app_exe: Path | None = None
        self.result_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.progress_value = 0
        self._progress_timer: object | None = None
        self._motion_tick = 0
        self._motion_widgets: list[object] = []
        self._progress_visual: object | None = None
        self.progress_percent_label: object | None = None
        self._stage_labels: list[object] = []
        self.InstallStage = self._make_install_stage_class()

        self.qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        self.qt_app.setApplicationName(PRODUCT_NAME)
        self.qt_app.setStyle("Fusion")
        self._register_cjk_font()
        self.qt_app.setFont(self._ui_font(10))

        self.window = QtWidgets.QMainWindow()
        self.window.setWindowTitle(f"{PRODUCT_NAME} 安装向导")
        icon = _bundled_app_icon()
        self.app_icon = QtGui.QIcon(str(icon)) if icon.is_file() else QtGui.QIcon()
        if not self.app_icon.isNull():
            self.qt_app.setWindowIcon(self.app_icon)
            self.window.setWindowIcon(self.app_icon)
        self.window.resize(980, 680)
        self.window.setMinimumSize(900, 640)
        self.window.setStyleSheet(INSTALLER_STYLE_SHEET)
        self.window.closeEvent = self._close_event  # type: ignore[method-assign]

        self.CheckBox = self._make_checkbox_class()
        self.install_dir_input = QtWidgets.QLineEdit(str(default_install_dir))
        self.install_dir_input.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        self.install_dir_input.setCursorPosition(0)
        self.desktop_shortcut_check = self.CheckBox("添加桌面快捷方式")
        self.desktop_shortcut_check.setChecked(default_desktop_shortcut)
        self.start_on_boot_check = self.CheckBox("开机自动启动")
        self.start_on_boot_check.setChecked(default_start_on_boot)
        self.launch_after_install_check = self.CheckBox("安装完成后启动应用")
        self.launch_after_install_check.setChecked(default_launch_after_install)

        self.stack = QtWidgets.QStackedWidget()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_label = QtWidgets.QLabel("准备资源")
        self._build_shell()
        self._progress_timer = QtCore.QTimer(self.window)
        self._progress_timer.setInterval(80)
        self._progress_timer.timeout.connect(self._animate_progress)
        self._motion_timer = QtCore.QTimer(self.window)
        self._motion_timer.timeout.connect(self._advance_motion)
        self._motion_timer.start(34)
        self._render_options_page()

    def run(self) -> int:
        self.window.show()
        self._center_window()
        self.qt_app.exec()
        return self.exit_code

    def _build_shell(self) -> None:
        QtWidgets = self.QtWidgets
        QtCore = self.QtCore

        root = QtWidgets.QWidget()
        root.setObjectName("InstallerRoot")
        root.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.window.setCentralWidget(root)

        page = QtWidgets.QVBoxLayout(root)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)

        shell = QtWidgets.QWidget()
        shell.setObjectName("InstallerShell")
        shell_layout = QtWidgets.QVBoxLayout(shell)
        shell_layout.setContentsMargins(28, 22, 28, 28)
        shell_layout.setSpacing(12)

        header = QtWidgets.QFrame()
        header.setObjectName("InstallerHeader")
        header_layout = QtWidgets.QHBoxLayout(header)
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(10)

        header_layout.addWidget(self._brand_logo_label(28))
        brand_name = QtWidgets.QLabel("VOICE INPUT")
        brand_name.setObjectName("InstallerBrandName")
        header_layout.addWidget(brand_name)
        separator = QtWidgets.QFrame()
        separator.setObjectName("BrandSeparator")
        separator.setFixedSize(1, 18)
        header_layout.addWidget(separator)
        brand_context = QtWidgets.QLabel("安装向导")
        brand_context.setObjectName("InstallerBrandContext")
        header_layout.addWidget(brand_context)
        header_layout.addStretch(1)

        status = QtWidgets.QLabel("●  全程本地处理")
        status.setObjectName("InstallerStatusChip")
        status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(status)
        header_layout.addStretch(1)

        profile_label = "轻量实时版" if self.model_profile == "streaming" else "完整离线版"
        release = QtWidgets.QLabel(f"v{VERSION} · {profile_label}")
        release.setObjectName("InstallerRelease")
        release.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(release)
        shell_layout.addWidget(header)

        stage_track = QtWidgets.QWidget()
        stage_track.setObjectName("StageTrack")
        self.stage_row = QtWidgets.QHBoxLayout(stage_track)
        self.stage_row.setContentsMargins(4, 0, 4, 0)
        self.stage_row.setSpacing(12)
        for index, name in enumerate(("准备", "部署", "完成"), start=1):
            label = QtWidgets.QLabel(f"{index:02d}   {name}")
            label.setObjectName("StageLabel")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.stage_row.addWidget(label)
            self._stage_labels.append(label)
        shell_layout.addWidget(stage_track)

        shell_layout.addWidget(self.stack, 1)
        page.addWidget(shell)
        self._set_stage(0)

    def _register_cjk_font(self) -> None:
        if sys.platform != "win32":
            return
        font_dir = Path("C:/Windows/Fonts")
        for filename in ("msyh.ttc", "msyh.ttf", "NotoSansSC-VF.ttf", "Deng.ttf", "simhei.ttf", "simsun.ttc"):
            path = font_dir / filename
            if path.exists() and self.QtGui.QFontDatabase.addApplicationFont(str(path)) >= 0:
                return

    def _ui_font(self, size: int = 10) -> object:
        available = set(self.QtGui.QFontDatabase.families())
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
        return self.QtGui.QFont(family, size)

    def _brand_logo_label(self, size: int) -> object:
        label = self.QtWidgets.QLabel()
        label.setObjectName("InstallerBrandLogo")
        label.setFixedSize(size, size)
        if not self.app_icon.isNull():
            label.setPixmap(self.app_icon.pixmap(size, size))
        return label

    def _render_options_page(self) -> None:
        QtWidgets = self.QtWidgets

        if self._progress_timer is not None:
            self._progress_timer.stop()
        self._progress_visual = None
        self.progress_percent_label = None
        self._set_stage(0)
        stage = self.InstallStage("options")
        layout = QtWidgets.QHBoxLayout(stage)
        layout.setContentsMargins(42, 38, 42, 38)
        layout.setSpacing(38)

        narrative_wrap = QtWidgets.QWidget()
        narrative = QtWidgets.QVBoxLayout(narrative_wrap)
        narrative.setContentsMargins(0, 4, 0, 4)
        narrative.setSpacing(0)
        kicker = QtWidgets.QLabel("LOCAL SETUP")
        kicker.setObjectName("SectionKicker")
        title = QtWidgets.QLabel(PRODUCT_NAME)
        title.setObjectName("InstallerTitle")
        subtitle_text = (
            "轻量实时版，专注低延迟的边录边转写。"
            if self.model_profile == "streaming"
            else "实时转写、音频导入与结束复核，全部在本机完成。"
        )
        subtitle = QtWidgets.QLabel(subtitle_text)
        subtitle.setObjectName("InstallerSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setMaximumWidth(410)
        narrative.addWidget(kicker)
        narrative.addSpacing(12)
        narrative.addWidget(title)
        narrative.addSpacing(12)
        narrative.addWidget(subtitle)
        narrative.addSpacing(30)

        model_summary = (
            "低延迟流式识别"
            if self.model_profile == "streaming"
            else "流式 · 离线复核 · VAD · 标点 · 说话人"
        )
        specs = QtWidgets.QFrame()
        specs.setObjectName("SetupSpecs")
        specs_layout = QtWidgets.QVBoxLayout(specs)
        specs_layout.setContentsMargins(0, 0, 0, 0)
        specs_layout.setSpacing(0)
        specs_layout.addWidget(self._spec_row("识别能力", model_summary))
        specs_layout.addWidget(self._spec_row("隐私方式", "音频与文本不离开本机"))
        specs_layout.addWidget(self._spec_row("录制时长", "单次最长 2 小时"))
        narrative.addWidget(specs)
        narrative.addStretch(1)

        footnote = QtWidgets.QLabel("VOICE INPUT  /  WINDOWS DESKTOP")
        footnote.setObjectName("SetupFootnote")
        narrative.addWidget(footnote)
        layout.addWidget(narrative_wrap, 5)

        form = QtWidgets.QFrame()
        form.setObjectName("GlassPanel")
        form_layout = QtWidgets.QVBoxLayout(form)
        form_layout.setContentsMargins(30, 30, 30, 28)
        form_layout.setSpacing(0)
        form_layout.addWidget(self._section_title("准备安装", "确认位置与系统入口。"))
        form_layout.addSpacing(26)

        path_wrap = QtWidgets.QWidget()
        path_layout = QtWidgets.QVBoxLayout(path_wrap)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(10)
        path_layout.addWidget(self._field_label("安装位置"))
        path_row = QtWidgets.QHBoxLayout()
        path_row.setSpacing(12)
        self.install_dir_input.setPlaceholderText("选择安装目录")
        self.install_dir_input.setToolTip("安装位置。长路径会优先显示左侧开头，右侧超出部分隐藏。")
        path_row.addWidget(self.install_dir_input, 1)
        browse = self._button("浏览", "secondary")
        browse.setFixedWidth(78)
        browse.clicked.connect(self._browse_install_dir)
        path_row.addWidget(browse)
        path_layout.addLayout(path_row)
        form_layout.addWidget(path_wrap)
        form_layout.addSpacing(22)

        option_divider = QtWidgets.QFrame()
        option_divider.setObjectName("OptionDivider")
        option_divider.setFixedHeight(1)
        form_layout.addWidget(option_divider)
        form_layout.addSpacing(16)
        form_layout.addWidget(self.desktop_shortcut_check)
        form_layout.addSpacing(8)
        form_layout.addWidget(self.start_on_boot_check)
        form_layout.addStretch(1)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(12)
        assurance = QtWidgets.QLabel("无需联网")
        assurance.setObjectName("AssuranceLabel")
        actions.addWidget(assurance)
        actions.addStretch(1)
        install = self._button("开始安装  →", "primary")
        install.setMinimumWidth(142)
        install.clicked.connect(self._begin_install)
        actions.addWidget(install)
        form_layout.addLayout(actions)
        layout.addWidget(form, 6)

        self._set_single_page(stage)

        def settle_initial_focus() -> None:
            try:
                self._show_install_path_start()
                install.setFocus(self.QtCore.Qt.FocusReason.OtherFocusReason)
            except RuntimeError:
                return

        self.QtCore.QTimer.singleShot(0, settle_initial_focus)

    def _render_progress_page(self) -> None:
        QtWidgets = self.QtWidgets

        self._set_stage(1)
        panel = self.InstallStage("progress")
        layout = QtWidgets.QHBoxLayout(panel)
        layout.setContentsMargins(52, 46, 52, 46)
        layout.setSpacing(52)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(0)
        eyebrow = QtWidgets.QLabel("INSTALLING LOCALLY")
        eyebrow.setObjectName("SectionKicker")
        left.addWidget(eyebrow)
        left.addSpacing(18)
        title = QtWidgets.QLabel("正在部署本地能力")
        title.setObjectName("ProgressTitle")
        left.addWidget(title)
        left.addSpacing(12)
        self.progress_label.setObjectName("InstallerSubtitle")
        left.addWidget(self.progress_label)
        left.addSpacing(34)

        self.progress_percent_label = QtWidgets.QLabel(str(self.progress_value))
        self.progress_percent_label.setObjectName("ProgressPercent")
        percent_suffix = QtWidgets.QLabel("%")
        percent_suffix.setObjectName("ProgressPercentSuffix")
        percent_row = QtWidgets.QHBoxLayout()
        percent_row.setSpacing(8)
        percent_row.addWidget(self.progress_percent_label)
        percent_row.addWidget(percent_suffix, 0, self.QtCore.Qt.AlignmentFlag.AlignBottom)
        percent_row.addStretch(1)
        left.addLayout(percent_row)
        left.addSpacing(14)

        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(self.progress_value)
        self.progress_bar.setTextVisible(False)
        left.addWidget(self.progress_bar)
        left.addSpacing(18)

        hint = QtWidgets.QLabel("窗口可以保持在后台，安装完成后会自动提示。")
        hint.setObjectName("ProgressHint")
        hint.setWordWrap(True)
        left.addWidget(hint)
        left.addStretch(1)
        privacy = QtWidgets.QLabel("●  不上传音频或文本")
        privacy.setObjectName("PrivacyNote")
        left.addWidget(privacy)
        layout.addLayout(left, 6)

        self._progress_visual = self._motion_panel("部署中", "正在写入模型与本地运行时", mode="progress")
        layout.addWidget(self._progress_visual, 5)
        self._set_single_page(panel)

    def _render_finish_page(self) -> None:
        QtWidgets = self.QtWidgets

        if self._progress_timer is not None:
            self._progress_timer.stop()
        self.progress_value = 100
        self.progress_percent_label = None
        self._set_stage(2)
        panel = self.InstallStage("finish")
        layout = QtWidgets.QHBoxLayout(panel)
        layout.setContentsMargins(42, 38, 42, 38)
        layout.setSpacing(36)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(18)
        title = QtWidgets.QLabel("安装完成")
        title.setObjectName("ProgressTitle")
        left.addWidget(title)
        subtitle = QtWidgets.QLabel("可以开始使用了。")
        subtitle.setObjectName("InstallerSubtitle")
        left.addWidget(subtitle)
        left.addWidget(self.launch_after_install_check)
        actions = QtWidgets.QHBoxLayout()
        later = self._button("关闭", "secondary")
        later.clicked.connect(self._finish_without_launch)
        start = self._button("完成", "primary")
        start.clicked.connect(self._finish)
        actions.addWidget(later)
        actions.addWidget(start)
        actions.addStretch(1)
        left.addLayout(actions)
        left.addStretch(1)
        layout.addLayout(left, 6)
        completion_visual = self._motion_panel("已就绪", "启动后即可本地识别", mode="complete")
        self._progress_visual = completion_visual
        layout.addWidget(completion_visual, 5)
        self._set_single_page(panel)

    def _render_error_page(self, message: str) -> None:
        QtWidgets = self.QtWidgets

        if self._progress_timer is not None:
            self._progress_timer.stop()
        self._progress_visual = None
        self.progress_percent_label = None
        self._set_stage(1, error=True)
        panel = self.InstallStage("error")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(14)
        layout.addStretch(1)
        icon = QtWidgets.QLabel("!")
        icon.setObjectName("ErrorGlyph")
        icon.setFixedSize(54, 54)
        icon.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon, 0, self.QtCore.Qt.AlignmentFlag.AlignCenter)
        title = QtWidgets.QLabel("安装失败")
        title.setObjectName("ProgressTitle")
        title.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        detail = QtWidgets.QPlainTextEdit(message)
        detail.setObjectName("ErrorDetails")
        detail.setReadOnly(True)
        detail.setMaximumHeight(132)
        layout.addWidget(detail)
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        copy_error = self._button("复制错误", "secondary")
        copy_error.clicked.connect(lambda: self.qt_app.clipboard().setText(message))
        actions.addWidget(copy_error)
        retry = self._button("返回重试", "primary")
        retry.clicked.connect(self._render_options_page)
        actions.addWidget(retry)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        self._set_single_page(panel)

    def _begin_install(self) -> None:
        try:
            install_dir = _normalize_install_dir(self.install_dir_input.text())
            _validate_install_dir_choice(install_dir)
        except Exception as exc:
            self._message_error(str(exc))
            return

        self._set_install_dir_text(install_dir)
        self.is_installing = True
        self.progress_value = 0
        self.progress_label.setText("正在准备本地识别资源")
        create_desktop_shortcut = self.desktop_shortcut_check.isChecked()
        start_on_boot = self.start_on_boot_check.isChecked()
        self._render_progress_page()
        if self._progress_timer is not None:
            self._progress_timer.start()
        worker = threading.Thread(
            target=self._install_worker,
            args=(install_dir, create_desktop_shortcut, start_on_boot),
            name="installer-worker",
            daemon=True,
        )
        worker.start()
        self.QtCore.QTimer.singleShot(120, self._poll_install_result)

    def _install_worker(
        self,
        install_dir: Path,
        create_desktop_shortcut: bool,
        start_on_boot: bool,
    ) -> None:
        try:
            app_exe = _install(
                self.payload,
                install_dir,
                create_desktop_shortcut=create_desktop_shortcut,
                start_on_boot=start_on_boot,
            )
            self.result_queue.put(("success", app_exe))
        except Exception as exc:
            self.result_queue.put(("error", str(exc)))

    def _poll_install_result(self) -> None:
        try:
            kind, payload = self.result_queue.get_nowait()
        except queue.Empty:
            self.QtCore.QTimer.singleShot(120, self._poll_install_result)
            return

        self.is_installing = False
        if self._progress_timer is not None:
            self._progress_timer.stop()
        if kind == "success":
            self.app_exe = payload if isinstance(payload, Path) else Path(str(payload))
            self.progress_value = 100
            self.progress_bar.setValue(100)
            self.progress_label.setText("安装完成")
            if self.progress_percent_label is not None:
                try:
                    self.progress_percent_label.setText("100")
                except RuntimeError:
                    self.progress_percent_label = None
            self._update_progress_visual(100)
            self.QtCore.QTimer.singleShot(420, self._render_finish_page)
            return
        self._render_error_page(str(payload))

    def _animate_progress(self) -> None:
        if not self.is_installing:
            if self._progress_timer is not None:
                self._progress_timer.stop()
            return
        if self.progress_value < 88:
            if self.progress_value > 62:
                self.progress_label.setText("创建入口")
            elif self.progress_value > 28:
                self.progress_label.setText("写入模型")
            self.progress_value = min(88, self.progress_value + (3 if self.progress_value < 36 else 2))
            self.progress_bar.setValue(self.progress_value)
            if self.progress_percent_label is not None:
                try:
                    self.progress_percent_label.setText(str(self.progress_value))
                except RuntimeError:
                    self.progress_percent_label = None
            self._update_progress_visual(self.progress_value)

    def _browse_install_dir(self) -> None:
        selected = self.QtWidgets.QFileDialog.getExistingDirectory(
            self.window,
            "选择安装位置",
            str(_existing_parent(Path(self.install_dir_input.text()).expanduser())),
        )
        if selected:
            target = Path(selected)
            if target.name != APP_FOLDER:
                target = target / APP_FOLDER
            self._set_install_dir_text(target)

    def _set_install_dir_text(self, path: Path) -> None:
        self.install_dir_input.setText(str(path))
        self._show_install_path_start()

    def _show_install_path_start(self) -> None:
        full_path = self.install_dir_input.text()
        self.install_dir_input.setToolTip(
            f"完整路径：{full_path}\n长路径会优先显示左侧开头，右侧超出部分隐藏。"
        )
        self.install_dir_input.setCursorPosition(0)
        self.install_dir_input.deselect()

    def _finish(self) -> None:
        if self.launch_after_install_check.isChecked():
            self._launch_installed_app()
        self.window.close()
        self.qt_app.quit()

    def _finish_without_launch(self) -> None:
        self.launch_after_install_check.setChecked(False)
        self.window.close()
        self.qt_app.quit()

    def _launch_installed_app(self) -> None:
        if self.app_exe is None:
            return
        try:
            _launch_app(self.app_exe, self.app_exe.parent)
            self.launch_after_install_check.setChecked(False)
        except Exception as exc:
            self._message_error(f"无法启动应用：{exc}")

    def _close_event(self, event: object) -> None:
        if self.is_installing:
            self._message_error("安装正在进行，请稍候。")
            event.ignore()
            return
        if self._progress_timer is not None:
            self._progress_timer.stop()
        if self._motion_timer is not None:
            self._motion_timer.stop()
        self._motion_widgets.clear()
        self._progress_visual = None
        event.accept()
        self.qt_app.quit()

    def _center_window(self) -> None:
        screen = self.window.screen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        frame = self.window.frameGeometry()
        frame.moveCenter(geometry.center())
        self.window.move(frame.topLeft())

    def _set_single_page(self, panel: object) -> None:
        while self.stack.count():
            old = self.stack.widget(0)
            for reusable in (
                self.install_dir_input,
                self.desktop_shortcut_check,
                self.start_on_boot_check,
                self.launch_after_install_check,
                self.progress_bar,
                self.progress_label,
            ):
                try:
                    if old.isAncestorOf(reusable):
                        reusable.setParent(self.stack)
                except RuntimeError:
                    continue
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.addWidget(panel)
        self.stack.setCurrentWidget(panel)
        self._fade_in(panel)

    def _fade_in(self, widget: object) -> None:
        effect = self.QtWidgets.QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)

        def start_animation() -> None:
            try:
                end_pos = widget.pos()
                widget.move(end_pos + self.QtCore.QPoint(0, 10))
                group = self.QtCore.QParallelAnimationGroup(widget)

                opacity = self.QtCore.QPropertyAnimation(effect, b"opacity", group)
                opacity.setDuration(260)
                opacity.setStartValue(0.0)
                opacity.setEndValue(1.0)
                opacity.setEasingCurve(self.QtCore.QEasingCurve.Type.OutCubic)

                position = self.QtCore.QPropertyAnimation(widget, b"pos", group)
                position.setDuration(320)
                position.setStartValue(end_pos + self.QtCore.QPoint(0, 10))
                position.setEndValue(end_pos)
                position.setEasingCurve(self.QtCore.QEasingCurve.Type.OutCubic)

                group.addAnimation(opacity)
                group.addAnimation(position)
                widget._fade_animation = group  # type: ignore[attr-defined]
                group.start(self.QtCore.QAbstractAnimation.DeletionPolicy.KeepWhenStopped)
            except RuntimeError:
                return

        self.QtCore.QTimer.singleShot(0, start_animation)

    def _set_stage(self, index: int, *, error: bool = False) -> None:
        for item_index, label in enumerate(self._stage_labels):
            label.setProperty("active", item_index == index)
            label.setProperty("done", item_index < index)
            label.setProperty("error", error and item_index == index)
            label.style().unpolish(label)
            label.style().polish(label)

    def _page_panel(self) -> object:
        panel = self.QtWidgets.QFrame()
        panel.setObjectName("InstallerPanel")
        return panel

    def _card(self) -> object:
        card = self.QtWidgets.QFrame()
        card.setObjectName("InstallerCard")
        return card

    def _spec_row(self, label: str, value: str) -> object:
        row = self.QtWidgets.QFrame()
        row.setObjectName("SpecRow")
        layout = self.QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 11, 0, 11)
        layout.setSpacing(20)

        label_widget = self.QtWidgets.QLabel(label)
        label_widget.setObjectName("SpecLabel")
        label_widget.setMinimumWidth(72)
        value_widget = self.QtWidgets.QLabel(value)
        value_widget.setObjectName("SpecValue")
        value_widget.setWordWrap(True)
        value_widget.setAlignment(
            self.QtCore.Qt.AlignmentFlag.AlignRight | self.QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(label_widget)
        layout.addWidget(value_widget, 1)
        return row

    def _motion_panel(self, title: str, subtitle: str, *, mode: str) -> object:
        QtCore = self.QtCore
        QtWidgets = self.QtWidgets
        owner = self

        class MotionCanvas(QtWidgets.QWidget):
            def paintEvent(self, event: object) -> None:
                owner._paint_motion(self, event)

        panel = QtWidgets.QFrame()
        panel.setObjectName("MotionPanel")
        panel.setProperty("motionMode", mode)
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(28, 28, 28, 26)
        layout.setSpacing(0)

        heading_row = QtWidgets.QHBoxLayout()
        heading_row.setSpacing(10)
        state_dot = QtWidgets.QLabel("●")
        state_dot.setObjectName("MotionStateDot")
        state_dot.setProperty("complete", mode == "complete")
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("MotionTitle")
        heading_row.addWidget(state_dot)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        layout.addLayout(heading_row)
        layout.addSpacing(7)

        body = QtWidgets.QLabel(subtitle)
        body.setObjectName("MotionSubtitle")
        body.setWordWrap(True)
        layout.addWidget(body)
        layout.addSpacing(18)

        canvas = MotionCanvas()
        canvas.setObjectName("MotionCanvas")
        canvas.setProperty("motionMode", mode)
        canvas.setProperty("progress", 100 if mode == "complete" else self.progress_value)
        canvas.setMinimumHeight(250)
        canvas.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        canvas.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        panel.motion_canvas = canvas  # type: ignore[attr-defined]
        self._motion_widgets.append(canvas)
        layout.addWidget(canvas, 1)
        return panel

    def _update_progress_visual(self, value: int) -> None:
        panel = self._progress_visual
        if panel is None:
            return
        try:
            canvas = getattr(panel, "motion_canvas", None)
            if canvas is None:
                return
            canvas.setProperty("progress", max(0, min(100, int(value))))
            canvas.update()
        except RuntimeError:
            self._progress_visual = None

    def _advance_motion(self) -> None:
        self._motion_tick = (self._motion_tick + 1) % 1_000_000
        active_widgets: list[object] = []
        for widget in self._motion_widgets:
            try:
                widget.update()
                active_widgets.append(widget)
            except RuntimeError:
                continue
        self._motion_widgets = active_widgets

    def _paint_motion(self, widget: object, _event: object) -> None:
        QtCore = self.QtCore
        QtGui = self.QtGui
        painter = QtGui.QPainter(widget)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        width = max(1, widget.width())
        height = max(1, widget.height())
        bounds = QtCore.QRectF(widget.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        background_path = QtGui.QPainterPath()
        background_path.addRoundedRect(bounds, 18, 18)
        painter.setClipPath(background_path)

        background = QtGui.QLinearGradient(0, 0, width, height)
        background.setColorAt(0.0, QtGui.QColor("#f8fbfb"))
        background.setColorAt(0.58, QtGui.QColor("#eff7f6"))
        background.setColorAt(1.0, QtGui.QColor("#edf4f6"))
        painter.fillPath(background_path, background)

        center = QtCore.QPointF(width * 0.5, height * 0.54)
        base_radius = min(width, height) * 0.205
        pulse = (math.sin(self._motion_tick * 0.055) + 1.0) * 0.5

        glow_radius = base_radius * (2.05 + pulse * 0.08)
        glow = QtGui.QRadialGradient(center, glow_radius)
        glow_center = QtGui.QColor(15, 127, 114, 24 + int(pulse * 10))
        glow_edge = QtGui.QColor(15, 127, 114, 0)
        glow.setColorAt(0.0, glow_center)
        glow.setColorAt(1.0, glow_edge)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for ratio, alpha in ((1.42, 38), (1.78, 24)):
            radius = base_radius * ratio
            painter.setPen(QtGui.QPen(QtGui.QColor(15, 127, 114, alpha), 1.0))
            painter.drawEllipse(center, radius, radius)

        orbit_radius = base_radius * 1.78
        orbit_rect = QtCore.QRectF(
            center.x() - orbit_radius,
            center.y() - orbit_radius,
            orbit_radius * 2,
            orbit_radius * 2,
        )
        rotation = self._motion_tick * 1.5
        painter.setPen(
            QtGui.QPen(
                QtGui.QColor(15, 127, 114, 116),
                2.0,
                QtCore.Qt.PenStyle.SolidLine,
                QtCore.Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawArc(orbit_rect, int((92 - rotation) * 16), -82 * 16)
        painter.setPen(
            QtGui.QPen(
                QtGui.QColor(52, 120, 201, 82),
                1.6,
                QtCore.Qt.PenStyle.SolidLine,
                QtCore.Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawArc(orbit_rect, int((274 - rotation) * 16), -38 * 16)

        dot_specs = ((0.0, "#0f7f72", 4.0), (2.35, "#f0c96c", 3.1), (4.2, "#3478c9", 2.8))
        orbit_angle = math.radians(rotation)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        for offset, color, dot_radius in dot_specs:
            angle = orbit_angle + offset
            point = QtCore.QPointF(
                center.x() + math.cos(angle) * orbit_radius,
                center.y() + math.sin(angle) * orbit_radius,
            )
            painter.setBrush(QtGui.QColor(color))
            painter.drawEllipse(point, dot_radius, dot_radius)

        ring_rect = QtCore.QRectF(
            center.x() - base_radius,
            center.y() - base_radius,
            base_radius * 2,
            base_radius * 2,
        )
        track_pen = QtGui.QPen(
            QtGui.QColor("#d9e7e6"),
            8.0,
            QtCore.Qt.PenStyle.SolidLine,
            QtCore.Qt.PenCapStyle.RoundCap,
        )
        painter.setPen(track_pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring_rect)

        mode = str(widget.property("motionMode") or "progress")
        progress = 100 if mode == "complete" else int(widget.property("progress") or 0)
        progress = max(0, min(100, progress))
        if progress:
            progress_pen = QtGui.QPen(
                QtGui.QColor("#0f7f72"),
                8.0,
                QtCore.Qt.PenStyle.SolidLine,
                QtCore.Qt.PenCapStyle.RoundCap,
            )
            painter.setPen(progress_pen)
            painter.drawArc(ring_rect, 90 * 16, -int(360 * 16 * progress / 100))

        inner_radius = base_radius * 0.63
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 210), 1))
        painter.setBrush(QtGui.QColor(255, 255, 255, 232))
        painter.drawEllipse(center, inner_radius, inner_radius)

        if mode == "complete":
            check = QtGui.QPainterPath()
            check.moveTo(center.x() - base_radius * 0.27, center.y() + base_radius * 0.01)
            check.lineTo(center.x() - base_radius * 0.07, center.y() + base_radius * 0.21)
            check.lineTo(center.x() + base_radius * 0.31, center.y() - base_radius * 0.24)
            painter.setPen(
                QtGui.QPen(
                    QtGui.QColor("#0f7f72"),
                    5.0,
                    QtCore.Qt.PenStyle.SolidLine,
                    QtCore.Qt.PenCapStyle.RoundCap,
                    QtCore.Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPath(check)
        else:
            core_radius = max(6.0, base_radius * 0.11)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(15, 127, 114, 54), 1.4))
            painter.drawEllipse(center, core_radius * 2.05, core_radius * 2.05)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#0f7f72"))
            painter.drawEllipse(center, core_radius, core_radius)

        painter.setClipping(False)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor("#d8e6e6"), 1))
        painter.drawPath(background_path)

    def _section_title(self, title: str, subtitle: str) -> object:
        QtWidgets = self.QtWidgets
        wrap = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("SectionTitle")
        subtitle_label = QtWidgets.QLabel(subtitle)
        subtitle_label.setObjectName("InstallerSubtitle")
        subtitle_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)
        return wrap

    def _field_label(self, text: str) -> object:
        label = self.QtWidgets.QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _fact_tile(self, title: str, body: str, accent: bool) -> object:
        QtWidgets = self.QtWidgets
        tile = self._card()
        tile.setProperty("accent", accent)
        layout = QtWidgets.QVBoxLayout(tile)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(8)
        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("FactTitle")
        body_label = QtWidgets.QLabel(body)
        body_label.setObjectName("InstallerSubtitle")
        body_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(body_label)
        return tile

    def _step_chip(self, number: int, title: str, desc: str) -> object:
        QtWidgets = self.QtWidgets
        chip = QtWidgets.QFrame()
        chip.setObjectName("StepChip")
        layout = QtWidgets.QHBoxLayout(chip)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        badge = QtWidgets.QLabel(str(number))
        badge.setObjectName("StepBadge")
        badge.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(badge)
        texts = QtWidgets.QVBoxLayout()
        texts.setSpacing(2)
        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("StepTitle")
        desc_label = QtWidgets.QLabel(desc)
        desc_label.setObjectName("StepDesc")
        texts.addWidget(title_label)
        texts.addWidget(desc_label)
        layout.addLayout(texts, 1)
        return chip

    def _button(self, text: str, variant: str) -> object:
        button = self.QtWidgets.QPushButton(text)
        button.setCursor(self.QtCore.Qt.CursorShape.PointingHandCursor)
        button.setProperty("variant", variant)
        button.setMinimumHeight(44 if variant != "primary" else 48)
        return button

    def _make_checkbox_class(self) -> type:
        QtCore = self.QtCore
        QtGui = self.QtGui
        QtWidgets = self.QtWidgets

        class InstallerCheckBox(QtWidgets.QCheckBox):
            def __init__(self, text: str) -> None:
                super().__init__(text)
                self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
                self.setMinimumHeight(30)

            def sizeHint(self) -> object:
                metrics = self.fontMetrics()
                return QtCore.QSize(metrics.horizontalAdvance(self.text()) + 42, max(30, metrics.height() + 10))

            def paintEvent(self, _event: object) -> None:
                painter = QtGui.QPainter(self)
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                box_size = 20
                box_x = 1
                box_y = (self.height() - box_size) / 2
                box = QtCore.QRectF(box_x, box_y, box_size, box_size)
                enabled = self.isEnabled()
                checked = self.isChecked()

                if checked and enabled:
                    fill = QtGui.QColor("#0f7f72")
                    border = QtGui.QColor("#0f7f72")
                    text = QtGui.QColor("#344555")
                elif checked:
                    fill = QtGui.QColor("#e8edef")
                    border = QtGui.QColor("#d7e0e4")
                    text = QtGui.QColor("#8b98a4")
                else:
                    fill = QtGui.QColor("#ffffff")
                    border = QtGui.QColor("#8b98a4" if enabled else "#d7e0e4")
                    text = QtGui.QColor("#344555" if enabled else "#8b98a4")

                painter.setPen(QtGui.QPen(border, 1.8))
                painter.setBrush(fill)
                painter.drawRoundedRect(box, 6, 6)

                if checked and enabled:
                    check = QtGui.QPainterPath()
                    check.moveTo(box_x + 5.0, box_y + 10.5)
                    check.lineTo(box_x + 8.4, box_y + 14.0)
                    check.lineTo(box_x + 15.5, box_y + 6.2)
                    painter.setPen(
                        QtGui.QPen(
                            QtGui.QColor("#ffffff"),
                            2.4,
                            QtCore.Qt.PenStyle.SolidLine,
                            QtCore.Qt.PenCapStyle.RoundCap,
                            QtCore.Qt.PenJoinStyle.RoundJoin,
                        )
                    )
                    painter.drawPath(check)

                painter.setPen(text)
                painter.setFont(self.font())
                painter.drawText(
                    QtCore.QRectF(32, 0, self.width() - 32, self.height()),
                    QtCore.Qt.AlignmentFlag.AlignVCenter,
                    self.text(),
                )

        return InstallerCheckBox

    def _message_error(self, message: str) -> None:
        self.QtWidgets.QMessageBox.critical(self.window, PRODUCT_NAME, message)

    def _make_install_stage_class(self) -> type:
        QtCore = self.QtCore
        QtGui = self.QtGui
        QtWidgets = self.QtWidgets

        class InstallStage(QtWidgets.QFrame):
            def __init__(self, mode: str) -> None:
                super().__init__()
                self.mode = mode
                self.tick = 0
                self.setObjectName("InstallStage")
                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, False)
                self._timer = QtCore.QTimer(self)
                self._timer.timeout.connect(self._step)
                self._timer.start(50)

            def _step(self) -> None:
                self.tick += 1
                self.update()

            def paintEvent(self, _event: object) -> None:
                painter = QtGui.QPainter(self)
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                rect = self.rect()
                width = max(1, rect.width())
                height = max(1, rect.height())
                bounds = QtCore.QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
                stage_path = QtGui.QPainterPath()
                stage_path.addRoundedRect(bounds, 28, 28)
                painter.setClipPath(stage_path)

                gradient = QtGui.QLinearGradient(0, 0, width, height)
                if self.mode == "error":
                    gradient.setColorAt(0.0, QtGui.QColor("#fffdfd"))
                    gradient.setColorAt(0.55, QtGui.QColor("#fff7f8"))
                    gradient.setColorAt(1.0, QtGui.QColor("#f4f8f8"))
                    accent_rgb = (189, 54, 82)
                else:
                    gradient.setColorAt(0.0, QtGui.QColor("#fdfefe"))
                    gradient.setColorAt(0.56, QtGui.QColor("#f7faf9"))
                    gradient.setColorAt(1.0, QtGui.QColor("#f1f7f6"))
                    accent_rgb = (15, 127, 114)
                painter.fillPath(stage_path, gradient)

                glow_center = QtCore.QPointF(
                    width * (0.78 + math.sin(self.tick * 0.018) * 0.012),
                    height * 0.20,
                )
                glow_radius = min(width, height) * (0.38 if self.mode == "error" else 0.48)
                glow = QtGui.QRadialGradient(glow_center, glow_radius)
                glow_start = QtGui.QColor(*accent_rgb, 24 if self.mode != "error" else 12)
                glow_end = QtGui.QColor(*accent_rgb, 0)
                glow.setColorAt(0.0, glow_start)
                glow.setColorAt(1.0, glow_end)
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(glow)
                painter.drawEllipse(glow_center, glow_radius, glow_radius)

                for row, (y_ratio, alpha, amplitude) in enumerate(((0.80, 14, 18.0),)):
                    phase = self.tick * 0.022 + row * 1.4
                    y = height * y_ratio
                    path = QtGui.QPainterPath()
                    path.moveTo(-36, y + math.sin(phase) * amplitude)
                    path.cubicTo(
                        width * 0.23,
                        y + math.cos(phase + 0.4) * amplitude,
                        width * 0.56,
                        y - math.sin(phase + 0.7) * amplitude,
                        width + 36,
                        y + math.cos(phase + 1.0) * amplitude,
                    )
                    painter.setPen(QtGui.QPen(QtGui.QColor(*accent_rgb, alpha), 1.4))
                    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)

                painter.setClipping(False)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.setPen(QtGui.QPen(QtGui.QColor("#d8e2e6"), 1))
                painter.drawPath(stage_path)

                return None

        return InstallStage


INSTALLER_STYLE_SHEET = """
QWidget#InstallerRoot {
    background: #edf2f3;
    color: #172126;
}

QWidget#InstallerShell {
    background: #f8faf9;
}

QFrame#InstallerHeader {
    background: transparent;
    border: 0;
}

QLabel#InstallerBrandLogo {
    background: transparent;
}

QLabel#InstallerBrandName {
    color: #172126;
    font-size: 13px;
    font-weight: 800;
}

QFrame#BrandSeparator {
    background: #d8e3e3;
    border: 0;
}

QLabel#InstallerBrandContext,
QLabel#InstallerRelease {
    color: #6a777d;
    font-size: 12px;
}

QLabel#InstallerStatusChip {
    color: #0f7f72;
    background: #edf7f5;
    border: 0;
    border-radius: 14px;
    padding: 6px 13px;
    font-weight: 700;
}

QWidget#StageTrack {
    background: transparent;
    border: 0;
    border-bottom: 1px solid #dfe8e8;
}

QFrame#InstallStage {
    background: transparent;
    border: 0;
    border-radius: 28px;
}

QLabel#InstallerTitle {
    color: #172126;
    font-size: 36px;
    font-weight: 800;
}

QLabel#InstallerSubtitle {
    color: #66747b;
    font-size: 14px;
}

QLabel#SectionKicker {
    color: #0f7f72;
    font-size: 11px;
    font-weight: 800;
}

QFrame#GlassPanel {
    background: rgba(255, 255, 255, 246);
    border: 1px solid #d8e4e3;
    border-radius: 20px;
}

QLabel#StageLabel {
    background: transparent;
    color: #6f7d82;
    border: 0;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 9px 4px 8px 4px;
    font-size: 12px;
    font-weight: 700;
}

QLabel#StageLabel[active="true"] {
    color: #0f7f72;
    background: transparent;
    border-bottom-color: #0f7f72;
}

QLabel#StageLabel[done="true"] {
    color: #52716e;
    background: transparent;
    border-bottom-color: #a9c8c4;
}

QLabel#StageLabel[error="true"] {
    color: #a92540;
    background: transparent;
    border-bottom-color: #bd3652;
}

QFrame#SetupSpecs {
    background: transparent;
    border: 0;
    border-top: 1px solid #dfe8e7;
}

QFrame#SpecRow {
    background: transparent;
    border: 0;
    border-bottom: 1px solid #dfe8e7;
}

QLabel#SpecLabel {
    color: #6f7d82;
    font-size: 12px;
    font-weight: 700;
}

QLabel#SpecValue {
    color: #263a3e;
    font-size: 13px;
    font-weight: 600;
}

QLabel#SetupFootnote {
    color: #7f8c91;
    font-size: 10px;
}

QFrame#OptionDivider {
    background: #e2e9e9;
    border: 0;
}

QLabel#AssuranceLabel {
    color: #6d7b80;
    font-size: 12px;
}

QLabel#FieldLabel {
    color: #33474b;
    font-weight: 800;
}

QLabel#SectionTitle,
QLabel#ProgressTitle {
    color: #172126;
    font-size: 30px;
    font-weight: 800;
}

QLabel#ProgressPercent {
    color: #172d31;
    font-size: 70px;
    font-weight: 700;
}

QLabel#ProgressPercentSuffix {
    color: #718084;
    font-size: 20px;
    font-weight: 600;
    padding-bottom: 11px;
}

QLabel#ProgressHint {
    color: #748186;
    font-size: 13px;
}

QLabel#PrivacyNote {
    color: #0f7f72;
    font-size: 12px;
    font-weight: 700;
}

QFrame#MotionPanel {
    background: rgba(255, 255, 255, 244);
    border: 1px solid #d5e3e1;
    border-radius: 20px;
}

QLabel#MotionTitle {
    color: #172126;
    font-size: 22px;
    font-weight: 800;
}

QLabel#MotionSubtitle {
    color: #68767b;
    font-size: 13px;
}

QLabel#MotionStateDot {
    color: #0f7f72;
    font-size: 10px;
}

QWidget#MotionCanvas {
    background: transparent;
    border: 0;
}

QLabel#ErrorGlyph {
    color: #a92540;
    background: #fdecef;
    border: 1px solid #f3cbd4;
    border-radius: 27px;
    font-size: 28px;
    font-weight: 900;
}

QPlainTextEdit#ErrorDetails {
    color: #7f2438;
    background: #fff7f8;
    border: 1px solid #f1d2d9;
    border-radius: 14px;
    padding: 12px;
    selection-background-color: #bd3652;
    selection-color: #ffffff;
}

QCheckBox {
    background: transparent;
    border: 0;
}

QLineEdit {
    background: #ffffff;
    color: #23373b;
    border: 1px solid #d3dfde;
    border-radius: 12px;
    padding: 11px 14px;
    min-height: 28px;
    selection-background-color: #0f7f72;
    selection-color: #ffffff;
}

QLineEdit:focus {
    border: 2px solid #0f7f72;
    padding: 10px 13px;
}

QLineEdit:disabled {
    color: #8b98a4;
    background: #f1f4f5;
}

QPushButton {
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 10px 22px;
    font-weight: 800;
}

QPushButton[variant="primary"] {
    background: #0f7f72;
    color: #ffffff;
}

QPushButton[variant="primary"]:hover {
    background: #085f57;
}

QPushButton[variant="primary"]:pressed {
    background: #064f49;
}

QPushButton[variant="secondary"] {
    background: rgba(255, 255, 255, 230);
    color: #34484c;
    border-color: #d3dfde;
}

QPushButton[variant="secondary"]:hover {
    color: #0f7f72;
    background: #ffffff;
    border-color: #9bc8c2;
}

QPushButton:focus {
    border: 2px solid #65aaa2;
    padding: 9px 21px;
}

QPushButton:disabled {
    color: #9aa5ae;
    background: #edf1f2;
    border-color: #e1e7ea;
}

QProgressBar {
    background: #dfe8e7;
    border: 0;
    border-radius: 3px;
    min-height: 5px;
    max-height: 5px;
}

QProgressBar::chunk {
    background: #0f7f72;
    border-radius: 3px;
}

QMessageBox,
QFileDialog {
    background: #f8faf9;
    color: #172126;
}

QMessageBox QLabel,
QFileDialog QLabel {
    color: #34484c;
}

QToolTip {
    color: #23373b;
    background: #ffffff;
    border: 1px solid #d3dfde;
    padding: 6px;
}
"""


def _payload_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    payload = base / PAYLOAD_NAME
    if payload.exists():
        return payload

    side_by_side = Path(sys.argv[0]).resolve().with_name(PAYLOAD_NAME)
    if side_by_side.exists():
        return side_by_side

    raise FileNotFoundError(f"missing installer payload: {PAYLOAD_NAME}")


def _bundled_app_icon() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = [
        base / APP_ICON_NAME,
        base / "assets" / "icons" / APP_ICON_NAME,
        Path(__file__).resolve().parents[2] / "assets" / "icons" / APP_ICON_NAME,
        Path(sys.argv[0]).resolve().with_name(APP_ICON_NAME),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _default_install_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Programs" / APP_FOLDER


def _validate_payload(payload: Path) -> None:
    with zipfile.ZipFile(payload) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PAYLOAD_FILES:
            raise RuntimeError(f"payload contains too many entries: {len(infos)}")
        total_size = sum(info.file_size for info in infos)
        if total_size > MAX_PAYLOAD_UNCOMPRESSED_BYTES:
            raise RuntimeError(f"payload uncompressed size is too large: {total_size} bytes")
        all_names = [info.filename for info in infos]
        seen_names: set[str] = set()
        seen_windows_names: set[str] = set()
        for name in all_names:
            if name in seen_names:
                raise RuntimeError(f"payload contains duplicate path: {name}")
            seen_names.add(name)
            windows_name = name.replace("\\", "/").rstrip("/").casefold()
            if windows_name in seen_windows_names:
                raise RuntimeError(f"payload contains Windows path collision: {name}")
            seen_windows_names.add(windows_name)
        for info in infos:
            _validate_payload_member(info)
        names = {info.filename for info in infos if not info.is_dir()}
        _validate_payload_manifest(archive, names)
        product_tier = _payload_product_tier(archive, names)
        model_profile = _payload_model_profile(archive, names)
        required_files = {
            "VoiceInputAssistant.exe": "main application",
            "Uninstall.exe": "uninstaller",
            "tools/VoiceInputDiagnose/VoiceInputDiagnose.exe": "ASR diagnosis tool",
            "tools/VoiceInputEvaluate/VoiceInputEvaluate.exe": "accuracy evaluation tool",
            "tools/VoiceInputStreamEvaluate/VoiceInputStreamEvaluate.exe": "streaming accuracy evaluation tool",
            "tools/VoiceInputHealth/VoiceInputHealth.exe": "runtime health tool",
            "assets/product.json": "product profile",
            "docs/使用帮助.md": "help document",
            "docs/验收清单.md": "acceptance checklist",
            "README.md": "readme",
        }
        required_files.update(_payload_required_funasr_models(archive, names, model_profile))
        required_files.update(_payload_required_lexicons(archive, names, product_tier))
        for required, label in required_files.items():
            if required not in names:
                raise RuntimeError(f"payload does not contain {label}: {required}")
            if archive.getinfo(required).file_size <= 0:
                raise RuntimeError(f"payload contains empty {label}: {required}")
        if model_profile == "full":
            ffmpeg_files = [
                name
                for name in names
                if name.startswith("_internal/imageio_ffmpeg/binaries/") and name.endswith(".exe")
            ]
            if not ffmpeg_files:
                raise RuntimeError("payload does not contain bundled FFmpeg binary")
            if not any(archive.getinfo(name).file_size > 0 for name in ffmpeg_files):
                raise RuntimeError("payload contains empty bundled FFmpeg binary")


def _validate_payload_member(info: zipfile.ZipInfo) -> None:
    if "\\" in info.filename:
        raise RuntimeError(f"payload contains non-canonical path: {info.filename}")
    name = info.filename
    path = PurePosixPath(name)
    raw_parts = name.rstrip("/").split("/")
    if (
        not name
        or name.startswith("/")
        or "//" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(
            part in {"", "."}
            or part.endswith((" ", "."))
            or ":" in part
            or any(ord(character) < 32 for character in part)
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in raw_parts
        )
    ):
        raise RuntimeError(f"payload contains unsafe path: {info.filename}")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise RuntimeError(f"payload contains symbolic link: {info.filename}")


def _validate_payload_manifest(archive: zipfile.ZipFile, names: set[str]) -> None:
    if PAYLOAD_MANIFEST_NAME not in names:
        raise RuntimeError(f"payload does not contain integrity manifest: {PAYLOAD_MANIFEST_NAME}")
    if archive.getinfo(PAYLOAD_MANIFEST_NAME).file_size <= 0:
        raise RuntimeError("payload integrity manifest is empty")
    try:
        payload = json.loads(archive.read(PAYLOAD_MANIFEST_NAME).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("payload integrity manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PAYLOAD_MANIFEST_SCHEMA_VERSION
        or payload.get("algorithm") != "sha256"
        or not isinstance(payload.get("files"), list)
    ):
        raise RuntimeError("payload integrity manifest is invalid")

    expected: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(payload["files"], start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"payload integrity manifest item {index} is invalid")
        name = str(item.get("path") or "").replace("\\", "/")
        size = item.get("size")
        sha256 = str(item.get("sha256") or "").strip().lower()
        path = PurePosixPath(name)
        if (
            not name
            or name == PAYLOAD_MANIFEST_NAME
            or path.is_absolute()
            or ".." in path.parts
            or any(part in {"", "."} or part.endswith(":") for part in path.parts)
            or name in expected
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeError(f"payload integrity manifest item {index} is invalid")
        expected[name] = (size, sha256)

    actual = names - {PAYLOAD_MANIFEST_NAME}
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if missing:
        raise RuntimeError(f"payload integrity manifest references missing file: {missing[0]}")
    if extra:
        raise RuntimeError(f"payload integrity manifest omits file: {extra[0]}")
    for name in sorted(actual):
        expected_size, expected_sha256 = expected[name]
        info = archive.getinfo(name)
        if info.file_size != expected_size:
            raise RuntimeError(f"payload integrity size mismatch: {name}")
        digest = hashlib.sha256()
        with archive.open(info) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"payload integrity SHA-256 mismatch: {name}")


def _payload_product_tier(archive: zipfile.ZipFile, names: set[str]) -> str:
    payload = _payload_product_profile(archive, names)
    product_tier = str(payload.get("product_tier") or payload.get("tier") or "enterprise").strip().lower()
    if product_tier not in {"basic", "standard", "enterprise"}:
        raise RuntimeError(f"payload product tier is invalid: {product_tier}")
    return product_tier


def _payload_model_profile(archive: zipfile.ZipFile, names: set[str]) -> str:
    profile = _payload_product_profile(archive, names)
    model_profile = str(profile.get("model_profile") or "").strip().lower()
    if model_profile:
        if model_profile not in {"streaming", "full"}:
            raise RuntimeError(f"payload model profile is invalid: {model_profile}")
        return model_profile
    raw_roles = profile.get("funasr_models")
    if isinstance(raw_roles, dict):
        if raw_roles.get("offline") and raw_roles.get("vad"):
            return "full"
        if raw_roles.get("streaming"):
            return "streaming"
    return "full"


def _payload_model_profile_from_path(payload: Path) -> str:
    try:
        with zipfile.ZipFile(payload) as archive:
            return _payload_model_profile(archive, set(archive.namelist()))
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return "full"


def _payload_product_profile(archive: zipfile.ZipFile, names: set[str]) -> dict[str, object]:
    manifest_name = "assets/product.json"
    if manifest_name not in names:
        raise RuntimeError(f"payload does not contain product profile: {manifest_name}")
    try:
        payload = json.loads(archive.read(manifest_name).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("payload product profile is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("payload product profile is invalid")
    return payload


def _payload_required_funasr_models(
    archive: zipfile.ZipFile,
    names: set[str],
    model_profile: str,
) -> dict[str, str]:
    profile = _payload_product_profile(archive, names)
    raw_models = profile.get("bundled_funasr_models")
    raw_roles = profile.get("funasr_models")
    roles = {
        role: str(raw_roles.get(role) or "").strip()
        for role in ("streaming", "offline", "vad", "punc", "speaker")
    } if isinstance(raw_roles, dict) else {}
    if not roles:
        roles = {
            "streaming": "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        }
        if model_profile == "full":
            roles.update(
                {
                    "offline": "speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
                    "vad": "speech_fsmn_vad_zh-cn-16k-common-pytorch",
                    "punc": "punc_ct-transformer_cn-en-common-vocab471067-large",
                    "speaker": "speech_campplus_sv_zh-cn_16k-common",
                }
            )
    required_roles = (
        ("streaming",)
        if model_profile == "streaming"
        else ("streaming", "offline", "vad", "punc", "speaker")
    )
    missing_roles = [role for role in required_roles if not roles.get(role)]
    if missing_roles:
        raise RuntimeError(
            f"payload {model_profile} model profile is missing FunASR role(s): {', '.join(missing_roles)}"
        )
    if model_profile == "streaming":
        unexpected_roles = [role for role in ("offline", "vad", "punc", "speaker") if roles.get(role)]
        if unexpected_roles:
            raise RuntimeError(
                "payload streaming model profile unexpectedly includes FunASR role(s): "
                + ", ".join(unexpected_roles)
            )
    if isinstance(raw_models, list):
        model_dirs = tuple(str(item or "").strip() for item in raw_models if str(item or "").strip())
    else:
        model_dirs = tuple(
            roles.get(role, "")
            for role in ("streaming", "offline", "vad", "punc", "speaker")
            if roles.get(role, "")
        )
    model_dirs = tuple(dict.fromkeys(model_dirs))
    for dirname in model_dirs:
        model_path = PurePosixPath(dirname.replace("\\", "/"))
        if (
            not dirname
            or model_path.is_absolute()
            or len(model_path.parts) != 1
            or model_path.name != dirname
            or dirname not in FUNASR_REQUIRED_FILES
        ):
            raise RuntimeError(f"payload product profile contains unsupported FunASR model: {dirname}")
    if roles:
        role_models = tuple(dict.fromkeys(roles[role] for role in roles if roles[role]))
        missing_models = sorted(set(role_models) - set(model_dirs))
        unused_models = sorted(set(model_dirs) - set(role_models))
        if missing_models:
            raise RuntimeError(
                "payload product profile omits model used by FunASR role: " + ", ".join(missing_models)
            )
        if unused_models:
            raise RuntimeError(
                "payload product profile includes model without a FunASR role: " + ", ".join(unused_models)
            )
    _validate_payload_funasr_capabilities(profile, model_profile, roles)

    raw_model_files = profile.get("funasr_model_files")
    if raw_model_files is not None and not isinstance(raw_model_files, dict):
        raise RuntimeError("payload product profile FunASR model file map is invalid")
    required: dict[str, str] = {}
    for dirname in model_dirs:
        label = (
            "bundled FunASR streaming model"
            if dirname == "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
            else f"bundled FunASR model {dirname}"
        )
        expected_files = FUNASR_REQUIRED_FILES[dirname]
        if isinstance(raw_model_files, dict):
            raw_files = raw_model_files.get(dirname)
            if not isinstance(raw_files, list):
                raise RuntimeError(f"payload product profile omits FunASR model file map: {dirname}")
            declared_files = tuple(str(item or "").strip() for item in raw_files)
            if set(declared_files) != set(expected_files) or len(declared_files) != len(expected_files):
                raise RuntimeError(f"payload product profile FunASR model file map is incomplete: {dirname}")
        for filename in expected_files:
            file_label = f"{label} weights" if filename == "model.pt" else f"{label} resource"
            required[f"models/funasr/{dirname}/{filename}"] = file_label
    if "models/funasr/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online/model.pt" not in required:
        raise RuntimeError("payload product profile does not include a FunASR streaming model")
    return required


def _validate_payload_funasr_capabilities(
    profile: dict[str, object],
    model_profile: str,
    roles: dict[str, str],
) -> None:
    raw_capabilities = profile.get("funasr_capabilities")
    if raw_capabilities is None:
        return
    if not isinstance(raw_capabilities, dict):
        raise RuntimeError("payload product profile FunASR capabilities are invalid")
    expected = {
        "streaming": bool(roles.get("streaming")) if roles else True,
        "offline_review": bool(roles.get("offline")) if roles else model_profile == "full",
        "vad": bool(roles.get("vad")) if roles else model_profile == "full",
        "punctuation": bool(roles.get("punc")) if roles else model_profile == "full",
        "speaker": bool(roles.get("speaker")) if roles else model_profile == "full",
        "background_review": (
            bool(roles.get("offline") and roles.get("vad"))
            if roles
            else model_profile == "full"
        ),
    }
    for capability, expected_value in expected.items():
        actual = raw_capabilities.get(capability)
        if not isinstance(actual, bool) or actual is not expected_value:
            raise RuntimeError(
                f"payload product profile capability {capability} is inconsistent with FunASR roles"
            )


def _payload_required_lexicons(
    archive: zipfile.ZipFile,
    names: set[str],
    product_tier: str,
) -> dict[str, str]:
    catalog_name = "assets/lexicons/catalog.json"
    if catalog_name not in names:
        return {
            "assets/lexicons/government_digital.json": "government lexicon",
            "assets/lexicons/official_document.json": "official document lexicon",
            "assets/lexicons/office_meeting.json": "meeting lexicon",
            "assets/lexicons/finance_budget.json": "finance budget lexicon",
            "assets/lexicons/project_engineering.json": "project engineering lexicon",
        }

    profile = _payload_product_profile(archive, names)
    catalog = _payload_lexicon_catalog(archive, catalog_name)
    enabled_ids = _payload_enabled_lexicon_ids(profile, catalog, product_tier)
    by_id = {item["id"]: item for item in catalog}
    required = {catalog_name: "lexicon catalog"}
    for lexicon_id in enabled_ids:
        item = by_id.get(lexicon_id)
        if item is None:
            raise RuntimeError(f"payload product profile references unknown bundled lexicon: {lexicon_id}")
        filename = item["filename"]
        required[f"assets/lexicons/{filename}"] = f"bundled lexicon {lexicon_id}"
    return required


def _payload_lexicon_catalog(archive: zipfile.ZipFile, catalog_name: str) -> list[dict[str, object]]:
    try:
        payload = json.loads(archive.read(catalog_name).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("payload lexicon catalog is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("lexicons"), list):
        raise RuntimeError("payload lexicon catalog is invalid")
    catalog: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(payload["lexicons"], start=1):
        if not isinstance(raw_item, dict):
            raise RuntimeError(f"payload lexicon catalog item {index} is invalid")
        lexicon_id = str(raw_item.get("id") or "").strip()
        if not lexicon_id or lexicon_id in seen:
            raise RuntimeError(f"payload lexicon catalog item {index} has invalid id")
        filename = _payload_safe_lexicon_filename(raw_item.get("filename"))
        min_tier = str(raw_item.get("min_tier") or "basic").strip().lower()
        if min_tier not in {"basic", "standard", "enterprise"}:
            raise RuntimeError(f"payload lexicon catalog item {lexicon_id} has invalid tier")
        seen.add(lexicon_id)
        catalog.append(
            {
                "id": lexicon_id,
                "filename": filename,
                "default_enabled": _payload_bool(raw_item.get("default_enabled"), default=True),
                "min_tier": min_tier,
            }
        )
    if not catalog:
        raise RuntimeError("payload lexicon catalog is empty")
    return catalog


def _payload_enabled_lexicon_ids(
    profile: dict[str, object],
    catalog: list[dict[str, object]],
    product_tier: str,
) -> tuple[str, ...]:
    rank = {"basic": 0, "standard": 1, "enterprise": 2}
    allowed = {
        str(item["id"])
        for item in catalog
        if rank[product_tier] >= rank.get(str(item.get("min_tier") or "standard"), rank["standard"])
    }
    raw_enabled = profile.get("enabled_builtin_lexicons")
    if isinstance(raw_enabled, str):
        selected = (raw_enabled,)
    elif isinstance(raw_enabled, list):
        selected = tuple(str(item) for item in raw_enabled if str(item).strip())
    else:
        selected = tuple(str(item["id"]) for item in catalog if item["default_enabled"] and str(item["id"]) in allowed)
    unavailable = [item for item in selected if item not in allowed]
    if unavailable:
        raise RuntimeError("payload product profile references unavailable bundled lexicon: " + ", ".join(unavailable))
    return tuple(dict.fromkeys(selected))


def _payload_safe_lexicon_filename(value: object) -> str:
    filename = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(filename)
    if not filename or path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise RuntimeError(f"payload lexicon catalog contains unsafe filename: {filename}")
    if path.suffix.lower() not in {".json", ".csv", ".txt"}:
        raise RuntimeError(f"payload lexicon catalog contains unsupported filename: {filename}")
    return path.as_posix()


def _payload_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return bool(value)


def _extract_payload(payload: Path, destination: Path) -> None:
    _validate_payload(payload)
    _validate_extract_destination(destination)
    staging = _extract_payload_to_staging(payload, destination)
    try:
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _extract_payload_for_install(payload: Path, destination: Path) -> None:
    _validate_payload(payload)
    _validate_install_dir_choice(destination)
    staging = _extract_payload_to_staging(payload, destination)
    backup: Path | None = None
    activated = False
    try:
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
            os.replace(destination, backup)
        os.replace(staging, destination)
        activated = True
        _run_post_install_health_check(destination)
    except Exception:
        if activated and destination.exists():
            shutil.rmtree(destination)
        if backup is not None and backup.exists():
            os.replace(backup, destination)
        raise
    else:
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _extract_payload_to_staging(payload: Path, destination: Path) -> Path:
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{destination.name}.install-{uuid.uuid4().hex}"
    try:
        staging.mkdir()
        with zipfile.ZipFile(payload) as archive:
            archive.extractall(staging)
        _validate_extracted_payload(staging)
        return staging
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _validate_extracted_payload(root: Path) -> None:
    manifest_path = root / Path(PAYLOAD_MANIFEST_NAME)
    if not manifest_path.is_file() or manifest_path.stat().st_size <= 0:
        raise RuntimeError(f"extracted payload integrity manifest is missing: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("extracted payload integrity manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PAYLOAD_MANIFEST_SCHEMA_VERSION
        or payload.get("algorithm") != "sha256"
        or not isinstance(payload.get("files"), list)
    ):
        raise RuntimeError("extracted payload integrity manifest is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for index, item in enumerate(payload["files"], start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"extracted payload integrity manifest item {index} is invalid")
        relative = str(item.get("path") or "").replace("\\", "/")
        size = item.get("size")
        sha256 = str(item.get("sha256") or "").strip().lower()
        path = PurePosixPath(relative)
        if (
            not relative
            or path.is_absolute()
            or ".." in path.parts
            or any(part in {"", "."} or part.endswith(":") for part in path.parts)
            or relative in expected
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeError(f"extracted payload integrity manifest item {index} is invalid")
        expected[relative] = (size, sha256)
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        raise RuntimeError(f"extracted payload is missing file: {missing[0]}")
    if extra:
        raise RuntimeError(f"extracted payload contains unlisted file: {extra[0]}")
    for relative, path in actual.items():
        expected_size, expected_sha256 = expected[relative]
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"extracted payload size mismatch: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"extracted payload SHA-256 mismatch: {relative}")


def _validate_extract_destination(destination: Path) -> None:
    if _is_blocked_install_dir(destination):
        raise RuntimeError("extract-only destination must be a dedicated subdirectory")
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"extract-only destination is not a directory: {destination}")
    if destination.exists() and _directory_has_content(destination):
        raise RuntimeError(
            f"extract-only destination must be empty; existing content was preserved: {destination}"
        )


def _run_post_install_health_check(install_dir: Path) -> None:
    health_exe = install_dir / "tools" / "VoiceInputHealth" / "VoiceInputHealth.exe"
    if not health_exe.is_file() or health_exe.stat().st_size <= 0:
        raise RuntimeError(f"installed health check executable not found: {health_exe}")
    with tempfile.TemporaryDirectory(prefix="voice-input-health-") as temp_dir:
        report_path = Path(temp_dir) / "report.json"
        completed = _run_hidden(
            [str(health_exe), "--json", "--output", str(report_path)],
            check=False,
            cwd=install_dir,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"post-install health check failed with exit code {completed.returncode}"
            )
        if not report_path.is_file() or report_path.stat().st_size <= 0:
            raise RuntimeError("post-install health check did not produce a report")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("post-install health check returned invalid JSON") from exc
    if not isinstance(report, dict) or report.get("status") not in {"ok", "warning"}:
        raise RuntimeError("post-install health check reported an invalid runtime state")


def _install(
    payload: Path,
    install_dir: Path,
    *,
    create_desktop_shortcut: bool,
    start_on_boot: bool,
) -> Path:
    install_dir = _normalize_install_dir(str(install_dir))
    _ensure_app_not_running()
    _extract_payload_for_install(payload, install_dir)

    app_exe = install_dir / "VoiceInputAssistant.exe"
    diagnose_exe = install_dir / "tools" / "VoiceInputDiagnose" / "VoiceInputDiagnose.exe"
    evaluate_exe = install_dir / "tools" / "VoiceInputEvaluate" / "VoiceInputEvaluate.exe"
    stream_evaluate_exe = install_dir / "tools" / "VoiceInputStreamEvaluate" / "VoiceInputStreamEvaluate.exe"
    health_exe = install_dir / "tools" / "VoiceInputHealth" / "VoiceInputHealth.exe"
    uninstall_exe = install_dir / "Uninstall.exe"
    if not app_exe.exists():
        raise RuntimeError(f"installed executable not found: {app_exe}")

    start_menu_dir = (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / PRODUCT_NAME
    )
    desktop_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    start_menu_dir.mkdir(parents=True, exist_ok=True)
    _remove_legacy_shortcuts()

    desktop_shortcut = desktop_dir / f"{PRODUCT_NAME}.lnk"
    if create_desktop_shortcut:
        _create_shortcut(
            desktop_shortcut,
            app_exe,
            working_directory=install_dir,
            icon=app_exe,
        )
    else:
        _remove_path(desktop_shortcut)

    _create_shortcut(
        start_menu_dir / f"{PRODUCT_NAME}.lnk",
        app_exe,
        working_directory=install_dir,
        icon=app_exe,
    )
    if diagnose_exe.exists():
        _create_shortcut(
            start_menu_dir / "ASR 诊断.lnk",
            diagnose_exe,
            arguments="",
            working_directory=diagnose_exe.parent,
            icon=diagnose_exe,
        )
    if health_exe.exists():
        _create_shortcut(
            start_menu_dir / "系统诊断.lnk",
            health_exe,
            arguments='--json --output "%APPDATA%\\voice_input\\health-report.json" --open',
            working_directory=health_exe.parent,
            icon=health_exe,
        )
    if evaluate_exe.exists():
        _create_shortcut(
            start_menu_dir / "准确率评测.lnk",
            evaluate_exe,
            arguments="--help",
            working_directory=evaluate_exe.parent,
            icon=evaluate_exe,
        )
    if stream_evaluate_exe.exists():
        _create_shortcut(
            start_menu_dir / "流式准确率评测.lnk",
            stream_evaluate_exe,
            arguments="--help",
            working_directory=stream_evaluate_exe.parent,
            icon=stream_evaluate_exe,
        )

    if not uninstall_exe.exists():
        raise RuntimeError(f"installed uninstaller not found: {uninstall_exe}")
    _create_shortcut(
        start_menu_dir / f"卸载 {PRODUCT_NAME}.lnk",
        uninstall_exe,
        arguments="",
        working_directory=install_dir,
        icon=uninstall_exe,
    )
    _write_uninstall_registry(install_dir, app_exe, uninstall_exe)
    _set_start_on_boot(app_exe, start_on_boot)
    return app_exe


def _normalize_install_dir(value: str) -> Path:
    path_text = value.strip().strip('"')
    if not path_text:
        raise RuntimeError("请选择安装位置。")
    return Path(path_text).expanduser().resolve()


def _validate_install_dir_choice(install_dir: Path) -> None:
    if _is_blocked_install_dir(install_dir):
        raise RuntimeError("安装位置不能是磁盘根目录、桌面或用户主目录，请选择一个专用文件夹。")
    if install_dir.exists() and not install_dir.is_dir():
        raise RuntimeError(f"安装位置不是文件夹：{install_dir}")
    if install_dir.exists() and _directory_has_content(install_dir) and not _looks_like_existing_install(install_dir):
        raise RuntimeError(
            "安装目录不是空目录。请选择空文件夹，或选择已有的 VoiceInputAssistant 安装目录。"
        )


def _prepare_install_dir(destination: Path) -> None:
    _validate_install_dir_choice(destination)
    if destination.exists():
        if _looks_like_existing_install(destination) or not _directory_has_content(destination):
            shutil.rmtree(destination)
        else:
            raise RuntimeError(
                "安装目录不是空目录。请选择空文件夹，或选择已有的 VoiceInputAssistant 安装目录。"
            )
    destination.mkdir(parents=True, exist_ok=True)


def _directory_has_content(path: Path) -> bool:
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False


def _looks_like_existing_install(path: Path) -> bool:
    return (
        (path / "VoiceInputAssistant.exe").exists()
        or (path / "models" / "funasr" / "speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online").exists()
    )


def _remove_legacy_shortcuts() -> None:
    for product_name in LEGACY_PRODUCT_NAMES:
        _remove_path(_start_menu_dir(product_name))
        _remove_path(Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / f"{product_name}.lnk")


def _is_blocked_install_dir(path: Path) -> bool:
    normalized = _normcase(path)
    if normalized == _normcase(path.anchor):
        return True

    blocked: list[Path] = []
    for env_name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(env_name)
        if value:
            blocked.append(Path(value))
    blocked.extend(
        [
            Path.home(),
            Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs",
        ]
    )
    return any(normalized == _normcase(candidate) for candidate in blocked)


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current if current.exists() else _default_install_dir().parent


def _start_menu_dir(product_name: str) -> Path:
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / product_name
    )


def _normcase(value: Path | str) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _write_uninstall_registry(install_dir: Path, app_exe: Path, uninstall_exe: Path) -> None:
    uninstall_command = f'"{uninstall_exe}"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, PRODUCT_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(app_exe))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, uninstall_command)
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, uninstall_command)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def _set_start_on_boot(app_exe: Path, enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, f'"{app_exe}"')
            return
        try:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            pass


def _ensure_app_not_running() -> None:
    try:
        completed = _run_hidden(
            [
                "tasklist.exe",
                "/FI",
                "IMAGENAME eq VoiceInputAssistant.exe",
                "/NH",
            ],
            check=False,
        )
    except FileNotFoundError:
        return
    if b"VoiceInputAssistant.exe" in completed.stdout:
        raise RuntimeError(f"请先退出正在运行的 {PRODUCT_NAME}，再重新安装或卸载。")


def _create_shortcut(
    link_path: Path,
    target: Path,
    *,
    arguments: str = "",
    working_directory: Path | None = None,
    icon: Path | None = None,
) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    shell_link: ctypes.POINTER(IShellLinkW) | None = None
    persist_file: ctypes.POINTER(IPersistFile) | None = None
    try:
        result = ctypes.oledll.ole32.CoInitialize(None)
        if result not in (0, 1):
            _raise_for_hresult(result, "无法初始化 Windows 快捷方式组件。")
        shell_link = ctypes.POINTER(IShellLinkW)()
        result = ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(CLSID_ShellLink),
            None,
            CLSCTX_INPROC_SERVER,
            ctypes.byref(IID_IShellLinkW),
            ctypes.byref(shell_link),
        )
        _raise_for_hresult(result, "无法创建 Windows 快捷方式组件。")
        if shell_link is None:
            raise RuntimeError("无法创建 Windows 快捷方式组件。")

        shell_vtbl = shell_link.contents.lpVtbl.contents
        _raise_for_hresult(shell_vtbl.SetPath(shell_link, str(target)), "无法设置快捷方式目标。")
        _raise_for_hresult(shell_vtbl.SetArguments(shell_link, arguments), "无法设置快捷方式参数。")
        _raise_for_hresult(
            shell_vtbl.SetWorkingDirectory(shell_link, str(working_directory or target.parent)),
            "无法设置快捷方式工作目录。",
        )
        _raise_for_hresult(
            shell_vtbl.SetIconLocation(shell_link, str(icon or target), 0),
            "无法设置快捷方式图标。",
        )

        persist_void = ctypes.c_void_p()
        _raise_for_hresult(
            shell_vtbl.QueryInterface(shell_link, ctypes.byref(IID_IPersistFile), ctypes.byref(persist_void)),
            "无法保存 Windows 快捷方式。",
        )
        persist_file = ctypes.cast(persist_void, ctypes.POINTER(IPersistFile))
        persist_vtbl = persist_file.contents.lpVtbl.contents
        _raise_for_hresult(persist_vtbl.Save(persist_file, str(link_path), True), "无法写入 Windows 快捷方式。")
    finally:
        if persist_file is not None:
            persist_file.contents.lpVtbl.contents.Release(persist_file)
        if shell_link is not None:
            shell_link.contents.lpVtbl.contents.Release(shell_link)
        ctypes.oledll.ole32.CoUninitialize()


def _run_hidden(
    command: list[str],
    *,
    check: bool,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if timeout is not None:
        kwargs["timeout"] = timeout
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        kwargs["startupinfo"] = startupinfo
    return subprocess.run(command, check=check, **kwargs)


def _launch_app(app_exe: Path, install_dir: Path) -> None:
    kwargs: dict[str, object] = {
        "cwd": str(install_dir),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([str(app_exe)], **kwargs)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        pass


def _show_info(message: str) -> None:
    try:
        from PySide6 import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        QtWidgets.QMessageBox.information(None, PRODUCT_NAME, message)
        app.quit()
    except Exception:
        print(message)


def _show_error(message: str) -> None:
    try:
        from PySide6 import QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
        QtWidgets.QMessageBox.critical(None, PRODUCT_NAME, message)
        app.quit()
    except Exception:
        print(message, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
