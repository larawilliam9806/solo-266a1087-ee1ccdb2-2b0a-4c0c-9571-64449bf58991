"""转换函数的单元级测试：多值 UI、未知 UID 保留、嵌套 PN/PatientID。"""
from __future__ import annotations

import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.tag import Tag

from app import dicom_proc
from app.dicom_proc import (
    BatchPlan,
    TAG_PATIENT_ID,
    TAG_PATIENT_NAME,
)


def _make_plan() -> BatchPlan:
    plan = BatchPlan()
    plan.value_roles = {
        "1.1.1": "study",
        "2.2.2": "series",
        "3.3.3": "sop",
        "9.9.9": "sop",   # 标签提示兜底进入的引用
    }
    plan.uid_remap = {
        "1.1.1": "5.5.1",
        "2.2.2": "5.5.2",
        "3.3.3": "5.5.3",
        "9.9.9": "5.5.9",
    }
    plan.patient_remap = {"PID-OLD": "P000000000001"}
    return plan


def test_multivalue_ui_and_unknown_uid_preserved():
    ds = Dataset()
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    # 多值 UI：一个在映射表，一个未知（应原样保留）
    ds.add_new(Tag(0x0008, 0x1155), "UI", ["3.3.3", "7.7.7"])
    # 类 UID（排除标签）即使值撞了身份 UID 也不动
    ds.add_new(Tag(0x0008, 0x0016), "UI", "1.1.1")
    # FrameOfReferenceUID 不动
    ds.add_new(Tag(0x0020, 0x0052), "UI", "8.8.8")
    # 普通身份
    ds.add_new(Tag(0x0020, 0x000D), "UI", "1.1.1")
    ds.add_new(Tag(0x0020, 0x000E), "UI", "2.2.2")
    ds.add_new(Tag(0x0008, 0x0018), "UI", "3.3.3")
    # 患者字段
    ds.add_new(TAG_PATIENT_NAME, "PN", "DOE^JOHN")
    ds.add_new(TAG_PATIENT_ID, "LO", "PID-OLD")

    # 嵌套序列里的 PN 与 UI
    seq_item = Dataset()
    seq_item.add_new(TAG_PATIENT_NAME, "PN", "NURSE^ANN")
    seq_item.add_new(Tag(0x0008, 0x1155), "UI", "9.9.9")
    seq = pydicom.Sequence([seq_item])
    ds.add_new(Tag(0x0040, 0x0275), "SQ", seq)

    # 文件元信息
    meta = pydicom.dataset.FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = "3.3.3"
    meta.TransferSyntaxUID = "1.2.840.10008.1.2.1"
    ds.file_meta = meta

    dicom_proc._transform_dataset(ds, _make_plan())

    # 多值 UI：已知换号、未知保留
    assert list(ds[Tag(0x0008, 0x1155)].value) == ["5.5.3", "7.7.7"]
    # 类 UID 不换号（即使值等于 study 身份 UID）
    assert str(ds[Tag(0x0008, 0x0016)].value) == "1.1.1"
    # FrameOfReference 不动
    assert str(ds[Tag(0x0020, 0x0052)].value) == "8.8.8"
    # 三级身份换号
    assert str(ds[Tag(0x0020, 0x000D)].value) == "5.5.1"
    assert str(ds[Tag(0x0020, 0x000E)].value) == "5.5.2"
    assert str(ds[Tag(0x0008, 0x0018)].value) == "5.5.3"
    # 患者
    assert str(ds[TAG_PATIENT_NAME].value) == ""
    assert str(ds[TAG_PATIENT_ID].value) == "P000000000001"
    # 嵌套
    nested = ds[Tag(0x0040, 0x0275)].value[0]
    assert str(nested[TAG_PATIENT_NAME].value) == ""
    assert str(nested[Tag(0x0008, 0x1155)].value) == "5.5.9"
    # 文件元信息同步，类与传输语法不动
    assert str(ds.file_meta.MediaStorageSOPInstanceUID) == "5.5.3"
    assert str(ds.file_meta.MediaStorageSOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"
    assert str(ds.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1"


def test_unregistered_nested_patient_id_is_remapped():
    """计划外的嵌套 PatientID 也必须派生替换，不得原样泄露。"""
    ds = Dataset()
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.add_new(TAG_PATIENT_NAME, "PN", "OUTER^NAME")
    ds.add_new(TAG_PATIENT_ID, "LO", "PID-KNOWN")

    item = Dataset()
    item.add_new(TAG_PATIENT_NAME, "PN", "INNER^NAME")
    item.add_new(TAG_PATIENT_ID, "LO", "PID-SECRET-NESTED")  # 未登记进 plan
    ds.add_new(Tag(0x0040, 0x0275), "SQ", pydicom.Sequence([item]))

    plan = _make_plan()  # 只含 PID-OLD，不含 PID-KNOWN / PID-SECRET-NESTED
    dicom_proc._transform_dataset(ds, plan)

    assert str(ds[TAG_PATIENT_NAME].value) == ""
    nested = ds[Tag(0x0040, 0x0275)].value[0]
    assert str(nested[TAG_PATIENT_NAME].value) == ""
    # 嵌套的秘密 ID 被替换，且原值不再出现
    nested_new = str(nested[TAG_PATIENT_ID].value)
    assert nested_new.startswith("P") and nested_new != "PID-SECRET-NESTED"
    # 确定性：同样的秘密 ID 派生结果稳定
    from app.identifiers import derive_patient_id
    assert nested_new == derive_patient_id("PID-SECRET-NESTED")


def test_role_conflict_rejected_at_plan(app, sample_files, tmp_path):
    """同一 UID 在批次内被声明为两种角色 -> 整批拒绝。"""
    from app import dicom_proc as dp
    from app.db import init_db

    init_db()
    # 复制一份 CT，把它的 SeriesInstanceUID 改成样例的 Study UID，
    # 于是该 UID 同时承担 study（另一文件声明）与 series（本文件声明）两种角色
    ds = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    from samples import generate_samples as gen
    ds.SeriesInstanceUID = gen.STUDY_UID
    evil = tmp_path / "conflict.dcm"
    ds.save_as(str(evil), write_like_original=False)

    good_ct2 = sample_files / "ct_002.dcm"
    with pytest.raises(dp.BatchRejected):
        dp.plan_batch([("conflict.dcm", evil), ("ct_002.dcm", good_ct2)])
