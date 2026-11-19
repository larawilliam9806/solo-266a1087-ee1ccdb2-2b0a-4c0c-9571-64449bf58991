"""运行配置。所有路径与密钥前缀均可通过环境变量覆盖。

路径类配置在访问时读取环境变量，便于测试在不重载模块的情况下隔离数据目录。
"""
from __future__ import annotations

import os
from pathlib import Path

# 允许上传的单请求总大小（默认 2GB）
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(2 * 1024 ** 3)))

# 确定性 UID 派生所用组织前缀（必须是合法 DICOM UID 前缀，且派生结果 <= 64 字符）。
# 默认使用 IANA 测试弧 1.2.826.0.1，仅用于测试；生产部署必须替换成机构自己的合法前缀。
DEFAULT_UID_PREFIX = "1.2.826.0.1.3680043.10.9999"

# SQLite 写锁等待（毫秒），配合 WAL 支持多 worker 并发上传
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "30000"))


class _Paths:
    """延迟解析的数据目录视图，每次属性访问都反映当前环境变量。"""

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("DICOM_DATA_DIR", "/data")).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dicom.db"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def upload_tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"


_paths = _Paths()
# 向后兼容的模块级名称（首次导入时快照；运行时请使用 get_* 函数）
DATA_DIR = _paths.data_dir
DB_PATH = _paths.db_path
OBJECTS_DIR = _paths.objects_dir
UPLOAD_TMP_DIR = _paths.upload_tmp_dir
BATCHES_DIR = _paths.batches_dir


def get_db_path() -> Path:
    return _paths.db_path


def get_objects_dir() -> Path:
    return _paths.objects_dir


def get_upload_tmp_dir() -> Path:
    return _paths.upload_tmp_dir


def get_batches_dir() -> Path:
    return _paths.batches_dir


def get_uid_prefix() -> str:
    prefix = os.environ.get("DICOM_UID_PREFIX", DEFAULT_UID_PREFIX)
    validate_uid_prefix(prefix)
    return prefix


def __getattr__(name):  # PEP 562：config.UID_PREFIX 动态读取环境变量
    if name == "UID_PREFIX":
        return get_uid_prefix()
    raise AttributeError(name)


def validate_uid_prefix(prefix: str) -> None:
    """校验 UID 前缀合法性，避免生成非法或超长 DICOM UID。"""
    if not prefix or not all(c.isdigit() for c in prefix.replace(".", "")):
        raise ValueError(f"DICOM_UID_PREFIX 非法（只能含数字与点）: {prefix!r}")
    parts = prefix.split(".")
    if any(part == "" or part[0] == "0" and len(part) > 1 for part in parts):
        raise ValueError(f"DICOM_UID_PREFIX 含空段或前导零段: {prefix!r}")
    # 前缀 + ".0."(3) + 31 位尾段 <= 64，故前缀最长 30 字符
    if len(prefix) > 30:
        raise ValueError("DICOM_UID_PREFIX 前缀过长，无法保证派生 UID <= 64 字符")


def ensure_dirs() -> None:
    for path in (
        _paths.data_dir,
        _paths.objects_dir,
        _paths.upload_tmp_dir,
        _paths.batches_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
