#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 分卷邮递 —— tkinter 桌面界面（Python 标准库，无第三方依赖）。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from qqvolmail import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_VOLUME_MB,
    MAX_VOLUME_MB,
    Cancelled,
    ConfigError,
    Job,
    PrepareConfig,
    QQVolMailError,
    Reporter,
    SmtpConfig,
    create_job,
    find_7zip,
    load_job,
    parse_email_list,
    send_job,
    test_smtp,
    validate_single_email,
)

# ---------------------------------------------------------------------------
# 视觉常量
# ---------------------------------------------------------------------------

BG = "#F4F7FC"
WHITE = "#FFFFFF"
BORDER = "#DDE5F0"
TEXT = "#172033"
MUTED = "#6B7A90"
FAINT = "#94A3B8"
PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
PRIMARY_DISABLED = "#A9C4FA"
PRIMARY_SOFT = "#EAF1FF"
GREEN = "#0F9F6E"
GREEN_SOFT = "#E8F8F1"
RED = "#D14343"
RED_SOFT = "#FDECEC"
AMBER = "#B7791F"
AMBER_SOFT = "#FFF7E6"
LOG_BG = "#F8FAFC"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

FORMAT_LABELS = {
    "7-Zip 原生分卷（推荐）": "auto",
    "ZIP 分卷（兼容模式）": "zip",
}
FORMAT_LABEL_BY_VALUE = {
    "auto": "7-Zip 原生分卷（推荐）",
    "7z": "7-Zip 原生分卷（推荐）",
    "zip": "ZIP 分卷（兼容模式）",
}


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        messagebox.showerror("无法打开", f"无法打开文件夹：\n{path}\n\n{exc}")


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    override = os.environ.get("QQVOLMAIL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if _is_frozen():
        return _app_dir() / "data"
    return Path(os.environ.get("APPDATA") or Path.home()) / "QQVolMail"


def _settings_path() -> Path:
    return _data_dir() / "settings.json"


def _legacy_settings_path() -> Path | None:
    """exe 便携版可读取旧版/源码版留在 APPDATA 的设置。"""
    if not _is_frozen():
        return None
    base = os.environ.get("APPDATA") or str(Path.home())
    legacy = Path(base) / "QQVolMail" / "settings.json"
    try:
        if legacy.resolve() == _settings_path().resolve():
            return None
    except Exception:
        pass
    return legacy


def _default_output_dir() -> Path:
    override = os.environ.get("QQVOLMAIL_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve().parent / "output"
    if _is_frozen():
        return _app_dir() / "output"
    return Path.home() / "QQVolMail"


def _load_settings() -> dict:
    candidates = [_settings_path()]
    legacy = _legacy_settings_path()
    if legacy is not None:
        candidates.append(legacy)
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            continue
        except Exception:
            return {}
    return {}


def _save_settings(data: dict) -> None:
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_transform(data: bytes, protect: bool) -> bytes:
    """调用 Windows DPAPI，把数据绑定到当前 Windows 用户。"""
    if os.name != "nt":
        raise OSError("DPAPI 仅在 Windows 上可用")
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(
        len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char))
    )
    entropy = b"QQVolMail-settings-v1"
    entropy_buffer = ctypes.create_string_buffer(entropy)
    entropy_blob = _DataBlob(
        len(entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char))
    )
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise OSError(f"DPAPI 调用失败（错误码 {ctypes.get_last_error()}）")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def protect_secret(secret: str) -> str:
    """优先使用 DPAPI 加密，非 Windows 环境使用 Base64 兼容保存。"""
    raw = secret.encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi_transform(raw, True)).decode("ascii")
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect_secret(value: str) -> str:
    if value.startswith("dpapi:"):
        if os.name != "nt":
            raise ValueError("此授权码由 Windows DPAPI 加密，当前系统无法解密")
        raw = _dpapi_transform(base64.b64decode(value[6:]), False)
        return raw.decode("utf-8")
    if value.startswith("b64:"):
        return base64.b64decode(value[4:]).decode("utf-8")
    # 兼容极早期版本可能存在的明文值。
    return value


def _saved_window_geometry(settings: dict, screen_w: int, screen_h: int) -> str | None:
    """恢复上次窗口位置，但避免恢复到屏幕外或异常尺寸。"""
    window = settings.get("window")
    if not isinstance(window, dict):
        return None
    try:
        width = int(window.get("width", 0))
        height = int(window.get("height", 0))
        x = int(window.get("x", -1))
        y = int(window.get("y", -1))
    except (TypeError, ValueError):
        return None
    if width < 840 or height < 740:
        return None
    if width > screen_w or height > screen_h:
        return None
    if x < 0 or y < 0:
        return None
    if x > screen_w - 180 or y > screen_h - 120:
        return None
    return f"{width}x{height}+{x}+{y}"


# ---------------------------------------------------------------------------
# 自定义控件
# ---------------------------------------------------------------------------


class Card(tk.Frame):
    """带标题的白色卡片。"""

    def __init__(self, master: tk.Misc, title: str, hint: str | None = None):
        super().__init__(
            master,
            bg=WHITE,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
        )
        header = tk.Frame(self, bg=WHITE)
        header.pack(fill="x", padx=18, pady=(13, 3))
        tk.Label(
            header,
            text=title,
            bg=WHITE,
            fg=TEXT,
            font=(FONT, 11, "bold"),
            anchor="w",
        ).pack(side="left")
        if hint:
            tk.Label(
                header,
                text=hint,
                bg=WHITE,
                fg=FAINT,
                font=(FONT, 8),
                anchor="w",
            ).pack(side="left", padx=(10, 0), pady=(2, 0))
        self.body = tk.Frame(self, bg=WHITE)
        self.body.pack(fill="both", expand=True, padx=18, pady=(2, 13))


class HeaderIcon:
    """持有 PhotoImage 引用，避免被垃圾回收。"""

    def __init__(self, path: Path):
        self.image: tk.PhotoImage | None = None
        if path.is_file():
            try:
                original = tk.PhotoImage(file=str(path))
                # 源图 256px，界面上显示 64px。
                self.image = original.subsample(4, 4)
            except Exception:
                self.image = None


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = _load_settings()
        self._running = False
        self._cancel = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._last_job_dir: Path | None = None
        self._smtp_testing = False
        self._progress_mode = ""
        self._after_id: str | None = None
        self._auth_consent = False

        self._configure_window()
        self._build_styles()
        self._build_ui()
        self._load_settings_into_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------------ 窗口

    def _configure_window(self) -> None:
        self.root.title(f"{APP_NAME}  {APP_VERSION}")
        width, height = 960, 960
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, max(840, screen_w - 120))
        height = min(height, max(740, screen_h - 120))
        saved_geometry = _saved_window_geometry(self.settings, screen_w, screen_h)
        if saved_geometry:
            self.root.geometry(saved_geometry)
        else:
            x = max(20, (screen_w - width) // 2)
            y = max(20, (screen_h - height) // 2)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(840, 740)
        self.root.configure(bg=BG)
        icon_path = Path(__file__).resolve().parent / "assets" / "app.ico"
        if icon_path.is_file():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except Exception:
                pass

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(FONT, 10), background=WHITE, foreground=TEXT)
        style.configure(
            "TEntry",
            fieldbackground=WHITE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            insertcolor=TEXT,
            padding=(8, 7),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", PRIMARY)],
            lightcolor=[("focus", PRIMARY)],
            darkcolor=[("focus", PRIMARY)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=WHITE,
            background=WHITE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
            padding=(8, 5),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", WHITE)],
            bordercolor=[("focus", PRIMARY)],
            lightcolor=[("focus", PRIMARY)],
            darkcolor=[("focus", PRIMARY)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=WHITE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
            padding=(6, 5),
        )
        style.map(
            "TSpinbox",
            bordercolor=[("focus", PRIMARY)],
            lightcolor=[("focus", PRIMARY)],
            darkcolor=[("focus", PRIMARY)],
        )
        style.configure(
            "Primary.TButton",
            background=PRIMARY,
            foreground=WHITE,
            borderwidth=0,
            focusthickness=0,
            padding=(20, 10),
            font=(FONT, 10, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("disabled", PRIMARY_DISABLED),
                ("active", PRIMARY_HOVER),
                ("pressed", PRIMARY_HOVER),
            ],
            foreground=[("disabled", "#F5F8FF")],
        )
        style.configure(
            "Secondary.TButton",
            background=WHITE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            focusthickness=0,
            padding=(14, 9),
            font=(FONT, 10),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#F4F7FC"), ("pressed", "#EDF2FA")],
            bordercolor=[("active", "#C7D4E8")],
        )
        style.configure(
            "Ghost.TButton",
            background=WHITE,
            foreground=PRIMARY,
            borderwidth=0,
            focusthickness=0,
            padding=(7, 4),
            font=(FONT, 9),
        )
        style.map("Ghost.TButton", background=[("active", PRIMARY_SOFT)])
        style.configure(
            "Danger.TButton",
            background=RED_SOFT,
            foreground=RED,
            borderwidth=0,
            focusthickness=0,
            padding=(12, 8),
            font=(FONT, 9),
        )
        style.map("Danger.TButton", background=[("active", "#FADADA")])
        style.configure(
            "App.Horizontal.TProgressbar",
            troughcolor="#E7EEF9",
            background=PRIMARY,
            borderwidth=0,
            thickness=7,
        )
        style.configure(
            "TCheckbutton",
            background=WHITE,
            foreground=MUTED,
            font=(FONT, 9),
            focusthickness=0,
        )
        style.map("TCheckbutton", background=[("active", WHITE)])
        style.configure(
            "Small.TCheckbutton",
            background=WHITE,
            foreground=MUTED,
            font=(FONT, 8),
            focusthickness=0,
        )
        style.map("Small.TCheckbutton", background=[("active", WHITE)])
        style.configure("TSeparator", background=BORDER)

    # ------------------------------------------------------------------ 界面

    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_action_card(self.root, row=2)

        container = tk.Frame(self.root, bg=BG)
        container.grid(row=1, column=0, sticky="nsew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.main_canvas = tk.Canvas(
            container, bg=BG, highlightthickness=0, bd=0, yscrollincrement=24
        )
        scrollbar = ttk.Scrollbar(
            container, orient="vertical", command=self.main_canvas.yview
        )
        self.main_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.main_canvas.configure(yscrollcommand=scrollbar.set)

        main = tk.Frame(self.main_canvas, bg=BG)
        self._main_window = self.main_canvas.create_window(
            (0, 0), window=main, anchor="nw"
        )
        main.grid_columnconfigure(0, weight=1)
        main.bind(
            "<Configure>",
            lambda _event: self.main_canvas.configure(
                scrollregion=self.main_canvas.bbox("all")
            ),
        )
        self.main_canvas.bind(
            "<Configure>",
            lambda event: self.main_canvas.itemconfigure(
                self._main_window, width=event.width
            ),
        )

        self._build_source_card(main)
        self._build_settings_card(main)
        self._build_account_card(main)
        self._build_log_card(main)
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if isinstance(event.widget, (tk.Text, ttk.Combobox, ttk.Spinbox)):
            return
        try:
            first, last = self.main_canvas.yview()
        except tk.TclError:
            return
        if last - first >= 0.999:
            return
        delta = -1 if event.delta > 0 else 1
        self.main_canvas.yview_scroll(delta * 3, "units")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=28, pady=(20, 14))
        header.grid_columnconfigure(0, weight=1)

        left = tk.Frame(header, bg=BG)
        left.grid(row=0, column=0, sticky="w")
        icon = HeaderIcon(Path(__file__).resolve().parent / "assets" / "app.png")
        if icon.image is not None:
            label = tk.Label(left, image=icon.image, bg=BG, bd=0)
            label.image = icon.image  # type: ignore[attr-defined]
            label.pack(side="left", padx=(0, 12))
        text = tk.Frame(left, bg=BG)
        text.pack(side="left")
        tk.Label(
            text,
            text=APP_NAME,
            bg=BG,
            fg=TEXT,
            font=(FONT, 21, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            text,
            text="压缩分卷 · QQ 邮箱逐卷发送 · 断点续传",
            bg=BG,
            fg=MUTED,
            font=(FONT, 9),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))

        badge = tk.Frame(
            header,
            bg=PRIMARY_SOFT,
            highlightbackground="#D5E3FF",
            highlightthickness=1,
            padx=12,
            pady=6,
        )
        badge.grid(row=0, column=1, sticky="e")
        tk.Label(
            badge,
            text=f"SMTP · QQ 邮箱    v{APP_VERSION}",
            bg=PRIMARY_SOFT,
            fg=PRIMARY,
            font=(FONT, 8, "bold"),
        ).pack()

    def _build_source_card(self, parent: tk.Misc) -> None:
        card = Card(parent, "1  选择内容", "支持文件或文件夹 · 源文件不记录")
        card.grid(row=0, column=0, sticky="ew", padx=28, pady=(0, 11))
        body = card.body
        body.grid_columnconfigure(0, weight=1)

        tk.Label(
            body, text="文件或文件夹", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(2, 3))
        self.source_var = tk.StringVar()
        source_entry = ttk.Entry(body, textvariable=self.source_var)
        source_entry.grid(row=1, column=0, sticky="ew", padx=(0, 7))
        ttk.Button(
            body, text="选择文件", style="Secondary.TButton", command=self._choose_file,
            cursor="hand2",
        ).grid(row=1, column=1, padx=(0, 6))
        ttk.Button(
            body, text="选择文件夹", style="Secondary.TButton", command=self._choose_folder,
            cursor="hand2",
        ).grid(row=1, column=2)

        tk.Label(
            body, text="输出目录", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(9, 3))
        self.output_var = tk.StringVar()
        output_entry = ttk.Entry(body, textvariable=self.output_var)
        output_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=(0, 7))
        ttk.Button(
            body, text="浏览", style="Secondary.TButton", command=self._choose_output,
            cursor="hand2",
        ).grid(row=3, column=2, sticky="ew")

    def _build_settings_card(self, parent: tk.Misc) -> None:
        card = Card(parent, "2  分卷设置", "推荐 20 MB，稳妥低于 QQ 普通附件上限")
        card.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 11))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = tk.Frame(body, bg=WHITE)
        left.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        tk.Label(
            left, text="每卷大小", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).pack(anchor="w", pady=(2, 3))
        size_row = tk.Frame(left, bg=WHITE)
        size_row.pack(anchor="w", fill="x")
        self.volume_var = tk.StringVar(value=str(DEFAULT_VOLUME_MB))
        spinbox = ttk.Spinbox(
            size_row,
            from_=1,
            to=MAX_VOLUME_MB,
            increment=1,
            width=6,
            textvariable=self.volume_var,
            justify="center",
        )
        spinbox.pack(side="left")
        tk.Label(
            size_row, text="MB", bg=WHITE, fg=TEXT, font=(FONT, 10)
        ).pack(side="left", padx=(7, 0))
        tk.Label(
            size_row,
            text=f"（1–{MAX_VOLUME_MB} MB）",
            bg=WHITE,
            fg=FAINT,
            font=(FONT, 8),
        ).pack(side="left", padx=(4, 0))

        right = tk.Frame(body, bg=WHITE)
        right.grid(row=0, column=1, sticky="ew")
        tk.Label(
            right, text="压缩方式", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).pack(anchor="w", pady=(2, 3))
        self.format_var = tk.StringVar(value=FORMAT_LABEL_BY_VALUE["auto"])
        combo = ttk.Combobox(
            right,
            textvariable=self.format_var,
            values=list(FORMAT_LABELS.keys()),
            state="readonly",
        )
        combo.pack(fill="x")

        self.volume_hint = tk.Label(
            body,
            text="",
            bg=WHITE,
            fg=FAINT,
            font=(FONT, 8),
            anchor="w",
            justify="left",
        )
        self.volume_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.volume_var.trace_add("write", lambda *_args: self._refresh_volume_hint())
        self._refresh_volume_hint()

    def _build_account_card(self, parent: tk.Misc) -> None:
        card = Card(parent, "3  QQ 邮箱", "授权码不是 QQ 登录密码")
        card.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 11))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        sender_box = tk.Frame(body, bg=WHITE)
        sender_box.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tk.Label(
            sender_box, text="发件 QQ 邮箱", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).pack(anchor="w", pady=(2, 3))
        self.sender_var = tk.StringVar()
        ttk.Entry(sender_box, textvariable=self.sender_var).pack(fill="x")

        auth_box = tk.Frame(body, bg=WHITE)
        auth_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        auth_header = tk.Frame(auth_box, bg=WHITE)
        auth_header.pack(fill="x", pady=(2, 3))
        tk.Label(
            auth_header, text="SMTP 授权码", bg=WHITE, fg=MUTED, font=(FONT, 9)
        ).pack(side="left")
        self.show_auth_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            auth_header,
            text="显示",
            variable=self.show_auth_var,
            command=self._toggle_auth,
        ).pack(side="right")
        self.auth_var = tk.StringVar()
        self.auth_entry = ttk.Entry(auth_box, textvariable=self.auth_var, show="*")
        self.auth_entry.pack(fill="x")

        self.remember_auth_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            auth_box,
            text="记住授权码（DPAPI 加密保存）",
            variable=self.remember_auth_var,
            command=self._on_remember_auth_toggled,
            style="Small.TCheckbutton",
            cursor="hand2",
        ).pack(anchor="w", pady=(4, 0))

        tk.Label(
            body, text="收件邮箱", bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w"
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(9, 3))
        self.to_var = tk.StringVar()
        ttk.Entry(body, textvariable=self.to_var).grid(
            row=2, column=0, columnspan=2, sticky="ew"
        )

        footer = tk.Frame(body, bg=WHITE)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        link = tk.Label(
            footer,
            text="如何获取授权码？",
            bg=WHITE,
            fg=PRIMARY,
            font=(FONT, 9, "underline"),
            cursor="hand2",
        )
        link.pack(side="left")
        link.bind("<Button-1>", lambda _event: self._show_auth_help())
        tk.Label(
            footer,
            text="   ·   多个收件人用逗号分隔   ·   取消勾选会清除已保存的授权码",
            bg=WHITE,
            fg=FAINT,
            font=(FONT, 8),
        ).pack(side="left")

        self.test_btn = ttk.Button(
            footer,
            text="测试邮箱连接",
            style="Ghost.TButton",
            command=self._test_connection,
            cursor="hand2",
        )
        self.test_btn.pack(side="right")

    def _build_action_card(self, parent: tk.Misc, row: int) -> None:
        card = tk.Frame(
            parent,
            bg=WHITE,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
        )
        card.grid(row=row, column=0, sticky="ew", padx=28, pady=(0, 18))
        body = tk.Frame(card, bg=WHITE)
        body.pack(fill="x", padx=18, pady=14)

        status_row = tk.Frame(body, bg=WHITE)
        status_row.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        self.detail_var = tk.StringVar(value="选择文件并填写邮箱后即可开始")
        tk.Label(
            status_row, textvariable=self.status_var, bg=WHITE, fg=TEXT,
            font=(FONT, 10, "bold"), anchor="w",
        ).pack(side="left")
        self.detail_label = tk.Label(
            status_row, textvariable=self.detail_var, bg=WHITE, fg=MUTED,
            font=(FONT, 9), anchor="w",
        )
        self.detail_label.pack(side="left", padx=(10, 0))

        self.progress = ttk.Progressbar(
            body, style="App.Horizontal.TProgressbar", mode="indeterminate"
        )
        self.progress.pack(fill="x", pady=(9, 12))

        buttons = tk.Frame(body, bg=WHITE)
        buttons.pack(fill="x")
        ttk.Button(
            buttons, text="打开输出目录", style="Ghost.TButton",
            command=self._open_output, cursor="hand2",
        ).pack(side="left")
        ttk.Button(
            buttons, text="继续未完成任务…", style="Ghost.TButton",
            command=self._resume_job, cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(
            buttons, text="取消", style="Danger.TButton",
            command=self._cancel_task, cursor="hand2",
        )
        self.cancel_btn.pack(side="right", padx=(8, 0))
        self.cancel_btn.pack_forget()

        self.send_btn = ttk.Button(
            buttons, text="压缩并发送", style="Primary.TButton",
            command=lambda: self._start(send=True), cursor="hand2",
        )
        self.send_btn.pack(side="right")
        self.prepare_btn = ttk.Button(
            buttons, text="仅压缩分卷", style="Secondary.TButton",
            command=lambda: self._start(send=False), cursor="hand2",
        )
        self.prepare_btn.pack(side="right", padx=(0, 8))

    def _build_log_card(self, parent: tk.Misc) -> None:
        card = Card(parent, "运行日志", "失败后可从这里继续未完成任务")
        card.grid(row=3, column=0, sticky="ew", padx=28, pady=(0, 28))
        card.body.grid_rowconfigure(0, weight=1)
        card.body.grid_columnconfigure(0, weight=1)
        self.log_text = ScrolledText(
            card.body,
            height=7,
            wrap="word",
            bg=LOG_BG,
            fg="#334155",
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            font=(MONO, 9),
            state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("time", foreground=FAINT)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("ok", foreground=GREEN)
        self.log_text.tag_configure("warn", foreground=AMBER)
        self._log("界面已就绪。源文件不记录，其他设置会自动保存。", "ok")

    # ------------------------------------------------------------------ 设置

    def _load_settings_into_ui(self) -> None:
        settings = self.settings
        # 源文件/文件夹属于一次性选择，不读取旧版本残留的 source 字段。
        settings.pop("source", None)
        self.source_var.set("")
        self.output_var.set(
            str(settings.get("output", str(_default_output_dir())))
        )
        self.sender_var.set(str(settings.get("sender", "")))
        self.to_var.set(str(settings.get("recipients", "")))
        remember_auth = bool(settings.get("remember_auth", True))
        self.remember_auth_var.set(remember_auth)
        self._auth_consent = bool(settings.get("auth_consent", False)) and remember_auth
        saved_auth = settings.get("auth_code")
        if remember_auth and saved_auth:
            try:
                self.auth_var.set(unprotect_secret(str(saved_auth)))
                self._auth_consent = True
            except Exception as exc:
                self.auth_var.set("")
                self.remember_auth_var.set(False)
                self._auth_consent = False
                self._log(f"已保存的授权码无法解密，已清除：{exc}", "warn")
        elif not remember_auth:
            self.auth_var.set("")
        self.volume_var.set(str(settings.get("volume", DEFAULT_VOLUME_MB)))
        fmt_value = str(settings.get("format", "auto"))
        self.format_var.set(FORMAT_LABEL_BY_VALUE.get(fmt_value, FORMAT_LABEL_BY_VALUE["auto"]))
        last_job = settings.get("last_job")
        if last_job:
            self._last_job_dir = Path(str(last_job))
        self._refresh_volume_hint()

    def _collect_settings(self) -> dict:
        remember_auth = bool(self.remember_auth_var.get())
        data = {
            "settings_version": 3,
            "output": self.output_var.get().strip(),
            "sender": self.sender_var.get().strip(),
            "recipients": self.to_var.get().strip(),
            "volume": self.volume_var.get().strip(),
            "format": FORMAT_LABELS.get(self.format_var.get(), "auto"),
            "last_job": str(self._last_job_dir) if self._last_job_dir else "",
            "remember_auth": remember_auth,
            "auth_consent": bool(self._auth_consent and remember_auth),
        }
        if remember_auth and self._auth_consent:
            auth_code = self.auth_var.get().replace(" ", "").strip()
            if auth_code:
                try:
                    data["auth_code"] = protect_secret(auth_code)
                except Exception:
                    pass
        try:
            if self.root.state() == "normal":
                width = self.root.winfo_width()
                height = self.root.winfo_height()
                x = self.root.winfo_x()
                y = self.root.winfo_y()
                if width >= 840 and height >= 740 and x >= 0 and y >= 0:
                    data["window"] = {
                        "width": width,
                        "height": height,
                        "x": x,
                        "y": y,
                    }
            else:
                previous = self.settings.get("window")
                if isinstance(previous, dict):
                    data["window"] = previous
        except Exception:
            pass
        return data

    def _save_settings(self) -> None:
        _save_settings(self._collect_settings())

    def _toggle_auth(self) -> None:
        self.auth_entry.configure(show="" if self.show_auth_var.get() else "*")

    def _confirm_auth_storage(self) -> bool:
        protection = (
            "Windows DPAPI 加密（与当前 Windows 用户绑定）"
            if os.name == "nt"
            else "Base64 编码（仅兼容，不具备强加密）"
        )
        return messagebox.askyesno(
            "保存 SMTP 授权码？",
            "将把 SMTP 授权码使用以下方式保存到本机：\n\n"
            f"{protection}\n\n"
            f"保存位置：\n{_settings_path()}\n\n"
            "提示：即使加密，能登录当前 Windows 账户的人或程序仍可能读取授权码。\n"
            "不建议在共用电脑上保存。\n\n"
            "是否保存授权码？",
            icon=messagebox.WARNING,
            default="no",
        )

    def _on_remember_auth_toggled(self) -> None:
        if self.remember_auth_var.get():
            if not self._auth_consent:
                if not self._confirm_auth_storage():
                    self.remember_auth_var.set(False)
                    self._auth_consent = False
                    self._save_settings()
                    self._log("未保存授权码。", "warn")
                    return
                self._auth_consent = True
            self._save_settings()
            self._log("已开启授权码记忆，授权码将加密保存到本机。", "warn")
        else:
            self._auth_consent = False
            self._save_settings()
            self._log("已关闭授权码记忆，保存的授权码已清除。", "warn")

    def _ensure_auth_save_consent(self) -> bool:
        """首次保存授权码前提示用户；用户拒绝时仍允许本次发送。"""
        if not self.remember_auth_var.get():
            return False
        if self._auth_consent:
            return True
        if self._confirm_auth_storage():
            self._auth_consent = True
            self._save_settings()
            return True
        self.remember_auth_var.set(False)
        self._auth_consent = False
        self._save_settings()
        self._log("未保存授权码；本次仍可正常发送。", "warn")
        return False

    def _refresh_volume_hint(self) -> None:
        try:
            volume = int(self.volume_var.get())
        except ValueError:
            volume = 0
        if volume <= 0:
            text = "请输入 1–30 MB 的整数。"
            color = RED
        elif volume > 25:
            text = "接近 QQ 附件上限，编码后可能超限；建议使用 20 MB。"
            color = AMBER
        else:
            text = "每封邮件只发送一个分卷；20 MB 分卷编码后约 27 MB，留有安全余量。"
            color = FAINT
        self.volume_hint.configure(text=text, fg=color)

    # ------------------------------------------------------------------ 选择

    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(title="选择要压缩的文件")
        if path:
            self.source_var.set(path)
            if not self.output_var.get().strip():
                self.output_var.set(str(_default_output_dir()))

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(title="选择要压缩的文件夹")
        if path:
            self.source_var.set(path)
            if not self.output_var.get().strip():
                self.output_var.set(str(_default_output_dir()))

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.output_var.get().strip() or str(Path.home()),
        )
        if path:
            self.output_var.set(path)

    def _open_output(self) -> None:
        raw = self.output_var.get().strip()
        path = Path(raw).expanduser() if raw else _default_output_dir()
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("无法打开", f"无法创建输出目录：\n{path}\n\n{exc}")
                return
        _open_path(path)

    # ------------------------------------------------------------------ 校验

    def _build_prepare_config(self, require_contacts: bool) -> PrepareConfig:
        source_raw = self.source_var.get().strip()
        if not source_raw:
            raise ConfigError("请选择要压缩的文件或文件夹。")
        source = Path(source_raw).expanduser()
        if not source.exists():
            raise ConfigError(f"文件或文件夹不存在：\n{source}")

        output_raw = self.output_var.get().strip()
        if not output_raw:
            output_raw = str(_default_output_dir())
            self.output_var.set(output_raw)

        try:
            volume = int(self.volume_var.get().strip())
        except ValueError as exc:
            raise ConfigError("分卷大小必须是整数。") from exc
        if not 1 <= volume <= MAX_VOLUME_MB:
            raise ConfigError(f"分卷大小必须在 1–{MAX_VOLUME_MB} MB 之间。")

        archive_format = FORMAT_LABELS.get(self.format_var.get(), "auto")
        if archive_format == "auto" and not find_7zip():
            raise ConfigError(
                "没有找到 7-Zip。请安装 7-Zip 后重试，"
                "或把“压缩方式”改为“ZIP 分卷（兼容模式）”。"
            )

        sender = self.sender_var.get().strip()
        recipients: list[str] = []
        raw_recipients = self.to_var.get().strip()
        if require_contacts:
            if not sender:
                raise ConfigError("请填写发件 QQ 邮箱。")
            if not raw_recipients:
                raise ConfigError("请填写收件邮箱。")
        if raw_recipients:
            recipients = parse_email_list(raw_recipients)
        if sender:
            sender = validate_single_email(sender, "发件邮箱")

        return PrepareConfig(
            source=source,
            output_root=Path(output_raw).expanduser(),
            volume_size_mb=volume,
            archive_format=archive_format,
            compression_level=5,
            subject_prefix="文件分卷",
            display_name=APP_NAME,
            sender=sender,
            recipients=recipients,
        )

    def _build_smtp_config(self, sender: str, recipients: list[str]) -> SmtpConfig:
        auth = self.auth_var.get().replace(" ", "").strip()
        if not auth:
            raise ConfigError("请填写 QQ 邮箱 SMTP 授权码。")
        if not sender:
            raise ConfigError("请填写发件 QQ 邮箱。")
        if not recipients:
            raise ConfigError("请填写收件邮箱。")
        return SmtpConfig(
            sender=sender,
            recipients=recipients,
            auth_code=auth,
            display_name=APP_NAME,
            send_interval=3.0,
            max_retries=3,
            timeout=90.0,
        )

    # ------------------------------------------------------------------ 执行

    def _start(self, send: bool) -> None:
        if self._running:
            return
        try:
            config = self._build_prepare_config(require_contacts=send)
            smtp = (
                self._build_smtp_config(config.sender, config.recipients)
                if send
                else None
            )
        except QQVolMailError as exc:
            messagebox.showwarning("请完善信息", str(exc))
            return

        if send:
            self._ensure_auth_save_consent()
        self._save_settings()
        self._cancel.clear()
        self._begin_running("正在压缩并发送…" if send else "正在压缩分卷…")
        self._log("=" * 52)
        self._log("开始新任务。")
        threading.Thread(
            target=self._work_new_job,
            args=(config, smtp, send),
            daemon=True,
        ).start()

    def _work_new_job(self, config: PrepareConfig, smtp: SmtpConfig | None, send: bool) -> None:
        job: Job | None = None
        try:
            reporter = Reporter(
                on_log=lambda message: self._queue.put(("log", message)),
                on_progress=lambda stage, current, total, text: self._queue.put(
                    ("progress", stage, current, total, text)
                ),
            )
            job = create_job(config, reporter, self._cancel)
            self._queue.put(("job", str(job.job_dir)))
            if send and smtp is not None:
                job = send_job(job, smtp, reporter, self._cancel)
            self._queue.put(("done", "send" if send else "prepare", job, len(job.parts)))
        except Cancelled:
            self._queue.put(("cancelled", str(job.job_dir) if job else ""))
        except QQVolMailError as exc:
            self._queue.put(
                ("error", "操作失败", str(exc), str(job.job_dir) if job else "", None)
            )
        except Exception as exc:
            self._queue.put(
                (
                    "error",
                    "程序错误",
                    f"{exc}",
                    str(job.job_dir) if job else "",
                    traceback.format_exc(),
                )
            )

    def _begin_running(self, status: str) -> None:
        self._cancel.clear()
        self._running = True
        self._set_busy(True)
        self.cancel_btn.configure(state="normal", text="取消")
        self._set_status(status, "正在准备…")
        self._progress_indeterminate()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (self.prepare_btn, self.send_btn):
            button.configure(state=state)
        self.test_btn.configure(state=state)
        if busy:
            self.cancel_btn.pack(side="right", padx=(8, 0))
        else:
            self.cancel_btn.pack_forget()

    def _cancel_task(self) -> None:
        if not self._running:
            return
        self._cancel.set()
        self.cancel_btn.configure(state="disabled", text="正在取消…")
        self._set_status("正在取消…", "已发送的分卷不会重复发送")

    def _set_status(self, status: str, detail: str) -> None:
        self.status_var.set(status)
        self.detail_var.set(detail)

    # ------------------------------------------------------------------ 进度

    def _progress_indeterminate(self) -> None:
        if self._progress_mode != "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="indeterminate", maximum=100, value=0)
            self.progress.start(14)
            self._progress_mode = "indeterminate"

    def _progress_determinate(self, current: int, total: int) -> None:
        if total <= 0:
            self._progress_indeterminate()
            return
        if self._progress_mode == "indeterminate":
            self.progress.stop()
        self.progress.configure(mode="determinate", maximum=total, value=current)
        self._progress_mode = "determinate"

    def _progress_finish(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self._progress_mode = "determinate"

    def _on_progress(self, stage: str, current: int, total: int, text: str) -> None:
        if stage == "send":
            self._progress_determinate(current, total)
            if text:
                self._set_status("正在发送分卷…", text)
        elif stage in {"compress", "hash", "split"}:
            if total > 0:
                self._progress_determinate(current, total)
            else:
                self._progress_indeterminate()
            if text:
                if stage == "hash":
                    self._set_status("正在校验分卷…", text)
                elif stage == "split":
                    self._set_status("正在切分分卷…", text)
                else:
                    self._set_status("正在压缩分卷…", text)
        elif stage == "scan":
            self._progress_indeterminate()
            if text:
                self._set_status("正在统计文件…", text)
        elif stage == "done":
            self._progress_finish()
            if text:
                self._set_status(text, "任务已完成")

    # ------------------------------------------------------------------ 队列

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                try:
                    kind = item[0]
                    if kind == "log":
                        self._log(str(item[1]))
                    elif kind == "progress":
                        self._on_progress(item[1], item[2], item[3], item[4])
                    elif kind == "job":
                        self._last_job_dir = Path(str(item[1]))
                    elif kind == "done":
                        self._handle_done(item[1], item[2], item[3])
                    elif kind == "error":
                        self._handle_error(item[1], item[2], item[3], item[4])
                    elif kind == "cancelled":
                        self._handle_cancelled(item[1])
                    elif kind == "smtp_ok":
                        self._on_smtp_test_done(True, item[1])
                    elif kind == "smtp_error":
                        self._on_smtp_test_done(False, item[1])
                    else:
                        self._log(f"未知界面事件：{kind}", "warn")
                except Exception:
                    # 单个事件处理失败不能停止队列轮询，否则界面会表现为“卡住”。
                    try:
                        self._log("界面事件处理失败：\n" + traceback.format_exc(), "error")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            try:
                if self.root.winfo_exists():
                    self._after_id = self.root.after(100, self._drain_queue)
            except tk.TclError:
                self._after_id = None

    def _handle_done(self, mode: str, job: Job, part_count: int) -> None:
        self._running = False
        self._set_busy(False)
        self._progress_finish()
        self._last_job_dir = job.job_dir
        self._save_settings()
        if mode == "send":
            self._set_status("发送完成", f"已发送 {part_count} 个分卷")
            self._log(f"全部完成：{part_count} 个分卷已发送。", "ok")
            self._log(f"任务目录：{job.job_dir}", "ok")
            answer = messagebox.askyesno(
                "发送完成",
                f"已成功发送 {part_count} 个分卷。\n\n任务目录：\n{job.job_dir}\n\n是否打开任务目录？",
            )
        else:
            self._set_status("分卷完成", f"共 {part_count} 个分卷，尚未发送")
            self._log(f"分卷完成：{part_count} 个分卷。", "ok")
            self._log(f"任务目录：{job.job_dir}", "ok")
            answer = messagebox.askyesno(
                "分卷完成",
                f"已生成 {part_count} 个分卷，尚未发送。\n\n"
                f"任务目录：\n{job.job_dir}\n\n是否打开任务目录？",
            )
        if answer:
            _open_path(job.job_dir)

    def _handle_error(self, title: str, message: str, job_dir: str, detail: str | None) -> None:
        self._running = False
        self._set_busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._progress_mode = ""
        self._set_status("操作失败", title)
        self._log(f"{title}：{message}", "error")
        if detail:
            self._log(detail, "error")
        if job_dir:
            self._last_job_dir = Path(job_dir)
            self._save_settings()
            self._log(f"任务目录：{job_dir}", "warn")
            message = (
                f"{message}\n\n任务已保留：\n{job_dir}\n\n"
                "修复问题后，可点击“继续未完成任务…”重试，已发送的分卷不会重复发送。"
            )
        messagebox.showerror(title, message)

    def _handle_cancelled(self, job_dir: str) -> None:
        self._running = False
        self._set_busy(False)
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._progress_mode = ""
        self._set_status("已取消", "已生成的分卷和发送记录会保留")
        self._log("任务已取消。", "warn")
        if job_dir:
            self._last_job_dir = Path(job_dir)
            self._save_settings()
            messagebox.showinfo(
                "已取消",
                f"任务已取消。\n\n任务目录：\n{job_dir}\n\n"
                "之后可点击“继续未完成任务…”继续发送。",
            )
        else:
            messagebox.showinfo("已取消", "任务已取消。")

    # ------------------------------------------------------------------ 续传

    def _resume_job(self) -> None:
        if self._running:
            return
        initial = (
            str(self._last_job_dir)
            if self._last_job_dir and self._last_job_dir.exists()
            else self.output_var.get().strip()
            or str(_default_output_dir())
        )
        manifest = filedialog.askopenfilename(
            title="选择要续传的任务（manifest.json）",
            initialdir=initial,
            filetypes=[("任务清单", "manifest.json"), ("JSON 文件", "*.json")],
        )
        if not manifest:
            return
        try:
            job = load_job(Path(manifest))
        except QQVolMailError as exc:
            messagebox.showerror("无法读取任务", str(exc))
            return

        sent = set(int(x) for x in job.data.get("send", {}).get("sent_parts", []))
        total = len(job.data.get("parts", []))
        if total and len(sent) >= total:
            messagebox.showinfo(
                "任务已完成",
                f"这个任务已经发送了全部 {total} 个分卷，无需继续。",
            )
            return

        auth = self.auth_var.get().replace(" ", "").strip()
        if not auth:
            messagebox.showwarning(
                "需要授权码",
                "继续发送仍需要 SMTP 授权码。\n请在界面中填写授权码后重试。",
            )
            self.auth_entry.focus_set()
            return

        sender = (job.data.get("sender") or self.sender_var.get()).strip()
        recipients = list(job.data.get("recipients") or [])
        if not recipients:
            try:
                recipients = parse_email_list(self.to_var.get())
            except QQVolMailError as exc:
                messagebox.showerror("缺少收件人", str(exc))
                return

        if not messagebox.askyesno(
            "继续发送",
            f"任务：{job.data.get('source_name', '')}\n"
            f"已发送 {len(sent)}/{total} 卷，将继续发送剩余 {total - len(sent)} 卷。\n\n"
            f"收件人：{', '.join(recipients)}\n\n是否继续？",
        ):
            return

        self._ensure_auth_save_consent()
        self._save_settings()
        self._begin_running("正在继续发送…")
        self._cancel.clear()
        self._log("=" * 52)
        self._log(f"继续任务：{job.job_dir}")
        threading.Thread(
            target=self._work_resume,
            args=(job, sender, recipients, auth),
            daemon=True,
        ).start()

    def _work_resume(self, job: Job, sender: str, recipients: list[str], auth: str) -> None:
        try:
            smtp = SmtpConfig(
                sender=sender,
                recipients=recipients,
                auth_code=auth,
                display_name=APP_NAME,
                send_interval=3.0,
                max_retries=3,
                timeout=90.0,
            )
            reporter = Reporter(
                on_log=lambda message: self._queue.put(("log", message)),
                on_progress=lambda stage, current, total, text: self._queue.put(
                    ("progress", stage, current, total, text)
                ),
            )
            job = send_job(job, smtp, reporter, self._cancel)
            self._queue.put(("done", "send", job, len(job.parts)))
        except Cancelled:
            self._queue.put(("cancelled", str(job.job_dir)))
        except QQVolMailError as exc:
            self._queue.put(("error", "继续发送失败", str(exc), str(job.job_dir), None))
        except Exception as exc:
            self._queue.put(
                ("error", "程序错误", str(exc), str(job.job_dir), traceback.format_exc())
            )

    # ------------------------------------------------------------------ SMTP

    def _test_connection(self) -> None:
        if self._smtp_testing or self._running:
            return
        sender = self.sender_var.get().strip()
        if not sender:
            messagebox.showwarning("请填写邮箱", "请先填写发件 QQ 邮箱。")
            return
        try:
            smtp = self._build_smtp_config(sender, ["test@example.com"])
        except QQVolMailError as exc:
            messagebox.showwarning("请完善信息", str(exc))
            return
        self._ensure_auth_save_consent()
        self._save_settings()
        self._smtp_testing = True
        self.test_btn.configure(state="disabled", text="正在测试…")
        self._set_status("正在测试邮箱连接…", "正在登录 smtp.qq.com")
        threading.Thread(target=self._work_test_smtp, args=(smtp,), daemon=True).start()

    def _work_test_smtp(self, smtp: SmtpConfig) -> None:
        try:
            result = test_smtp(smtp)
            self._queue.put(("smtp_ok", result))
        except Exception as exc:
            self._queue.put(("smtp_error", str(exc)))

    def _on_smtp_test_done(self, ok: bool, message: str) -> None:
        self._smtp_testing = False
        self.test_btn.configure(state="normal", text="测试邮箱连接")
        if ok:
            self._set_status("邮箱连接正常", "SMTP 登录成功")
            self._log(f"SMTP 测试成功：{message}", "ok")
            messagebox.showinfo("连接成功", f"QQ 邮箱 SMTP 登录成功。\n\n{message}")
        else:
            self._set_status("邮箱连接失败", "请检查授权码与网络")
            self._log(f"SMTP 测试失败：{message}", "error")
            messagebox.showerror("连接失败", message)

    # ------------------------------------------------------------------ 帮助

    def _show_auth_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("如何获取 QQ 邮箱授权码")
        win.configure(bg=WHITE)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        icon_path = Path(__file__).resolve().parent / "assets" / "app.ico"
        if icon_path.is_file():
            try:
                win.iconbitmap(default=str(icon_path))
            except Exception:
                pass
        width, height = 560, 440
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

        tk.Label(
            win, text="获取 QQ 邮箱 SMTP 授权码", bg=WHITE, fg=TEXT,
            font=(FONT, 15, "bold"), anchor="w",
        ).pack(fill="x", padx=24, pady=(22, 4))
        tk.Label(
            win,
            text="授权码是 QQ 邮箱为第三方客户端生成的专用密码，不是 QQ 登录密码。",
            bg=WHITE, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
            wraplength=500,
        ).pack(fill="x", padx=24, pady=(0, 12))

        steps = [
            ("1", "用浏览器登录 QQ 邮箱（mail.qq.com）。"),
            ("2", "点击右上角“设置” → “账号”。"),
            ("3", "找到“POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务”。"),
            ("4", "开启“POP3/SMTP 服务”或“IMAP/SMTP 服务”。"),
            ("5", "按页面提示完成验证，生成 16 位授权码。"),
            ("6", "把授权码填入本工具；修改 QQ 密码后授权码会失效。"),
        ]
        steps_frame = tk.Frame(win, bg=WHITE)
        steps_frame.pack(fill="both", expand=True, padx=24)
        for number, text in steps:
            row = tk.Frame(steps_frame, bg=WHITE)
            row.pack(fill="x", pady=4)
            badge = tk.Label(
                row, text=number, bg=PRIMARY_SOFT, fg=PRIMARY,
                font=(FONT, 8, "bold"), width=2, height=1,
            )
            badge.pack(side="left", anchor="n", pady=1)
            tk.Label(
                row, text=text, bg=WHITE, fg=TEXT, font=(FONT, 9),
                anchor="w", justify="left", wraplength=470,
            ).pack(side="left", padx=(10, 0), fill="x")

        note = tk.Frame(win, bg=AMBER_SOFT)
        note.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(
            note,
            text="安全提示：授权码默认不保存；勾选“记住授权码”后会用 Windows DPAPI 加密保存，首次会提示。",
            bg=AMBER_SOFT, fg=AMBER, font=(FONT, 8), anchor="w", justify="left",
            wraplength=480, padx=10, pady=7,
        ).pack(fill="x")

        actions = tk.Frame(win, bg=WHITE)
        actions.pack(fill="x", padx=24, pady=16)
        ttk.Button(
            actions, text="打开 QQ 邮箱", style="Primary.TButton",
            command=lambda: webbrowser.open("https://mail.qq.com/"), cursor="hand2",
        ).pack(side="left")
        ttk.Button(
            actions, text="关闭", style="Secondary.TButton",
            command=win.destroy, cursor="hand2",
        ).pack(side="right")

    # ------------------------------------------------------------------ 日志

    def _log(self, message: str, kind: str = "info") -> None:
        if not message:
            return
        self.log_text.configure(state="normal")
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] ", "time")
        text = message.rstrip() + "\n"
        if kind == "info":
            self.log_text.insert("end", text)
        else:
            self.log_text.insert("end", text, kind)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 900:
            self.log_text.delete("1.0", "120.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ 关闭

    def _on_close(self) -> None:
        if self._running:
            messagebox.showwarning("任务进行中", "任务正在进行中，请先点击“取消”再退出。")
            return
        if (
            self.auth_var.get().replace(" ", "").strip()
            and self.remember_auth_var.get()
            and not self._auth_consent
        ):
            self._ensure_auth_save_consent()
        self._save_settings()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.root.destroy()


def main() -> None:
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
