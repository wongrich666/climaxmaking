const form = document.getElementById("rewrite-form");
const statusText = document.getElementById("status-text");
const submitButton = document.getElementById("submit-button");
const resultTitle = document.getElementById("result-title");
const resultMeta = document.getElementById("result-meta");
const resultContent = document.getElementById("result-content");
const downloadButton = document.getElementById("download-button");
const diagnosticOutput = document.getElementById("diagnostic-output");
const requestState = document.getElementById("request-state");
const auditSummary = document.getElementById("audit-summary");
const auditList = document.getElementById("audit-list");

let latestFileName = "";

function appendDiagnostic(message) {
    if (!diagnosticOutput) {
        return;
    }
    const timestamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    diagnosticOutput.textContent = `${diagnosticOutput.textContent}\n[${timestamp}] ${message}`.trim();
    diagnosticOutput.scrollTop = diagnosticOutput.scrollHeight;
}

function resetDiagnostic(message) {
    if (!diagnosticOutput) {
        return;
    }
    diagnosticOutput.textContent = message;
}

function setBusyState(isBusy) {
    submitButton.disabled = isBusy;
    downloadButton.disabled = isBusy || !resultContent.value;
    if (isBusy) {
        requestState.textContent = "请求进行中";
    }
}

function renderAudits(audits = [], fallbackCount = 0) {
    if (!auditList || !auditSummary) {
        return;
    }

    if (!audits.length) {
        auditSummary.textContent = "暂无审核";
        auditList.innerHTML = `
            <article class="audit-item muted">
                <h3>还没有处理任何剧本</h3>
                <p>每集首场的长度审核、剧情一致性审核和回退情况会显示在这里。</p>
            </article>
        `;
        return;
    }

    const passCount = audits.filter((item) => !item.fallback_used).length;
    auditSummary.textContent = `通过 ${passCount}/${audits.length} 集 · 回退 ${fallbackCount} 集`;
    auditList.innerHTML = audits.map((item) => {
        const verdictClass = `verdict-${item.verdict}`;
        const hookScore = item.hook_score ?? "-";
        const consistencyScore = item.consistency_score ?? "-";
        const heading = escapeHtml(item.episode_heading || "");
        const summary = escapeHtml(item.summary || "");
        const tagText = escapeHtml(item.fallback_used ? "已回退" : item.verdict);
        return `
            <article class="audit-item ${item.fallback_used ? "fallback" : ""}">
                <div class="audit-item-header">
                    <h3>${heading}</h3>
                    <span class="audit-tag ${verdictClass}">${tagText}</span>
                </div>
                <p>${summary}</p>
                <p>字数 ${item.rewritten_length}/${item.original_length}，允许范围 ${item.min_length}-${item.max_length}，Hook ${hookScore}，一致性 ${consistencyScore}</p>
            </article>
        `;
    }).join("");
}

function extractErrorMessage(error) {
    if (!error) {
        return "处理失败，请稍后重试。";
    }
    if (typeof error === "string") {
        return error;
    }
    return error.message || "处理失败，请稍后重试。";
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

window.addEventListener("error", (event) => {
    appendDiagnostic(`前端脚本错误: ${event.message}`);
    if (requestState) {
        requestState.textContent = "前端错误";
    }
});

window.addEventListener("unhandledrejection", (event) => {
    appendDiagnostic(`Promise 未处理异常: ${extractErrorMessage(event.reason)}`);
    if (requestState) {
        requestState.textContent = "前端错误";
    }
});

if (!form || !statusText || !submitButton || !resultContent) {
    appendDiagnostic("页面初始化失败，关键 DOM 节点缺失。");
} else {
    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const file = formData.get("script_file");
        const endpoint = form.getAttribute("action") || "/api/process";

        if (!(file instanceof File) || !file.name) {
            statusText.textContent = "请先选择一个 txt 文件。";
            appendDiagnostic("拦截提交：未选择文件。");
            return;
        }

        setBusyState(true);
        resetDiagnostic(`准备发送 POST ${endpoint}`);
        appendDiagnostic(`已选择文件: ${file.name} (${file.size} bytes)`);
        appendDiagnostic(`模型渠道: ${formData.get("provider")}`);
        statusText.textContent = "正在逐集改写首场，这一步会按集顺序调用模型，请稍等。";
        resultMeta.textContent = "处理中";
        requestState.textContent = "POST 已发送";

        try {
            const response = await fetch(endpoint, {
                method: "POST",
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "fetch",
                },
                body: formData,
                credentials: "same-origin",
            });

            const contentType = response.headers.get("content-type") || "";
            const requestId = response.headers.get("X-Request-Id") || "";
            appendDiagnostic(`收到响应: HTTP ${response.status} content-type=${contentType || "unknown"} request-id=${requestId || "-"}`);

            let data;
            if (contentType.includes("application/json")) {
                data = await response.json();
            } else {
                const rawText = await response.text();
                appendDiagnostic(`非 JSON 响应片段: ${rawText.slice(0, 200)}`);
                throw new Error(`服务端返回了非 JSON 内容（HTTP ${response.status}），请检查 Flask 日志。`);
            }

            if (!response.ok || !data.ok) {
                throw new Error(data.error || `处理失败，request_id=${data.request_id || requestId || "-"}`);
            }

            latestFileName = data.download_name;
            resultTitle.textContent = data.title;
            resultMeta.textContent = `共 ${data.episode_count} 集 · 使用 ${data.provider} · request ${data.request_id}`;
            resultContent.value = data.content;
            statusText.textContent = "处理完成，完整剧本已经更新到下方文本框，可直接下载。";
            downloadButton.disabled = false;
            requestState.textContent = "请求完成";
            appendDiagnostic(`后端处理完成: request_id=${data.request_id}，通过 ${data.passed_audit_count} 集，回退 ${data.fallback_count} 集。`);
            renderAudits(data.audits, data.fallback_count);
        } catch (error) {
            resultMeta.textContent = "处理失败";
            requestState.textContent = "请求失败";
            const message = extractErrorMessage(error);
            statusText.textContent = message;
            appendDiagnostic(`请求失败: ${message}`);
        } finally {
            setBusyState(false);
        }
    });

    downloadButton.addEventListener("click", () => {
        const content = resultContent.value;
        if (!content) {
            appendDiagnostic("下载中止：结果内容为空。");
            return;
        }

        const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = latestFileName || "改写后剧本.txt";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        appendDiagnostic(`已触发下载: ${link.download}`);
    });
}
