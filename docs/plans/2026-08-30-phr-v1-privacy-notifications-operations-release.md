# PHR V1 Privacy, Notifications, Operations, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete P08, feedback, safe notifications/analytics, document and account deletion, operations/monitoring, security hardening, browser/performance QA, and the full PRD release evidence.

**Architecture:** Privacy-sensitive actions are explicit application services with audit-safe metadata and asynchronous deletion jobs. Notifications and analytics use strict allowlist serializers so medical content cannot enter external channels; operations expose only least-privilege controls, while a requirement trace matrix gates release on every MUST, AC-00–AC-22 and scenario 1–26.

**Tech Stack:** Python 3.11, Django 5.2, Celery 5.6, Redis, PostgreSQL 18, Web Push/VAPID, Prometheus-compatible metrics, pytest, Playwright, Lighthouse/axe-compatible accessibility checks

**Spec:** `docs/specs/2026-08-30-phr-v1-system-design.md`

## Global Constraints

- Feedback never requires users to provide corrected medical content.
- Browser notifications require explicit user action and contain no indicator, value, diagnosis, full patient identity or medical text.
- Analytics and logs reject unknown attributes and never contain raw medical text, values, filename, query text, phone or patient display name.
- Single-document deletion immediately removes every user/query access and asynchronously removes original, pages, thumbnails, OCR, fields and index.
- Account deletion immediately disables access, revokes every session/push subscription and starts full deletion; V1 offers no restore.
- Backups retain at most 30 days and restore must replay deletion tombstones.
- Internal staff cannot browse originals/OCR by default; any support access is reason-bound, time-limited and audited.
- Core flows support keyboard operation, visible focus and non-color-only status.
- Supported desktop browsers and viewports follow PRD 11.5; skipped Safari evidence remains a release blocker.
- Release requires all MUST functions and AC-00–AC-22; SHOULD items are implemented rather than hidden behind scope trimming.

---

### Task 1: Strict analytics schema and privacy-safe audit events

**Files:**
- Create: `apps/analytics/__init__.py`
- Create: `apps/analytics/apps.py`
- Create: `apps/analytics/models.py`
- Create: `apps/analytics/events.py`
- Create: `apps/analytics/migrations/0001_initial.py`
- Create: `apps/operations/__init__.py`
- Create: `apps/operations/apps.py`
- Create: `apps/operations/models.py`
- Create: `apps/operations/audit.py`
- Create: `apps/operations/migrations/0001_initial.py`
- Create: `tests/analytics/test_event_schema.py`
- Create: `tests/privacy/test_event_payloads.py`
- Create: `tests/operations/test_audit.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: named PRD events plus explicit properties and security actions plus opaque identifiers.
- Produces: `record_product_event(name, properties)`, `record_audit_event(actor, action, target_id, result, reason=None)`.

- [ ] **Step 1: Write failing allowlist/privacy tests**

For every PRD 10.2 event, define literal allowed keys/types. Assert unknown event/key, raw query, filename, OCR, phone, patient name, lab name/value and nested arbitrary data are rejected before persistence/export. Audit events accept UUIDs, action/result and reason code but not medical payload.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/analytics/test_event_schema.py tests/privacy/test_event_payloads.py tests/operations/test_audit.py -q`

Expected: modules missing.

- [ ] **Step 3: Implement closed serializers and append-only records**

Use per-event dataclasses/schema definitions, size bounds and enum values. Hash account identifier with a dedicated analytics key. Audit records are append-only through application services; admin cannot edit them.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/analytics tests/privacy/test_event_payloads.py tests/operations/test_audit.py -q`

Expected: every allowed and forbidden payload test passes.

### Task 2: In-app notifications and opt-in Web Push

**Files:**
- Create: `apps/notifications/__init__.py`
- Create: `apps/notifications/apps.py`
- Create: `apps/notifications/models.py`
- Create: `apps/notifications/services.py`
- Create: `apps/notifications/webpush.py`
- Create: `apps/notifications/views.py`
- Create: `apps/notifications/urls.py`
- Create: `apps/notifications/migrations/0001_initial.py`
- Create: `static/js/notifications.js`
- Create: `static/service-worker.js`
- Create: `tests/notifications/test_services.py`
- Create: `tests/notifications/test_webpush.py`
- Create: `tests/js/service-worker.test.mjs`
- Modify: `config/urls.py`
- Modify: `templates/base_app.html`

**Interfaces:**
- Consumes: completed/failed batch and an explicit subscription action.
- Produces: in-app `Notification`, unread badge, private Web Push subscription and generic task notification.

- [ ] **Step 1: Write failing trigger/content tests**

Assert one notification per finished batch, failure variant, idempotent duplicate completion, account scope and exact generic copy. Feed indicator/value/filename/patient text into source objects and prove none appears in notification payload.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/notifications/test_services.py -q`

Expected: notification service missing.

- [ ] **Step 3: Implement station notification and permission endpoints**

Subscription creation is POST/CSRF/auth protected and only follows a user click. Store endpoint and keys encrypted; rejection state stays client-side and prevents repeated prompts. Disable/revoke deletes the subscription.

- [ ] **Step 4: Implement service worker with cache exclusions**

Push data contains only notification UUID and generic title/body. On click, open/focus the app and let authenticated Web resolve the destination. Tests prove `/originals/`, `/viewer/`, `/api/evidence/` and medical responses are never cached.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/notifications -q`

Run: `node --test tests/js/service-worker.test.mjs`

Expected: all trigger, privacy, subscription and cache tests pass.

### Task 3: Feedback and P08 account/settings page

**Files:**
- Create: `apps/feedback/__init__.py`
- Create: `apps/feedback/apps.py`
- Create: `apps/feedback/models.py`
- Create: `apps/feedback/services.py`
- Create: `apps/feedback/views.py`
- Create: `apps/feedback/urls.py`
- Create: `apps/feedback/migrations/0001_initial.py`
- Create: `apps/patients/settings.py`
- Create: `templates/patients/settings.html`
- Create: `templates/feedback/form.html`
- Create: `static/css/settings.css`
- Create: `tests/feedback/test_feedback.py`
- Create: `tests/patients/test_settings.py`
- Create: `tests/accessibility/test_settings_markup.py`
- Modify: `config/urls.py`

**Interfaces:**
- Consumes: current patient, optional owned document/field, feedback category and optional bounded free text.
- Produces: inaccurate-feedback event, product-opinion event, display-name change, quota/privacy/notification controls.

- [ ] **Step 1: Write failing feedback tests**

Assert one-click inaccurate feedback succeeds with no corrected value, another account's document/field returns 404, free text is optional/500 chars, and analytics record contains category but not the text.

- [ ] **Step 2: Verify RED and implement feedback**

Run: `python -m pytest tests/feedback/test_feedback.py -q`

Expected route/service missing, then GREEN.

- [ ] **Step 3: Write failing settings/name/quota tests**

Assert P08 shows and changes 1–20-character patient name, current quota usage/limits, privacy policy versions, feedback entry, notification toggle and deletion action; no phone appears as a patient field.

- [ ] **Step 4: Implement P08 and verify GREEN**

Run: `python -m pytest tests/patients/test_settings.py tests/accessibility/test_settings_markup.py -q`

Expected: all validation, copy, tenant and markup tests pass.

### Task 4: Single-document deletion with immediate invisibility

**Files:**
- Create: `apps/documents/deletion.py`
- Create: `apps/documents/deletion_tasks.py`
- Create: `templates/documents/delete_confirm.html`
- Create: `tests/documents/test_document_deletion.py`
- Create: `tests/documents/test_deletion_tasks.py`
- Modify: `apps/documents/models.py`
- Modify: `apps/documents/views/`
- Modify: `apps/documents/urls.py`

**Interfaces:**
- Consumes: authenticated patient and owned document UUID plus confirmation POST.
- Produces: tombstone/`DeletionJob`, immediate query denial and idempotent physical purge.

- [ ] **Step 1: Write failing immediate-visibility matrix**

After delete confirmation, assert document disappears from home/archive/search/detail/original URL/trend and cannot be retried. Account B cannot trigger deletion. Repeated delete is safe and does not expose prior existence.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_document_deletion.py -q`

Expected: deletion service missing.

- [ ] **Step 3: Implement transactional tombstone and on-commit purge enqueue**

Lock owned document, set deleted timestamp/status, invalidate active object grants and remove active search row in one transaction. Queue purge only after commit and record audit without filename/content.

- [ ] **Step 4: Write failing purge-layer tests and implement idempotent cleanup**

Use fakes for originals/thumbnails/page renders and verify OCR, fields, versions, pages, index and objects are all deleted; a transient object-store failure leaves the job retryable and user data invisible.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/documents/test_document_deletion.py tests/documents/test_deletion_tasks.py -q`

Expected: all visibility, isolation, idempotency and retry tests pass.

### Task 5: Account deletion, session revocation, and backup tombstones

**Files:**
- Create: `apps/accounts/deletion.py`
- Create: `apps/accounts/session_registry.py`
- Create: `apps/operations/deletion_tasks.py`
- Create: `apps/operations/tombstones.py`
- Create: `templates/accounts/delete_confirm.html`
- Create: `tests/accounts/test_account_deletion.py`
- Create: `tests/operations/test_account_purge.py`
- Create: `tests/operations/test_restore_tombstones.py`
- Modify: `apps/accounts/models.py`
- Modify: `apps/accounts/views.py`
- Modify: `apps/accounts/urls.py`

**Interfaces:**
- Consumes: authenticated account and explicit second confirmation.
- Produces: disabled account, revoked sessions/subscriptions, top-level deletion job and durable restore tombstone.

- [ ] **Step 1: Write failing immediate-revocation tests**

Create two sessions and a push subscription. Confirm deletion from one and assert both sessions, all app routes, object grants and push are denied immediately; OTP login cannot reactivate the deleting account.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/accounts/test_account_deletion.py -q`

Expected: deletion/session registry missing.

- [ ] **Step 3: Implement disable/revoke and child purge fan-out**

Set deleting state under row lock, rotate session revocation generation, delete push subscriptions, hide patient/documents and create per-document purge jobs. Final account identifiers are deleted after children finish, retaining only irreversible audit hash and tombstone.

- [ ] **Step 4: Implement restore tombstone replay**

Given a restored backup snapshot plus a newer tombstone log, replay must re-delete each account/document before opening traffic. Tests prove a deleted fixture never reappears and tombstone application is idempotent.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/accounts/test_account_deletion.py tests/operations/test_account_purge.py tests/operations/test_restore_tombstones.py -q`

Expected: all revocation, purge ordering and restore tests pass.

### Task 6: Least-privilege operations, dictionary/version controls, and monitoring

**Files:**
- Create: `apps/operations/admin.py`
- Create: `apps/operations/permissions.py`
- Create: `apps/operations/services.py`
- Create: `apps/operations/metrics.py`
- Create: `apps/operations/health.py`
- Create: `apps/operations/alerts.py`
- Create: `tests/operations/test_permissions.py`
- Create: `tests/operations/test_services.py`
- Create: `tests/operations/test_metrics_privacy.py`
- Create: `tests/operations/test_health.py`
- Modify: `config/urls.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: staff roles and opaque document/run/dictionary IDs.
- Produces: restricted task requeue, parse-version switch, dictionary publish, quota change, deletion status, health/metrics endpoints and alerts.

- [ ] **Step 1: Write failing role/action matrix**

Define roles `support`, `processor_operator`, `dictionary_manager`, `privacy_admin`. Assert each allowed action and denial, default original/OCR denial, reason/time-limited support grant, TOTP-required sensitive action flag, and audit record.

- [ ] **Step 2: Verify RED and implement permission services**

Run: `python -m pytest tests/operations/test_permissions.py tests/operations/test_services.py -q`

Expected missing modules, then GREEN.

- [ ] **Step 3: Write failing metric/privacy/health tests**

Assert queue length, stage latency, retry rate, original-open failures, index differences, deletion backlog and provider error counters contain labels only from fixed enums; patient/document/filename/medical values are rejected. Health checks distinguish live, ready and dependency-degraded.

- [ ] **Step 4: Implement monitoring and verify GREEN**

Run: `python -m pytest tests/operations -q`

Expected: permission, action, privacy, health and alert tests pass.

### Task 7: Security headers, upload/session abuse checks, and privacy scan

**Files:**
- Create: `apps/core/security.py`
- Create: `apps/core/csp.py`
- Create: `tests/security/test_headers.py`
- Create: `tests/security/test_csrf_and_idor.py`
- Create: `tests/security/test_rate_limits.py`
- Create: `tests/privacy/test_runtime_artifacts.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: all existing HTTP routes and captured runtime artifacts.
- Produces: CSP/HSTS/cookie/referrer/content headers, cross-route IDOR matrix, abuse-limit evidence and privacy scanner.

- [ ] **Step 1: Write failing security header tests**

Assert production Secure/HttpOnly/SameSite cookies, HSTS, frame denial, nosniff, strict referrer policy and CSP that permits only required self-hosted assets plus explicitly configured push endpoint.

- [ ] **Step 2: Implement headers and verify focused GREEN**

Run: `python -m pytest tests/security/test_headers.py -q`

- [ ] **Step 3: Build route-generated CSRF/IDOR matrix**

Enumerate every mutation route and every patient-scoped read route from URL resolver. Assert POST/CSRF requirements and account A denial against account B UUIDs, rather than maintaining an incomplete handwritten subset.

- [ ] **Step 4: Scan runtime artifacts**

Run representative login/upload/OCR/search/view/delete flows into captured logs, analytics, notifications, cache keys and HTML cache. Seed unique canary phone/name/filename/query/lab values and assert zero occurrences outside their authorized response/source tables.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/security tests/privacy -q`

Expected: all headers, route matrix, rate limit and canary scans pass.

### Task 8: AC-14–AC-22 and test-scenario trace matrix

**Files:**
- Create: `tests/acceptance/test_ac14_ac22.py`
- Create: `docs/verification/prd-trace-matrix.md`
- Create: `docs/verification/ac14-ac22.md`
- Create: `tools/verify_prd_trace.py`
- Create: `tests/tools/test_verify_prd_trace.py`

**Interfaces:**
- Consumes: PRD MUST list, AC table and scenarios 1–26 plus all verification/test files.
- Produces: machine-checked matrix with one or more authoritative evidence links per requirement.

- [ ] **Step 1: Write failing trace-verifier tests**

Use a fixture matrix missing one MUST, AC-22 and scenario 26 and assert exact failures. Reject duplicate IDs, nonexistent test paths, `planned` status and unsupported claims without command/evidence path.

- [ ] **Step 2: Verify RED and implement verifier**

Run: `python -m pytest tests/tools/test_verify_prd_trace.py -q`

Expected missing verifier, then GREEN.

- [ ] **Step 3: Write integrated AC-14–AC-22 tests**

Cover optional correction-free feedback, single delete, account delete, notification privacy, full cross-account matrix, analytics/log privacy, versioned reparse, 1280×720 layout hooks and supported-browser manifest.

- [ ] **Step 4: Populate the complete matrix from real evidence**

Every MUST, AC-00–AC-22 and scenario 1–26 links to an automated test or a named manual/browser/performance evidence record. Missing evidence stays `blocked`; it cannot be changed to `pass` by prose.

- [ ] **Step 5: Verify GREEN**

Run: `python tools/verify_prd_trace.py docs/verification/prd-trace-matrix.md`

Run: `python -m pytest tests/acceptance/test_ac14_ac22.py tests/tools/test_verify_prd_trace.py -q`

Expected: verifier reports complete coverage and tests pass.

### Task 9: Browser, accessibility, performance, backup and release gate

**Files:**
- Create: `tests/e2e/phr-v1.spec.ts`
- Create: `tests/performance/k6-upload-search.js`
- Create: `deploy/compose.yaml`
- Create: `deploy/Caddyfile`
- Create: `deploy/backup.ps1`
- Create: `deploy/restore.ps1`
- Create: `docs/deployment/production-runbook.md`
- Create: `docs/verification/browser-accessibility.md`
- Create: `docs/verification/performance.md`
- Create: `docs/verification/backup-restore.md`
- Create: `docs/verification/release-gate.md`

**Interfaces:**
- Consumes: complete V1 application and fixed verification environment.
- Produces: deployable local/closed-trial stack and release decision evidence.

- [ ] **Step 1: Implement production-like local deployment**

Run Web, worker, scheduler, PostgreSQL, Redis and private MinIO behind HTTPS reverse proxy. Use health checks, non-root containers, read-only app filesystem where practical, explicit secrets and named encrypted-capable volumes. Production startup rejects development OTP, missing encryption keys and public object buckets.

- [ ] **Step 2: Run full browser and accessibility suite**

Automate P00-P08 at 1280×720 and 1440×900 in current Chrome and Edge; run current Safari plus previous Safari on the release macOS environment. Verify keyboard-only core flow, focus, labels, non-color status, no horizontal overflow and required copy.

- [ ] **Step 3: Run fixed-load performance suite**

Use the design environment: 100 online accounts, 10 concurrent upload batches, 300 documents/patient, average 3 pages and 2,000 OCR characters/page. Record P50/P95/P99 for PRD metrics and time-to-first-result; do not omit failures from percentiles.

- [ ] **Step 4: Execute backup/restore/deletion-tombstone drill**

Create data, back up, delete selected account/documents, restore the older backup, replay tombstones and prove deleted data remains inaccessible and absent from source/search/object storage. Record RPO/RTO measurements against 15 minutes/4 hours.

- [ ] **Step 5: Run the complete release gate**

Run: `python manage.py check --deploy --settings=config.settings.production`

Run: `python -m pytest -q`

Run: `node --test tests/js/*.test.mjs`

Run the trace verifier, browser suite, performance thresholds, privacy scan and backup drill. `release-gate.md` may say PASS only when all MUST, AC-00–AC-22, scenarios 1–26, cross-account zero-leak, deletion, browser and performance requirements have current evidence.

## Plan self-review

- Feedback, notifications, settings, deletion, operations, privacy and release concerns are implemented as behavior, not documentation-only promises.
- Deletion has immediate visibility denial, asynchronous physical purge and backup-restore enforcement.
- Analytics/notification privacy uses closed schemas and seeded canary scans.
- The trace verifier prevents narrow green suites from standing in for the whole PRD.
- External SMS/cloud credentials and macOS Safari are explicit release-environment dependencies; local implementation can progress without them, but release cannot be declared complete without their evidence.
