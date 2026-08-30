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
      const previousStatus = this.card.querySelector("[data-task-main-status]").textContent;
      const wasTerminal = this.card.dataset.terminal === "true";
      for (const name of ["processing", "completed", "failed"]) {
        const element = this.card.querySelector(`[data-task-count="${name}"]`);
        if (element) element.textContent = String(payload.counts[name]);
      }
      const updatedStatus = mainStatus(payload.counts);
      this.card.querySelector("[data-task-main-status]").textContent = updatedStatus;
      for (const item of payload.items) {
        const element = this.items.get(item.item_id);
        if (element) element.textContent = STATUS_COPY[item.status] || "状态更新中";
      }
      this.card.dataset.terminal = payload.terminal ? "true" : "false";
      if (updatedStatus !== previousStatus) announce(`任务状态已更新：${updatedStatus}。`);
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
