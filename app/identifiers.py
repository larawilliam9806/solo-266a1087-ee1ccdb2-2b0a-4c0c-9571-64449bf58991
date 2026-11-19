"""确定性新标识派生。

同一原标识在任何时间、任何批次、任何并发 worker 上都派生出同一个新标识，
因此“引用先上传、实例后上传”也天然一致，且不依赖先写库再查库的顺序。
派生结果同时持久化到 SQLite 作为审计与数据湖关联依据。
"""
from __future__ import annotations

import hashlib
import uuid

from . import config

# 固定命名空间（本应用自有常量，不随部署变化，保证跨重启可复现）
_NAMESPACE = uuid.UUID("0c17c8b5-7e92-4f1a-9d3a-2e6b4f8a1c10")

# 十进制尾段最大位数：与前缀校验（<=60 字符）配合，保证整体 UID <= 64 字符。
# 31 位十进制约 103 bit 输出空间，碰撞概率可忽略。
_MAX_DIGITS = 31
# 31 位十进制的上界（10^31），用于把 128bit 摘要等映射到定长区间
_MOD = 10 ** _MAX_DIGITS


def _digest(kind: str, original: str) -> int:
    """128 位整数摘要。kind 隔离不同角色，防止不同角色的同值 UID 串号。"""
    h = hashlib.sha256(f"{kind}\x1f{original}".encode("utf-8")).digest()
    return int.from_bytes(h[:16], "big")


def derive_uid(role: str, original_uid: str) -> str:
    """由 (角色, 原 UID) 确定性派生合法 DICOM UID。

    结构：<机构前缀>.0.<最多 31 位十进制整数>。尾段来自 128bit 摘要取模，
    首位可能为 0 时补齐为非零引导（DICOM 各段不允许无意义前导零），整体 <= 64 字符。
    """
    if role not in ("study", "series", "sop"):
        raise ValueError(f"未知 UID 角色: {role}")
    n = _digest(role, original_uid) % _MOD
    suffix = str(n)
    if suffix[0] == "0":
        # 用非零数字替换前导零，保持定长且不引入前导零段
        suffix = "9" + suffix[1:]
    new_uid = f"{config.get_uid_prefix()}.0.{suffix}"
    if len(new_uid) > 64:
        raise RuntimeError(
            f"派生 UID 超过 64 字符（{len(new_uid)}），请缩短 DICOM_UID_PREFIX"
        )
    return new_uid


def derive_patient_id(original_patient_id: str) -> str:
    """确定性派生 12 位数字患者 ID（零填充，定长便于数据湖关联）。"""
    n = _digest("patient", original_patient_id)
    return f"P{n % 10**12:012d}"
