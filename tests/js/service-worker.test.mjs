import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../../static/service-worker.js", import.meta.url), "utf8");

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
