"""整理（archive）与按需重建测试。"""
from __future__ import annotations

import io
import threading
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

from app import archive, config, services
from app.db import get_db


def _upload_batch(client, paths):
    data = MultiDict()
    handles = []
    for p in paths:
        fh = open(p, "rb")
        handles.append(fh)
        data.add("dicom_files", (fh, Path(p).name, "application/dicom"))
    try:
        resp = client.post("/upload", data=data, content_type="multipart/form-data",
                           follow_redirects=False)
    finally:
        for fh in handles:
            fh.close()
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def _zip_members(payload: bytes):
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None
        return [(info.filename, info.date_time, info.compress_type,
                 zf.read(info.filename)) for info in zf.infolist()]


def _download(client, batch_id):
    return client.get(f"/batches/{batch_id}/download")


def _original_zip_path(batch_id: str) -> Path:
    return config.get_batches_dir() / f"{batch_id}.zip"


def _upload_one(client, sample_files):
    return _upload_batch(client, sorted(sample_files.glob("*.dcm")))


# ------------------------------------------------------------------ 整理主流程
def test_archive_rebuild_is_identical(client, sample_files):
    batch_id = _upload_one(client, sample_files)

    before = _download(client, batch_id)
    assert before.status_code == 200
    before_members = _zip_members(before.data)
    before_disposition = before.headers["Content-Disposition"]
    manifest_before = dict((n, b) for n, _, _, b in before_members)["manifest.csv"]

    # 整理前原 ZIP 在磁盘上
    original_zip = _original_zip_path(batch_id)
    assert original_zip.exists()

    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert resp.status_code == 200
    assert "整理完成".encode() in resp.data

    # 旧 ZIP 已删除，重建依据与原 manifest.csv 已持久化
    assert not original_zip.exists()
    job = services.get_archive_job(batch_id)
    assert job is not None
    persisted_manifest = config.get_archives_dir() / batch_id / job["manifest_name"]
    assert persisted_manifest.read_bytes() == manifest_before

    stored = services.get_batch(batch_id)
    assert stored["zip_path"] is None
    assert stored["archive_status"] == "archived"
    assert stored["archive_error"] is None

    # 从原地址按需重建：文件名、成员名/顺序、成员字节全部一致
    after = _download(client, batch_id)
    assert after.status_code == 200
    assert after.headers["Content-Disposition"] == before_disposition
    after_members = _zip_members(after.data)
    assert [m[0] for m in after_members] == [m[0] for m in before_members]
    assert [m[1] for m in after_members] == [m[1] for m in before_members]
    assert [m[2] for m in after_members] == [m[2] for m in before_members]
    assert after_members == before_members

    # 重复整理幂等
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert services.get_archive_job(batch_id) is not None
    assert _download(client, batch_id).status_code == 200


def test_archive_rejects_non_ready_batch(client, tmp_path):
    junk = tmp_path / "junk.dcm"
    junk.write_bytes(b"not a dicom file" * 20)
    batch_id = _upload_batch(client, [junk])
    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "只有已发布的批次".encode() in resp.data
    assert services.get_archive_job(batch_id) is None


# ------------------------------------------------------------------ 核对失败：保留原包
def test_archive_detects_changed_object_and_keeps_zip(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    before = _download(client, batch_id).data

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    obj = config.get_objects_dir() / row["object_path"]
    original = obj.read_bytes()
    obj.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))

    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "整理失败".encode() in resp.data
    assert "不一致".encode() in resp.data

    batch = services.get_batch(batch_id)
    assert batch["archive_status"] == "failed"
    assert batch["archive_error"]
    assert services.get_archive_job(batch_id) is None
    assert _original_zip_path(batch_id).exists()
    assert _download(client, batch_id).data == before  # 原地址仍可下载

    # 恢复正确字节后可重试成功
    obj.write_bytes(original)
    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "整理完成".encode() in resp.data
    assert not _original_zip_path(batch_id).exists()
    assert _download(client, batch_id).data == before


def test_archive_detects_missing_object_and_keeps_zip(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    obj = config.get_objects_dir() / row["object_path"]
    obj.unlink()

    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "共享对象缺失".encode() in resp.data
    assert services.get_archive_job(batch_id) is None
    assert _original_zip_path(batch_id).exists()
    assert _download(client, batch_id).status_code == 200


def test_archive_detects_modified_zip_member(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    zip_path = _original_zip_path(batch_id)
    with zipfile.ZipFile(zip_path) as zf:
        items = [(i.filename, i.date_time, zf.read(i.filename)) for i in zf.infolist()]
    tampered = zip_path.with_suffix(".tmp")
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, dt, data in items:
            zf.writestr(zipfile.ZipInfo(name, dt), data if name == "manifest.csv"
                        else data + b"x")
    tampered.replace(zip_path)

    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "整理失败".encode() in resp.data
    assert services.get_archive_job(batch_id) is None
    assert zip_path.exists()


def test_archive_persist_failure_keeps_zip(client, sample_files, monkeypatch):
    """依据保存（数据库提交）失败时必须保留原包。"""
    batch_id = _upload_one(client, sample_files)

    class Boom(Exception):
        pass

    def broken_tx(_conn):
        raise Boom("模拟数据库写失败")

    monkeypatch.setattr(archive, "immediate_tx", broken_tx)
    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    monkeypatch.undo()
    assert "整理失败".encode() in resp.data

    assert services.get_archive_job(batch_id) is None
    assert _original_zip_path(batch_id).exists()
    assert _download(client, batch_id).status_code == 200
    # 失败可重试
    resp = client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "整理完成".encode() in resp.data


# ------------------------------------------------------------------ 中断重启
def test_interrupted_archive_resumes_after_restart(app, client, sample_files):
    """模拟：依据已提交、ZIP 尚未删除时进程中断；重启后仍可下载且可重试整理。"""
    from app.web import create_app

    batch_id = _upload_one(client, sample_files)
    before = _download(client, batch_id).data
    zip_path = _original_zip_path(batch_id)

    archive.verify_and_persist(batch_id, zip_path, services.download_name(batch_id))
    assert zip_path.exists()  # 模拟删 ZIP 前中断
    conn = get_db()
    try:
        conn.execute("UPDATE batches SET archive_status='archiving' WHERE id=?",
                     (batch_id,))
    finally:
        conn.close()

    # “重启”：新 app 会执行迁移/恢复逻辑，但不能把已提交的整理依据丢掉
    restarted = create_app()
    restarted.config.update(TESTING=True)
    restarted_client = restarted.test_client()
    assert services.get_archive_job(batch_id) is not None
    assert _download(restarted_client, batch_id).data == before

    resp = restarted_client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert "整理完成".encode() in resp.data
    assert not zip_path.exists()
    assert _download(restarted_client, batch_id).data == before


# ------------------------------------------------------------------ 重建失败：明确报错、无半包
def test_rebuild_missing_object_fails_without_package(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert services.get_archive_job(batch_id) is not None

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    (config.get_objects_dir() / row["object_path"]).unlink()

    resp = _download(client, batch_id)
    assert resp.status_code == 409
    assert "无法重建文件包".encode() in resp.data
    assert "缺失".encode() in resp.data
    assert not resp.data.startswith(b"PK")  # 不返回半包


def test_rebuild_changed_object_fails_without_package(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    obj = config.get_objects_dir() / row["object_path"]
    data = bytearray(obj.read_bytes())
    data[-1] ^= 0xFF
    obj.write_bytes(bytes(data))

    resp = _download(client, batch_id)
    assert resp.status_code == 409
    assert "内容已变化".encode() in resp.data
    assert not resp.data.startswith(b"PK")


def test_rebuild_manifest_loss_fails_explicitly(client, sample_files):
    batch_id = _upload_one(client, sample_files)
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    manifest = config.get_archives_dir() / batch_id / "manifest.csv"
    manifest.unlink()

    resp = _download(client, batch_id)
    assert resp.status_code == 409
    assert "manifest.csv 丢失".encode() in resp.data
    assert not resp.data.startswith(b"PK")


# ------------------------------------------------------------------ 整理期间仍可下载
def test_download_available_while_archiving(app, client, sample_files, monkeypatch):
    batch_id = _upload_one(client, sample_files)
    before = _download(client, batch_id).data

    started = threading.Event()
    release = threading.Event()
    real = archive.verify_and_persist

    def slow_verify(*args, **kwargs):
        started.set()
        assert release.wait(timeout=30)
        return real(*args, **kwargs)

    monkeypatch.setattr(archive, "verify_and_persist", slow_verify)
    result: dict = {}

    def do_archive():
        result["resp"] = client.post(
            f"/batches/{batch_id}/archive", follow_redirects=True
        )

    worker = threading.Thread(target=do_archive)
    worker.start()
    try:
        assert started.wait(timeout=30)
        # 整理尚未完成，原地址仍应从原 ZIP 正常下载
        mid = _download(client, batch_id)
        assert mid.status_code == 200
        assert mid.data == before
        assert _original_zip_path(batch_id).exists()
    finally:
        release.set()
        worker.join(timeout=60)
    assert "整理完成".encode() in result["resp"].data
    assert _download(client, batch_id).data == before


def test_concurrent_archive_requests_are_consistent(app, client, sample_files):
    """两个并发整理请求：一个成功，另一个被锁拒绝或幂等返回；结果仍正确。"""
    batch_id = _upload_one(client, sample_files)
    before = _download(client, batch_id).data

    real = archive.verify_and_persist
    entered = threading.Event()
    release = threading.Event()

    def slow_verify(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=30)
        return real(*args, **kwargs)

    original_verify = archive.verify_and_persist
    archive.verify_and_persist = slow_verify
    results: list = []

    def do_archive():
        results.append(client.post(f"/batches/{batch_id}/archive",
                                   follow_redirects=True).data)

    first = threading.Thread(target=do_archive)
    first.start()
    try:
        assert entered.wait(timeout=30)
        second = threading.Thread(target=do_archive)
        second.start()
        second.join(timeout=30)
    finally:
        archive.verify_and_persist = original_verify
        release.set()
        first.join(timeout=60)

    assert len(results) == 2
    # 一个提示整理完成，另一个被拒绝（正在整理中）或被幂等处理
    joined = b"".join(results)
    assert "整理完成".encode() in joined or "正在整理中".encode() in joined
    assert services.get_archive_job(batch_id) is not None
    assert _download(client, batch_id).data == before


def test_crashed_archive_marker_recovered(app, client, sample_files):
    """崩溃遗留的 archiving 标记在重启后可重试（flock 已随进程释放）。"""
    from app.web import create_app

    batch_id = _upload_one(client, sample_files)
    before = _download(client, batch_id).data
    conn = get_db()
    try:
        conn.execute(
            "UPDATE batches SET archive_status='archiving', "
            "archive_error=NULL WHERE id=?", (batch_id,)
        )
    finally:
        conn.close()

    restarted = create_app()  # 启动恢复
    restarted.config.update(TESTING=True)
    restarted_client = restarted.test_client()
    batch = services.get_batch(batch_id)
    assert batch["archive_status"] == "failed"
    assert "可重试" in batch["archive_error"]

    resp = restarted_client.post(f"/batches/{batch_id}/archive",
                                 follow_redirects=True)
    assert "整理完成".encode() in resp.data
    assert _download(restarted_client, batch_id).data == before


def test_concurrent_archive_file_lock(app):
    """直接验证批次锁：同批次第二个持有者被拒绝，释放后可再次获取。"""
    batch_id = "locktest"
    with archive.batch_lock(batch_id):
        with pytest.raises(archive.ArchiveBusy):
            with archive.batch_lock(batch_id):
                pass
    with archive.batch_lock(batch_id):
        pass


def test_archive_verify_streams_large_members(client, sample_files, monkeypatch):
    """整理核对 16MiB 成员时峰值内存应远小于包体积。"""
    batch_id = _upload_one(client, sample_files)
    zip_path = _original_zip_path(batch_id)
    with zipfile.ZipFile(zip_path) as zf:
        members = [(i.filename, i.date_time, i.compress_type, zf.read(i.filename))
                   for i in zf.infolist()]

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path, i.new_sop_uid FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    target_member = f"{row['new_sop_uid']}.dcm"
    obj = config.get_objects_dir() / row["object_path"]
    original = obj.read_bytes()

    big_size = 16 * 1024 * 1024
    enlarged = original + b"\0" * (big_size - len(original))
    obj.write_bytes(enlarged)

    # 重写原 ZIP，使该成员与放大后的对象逐字节一致
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, dt, compress, data in members:
            info = zipfile.ZipInfo(name, dt)
            info.compress_type = compress
            zf.writestr(info, enlarged if name == target_member else data)

    monkeypatch.setattr(archive, "CHUNK_SIZE", 64 * 1024)
    tracemalloc.start()
    services.archive_batch(batch_id)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert services.get_archive_job(batch_id) is not None
    assert not zip_path.exists()
    assert peak < 8 * 1024 * 1024, f"峰值内存 {peak} 过大，疑似整包载入内存"

    resp = _download(client, batch_id)
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.getinfo(target_member).file_size == big_size


def test_rebuilt_temp_package_cleaned_after_send(app, client, sample_files):
    """重建临时包在响应发送完成后删除，不占磁盘。"""
    from werkzeug.test import EnvironBuilder

    batch_id = _upload_one(client, sample_files)
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    rebuild_dir = config.get_upload_tmp_dir() / "rebuild"

    env = EnvironBuilder(path=f"/batches/{batch_id}/download", method="GET") \
        .get_environ()
    status_holder: dict = {}

    def start_response(status, headers, exc_info=None):
        status_holder["status"] = status
        return lambda _data: None

    app_iter = app.wsgi_app(env, start_response)
    try:
        payload = b"".join(app_iter)
    finally:
        app_iter.close()

    assert status_holder["status"].startswith("200")
    assert payload.startswith(b"PK")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None
    assert list(rebuild_dir.glob("*")) == []


# ------------------------------------------------------------------ 常量内存
def test_rebuild_does_not_load_whole_package(client, sample_files, tmp_path,
                                             monkeypatch):
    """重建大对象时峰值内存应远小于包体积。"""
    import hashlib

    batch_id = _upload_one(client, sample_files)
    client.post(f"/batches/{batch_id}/archive", follow_redirects=True)
    assert services.get_archive_job(batch_id) is not None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.object_path FROM batch_files bf "
            "JOIN instances i ON i.sop_uid=bf.sop_uid WHERE bf.batch_id=? LIMIT 1",
            (batch_id,),
        ).fetchone()
        entry = conn.execute(
            "SELECT member_name FROM archive_entries WHERE batch_id=? "
            "AND object_path=?",
            (batch_id, row["object_path"]),
        ).fetchone()
    finally:
        conn.close()
    obj = config.get_objects_dir() / row["object_path"]
    original = obj.read_bytes()

    # 把一个共享对象放大到 16MiB，并同步更新重建依据里的期望值
    big_size = 16 * 1024 * 1024
    with open(obj, "ab") as fh:
        fh.write(b"\0" * (big_size - len(original)))
    h = hashlib.sha256()
    with open(obj, "rb") as fh:
        for chunk in iter(lambda: fh.read(archive.CHUNK_SIZE), b""):
            h.update(chunk)
    conn = get_db()
    try:
        conn.execute(
            "UPDATE archive_entries SET size=?, sha256=? WHERE batch_id=? AND seq=?",
            (big_size, h.hexdigest(), batch_id,
             conn.execute("SELECT seq FROM archive_entries WHERE batch_id=? AND "
                          "member_name=?", (batch_id, entry["member_name"])).fetchone()["seq"]),
        )
    finally:
        conn.close()

    # 临时增大分块，让固定缓冲在 16MiB 包体下仍可被明确测量与区分
    monkeypatch.setattr(archive, "CHUNK_SIZE", 64 * 1024)

    dest = tmp_path / "rebuilt.zip"
    tracemalloc.start()
    archive.rebuild_to(batch_id, dest)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert dest.stat().st_size > 0
    # 若整包载入内存，峰值至少 16MiB；流式应远低于此
    assert peak < 4 * 1024 * 1024, f"峰值内存 {peak} 过大，疑似整包载入内存"
    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        info = zf.getinfo(entry["member_name"])
        assert info.file_size == big_size


# ------------------------------------------------------------------ 兼容旧数据库
def test_legacy_database_migrates(tmp_path, monkeypatch):
    """旧版本数据库（batches 无整理字段）启动时自动迁移且批次仍可整理。"""
    import sqlite3

    monkeypatch.setenv("DICOM_DATA_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("DICOM_UID_PREFIX", "1.2.826.0.9999")
    data_dir = tmp_path / "legacy"
    data_dir.mkdir(parents=True)
    conn = sqlite3.connect(data_dir / "dicom.db")
    try:
        conn.executescript(
            """
            CREATE TABLE batches (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                original_count INTEGER NOT NULL DEFAULT 0,
                zip_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            """
        )
    finally:
        conn.close()

    from app.web import create_app
    application = create_app()
    application.config.update(TESTING=True)
    conn = get_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
    finally:
        conn.close()
    assert {"archive_status", "archive_error", "archived_at"} <= cols
