"""端到端与不变量测试。"""
from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

import pydicom
from werkzeug.datastructures import MultiDict

from samples import generate_samples as gen


def _upload_batch(client, paths):
    data = MultiDict()
    handles = []
    for p in paths:
        fh = open(p, "rb")
        handles.append(fh)
        data.add("dicom_files", (fh, Path(p).name, "application/dicom"))
    resp = client.post("/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=False)
    for fh in handles:
        fh.close()
    return resp


def _batch_id_from_redirect(resp) -> str:
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def _download_zip(client, batch_id: str) -> zipfile.ZipFile:
    resp = client.get(f"/batches/{batch_id}/download")
    assert resp.status_code == 200
    return zipfile.ZipFile(io.BytesIO(resp.data))


def _read_member(zf: zipfile.ZipFile, name: str) -> pydicom.Dataset:
    return pydicom.dcmread(io.BytesIO(zf.read(name)))


# ---------------------------------------------------------------- 基本流程
def test_full_batch_deidentification(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    resp = _upload_batch(client, paths)
    batch_id = _batch_id_from_redirect(resp)

    zf = _download_zip(client, batch_id)
    names = zf.namelist()
    assert "manifest.csv" in names
    assert len([n for n in names if n.endswith(".dcm")]) == 4

    out_ct1 = _read_member(zf, next(n for n in names if n.endswith(".dcm")
                                    and gen.SOP_CT_1 not in n))
    # 找到对应原 SOP_CT_1 的输出：通过 manifest 反查更稳妥
    manifest = zf.read("manifest.csv").decode("utf-8-sig")
    assert gen.SOP_CT_1 in manifest and gen.PATIENT_ID in manifest
    ct1_new_name = [
        line.split(",")[8] + ".dcm"
        for line in manifest.splitlines()[1:]
        if gen.SOP_CT_1 in line
    ][0]
    out_ct1 = _read_member(zf, ct1_new_name)

    # PatientName 清空，PatientID 替换
    assert str(out_ct1.PatientName) == ""
    assert str(out_ct1.PatientID) != gen.PATIENT_ID
    assert str(out_ct1.PatientID).startswith("P") and len(str(out_ct1.PatientID)) == 13

    # 三类 UID 全部换号
    assert str(out_ct1.StudyInstanceUID) != gen.STUDY_UID
    assert str(out_ct1.SeriesInstanceUID) != gen.SERIES_CT_UID
    assert str(out_ct1.SOPInstanceUID) != gen.SOP_CT_1

    # 文件元信息同步：Media Storage SOP Instance UID == 新 SOP UID
    assert str(out_ct1.file_meta.MediaStorageSOPInstanceUID) == str(out_ct1.SOPInstanceUID)

    # 类 UID、传输语法保持原样
    assert str(out_ct1.file_meta.MediaStorageSOPClassUID) == str(
        pydicom.uid.CTImageStorage)
    assert str(out_ct1.SOPClassUID) == str(pydicom.uid.CTImageStorage)
    assert str(out_ct1.file_meta.TransferSyntaxUID) == str(
        pydicom.uid.ExplicitVRLittleEndian)

    # 像素数据逐字节不变
    orig_ct1 = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    assert out_ct1.PixelData == orig_ct1.PixelData

    # FrameOfReferenceUID 不换号
    assert str(out_ct1.FrameOfReferenceUID) == gen.FRAME_OF_REF_UID


# ---------------------------------------------------------------- 嵌套/跨文件引用
def test_cross_file_references_remapped_consistently(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    resp = _upload_batch(client, paths)
    batch_id = _batch_id_from_redirect(resp)
    zf = _download_zip(client, batch_id)
    manifest = zf.read("manifest.csv").decode("utf-8-sig").splitlines()

    mapping = {}
    for line in manifest[1:]:
        cols = line.split(",")
        mapping[cols[7]] = cols[8]  # 原 SOP -> 新 SOP
    new_ct1 = mapping[gen.SOP_CT_1]
    new_ct2 = mapping[gen.SOP_CT_2]

    # 推导出新 study/series
    ct_out = _read_member(zf, new_ct1 + ".dcm")
    new_study = str(ct_out.StudyInstanceUID)
    new_series = str(ct_out.SeriesInstanceUID)

    sr_out = _read_member(zf, mapping[gen.SOP_SR] + ".dcm")

    # ReferencedStudySequence 中承载 Study UID 的字段按 study 角色换号
    ref_study_item = sr_out.ReferencedStudySequence[0]
    assert str(ref_study_item.ReferencedSOPInstanceUID) == new_study
    # 其类 UID 不换号
    assert str(ref_study_item.ReferencedSOPClassUID) == "1.2.840.10008.3.1.2.3.1"

    # ReferencedSeriesSequence -> 嵌套 CT SOP 引用
    ref_series = sr_out.ReferencedSeriesSequence[0]
    assert str(ref_series.SeriesInstanceUID) == new_series
    assert str(ref_series.ReferencedImageSequence[0].ReferencedSOPInstanceUID) == new_ct1
    assert str(ref_series.ReferencedImageSequence[0].ReferencedSOPClassUID) == str(
        pydicom.uid.CTImageStorage)

    # Evidence 序列三级引用
    ev_study = sr_out.CurrentRequestedProcedureEvidenceSequence[0]
    assert str(ev_study.StudyInstanceUID) == new_study
    ev_series = ev_study.ReferencedSeriesSequence[0]
    assert str(ev_series.SeriesInstanceUID) == new_series
    ev_sops = {str(x.ReferencedSOPInstanceUID) for x in ev_series.ReferencedSOPSequence}
    assert ev_sops == {new_ct1, new_ct2}

    # RT Structure Set 的跨文件引用
    rt_out = _read_member(zf, mapping[gen.SOP_RT] + ".dcm")
    rt_ref_study = rt_out.ReferencedFrameOfReferenceSequence[0] \
        .RTReferencedStudySequence[0]
    assert str(rt_ref_study.StudyInstanceUID) == new_study
    rt_series = rt_ref_study.RTReferencedSeriesSequence[0]
    assert str(rt_series.SeriesInstanceUID) == new_series
    rt_sops = {str(x.ReferencedSOPInstanceUID)
               for x in rt_series.ContourImageSequence}
    assert rt_sops == {new_ct1, new_ct2}
    # FrameOfReferenceUID 不动
    assert str(rt_out.ReferencedFrameOfReferenceSequence[0]
               .FrameOfReferenceUID) == gen.FRAME_OF_REF_UID


# ---------------------------------------------------------------- 跨批次一致
def test_mapping_stable_across_batches(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    r1 = _batch_id_from_redirect(_upload_batch(client, paths))
    z1 = _download_zip(client, r1)
    m1 = {line.split(",")[7]: line.split(",")[8]
          for line in z1.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}

    # 只上传一张 CT（引用目标先于/后于其他文件的场景），再上传 SR
    r2 = _batch_id_from_redirect(
        _upload_batch(client, [sample_files / "sr_001.dcm"]))
    z2 = _download_zip(client, r2)
    m2 = {line.split(",")[7]: line.split(",")[8]
          for line in z2.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}

    # 同一原 SOP 跨批次映射完全相同
    assert m2[gen.SOP_SR] == m1[gen.SOP_SR]

    # SR 里引用的 CT（本批未上传）也与第一批结果一致
    sr_out = _read_member(z2, m2[gen.SOP_SR] + ".dcm")
    ref_sop = str(sr_out.ReferencedSeriesSequence[0]
                  .ReferencedImageSequence[0].ReferencedSOPInstanceUID)
    assert ref_sop == m1[gen.SOP_CT_1]
    assert str(sr_out.ReferencedStudySequence[0].ReferencedSOPInstanceUID) == \
        str(_read_member(z1, m1[gen.SOP_CT_1] + ".dcm").StudyInstanceUID)

    # 再传一次完整批次，所有映射保持不变（重试不变号）
    r3 = _batch_id_from_redirect(_upload_batch(client, paths))
    z3 = _download_zip(client, r3)
    m3 = {line.split(",")[7]: line.split(",")[8]
          for line in z3.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}
    assert m3 == m1


# ---------------------------------------------------------------- 字节冲突整批拒绝
def test_same_uid_different_bytes_rejects_whole_batch(client, sample_files, tmp_path):
    paths = sorted(sample_files.glob("*.dcm"))
    r1 = _batch_id_from_redirect(_upload_batch(client, paths))
    assert client.get(f"/batches/{r1}/download").status_code == 200

    # 复制一份 CT1 并改动像素（保持 UID 不变）
    evil_dir = tmp_path / "evil"
    evil_dir.mkdir()
    ds = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    ds.PixelData = bytes([0xAA]) * len(ds.PixelData)
    evil_path = evil_dir / "ct_001_modified.dcm"
    ds.save_as(str(evil_path), write_like_original=False)

    resp = _upload_batch(client, [evil_path, sample_files / "ct_002.dcm"])
    batch_id = _batch_id_from_redirect(resp)
    detail = client.get(f"/batches/{batch_id}")
    assert b"failed" in detail.data
    assert "字节不同".encode() in detail.data
    # 失败批次绝不提供下载
    assert client.get(f"/batches/{batch_id}/download").status_code == 409


# ---------------------------------------------------------------- 同字节复用
def test_identical_bytes_reuses_result(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    r1 = _batch_id_from_redirect(_upload_batch(client, paths))
    z1 = _download_zip(client, r1)
    m1 = {line.split(",")[7]: line.split(",")[8]
          for line in z1.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}

    r2 = _batch_id_from_redirect(
        _upload_batch(client, [sample_files / "ct_001.dcm"]))
    z2 = _download_zip(client, r2)
    m2 = {line.split(",")[7]: line.split(",")[8]
          for line in z2.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}
    assert m2[gen.SOP_CT_1] == m1[gen.SOP_CT_1]
    # 产物内容完全一致（复用同一对象存储文件）
    assert z2.read(m2[gen.SOP_CT_1] + ".dcm") == z1.read(m1[gen.SOP_CT_1] + ".dcm")


# ---------------------------------------------------------------- 压缩文件拒绝
def test_compressed_transfer_syntax_rejected(client, sample_files, tmp_path):
    # SR 无像素数据，仅改传输语法 UID 即可构造“压缩 TS”样本
    ds = pydicom.dcmread(str(sample_files / "sr_001.dcm"))
    ds.file_meta.TransferSyntaxUID = pydicom.uid.JPEGBaseline8Bit
    bad = tmp_path / "compressed.dcm"
    ds.save_as(str(bad), write_like_original=False)
    resp = _upload_batch(client, [bad])
    batch_id = _batch_id_from_redirect(resp)
    detail = client.get(f"/batches/{batch_id}")
    assert b"failed" in detail.data
    assert "压缩传输语法".encode() in detail.data
    assert client.get(f"/batches/{batch_id}/download").status_code == 409


# ---------------------------------------------------------------- 并发上传
def test_concurrent_uploads_consistent(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    results: list[tuple[int, str]] = []
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            resp = _upload_batch(client, paths)
            results.append((idx, _batch_id_from_redirect(resp)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors

    maps = []
    for _, batch_id in results:
        zf = _download_zip(client, batch_id)
        assert zf.testzip() is None
        m = {line.split(",")[7]: line.split(",")[8]
             for line in zf.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}
        maps.append(m)
    # 四个并发批次对同一原标识给出完全相同的新标识
    assert all(m == maps[0] for m in maps[1:])


# ---------------------------------------------------------------- 中断恢复
def test_interrupted_batch_not_downloadable(app, client, sample_files, monkeypatch):
    from app import services
    from app.db import get_db, recover_interrupted_batches

    boom = RuntimeError("模拟断电")

    def broken_zip(batch_id, plan):  # noqa: ANN001
        raise boom

    monkeypatch.setattr(services, "_build_zip", broken_zip, raising=True)
    paths = sorted(sample_files.glob("*.dcm"))
    resp = _upload_batch(client, paths)
    batch_id = _batch_id_from_redirect(resp)
    assert client.get(f"/batches/{batch_id}/download").status_code == 409

    # 模拟服务重启：重新执行恢复逻辑
    recover_interrupted_batches()
    batch = services.get_batch(batch_id)
    assert batch["status"] == "failed"
    assert client.get(f"/batches/{batch_id}/download").status_code == 409

    # 已入库映射不丢失；重试后标识不变且可成功下载
    monkeypatch.undo()
    r2 = _batch_id_from_redirect(_upload_batch(client, paths))
    zf = _download_zip(client, r2)
    assert zf.testzip() is None
    db = get_db()
    try:
        # 映射表只有一套（INSERT OR IGNORE + 确定性派生）
        count = db.execute(
            "SELECT COUNT(*) c FROM uid_mappings WHERE original_uid=?",
            (gen.SOP_CT_1,),
        ).fetchone()["c"]
        assert count == 1
    finally:
        db.close()


# ---------------------------------------------------------------- 空批次/非 DICOM
def test_empty_upload_rejected(client):
    resp = client.post("/upload", data={}, content_type="multipart/form-data",
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "至少选择一个".encode() in resp.data


def test_non_dicom_rejected(client, tmp_path):
    junk = tmp_path / "junk.dcm"
    junk.write_bytes(b"not a dicom file at all" * 10)
    resp = _upload_batch(client, [junk])
    batch_id = _batch_id_from_redirect(resp)
    assert client.get(f"/batches/{batch_id}/download").status_code == 409


# ---------------------------------------------------------------- 患者 ID 一致
def test_patient_id_mapping_stable(client, sample_files):
    paths = sorted(sample_files.glob("*.dcm"))
    r1 = _batch_id_from_redirect(_upload_batch(client, paths))
    z1 = _download_zip(client, r1)
    pids = {line.split(",")[2]
            for line in z1.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}
    assert pids == {pids and list(pids)[0]}  # 同患者只有一个新 ID

    r2 = _batch_id_from_redirect(
        _upload_batch(client, [sample_files / "ct_001.dcm"]))
    z2 = _download_zip(client, r2)
    pids2 = {line.split(",")[2]
             for line in z2.read("manifest.csv").decode("utf-8-sig").splitlines()[1:]}
    assert pids2 == pids
