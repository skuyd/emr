# PHR Visual Style Gallery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained HTML gallery containing four materially different visual directions for the three core PHR screens.

**Architecture:** A static comparison shell embeds four standalone prototypes. Each prototype owns its visual tokens and uses a small shared interaction contract: `[data-nav]` changes views, `[data-open-detail]` opens the detail view, and search inputs filter document rows. Node's built-in test runner verifies structure, copy boundaries, navigation hooks, and offline portability.

**Tech Stack:** HTML5, CSS3, vanilla JavaScript, Node.js built-in `node:test`

**Spec:** `docs/specs/2026-08-29-phr-visual-style-gallery-design.md`

## Global Constraints

- Product is a patient/family PHR, not a hospital EMR.
- No diagnosis, treatment advice, efficacy judgment, or real patient data.
- Original documents remain the primary evidence on every detail screen.
- Four variants must differ in font family, palette, layout, and interaction emphasis.
- No external runtime dependencies, remote fonts, remote images, or build step.
- This workspace is not a Git repository, so commit steps are intentionally omitted.

---

### Task 1: Executable gallery contract

**Files:**
- Create: `prototype-gallery/tests/gallery.test.mjs`

**Interfaces:**
- Consumes: Files under `prototype-gallery/`.
- Produces: A `node:test` suite that fails until the gallery and four variants exist.

- [ ] **Step 1: Write the failing structural tests**

```js
test('comparison board exposes four named variants', () => {
  const html = read('index.html');
  for (const name of ['暖笺', '明晰', '经纬', '随身']) assert.match(html, new RegExp(name));
});

test('each prototype contains the three core views and trust copy', () => {
  for (const file of variants) {
    const html = read(`variants/${file}`);
    for (const view of ['home', 'records', 'detail']) assert.match(html, new RegExp(`data-view=\\"${view}\\"`));
    assert.match(html, /请以原始报告为准/);
  }
});
```

- [ ] **Step 2: Run tests and verify RED**

Run: `node --test prototype-gallery/tests/gallery.test.mjs`

Expected: FAIL because `prototype-gallery/index.html` and variant files do not exist.

### Task 2: Comparison board

**Files:**
- Create: `prototype-gallery/index.html`
- Create: `prototype-gallery/gallery.css`
- Create: `prototype-gallery/gallery.js`

**Interfaces:**
- Consumes: Four relative iframe URLs under `variants/`.
- Produces: `selectVariant(id)`, persisted `localStorage` choice, full-screen prototype links.

- [ ] **Step 1: Implement the smallest gallery that satisfies the board test**

```html
<article class="variant-card" data-variant="a">
  <iframe src="variants/style-a-warm.html" title="A 暖笺预览"></iframe>
  <button type="button" data-select="a">选择 A</button>
</article>
```

- [ ] **Step 2: Add responsive comparison layout and visible selection state**

Use a two-column grid above 1100px, one column below it, and a sticky summary bar showing the locally selected direction.

- [ ] **Step 3: Run tests and keep the board contract green**

Run: `node --test prototype-gallery/tests/gallery.test.mjs`

### Task 3: Four standalone page systems

**Files:**
- Create: `prototype-gallery/variants/style-a-warm.html`
- Create: `prototype-gallery/variants/style-b-clinical.html`
- Create: `prototype-gallery/variants/style-c-timeline.html`
- Create: `prototype-gallery/variants/style-d-companion.html`

**Interfaces:**
- Consumes: Shared semantic data attributes defined in the architecture.
- Produces: Four offline-capable prototypes with `home`, `records`, and `detail` views.

- [ ] **Step 1: Implement A with editorial cards and a warm upload-first home**

Use `Georgia, 'Noto Serif SC', serif` for display text, cream `#F7F1E7`, terracotta `#B85C3F`, and sage `#47685B`. The home view leads with a single large upload action.

- [ ] **Step 2: Implement B with a dense left rail and global retrieval**

Use `'Segoe UI', Arial, sans-serif`, white `#FFFFFF`, cobalt `#155EEF`, and cyan `#06AED4`. The records view uses table semantics and persistent search.

- [ ] **Step 3: Implement C with a dark chronological canvas**

Use `'Trebuchet MS', sans-serif`, navy `#0D1B2A`, mint `#63E6BE`, and lime `#C6FF4A`. Dates and timeline nodes lead the hierarchy while evidence actions remain visible.

- [ ] **Step 4: Implement D with a mobile-centered task flow**

Use `'Arial Rounded MT Bold', 'Microsoft YaHei', sans-serif`, mist `#F5F1FF`, violet `#6C5CE7`, and coral `#FF8066`. Use a 720px focused column, bottom navigation, and at least 44px touch targets.

- [ ] **Step 5: Add navigation, search filtering, detail opening, and feedback acknowledgement**

```js
document.querySelectorAll('[data-nav]').forEach(button => {
  button.addEventListener('click', () => showView(button.dataset.nav));
});
document.querySelectorAll('[data-open-detail]').forEach(button => {
  button.addEventListener('click', () => showView('detail'));
});
```

- [ ] **Step 6: Run tests and verify GREEN**

Run: `node --test prototype-gallery/tests/gallery.test.mjs`

Expected: all structure, safety-copy, offline, and interaction-hook tests pass.

### Task 4: Visual and responsive verification

**Files:**
- Create: `prototype-gallery/screenshots/style-a.png`
- Create: `prototype-gallery/screenshots/style-b.png`
- Create: `prototype-gallery/screenshots/style-c.png`
- Create: `prototype-gallery/screenshots/style-d.png`

**Interfaces:**
- Consumes: Static HTML prototypes through a local HTTP server.
- Produces: Review evidence at 1440×1000 and a responsive overflow check at 390×844.

- [ ] **Step 1: Serve the gallery locally**

Run: `python -m http.server 4173 --directory prototype-gallery`

- [ ] **Step 2: Capture each prototype at desktop size with an installed Chromium browser**

Run the installed browser in headless screenshot mode against `http://127.0.0.1:4173/variants/<file>`.

- [ ] **Step 3: Check screenshots for clipping, weak hierarchy, and accidental convergence**

Reject any pair that shares the same font character, palette temperature, and layout rhythm; adjust the weaker direction and recapture.

- [ ] **Step 4: Run final automated verification**

Run: `node --test prototype-gallery/tests/gallery.test.mjs`

Expected: PASS with no warnings or skipped assertions.

