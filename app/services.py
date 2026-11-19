"""批次编排：暂存 -> 计划 -> 映射入库 -> 对象落盘 -> 原子打包 -> 状态流转。"""
from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import uuid
import zipfile
from pathlib import Path

from . import config, dicom_proc
from .db import get_db, immediate_tx


class BatchError(Exception):
    pass


def save_uploads(files) -> tuple[str, list[tuple[str, Path]]]:
    """把上传流保存到独立暂存目录（每个批次一个新空目录）。"""
    batch_id = uuid.uuid4().hex
    staging = config.get_upload_tmp_dir() / batch_id
    staging.mkdir(parents=True, exist_ok=False)
    saved: list[tuple[str, Path]] = []
    try:
        for storage in files:
            if not storage or not storage.filename:
                continue
            # 仅取文件名部分，防止路径穿越
            safe_name = Path(storage.filename).name
            target = staging / f"{len(saved):05d}_{safe_name}"
            storage.save(str(target))
            saved.append((storage.filename, target))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return batch_id, saved


def _cleanup_staging(batch_id: str) -> None:
    shutil.rmtree(config.get_upload_tmp_dir() / batch_id, ignore_errors=True)


def _mark_failed(batch_id: str, error: str) -> None:
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "UPDATE batches SET status='failed', error=?, "
                "finished_at=datetime('now') WHERE id=?",
                (error, batch_id),
            )
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _build_manifest(batch_id: str) -> bytes:
    """生成 CSV 映射清单，随 zip 交付供数据湖关联。"""
    conn = get_db()
    try:
        files = conn.execute(
            "SELECT * FROM batch_files WHERE batch_id=? ORDER BY orig_name",
            (batch_id,),
        ).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "original_filename", "original_patient_id", "new_patient_id",
            "original_study_uid", "new_study_uid",
            "original_series_uid", "new_series_uid",
            "original_sop_uid", "new_sop_uid", "sha256",
        ])
        for f in files:
            inst = conn.execute(
                "SELECT new_sop_uid, sha256 FROM instances WHERE sop_uid=?",
                (f["sop_uid"],),
            ).fetchone()
            study_new = conn.execute(
                "SELECT new_uid FROM uid_mappings WHERE role='study' AND original_uid=?",
                (f["original_study_uid"],),
            ).fetchone()
            series_new = conn.execute(
                "SELECT new_uid FROM uid_mappings WHERE role='series' AND original_uid=?",
                (f["original_series_uid"],),
            ).fetchone()
            pid_new = conn.execute(
                "SELECT new_patient_id FROM patient_mappings WHERE original_patient_id=?",
                (f["original_patient_id"],),
            ).fetchone()
            writer.writerow([
                f["orig_name"],
                f["original_patient_id"],
                pid_new["new_patient_id"] if pid_new else "",
                f["original_study_uid"],
                study_new["new_uid"] if study_new else "",
                f["original_series_uid"],
                series_new["new_uid"] if series_new else "",
                f["sop_uid"],
                inst["new_sop_uid"],
                inst["sha256"],
            ])
        # UTF-8 BOM，方便 Excel 直接打开中文
        return buf.getvalue().encode("utf-8-sig")
    finally:
        conn.close()


def _build_zip(batch_id: str, plan) -> Path:
    """把对象存储中的处理结果与清单打成 zip，先写临时文件再原子 rename。"""
    batches_dir = config.get_batches_dir()
    final_zip = batches_dir / f"{batch_id}.zip"
    tmp_zip = batches_dir / f".{batch_id}.zip.tmp"
    conn = get_db()
    try:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for pf in plan.files:
                inst = conn.execute(
                    "SELECT object_path, new_sop_uid FROM instances WHERE sop_uid=?",
                    (pf.sop_uid,),
                ).fetchone()
                abs_path = config.get_objects_dir() / inst["object_path"]
                if not abs_path.exists():
                    raise BatchError(f"处理结果缺失: {abs_path}")
                zf.write(abs_path, arcname=f"{inst['new_sop_uid']}.dcm")
            zf.writestr("manifest.csv", _build_manifest(batch_id))
        with open(tmp_zip, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_zip, final_zip)
        return final_zip
    except Exception:
        Path(tmp_zip).unlink(missing_ok=True)
        raise
    finally:
        conn.close()


def run_batch(batch_id: str, saved: list[tuple[str, Path]]):
    """完整执行一个批次。任何异常 -> failed，绝不出现可下载的半批。"""
    # 先登记批次行，保证计划阶段（含整批拒绝）的失败也可在详情页追溯
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT INTO batches(id, status, original_count) "
                "VALUES(?, 'processing', ?)",
                (batch_id, len(saved)),
            )
    finally:
        conn.close()
    try:
        plan = dicom_proc.plan_batch(saved)
        dicom_proc.persist_mappings(plan)
        dicom_proc.process_plan(plan, batch_id)
        zip_path = _build_zip(batch_id, plan)
        conn = get_db()
        try:
            with immediate_tx(conn):
                conn.execute(
                    "UPDATE batches SET status='ready', zip_path=?, "
                    "finished_at=datetime('now') WHERE id=?",
                    (str(zip_path), batch_id),
                )
        finally:
            conn.close()
        return batch_id
    except dicom_proc.BatchRejected as exc:
        _mark_failed(batch_id, f"整批拒绝: {exc}")
        raise BatchError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _mark_failed(batch_id, f"处理失败: {exc}")
        raise
    finally:
        _cleanup_staging(batch_id)


def get_batch(batch_id: str) -> sqlite3.Row | None:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()


def list_batches(limit: int = 50) -> list[sqlite3.Row]:
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
