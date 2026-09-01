import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../static/js/notifications.js", import.meta.url), "utf8");

class FakeElement {
  constructor() {
    this.attributes = new Map();
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.listeners = new Map();
    this.textContent = "";
  }

  addEventListener(type, listener) {
    this.listeners.set(type, listener);
  }

  append(...children) {
    this.children.push(...children);
  }

  querySelectorAll() {
    return [];
  }

  replaceChildren(...children) {
    this.children = children;
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }
}

test("notification polling keeps the toggle accessible name in sync with unread count", async () => {
  const toggle = new FakeElement();
  const panel = new FakeElement();
  panel.hidden = true;
  const list = new FakeElement();
  const badge = new FakeElement();
  const toast = new FakeElement();
  const elements = new Map([
    ["[data-notification-toggle]", toggle],
    ["[data-notification-panel]", panel],
    ["[data-notification-list]", list],
    ["[data-notification-badge]", badge],
    ["[data-notification-toast]", toast],
  ]);
  const center = new FakeElement();
  center.querySelector = (selector) => elements.get(selector);
  center.contains = () => false;

  const document = {
    hidden: false,
    addEventListener() {},
    createElement: () => new FakeElement(),
    querySelector: (selector) => selector === "[data-notification-center]" ? center : null,
  };
  const window = {
    addEventListener() {},
    clearInterval() {},
    clearTimeout() {},
    setInterval: () => 1,
    setTimeout: () => 1,
  };
  const fetch = async () => ({
    ok: true,
    json: async () => ({ unread_count: 3, notifications: [] }),
  });

  vm.runInNewContext(source, { document, fetch, Set, window });
  toggle.listeners.get("click")();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(badge.textContent, "3");
  assert.equal(badge.hidden, false);
  assert.equal(toggle.attributes.get("aria-label"), "任务通知，3 条未读");
});
