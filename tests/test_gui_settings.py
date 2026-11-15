#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""界面设置的持久化测试。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
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


class MemoryKeychainMixin:
    """把钥匙串后端换成内存字典，测试绝不触碰用户的真实钥匙串。"""

    def setUp(self) -> None:
        super().setUp()
        self.stack = ExitStack()
        self.keychain_items: dict[str, str] = {}
        self.stack.enter_context(
            mock.patch.object(gui, "_keychain_available", return_value=True)
        )
        self.stack.enter_context(
            mock.patch.object(
                gui,
                "_keychain_set",
                side_effect=lambda account, secret: self.keychain_items.__setitem__(
                    account, secret
                ),
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                gui,
                "_keychain_get",
                side_effect=lambda account: self.keychain_items[account],
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                gui,
                "_keychain_delete",
                side_effect=lambda account: self.keychain_items.pop(account, None),
            )
        )

    def tearDown(self) -> None:
        try:
            super().tearDown()
        finally:
            self.stack.close()


class SecretProtectionTests(MemoryKeychainMixin, unittest.TestCase):
    def test_protect_and_unprotect(self) -> None:
        secret = "abcdefghijklmnop"
        with mock.patch.object(gui.os, "name", "posix"):
            protected = gui.protect_secret(secret, account="me@qq.com")
            self.assertEqual(protected, "keychain:me@qq.com")
            self.assertNotIn(secret, protected)
            self.assertEqual(gui.unprotect_secret(protected), secret)
            self.assertEqual(self.keychain_items["me@qq.com"], secret)
            gui.forget_secret(protected)
            self.assertNotIn("me@qq.com", self.keychain_items)

    def test_no_plaintext_fallback_without_keychain(self) -> None:
        with mock.patch.object(gui.os, "name", "posix"), mock.patch.object(
            gui, "_keychain_available", return_value=False
        ):
            with self.assertRaises(OSError):
                gui.protect_secret("abcdefghijklmnop")

    def test_legacy_values_still_readable(self) -> None:
        import base64

        legacy = "b64:" + base64.b64encode(b"old-code").decode("ascii")
        self.assertEqual(gui.unprotect_secret(legacy), "old-code")
        self.assertEqual(gui.unprotect_secret("plain-old-code"), "plain-old-code")

    def test_windows_dpapi_branch_preserved(self) -> None:
        with mock.patch.object(gui.os, "name", "nt"), mock.patch.object(
            gui, "_dpapi_transform", return_value=b"cipher-bytes"
        ) as transform:
            protected = gui.protect_secret("abcdefghijklmnop")
            self.assertTrue(protected.startswith("dpapi:"))
            transform.assert_called_once()
        with mock.patch.object(gui.os, "name", "posix"):
            with self.assertRaises(ValueError):
                gui.unprotect_secret(protected)
        with mock.patch.object(gui.os, "name", "nt"), mock.patch.object(
            gui, "_dpapi_transform", return_value=b"abcdefghijklmnop"
        ):
            self.assertEqual(gui.unprotect_secret(protected), "abcdefghijklmnop")

    def test_invalid_ciphertext_rejected(self) -> None:
        with mock.patch.object(gui.os, "name", "nt"):
            with self.assertRaises(Exception):
                gui.unprotect_secret("dpapi:not-base64!!")


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
class GuiSettingsTests(MemoryKeychainMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
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
        super().tearDown()  # 关闭 MemoryKeychainMixin 的 patch，避免泄漏到其他测试

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
        self.assertTrue(data["auth_code"].startswith("keychain:"))
        self.assertNotIn("b64:", text)
        self.assertEqual(gui.unprotect_secret(data["auth_code"]), code)
        self.assertEqual(
            self.keychain_items[gui.KEYCHAIN_DEFAULT_ACCOUNT], code
        )

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

    def test_sender_is_used_as_keychain_account(self) -> None:
        self.app.sender_var.set("me@qq.com")
        self.app.remember_auth_var.set(True)
        self.app._auth_consent = True
        self.app.auth_var.set("abcdefghijklmnop")
        self.app._save_settings()
        data = json.loads((self.temp / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(data["auth_code"], "keychain:me@qq.com")
        self.assertEqual(self.keychain_items["me@qq.com"], "abcdefghijklmnop")

    def test_unchecking_remember_deletes_keychain_item(self) -> None:
        self.app.remember_auth_var.set(True)
        self.app._auth_consent = True
        self.app.auth_var.set("abcdefghijklmnop")
        self.app._save_settings()
        data = json.loads((self.temp / "settings.json").read_text(encoding="utf-8"))
        self.app.settings = data
        self.assertIn(gui.KEYCHAIN_DEFAULT_ACCOUNT, self.keychain_items)

        self.app.remember_auth_var.set(False)
        self.app._on_remember_auth_toggled()
        self.assertNotIn(gui.KEYCHAIN_DEFAULT_ACCOUNT, self.keychain_items)
        self.assertNotIn(
            "auth_code",
            json.loads((self.temp / "settings.json").read_text(encoding="utf-8")),
        )

    def test_no_plaintext_fallback_in_gui(self) -> None:
        self.app.remember_auth_var.set(True)
        self.app._auth_consent = True
        self.app.auth_var.set("abcdefghijklmnop")
        with mock.patch.object(gui.os, "name", "posix"), mock.patch.object(
            gui, "_keychain_available", return_value=False
        ):
            self.app._save_settings()
        text = (self.temp / "settings.json").read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertFalse(data["remember_auth"])
        self.assertFalse(data["auth_consent"])
        self.assertNotIn("auth_code", data)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("b64:", text)

    def test_mousewheel_small_delta_still_scrolls(self) -> None:
        # macOS Aqua 的 delta 常为 ±1，不能因为小于 120 就不滚动。
        event = type(
            "WheelEvent", (), {"widget": self.app.root, "delta": -1, "num": 0}
        )()
        with mock.patch.object(
            self.app.main_canvas, "yview", return_value=(0.0, 0.5)
        ), mock.patch.object(self.app.main_canvas, "yview_scroll") as scroll:
            self.app._on_mousewheel(event)
        scroll.assert_called_once_with(3, "units")


class MacOSPathTests(unittest.TestCase):
    """macOS 专有路径与打开方式的回归。"""

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS")
    def test_data_dir_uses_application_support(self) -> None:
        with mock.patch.dict("os.environ", {"QQVOLMAIL_DATA_DIR": ""}, clear=False), \
             mock.patch.object(gui, "_is_frozen", return_value=False):
            self.assertEqual(
                gui._data_dir(),
                Path.home() / "Library" / "Application Support" / "QQVolMail",
            )

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS")
    def test_frozen_macos_app_writes_outside_bundle(self) -> None:
        with mock.patch.dict(
            "os.environ", {"QQVOLMAIL_DATA_DIR": ""}, clear=False
        ), mock.patch.object(gui, "_is_frozen", return_value=True):
            self.assertEqual(
                gui._data_dir(),
                Path.home() / "Library" / "Application Support" / "QQVolMail",
            )
            self.assertEqual(gui._default_output_dir(), Path.home() / "QQVolMail")

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS")
    def test_open_path_uses_open_command(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            gui._open_path(Path("/tmp/qqvolmail-open-test"))
        popen.assert_called_once_with(["open", "/tmp/qqvolmail-open-test"])

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS")
    def test_macos_uses_system_fonts(self) -> None:
        self.assertEqual(gui.FONT, "PingFang SC")
        self.assertEqual(gui.MONO, "Menlo")

    @unittest.skipUnless(sys.platform == "darwin", "仅 macOS")
    def test_storage_wording_mentions_keychain_not_dpapi(self) -> None:
        text = gui._secret_storage_details()
        self.assertIn("钥匙串", text)
        self.assertNotIn("Base64", text)
        self.assertIn("设置文件只保存引用", text)


@unittest.skipUnless(HAS_TK and sys.platform == "darwin", "仅 macOS 且需要 tkinter")
class WindowIconTests(unittest.TestCase):
    def test_png_icon_is_applied_and_retained(self) -> None:
        root = tk.Tk()
        root.withdraw()
        try:
            with mock.patch.object(gui.os, "name", "posix"):
                gui._apply_window_icon(root)
            self.assertIsInstance(
                getattr(root, "_qqvolmail_icon", None), tk.PhotoImage
            )
        finally:
            root.destroy()


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/security").is_file(),
    "仅 macOS 且需要 /usr/bin/security",
)
class KeychainIntegrationTests(unittest.TestCase):
    """用一次性临时钥匙串验证真实 security 命令，不碰用户的登录钥匙串。"""

    def test_real_keychain_roundtrip(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="qqvolmail-kc-"))
        keychain = tmp / "test.keychain-db"
        password = "unit-test-pass"
        created = subprocess.run(
            ["security", "create-keychain", "-p", password, str(keychain)],
            capture_output=True,
        )
        if created.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            self.skipTest(
                f"无法创建临时钥匙串：{created.stderr.decode(errors='replace')}"
            )
        try:
            subprocess.run(
                ["security", "unlock-keychain", "-p", password, str(keychain)],
                check=True,
                capture_output=True,
            )
            with mock.patch.dict(
                os.environ, {"QQVOLMAIL_KEYCHAIN_PATH": str(keychain)}, clear=False
            ):
                secret = "abcdefghijklmnop"
                ref = gui.protect_secret(secret, account="probe@qq.com")
                self.assertEqual(ref, "keychain:probe@qq.com")
                self.assertNotIn(secret, json.dumps({"auth_code": ref}))
                self.assertEqual(gui.unprotect_secret(ref), secret)
                gui.forget_secret(ref)
                with self.assertRaises(OSError):
                    gui.unprotect_secret(ref)
        finally:
            subprocess.run(
                ["security", "delete-keychain", str(keychain)], capture_output=True
            )
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
