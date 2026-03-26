from __future__ import annotations

import logging
from pathlib import Path
from threading import Thread
from time import perf_counter
from uuid import uuid4

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from config import settings
from job_store import JobStore
from llm_client import LLMClient
from rewriter import RewriteResult, RewriteStoppedError, ScriptRewriter
from script_parser import decode_text_file, parse_script


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


configure_logging()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.logger.setLevel(logging.INFO)

rewriter = ScriptRewriter(LLMClient(settings))
jobs = JobStore()
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


@app.before_request
def log_request_start() -> None:
    g.request_id = uuid4().hex[:8]
    g.request_started_at = perf_counter()
    app.logger.info(
        "[%s] 收到请求 %s %s content_type=%s content_length=%s",
        g.request_id,
        request.method,
        request.path,
        request.content_type,
        request.content_length,
    )


@app.after_request
def log_request_end(response):
    elapsed_ms = int((perf_counter() - getattr(g, "request_started_at", perf_counter())) * 1000)
    request_id = getattr(g, "request_id", "-")
    app.logger.info(
        "[%s] 响应完成 status=%s elapsed_ms=%s",
        request_id,
        response.status_code,
        elapsed_ms,
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(error: RequestEntityTooLarge):
    app.logger.warning("[%s] 上传文件过大", getattr(g, "request_id", "-"))
    return jsonify({"ok": False, "error": "上传文件超过 10MB 限制。", "request_id": getattr(g, "request_id", "-")}), 413


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": error.description,
                        "request_id": getattr(g, "request_id", "-"),
                    }
                ),
                error.code,
            )
        return error
    if request.path.startswith("/api/"):
        app.logger.exception("[%s] API 未捕获异常", getattr(g, "request_id", "-"))
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"服务器内部错误：{error}",
                    "request_id": getattr(g, "request_id", "-"),
                }
            ),
            500,
        )
    app.logger.exception("[%s] 页面请求未捕获异常", getattr(g, "request_id", "-"))
    return "服务器内部错误，请查看终端日志。", 500


@app.get("/")
def index():
    return render_template(
        "index.html",
        default_provider=settings.default_provider,
        providers=settings.provider_options(),
    )


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "message": "API is ready"})


@app.get("/api/jobs/<job_id>")
def get_job_status(job_id: str):
    state = jobs.get_job(job_id)
    if not state:
        return jsonify({"ok": False, "error": "任务不存在。", "request_id": g.request_id}), 404
    payload = state.to_dict()
    payload["ok"] = True
    return jsonify(payload)


@app.post("/api/process")
def process_script():
    app.logger.info("[%s] 进入 /api/process 处理函数", g.request_id)
    app.logger.info("[%s] form_keys=%s file_keys=%s", g.request_id, list(request.form.keys()), list(request.files.keys()))
    upload = request.files.get("script_file")
    provider_name = request.form.get("provider") or settings.default_provider
    if not upload or not upload.filename:
        app.logger.warning("[%s] 缺少 script_file", g.request_id)
        return jsonify({"ok": False, "error": "请先选择一个剧本 txt 文件。", "request_id": g.request_id}), 400
    if not upload.filename.lower().endswith(".txt"):
        app.logger.warning("[%s] 文件类型不支持 filename=%s", g.request_id, upload.filename)
        return jsonify({"ok": False, "error": "目前只支持上传 .txt 文件。", "request_id": g.request_id}), 400

    raw_file = upload.read()
    app.logger.info(
        "[%s] 开始解析文件 filename=%s size_bytes=%s provider=%s",
        g.request_id,
        upload.filename,
        len(raw_file),
        provider_name,
    )
    try:
        script_text = decode_text_file(raw_file)
        parsed = parse_script(script_text)
        initial_content = rebuild_original_script(parsed)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("[%s] 预解析剧本失败", g.request_id)
        return jsonify({"ok": False, "error": str(exc), "request_id": g.request_id}), 400

    job_id = uuid4().hex
    initial_output_path = persist_output(job_id, parsed.download_name, initial_content)
    jobs.create_job(
        job_id=job_id,
        status="queued",
        title=parsed.title,
        provider=provider_name,
        episode_count=len(parsed.episodes),
        completed_count=0,
        passed_audit_count=0,
        fallback_count=0,
        download_name=parsed.download_name,
        content=initial_content,
        audits=[],
        partial_output_path=initial_output_path,
    )

    worker = Thread(
        target=run_rewrite_job,
        args=(job_id, script_text, provider_name),
        daemon=True,
    )
    worker.start()
    app.logger.info("[%s] 已启动后台任务 job_id=%s", g.request_id, job_id)

    return (
        jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "request_id": g.request_id,
                "status": "queued",
                "title": parsed.title,
                "provider": provider_name,
                "episode_count": len(parsed.episodes),
                "download_name": parsed.download_name,
                "content": initial_content,
                "partial_output_path": initial_output_path,
            }
        ),
        202,
    )


def run_rewrite_job(job_id: str, script_text: str, provider_name: str) -> None:
    jobs.update_job(job_id, status="running")
    app.logger.info("[job:%s] 后台任务开始", job_id)

    def on_progress(snapshot: RewriteResult) -> None:
        passed_count, fallback_count = summarize_audits(snapshot)
        output_path = persist_output(job_id, snapshot.download_name, snapshot.content)
        jobs.update_job(
            job_id,
            status="running",
            title=snapshot.title,
            provider=snapshot.provider,
            episode_count=snapshot.episode_count,
            completed_count=snapshot.completed_count,
            passed_audit_count=passed_count,
            fallback_count=fallback_count,
            download_name=snapshot.download_name,
            content=snapshot.content,
            audits=[audit.to_dict() for audit in snapshot.audits],
            partial_output_path=output_path,
        )
        app.logger.info(
            "[job:%s] 已完成 %s/%s 集",
            job_id,
            snapshot.completed_count,
            snapshot.episode_count,
        )

    try:
        result = rewriter.rewrite_script_progressive(
            script_text,
            provider_name=provider_name,
            progress_callback=on_progress,
        )
        passed_count, fallback_count = summarize_audits(result)
        output_path = persist_output(job_id, result.download_name, result.content)
        jobs.update_job(
            job_id,
            status="completed",
            title=result.title,
            provider=result.provider,
            episode_count=result.episode_count,
            completed_count=result.completed_count,
            passed_audit_count=passed_count,
            fallback_count=fallback_count,
            download_name=result.download_name,
            content=result.content,
            audits=[audit.to_dict() for audit in result.audits],
            partial_output_path=output_path,
            error="",
        )
        app.logger.info("[job:%s] 后台任务完成", job_id)
    except RewriteStoppedError as exc:
        partial = exc.partial_result
        passed_count, fallback_count = summarize_audits(partial)
        output_path = persist_output(job_id, partial.download_name, partial.content)
        jobs.update_job(
            job_id,
            status="failed",
            title=partial.title,
            provider=partial.provider,
            episode_count=partial.episode_count,
            completed_count=partial.completed_count,
            passed_audit_count=passed_count,
            fallback_count=fallback_count,
            download_name=partial.download_name,
            content=partial.content,
            audits=[audit.to_dict() for audit in partial.audits],
            partial_output_path=output_path,
            error=str(exc),
        )
        app.logger.warning("[job:%s] 后台任务失败，但已保留前序成果: %s", job_id, exc)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("[job:%s] 后台任务异常", job_id)
        jobs.update_job(job_id, status="failed", error=str(exc))


def rebuild_original_script(parsed) -> str:
    episodes_text = "".join(f"{episode.heading}{episode.content}" for episode in parsed.episodes)
    return f"{parsed.script_prefix}{parsed.body_prefix}{episodes_text}"


def summarize_audits(result: RewriteResult) -> tuple[int, int]:
    passed_count = sum(1 for item in result.audits if item.verdict == "pass" and not item.fallback_used)
    fallback_count = sum(1 for item in result.audits if item.fallback_used)
    return passed_count, fallback_count


def persist_output(job_id: str, download_name: str, content: str) -> str:
    filename = download_name or f"{job_id}.txt"
    safe_path = OUTPUT_DIR / filename
    safe_path.write_text(content, encoding="utf-8")
    return str(safe_path)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
