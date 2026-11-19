"""SQLite 连接管理与建表/恢复逻辑。

设计要点：
- WAL + busy_timeout + 每次写事务使用 IMMEDIATE，保证多 gunicorn worker 并发安全；
- 标识映射（UID/PatientID）全局唯一且只插入、不更新，保证跨批次一致与重试不变号；
- 实例按 (原 SOP UID, 原文件 sha256) 去重，字节不同直接冲突，由上层整批拒绝。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS uid_mappings (
    role          TEXT NOT NULL CHECK (role IN ('study','series','sop')),
    original_uid  TEXT NOT NULL,
    new_uid       TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (role, original_uid)
);

CREATE TABLE IF NOT EXISTS patient_mappings (
    original_patient_id TEXT PRIMARY KEY,
    new_patient_id      TEXT NOT NULL UNIQUE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS instances (
    sop_uid        TEXT PRIMARY KEY,           -- 原 SOPInstanceUID
    sha256         TEXT NOT NULL,
    new_sop_uid    TEXT NOT NULL UNIQUE,
    object_path    TEXT NOT NULL,              -- 处理结果在对象存储中的相对路径
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id               TEXT PRIMARY KEY,
    status           TEXT NOT NULL CHECK (status IN ('processing','ready','failed')),
    original_count   INTEGER NOT NULL DEFAULT 0,
    zip_path         TEXT,
    error            TEXT,
    archive_status   TEXT,                     -- NULL / archiving / archived / failed
    archive_error    TEXT,
    archived_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS batch_files (
    batch_id              TEXT NOT NULL REFERENCES batches(id),
    sop_uid               TEXT NOT NULL,                  -- 原 SOP UID
    orig_name             TEXT NOT NULL,
    original_patient_id   TEXT NOT NULL DEFAULT '',
    original_study_uid    TEXT NOT NULL DEFAULT '',
    original_series_uid   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, sop_uid)
);

CREATE INDEX IF NOT EXISTS idx_batch_files_sop ON batch_files(sop_uid);
CREATE INDEX IF NOT EXISTS idx_instances_sha  ON instances(sha256);

-- 整理（archive）后的重建依据。
-- archive_jobs 有记录即表示重建依据已完整持久化（整理的提交点）；
-- 只有在此之后才允许删除原 ZIP，下载时改为按需重建。
CREATE TABLE IF NOT EXISTS archive_jobs (
    batch_id         TEXT PRIMARY KEY REFERENCES batches(id),
    download_name    TEXT NOT NULL,             -- 下载文件名（与原包一致）
    manifest_name    TEXT NOT NULL,             -- 原 manifest.csv 的成员名
    manifest_sha256  TEXT NOT NULL,
    manifest_size    INTEGER NOT NULL,
    entry_count      INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS archive_entries (
    batch_id       TEXT NOT NULL
                   REFERENCES archive_jobs(batch_id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,            -- 原 ZIP 中的成员顺序
    member_name    TEXT NOT NULL,               -- 原 ZIP 成员名
    object_path    TEXT NOT NULL DEFAULT '',    -- 共享对象相对路径（manifest 为空）
    size           INTEGER NOT NULL,
    sha256         TEXT NOT NULL,
    compress_type  INTEGER NOT NULL,
    date_time      TEXT NOT NULL,               -- YYYY-MM-DDTHH:MM:SS
    external_attr  INTEGER NOT NULL,
    create_system  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, seq),
    UNIQUE (batch_id, member_name)
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.get_db_path(),
        timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 手动事务
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={config.SQLITE_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 事务，避免写-写竞争下的 deferred 死锁。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """对既有数据库做向前兼容的增量迁移（幂等）。"""
    batch_cols = _column_names(conn, "batches")
    if "archive_status" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN archive_status TEXT")
    if "archive_error" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN archive_error TEXT")
    if "archived_at" not in batch_cols:
        conn.execute("ALTER TABLE batches ADD COLUMN archived_at TEXT")
    # 兼容早期数据库：有重建依据就视为已整理
    conn.execute(
        "UPDATE batches SET archive_status='archived' "
        "WHERE status='ready' AND archive_status IS NULL "
        "AND id IN (SELECT batch_id FROM archive_jobs)"
    )


def init_db() -> None:
    config.ensure_dirs()
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()


def recover_interrupted_batches() -> list[str]:
    """进程启动时把上次中断遗留的 processing 批次标记为 failed。

    已成功入库的 instances/映射一律保留（它们本身是有效的、可被后续批次复用）；
    没有生成 zip 的批次绝不可能以 ready 状态出现，因此不存在可下载的半批。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM batches WHERE status='processing'"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.execute(
                "UPDATE batches SET status='failed', error='服务重启，批次中断', "
                "finished_at=datetime('now') WHERE status='processing'"
            )
        return ids
    finally:
        conn.close()
