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
from rewriter import EpisodeAudit, RewriteResult, ScriptRewriter
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

ACTIVE_JOB_STATUSES = {"queued", "running", "pausing"}
TERMINAL_JOB_STATUSES = {"completed", "failed"}


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


@app.post("/api/jobs/<job_id>/pause")
def pause_job(job_id: str):
    try:
        state = jobs.mutate_job(job_id, _mark_pause_requested)
    except KeyError:
        return jsonify({"ok": False, "error": "任务不存在。", "request_id": g.request_id}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "request_id": g.request_id}), 409
    payload = state.to_dict()
    payload["ok"] = True
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/resume")
def resume_job(job_id: str):
    should_spawn_worker = False
    def apply_resume(current) -> None:
        nonlocal should_spawn_worker
        should_spawn_worker = _resume_job_state(current)

    try:
        state = jobs.mutate_job(job_id, apply_resume)
    except KeyError:
        return jsonify({"ok": False, "error": "任务不存在。", "request_id": g.request_id}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "request_id": g.request_id}), 409

    if should_spawn_worker:
        start_job_worker(job_id, state.run_revision)

    payload = state.to_dict()
    payload["ok"] = True
    return jsonify(payload)


@app.post("/api/jobs/<job_id>/restart")
def restart_job(job_id: str):
    try:
        state = jobs.mutate_job(job_id, _restart_job_state)
    except KeyError:
        return jsonify({"ok": False, "error": "任务不存在。", "request_id": g.request_id}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "request_id": g.request_id}), 409

    output_path = persist_output(job_id, state.download_name, state.initial_content or state.content)
    state = jobs.update_job(job_id, partial_output_path=output_path)
    start_job_worker(job_id, state.run_revision)

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
        script_text=script_text,
        initial_content=initial_content,
        rewritten_episodes=[],
        pause_requested=False,
        run_revision=1,
    )

    start_job_worker(job_id, run_revision=1)
    app.logger.info("[%s] 已启动后台任务 job_id=%s revision=%s", g.request_id, job_id, 1)

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


def start_job_worker(job_id: str, run_revision: int) -> None:
    worker = Thread(
        target=run_rewrite_job,
        args=(job_id, run_revision),
        daemon=True,
    )
    worker.start()


def run_rewrite_job(job_id: str, run_revision: int) -> None:
    state = jobs.get_job(job_id)
    if not state or state.run_revision != run_revision:
        app.logger.info("[job:%s] 放弃启动过期后台任务 revision=%s", job_id, run_revision)
        return

    jobs.update_job(
        job_id,
        status="pausing" if state.pause_requested else "running",
        error="",
    )
    app.logger.info("[job:%s] 后台任务开始 revision=%s", job_id, run_revision)

    def control_callback() -> str:
        current = jobs.get_job(job_id)
        if not current or current.run_revision != run_revision:
            return "abort"
        if current.pause_requested:
            return "pause"
        return "continue"

    def on_progress(snapshot: RewriteResult) -> None:
        current_state = jobs.get_job(job_id)
        if not current_state or current_state.run_revision != run_revision:
            return
        passed_count, fallback_count = summarize_audits(snapshot)
        output_path = persist_output(job_id, snapshot.download_name, snapshot.content)
        jobs.update_job(
            job_id,
            status="pausing" if current_state.pause_requested else "running",
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
            rewritten_episodes=list(snapshot.rewritten_episodes),
            error="",
        )
        app.logger.info(
            "[job:%s] 已完成 %s/%s 集 revision=%s",
            job_id,
            snapshot.completed_count,
            snapshot.episode_count,
            run_revision,
        )

    try:
        existing_audits = [EpisodeAudit.from_dict(item) for item in state.audits]
        outcome = rewriter.rewrite_script_progressive(
            state.script_text,
            provider_name=state.provider,
            progress_callback=on_progress,
            start_episode_index=state.completed_count,
            existing_rewritten_episodes=state.rewritten_episodes,
            existing_audits=existing_audits,
            control_callback=control_callback,
        )
        latest_state = jobs.get_job(job_id)
        if not latest_state or latest_state.run_revision != run_revision:
            app.logger.info("[job:%s] 过期后台任务已停止 revision=%s", job_id, run_revision)
            return

        result = outcome.result
        passed_count, fallback_count = summarize_audits(result)
        output_path = persist_output(job_id, result.download_name, result.content)
        final_status = "completed" if outcome.status == "completed" else "paused"
        jobs.update_job(
            job_id,
            status=final_status,
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
            rewritten_episodes=list(result.rewritten_episodes),
            pause_requested=False,
            error="",
        )
        app.logger.info("[job:%s] 后台任务结束 status=%s revision=%s", job_id, final_status, run_revision)
    except Exception as exc:  # noqa: BLE001
        latest_state = jobs.get_job(job_id)
        if not latest_state or latest_state.run_revision != run_revision:
            app.logger.info("[job:%s] 过期后台任务异常已忽略 revision=%s", job_id, run_revision)
            return
        app.logger.exception("[job:%s] 后台任务异常 revision=%s", job_id, run_revision)
        jobs.update_job(job_id, status="failed", pause_requested=False, error=str(exc))


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


def _mark_pause_requested(state) -> None:
    if state.status in TERMINAL_JOB_STATUSES:
        raise ValueError("当前任务已经结束，不能暂停。")
    if state.status == "paused":
        return
    state.pause_requested = True
    if state.status in ACTIVE_JOB_STATUSES:
        state.status = "pausing"


def _resume_job_state(state) -> bool:
    if state.status == "paused":
        state.pause_requested = False
        state.status = "queued"
        state.run_revision += 1
        state.error = ""
        return True
    if state.status == "pausing":
        state.pause_requested = False
        state.status = "running"
        state.error = ""
        return False
    if state.status in {"queued", "running"}:
        return False
    raise ValueError("当前任务不在可继续状态。")


def _restart_job_state(state) -> None:
    if not state.script_text:
        raise ValueError("当前任务缺少原始剧本，无法重新生成。")
    state.pause_requested = False
    state.status = "queued"
    state.completed_count = 0
    state.passed_audit_count = 0
    state.fallback_count = 0
    state.content = state.initial_content
    state.audits = []
    state.rewritten_episodes = []
    state.error = ""
    state.run_revision += 1


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
