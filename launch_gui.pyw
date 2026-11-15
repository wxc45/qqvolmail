#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无控制台启动图形界面。"""

from __future__ import annotations

import sys


def _show_fatal(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("QQ 分卷邮递 - 启动失败", message)
        root.destroy()
    except Exception:
        pass


def _run_selftest() -> None:
    """打包验证用：启动界面、保存一次设置并写标记文件。"""
    import os
    from pathlib import Path
    import tkinter as tk

    from qqvolmail_gui import App, APP_VERSION, _settings_path

    root = tk.Tk()
    root.withdraw()
    app = App(root)
    app._save_settings()
    path = _settings_path()
    marker = Path(os.environ.get("QQVOLMAIL_SELFTEST_LOG", str(path) + ".selftest"))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"settings={path}\nexists={path.exists()}\nversion={APP_VERSION}\n",
        encoding="utf-8",
    )
    if app._after_id is not None:
        root.after_cancel(app._after_id)
    root.destroy()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        try:
            _run_selftest()
        except Exception as exc:
            import traceback

            _show_fatal(f"{exc}\n\n{traceback.format_exc()}")
            raise
        raise SystemExit(0)
    try:
        from qqvolmail_gui import main

        main()
    except Exception as exc:
        import traceback

        _show_fatal(f"{exc}\n\n{traceback.format_exc()}")
        raise
