from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from config import settings
from llm_client import LLMClient
from rewriter import ScriptRewriter
from script_parser import decode_text_file


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

rewriter = ScriptRewriter(LLMClient(settings))


@app.get("/")
def index():
    return render_template(
        "index.html",
        default_provider=settings.default_provider,
        providers=settings.provider_options(),
    )


@app.post("/api/process")
def process_script():
    upload = request.files.get("script_file")
    provider_name = request.form.get("provider") or settings.default_provider
    if not upload or not upload.filename:
        return jsonify({"ok": False, "error": "请先选择一个剧本 txt 文件。"}), 400
    if not upload.filename.lower().endswith(".txt"):
        return jsonify({"ok": False, "error": "目前只支持上传 .txt 文件。"}), 400

    try:
        script_text = decode_text_file(upload.read())
        result = rewriter.rewrite_script(script_text, provider_name=provider_name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        {
            "ok": True,
            "title": result.title,
            "provider": result.provider,
            "episode_count": result.episode_count,
            "download_name": result.download_name,
            "content": result.content,
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
