"""pytest 夹具：每个用例独立的数据目录与 Flask 测试客户端。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

# 保证可以 import 项目根的 app 包与 samples 包
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app(tmp_path):
    # 用独立的 MonkeyPatch 实例管理环境变量：测试内使用 monkeypatch.undo()
    # 撤销函数补丁时，不会连带把 DICOM_DATA_DIR 等环境变量一起恢复
    env_patch = MonkeyPatch()
    env_patch.setenv("DICOM_DATA_DIR", str(tmp_path / "data"))
    env_patch.setenv("DICOM_UID_PREFIX", "1.2.826.0.9999")
    from app.web import create_app
    application = create_app()
    application.config.update(TESTING=True)
    yield application
    env_patch.undo()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sample_files():
    """返回合成样例目录，不存在则现场生成。"""
    data_dir = ROOT / "samples" / "data"
    if not data_dir.exists():
        from samples.generate_samples import main
        main(data_dir)
    return data_dir
