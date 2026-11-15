#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心逻辑测试：不访问真实网络，不发送真实邮件。"""

from __future__ import annotations

import hashlib
import os
import shutil
import smtplib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import qqvolmail as core  # noqa: E402


class FakeSMTP:
    """最小 SMTP 替身，记录邮件而不联网。"""

    instances: list["FakeSMTP"] = []
    fail_on_call: int | None = None
    call_count = 0

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.sent: list[tuple] = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def login(self, user, password):
        self.user = user
        self.password = password
        return 235, b"Authentication successful"

    def send_message(self, msg, from_addr=None, to_addrs=None):
        type(self).call_count += 1
        if (
            type(self).fail_on_call is not None
            and type(self).call_count == type(self).fail_on_call
        ):
            type(self).fail_on_call = None
            raise smtplib.SMTPServerDisconnected("simulated disconnect")
        self.sent.append((msg, from_addr, to_addrs))
        return {}

    def noop(self):
        return 250, b"OK"

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


def reset_fake() -> None:
    FakeSMTP.instances = []
    FakeSMTP.fail_on_call = None
    FakeSMTP.call_count = 0


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="qqvolmail-test-"))
        reset_fake()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)
        reset_fake()

    # ------------------------------------------------------------------ 工具

    def make_file(self, size: int, name: str = "payload.bin") -> Path:
        path = self.temp / name
        path.write_bytes(os.urandom(size))
        return path

    def make_job(self, size: int = 2 * 1024 * 1024 + 500_000, fmt: str = "zip", volume_mb: int = 1):
        source = self.make_file(size)
        config = core.PrepareConfig(
            source=source,
            output_root=self.temp / "out",
            volume_size_mb=volume_mb,
            archive_format=fmt,
            compression_level=1,
        )
        return core.create_job(config, core.Reporter())

    # ------------------------------------------------------------------ 分卷

    def test_zip_split_and_merge(self) -> None:
        job = self.make_job(fmt="zip")
        self.assertEqual(job.data["archive_format"], "zip")
        self.assertGreater(len(job.parts), 1)
        names = [p["name"] for p in job.parts]
        self.assertTrue(names[0].endswith(".zip.001"))
        self.assertEqual(
            [p["index"] for p in job.parts], list(range(1, len(job.parts) + 1))
        )
        for part in job.parts:
            path = job.job_dir / part["name"]
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, part["size"])
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), part["sha256"]
            )

        merged = job.job_dir / "merged.zip"
        with open(merged, "wb") as target:
            for part in job.parts:
                target.write((job.job_dir / part["name"]).read_bytes())
        with zipfile.ZipFile(merged) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("payload.bin", archive.namelist())

    @unittest.skipUnless(core.find_7zip(), "未安装 7-Zip")
    def test_7z_native_volumes(self) -> None:
        job = self.make_job(fmt="7z")
        self.assertEqual(job.data["archive_format"], "7z")
        self.assertGreater(len(job.parts), 1)
        self.assertTrue(job.parts[0]["name"].endswith(".7z.001"))
        self.assertEqual(job.data["archive_name"], "payload.7z")

    @unittest.skipUnless(core.find_7zip(), "未安装 7-Zip")
    def test_7z_single_volume_renamed(self) -> None:
        job = self.make_job(size=1000, fmt="7z")
        self.assertEqual(len(job.parts), 1)
        self.assertTrue(job.parts[0]["name"].endswith(".7z"))
        self.assertFalse(job.parts[0]["name"].endswith(".001"))
        self.assertTrue((job.job_dir / job.parts[0]["name"]).is_file())

    def test_directory_archive_keeps_folder(self) -> None:
        source = self.temp / "资料夹"
        source.mkdir()
        (source / "说明.txt").write_text("你好，分卷。\n" * 100, encoding="utf-8")
        (source / "空目录").mkdir()
        config = core.PrepareConfig(
            source=source,
            output_root=self.temp / "out_dir",
            volume_size_mb=1,
            archive_format="zip",
        )
        job = core.create_job(config, core.Reporter())
        self.assertEqual(job.data["source_type"], "directory")
        merged = job.job_dir / "merged.zip"
        with open(merged, "wb") as target:
            for part in job.parts:
                target.write((job.job_dir / part["name"]).read_bytes())
        with zipfile.ZipFile(merged) as archive:
            names = archive.namelist()
            self.assertIn("资料夹/说明.txt", names)
            self.assertIn("资料夹/空目录/", names)

    def test_output_inside_source_rejected(self) -> None:
        source = self.temp / "folder"
        source.mkdir()
        (source / "a.txt").write_text("x", encoding="utf-8")
        config = core.PrepareConfig(
            source=source,
            output_root=source / "out",
            volume_size_mb=1,
            archive_format="zip",
        )
        with self.assertRaises(core.ConfigError):
            core.create_job(config, core.Reporter())

    # ------------------------------------------------------------------ 邮箱

    def test_parse_recipients(self) -> None:
        self.assertEqual(
            core.parse_email_list("a@example.com, b@example.com；a@example.com"),
            ["a@example.com", "b@example.com"],
        )
        self.assertEqual(
            core.parse_email_list(["A <a@example.com>", "b@example.com"]),
            ["a@example.com", "b@example.com"],
        )
        with self.assertRaises(core.ConfigError):
            core.parse_email_list("not-an-email")

    def test_build_message(self) -> None:
        job = self.make_job(fmt="zip")
        part = job.parts[0]
        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=["to@example.com"],
            auth_code="x",
            display_name="测试发件人",
        )
        sender, recipients, display_name, prefix = core._merge_send_metadata(job, smtp)
        message = core.build_message(
            job, part, 1, len(job.parts), sender, recipients, display_name, prefix
        )
        self.assertIn("[1/", str(message["Subject"]))
        self.assertIn("to@example.com", str(message["To"]))
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), part["name"])
        self.assertLess(len(message.as_bytes()), core.MAX_MESSAGE_BYTES)
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertIn(part["sha256"], body)

    def test_send_and_resume(self) -> None:
        job = self.make_job(fmt="zip")
        total = len(job.parts)
        self.assertGreater(total, 1)
        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=["to@example.com"],
            auth_code="auth-code",
            send_interval=0,
            max_retries=1,
        )
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            job = core.send_job(job, smtp, core.Reporter())
        self.assertEqual(job.data["send"]["status"], "sent")
        self.assertEqual(job.data["send"]["sent_parts"], list(range(1, total + 1)))
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(len(FakeSMTP.instances[0].sent), total)
        for message, from_addr, to_addrs in FakeSMTP.instances[0].sent:
            self.assertEqual(from_addr, "sender@qq.com")
            self.assertEqual(to_addrs, ["to@example.com"])
            self.assertEqual(len(list(message.iter_attachments())), 1)

        before = len(FakeSMTP.instances[0].sent)
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            job = core.send_job(job, smtp, core.Reporter())
        self.assertEqual(len(FakeSMTP.instances[0].sent), before, "已发送任务不应重复发送")

    def test_partial_failure_then_resume(self) -> None:
        job = self.make_job(fmt="zip")
        total = len(job.parts)
        self.assertGreater(total, 2)
        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=["to@example.com"],
            auth_code="auth-code",
            send_interval=0,
            max_retries=0,
        )
        FakeSMTP.fail_on_call = 2
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            with self.assertRaises(core.SendError):
                core.send_job(job, smtp, core.Reporter())
        job = core.load_job(job.job_dir)
        self.assertEqual(job.data["send"]["sent_parts"], [1])
        self.assertEqual(job.data["send"]["status"], "partial")
        self.assertEqual(len(FakeSMTP.instances[0].sent), 1)

        reset_fake()
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            job = core.send_job(job, smtp, core.Reporter())
        self.assertEqual(job.data["send"]["status"], "sent")
        self.assertEqual(
            len(FakeSMTP.instances[0].sent), total - 1,
            "续传应只发送未完成的分卷",
        )

    def test_auth_error_is_friendly(self) -> None:
        job = self.make_job(size=1000, fmt="zip")

        class BadAuthSMTP(FakeSMTP):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=["to@example.com"],
            auth_code="wrong",
            send_interval=0,
            max_retries=0,
        )
        with mock.patch.object(core.smtplib, "SMTP_SSL", BadAuthSMTP):
            with self.assertRaises(core.SendError) as ctx:
                core.send_job(job, smtp, core.Reporter())
        self.assertIn("授权码", str(ctx.exception))
        self.assertEqual(core.load_job(job.job_dir).data["send"]["status"], "partial")

    def test_test_smtp(self) -> None:
        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=[],
            auth_code="auth-code",
        )
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            result = core.test_smtp(smtp)
        self.assertIn("连接成功", result)
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertTrue(FakeSMTP.instances[0].closed)

    def test_metadata_mismatch_refused(self) -> None:
        job = self.make_job(size=1000, fmt="zip")
        smtp = core.SmtpConfig(
            sender="sender@qq.com",
            recipients=["to@example.com"],
            auth_code="auth-code",
        )
        with mock.patch.object(core.smtplib, "SMTP_SSL", FakeSMTP):
            core.send_job(job, smtp, core.Reporter())
        other = core.SmtpConfig(
            sender="other@qq.com",
            recipients=["to@example.com"],
            auth_code="auth-code",
        )
        with self.assertRaises(core.SendError):
            core.send_job(job, other, core.Reporter())


class PlatformCompatTests(unittest.TestCase):
    """跨平台参数构造的确定性回归：不依赖真实 7-Zip。"""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="qqvolmail-platform-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _capture_7z_args(self, os_name: str) -> dict:
        captured: dict = {}

        class FakeProcess:
            def wait(self, timeout=None):
                return 0

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess()

        source = self.temp / "资料夹"
        source.mkdir()
        (source / "a.txt").write_text("hello", encoding="utf-8")
        job_dir = self.temp / "job"
        job_dir.mkdir()
        archive = job_dir / "out.7z"
        with mock.patch.object(core.os, "name", os_name), mock.patch.object(
            core.subprocess, "Popen", fake_popen
        ):
            core._run_7z(
                "7zz", source, archive, 1024, 1, job_dir, core.Reporter(), None
            )
        return captured

    def test_7z_args_posix_style(self) -> None:
        captured = self._capture_7z_args("posix")
        self.assertNotIn("-sccUTF-8", captured["args"])
        self.assertEqual(captured["args"][-1], "资料夹")
        self.assertNotIn("\\", captured["args"][-1])
        self.assertNotIn("creationflags", captured["kwargs"])

    def test_7z_args_windows_style_preserved(self) -> None:
        captured = self._capture_7z_args("nt")
        self.assertIn("-sccUTF-8", captured["args"])
        self.assertEqual(captured["args"][-1], ".\\资料夹")
        self.assertIn("creationflags", captured["kwargs"])

    def test_auto_falls_back_to_zip_without_7zip(self) -> None:
        source = self.temp / "small.bin"
        source.write_bytes(b"x" * 1000)
        with mock.patch.object(core, "find_7zip", return_value=None):
            job = core.create_job(
                core.PrepareConfig(
                    source=source,
                    output_root=self.temp / "out",
                    volume_size_mb=1,
                    archive_format="auto",
                ),
                core.Reporter(),
            )
        self.assertEqual(job.data["archive_format"], "zip")

    def test_broken_7zip_binary_is_rejected(self) -> None:
        fake = self.temp / "fake7z"
        fake.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
        fake.chmod(0o755)
        self.assertFalse(core._sevenzip_is_usable(str(fake)))

    def test_sevenzip_env_override_is_accepted(self) -> None:
        fake = self.temp / "my7z"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"SEVENZIP_PATH": str(fake)}):
            self.assertEqual(core.find_7zip(), str(fake))


class InstructionsTests(unittest.TestCase):
    """邮件正文的合并指引必须同时覆盖 Windows 与 macOS/Linux 收件人。"""

    @staticmethod
    def _parts(total: int) -> list[dict]:
        return [{"name": f"src.zip.{index:03d}"} for index in range(1, total + 1)]

    def test_single_part_zip(self) -> None:
        self.assertIn(
            "直接解压",
            core._build_instructions("zip", 1, self._parts(1)),
        )

    def test_zip_instructions_include_both_platforms(self) -> None:
        text = core._build_instructions("zip", 3, self._parts(3))
        self.assertIn("copy /b", text)
        self.assertIn('"src.zip.001" + "src.zip.002" + "src.zip.003"', text)
        self.assertIn("cat src.zip.* > src.zip", text)
        self.assertIn("macOS / Linux", text)
        self.assertIn("Windows", text)

    def test_many_parts_use_powershell_and_posix_cat(self) -> None:
        text = core._build_instructions("zip", 25, self._parts(25))
        self.assertIn("PowerShell", text)
        self.assertIn("cat src.zip.* > src.zip", text)
        self.assertNotIn("copy /b", text)

    def test_7z_instructions_unchanged(self) -> None:
        text = core._build_instructions("7z", 2, self._parts(2))
        self.assertIn("7-Zip", text)
        self.assertNotIn("copy /b", text)
        self.assertNotIn("cat ", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
