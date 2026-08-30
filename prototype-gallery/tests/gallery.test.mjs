import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const galleryCorePath = resolve(here, '../gallery-core.js');
const prototypeCorePath = resolve(here, '../prototype-core.js');
const require = createRequire(import.meta.url);

test('gallery core is available to the comparison page', () => {
  assert.equal(existsSync(galleryCorePath), true, 'gallery-core.js must exist');
});

test('prototype core is available to every design direction', () => {
  assert.equal(existsSync(prototypeCorePath), true, 'prototype-core.js must exist');
});

if (existsSync(galleryCorePath)) {
  const gallery = require(galleryCorePath);

  test('gallery offers four named and materially distinct directions', () => {
    assert.deepEqual(
      gallery.variants.map((variant) => variant.name),
      ['暖笺', '明晰', '经纬', '随身'],
    );
    assert.equal(new Set(gallery.variants.map((variant) => variant.font)).size, 4);
    assert.equal(new Set(gallery.variants.map((variant) => variant.palette)).size, 4);
    assert.equal(new Set(gallery.variants.map((variant) => variant.layout)).size, 4);
  });

  test('choosing a valid direction persists and returns the full choice', () => {
    const writes = [];
    const storage = { setItem: (key, value) => writes.push([key, value]) };
    const selected = gallery.chooseVariant('c', storage);

    assert.equal(selected.name, '经纬');
    assert.deepEqual(writes, [['phr-style-choice', 'c']]);
  });

  test('choosing an unknown direction cannot corrupt the saved preference', () => {
    const storage = { setItem: () => assert.fail('invalid choice must not be persisted') };
    assert.throws(() => gallery.chooseVariant('z', storage), /未知的设计方向/);
  });
}

if (existsSync(prototypeCorePath)) {
  const prototype = require(prototypeCorePath);
  const documents = [
    { title: '血常规检验报告', institution: '市第一医院', date: '2026-08-22', status: '已整理' },
    { title: '肿瘤标志物', institution: '省肿瘤医院', date: '2026-08-10', status: '已整理' },
    { title: '腹部增强 CT 报告', institution: '市第一医院', date: '2026-07-28', status: '仅原件' },
  ];

  test('search finds documents by title, hospital, date, or status', () => {
    assert.deepEqual(prototype.filterDocuments(documents, '肿瘤').map((item) => item.title), ['肿瘤标志物']);
    assert.equal(prototype.filterDocuments(documents, '市第一医院').length, 2);
    assert.equal(prototype.filterDocuments(documents, '2026-08').length, 2);
    assert.equal(prototype.filterDocuments(documents, '仅原件').length, 1);
  });

  test('blank search preserves the full record list without mutating it', () => {
    const result = prototype.filterDocuments(documents, '   ');
    assert.deepEqual(result, documents);
    assert.notEqual(result, documents);
  });

  test('view changes allow only the three core pages', () => {
    assert.equal(prototype.nextView('home', 'records'), 'records');
    assert.equal(prototype.nextView('records', 'detail'), 'detail');
    assert.equal(prototype.nextView('detail', 'unknown'), 'detail');
  });

  test('direct preview links can open a valid core page safely', () => {
    assert.equal(prototype.viewFromSearch('?view=records'), 'records');
    assert.equal(prototype.viewFromSearch('?view=detail&source=gallery'), 'detail');
    assert.equal(prototype.viewFromSearch('?view=unknown'), 'home');
    assert.equal(prototype.viewFromSearch(''), 'home');
  });

  test('status summary keeps all four user-visible processing states', () => {
    const summary = prototype.summarizeStatuses([
      { status: '处理中' },
      { status: '已整理' },
      { status: '已整理' },
      { status: '仅原件' },
      { status: '处理失败' },
    ]);
    assert.deepEqual(summary, { 处理中: 1, 已整理: 2, 仅原件: 1, 处理失败: 1 });
  });
}
