# PHR V1 Archive, Search, Evidence Viewer, and Trends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement PRD P04-P07 so a user can browse by date, search OCR/metadata/lab content, inspect versioned results, return to exact source evidence, and view only safely comparable experimental trends.

**Architecture:** Patient-scoped selectors build archive/search read models from the active parsing version. PostgreSQL `pg_trgm` indexes a normalized document payload; detail and evidence endpoints always resolve ownership in SQL, while trend eligibility is a pure conservative rule over raw result facts.

**Tech Stack:** Python 3.11, Django 5.2, PostgreSQL 18 with `pg_trgm`, PDF.js, ECharts, vanilla JavaScript/CSS, pytest, Playwright

**Spec:** `docs/specs/2026-08-30-phr-v1-system-design.md`

## Global Constraints

- Every archive, search, detail, evidence and trend query is filtered by the authenticated account's patient in SQL.
- Archive ordering uses recognized document date descending, upload time descending within a day, and a final unrecognized-date group ordered by upload time.
- Search covers OCR, filename, document type, institution, date, raw/canonical indicator names, raw results and units.
- Search snippets quote escaped source text only and never generate a medical summary.
- Detail displays raw value precision/unit and report flags without interpreting severity.
- Every structured field links to source page; coordinates highlight only when reliable.
- Object URLs are private, expire after five minutes, and are never cached offline.
- Trend entry requires at least two ordinary numeric values, day-level dates, identical raw units, canonical mapping confidence at least 0.95, and compatible method/institution evidence.
- Trends never convert units, compute percentage change, fill missing points or generate progress/efficacy language.

---

### Task 1: Patient-scoped archive selectors and filter contract

**Files:**
- Create: `apps/documents/archive.py`
- Create: `apps/documents/filters.py`
- Create: `tests/documents/test_archive.py`
- Create: `tests/documents/test_filters.py`

**Interfaces:**
- Consumes: patient, active documents, document type and optional public status.
- Produces: `archive_for_patient(patient, filters, page) -> ArchivePage` grouped into date labels plus “日期未识别”.

- [ ] **Step 1: Write failing literal ordering/filter tests**

Create documents with same date/different upload time, different dates, missing dates, all PRD types and statuses. Assert exact ID order, group labels, single type plus one status composition, 20-item pagination, deleted exclusion and account isolation.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_archive.py tests/documents/test_filters.py -q`

Expected: archive interfaces missing.

- [ ] **Step 3: Implement SQL ordering and typed filters**

Reject unknown filter values rather than silently broadening results. Annotate the selected active parsing metadata without per-card queries. Do not order by report flags, values or field count.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/documents/test_archive.py tests/documents/test_filters.py -q`

Expected: all order, grouping, pagination and isolation tests pass.

### Task 2: Versioned search document and transactional index builder

**Files:**
- Create: `apps/search/__init__.py`
- Create: `apps/search/apps.py`
- Create: `apps/search/models.py`
- Create: `apps/search/normalization.py`
- Create: `apps/search/indexing.py`
- Create: `apps/search/migrations/0001_initial.py`
- Create: `tests/search/test_normalization.py`
- Create: `tests/search/test_indexing.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: document plus one complete parsing version.
- Produces: one active `SearchDocument` with separated normalized metadata, lab and OCR payloads.

- [ ] **Step 1: Write failing normalization tests**

Assert Unicode NFKC, full/half-width, case, whitespace and punctuation handling with hand-derived Chinese/Latin literals. Raw source remains unchanged in OCR/field tables.

- [ ] **Step 2: Verify RED and implement normalization**

Run: `python -m pytest tests/search/test_normalization.py -q`

Expected RED before implementation and GREEN after pure normalizer implementation.

- [ ] **Step 3: Write failing index completeness/version tests**

Create OCR-only and structured documents. Assert searchable payload includes every PRD source, an inactive parsing version cannot replace the index, active-version publication swaps the whole row atomically, repeated build is idempotent and deletion removes visibility.

- [ ] **Step 4: Implement indexed model and GIN migration**

Enable `pg_trgm`, add patient/document/version identifiers, separated payload fields and GIN trigram indexes. SQLite tests exercise builder semantics; PostgreSQL integration tests prove extension/index SQL.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/search/test_normalization.py tests/search/test_indexing.py -q`

Expected: all payload, version and idempotency tests pass.

### Task 3: Ranked search query, snippets, and consistency rebuild

**Files:**
- Create: `apps/search/query.py`
- Create: `apps/search/snippets.py`
- Create: `apps/search/tasks.py`
- Create: `tests/search/test_query.py`
- Create: `tests/search/test_snippets.py`
- Create: `tests/search/test_consistency.py`

**Interfaces:**
- Consumes: patient, visible query up to 100 characters, optional filters and page.
- Produces: ranked `SearchResult` with escaped source snippet, match category and document link.

- [ ] **Step 1: Write failing ranking and scope tests**

Use fixed documents where the same term exactly matches canonical indicator, metadata, raw indicator and OCR. Assert that order, then document date/upload fallback. Assert query trim, blank behavior, 100-character rejection, 20-item pagination and account isolation.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/search/test_query.py -q`

Expected: query service missing.

- [ ] **Step 3: Implement parameterized PostgreSQL query with SQLite fallback for unit tests**

Patient ID is a mandatory query parameter in the innermost relation. Exact normalized matches outrank trigram similarity. A one-character query uses escaped containment/prefix behavior; no raw SQL interpolation is allowed.

- [ ] **Step 4: Implement source-only snippets and consistency task**

Escape markup before highlighting. The daily task compares source/index version/hash, rebuilds missing or stale rows and records counts without payload content.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/search -q`

Expected: all ranking, injection, snippet, tenant and rebuild tests pass.

### Task 4: P04 archive/search interface

**Files:**
- Create: `apps/search/views.py`
- Create: `apps/search/urls.py`
- Create: `templates/documents/archive.html`
- Create: `templates/documents/_archive_card.html`
- Create: `templates/search/_results.html`
- Create: `static/css/archive.css`
- Create: `static/js/archive-search.js`
- Create: `tests/search/test_views.py`
- Create: `tests/accessibility/test_archive_markup.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: Tasks 1 and 3 selectors.
- Produces: `/records/`, search/filter query parameters and progressive-enhancement result fragments.

- [ ] **Step 1: Write failing page/tenant/copy tests**

Assert required search/filter/date groups, PRD empty copy, card fields, no medical conclusion, safe query reflection, login redirects and account isolation.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/search/test_views.py -q`

Expected: route missing.

- [ ] **Step 3: Implement P04 with server-rendered fallback**

Search submits a normal GET form without JavaScript; JavaScript progressively updates results and URL using abortable requests. Filters allow one type plus one status. Loading, empty and error states retain the query.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/search/test_views.py tests/accessibility/test_archive_markup.py -q`

Expected: all route, copy, keyboard and isolation tests pass.

### Task 5: P05 document detail and active-version field presentation

**Files:**
- Create: `apps/documents/detail.py`
- Create: `templates/documents/detail.html`
- Create: `templates/documents/_field_row.html`
- Create: `templates/documents/_ocr_text.html`
- Create: `static/css/detail.css`
- Create: `static/js/detail.js`
- Create: `tests/documents/test_detail.py`
- Create: `tests/accessibility/test_detail_markup.py`
- Modify: `apps/documents/views/`
- Modify: `apps/documents/urls.py`

**Interfaces:**
- Consumes: patient-scoped document UUID and active parsing version.
- Produces: `/records/<uuid>/` with original preview metadata, fields in report order, OCR disclosure and source links.

- [ ] **Step 1: Write failing detail behavior tests**

Assert raw name/value/unit/range/flag preservation, optional canonical name only when accepted, report order, fixed disclaimer, OCR collapse, original button always visible, failed/retry state, feedback/delete entries and another account's UUID returns 404.

- [ ] **Step 2: Verify RED and implement active-version selector**

Run: `python -m pytest tests/documents/test_detail.py -q`

Expected route/selector missing, then GREEN after implementation.

- [ ] **Step 3: Implement responsive two-column P05**

At supported desktop widths render results left and preview right. If preview collapses, keep “查看完整原件”. Field source links carry only field UUID; the server resolves page/evidence after ownership checks.

- [ ] **Step 4: Verify markup GREEN**

Run: `python -m pytest tests/documents/test_detail.py tests/accessibility/test_detail_markup.py -q`

Expected: all presentation, disclaimer, navigation and isolation tests pass.

### Task 6: P06 private original viewer and evidence positioning

**Files:**
- Create: `apps/documents/originals.py`
- Create: `templates/documents/viewer.html`
- Create: `static/vendor/pdfjs/README.md`
- Create: `static/js/image-viewer.js`
- Create: `static/js/pdf-viewer.js`
- Create: `static/css/viewer.css`
- Create: `tests/documents/test_original_access.py`
- Create: `tests/documents/test_evidence_access.py`
- Create: `tests/js/viewer.test.mjs`
- Modify: `apps/documents/views/`
- Modify: `apps/documents/urls.py`

**Interfaces:**
- Consumes: owned document UUID or field UUID.
- Produces: viewer page, permission-checked short-lived original access and normalized evidence JSON.

- [ ] **Step 1: Write failing access tests**

Assert unauthenticated redirect, wrong account 404, deleted document 404, five-minute URL expiry, no object key in HTML/logs, invalid field/document pairing rejection and page-only fallback when no coordinates exist.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_original_access.py tests/documents/test_evidence_access.py -q`

Expected: original/evidence services missing.

- [ ] **Step 3: Implement access grant and normalized evidence API**

Use a same-origin permission endpoint that issues or proxies a short-lived object response with `Cache-Control: private, no-store`. A service worker must not cache viewer/original routes.

- [ ] **Step 4: Implement image/PDF interactions test-first**

Node tests exercise zoom bounds, rotate, page selection, keyboard previous/next, coordinate scaling and detail-scroll restoration as pure functions. UI then wires those functions to image elements/PDF.js.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/documents/test_original_access.py tests/documents/test_evidence_access.py -q`

Run: `node --test tests/js/viewer.test.mjs`

Expected: all permission, expiry, coordinate and interaction tests pass.

### Task 7: P07 conservative trend eligibility and presentation

**Files:**
- Create: `apps/labs/trends.py`
- Create: `apps/labs/views.py`
- Create: `apps/labs/urls.py`
- Create: `templates/labs/trend.html`
- Create: `static/js/trend.js`
- Create: `static/css/trend.css`
- Create: `tests/labs/test_trend_eligibility.py`
- Create: `tests/labs/test_trend_views.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: patient, canonical indicator code and active-version fields.
- Produces: eligible comparison groups and `/trends/<code>/`.

- [ ] **Step 1: Write failing eligibility decision table**

Use literal accepted/rejected cases for count, numeric type, `<`/`>`, day precision, unit identity, mapping confidence 0.95 boundary, method identity, same-institution fallback, conflict grouping and deleted/inactive sources.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/labs/test_trend_eligibility.py -q`

Expected: trend rule missing.

- [ ] **Step 3: Implement pure grouping rule and patient-scoped selector**

Do not normalize/convert units. Sort data points by observation date, then source document upload time. Separate conflict groups and mark them as unconnected series.

- [ ] **Step 4: Implement P07 and source navigation**

Render raw values/units, date, institution, fixed experimental disclaimer and point-to-source links. Chart axes use neutral colors; no report-flag color or change percentage.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/labs/test_trend_eligibility.py tests/labs/test_trend_views.py -q`

Expected: all eligibility, presentation and isolation tests pass.

### Task 8: Archive/search/viewer/trend acceptance and security evidence

**Files:**
- Create: `tests/acceptance/test_ac08_ac13.py`
- Create: `tests/security/test_read_path_isolation.py`
- Create: `tests/privacy/test_search_and_viewer_logs.py`
- Create: `docs/verification/ac08-ac13.md`

**Interfaces:**
- Consumes: Tasks 1–7 and prior upload/processing plans.
- Produces: repeatable PRD AC-08 through AC-13 plus read-path AC-18/AC-19 evidence.

- [ ] **Step 1: Write integrated failing scenarios**

Cover OCR-only search, raw/canonical/value search, field-to-page/highlight, no-coordinate page fallback, raw value/unit preservation, state results not zero, eligible/ineligible trends and every account-B read path attempted by account A.

- [ ] **Step 2: Verify RED and close integration gaps**

Run: `python -m pytest tests/acceptance/test_ac08_ac13.py tests/security/test_read_path_isolation.py tests/privacy/test_search_and_viewer_logs.py -q`

Expected before final wiring: at least one genuine behavior failure.

- [ ] **Step 3: Run full suites and UI browser flow**

Run: `python -m pytest -q`

Run: `node --test tests/js/*.test.mjs`

Run the Playwright archive-to-evidence flow at 1280×720 and 1440×900 in installed Chromium; Safari remains a separate release-environment check.

- [ ] **Step 4: Record evidence**

Write commands, versions, counts, screenshots/trace paths and skipped Safari evidence to `docs/verification/ac08-ac13.md`. Any skipped supported-browser check remains a release blocker.

## Plan self-review

- Archive and search rules are deterministic, patient scoped and paginated.
- Search covers OCR-only documents and never creates medical summaries.
- Detail/viewer preserve raw evidence and enforce ownership before object access.
- Trend eligibility exactly reflects PRD constraints and is deliberately conservative.
- Feedback, notifications, deletion, account settings, analytics and production operations remain in the final product plan.
