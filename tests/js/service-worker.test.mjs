import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../static/service-worker.js", import.meta.url), "utf8");
const taskStatusSource = readFileSync(new URL("../../static/js/task-status.js", import.meta.url), "utf8");

class FakeClassList {
  constructor(element) {
    this.element = element;
  }

  add(...tokens) {
    const values = new Set(this.element.className.split(/\s+/).filter(Boolean));
    for (const token of tokens) values.add(token);
    this.element.className = [...values].join(" ");
  }

  contains(token) {
    return this.element.className.split(/\s+/).includes(token);
  }

  remove(...tokens) {
    const removed = new Set(tokens);
    this.element.className = this.element.className
      .split(/\s+/)
      .filter((token) => token && !removed.has(token))
      .join(" ");
  }
}

class FakeTaskElement {
  constructor({ className = "", dataset = {}, textContent = "" } = {}) {
    this.className = className;
    this.classList = new FakeClassList(this);
    this.dataset = { ...dataset };
    this.hidden = false;
    this.isConnected = true;
    this.textContent = textContent;
    this.attributes = new Map();
    this.children = [];
    this.queries = new Map();
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith("data-")) {
      this.dataset[name.slice(5).replaceAll(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = String(value);
    }
  }

  getAttribute(name) {
    return this.attributes.get(name) || null;
  }

  replaceChildren(...children) {
    this.children = children;
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  querySelector(selector) {
    return this.queries.get(selector) || null;
  }

  querySelectorAll(selector) {
    return this.queries.get(selector) || [];
  }
}

function statusSlot(label, key) {
  const icon = new FakeTaskElement({ dataset: { statusIcon: key } });
  icon.replaceChildren(new FakeTaskElement({ dataset: { statusIcon: key } }));
  const labelElement = new FakeTaskElement({ textContent: label });
  const badge = new FakeTaskElement({ className: `status-badge status-badge--${key}` });
  badge.queries.set(".status-badge__icon", icon);
  badge.queries.set(".status-badge__label", labelElement);
  const slot = new FakeTaskElement({ dataset: { statusKey: key }, textContent: label });
  slot.queries.set(".status-badge", badge);
  const uploadFailure = new FakeTaskElement();
  uploadFailure.hidden = true;
  const processingFailure = new FakeTaskElement();
  processingFailure.hidden = true;
  const retryAction = new FakeTaskElement();
  retryAction.hidden = true;
  slot.queries.set('[data-task-item-help="UPLOAD_FAILED"]', uploadFailure);
  slot.queries.set('[data-task-item-help="PROCESSING_FAILED"]', processingFailure);
  slot.queries.set("[data-task-item-action]", retryAction);
  return { badge, icon, label: labelElement, processingFailure, retryAction, slot, uploadFailure };
}

test("service worker never creates or writes a cache", () => {
  assert.doesNotMatch(source, /\bcaches\b/);
  assert.doesNotMatch(source, /CacheStorage|cache\.put|respondWith/);
  for (const route of ["/originals/", "/viewer/", "/api/evidence/"]) {
    assert.doesNotMatch(source, new RegExp(route.replaceAll("/", "\\/")));
  }
});

test("push handler accepts only an opaque notification id and exact generic copy", () => {
  assert.match(source, /notification_id/);
  assert.match(source, /资料整理完成/);
  assert.match(source, /你上传的资料已整理完成，点击查看结果。/);
  assert.match(source, /资料整理有未完成项目/);
  assert.match(source, /你上传的资料中有未完成项目，点击查看任务状态。/);
  assert.doesNotMatch(source, /filename|patient|indicator|diagnosis|metric|ocr/i);
});

test("notification click delegates destination resolution to authenticated web", () => {
  assert.match(source, /\/notifications\/.*\/open\//);
  assert.doesNotMatch(source, /records\/|originals\/|viewer\//);
});

test("task polling updates semantic badge hooks and visible labels without replacing task contracts", async () => {
  const main = statusSlot("处理中", "processing");
  const item = statusSlot("处理中", "processing");
  const uploadItem = statusSlot("initial", "processing");
  const initialMainIcon = main.icon.children[0];
  const initialItemIcon = item.icon.children[0];
  item.slot.dataset.taskItemId = "item-1";
  uploadItem.slot.dataset.taskItemId = "item-2";
  const counts = new Map(
    ["processing", "completed", "failed"].map((name) => [
      `[data-task-count="${name}"]`,
      new FakeTaskElement({ textContent: name === "processing" ? "1" : "0" }),
    ]),
  );
  const card = new FakeTaskElement({
    dataset: { statusUrl: "/api/upload-batches/batch-1/status/", terminal: "false" },
  });
  card.queries.set("[data-task-item-id]", [item.slot, uploadItem.slot]);
  card.queries.set("[data-task-main-status]", main.slot);
  for (const [selector, element] of counts) card.queries.set(selector, element);

  const liveRegion = new FakeTaskElement();
  const navigationSummary = new FakeTaskElement();
  const document = {
    hidden: false,
    createElementNS() {
      return new FakeTaskElement();
    },
    querySelector(selector) {
      if (selector === "[data-task-live-region]") return liveRegion;
      if (selector === "[data-navigation-task-summary]") return navigationSummary;
      return null;
    },
    querySelectorAll(selector) {
      return selector === "[data-task-card]" ? [card] : [];
    },
  };
  const scheduled = [];
  const dispatched = [];
  const window = {
    addEventListener() {},
    clearTimeout() {},
    dispatchEvent(event) {
      dispatched.push(event.type);
    },
    requestAnimationFrame(callback) {
      callback();
    },
    setTimeout(callback) {
      scheduled.push(callback);
      return scheduled.length;
    },
  };
  const fetch = async () => ({
    headers: { get: () => '"terminal"' },
    json: async () => ({
      counts: { processing: 0, completed: 0, failed: 2 },
      items: [
        { item_id: "item-1", status: "PROCESSING_FAILED" },
        { item_id: "item-2", status: "UPLOAD_FAILED" },
      ],
      terminal: true,
    }),
    ok: true,
    status: 200,
  });
  class FakeCustomEvent {
    constructor(type) {
      this.type = type;
    }
  }

  vm.runInNewContext(taskStatusSource, {
    AbortController,
    CustomEvent: FakeCustomEvent,
    document,
    fetch,
    Map,
    Set,
    window,
  });
  assert.equal(scheduled.length, 1);
  scheduled.shift()();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(main.slot.dataset.statusKey, "failed");
  assert.equal(main.badge.classList.contains("status-badge--failed"), true);
  assert.equal(main.badge.classList.contains("status-badge--processing"), false);
  assert.equal(main.label.textContent, "处理失败");
  assert.equal(main.icon.dataset.statusIcon, "failed");
  assert.equal(main.icon.children[0].dataset.statusIcon, "failed");
  assert.notEqual(main.icon.children[0], initialMainIcon);
  assert.equal(item.slot.dataset.statusKey, "failed");
  assert.equal(item.badge.classList.contains("status-badge--failed"), true);
  assert.equal(item.badge.classList.contains("status-badge--processing"), false);
  assert.equal(item.label.textContent, "处理失败");
  assert.equal(item.icon.dataset.statusIcon, "failed");
  assert.equal(item.icon.children[0].dataset.statusIcon, "failed");
  assert.notEqual(item.icon.children[0], initialItemIcon);
  assert.equal(item.uploadFailure.hidden, true);
  assert.equal(item.processingFailure.hidden, false);
  assert.equal(item.retryAction.hidden, false);
  assert.equal(uploadItem.slot.dataset.statusKey, "failed");
  assert.equal(uploadItem.badge.classList.contains("status-badge--processing"), false);
  assert.equal(uploadItem.label.textContent, "上传失败");
  assert.equal(uploadItem.uploadFailure.hidden, false);
  assert.equal(uploadItem.processingFailure.hidden, true);
  assert.equal(uploadItem.retryAction.hidden, true);
  assert.equal(counts.get('[data-task-count="failed"]').textContent, "2");
  assert.equal(card.dataset.terminal, "true");
  assert.equal(liveRegion.textContent, "任务状态已更新：处理失败。");
  assert.deepEqual(dispatched, ["phr:task-terminal"]);
});
