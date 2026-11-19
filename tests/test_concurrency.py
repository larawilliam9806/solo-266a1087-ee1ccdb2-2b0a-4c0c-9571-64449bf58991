"""跨进程并发安全测试：模拟 gunicorn 多 worker 同时写同一数据目录。"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import pydicom
import pytest


def _worker(data_dir: str, uid_prefix: str, evil: bool, samples_dir: str, queue) -> None:
    """子进程：建 app 并上传一批文件，把批次状态放回队列。"""
    os.environ["DICOM_DATA_DIR"] = data_dir
    os.environ["DICOM_UID_PREFIX"] = uid_prefix
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from werkzeug.datastructures import MultiDict  # noqa: E402
    from app.web import create_app  # noqa: E402

    client = create_app().test_client()
    if evil:
        import tempfile
        ds = pydicom.dcmread(str(Path(samples_dir) / "ct_001.dcm"))
        ds.PixelData = bytes([0x77]) * len(ds.PixelData)
        tmp = Path(tempfile.mkdtemp()) / "ct_evil.dcm"
        ds.save_as(str(tmp), write_like_original=False)
        paths = [tmp]
    else:
        paths = sorted(Path(samples_dir).glob("*.dcm"))

    data = MultiDict()
    for p in paths:
        data.add("dicom_files", (open(p, "rb"), p.name, "application/dicom"))
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    bid = resp.headers["Location"].rstrip("/").split("/")[-1]
    # 以“是否可下载”作为成功判据：失败/中断批次一律返回 409
    download_status = client.get(f"/batches/{bid}/download").status_code
    status = "ready" if download_status == 200 else "failed"
    queue.put((evil, status, bid))


@pytest.mark.parametrize("round_idx", range(3))
def test_cross_process_byte_conflict(app, client, sample_files, tmp_path, round_idx):
    # 复用 app 夹具的数据目录（环境变量已由夹具设置）
    data_dir = os.environ["DICOM_DATA_DIR"]
    uid_prefix = os.environ["DICOM_UID_PREFIX"]
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker,
                    args=(data_dir, uid_prefix, False, str(sample_files), queue)),
        ctx.Process(target=_worker,
                    args=(data_dir, uid_prefix, True, str(sample_files), queue)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    results = [queue.get() for _ in procs]
    statuses = {("evil" if evil else "good"): status for evil, status, _ in results}

    # 不变量 1：同 SOP 字节冲突时，至少冲突一方必须失败（绝不允许两个不同字节
    # 的批次都 ready 并产出可下载结果）
    assert not (statuses["evil"] == "ready" and statuses["good"] == "ready"), (
        f"字节冲突的两个批次不应同时成功: {statuses}"
    )

    # 不变量 2：任意时刻一个 SOP 只对应一个入库哈希，且所有 ready 批次可下载
    from app.db import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT sop_uid, COUNT(DISTINCT sha256) distinct_hashes, "
            "MIN(sha256) sha FROM instances GROUP BY sop_uid HAVING distinct_hashes > 1"
        ).fetchall()
        assert rows == [], f"同一 SOP 出现多个入库字节版本: {[dict(r) for r in rows]}"

        ready = conn.execute("SELECT id FROM batches WHERE status='ready'").fetchall()
        for row in ready:
            resp = client.get(f"/batches/{row['id']}/download")
            assert resp.status_code == 200
        failed = conn.execute(
            "SELECT id FROM batches WHERE status='failed'"
        ).fetchall()
        for row in failed:
            assert client.get(f"/batches/{row['id']}/download").status_code == 409

        # 入库实例引用的对象存储文件必须真实存在
        from app import config
        for inst in conn.execute("SELECT object_path FROM instances").fetchall():
            assert (config.get_objects_dir() / inst["object_path"]).exists()
    finally:
        conn.close()


def test_cross_process_identical_batches(app, sample_files):
    data_dir = os.environ["DICOM_DATA_DIR"]
    uid_prefix = os.environ["DICOM_UID_PREFIX"]
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker,
                    args=(data_dir, uid_prefix, False, str(sample_files), queue))
        for _ in range(3)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    results = [queue.get() for _ in procs]
    assert all(status == "ready" for _, status, _ in results)

    from app.db import get_db
    conn = get_db()
    try:
        # 同一原标识在三个进程下仍只有一套映射
        dup = conn.execute(
            "SELECT original_uid, COUNT(*) c FROM uid_mappings "
            "GROUP BY original_uid HAVING c > 1"
        ).fetchall()
        assert dup == []
        assert conn.execute("SELECT COUNT(*) c FROM instances").fetchone()["c"] == 4
    finally:
        conn.close()
