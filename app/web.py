"""Flask 应用工厂与路由。"""
from __future__ import annotations

import os

from flask import (
    Flask,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import archive, config, services
from .db import get_db, init_db, recover_interrupted_batches


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")

    init_db()
    recover_interrupted_batches()
    archive.recover_interrupted_archives()
    archive.reclaim_orphaned_archives()

    @app.get("/")
    def index():
        return render_template("index.html", batches=services.list_batches())

    @app.post("/upload")
    def upload():
        files = request.files.getlist("dicom_files")
        if not files or all(not f.filename for f in files):
            flash("请至少选择一个 DICOM 文件", "error")
            return redirect(url_for("index"))
        try:
            batch_id, saved = services.save_uploads(files)
        except Exception as exc:  # noqa: BLE001
            flash(f"上传暂存失败: {exc}", "error")
            return redirect(url_for("index"))
        try:
            services.run_batch(batch_id, saved)
        except services.BatchError as exc:
            flash(str(exc), "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("批次 %s 处理异常", batch_id)
            flash(f"处理失败: {exc}", "error")
            return redirect(url_for("batch_detail", batch_id=batch_id))
        flash(f"批次 {batch_id} 处理完成", "ok")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.get("/batches/<batch_id>")
    def batch_detail(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        conn = get_db()
        try:
            files = conn.execute(
                "SELECT * FROM batch_files WHERE batch_id=? ORDER BY orig_name",
                (batch_id,),
            ).fetchall()
        finally:
            conn.close()
        job = services.get_archive_job(batch_id)
        locked = archive.is_batch_locked(batch_id)
        return render_template("batch.html", batch=batch, files=files,
                               archive=job, archive_locked=locked)

    @app.post("/batches/<batch_id>/archive")
    def archive_batch(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        try:
            services.archive_batch(batch_id)
        except services.BatchError as exc:
            flash(str(exc), "error")
        except (services.ArchiveError, archive.ArchiveBusy) as exc:
            flash(f"整理失败，原包已保留，可重试：{exc}", "error")
        except Exception as exc:  # noqa: BLE001
            app.logger.exception("批次 %s 整理异常", batch_id)
            flash(f"整理失败，原包已保留，可重试：{exc}", "error")
        else:
            flash("整理完成：核对通过，重建依据与原 manifest.csv 已保存", "ok")
        return redirect(url_for("batch_detail", batch_id=batch_id))

    @app.get("/batches/<batch_id>/download")
    def download(batch_id: str):
        batch = services.get_batch(batch_id)
        if batch is None:
            abort(404)
        if batch["status"] != "ready":
            # 半成品绝不提供下载
            abort(409, description="批次尚未处理成功，无可下载文件包")
        try:
            path, name, temporary = services.open_download(batch_id)
        except (services.ArchiveError, services.BatchError) as exc:
            # 对象缺失/内容变化/重建失败：明确报错，不返回半包
            abort(409, description=f"无法重建文件包：{exc}")

        if not temporary:
            return send_file(
                path,
                mimetype="application/zip",
                as_attachment=True,
                download_name=name,
                conditional=True,
            )

        # 重建出的临时包采用流式响应：生成器结束或被关闭时删除临时文件。
        # 注意 direct_passthrough 响应不会触发 call_on_close，故显式包装。
        def stream_and_cleanup():
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
            finally:
                path.unlink(missing_ok=True)

        response = make_response(stream_and_cleanup())
        response.mimetype = "application/zip"
        response.headers["Content-Length"] = str(path.stat().st_size)
        # 与 send_file 相同的 Content-Disposition 生成方式（含非 ASCII 文件名处理）
        response.headers.set("Content-Disposition", "attachment", filename=name)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.errorhandler(413)
    def too_large(_err):
        flash("上传内容超过服务端大小限制", "error")
        return redirect(url_for("index"))

    return app
