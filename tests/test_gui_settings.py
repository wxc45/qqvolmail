#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面设置的持久化测试。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import qqvolmail_gui as gui  # noqa: E402

try:
    import tkinter as tk

    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    HAS_TK = True
except Exception:
    HAS_TK = False


class WindowGeometryTests(unittest.TestCase):
    def test_valid_geometry(self) -> None:
        settings = {"window": {"width": 900, "height": 800, "x": 50, "y": 60}}
        self.assertEqual(
            gui._saved_window_geometry(settings, 1920, 1080),
            "900x800+50+60",
        )

    def test_invalid_geometry_rejected(self) -> None:
        self.assertIsNone(gui._saved_window_geometry({}, 1920, 1080))
        too_small = {"window": {"width": 400, "height": 300, "x": 20, "y": 20}}
        self.assertIsNone(gui._saved_window_geometry(too_small, 1920, 1080))
        off_screen = {"window": {"width": 900, "height": 800, "x": 1900, "y": 20}}
        self.assertIsNone(gui._saved_window_geometry(off_screen, 1920, 1080))


class SecretProtectionTests(unittest.TestCase):
    def test_protect_and_unprotect(self) -> None:
        secret = "abcdefghijklmnop"
        protected = gui.protect_secret(secret)
        self.assertNotIn(secret, protected)
        self.assertEqual(gui.unprotect_secret(protected), secret)

    def test_invalid_ciphertext_rejected(self) -> None:
        protected = gui.protect_secret("abcdefghijklmnop")
        if protected.startswith("dpapi:"):
            with self.assertRaises(Exception):
                gui.unprotect_secret(protected[:-4])


class PortablePathTests(unittest.TestCase):
    def test_data_dir_and_output_override(self) -> None:
        with tempfile.TemporaryDirectory(prefix="qqvolmail-paths-") as tmp:
            data_dir = Path(tmp) / "data"
            with mock.patch.dict(
                "os.environ", {"QQVOLMAIL_DATA_DIR": str(data_dir)}, clear=False
            ):
                self.assertEqual(gui._data_dir(), data_dir.resolve())
                self.assertEqual(
                    gui._settings_path(), data_dir.resolve() / "settings.json"
                )
                self.assertEqual(
                    gui._default_output_dir(),
                    data_dir.resolve().parent / "output",
                )


@unittest.skipUnless(HAS_TK, "当前环境无法创建 tkinter 窗口")
class GuiSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="qqvolmail-gui-test-"))
        self.original_path = gui._settings_path
        gui._settings_path = lambda: self.temp / "settings.json"
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root)
        self.root.update_idletasks()

    def tearDown(self) -> None:
        try:
            if self.app._after_id is not None:
                self.root.after_cancel(self.app._after_id)
                self.app._after_id = None
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        gui._settings_path = self.original_path
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_source_is_not_saved_but_other_settings_are(self) -> None:
        self.app.source_var.set(r"D:\secret\private-file.bin")
        self.app.output_var.set(r"D:\QQVolMail")
        self.app.sender_var.set("sender@qq.com")
        self.app.to_var.set("a@example.com, b@example.com")
        self.app.volume_var.set("15")
        self.app.format_var.set("ZIP 分卷（兼容模式）")
        self.app._save_settings()

        path = self.temp / "settings.json"
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertNotIn("source", data)
        self.assertNotIn("private-file", text)
        self.assertEqual(data["output"], r"D:\QQVolMail")
        self.assertEqual(data["sender"], "sender@qq.com")
        self.assertEqual(data["recipients"], "a@example.com, b@example.com")
        self.assertEqual(data["volume"], "15")
        self.assertEqual(data["format"], "zip")
        self.assertNotIn("auth_code", data)

    def test_legacy_source_setting_is_ignored_and_removed(self) -> None:
        self.app.settings["source"] = r"D:\old\remembered.bin"
        self.app.settings["output"] = r"D:\QQVolMail"
        self.app.source_var.set(r"D:\old\remembered.bin")
        self.app.output_var.set(r"D:\QQVolMail")
        self.app._load_settings_into_ui()
        self.assertEqual(self.app.source_var.get(), "")
        self.assertNotIn("source", self.app.settings)
        self.assertEqual(self.app.output_var.get(), r"D:\QQVolMail")

    def test_auth_code_saved_encrypted_and_restored(self) -> None:
        code = "abcdefghijklmnop"
        self.app.remember_auth_var.set(True)
        self.app._auth_consent = True
        self.app.auth_var.set(code)
        self.app._save_settings()

        path = self.temp / "settings.json"
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertTrue(data["remember_auth"])
        self.assertTrue(data["auth_consent"])
        self.assertIn("auth_code", data)
        self.assertNotIn(code, text)
        self.assertEqual(gui.unprotect_secret(data["auth_code"]), code)

        self.app.auth_var.set("")
        self.app._auth_consent = False
        self.app.settings = data
        self.app._load_settings_into_ui()
        self.assertEqual(self.app.auth_var.get(), code)
        self.assertTrue(self.app._auth_consent)
        self.assertTrue(self.app.remember_auth_var.get())

    def test_handle_error_resets_progress_without_attribute_error(self) -> None:
        with mock.patch.object(gui.messagebox, "showerror") as showerror:
            self.app._handle_error("压缩失败", "测试错误", "", None)
        self.assertFalse(self.app._running)
        showerror.assert_called_once()
        self.assertIn("测试错误", showerror.call_args[0][1])

    def test_encryption_password_is_not_saved(self) -> None:
        self.app.encrypt_var.set(True)
        self.app.archive_password_var.set("S3cret!中文")
        self.app.archive_password_confirm_var.set("S3cret!中文")
        self.app._save_settings()
        text = (self.temp / "settings.json").read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertNotIn("S3cret!中文", text)
        self.assertNotIn("archive_password", data)
        self.assertNotIn("archive_password_confirm", data)

    def test_encryption_rejects_mismatched_passwords(self) -> None:
        source = self.temp / "source.bin"
        source.write_bytes(b"data")
        self.app.source_var.set(str(source))
        self.app.encrypt_var.set(True)
        self.app.archive_password_var.set("abcd")
        self.app.archive_password_confirm_var.set("abce")
        with self.assertRaises(gui.ConfigError):
            self.app._build_prepare_config(False)

    def test_encryption_switches_format_to_7z(self) -> None:
        self.app.format_var.set(gui.FORMAT_LABEL_BY_VALUE["zip"])
        self.app.encrypt_var.set(True)
        self.app._toggle_encryption()
        self.assertEqual(self.app.format_var.get(), gui.FORMAT_LABEL_BY_VALUE["7z"])
        self.assertEqual(str(self.app.format_combo.cget("state")), "disabled")
        self.app.encrypt_var.set(False)
        self.app._toggle_encryption()
        self.assertEqual(str(self.app.format_combo.cget("state")), "readonly")
        self.assertEqual(self.app.archive_password_var.get(), "")
        self.assertEqual(self.app.archive_password_confirm_var.get(), "")

    @unittest.skipUnless(gui.find_7zip(), "未安装 7-Zip")
    def test_encryption_password_reaches_prepare_config(self) -> None:
        source = self.temp / "source.bin"
        source.write_bytes(b"data")
        self.app.source_var.set(str(source))
        self.app.format_var.set(gui.FORMAT_LABEL_BY_VALUE["7z"])
        self.app.encrypt_var.set(True)
        self.app.archive_password_var.set("S3cret!中文")
        self.app.archive_password_confirm_var.set("S3cret!中文")
        config = self.app._build_prepare_config(False)
        self.assertEqual(config.archive_password, "S3cret!中文")
        self.assertEqual(config.archive_format, "auto")

    def test_unchecking_remember_removes_auth_code(self) -> None:
        self.app.remember_auth_var.set(True)
        self.app._auth_consent = True
        self.app.auth_var.set("abcdefghijklmnop")
        self.app._save_settings()
        self.assertIn(
            "auth_code", json.loads((self.temp / "settings.json").read_text(encoding="utf-8"))
        )

        self.app.remember_auth_var.set(False)
        self.app._auth_consent = False
        self.app._save_settings()
        data = json.loads((self.temp / "settings.json").read_text(encoding="utf-8"))
        self.assertFalse(data["remember_auth"])
        self.assertNotIn("auth_code", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
