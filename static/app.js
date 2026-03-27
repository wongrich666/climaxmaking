const form = document.getElementById("rewrite-form");
const fileInput = document.getElementById("script-file");
const providerSelect = document.getElementById("provider");
const statusText = document.getElementById("status-text");
const startButton = document.getElementById("start-button");
const pauseButton = document.getElementById("pause-button");
const resumeButton = document.getElementById("resume-button");
const restartButton = document.getElementById("restart-button");
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
let activeJobStatus = "";
let pollingTimer = null;
let lastCompletedCount = -1;
let requestInFlight = false;

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

function stopPolling() {
    if (pollingTimer) {
        clearTimeout(pollingTimer);
        pollingTimer = null;
    }
}

function isWorkingStatus(status) {
    return ["queued", "running", "pausing"].includes(status);
}

function isLockedStatus(status) {
    return [...new Set(["queued", "running", "pausing", "paused"])].includes(status);
}

function syncActionButtons() {
    const hasContent = Boolean(resultContent && resultContent.value);
    const hasJob = Boolean(activeJobId);
    const lockedInputs = isLockedStatus(activeJobStatus);

    if (startButton) {
        startButton.disabled = requestInFlight || lockedInputs;
    }
    if (pauseButton) {
        pauseButton.disabled = requestInFlight || !hasJob || !isWorkingStatus(activeJobStatus);
    }
    if (resumeButton) {
        resumeButton.disabled = requestInFlight || !hasJob || activeJobStatus !== "paused";
    }
    if (restartButton) {
        restartButton.disabled = requestInFlight || !hasJob;
    }
    if (fileInput) {
        fileInput.disabled = requestInFlight || lockedInputs;
    }
    if (providerSelect) {
        providerSelect.disabled = requestInFlight || lockedInputs;
    }
    if (copyButton) {
        copyButton.disabled = !hasContent;
    }
    if (downloadButton) {
        downloadButton.disabled = !hasContent;
    }
}

function setRequestInFlight(isBusy) {
    requestInFlight = isBusy;
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
    if (data.status) {
        activeJobStatus = data.status;
    }
    if (resultTitle && data.title) {
        resultTitle.textContent = data.title;
    }
    if (resultMeta) {
        const progressText = `${data.completed_count || 0}/${data.episode_count || 0} 集`;
        resultMeta.textContent = `${data.status || "unknown"} · ${progressText} · job ${data.job_id || activeJobId || "-"}`;
    }
}

function applyJobSnapshot(data) {
    if (data.job_id) {
        activeJobId = data.job_id;
    }
    if (data.status) {
        activeJobStatus = data.status;
    }
    latestFileName = data.download_name || latestFileName;
    updateResultHeader(data);
    updateContent(data.content || "");
    renderAudits(data.audits || [], data.fallback_count || 0);
    syncActionButtons();
}

function jobStateLabel(data) {
    const done = data.completed_count || 0;
    const total = data.episode_count || 0;
    if (data.status === "queued") {
        return `排队中 ${done}/${total}`;
    }
    if (data.status === "running") {
        return `处理中 ${done}/${total}`;
    }
    if (data.status === "pausing") {
        return `暂停中 ${done}/${total}`;
    }
    if (data.status === "paused") {
        return `已暂停 ${done}/${total}`;
    }
    if (data.status === "completed") {
        return "任务完成";
    }
    if (data.status === "failed") {
        return "任务失败";
    }
    return "等待提交";
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
        if (jobId !== activeJobId) {
            return;
        }

        applyJobSnapshot(data);
        const completedCount = data.completed_count || 0;
        if (completedCount !== lastCompletedCount) {
            lastCompletedCount = completedCount;
            appendDiagnostic(`任务进度更新: ${completedCount}/${data.episode_count || 0} 集，status=${data.status}，request-id=${requestId || "-"}`);
        }

        if (requestState) {
            requestState.textContent = jobStateLabel(data);
        }

        if (data.status === "completed") {
            stopPolling();
            statusText.textContent = "全部集数处理完成，结果已保留在文本框与 outputs 目录里。";
            appendDiagnostic(`任务完成: job=${jobId}，输出文件=${data.partial_output_path || "-"}`);
            return;
        }

        if (data.status === "paused") {
            stopPolling();
            statusText.textContent = "任务已暂停，点击“继续任务”会从当前进度继续。";
            appendDiagnostic(`任务已暂停: job=${jobId}，当前进度 ${completedCount}/${data.episode_count || 0}`);
            return;
        }

        if (data.status === "failed") {
            stopPolling();
            statusText.textContent = `任务中断，但前面的成果已保留：${data.error || "请查看审核结果和终端日志。"}`;
            appendDiagnostic(`任务失败: job=${jobId}，已保留部分结果，输出文件=${data.partial_output_path || "-"}`);
            return;
        }

        pollingTimer = setTimeout(() => pollJob(jobId), 1000);
    } catch (error) {
        stopPolling();
        const message = extractErrorMessage(error);
        if (requestState) {
            requestState.textContent = "轮询失败";
        }
        statusText.textContent = message;
        appendDiagnostic(`轮询失败: ${message}`);
        syncActionButtons();
    }
}

async function sendJobCommand(action, labels) {
    if (!activeJobId) {
        statusText.textContent = "当前没有可控制的任务。";
        return;
    }

    setRequestInFlight(true);
    if (requestState) {
        requestState.textContent = labels.pending;
    }
    appendDiagnostic(`${labels.name} 请求已发送: job=${activeJobId}`);

    try {
        const { response, data, requestId } = await fetchJson(`/api/jobs/${activeJobId}/${action}`, {
            method: "POST",
            headers: {
                "X-Requested-With": "fetch",
            },
        });
        appendDiagnostic(`${labels.name} 响应: HTTP ${response.status} request-id=${requestId || "-"}`);

        if (!response.ok || !data.ok) {
            throw new Error(data.error || `${labels.name}失败，request_id=${data.request_id || requestId || "-"}`);
        }

        if (action === "restart") {
            lastCompletedCount = -1;
        }

        applyJobSnapshot(data);
        if (requestState) {
            requestState.textContent = jobStateLabel(data);
        }
        statusText.textContent = labels.success(data);

        stopPolling();
        if (isWorkingStatus(data.status)) {
            pollingTimer = setTimeout(() => pollJob(activeJobId), 350);
        }
    } catch (error) {
        const message = extractErrorMessage(error);
        statusText.textContent = message;
        appendDiagnostic(`${labels.name}失败: ${message}`);
    } finally {
        setRequestInFlight(false);
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

if (!form || !statusText || !startButton || !resultContent) {
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

        stopPolling();
        lastCompletedCount = -1;
        activeJobId = "";
        activeJobStatus = "";
        latestFileName = "";
        renderAudits([], 0);
        syncActionButtons();

        setRequestInFlight(true);
        resetDiagnostic(`准备发送 POST ${endpoint}`);
        appendDiagnostic(`已选择文件: ${file.name} (${file.size} bytes)`);
        appendDiagnostic(`模型渠道: ${formData.get("provider")}`);
        statusText.textContent = "后台任务启动中，系统会逐集处理并实时刷新结果。";
        if (resultMeta) {
            resultMeta.textContent = "任务启动中";
        }
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

            applyJobSnapshot({
                ...data,
                completed_count: 0,
                fallback_count: 0,
                audits: [],
            });
            if (requestState) {
                requestState.textContent = jobStateLabel({ ...data, completed_count: 0 });
            }
            appendDiagnostic(`任务已创建: job=${activeJobId}，初始输出=${data.partial_output_path || "-"}`);
            pollingTimer = setTimeout(() => pollJob(activeJobId), 400);
        } catch (error) {
            const message = extractErrorMessage(error);
            activeJobId = "";
            activeJobStatus = "";
            if (requestState) {
                requestState.textContent = "请求失败";
            }
            if (resultMeta) {
                resultMeta.textContent = "处理失败";
            }
            statusText.textContent = message;
            appendDiagnostic(`启动失败: ${message}`);
            syncActionButtons();
        } finally {
            setRequestInFlight(false);
        }
    });

    if (pauseButton) {
        pauseButton.addEventListener("click", async () => {
            await sendJobCommand("pause", {
                name: "暂停生成",
                pending: "暂停请求已发送",
                success: (data) => (
                    data.status === "paused"
                        ? "任务已暂停，点击“继续任务”会从当前进度继续。"
                        : "正在暂停任务，当前步骤完成后会停在断点处。"
                ),
            });
        });
    }

    if (resumeButton) {
        resumeButton.addEventListener("click", async () => {
            await sendJobCommand("resume", {
                name: "继续任务",
                pending: "继续请求已发送",
                success: () => "任务已恢复，会从暂停时的进度继续处理。",
            });
        });
    }

    if (restartButton) {
        restartButton.addEventListener("click", async () => {
            await sendJobCommand("restart", {
                name: "全部重新生成",
                pending: "重开请求已发送",
                success: () => "任务已从头重新开始生成，之前的进度已重置。",
            });
        });
    }

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

    syncActionButtons();
}
