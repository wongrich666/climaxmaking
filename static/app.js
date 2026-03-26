const form = document.getElementById("rewrite-form");
const statusText = document.getElementById("status-text");
const submitButton = document.getElementById("submit-button");
const resultTitle = document.getElementById("result-title");
const resultMeta = document.getElementById("result-meta");
const resultContent = document.getElementById("result-content");
const copyButton = document.getElementById("copy-button");
const downloadButton = document.getElementById("download-button");
const diagnosticOutput = document.getElementById("diagnostic-output");
const requestState = document.getElementById("request-state");
const auditSummary = document.getElementById("audit-summary");
const auditList = document.getElementById("audit-list");

let latestFileName = "";
let activeJobId = "";
let pollingTimer = null;
let lastCompletedCount = -1;

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

function syncActionButtons() {
    const hasContent = Boolean(resultContent && resultContent.value);
    if (copyButton) {
        copyButton.disabled = !hasContent;
    }
    if (downloadButton) {
        downloadButton.disabled = !hasContent;
    }
}

function setBusyState(isBusy) {
    if (submitButton) {
        submitButton.disabled = isBusy;
    }
    if (isBusy && requestState) {
        requestState.textContent = "请求进行中";
    }
    syncActionButtons();
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
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

    const passCount = audits.filter((item) => item.verdict === "pass" && !item.fallback_used).length;
    auditSummary.textContent = `通过 ${passCount}/${audits.length} 集 · 回退 ${fallbackCount} 集`;
    auditList.innerHTML = audits.map((item) => {
        const verdictClass = `verdict-${item.verdict}`;
        const hookScore = item.hook_score ?? "-";
        const consistencyScore = item.consistency_score ?? "-";
        const attemptsUsed = item.attempts_used ?? "-";
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
                <p>字数 ${item.rewritten_length}/${item.original_length}，允许范围 ${item.min_length}-${item.max_length}，Hook ${hookScore}，一致性 ${consistencyScore}，重写 ${attemptsUsed} 次</p>
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

function stopPolling() {
    if (pollingTimer) {
        clearTimeout(pollingTimer);
        pollingTimer = null;
    }
}

function updateContent(content) {
    if (!resultContent || typeof content !== "string") {
        return;
    }
    if (resultContent.value === content) {
        return;
    }
    const wasAtBottom = resultContent.scrollHeight - resultContent.scrollTop - resultContent.clientHeight < 24;
    resultContent.value = content;
    if (wasAtBottom) {
        resultContent.scrollTop = resultContent.scrollHeight;
    }
    syncActionButtons();
}

function updateResultHeader(data) {
    if (resultTitle && data.title) {
        resultTitle.textContent = data.title;
    }
    if (resultMeta) {
        const progressText = `${data.completed_count || 0}/${data.episode_count || 0} 集`;
        resultMeta.textContent = `${data.status || "unknown"} · ${progressText} · job ${data.job_id || activeJobId || "-"}`;
    }
}

async function fetchJson(url, options = {}) {
    const mergedHeaders = {
        Accept: "application/json",
        ...(options.headers || {}),
    };
    const { headers, ...restOptions } = options;
    const response = await fetch(url, {
        credentials: "same-origin",
        ...restOptions,
        headers: mergedHeaders,
    });
    const contentType = response.headers.get("content-type") || "";
    const requestId = response.headers.get("X-Request-Id") || "";
    if (!contentType.includes("application/json")) {
        const rawText = await response.text();
        throw new Error(`服务端返回了非 JSON 内容（HTTP ${response.status}）：${rawText.slice(0, 120)}`);
    }
    const data = await response.json();
    return { response, data, requestId };
}

async function pollJob(jobId) {
    try {
        const { data, requestId } = await fetchJson(`/api/jobs/${jobId}`);
        if (!data.ok) {
            throw new Error(data.error || "任务状态查询失败。");
        }

        latestFileName = data.download_name || latestFileName;
        updateResultHeader(data);
        updateContent(data.content || "");
        renderAudits(data.audits || [], data.fallback_count || 0);
        syncActionButtons();

        if ((data.completed_count || 0) !== lastCompletedCount) {
            lastCompletedCount = data.completed_count || 0;
            appendDiagnostic(`任务进度更新: ${lastCompletedCount}/${data.episode_count || 0} 集，status=${data.status}，request-id=${requestId || "-"}`);
        }

        if (requestState) {
            requestState.textContent = `处理中 ${data.completed_count || 0}/${data.episode_count || 0}`;
        }

        if (data.status === "completed") {
            stopPolling();
            setBusyState(false);
            if (requestState) {
                requestState.textContent = "任务完成";
            }
            statusText.textContent = "全部集数处理完成，结果已保留在文本框与 outputs 目录里。";
            appendDiagnostic(`任务完成: job=${jobId}，输出文件=${data.partial_output_path || "-"}`);
            return;
        }

        if (data.status === "failed") {
            stopPolling();
            setBusyState(false);
            if (requestState) {
                requestState.textContent = "任务失败";
            }
            statusText.textContent = `任务中断，但前面的成果已保留：${data.error || "请查看审核结果和终端日志。"}`;
            appendDiagnostic(`任务失败: job=${jobId}，已保留部分结果，输出文件=${data.partial_output_path || "-"}`);
            return;
        }

        pollingTimer = setTimeout(() => pollJob(jobId), 1000);
    } catch (error) {
        stopPolling();
        setBusyState(false);
        if (requestState) {
            requestState.textContent = "轮询失败";
        }
        const message = extractErrorMessage(error);
        statusText.textContent = message;
        appendDiagnostic(`轮询失败: ${message}`);
    }
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
        stopPolling();
        lastCompletedCount = -1;
        activeJobId = "";

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
        statusText.textContent = "后台任务已启动，系统会逐集处理并实时刷新结果。";
        resultMeta.textContent = "任务启动中";
        if (requestState) {
            requestState.textContent = "POST 已发送";
        }

        try {
            const { response, data, requestId } = await fetchJson(endpoint, {
                method: "POST",
                headers: {
                    "X-Requested-With": "fetch",
                },
                body: formData,
            });
            appendDiagnostic(`收到启动响应: HTTP ${response.status} request-id=${requestId || "-"}`);

            if (!response.ok || !data.ok) {
                throw new Error(data.error || `启动任务失败，request_id=${data.request_id || requestId || "-"}`);
            }

            activeJobId = data.job_id;
            latestFileName = data.download_name || latestFileName;
            resultTitle.textContent = data.title || "处理中";
            resultMeta.textContent = `queued · 0/${data.episode_count || 0} 集 · job ${activeJobId}`;
            updateContent(data.content || "");
            renderAudits([], 0);
            syncActionButtons();
            appendDiagnostic(`任务已创建: job=${activeJobId}，初始输出=${data.partial_output_path || "-"}`);
            pollingTimer = setTimeout(() => pollJob(activeJobId), 400);
        } catch (error) {
            setBusyState(false);
            if (requestState) {
                requestState.textContent = "请求失败";
            }
            const message = extractErrorMessage(error);
            resultMeta.textContent = "处理失败";
            statusText.textContent = message;
            appendDiagnostic(`启动失败: ${message}`);
        }
    });

    if (copyButton) {
        copyButton.addEventListener("click", async () => {
            const content = resultContent.value;
            if (!content) {
                appendDiagnostic("复制中止：结果内容为空。");
                return;
            }
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(content);
                } else {
                    resultContent.focus();
                    resultContent.select();
                    document.execCommand("copy");
                }
                appendDiagnostic("已复制当前全文到剪贴板。");
                statusText.textContent = "当前显示的剧本内容已经复制到剪贴板。";
            } catch (error) {
                const message = extractErrorMessage(error);
                appendDiagnostic(`复制失败: ${message}`);
                statusText.textContent = `复制失败：${message}`;
            }
        });
    }

    if (downloadButton) {
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
}
