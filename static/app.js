const form = document.getElementById("rewrite-form");
const statusText = document.getElementById("status-text");
const submitButton = document.getElementById("submit-button");
const resultTitle = document.getElementById("result-title");
const resultMeta = document.getElementById("result-meta");
const resultContent = document.getElementById("result-content");
const downloadButton = document.getElementById("download-button");

let latestFileName = "";

function setBusyState(isBusy) {
    submitButton.disabled = isBusy;
    downloadButton.disabled = isBusy || !resultContent.value;
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    const file = formData.get("script_file");

    if (!(file instanceof File) || !file.name) {
        statusText.textContent = "请先选择一个 txt 文件。";
        return;
    }

    setBusyState(true);
    statusText.textContent = "正在逐集改写首场，这一步会按集顺序调用模型，请稍等。";
    resultMeta.textContent = "处理中";

    try {
        const response = await fetch("/api/process", {
            method: "POST",
            body: formData,
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
            throw new Error(data.error || "处理失败，请检查日志。");
        }

        latestFileName = data.download_name;
        resultTitle.textContent = data.title;
        resultMeta.textContent = `共 ${data.episode_count} 集 · 使用 ${data.provider}`;
        resultContent.value = data.content;
        statusText.textContent = "处理完成，完整剧本已经更新到下方文本框，可直接下载。";
        downloadButton.disabled = false;
    } catch (error) {
        resultMeta.textContent = "处理失败";
        statusText.textContent = error.message || "处理失败，请稍后重试。";
    } finally {
        setBusyState(false);
    }
});

downloadButton.addEventListener("click", () => {
    const content = resultContent.value;
    if (!content) {
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
});
