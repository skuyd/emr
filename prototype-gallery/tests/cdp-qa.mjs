import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const port = Number(process.argv[2] || 9333);
const outputDir = resolve('prototype-gallery/screenshots');
mkdirSync(outputDir, { recursive: true });

const pages = [
  { name: 'style-a-mobile-exact', file: 'style-a-warm.html', bottomNav: '.nav' },
  { name: 'style-b-mobile-exact', file: 'style-b-clinical.html', bottomNav: '.sidebar' },
  { name: 'style-c-mobile-exact', file: 'style-c-timeline.html', bottomNav: '.nav' },
  { name: 'style-d-mobile-exact', file: 'style-d-companion.html', bottomNav: '.bottom-nav' },
];

function wait(milliseconds) {
  return new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));
}

async function openProtocol(webSocketDebuggerUrl) {
  const socket = new WebSocket(webSocketDebuggerUrl);
  const pending = new Map();
  const exceptions = [];
  let commandId = 0;

  await new Promise((resolveOpen, rejectOpen) => {
    socket.onopen = resolveOpen;
    socket.onerror = rejectOpen;
  });

  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolveCommand, rejectCommand } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) rejectCommand(new Error(message.error.message));
      else resolveCommand(message.result);
    }
    if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
  };

  function send(method, params = {}) {
    const id = ++commandId;
    socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolveCommand, rejectCommand) => pending.set(id, { resolveCommand, rejectCommand }));
  }

  return { send, exceptions, close: () => socket.close() };
}

for (const page of pages) {
  const targetResponse = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
  assert.equal(targetResponse.ok, true, `cannot create Chrome target for ${page.name}`);
  const target = await targetResponse.json();
  const protocol = await openProtocol(target.webSocketDebuggerUrl);
  await protocol.send('Page.enable');
  await protocol.send('Runtime.enable');
  await protocol.send('Network.enable');
  await protocol.send('Network.setCacheDisabled', { cacheDisabled: true });
  await protocol.send('Emulation.setDeviceMetricsOverride', {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
    screenWidth: 390,
    screenHeight: 844,
  });
  await protocol.send('Page.navigate', { url: `http://127.0.0.1:4173/variants/${page.file}?qa=${Date.now()}` });
  await wait(700);

  const metricsResult = await protocol.send('Runtime.evaluate', {
    expression: `(() => {
      const root = document.documentElement;
      const body = document.body;
      const nav = document.querySelector(${JSON.stringify(page.bottomNav)});
      const rect = nav?.getBoundingClientRect();
      const ancestors = [];
      for (let current = nav?.parentElement; current; current = current.parentElement) {
        const style = getComputedStyle(current);
        ancestors.push({
          tag: current.tagName,
          className: current.className,
          position: style.position,
          transform: style.transform,
          filter: style.filter,
          backdropFilter: style.backdropFilter,
          contain: style.contain,
          willChange: style.willChange,
        });
      }
      return {
        innerWidth,
        innerHeight,
        clientWidth: root.clientWidth,
        scrollWidth: Math.max(root.scrollWidth, body.scrollWidth),
        navTop: rect?.top ?? null,
        navBottom: rect?.bottom ?? null,
        navPosition: nav ? getComputedStyle(nav).position : null,
        navBottomRule: nav ? getComputedStyle(nav).bottom : null,
        ancestors,
      };
    })()`,
    returnByValue: true,
  });
  const metrics = metricsResult.result.value;

  assert.equal(metrics.innerWidth, 390, `${page.name} must render at an exact 390px viewport`);
  assert.ok(metrics.scrollWidth <= metrics.clientWidth + 1, `${page.name} overflows horizontally: ${JSON.stringify(metrics)}`);
  assert.ok(metrics.navTop >= 730, `${page.name} mobile navigation must stay near the viewport bottom: ${JSON.stringify(metrics)}`);

  const capture = await protocol.send('Page.captureScreenshot', { format: 'png', fromSurface: true });
  writeFileSync(resolve(outputDir, `${page.name}.png`), Buffer.from(capture.data, 'base64'));

  await protocol.send('Runtime.evaluate', {
    expression: `document.querySelector('[data-nav="records"]')?.click()`,
  });
  await wait(120);
  const recordsResult = await protocol.send('Runtime.evaluate', {
    expression: `(() => {
      const root = document.documentElement;
      const rows = [...document.querySelectorAll('[data-view="records"] [data-record]')];
      return {
        activeView: [...document.querySelectorAll('[data-view]')].find((view) => !view.hidden)?.dataset.view,
        visibleRecords: rows.filter((row) => !row.hidden).length,
        scrollWidth: Math.max(root.scrollWidth, document.body.scrollWidth),
        clientWidth: root.clientWidth,
      };
    })()`,
    returnByValue: true,
  });
  const records = recordsResult.result.value;
  assert.equal(records.activeView, 'records', `${page.name} records navigation must open the archive view`);
  assert.equal(records.visibleRecords, 4, `${page.name} records view must show all four sample states`);
  assert.ok(records.scrollWidth <= records.clientWidth + 1, `${page.name} records view overflows horizontally: ${JSON.stringify(records)}`);

  const searchResult = await protocol.send('Runtime.evaluate', {
    expression: `(() => {
      const input = document.querySelector('[data-view="records"] [data-search]');
      input.value = '标志物';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      const rows = [...document.querySelectorAll('[data-view="records"] [data-record]')];
      return rows.filter((row) => !row.hidden).length;
    })()`,
    returnByValue: true,
  });
  assert.equal(searchResult.result.value, 1, `${page.name} search must narrow the archive to one matching record`);

  await protocol.send('Runtime.evaluate', {
    expression: `(() => {
      const row = [...document.querySelectorAll('[data-view="records"] [data-record]')].find((item) => !item.hidden);
      const opener = row?.matches('[data-open-detail]') ? row : row?.querySelector('[data-open-detail]');
      opener?.click();
    })()`,
  });
  await wait(120);
  const detailResult = await protocol.send('Runtime.evaluate', {
    expression: `(() => {
      const root = document.documentElement;
      const detail = document.querySelector('[data-view="detail"]');
      return {
        activeView: [...document.querySelectorAll('[data-view]')].find((view) => !view.hidden)?.dataset.view,
        hasRecognitionWarning: detail?.textContent.includes('自动识别结果可能不准确') ?? false,
        hasOriginalAuthority: detail?.textContent.includes('请以原始报告为准') ?? false,
        scrollWidth: Math.max(root.scrollWidth, document.body.scrollWidth),
        clientWidth: root.clientWidth,
      };
    })()`,
    returnByValue: true,
  });
  const detail = detailResult.result.value;
  assert.equal(detail.activeView, 'detail', `${page.name} record action must open the detail view`);
  assert.equal(detail.hasRecognitionWarning, true, `${page.name} detail view must disclose recognition uncertainty`);
  assert.equal(detail.hasOriginalAuthority, true, `${page.name} detail view must identify the original report as authoritative`);
  assert.ok(detail.scrollWidth <= detail.clientWidth + 1, `${page.name} detail view overflows horizontally: ${JSON.stringify(detail)}`);
  assert.deepEqual(protocol.exceptions, [], `${page.name} raised browser exceptions`);

  protocol.close();
  await fetch(`http://127.0.0.1:${port}/json/close/${target.id}`);
  console.log(`${page.name}: home=${JSON.stringify(metrics)} records=${JSON.stringify(records)} detail=${JSON.stringify(detail)}`);
}

const galleryTargetResponse = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' });
assert.equal(galleryTargetResponse.ok, true, 'cannot create Chrome target for the comparison gallery');
const galleryTarget = await galleryTargetResponse.json();
const galleryProtocol = await openProtocol(galleryTarget.webSocketDebuggerUrl);
await galleryProtocol.send('Page.enable');
await galleryProtocol.send('Runtime.enable');
await galleryProtocol.send('Network.enable');
await galleryProtocol.send('Network.setCacheDisabled', { cacheDisabled: true });
await galleryProtocol.send('Emulation.setDeviceMetricsOverride', {
  width: 1280,
  height: 900,
  deviceScaleFactor: 1,
  mobile: false,
  screenWidth: 1280,
  screenHeight: 900,
});
await galleryProtocol.send('Page.navigate', { url: `http://127.0.0.1:4173/index.html?qa=${Date.now()}` });
await wait(900);
const galleryResult = await galleryProtocol.send('Runtime.evaluate', {
  expression: `(() => {
    localStorage.removeItem('phr-style-choice');
    document.querySelector('[data-select="c"]')?.click();
    return {
      cardCount: document.querySelectorAll('[data-variant]').length,
      iframeCount: document.querySelectorAll('.frame-wrap iframe').length,
      selectedCount: document.querySelectorAll('[data-variant].is-selected').length,
      selectedId: localStorage.getItem('phr-style-choice'),
      choiceText: document.querySelector('#choiceText')?.textContent,
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth),
      clientWidth: document.documentElement.clientWidth,
    };
  })()`,
  returnByValue: true,
});
const gallery = galleryResult.result.value;
assert.equal(gallery.cardCount, 4, 'comparison gallery must show four design directions');
assert.equal(gallery.iframeCount, 4, 'comparison gallery must embed four interactive previews');
assert.equal(gallery.selectedCount, 1, 'comparison gallery must visibly mark one selected direction');
assert.equal(gallery.selectedId, 'c', 'comparison gallery must persist the selected direction');
assert.ok(gallery.choiceText?.startsWith('C'), `comparison gallery must announce the selected direction: ${JSON.stringify(gallery)}`);
assert.ok(gallery.scrollWidth <= gallery.clientWidth + 1, `comparison gallery overflows horizontally: ${JSON.stringify(gallery)}`);
assert.deepEqual(galleryProtocol.exceptions, [], 'comparison gallery raised browser exceptions');

galleryProtocol.exceptions.length = 0;
const directFileUrl = `${pathToFileURL(resolve('prototype-gallery/index.html')).href}?qa=${Date.now()}`;
await galleryProtocol.send('Page.navigate', { url: directFileUrl });
await wait(900);
const directFileResult = await galleryProtocol.send('Runtime.evaluate', {
  expression: `(() => {
    localStorage.removeItem('phr-style-choice');
    document.querySelector('[data-select="d"]')?.click();
    return {
      cardCount: document.querySelectorAll('[data-variant]').length,
      iframeCount: document.querySelectorAll('.frame-wrap iframe').length,
      selectedCount: document.querySelectorAll('[data-variant].is-selected').length,
      selectedId: localStorage.getItem('phr-style-choice'),
      choiceText: document.querySelector('#choiceText')?.textContent,
    };
  })()`,
  returnByValue: true,
});
const directFile = directFileResult.result.value;
assert.equal(directFile.cardCount, 4, 'direct file gallery must show four design directions');
assert.equal(directFile.iframeCount, 4, 'direct file gallery must embed four interactive previews');
assert.equal(directFile.selectedCount, 1, 'direct file gallery must visibly mark one selected direction');
assert.equal(directFile.selectedId, 'd', 'direct file gallery must persist the selected direction');
assert.ok(directFile.choiceText?.startsWith('D'), `direct file gallery must announce the selected direction: ${JSON.stringify(directFile)}`);
assert.deepEqual(galleryProtocol.exceptions, [], 'direct file gallery raised browser exceptions');
galleryProtocol.close();
await fetch(`http://127.0.0.1:${port}/json/close/${galleryTarget.id}`);
console.log(`comparison-gallery: ${JSON.stringify(gallery)}`);
console.log(`direct-file-gallery: ${JSON.stringify(directFile)}`);
