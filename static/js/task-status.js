(() => {
  "use strict";

  const cards = Array.from(document.querySelectorAll("[data-task-card]"));
  if (!cards.length) return;

  const STATUS_COPY = {
    PENDING: "待上传",
    UPLOADING: "上传中",
    UPLOAD_FAILED: "上传失败",
    PROCESSING: "处理中",
    ORGANIZED: "已整理",
    ORIGINAL_ONLY: "仅原件",
    PROCESSING_FAILED: "处理失败",
    EXACT_DUPLICATE: "已存在",
  };
  const STATUS_KEYS = ["processing", "saved", "organized", "original", "failed"];
  const STATUS_ICON_SHAPES = {
    processing: [
      ["circle", { cx: "12", cy: "12", r: "8" }],
      ["path", { d: "M12 7v5l3 2" }],
    ],
    saved: [
      ["path", { d: "M5 4h11l3 3v13H5z" }],
      ["path", { d: "M8 4v6h8V5M9 16h6" }],
    ],
    organized: [
      ["circle", { cx: "12", cy: "12", r: "8" }],
      ["path", { d: "m8.5 12 2.3 2.3 4.8-5" }],
    ],
    original: [
      ["path", { d: "M7 3h7l4 4v14H7z" }],
      ["path", { d: "M14 3v5h4M10 13h5M10 17h5" }],
    ],
    failed: [
      ["circle", { cx: "12", cy: "12", r: "8" }],
      ["path", { d: "M12 7.5v6M12 17h.01" }],
    ],
  };
  const SVG_NS = "http://www.w3.org/2000/svg";
  const controllers = new Set();
  const timers = new Set();
  const liveRegion = document.querySelector("[data-task-live-region]");

  function notifyTaskFinished() {
    window.dispatchEvent(new CustomEvent("phr:task-terminal"));
  }

  function announce(message) {
    if (!liveRegion) return;
    liveRegion.textContent = "";
    window.requestAnimationFrame(() => { liveRegion.textContent = message; });
  }

  function mainStatus(counts) {
    if (counts.processing) return "处理中";
    if (counts.failed && counts.completed) return "部分完成";
    if (counts.failed) return "处理失败";
    if (counts.completed) return "已完成";
    return "待上传";
  }

  function mainStatusKey(counts) {
    if (counts.processing || (!counts.completed && !counts.failed)) return "processing";
    if (counts.failed) return "failed";
    return "organized";
  }

  function itemStatusKey(status) {
    if (["PENDING", "UPLOADING", "PROCESSING"].includes(status)) return "processing";
    if (status === "EXACT_DUPLICATE") return "saved";
    if (status === "ORGANIZED") return "organized";
    if (status === "ORIGINAL_ONLY") return "original";
    return "failed";
  }

  function statusKeyFromElement(element) {
    if (!element) return "";
    if (element.dataset.statusKey) return element.dataset.statusKey;
    const badge = element.querySelector(".status-badge");
    if (!badge) return "";
    return STATUS_KEYS.find((key) => badge.classList.contains(`status-badge--${key}`)) || "";
  }

  function updateStatusIcon(icon, key) {
    if (!icon || !document.createElementNS || !icon.replaceChildren) return;
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    svg.setAttribute("data-status-icon", key);
    for (const [tagName, attributes] of STATUS_ICON_SHAPES[key] || STATUS_ICON_SHAPES.processing) {
      const shape = document.createElementNS(SVG_NS, tagName);
      for (const [name, value] of Object.entries(attributes)) shape.setAttribute(name, value);
      shape.setAttribute("stroke", "currentColor");
      shape.setAttribute("stroke-width", tagName === "path" && key === "failed" ? "2" : "1.8");
      shape.setAttribute("stroke-linecap", "round");
      shape.setAttribute("stroke-linejoin", "round");
      svg.appendChild(shape);
    }
    icon.replaceChildren(svg);
    icon.dataset.statusIcon = key;
  }

  function updateStatusElement(element, key, label, status = "") {
    if (!element) return;
    element.dataset.statusKey = key;
    const badge = element.querySelector(".status-badge");
    if (!badge) {
      element.textContent = label;
      return;
    }
    for (const name of STATUS_KEYS) badge.classList.remove(`status-badge--${name}`);
    badge.classList.add(`status-badge--${key}`);
    const icon = badge.querySelector(".status-badge__icon");
    updateStatusIcon(icon, key);
    const labelElement = badge.querySelector(".status-badge__label");
    if (labelElement) labelElement.textContent = label;
    const uploadFailure = element.querySelector('[data-task-item-help="UPLOAD_FAILED"]');
    if (uploadFailure) uploadFailure.hidden = status !== "UPLOAD_FAILED";
    const processingFailure = element.querySelector('[data-task-item-help="PROCESSING_FAILED"]');
    if (processingFailure) processingFailure.hidden = status !== "PROCESSING_FAILED";
    const retryAction = element.querySelector("[data-task-item-action]");
    if (retryAction) retryAction.hidden = status !== "PROCESSING_FAILED";
  }

  function updateNavigationCount() {
    const count = cards.filter((card) => card.isConnected && card.dataset.terminal !== "true").length;
    const summary = document.querySelector("[data-navigation-task-summary]");
    if (!summary) return;
    summary.textContent = count ? `${count} 个任务正在处理。` : "暂无正在处理的任务。";
  }

  function schedule(poller, delay) {
    const timer = window.setTimeout(() => {
      timers.delete(timer);
      poller.poll();
    }, delay);
    timers.add(timer);
  }

  class TaskPoller {
    constructor(card) {
      this.card = card;
      this.etag = "";
      this.items = new Map(
        Array.from(card.querySelectorAll("[data-task-item-id]")).map((element) => [
          element.dataset.taskItemId,
          element,
        ])
      );
    }

    apply(payload) {
      const mainElement = this.card.querySelector("[data-task-main-status]");
      const previousStatusKey = statusKeyFromElement(mainElement);
      const previousStatus = mainElement.textContent.trim();
      const wasTerminal = this.card.dataset.terminal === "true";
      for (const name of ["processing", "completed", "failed"]) {
        const element = this.card.querySelector(`[data-task-count="${name}"]`);
        if (element) element.textContent = String(payload.counts[name]);
      }
      const updatedStatus = mainStatus(payload.counts);
      const updatedStatusKey = mainStatusKey(payload.counts);
      updateStatusElement(mainElement, updatedStatusKey, updatedStatus);
      for (const item of payload.items) {
        const element = this.items.get(item.item_id);
        if (element) updateStatusElement(element, itemStatusKey(item.status), STATUS_COPY[item.status] || "状态更新中", item.status);
        const materialLabel = element?.querySelector("[data-material-label]");
        if (materialLabel) {
          materialLabel.textContent = item.material?.label || "";
          materialLabel.hidden = !materialLabel.textContent;
          const link = element.querySelector("[data-material-link]");
          if (link) link.hidden = !materialLabel.textContent;
        }
      }
      this.card.dataset.terminal = payload.terminal ? "true" : "false";
      if (previousStatusKey ? updatedStatusKey !== previousStatusKey : updatedStatus !== previousStatus) {
        announce(`任务状态已更新：${updatedStatus}。`);
      }
      if (!wasTerminal && payload.terminal) notifyTaskFinished();
      updateNavigationCount();
    }

    async poll() {
      if (!this.card.isConnected || this.card.dataset.terminal === "true") return;
      if (document.hidden) return schedule(this, 3000);
      const controller = new AbortController();
      controllers.add(controller);
      try {
        const headers = { Accept: "application/json" };
        if (this.etag) headers["If-None-Match"] = this.etag;
        const response = await fetch(this.card.dataset.statusUrl, {
          headers,
          credentials: "same-origin",
          signal: controller.signal,
        });
        if (response.status === 404) {
          this.card.remove();
          updateNavigationCount();
          return;
        }
        if (response.status === 304) return schedule(this, 3000);
        if (!response.ok) return schedule(this, 5000);
        this.etag = response.headers.get("ETag") || "";
        const payload = await response.json();
        this.apply(payload);
        if (!payload.terminal) schedule(this, 3000);
      } catch (error) {
        if (error.name !== "AbortError") schedule(this, 5000);
      } finally {
        controllers.delete(controller);
      }
    }
  }

  cards.forEach((card) => {
    if (card.dataset.terminal !== "true") schedule(new TaskPoller(card), 1000);
  });
  window.addEventListener("pagehide", () => {
    timers.forEach((timer) => window.clearTimeout(timer));
    timers.clear();
    controllers.forEach((controller) => controller.abort());
    controllers.clear();
  });
  updateNavigationCount();
})();
