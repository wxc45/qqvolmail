#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 分卷邮递。

把文件或文件夹压缩成分卷，再通过 QQ 邮箱 SMTP 逐卷发送。

设计要点：
- 优先使用 7-Zip 生成原生分卷（.7z.001、.7z.002 ...），收件人可直接解压；
- 未安装 7-Zip 时退化为 ZIP + 二进制切片（.zip.001 ...），需要先合并；
- 每封邮件只带一个分卷，默认 20 MB，避免超过 QQ 邮箱普通附件上限；
- 每个任务生成 manifest.json，记录分卷哈希与发送进度，支持断点续传；
- 授权码只从界面/环境变量/终端读取，绝不写入任务文件。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Callable, Iterable, Sequence

APP_NAME = "QQ 分卷邮递"
APP_VERSION = "1.2.4"
SCHEMA_VERSION = 1

DEFAULT_VOLUME_MB = 20
MIN_VOLUME_MB = 1
# QQ 邮箱普通附件上限约 50 MB。30 MB 原文件经 Base64 后约 40 MB，
# 留出邮件头与编码余量；再大就可能被服务器拒收。
MAX_VOLUME_MB = 30
MAX_ATTACHMENT_BYTES = MAX_VOLUME_MB * 1024 * 1024
MAX_MESSAGE_BYTES = 46 * 1024 * 1024

QQ_SMTP_HOST = "smtp.qq.com"
QQ_SMTP_SSL_PORT = 465
QQ_SMTP_STARTTLS_PORT = 587
DEFAULT_AUTH_ENV = "QQ_SMTP_AUTH_CODE"

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ---------------------------------------------------------------------------
# 异常与回调
# ---------------------------------------------------------------------------


class QQVolMailError(Exception):
    """可向用户展示的业务错误。"""


class ConfigError(QQVolMailError):
    pass


class ArchiveError(QQVolMailError):
    pass


class SendError(QQVolMailError):
    pass


class Cancelled(QQVolMailError):
    pass


class Reporter:
    """把日志与进度同时送给 CLI 或 GUI。"""

    def __init__(
        self,
        on_log: Callable[[str], None] | None = None,
        on_progress: Callable[[str, int, int, str], None] | None = None,
    ) -> None:
        self._log = on_log
        self._progress = on_progress

    def log(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def progress(self, stage: str, current: int = 0, total: int = 0, text: str = "") -> None:
        if self._progress is not None:
            self._progress(stage, current, total, text)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消。")


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------


@dataclass
class PrepareConfig:
    source: Path
    output_root: Path
    volume_size_mb: int = DEFAULT_VOLUME_MB
    archive_format: str = "auto"  # auto / 7z / zip
    compression_level: int = 5
    subject_prefix: str = "文件分卷"
    display_name: str = APP_NAME
    sender: str = ""
    recipients: list[str] = field(default_factory=list)


@dataclass
class SmtpConfig:
    sender: str
    recipients: list[str]
    auth_code: str = field(repr=False)
    host: str = QQ_SMTP_HOST
    port: int = QQ_SMTP_SSL_PORT
    security: str = "auto"  # auto / ssl / starttls
    display_name: str = APP_NAME
    send_interval: float = 3.0
    max_retries: int = 3
    timeout: float = 90.0


@dataclass
class Job:
    job_dir: Path
    manifest_path: Path
    data: dict

    @property
    def parts(self) -> list[dict]:
        return list(self.data.get("parts", []))


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def format_bytes(num: float) -> str:
    num = float(num)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if abs(num) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} TB"


def safe_filename(name: str, max_len: int = 80) -> str:
    """生成 Windows 上安全的文件/目录名，保留中文。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "archive"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name or "archive"


def parse_email_list(raw: str | Iterable[str]) -> list[str]:
    if isinstance(raw, str):
        text = raw.replace("；", ",").replace(";", ",")
        pairs = getaddresses([text])
        addresses = [addr for _, addr in pairs if addr]
        if not addresses:
            addresses = [x.strip() for x in re.split(r"[,，\s]+", text) if x.strip()]
    else:
        addresses = []
        for item in raw:
            addresses.extend(parse_email_list(item))
    result: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        address = address.strip()
        if "\r" in address or "\n" in address or not EMAIL_RE.match(address):
            raise ConfigError(f"邮箱地址格式不正确：{address or '（空）'}")
        key = address.lower()
        if key not in seen:
            seen.add(key)
            result.append(address)
    if not result:
        raise ConfigError("请至少填写一个收件邮箱。")
    return result


def validate_single_email(raw: str, label: str) -> str:
    address = (raw or "").strip()
    if "\r" in address or "\n" in address or not EMAIL_RE.match(address):
        raise ConfigError(f"{label}格式不正确：{address or '（空）'}")
    return address


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def natural_part_key(path: Path) -> tuple[int, str]:
    match = re.search(r"\.(\d+)$", path.name)
    return (int(match.group(1)) if match else 0, path.name)


def sha256_file(path: Path, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            _check_cancel(cancel)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON 格式错误：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"JSON 顶层必须是对象：{path}")
    return data


# ---------------------------------------------------------------------------
# 7-Zip 与压缩分卷
# ---------------------------------------------------------------------------


def find_7zip() -> str | None:
    env_path = os.environ.get("SEVENZIP_PATH", "").strip()
    candidates: list[str] = []
    if env_path:
        candidates.append(env_path)
    for name in ("7z.exe", "7z", "7za.exe", "7za"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(
        [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
        ]
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path)
    return None


def _is_reparse_point(path: Path) -> bool:
    """识别符号链接、目录联接和云占位符，避免扫描时递归成环。"""
    try:
        if path.is_symlink():
            return True
        stat_result = path.stat(follow_symlinks=False)
        attributes = getattr(stat_result, "st_file_attributes", 0)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _scan_source(source: Path, reporter: Reporter, cancel: threading.Event | None) -> tuple[int, int]:
    """返回 (文件数, 总字节数)。"""
    if source.is_file():
        return 1, source.stat().st_size
    if not source.is_dir():
        raise ConfigError(f"源路径既不是文件也不是文件夹：{source}")

    def walk_error(exc: OSError) -> None:
        raise ConfigError(f"无法读取文件夹：{exc.filename or source}\n{exc}") from exc

    file_count = 0
    total_size = 0
    last_report = 0.0
    try:
        for root, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
            _check_cancel(cancel)
            dirs.sort()
            files.sort()
            kept_dirs: list[str] = []
            for dirname in dirs:
                child = Path(root) / dirname
                if _is_reparse_point(child):
                    reporter.log(f"已跳过链接/重解析点：{child}")
                else:
                    kept_dirs.append(dirname)
            dirs[:] = kept_dirs
            for filename in files:
                path = Path(root) / filename
                if _is_reparse_point(path):
                    reporter.log(f"已跳过链接文件：{path}")
                    continue
                try:
                    total_size += path.stat().st_size
                except OSError as exc:
                    raise ConfigError(f"无法读取文件：{path}\n{exc}") from exc
                file_count += 1
                now = time.monotonic()
                if now - last_report > 0.25:
                    reporter.progress(
                        "scan", 0, 0, f"正在统计文件：{file_count} 个，{format_bytes(total_size)}"
                    )
                    reporter.log(f"正在统计文件：{file_count} 个，{format_bytes(total_size)}")
                    last_report = now
    except PermissionError as exc:
        raise ConfigError(f"没有权限读取文件夹：{source}\n{exc}") from exc
    return file_count, total_size


def _check_disk_space(job_dir: Path, source_size: int, archive_format: str) -> None:
    try:
        free = shutil.disk_usage(job_dir).free
    except OSError:
        return
    # ZIP 需要先写完整压缩包再切片；7z 直接写分卷，只需约一份空间。
    factor = 2.05 if archive_format == "zip" else 1.03
    needed = int(source_size * factor) + 50 * 1024 * 1024
    if free < needed:
        raise ConfigError(
            f"磁盘空间不足：输出盘剩余 {format_bytes(free)}，"
            f"本次预计至少需要 {format_bytes(needed)}。\n请更换输出目录或清理磁盘。"
        )


def _create_job_dir(output_root: Path, source: Path) -> Path:
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建输出目录：{output_root}\n{exc}") from exc
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{safe_filename(source.name, 60)}_{stamp}"
    for index in range(1, 1000):
        name = base if index == 1 else f"{base}_{index}"
        candidate = output_root / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise ConfigError(f"无法创建任务目录：{candidate}\n{exc}") from exc
    raise ConfigError("无法创建任务目录，请检查输出目录权限。")


def _run_7z(
    sevenzip: str,
    source: Path,
    archive_path: Path,
    volume_bytes: int,
    level: int,
    job_dir: Path,
    reporter: Reporter,
    cancel: threading.Event | None,
) -> None:
    log_path = job_dir / "7z.log"
    args = [
        sevenzip,
        "a",
        "-t7z",
        f"-mx={level}",
        f"-v{volume_bytes}b",
        "-y",
        "-sccUTF-8",
        "-bsp1",
        str(archive_path),
        f".\\{source.name}",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    reporter.progress("compress", 0, 0, "正在使用 7-Zip 压缩并分卷...")
    reporter.log("正在使用 7-Zip 压缩并分卷（大文件可能需要较长时间）...")
    started_at = time.monotonic()
    last_heartbeat = started_at
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as log_handle:
            process = subprocess.Popen(
                args,
                cwd=str(source.parent),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            reporter.log("7-Zip 已启动，正在压缩并写入分卷...")
            while True:
                try:
                    return_code = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise Cancelled("任务已取消。")
                    now = time.monotonic()
                    if now - last_heartbeat >= 10:
                        elapsed = int(now - started_at)
                        text = f"7-Zip 正在压缩...已运行 {elapsed} 秒"
                        reporter.progress("compress", 0, 0, text)
                        reporter.log(text)
                        last_heartbeat = now
    except OSError as exc:
        raise ArchiveError(
            f"无法启动 7-Zip：{sevenzip}\n{exc}\n"
            "请确认已安装 7-Zip，或把压缩方式改为 ZIP。"
        ) from exc

    if return_code != 0:
        detail = ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            detail = text[-4000:].strip()
        except OSError:
            pass
        raise ArchiveError(f"7-Zip 压缩失败（退出码 {return_code}）。\n{detail}")


def _collect_7z_parts(archive_path: Path) -> tuple[list[Path], list[str]]:
    prefix = archive_path.name + "."
    parts: list[tuple[int, Path]] = []
    if archive_path.parent.exists():
        for item in archive_path.parent.iterdir():
            if not item.is_file() or not item.name.startswith(prefix):
                continue
            suffix = item.name[len(prefix):]
            if suffix.isdigit():
                parts.append((int(suffix), item))
    parts.sort(key=lambda pair: pair[0])
    logs: list[str] = []
    if not parts:
        if archive_path.is_file():
            return [archive_path], logs
        raise ArchiveError("7-Zip 没有生成任何分卷，请检查源文件是否可读。")
    if archive_path.is_file():
        # 极少数情况下 7-Zip 未按分卷模式输出。
        archive_path.unlink()
        logs.append(f"已移除未分卷的中间文件：{archive_path.name}")
    return [path for _, path in parts], logs


def _create_zip(
    source: Path,
    archive_path: Path,
    level: int,
    source_size: int,
    reporter: Reporter,
    cancel: threading.Event | None,
) -> None:
    reporter.progress("compress", 0, 0, "正在创建 ZIP 压缩包...")
    reporter.log("正在创建 ZIP 压缩包...")
    done = 0
    last_report = 0.0

    def walk_error(exc: OSError) -> None:
        raise ArchiveError(f"无法读取文件夹：{exc.filename or source}\n{exc}") from exc

    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=max(1, min(9, level)),
            allowZip64=True,
        ) as archive:
            if source.is_file():
                archive.write(source, arcname=source.name)
                done = source_size
            else:
                for root, dirs, files in os.walk(
                    source, followlinks=False, onerror=walk_error
                ):
                    _check_cancel(cancel)
                    dirs.sort()
                    files.sort()
                    root_path = Path(root)
                    relative = root_path.relative_to(source)
                    if relative == Path("."):
                        arc_dir = source.name
                    else:
                        arc_dir = f"{source.name}/{relative.as_posix()}"
                    archive.writestr(arc_dir.rstrip("/") + "/", b"")
                    for filename in files:
                        _check_cancel(cancel)
                        file_path = root_path / filename
                        archive.write(
                            file_path,
                            arcname=f"{arc_dir}/{filename}",
                        )
                        try:
                            done += file_path.stat().st_size
                        except OSError:
                            done += 1
                        now = time.monotonic()
                        if now - last_report > 0.25:
                            percent = min(100, int(done * 100 / source_size)) if source_size else 0
                            reporter.progress(
                                "compress",
                                percent,
                                100,
                                f"正在压缩：{percent}%（{format_bytes(done)} / {format_bytes(source_size)}）",
                            )
                            reporter.log(f"正在压缩：{percent}%")
                            last_report = now
    except PermissionError as exc:
        raise ArchiveError(f"没有权限读取文件，ZIP 创建失败：{exc}") from exc
    except OSError as exc:
        raise ArchiveError(f"创建 ZIP 失败：{exc}") from exc


def _split_zip(
    archive_path: Path,
    volume_bytes: int,
    reporter: Reporter,
    cancel: threading.Event | None,
) -> list[Path]:
    archive_size = archive_path.stat().st_size
    if archive_size <= volume_bytes:
        return [archive_path]

    reporter.log(f"正在把 ZIP 切分为 {format_bytes(volume_bytes)} 的分卷...")
    parts: list[Path] = []
    try:
        with open(archive_path, "rb") as source:
            index = 1
            while True:
                _check_cancel(cancel)
                chunk = source.read(volume_bytes)
                if not chunk:
                    break
                part_path = archive_path.with_name(f"{archive_path.name}.{index:03d}")
                with open(part_path, "wb") as target:
                    target.write(chunk)
                parts.append(part_path)
                reporter.progress(
                    "split",
                    0,
                    0,
                    f"正在生成第 {index} 个分卷（已读取 {format_bytes(source.tell())}）",
                )
                index += 1
        if not parts:
            raise ArchiveError("ZIP 文件为空，无法分卷。")
    except Exception:
        for part in parts:
            try:
                part.unlink()
            except OSError:
                pass
        raise

    archive_path.unlink()
    return parts


def _write_readme(job_dir: Path, manifest: dict) -> None:
    parts = manifest["parts"]
    total = len(parts)
    lines = [
        f"{APP_NAME} - 解压说明",
        "=" * 60,
        "",
        f"原始内容：{manifest['source_name']}",
        f"压缩格式：{manifest['archive_format']}",
        f"分卷数量：{total}",
        f"分卷大小：{format_bytes(manifest['volume_size_bytes'])}",
        f"任务目录：{job_dir}",
        f"创建时间：{manifest['created_at']}",
        "",
        "解压方法",
        "-" * 60,
        manifest["instructions"],
        "",
        "分卷清单",
        "-" * 60,
    ]
    for part in parts:
        lines.append(f"{part['index']:03d}  {part['name']}")
        lines.append(f"     大小：{format_bytes(part['size'])}")
        lines.append(f"     SHA-256：{part['sha256']}")
    lines.extend(
        [
            "",
            "提示",
            "-" * 60,
            "1. 请将全部分卷放在同一个文件夹内，不要重命名。",
            "2. 下载后建议核对每个分卷的 SHA-256。",
            "3. 如果缺少任意一卷，压缩包将无法完整解压。",
            "",
        ]
    )
    (job_dir / "解压说明.txt").write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 建立任务
# ---------------------------------------------------------------------------


def _validate_prepare(config: PrepareConfig) -> tuple[Path, Path, int, str]:
    source = Path(config.source).expanduser().resolve()
    output_root = Path(config.output_root).expanduser().resolve()
    if not source.exists():
        raise ConfigError(f"要压缩的文件或文件夹不存在：\n{source}")
    if not source.name:
        raise ConfigError("不能直接压缩磁盘根目录，请选择具体的文件或文件夹。")
    if source.is_dir() and is_relative_to(output_root, source):
        raise ConfigError("输出目录不能位于要压缩的文件夹内部，否则会把输出文件也压缩进去。\n请选择其他输出目录。")
    if output_root.exists() and not output_root.is_dir():
        raise ConfigError(f"输出路径已经存在，但不是文件夹：\n{output_root}")
    if not 1 <= int(config.volume_size_mb) <= MAX_VOLUME_MB:
        raise ConfigError(f"分卷大小必须在 {MIN_VOLUME_MB}~{MAX_VOLUME_MB} MB 之间。")
    if not 0 <= int(config.compression_level) <= 9:
        raise ConfigError("压缩级别必须在 0~9 之间。")
    if config.archive_format not in {"auto", "7z", "zip"}:
        raise ConfigError("压缩方式必须是 auto、7z 或 zip。")
    return source, output_root, int(config.volume_size_mb) * 1024 * 1024, config.archive_format


def create_job(
    config: PrepareConfig,
    reporter: Reporter | None = None,
    cancel: threading.Event | None = None,
) -> Job:
    reporter = reporter or Reporter()
    source, output_root, volume_bytes, requested_format = _validate_prepare(config)
    _check_cancel(cancel)

    sevenzip = find_7zip()
    archive_format = requested_format
    if archive_format == "auto":
        archive_format = "7z" if sevenzip else "zip"
    if archive_format == "7z" and not sevenzip:
        raise ConfigError(
            "没有找到 7-Zip。请先安装 7-Zip（https://www.7-zip.org/），"
            "或把压缩方式改为 ZIP。"
        )

    reporter.log(f"源路径：{source}")
    reporter.log(f"压缩方式：{'7-Zip 原生分卷' if archive_format == '7z' else 'ZIP + 二进制分卷'}")
    reporter.log(f"分卷大小：{format_bytes(volume_bytes)}")

    file_count, source_size = _scan_source(source, reporter, cancel)
    _check_cancel(cancel)
    reporter.log(f"共 {file_count} 个文件，原始大小 {format_bytes(source_size)}。")

    reporter.log("正在创建任务目录...")
    job_dir = _create_job_dir(output_root, source)
    reporter.log(f"任务目录：{job_dir}")
    reporter.log("正在检查输出磁盘空间...")
    _check_disk_space(job_dir, source_size, archive_format)

    archive_base = safe_filename(source.stem if source.is_file() else source.name, 70)
    manifest_path = job_dir / "manifest.json"
    parts: list[Path] = []

    try:
        if archive_format == "7z":
            archive_path = job_dir / f"{archive_base}.7z"
            assert sevenzip is not None
            _run_7z(
                sevenzip, source, archive_path, volume_bytes, int(config.compression_level),
                job_dir, reporter, cancel,
            )
            parts, log_lines = _collect_7z_parts(archive_path)
            for line in log_lines:
                reporter.log(line)
            if len(parts) == 1 and parts[0].name == archive_path.name + ".001":
                # 只有一卷时还原成普通 .7z，收件人无需处理分卷编号。
                single = archive_path
                parts[0].rename(single)
                parts = [single]
                reporter.log("压缩结果只有一个分卷，已保存为普通 .7z 文件。")
        else:
            archive_path = job_dir / f"{archive_base}.zip"
            _create_zip(
                source, archive_path, int(config.compression_level),
                source_size, reporter, cancel,
            )
            parts = _split_zip(archive_path, volume_bytes, reporter, cancel)

        _check_cancel(cancel)
        if not parts:
            raise ArchiveError("没有生成任何分卷。")

        reporter.log(f"正在计算 {len(parts)} 个分卷的 SHA-256...")
        part_records: list[dict] = []
        for index, part_path in enumerate(parts, start=1):
            _check_cancel(cancel)
            reporter.progress(
                "hash", index - 1, len(parts),
                f"正在校验第 {index}/{len(parts)} 个分卷...",
            )
            part_records.append(
                {
                    "index": index,
                    "name": part_path.name,
                    "size": part_path.stat().st_size,
                    "sha256": sha256_file(part_path, cancel),
                }
            )

        total_bytes = sum(item["size"] for item in part_records)
        instructions = _build_instructions(archive_format, len(part_records), part_records)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "job_id": job_dir.name,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "source": str(source),
            "source_name": source.name,
            "source_type": "file" if source.is_file() else "directory",
            "source_size": source_size,
            "file_count": file_count,
            "archive_format": archive_format,
            "archive_name": parts[0].name if len(parts) == 1 else f"{archive_base}.{archive_format}",
            "volume_size_bytes": volume_bytes,
            "compression_level": int(config.compression_level),
            "total_parts": len(part_records),
            "total_bytes": total_bytes,
            "parts": part_records,
            "subject_prefix": config.subject_prefix.strip() or "文件分卷",
            "display_name": config.display_name.strip() or APP_NAME,
            "sender": config.sender.strip(),
            "recipients": list(config.recipients),
            "instructions": instructions,
            "send": {
                "status": "not_started",
                "sent_parts": [],
                "sent_at": {},
                "message_ids": {},
                "last_error": None,
            },
        }
        atomic_write_json(manifest_path, manifest)
        _write_readme(job_dir, manifest)
        reporter.progress("done", len(part_records), len(part_records), "分卷完成")
        reporter.log(
            f"分卷完成：共 {len(part_records)} 卷，"
            f"压缩后总计 {format_bytes(total_bytes)}。"
        )
        return Job(job_dir=job_dir, manifest_path=manifest_path, data=manifest)
    except Exception:
        # 保留任务目录和 7z.log，便于排查；不删除已生成的分卷。
        raise


def _build_instructions(archive_format: str, total: int, parts: list[dict]) -> str:
    if total == 1:
        return "附件就是完整的压缩包，下载后直接解压即可。"
    first = parts[0]["name"]
    if archive_format == "7z":
        return (
            f"请把全部 {total} 个分卷下载到同一个文件夹，安装 7-Zip 后，"
            f"右键点击第 1 个分卷（{first}）→“7-Zip”→“提取到当前文件夹”。"
            "请勿重命名或缺少任何分卷。"
        )
    names = [item["name"] for item in parts]
    zip_name = names[0][:-4] if names[0].endswith(".001") else "merged.zip"
    if total <= 20:
        command = f'copy /b "{names[0]}"' + "".join(f' + "{name}"' for name in names[1:])
        command += f' "{zip_name}"'
        return (
            f"这是 ZIP 分卷，请把全部 {total} 个分卷下载到同一个文件夹，"
            "然后在命令提示符中执行：\n\n"
            f"{command}\n\n"
            "合并完成后解压生成的 ZIP 文件。请勿重命名分卷。"
        )
    return (
        f"这是 ZIP 分卷。请把全部 {total} 个分卷下载到同一个文件夹，"
        "然后在 PowerShell 中执行以下命令合并：\n\n"
        f'$out=[IO.File]::Create("merged.zip"); '
        f'Get-ChildItem "{zip_name}.*" | Sort-Object Name | '
        'ForEach-Object { $in=[IO.File]::OpenRead($_.FullName); $in.CopyTo($out); $in.Close() }; '
        '$out.Close()\n\n'
        "合并完成后解压 merged.zip。请勿重命名分卷。"
    )


# ---------------------------------------------------------------------------
# 读取任务
# ---------------------------------------------------------------------------


def load_job(path: Path | str) -> Job:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_dir():
        manifest_path = candidate / "manifest.json"
    else:
        manifest_path = candidate
    if not manifest_path.is_file():
        raise ConfigError(f"找不到任务清单：{manifest_path}")
    data = read_json(manifest_path)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"不支持的任务清单版本：{data.get('schema_version')}")
    parts = data.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ConfigError("任务清单中没有分卷信息。")
    job_dir = manifest_path.parent
    for part in parts:
        part_path = job_dir / str(part.get("name", ""))
        if not part_path.is_file():
            raise ConfigError(f"任务缺少分卷文件：{part_path}")
        if int(part.get("size", -1)) != part_path.stat().st_size:
            raise ConfigError(f"分卷大小与清单不一致，文件可能损坏：{part_path.name}")
    return Job(job_dir=job_dir, manifest_path=manifest_path, data=data)


def _save_job(job: Job) -> None:
    job.data["updated_at"] = now_iso()
    atomic_write_json(job.manifest_path, job.data)


# ---------------------------------------------------------------------------
# 邮件构造与 SMTP
# ---------------------------------------------------------------------------


def _email_body(job: Job, part: dict, index: int, total: int) -> str:
    data = job.data
    instructions = data.get("instructions") or _build_instructions(
        data.get("archive_format", "7z"), total, data["parts"]
    )
    header = (
        f"您好：\n\n"
        f"这是「{data['source_name']}」压缩分卷的第 {index}/{total} 卷，共 {total} 封邮件。\n\n"
        f"附件：{part['name']}\n"
        f"大小：{format_bytes(part['size'])}\n"
        f"SHA-256：{part['sha256']}\n\n"
        f"{instructions}\n\n"
        f"发送时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"本邮件由“{APP_NAME}”自动发送，请勿直接回复。\n"
    )
    return header


def build_message(
    job: Job,
    part: dict,
    index: int,
    total: int,
    sender: str,
    recipients: Sequence[str],
    display_name: str,
    subject_prefix: str,
) -> EmailMessage:
    data = job.data

    def one_line(value: object) -> str:
        return re.sub(r"[\r\n]+", " ", str(value)).strip()

    display_name = one_line(display_name) or APP_NAME
    subject_prefix = one_line(subject_prefix) or "文件分卷"
    source_name = one_line(data.get("source_name", "文件"))
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((display_name, sender))
    message["To"] = ", ".join(recipients)
    subject = f"[{index}/{total}] {subject_prefix} - {source_name}"
    if len(subject) > 180:
        subject = subject[:177] + "..."
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@", 1)[-1] or None)
    message["X-QQVolMail-Job"] = one_line(data.get("job_id", ""))
    message["X-QQVolMail-Part"] = f"{index}/{total}"
    message.set_content(_email_body(job, part, index, total), subtype="plain", charset="utf-8")

    part_path = job.job_dir / part["name"]
    payload = part_path.read_bytes()
    message.add_attachment(
        payload,
        maintype="application",
        subtype="octet-stream",
        filename=part["name"],
    )
    return message


def _smtp_error_text(exc: smtplib.SMTPResponseException) -> str:
    detail = exc.smtp_error
    if isinstance(detail, bytes):
        for encoding in ("utf-8", "gb18030"):
            try:
                detail = detail.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            detail = repr(detail)
    return f"{exc.smtp_code} {detail}"


def _is_transient(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            smtplib.SMTPHeloError,
            socket.timeout,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= int(exc.smtp_code) < 500
    return isinstance(exc, OSError)


def _open_smtp(config: SmtpConfig, reporter: Reporter) -> smtplib.SMTP:
    context = ssl.create_default_context()
    host = config.host or QQ_SMTP_HOST
    port = int(config.port)
    security = config.security
    if security == "auto":
        security = "ssl" if port == QQ_SMTP_SSL_PORT else "starttls"
    reporter.log(f"正在连接邮箱服务器 {host}:{port} ...")
    connection: smtplib.SMTP | None = None
    try:
        if security == "ssl":
            connection = smtplib.SMTP_SSL(
                host, port, timeout=config.timeout, context=context
            )
        else:
            connection = smtplib.SMTP(host, port, timeout=config.timeout)
            connection.ehlo()
            connection.starttls(context=context)
            connection.ehlo()
        connection.login(config.sender, config.auth_code)
        reporter.log("邮箱认证成功。")
        return connection
    except smtplib.SMTPAuthenticationError as exc:
        _close_quietly(connection)
        raise SendError(
            "QQ 邮箱认证失败，请检查：\n"
            "1. 发件邮箱是否填写完整（例如 123456@qq.com）；\n"
            "2. 是否已在 QQ 邮箱设置中开启 SMTP 服务；\n"
            "3. 是否使用的是“授权码”，而不是 QQ 登录密码。\n\n"
            f"服务器信息：{_smtp_error_text(exc)}"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        _close_quietly(connection)
        raise SendError(
            f"无法连接 QQ 邮箱服务器：{exc}\n"
            "请检查网络、代理或防火墙设置；QQ 邮箱 SSL 端口为 465。"
        ) from exc


def _close_quietly(connection: smtplib.SMTP | None) -> None:
    if connection is None:
        return
    try:
        connection.quit()
    except Exception:
        try:
            connection.close()
        except Exception:
            pass


def _merge_send_metadata(job: Job, smtp: SmtpConfig) -> tuple[str, list[str], str, str]:
    data = job.data
    sender = (smtp.sender or data.get("sender") or "").strip()
    if not sender:
        raise SendError("请填写发件 QQ 邮箱。")
    sender = validate_single_email(sender, "发件邮箱")
    saved_sender = (data.get("sender") or "").strip()
    if saved_sender and saved_sender.lower() != sender.lower():
        raise SendError(
            f"当前发件邮箱与任务记录不一致。\n任务记录：{saved_sender}\n当前输入：{sender}"
        )

    saved_recipients = list(data.get("recipients") or [])
    input_recipients = list(smtp.recipients or [])
    if saved_recipients and input_recipients:
        if {x.lower() for x in saved_recipients} != {x.lower() for x in input_recipients}:
            raise SendError("当前收件人与任务记录不一致，请新建任务，避免误发。")
        recipients = saved_recipients
    else:
        recipients = saved_recipients or input_recipients
    if not recipients:
        raise SendError("请至少填写一个收件邮箱。")
    recipients = parse_email_list(recipients)

    data["sender"] = sender
    data["recipients"] = recipients
    display_name = (data.get("display_name") or smtp.display_name or APP_NAME).strip() or APP_NAME
    subject_prefix = (data.get("subject_prefix") or "文件分卷").strip() or "文件分卷"
    return sender, recipients, display_name, subject_prefix


def send_job(
    job_or_path: Job | Path | str,
    smtp: SmtpConfig,
    reporter: Reporter | None = None,
    cancel: threading.Event | None = None,
    force: bool = False,
) -> Job:
    reporter = reporter or Reporter()
    job = job_or_path if isinstance(job_or_path, Job) else load_job(job_or_path)
    data = job.data
    sender, recipients, display_name, subject_prefix = _merge_send_metadata(job, smtp)
    auth_code = (smtp.auth_code or "").replace(" ", "").strip()
    if not auth_code:
        raise SendError("请填写 QQ 邮箱 SMTP 授权码。")
    smtp.sender = sender
    smtp.recipients = recipients
    smtp.auth_code = auth_code

    parts = data["parts"]
    total = len(parts)
    for part in parts:
        if int(part["size"]) > MAX_ATTACHMENT_BYTES:
            raise SendError(
                f"分卷 {part['name']} 超过 {MAX_VOLUME_MB} MB，QQ 邮箱可能拒收。"
                "请减小分卷大小后重新创建任务。"
            )

    send_state = data.setdefault("send", {})
    sent_parts = {int(x) for x in send_state.get("sent_parts", [])}
    if force:
        sent_parts.clear()
        send_state["sent_parts"] = []
        send_state["sent_at"] = {}
        send_state["message_ids"] = {}
        send_state["last_error"] = None
        reporter.log("已按“强制重发”清空发送记录。")
    pending = [part for part in parts if int(part["index"]) not in sent_parts]
    if not pending:
        send_state["status"] = "sent"
        send_state["completed_at"] = send_state.get("completed_at") or now_iso()
        _save_job(job)
        reporter.log("该任务的全部分卷都已发送，无需重发。")
        return job

    send_state["status"] = "sending"
    send_state["last_attempt_at"] = now_iso()
    send_state["last_error"] = None
    _save_job(job)

    reporter.log(
        f"准备发送 {len(pending)} 个分卷给：{', '.join(recipients)}"
        f"（每封间隔 {smtp.send_interval:g} 秒）"
    )
    connection: smtplib.SMTP | None = None
    try:
        for part in pending:
            _check_cancel(cancel)
            index = int(part["index"])
            attempt = 0
            while True:
                _check_cancel(cancel)
                try:
                    if connection is None:
                        connection = _open_smtp(smtp, reporter)
                    message = build_message(
                        job, part, index, total, sender, recipients, display_name, subject_prefix
                    )
                    raw_size = len(message.as_bytes())
                    if raw_size > MAX_MESSAGE_BYTES:
                        raise SendError(
                            f"第 {index} 封邮件编码后约 {format_bytes(raw_size)}，"
                            "可能超过 QQ 邮箱附件上限。请减小分卷大小后重新创建任务。"
                        )
                    reporter.progress(
                        "send", index - 1, total,
                        f"正在发送第 {index}/{total} 卷（{format_bytes(part['size'])}）...",
                    )
                    reporter.log(
                        f"正在发送第 {index}/{total} 卷：{part['name']} "
                        f"（邮件约 {format_bytes(raw_size)}）"
                    )
                    connection.send_message(
                        message, from_addr=sender, to_addrs=recipients
                    )
                    break
                except smtplib.SMTPAuthenticationError as exc:
                    raise SendError(
                        "QQ 邮箱认证失败，请检查发件邮箱和 SMTP 授权码。\n"
                        f"服务器信息：{_smtp_error_text(exc)}"
                    ) from exc
                except smtplib.SMTPRecipientsRefused as exc:
                    details = "；".join(
                        f"{addr}: {_smtp_error_text(err) if isinstance(err, smtplib.SMTPResponseException) else err}"
                        for addr, err in exc.recipients.items()
                    )
                    raise SendError(f"收件邮箱被服务器拒绝：{details}") from exc
                except smtplib.SMTPSenderRefused as exc:
                    raise SendError(
                        f"发件邮箱被服务器拒绝：{_smtp_error_text(exc)}"
                    ) from exc
                except smtplib.SMTPDataError as exc:
                    if int(exc.smtp_code) in (552, 554):
                        raise SendError(
                            "邮件被 QQ 邮箱拒收，通常是附件过大或内容被拦截。\n"
                            f"服务器信息：{_smtp_error_text(exc)}"
                        ) from exc
                    if _is_transient(exc) and attempt < smtp.max_retries:
                        _close_quietly(connection)
                        connection = None
                        attempt += 1
                        delay = min(2 ** attempt, 15)
                        reporter.log(f"发送失败，{delay} 秒后重试（{attempt}/{smtp.max_retries}）...")
                        if cancel is not None:
                            if cancel.wait(delay):
                                raise Cancelled("任务已取消。")
                        else:
                            time.sleep(delay)
                        continue
                    raise SendError(
                        f"发送第 {index} 卷时服务器返回错误：{_smtp_error_text(exc)}"
                    ) from exc
                except (smtplib.SMTPException, OSError) as exc:
                    if _is_transient(exc) and attempt < smtp.max_retries:
                        _close_quietly(connection)
                        connection = None
                        attempt += 1
                        delay = min(2 ** attempt, 15)
                        reporter.log(
                            f"网络或服务器暂时不可用：{exc}；{delay} 秒后重试"
                            f"（{attempt}/{smtp.max_retries}）..."
                        )
                        if cancel is not None:
                            if cancel.wait(delay):
                                raise Cancelled("任务已取消。")
                        else:
                            time.sleep(delay)
                        continue
                    raise SendError(f"发送第 {index} 卷失败：{exc}") from exc

            sent_parts.add(index)
            send_state["sent_parts"] = sorted(sent_parts)
            send_state.setdefault("sent_at", {})[str(index)] = now_iso()
            send_state.setdefault("message_ids", {})[str(index)] = str(message["Message-ID"])
            send_state["status"] = "partial" if len(sent_parts) < total else "sent"
            send_state["last_error"] = None
            if send_state["status"] == "sent":
                send_state["completed_at"] = now_iso()
            _save_job(job)
            reporter.log(f"第 {index}/{total} 卷发送成功。")
            reporter.progress(
                "send", index, total,
                f"已发送 {index}/{total} 卷",
            )
            if index != total and smtp.send_interval > 0:
                _check_cancel(cancel)
                if cancel is not None:
                    if cancel.wait(smtp.send_interval):
                        raise Cancelled("任务已取消。")
                else:
                    time.sleep(smtp.send_interval)

        _close_quietly(connection)
        connection = None
        if len(sent_parts) >= total:
            send_state["status"] = "sent"
            send_state["completed_at"] = now_iso()
        else:
            send_state["status"] = "partial"
        _save_job(job)
        reporter.log(f"发送完成：{len(sent_parts)}/{total} 卷。")
        reporter.progress("done", total, total, "全部发送完成")
        return job
    except Cancelled:
        send_state["status"] = "partial"
        send_state["last_error"] = "用户取消"
        _save_job(job)
        reporter.log("任务已取消，已发送的分卷不会重复发送。")
        raise
    except Exception as exc:
        send_state["status"] = "partial" if len(sent_parts) < total else "sent"
        send_state["last_error"] = str(exc)
        _save_job(job)
        raise
    finally:
        _close_quietly(connection)


def test_smtp(config: SmtpConfig) -> str:
    sender = validate_single_email(config.sender, "发件邮箱")
    auth_code = (config.auth_code or "").replace(" ", "").strip()
    if not auth_code:
        raise SendError("请填写 QQ 邮箱 SMTP 授权码。")
    config.sender = sender
    config.auth_code = auth_code
    reporter = Reporter()
    connection = _open_smtp(config, reporter)
    try:
        code, response = connection.noop()
        if isinstance(response, bytes):
            response = response.decode("utf-8", "replace")
        return f"连接成功（{code} {response}）"
    finally:
        _close_quietly(connection)


# ---------------------------------------------------------------------------
# 配置文件
# ---------------------------------------------------------------------------


def load_config_file(path: Path | str | None) -> dict:
    if not path:
        return {}
    return read_json(Path(path).expanduser())


def _config_value(config: dict, key: str, default=None):
    return config.get(key, default)


def _resolve_prepare(args: argparse.Namespace, config: dict) -> PrepareConfig:
    source_raw = args.source or _config_value(config, "source", "")
    if not source_raw:
        raise ConfigError("请指定要压缩的文件或文件夹（--source）。")
    output_raw = args.output or _config_value(config, "output", str(Path.home() / "QQVolMail"))
    volume = args.volume_mb
    if volume is None:
        volume = _config_value(config, "volume_mb", DEFAULT_VOLUME_MB)
    archive_format = args.format or _config_value(config, "format", "auto")
    level = args.level
    if level is None:
        level = _config_value(config, "level", 5)
    subject_prefix = args.subject_prefix or _config_value(config, "subject_prefix", "文件分卷")
    display_name = args.display_name or _config_value(config, "display_name", APP_NAME)
    sender = args.sender or _config_value(config, "sender", "")
    if args.to:
        recipients_raw: str | list[str] = args.to
    else:
        recipients_raw = _config_value(config, "recipients", [])
    recipients = parse_email_list(recipients_raw) if recipients_raw else []
    return PrepareConfig(
        source=Path(source_raw),
        output_root=Path(output_raw),
        volume_size_mb=int(volume),
        archive_format=str(archive_format),
        compression_level=int(level),
        subject_prefix=str(subject_prefix),
        display_name=str(display_name),
        sender=str(sender).strip(),
        recipients=recipients,
    )


def _resolve_smtp(
    args: argparse.Namespace,
    config: dict,
    sender: str,
    recipients: Sequence[str],
) -> SmtpConfig:
    auth_code = (
        getattr(args, "auth_code", None)
        or os.environ.get(
            getattr(args, "auth_code_env", None)
            or _config_value(config, "auth_code_env", DEFAULT_AUTH_ENV),
            "",
        )
    )
    if not auth_code and sys.stdin.isatty():
        auth_code = getpass.getpass("请输入 QQ 邮箱 SMTP 授权码（输入不显示）：")
    if not auth_code:
        raise ConfigError(
            "未提供 SMTP 授权码。请通过环境变量 QQ_SMTP_AUTH_CODE 设置，"
            "或在终端中运行时按提示输入。"
        )
    return SmtpConfig(
        sender=sender,
        recipients=list(recipients),
        auth_code=auth_code,
        host=getattr(args, "smtp_host", None)
        or _config_value(config, "smtp_host", QQ_SMTP_HOST),
        port=int(
            getattr(args, "smtp_port", None)
            or _config_value(config, "smtp_port", QQ_SMTP_SSL_PORT)
        ),
        security=getattr(args, "smtp_security", None)
        or _config_value(config, "smtp_security", "auto"),
        display_name=getattr(args, "display_name", None)
        or _config_value(config, "display_name", APP_NAME),
        send_interval=float(
            getattr(args, "interval", None)
            if getattr(args, "interval", None) is not None
            else _config_value(config, "send_interval", 3.0)
        ),
        max_retries=int(
            getattr(args, "retries", None)
            if getattr(args, "retries", None) is not None
            else _config_value(config, "max_retries", 3)
        ),
        timeout=float(
            getattr(args, "timeout", None)
            if getattr(args, "timeout", None) is not None
            else _config_value(config, "timeout", 90.0)
        ),
    )


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _add_prepare_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", help="要压缩的文件或文件夹")
    parser.add_argument("--output", help="输出根目录（默认 ~/QQVolMail）")
    parser.add_argument("--volume-mb", type=int, help=f"分卷大小，1~{MAX_VOLUME_MB} MB（默认 20）")
    parser.add_argument("--format", choices=("auto", "7z", "zip"), help="压缩方式")
    parser.add_argument("--level", type=int, help="压缩级别 0~9（默认 5）")
    parser.add_argument("--subject-prefix", help="邮件主题前缀")
    parser.add_argument("--display-name", help="发件人显示名称")
    parser.add_argument("--sender", help="发件 QQ 邮箱")
    parser.add_argument("--to", action="append", help="收件邮箱，可重复或逗号分隔")


def _add_smtp_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--auth-code", help="SMTP 授权码（不推荐，会出现在命令历史中）")
    parser.add_argument("--auth-code-env", help=f"保存授权码的环境变量名（默认 {DEFAULT_AUTH_ENV}）")
    parser.add_argument("--smtp-host", help="SMTP 服务器，默认 smtp.qq.com")
    parser.add_argument("--smtp-port", type=int, help="SMTP 端口，默认 465")
    parser.add_argument("--smtp-security", choices=("auto", "ssl", "starttls"), help="连接加密方式")
    parser.add_argument("--interval", type=float, help="每封邮件间隔秒数，默认 3")
    parser.add_argument("--retries", type=int, help="网络失败重试次数，默认 3")
    parser.add_argument("--timeout", type=float, help="SMTP 超时秒数，默认 90")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqvolmail",
        description="把文件/文件夹压缩成分卷，并通过 QQ 邮箱逐卷发送。",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    prepare_parser = subparsers.add_parser("prepare", help="只压缩分卷，不发送")
    prepare_parser.add_argument("--config", help="JSON 配置文件")
    _add_prepare_options(prepare_parser)

    send_parser = subparsers.add_parser("send", help="压缩分卷并发送")
    send_parser.add_argument("--config", help="JSON 配置文件")
    _add_prepare_options(send_parser)
    _add_smtp_options(send_parser)

    resume_parser = subparsers.add_parser("resume", help="继续发送未完成的任务")
    resume_parser.add_argument("job", help="任务目录或 manifest.json 路径")
    resume_parser.add_argument("--config", help="JSON 配置文件")
    resume_parser.add_argument("--sender", help="仅当任务未记录发件人时使用")
    resume_parser.add_argument("--to", action="append", help="仅当任务未记录收件人时使用")
    resume_parser.add_argument("--force", action="store_true", help="清空发送记录并重发全部分卷")
    _add_smtp_options(resume_parser)

    test_parser = subparsers.add_parser("test", help="测试 QQ 邮箱 SMTP 登录")
    test_parser.add_argument("--config", help="JSON 配置文件")
    test_parser.add_argument("--sender", help="发件 QQ 邮箱")
    test_parser.add_argument("--auth-code", help="SMTP 授权码")
    test_parser.add_argument("--auth-code-env", help=f"授权码环境变量名（默认 {DEFAULT_AUTH_ENV}）")
    test_parser.add_argument("--smtp-host", help="SMTP 服务器")
    test_parser.add_argument("--smtp-port", type=int, help="SMTP 端口")
    test_parser.add_argument("--smtp-security", choices=("auto", "ssl", "starttls"))
    test_parser.add_argument("--timeout", type=float)

    subparsers.add_parser("gui", help="打开图形界面")
    return parser


def _print_log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


class _ConsoleProgress:
    def __init__(self) -> None:
        self._last = ""

    def __call__(self, stage: str, current: int, total: int, text: str) -> None:
        if not text or text == self._last:
            return
        if stage in {"send", "done"}:
            self._last = text
            print(f"  {text}", flush=True)
        elif stage == "compress" and text != self._last:
            self._last = text
            print(f"  {text}", flush=True)


def _run_cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        from qqvolmail_gui import main as gui_main

        gui_main()
        return 0

    config = load_config_file(getattr(args, "config", None))
    reporter = Reporter(on_log=_print_log, on_progress=_ConsoleProgress())

    if args.command == "test":
        sender = args.sender or _config_value(config, "sender", "")
        if not sender:
            raise ConfigError("请指定发件邮箱（--sender）。")
        smtp = _resolve_smtp(args, config, sender, [])
        result = test_smtp(smtp)
        _print_log(result)
        return 0

    if args.command == "prepare":
        prepare_config = _resolve_prepare(args, config)
        job = create_job(prepare_config, reporter)
        _print_log(f"任务目录：{job.job_dir}")
        _print_log(f"共 {len(job.parts)} 个分卷，已生成 manifest.json 和《解压说明.txt》。")
        _print_log(
            "如需稍后发送，请运行：qqvolmail resume "
            f"\"{job.job_dir}\""
        )
        return 0

    if args.command == "send":
        prepare_config = _resolve_prepare(args, config)
        smtp = _resolve_smtp(
            args, config, prepare_config.sender, prepare_config.recipients
        )
        job = create_job(prepare_config, reporter)
        job = send_job(job, smtp, reporter)
        _print_log(f"全部完成。任务目录：{job.job_dir}")
        return 0

    if args.command == "resume":
        job = load_job(Path(args.job))
        sender = (
            args.sender
            or job.data.get("sender")
            or _config_value(config, "sender", "")
        )
        if args.to:
            recipients = parse_email_list(args.to)
        else:
            recipients = list(job.data.get("recipients") or _config_value(config, "recipients", []))
        smtp = _resolve_smtp(args, config, sender, recipients)
        job = send_job(job, smtp, reporter, force=args.force)
        _print_log(f"任务状态：{job.data['send'].get('status')}，目录：{job.job_dir}")
        return 0

    parser.error("未知命令。")
    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _run_cli(list(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except (ConfigError, ArchiveError, SendError, Cancelled) as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 避免 GUI 双击运行时静默退出
        print(f"\n未预期的错误：{exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
