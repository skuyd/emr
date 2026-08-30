(() => {
  "use strict";

  const app = document.querySelector("[data-upload-app]");
  if (!app) return;

  const MAX_FILES = 20;
  const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
  const MAX_PDF_BYTES = 100 * 1024 * 1024;
  const MAX_CONCURRENT_UPLOADS = 3;
  const COPY = {
    invalid_file_metadata: "文件信息无效，请重新选择",
    unsupported_file: "暂不支持这种文件格式",
    file_too_large: "文件过大或页数过多",
    too_many_pages: "文件过大或页数过多",
    unreadable_file: "文件无法读取",
    encrypted_pdf: "加密 PDF 暂不支持",
    unsafe_pdf: "PDF 包含暂不支持的内容",
    extension_mismatch: "文件格式与扩展名不一致",
    batch_file_limit: "每批最多选择 20 个文件",
    batch_page_limit: "每批总页数最多 60 页",
    document_limit: "已达到试用资料数量上限",
    page_limit: "已达到试用页数上限",
    storage_limit: "已达到试用存储上限",
    upload_rate_limited: "操作较频繁，请稍后重试",
    storage_unavailable: "保存服务暂时不可用，请重试",
    upload_service_unavailable: "上传暂时未完成，请重试",
    invalid_upload_request: "上传请求无效，请重试",
    network_error: "上传未完成，请检查网络后重试",
    upload_state_conflict: "这份资料的状态已更新，请刷新页面",
  };

  const form = app.querySelector("[data-upload-form]");
  const input = app.querySelector("[data-file-input]");
  const dropzone = app.querySelector("[data-dropzone]");
  const startButton = app.querySelector("[data-start-upload]");
  const clearButton = app.querySelector("[data-clear-files]");
  const summary = app.querySelector("[data-selection-summary]");
  const list = app.querySelector("[data-file-list]");
  const empty = app.querySelector("[data-queue-empty]");
  const live = app.querySelector("[data-live-region]");
  const leaveNotice = app.querySelector("[data-leave-notice]");
  const batchSummary = app.querySelector("[data-batch-summary]");
  const rowTemplate = document.querySelector("#upload-file-row-template");
  const csrfToken = form.querySelector("input[name=csrfmiddlewaretoken]").value;

  let rows = [];
  let batchId = null;
  let activeUploads = 0;
  let uploadStarted = false;
  let lastEtag = "";
  let pollTimer = null;
  let pollController = null;

  function announce(message) {
    live.textContent = "";
    window.requestAnimationFrame(() => { live.textContent = message; });
  }

  function humanSize(bytes) {
    if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
  }

  function extension(name) {
    const index = name.lastIndexOf(".");
    return index < 0 ? "" : name.slice(index + 1).toLowerCase();
  }

  function localError(file) {
    const suffix = extension(file.name);
    if (!["jpg", "jpeg", "png", "heic", "pdf"].includes(suffix)) return "unsupported_file";
    const limit = suffix === "pdf" ? MAX_PDF_BYTES : MAX_IMAGE_BYTES;
    if (!file.size || file.size > limit) return "file_too_large";
    return "";
  }

  function stateCopy(state, errorCode = "") {
    if (state === "PENDING") return "待上传";
    if (state === "QUEUED") return "等待上传";
    if (state === "UPLOADING") return "上传中";
    if (state === "PROCESSING") return "原件已保存，正在处理";
    if (state === "ORGANIZED") return "已整理";
    if (state === "ORIGINAL_ONLY") return "仅原件";
    if (state === "PROCESSING_FAILED") return "处理失败，原件仍可查看";
    if (state === "EXACT_DUPLICATE") return "这份资料已经存在";
    if (state === "UPLOAD_FAILED") return COPY[errorCode] || "上传失败，请重试";
    return "状态更新中";
  }

  function updateBoundary() {
    const unsaved = uploadStarted && rows.some((row) => ["PENDING", "QUEUED", "UPLOADING"].includes(row.state));
    const saved = rows.some((row) => row.saved);
    if (unsaved) leaveNotice.textContent = "仍有原件尚未保存，离开本页可能需要重新上传。";
    else if (saved) leaveNotice.textContent = "原件已保存，系统正在处理；你现在可以离开此页面。";
    else leaveNotice.textContent = uploadStarted ? "本批没有已保存的原件。" : "原件尚未开始上传。";
  }

  function updateControls() {
    empty.hidden = rows.length > 0;
    summary.textContent = rows.length ? `已选择 ${rows.length} 个文件` : "尚未选择文件";
    startButton.disabled = rows.length === 0 || uploadStarted;
    clearButton.disabled = rows.length === 0 || uploadStarted;
    input.disabled = uploadStarted;
    updateBoundary();
  }

  function setRowState(row, state, errorCode = "") {
    row.state = state;
    row.errorCode = errorCode;
    const status = row.element.querySelector("[data-file-status]");
    status.textContent = stateCopy(state, errorCode);
    status.dataset.state = state;
    const retry = row.element.querySelector("[data-retry-file]");
    const remove = row.element.querySelector("[data-remove-file]");
    retry.hidden = state !== "UPLOAD_FAILED" || !["network_error", "storage_unavailable", "upload_service_unavailable"].includes(errorCode);
    remove.disabled = ["UPLOADING", "PROCESSING", "ORGANIZED", "ORIGINAL_ONLY", "PROCESSING_FAILED", "EXACT_DUPLICATE"].includes(state);
    const progress = row.element.querySelector("[data-file-progress]");
    progress.hidden = !["QUEUED", "UPLOADING"].includes(state);
    if (state === "UPLOAD_FAILED") progress.value = 0;
    updateBoundary();
  }

  function setPageCount(row, pageCount) {
    if (!Number.isInteger(pageCount) || pageCount < 1) return;
    row.pageCount = pageCount;
    row.element.querySelector("[data-file-meta]").textContent = `${pageCount} 页 · ${humanSize(row.file.size)}`;
  }

  function revokePreview(row) {
    if (row.previewUrl) URL.revokeObjectURL(row.previewUrl);
    row.previewUrl = "";
  }

  function resetBatchSession() {
    batchId = null;
    uploadStarted = false;
    lastEtag = "";
    window.clearTimeout(pollTimer);
    if (pollController) pollController.abort();
    pollController = null;
    batchSummary.hidden = true;
    batchSummary.textContent = "";
  }

  function reindexRows() {
    rows.forEach((row, index) => { row.ordinal = index + 1; });
  }

  async function removeRow(row) {
    if (["UPLOADING", "PROCESSING", "ORGANIZED", "ORIGINAL_ONLY", "PROCESSING_FAILED", "EXACT_DUPLICATE"].includes(row.state)) return;
    if (batchId && row.itemId) {
      try {
        const response = await fetch(`/api/upload-batches/${batchId}/items/${row.itemId}/remove/`, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken, "Accept": "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) {
          announce("当前无法移除这份资料。");
          return;
        }
        const result = await response.json();
        if (result.batch_deleted) resetBatchSession();
      } catch (_error) {
        announce("网络连接中断，暂时无法移除。");
        return;
      }
    }
    revokePreview(row);
    row.element.remove();
    rows = rows.filter((candidate) => candidate !== row);
    if (!uploadStarted) reindexRows();
    updateControls();
    announce("已移除一份资料。");
  }

  function buildRow(file) {
    const fragment = rowTemplate.content.cloneNode(true);
    const element = fragment.querySelector("[data-file-row]");
    const row = {
      file,
      ordinal: rows.length + 1,
      state: "PENDING",
      errorCode: localError(file),
      saved: false,
      itemId: null,
      pageCount: null,
      previewUrl: "",
      element,
    };
    element.querySelector("[data-file-name]").textContent = file.name;
    element.querySelector("[data-file-meta]").textContent = humanSize(file.size);
    const preview = element.querySelector("[data-preview]");
    if (["jpg", "jpeg", "png"].includes(extension(file.name))) {
      const image = document.createElement("img");
      image.alt = "";
      row.previewUrl = URL.createObjectURL(file);
      image.src = row.previewUrl;
      preview.append(image);
    } else {
      preview.textContent = extension(file.name) === "pdf" ? "PDF" : "图片";
    }
    element.querySelector("[data-remove-file]").addEventListener("click", () => removeRow(row));
    element.querySelector("[data-retry-file]").addEventListener("click", () => {
      setRowState(row, "QUEUED");
      pumpQueue();
    });
    list.append(fragment);
    if (row.errorCode) setRowState(row, "UPLOAD_FAILED", row.errorCode);
    return row;
  }

  function addFiles(fileList) {
    if (uploadStarted) return;
    const incoming = Array.from(fileList);
    const available = Math.max(0, MAX_FILES - rows.length);
    incoming.slice(0, available).forEach((file) => rows.push(buildRow(file)));
    if (incoming.length > available) announce("每批最多选择 20 个文件，多出的文件未加入。");
    else if (incoming.length) announce(`已加入 ${incoming.length} 个文件。`);
    input.value = "";
    updateControls();
  }

  async function createBatch() {
    const response = await fetch(app.dataset.createUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken, "Accept": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ files: rows.map((row) => ({ name: row.file.name, byte_size: row.file.size })) }),
    });
    const result = await response.json().catch(() => ({ error: { code: "upload_service_unavailable" } }));
    if (!response.ok) throw new Error(result.error?.code || "upload_service_unavailable");
    batchId = result.batch_id;
    result.items.forEach((serverItem) => {
      const row = rows[serverItem.ordinal - 1];
      if (!row) return;
      row.itemId = serverItem.item_id;
      if (serverItem.accepted) setRowState(row, "QUEUED");
      else setRowState(row, "UPLOAD_FAILED", serverItem.error_code);
    });
  }

  function parseXhr(xhr) {
    try { return JSON.parse(xhr.responseText); }
    catch (_error) { return { error: { code: "upload_service_unavailable" } }; }
  }

  function uploadRow(row) {
    activeUploads += 1;
    setRowState(row, "UPLOADING");
    const progress = row.element.querySelector("[data-file-progress]");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload-batches/${batchId}/items/${row.itemId}/content/`);
    xhr.responseType = "text";
    xhr.setRequestHeader("X-CSRFToken", csrfToken);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) progress.value = Math.min(99, Math.round((event.loaded / event.total) * 100));
    });
    xhr.addEventListener("load", () => {
      const result = parseXhr(xhr);
      if (xhr.status >= 200 && xhr.status < 300 && result.saved === true) {
        row.saved = true;
        setPageCount(row, result.page_count);
        progress.value = 100;
        setRowState(row, result.status || "PROCESSING");
        if (result.outcome === "EXACT_DUPLICATE" && result.document_id) {
          const link = row.element.querySelector("[data-existing-link]");
          link.href = `/records/${encodeURIComponent(result.document_id)}/`;
          link.hidden = false;
        }
        if (result.possible_duplicate === true) {
          row.element.querySelector("[data-possible-duplicate]").hidden = false;
        }
        announce(
          result.outcome === "EXACT_DUPLICATE"
            ? "这份资料已经存在，可打开已有资料。"
            : result.possible_duplicate === true
              ? "原件已保存；这份资料可能与已有资料重复，系统仍会继续整理。"
              : "一份原件已保存，正在处理。"
        );
      } else {
        setRowState(row, "UPLOAD_FAILED", result.error?.code || "upload_service_unavailable");
        announce(stateCopy("UPLOAD_FAILED", row.errorCode));
      }
    });
    xhr.addEventListener("error", () => {
      setRowState(row, "UPLOAD_FAILED", "network_error");
      announce(COPY.network_error);
    });
    xhr.addEventListener("abort", () => setRowState(row, "UPLOAD_FAILED", "network_error"));
    xhr.addEventListener("loadend", () => {
      activeUploads = Math.max(0, activeUploads - 1);
      pumpQueue();
      if (activeUploads === 0 && !rows.some((candidate) => candidate.state === "QUEUED")) schedulePoll(500);
    });
    const body = new FormData();
    body.append("file", row.file, row.file.name);
    xhr.send(body);
  }

  function pumpQueue() {
    while (activeUploads < MAX_CONCURRENT_UPLOADS) {
      const row = rows.find((candidate) => candidate.state === "QUEUED");
      if (!row) break;
      uploadRow(row);
    }
  }

  function applyProjection(payload) {
    payload.items.forEach((serverItem) => {
      const row = rows.find((candidate) => candidate.itemId === serverItem.item_id);
      if (!row || row.state === "UPLOADING") return;
      if (serverItem.status === "UPLOAD_FAILED") setRowState(row, "UPLOAD_FAILED", serverItem.error_code || "upload_service_unavailable");
      else if (["PROCESSING", "ORGANIZED", "ORIGINAL_ONLY", "PROCESSING_FAILED", "EXACT_DUPLICATE"].includes(serverItem.status)) {
        row.saved = serverItem.status !== "UPLOAD_FAILED";
        setPageCount(row, serverItem.page_count);
        setRowState(row, serverItem.status);
      }
    });
    const counts = payload.counts;
    batchSummary.hidden = false;
    batchSummary.textContent = `处理中 ${counts.processing}，已完成 ${counts.completed}，失败 ${counts.failed}`;
  }

  async function pollStatus() {
    if (!batchId || document.hidden) return schedulePoll(3000);
    pollController = new AbortController();
    try {
      const headers = { "Accept": "application/json" };
      if (lastEtag) headers["If-None-Match"] = lastEtag;
      const response = await fetch(`/api/upload-batches/${batchId}/status/`, {
        headers,
        credentials: "same-origin",
        signal: pollController.signal,
      });
      if (response.status === 304) return schedulePoll(3000);
      if (!response.ok) return schedulePoll(5000);
      lastEtag = response.headers.get("ETag") || "";
      const payload = await response.json();
      applyProjection(payload);
      if (!payload.terminal) schedulePoll(3000);
    } catch (error) {
      if (error.name !== "AbortError") schedulePoll(5000);
    }
  }

  function schedulePoll(delay) {
    window.clearTimeout(pollTimer);
    if (!batchId) return;
    pollTimer = window.setTimeout(pollStatus, delay);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!rows.length || uploadStarted) return;
    uploadStarted = true;
    updateControls();
    try {
      await createBatch();
      announce("上传任务已创建，开始逐份保存原件。");
      pumpQueue();
    } catch (error) {
      uploadStarted = false;
      rows.forEach((row) => {
        if (row.state === "PENDING") setRowState(row, "UPLOAD_FAILED", error.message || "upload_service_unavailable");
      });
      announce(COPY[error.message] || "暂时无法开始上传，请重试。");
      updateControls();
    }
  });

  input.addEventListener("change", () => addFiles(input.files));
  clearButton.addEventListener("click", () => {
    rows.forEach(revokePreview);
    rows = [];
    list.replaceChildren();
    updateControls();
    announce("已清空所选文件。");
  });
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    if (!uploadStarted) dropzone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  }));
  dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

  window.addEventListener("beforeunload", (event) => {
    if (uploadStarted && rows.some((row) => ["PENDING", "QUEUED", "UPLOADING"].includes(row.state))) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(pollTimer);
    if (pollController) pollController.abort();
    rows.forEach(revokePreview);
  });

  updateControls();
})();
