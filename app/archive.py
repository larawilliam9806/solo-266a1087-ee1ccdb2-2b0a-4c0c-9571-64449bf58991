"""批次整理（archive）与按需重建（rebuild）。

整理流程（全部流式处理，绝不把整包载入内存）：

1. 打开原 ZIP，逐个成员与共享对象存储中的文件做**逐字节**比对；
2. 把原 ZIP 的成员元数据（成员名、顺序、压缩方式、时间戳、权限等）以及
   原 ``manifest.csv`` 的原始字节持久化下来，作为重建依据；
3. 上述依据落盘并提交数据库后，才删除旧 ZIP。

重建流程从对象存储按记录的顺序和元数据重新打包，成员内容、成员名、下载文件名
与原包一致。任何对象缺失/内容变化都会在打包**完成前**报错，因此不会返回半包。

关键不变量：
- 依据未持久化成功（数据库提交）之前，原 ZIP 一定保留；
- 依据持久化之后再删除 ZIP，故中断后既能继续下载（原包仍在），也能重试整理
  （依据已在则直接收尾）。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config
from .db import get_db, immediate_tx

MANIFEST_NAME = "manifest.csv"
# 读写分块大小：保证整理/重建时的常驻内存与包大小无关
CHUNK_SIZE = 1024 * 1024

# 允许重建的压缩方式（本服务自己产出的包只会用到这两种）
_SUPPORTED_COMPRESS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


class ArchiveError(Exception):
    """整理或重建失败；调用方必须保证不返回半包、不删除原包。"""


class ArchiveBusy(Exception):
    """同一批次已有整理任务在执行。"""


# ------------------------------------------------------------------ 批次锁
def _lock_path(batch_id: str) -> Path:
    return config.get_archives_dir() / batch_id / ".archive.lock"


@contextmanager
def batch_lock(batch_id: str):
    """跨进程串行化同一批次的整理。

    使用 flock：进程崩溃（含 kill -9）时内核自动释放，重启后可立即重试整理；
    因此不需要把“整理中”状态当作永久锁。
    """
    path = _lock_path(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ArchiveBusy("该批次正在整理中，请稍后刷新查看") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def is_batch_locked(batch_id: str) -> bool:
    """判断当前是否有进程正在整理该批次（不阻塞、不创建新目录）。"""
    if not _lock_path(batch_id).exists():
        return False
    try:
        with batch_lock(batch_id):
            return False
    except ArchiveBusy:
        return True


def reclaim_orphaned_archives() -> int:
    """删除已持久化整理依据、但因中断未删掉的原 ZIP。

    重建依据已提交，因此删除残留原包是安全的；下载会改为按需重建。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, zip_path FROM batches WHERE zip_path IS NOT NULL "
            "AND id IN (SELECT batch_id FROM archive_jobs)"
        ).fetchall()
    finally:
        conn.close()
    removed = 0
    for row in rows:
        try:
            with batch_lock(row["id"]):
                Path(row["zip_path"]).unlink(missing_ok=True)
                _fsync_dir(Path(row["zip_path"]).parent)
                conn = get_db()
                try:
                    with immediate_tx(conn):
                        conn.execute(
                            "UPDATE batches SET zip_path=NULL, archive_status='archived' "
                            "WHERE id=?",
                            (row["id"],),
                        )
                finally:
                    conn.close()
                removed += 1
        except ArchiveBusy:
            continue
        except OSError:
            continue
    return removed


def recover_interrupted_archives() -> list[str]:
    """启动时清理崩溃遗留的“整理中”标记，使其可重试。

    只有拿得到批次锁才说明确实没有进程在整理；此时把状态标为 failed 并
    保留可读错误。已持久化的重建依据（archive_jobs）始终保留。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM batches WHERE archive_status='archiving' "
            "AND id NOT IN (SELECT batch_id FROM archive_jobs)"
        ).fetchall()
    finally:
        conn.close()
    recovered: list[str] = []
    for row in rows:
        try:
            with batch_lock(row["id"]):
                conn = get_db()
                try:
                    with immediate_tx(conn):
                        conn.execute(
                            "UPDATE batches SET archive_status='failed', "
                            "archive_error='服务重启，整理中断，可重试' "
                            "WHERE id=? AND archive_status='archiving'",
                            (row["id"],),
                        )
                finally:
                    conn.close()
                recovered.append(row["id"])
        except ArchiveBusy:
            continue
    return recovered


# ------------------------------------------------------------------ 基础工具
def _read_exact(fh, size: int) -> bytes:
    """尽量读满 size 字节（zip/普通文件流都可能短读）。

    分块累加而不是一次性 fh.read(size)，避免超大成员放大单次分配。
    """
    buf = bytearray()
    while len(buf) < size:
        chunk = fh.read(min(65536, size - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as fh:
        os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    """尽力 fsync 目录项，保证 rename 后的目录结构落盘。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# 公开别名：供批次编排层在删除旧 ZIP 后同步目录项
fsync_dir = _fsync_dir


def _format_date_time(dt: tuple[int, int, int, int, int, int]) -> str:
    return "%04d-%02d-%02dT%02d:%02d:%02d" % tuple(dt)


def _parse_date_time(value: str) -> tuple[int, int, int, int, int, int]:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise ArchiveError(f"重建依据中的成员时间戳非法: {value!r}") from exc
    return (
        parsed.year, parsed.month, parsed.day,
        parsed.hour, parsed.minute, parsed.second,
    )


def _object_abs_path(object_relpath: str) -> Path:
    return config.get_objects_dir() / object_relpath


def get_archive_job(batch_id: str):
    """返回整理依据记录；不存在表示该批次尚未完成整理。"""
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM archive_jobs WHERE batch_id=?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()


def get_archive_entries(batch_id: str):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM archive_entries WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()


def _expected_members(conn, batch_id: str) -> dict[str, str]:
    """ZIP 成员名 -> 共享对象相对路径，来自本批次已入库实例。"""
    rows = conn.execute(
        "SELECT bf.sop_uid, i.new_sop_uid, i.object_path "
        "FROM batch_files bf JOIN instances i ON i.sop_uid = bf.sop_uid "
        "WHERE bf.batch_id=?",
        (batch_id,),
    ).fetchall()
    expected: dict[str, str] = {}
    for row in rows:
        name = f"{row['new_sop_uid']}.dcm"
        if name in expected:
            raise ArchiveError(f"批次内出现重复的包内成员名: {name}")
        expected[name] = row["object_path"]
    if not expected:
        raise ArchiveError("批次没有可核对的影像文件")
    return expected


def _copy_member_to_file(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                         target: Path) -> tuple[str, int]:
    """把 ZIP 成员流式写入文件，返回 (sha256, 字节数)。"""
    h = hashlib.sha256()
    size = 0
    with zf.open(info) as src, open(target, "wb") as dst:
        while True:
            chunk = _read_exact(src, CHUNK_SIZE)
            if not chunk:
                break
            dst.write(chunk)
            h.update(chunk)
            size += len(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return h.hexdigest(), size


def _compare_member_with_object(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                                object_path: Path) -> tuple[str, int]:
    """逐字节比对 ZIP 成员与共享对象，返回对象 (sha256, 字节数)。"""
    if not object_path.exists():
        raise ArchiveError(f"共享对象缺失: {object_path}")
    obj_size = object_path.stat().st_size
    if obj_size != info.file_size:
        raise ArchiveError(
            f"共享对象大小与原 ZIP 成员不一致: {info.filename} "
            f"(对象 {obj_size} 字节，ZIP 成员 {info.file_size} 字节)"
        )
    h = hashlib.sha256()
    total = 0
    with zf.open(info) as src, open(object_path, "rb") as obj:
        while True:
            from_zip = _read_exact(src, CHUNK_SIZE)
            from_obj = _read_exact(obj, CHUNK_SIZE)
            if from_zip != from_obj:
                raise ArchiveError(
                    f"共享对象内容与原 ZIP 成员不一致: {info.filename}"
                )
            if not from_zip:
                break
            h.update(from_zip)
            total += len(from_zip)
    return h.hexdigest(), total


def _validate_info(info: zipfile.ZipInfo) -> None:
    if info.is_dir():
        raise ArchiveError(f"原 ZIP 含目录条目，无法作为影像包重建: {info.filename}")
    if info.compress_type not in _SUPPORTED_COMPRESS:
        raise ArchiveError(
            f"原 ZIP 成员 {info.filename} 使用了不支持的压缩方式 "
            f"{info.compress_type}，无法保证重建一致"
        )
    year = info.date_time[0]
    if not 1980 <= year <= 2107:
        raise ArchiveError(
            f"原 ZIP 成员 {info.filename} 的时间戳超出可重建范围: {info.date_time}"
        )


# ------------------------------------------------------------------ 整理
def verify_and_persist(batch_id: str, zip_path: Path, download_name: str) -> None:
    """核对原 ZIP 与共享对象并把重建依据持久化。

    只有在全部核对通过后才写数据库；数据库提交是整理的提交点。
    任何失败都会保留原 ZIP，并抛出 ArchiveError。
    """
    if not zip_path.exists():
        raise ArchiveError(f"原 ZIP 缺失，无法核对: {zip_path}")

    archives_root = config.get_archives_dir()
    archives_dir = archives_root / batch_id
    archives_dir.mkdir(parents=True, exist_ok=True)
    manifest_tmp = archives_dir / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    manifest_final = archives_dir / MANIFEST_NAME

    conn = get_db()
    try:
        expected = _expected_members(conn, batch_id)
        entries: list[dict] = []
        seen: set[str] = set()
        manifest_checksum = ""
        manifest_size = 0
        manifest_seen = False

        try:
            with zipfile.ZipFile(zip_path) as zf:
                for seq, info in enumerate(zf.infolist()):
                    _validate_info(info)
                    if info.filename in seen:
                        raise ArchiveError(
                            f"原 ZIP 含重复成员名，无法无歧义重建: {info.filename}"
                        )
                    seen.add(info.filename)

                    if info.filename == MANIFEST_NAME:
                        if manifest_seen:
                            raise ArchiveError("原 ZIP 含多个 manifest.csv")
                        manifest_seen = True
                        manifest_checksum, manifest_size = _copy_member_to_file(
                            zf, info, manifest_tmp
                        )
                        entries.append({
                            "seq": seq,
                            "member_name": info.filename,
                            "object_path": "",
                            "size": info.file_size,
                            "sha256": manifest_checksum,
                            "compress_type": info.compress_type,
                            "date_time": _format_date_time(info.date_time),
                            "external_attr": info.external_attr,
                            "create_system": info.create_system,
                        })
                        continue

                    object_relpath = expected.get(info.filename)
                    if object_relpath is None:
                        raise ArchiveError(
                            f"原 ZIP 成员无法映射到共享对象: {info.filename}"
                        )
                    checksum, size = _compare_member_with_object(
                        zf, info, _object_abs_path(object_relpath)
                    )
                    entries.append({
                        "seq": seq,
                        "member_name": info.filename,
                        "object_path": object_relpath,
                        "size": size,
                        "sha256": checksum,
                        "compress_type": info.compress_type,
                        "date_time": _format_date_time(info.date_time),
                        "external_attr": info.external_attr,
                        "create_system": info.create_system,
                    })

            if not manifest_seen:
                raise ArchiveError("原 ZIP 缺少 manifest.csv，无法保存重建依据")
            missing = set(expected) - seen
            if missing:
                raise ArchiveError(
                    "原 ZIP 缺少以下共享对象成员: " + ", ".join(sorted(missing))
                )

            # 先让 manifest 原始字节原子落盘，再提交数据库（提交点）
            os.replace(manifest_tmp, manifest_final)
            _fsync_file(manifest_final)
            _fsync_dir(archives_dir)

            with immediate_tx(conn):
                conn.execute(
                    "DELETE FROM archive_entries WHERE batch_id=?", (batch_id,)
                )
                conn.execute("DELETE FROM archive_jobs WHERE batch_id=?", (batch_id,))
                conn.execute(
                    "INSERT INTO archive_jobs(batch_id, download_name, manifest_name, "
                    "manifest_sha256, manifest_size, entry_count) VALUES(?,?,?,?,?,?)",
                    (batch_id, download_name, MANIFEST_NAME,
                     manifest_checksum, manifest_size, len(entries)),
                )
                conn.executemany(
                    "INSERT INTO archive_entries(batch_id, seq, member_name, object_path, "
                    "size, sha256, compress_type, date_time, external_attr, create_system) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        (batch_id, e["seq"], e["member_name"], e["object_path"],
                         e["size"], e["sha256"], e["compress_type"], e["date_time"],
                         e["external_attr"], e["create_system"])
                        for e in entries
                    ],
                )
                conn.execute(
                    "UPDATE batches SET archive_error=NULL WHERE id=?", (batch_id,)
                )
        finally:
            manifest_tmp.unlink(missing_ok=True)
    finally:
        conn.close()


# ------------------------------------------------------------------ 重建
def _iter_verified_file(path: Path, expected_sha: str, expected_size: int,
                        member_name: str, what: str):
    """流式产出一个文件的内容，同时校验大小与 sha256。

    校验失败时抛出 ArchiveError；由于调用方先写临时 ZIP、全部成功后才发送，
    失败不会产生半包。
    """
    if not path.exists():
        raise ArchiveError(f"{what}缺失: {path}")
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = _read_exact(fh, CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
            yield chunk
    if total != expected_size:
        raise ArchiveError(
            f"{what}大小已变化: {member_name}（期望 {expected_size}，实际 {total}）"
        )
    if h.hexdigest() != expected_sha:
        raise ArchiveError(f"{what}内容已变化: {member_name}")


def _validate_archive_record(job, entries) -> None:
    """重建前复核依据自洽，避免按残缺记录打出错误包。"""
    names: set[str] = set()
    manifest_count = 0
    download_name = job["download_name"]
    if not download_name or "/" in download_name or "\\" in download_name:
        raise ArchiveError(f"重建依据中的下载文件名非法: {download_name!r}")
    for entry in entries:
        name = entry["member_name"]
        if not name or name.endswith("/"):
            raise ArchiveError(f"重建依据中的成员名非法: {name!r}")
        if name in names:
            raise ArchiveError(f"重建依据含重复成员名: {name}")
        names.add(name)
        if entry["compress_type"] not in _SUPPORTED_COMPRESS:
            raise ArchiveError(f"重建依据含不支持的压缩方式: {name}")
        if name == job["manifest_name"]:
            manifest_count += 1
        elif not entry["object_path"]:
            raise ArchiveError(f"重建依据缺少对象路径: {name}")
    if manifest_count != 1:
        raise ArchiveError("重建依据必须且只能包含一个原 manifest.csv")


def rebuild_to(batch_id: str, dest_path: Path) -> None:
    """按持久化的重建依据把批次重新打包到 dest_path（流式，常量内存）。"""
    job = get_archive_job(batch_id)
    if job is None:
        raise ArchiveError("批次尚未整理，缺少重建依据")
    entries = get_archive_entries(batch_id)
    if not entries:
        raise ArchiveError("批次缺少重建依据明细")
    _validate_archive_record(job, entries)

    manifest_path = config.get_archives_dir() / batch_id / job["manifest_name"]
    if not manifest_path.exists():
        raise ArchiveError("原 manifest.csv 丢失，无法重建")

    try:
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED,
                             allowZip64=True) as zf:
            for entry in entries:
                info = zipfile.ZipInfo(
                    entry["member_name"],
                    date_time=_parse_date_time(entry["date_time"]),
                )
                info.compress_type = entry["compress_type"]
                info.create_system = entry["create_system"]
                info.external_attr = entry["external_attr"]

                if entry["member_name"] == job["manifest_name"]:
                    source = manifest_path
                    what = "原 manifest.csv "
                else:
                    if not entry["object_path"]:
                        raise ArchiveError(
                            f"重建依据缺少对象路径: {entry['member_name']}"
                        )
                    source = _object_abs_path(entry["object_path"])
                    what = "共享对象"

                chunks = _iter_verified_file(
                    source, entry["sha256"], entry["size"],
                    entry["member_name"], what,
                )
                force_zip64 = entry["size"] >= zipfile.ZIP64_LIMIT
                with zf.open(info, "w", force_zip64=force_zip64) as dst:
                    for chunk in chunks:
                        dst.write(chunk)
        _fsync_file(dest_path)
    except ArchiveError:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转为可读的整理/重建错误
        dest_path.unlink(missing_ok=True)
        raise ArchiveError(f"重建打包失败: {exc}") from exc


def build_temp_zip(batch_id: str) -> Path:
    """重建到独立临时文件，返回路径；调用方负责发送后清理。"""
    tmp_dir = config.get_upload_tmp_dir() / "rebuild"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{batch_id}-", suffix=".zip", dir=tmp_dir)
    os.close(fd)
    path = Path(name)
    try:
        rebuild_to(batch_id, path)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def cleanup_stale_rebuilds(max_age_seconds: int = 3600) -> None:
    """清理残留的临时重建包（例如客户端中断后遗留），避免占满磁盘。"""
    tmp_dir = config.get_upload_tmp_dir() / "rebuild"
    if not tmp_dir.exists():
        return
    now = datetime.now().timestamp()
    for path in tmp_dir.iterdir():
        try:
            if path.is_file() and now - path.stat().st_mtime > max_age_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            continue
