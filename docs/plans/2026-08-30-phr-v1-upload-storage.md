# PHR V1 Upload, Storage, and Processing-State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement PRD P02/P03 upload flow, immutable private original storage, batch/file limits, exact and possible duplicate handling, recoverable background task states, and persistent home-page progress.

**Architecture:** Django services own upload authorization, validation and document records; an `ObjectStore` port isolates local/S3 storage. A document is created only after server validation and durable object promotion, then an idempotent Celery task advances the internal processing run while public statuses remain the PRD vocabulary.

**Tech Stack:** Python 3.11, Django 5.2, Celery 5.6, Redis, PostgreSQL 18, boto3/S3-compatible storage, Pillow, pillow-heif, pypdf, pypdfium2/PDFium, Magika, pytest, pytest-django

**Spec:** `docs/specs/2026-08-30-phr-v1-system-design.md`

## Global Constraints

- A batch contains at most 20 files and 60 total pages.
- JPEG/JPG, PNG and HEIC images are at most 20 MB each; PDFs are at most 100 MB each.
- A trial account is limited to 300 documents, 1,000 pages and 2 GiB of originals.
- A document record is created only after the complete original is durably stored.
- Exact duplicate detection is scoped to one patient and uses SHA-256; it must never disclose another account's content.
- Possible duplicate detection is scoped to one patient, warns only, and never blocks or deletes.
- Upload failure creates no archive document; parsing failure retains the document and original.
- Public file states are `待上传`, `上传中`, `上传失败`, `处理中`, `已整理`, `仅原件`, `处理失败`, and `已存在`.
- The original object is immutable and private; no permanent public URL may exist.
- No log or analytics event may contain the original filename or medical content.

---

### Task 1: Document domain, public state machine, and quotas

**Files:**
- Create: `apps/documents/__init__.py`
- Create: `apps/documents/apps.py`
- Create: `apps/documents/models.py`
- Create: `apps/documents/domain.py`
- Create: `apps/documents/quotas.py`
- Create: `apps/documents/migrations/0001_initial.py`
- Create: `tests/documents/test_state_machine.py`
- Create: `tests/documents/test_quotas.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: `patients.Patient` and account quota settings.
- Produces: `UploadBatch`, `Document`, `DocumentPage`, `ProcessingRun`, `DocumentStatus`, `transition_document(document, target)`, and `check_upload_quota(patient, proposed)`.

- [ ] **Step 1: Write failing state-transition tests**

```python
def test_saved_document_moves_from_processing_to_organized(document):
    assert transition_document(document, DocumentStatus.ORGANIZED).status == DocumentStatus.ORGANIZED


def test_upload_failed_is_not_a_persisted_document_state(document):
    with pytest.raises(InvalidTransition):
        transition_document(document, "UPLOAD_FAILED")


def test_processing_failure_keeps_original_reference(document):
    transition_document(document, DocumentStatus.PROCESSING_FAILED)
    document.refresh_from_db()
    assert document.original_object_key
```

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/documents/test_state_machine.py -q`

Expected: import failure because the domain does not exist.

- [ ] **Step 3: Implement models and explicit transitions**

Use UUID primary keys. `Document` includes patient, sanitized display filename, content type, byte size, page count, SHA-256, optional perceptual hash, immutable object key, status, timestamps and soft-delete visibility flag. Database uniqueness is `(patient_id, sha256, deleted_at IS NULL)` via a conditional constraint. `ProcessingRun` has a unique idempotency key and internal stage.

- [ ] **Step 4: Write failing quota boundary tests**

Use literal cases for exactly 20 files/60 pages/300 documents/1,000 pages/2 GiB accepted, and one unit beyond each rejected with a stable machine error code.

- [ ] **Step 5: Implement quota value object and verify GREEN**

Run: `python -m pytest tests/documents/test_state_machine.py tests/documents/test_quotas.py -q`

Expected: all transitions, persistence and boundaries pass.

### Task 2: File inspection and private object-store contract

**Files:**
- Create: `apps/documents/storage.py`
- Create: `apps/documents/inspection.py`
- Create: `apps/documents/errors.py`
- Create: `tests/documents/fakes.py`
- Create: `tests/documents/test_inspection.py`
- Create: `tests/documents/test_storage.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: an uploaded binary stream and claimed metadata.
- Produces: `inspect_upload(stream, claimed_name) -> InspectedFile` and `ObjectStore` methods `put_staging`, `promote_immutable`, `open_private`, `delete`, `presign_get`.

- [ ] **Step 1: Add bounded dependencies and write failing real-fixture tests**

Add `Pillow>=11,<13`, `pillow-heif>=1,<2`, `pypdf>=6.16,<7`, `pypdfium2>=5.13,<6`, and `magika>=0.6,<1`. Use pypdf for structural/security inspection and pypdfium2 for parse/render validation; retain the bundled PDFium dependency notices in release artifacts. Generate tiny valid JPEG, PNG and PDF fixtures inside tests; include malformed bytes, extension/MIME mismatch, encrypted PDF and a decompression-bomb dimension header.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_inspection.py -q`

Expected: missing inspection implementation.

- [ ] **Step 3: Implement server-side inspection**

Read a bounded header for type detection, then validate the complete stream. Return canonical type, size, page count, SHA-256, dimensions and normalized extension. Reject unsupported, unreadable, encrypted, mismatched and oversized inputs with PRD-aligned error codes; never trust browser metadata.

- [ ] **Step 4: Write failing immutable-storage contract tests**

The in-memory fake must prove staging objects are not readable through document APIs, promotion is atomic from the service perspective, an existing immutable key cannot be overwritten, and presigned URLs expire after five minutes.

- [ ] **Step 5: Implement local and S3 adapters and verify GREEN**

Run: `python -m pytest tests/documents/test_inspection.py tests/documents/test_storage.py -q`

Expected: all format, boundary, privacy and immutability tests pass.

### Task 3: Atomic upload service, exact duplicates, and batch aggregation

**Files:**
- Create: `apps/documents/services.py`
- Create: `apps/documents/deduplication.py`
- Create: `tests/documents/test_upload_service.py`
- Create: `tests/documents/test_deduplication.py`

**Interfaces:**
- Consumes: authenticated patient, `InspectedFile`, staging key, object store and batch ID.
- Produces: `finalize_upload(...) -> UploadOutcome` where outcome is `CREATED`, `EXACT_DUPLICATE`, or a typed failure.

- [ ] **Step 1: Write failing atomicity tests**

Test durable promotion before `Document` creation, object rollback when database creation fails, no document when inspection fails, and a document plus queued run only after success.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_upload_service.py -q`

Expected: missing service.

- [ ] **Step 3: Implement transaction and object compensation**

Lock the patient quota row, recheck quota server-side, query exact hash inside the patient, promote the original, create document/pages/run in one database transaction, and delete the promoted object if commit fails. Queue processing with `transaction.on_commit`.

- [ ] **Step 4: Add cross-account duplicate tests**

Use the same bytes in two patient accounts. The second account must receive `CREATED`, never a pointer or timing result from the first. Re-upload within the same patient must return `EXACT_DUPLICATE` and the existing document UUID without a new object.

- [ ] **Step 5: Implement patient-scoped comparison and verify GREEN**

Run: `python -m pytest tests/documents/test_upload_service.py tests/documents/test_deduplication.py -q`

Expected: all atomicity, quota and tenant cases pass.

### Task 4: P03 batch-upload HTTP flow and accessible progress UI

**Files:**
- Create: `apps/documents/forms.py`
- Create: `apps/documents/views.py`
- Create: `apps/documents/urls.py`
- Create: `templates/documents/upload.html`
- Create: `templates/documents/_file_row.html`
- Create: `static/css/upload.css`
- Create: `static/js/upload.js`
- Create: `tests/documents/test_upload_views.py`
- Create: `tests/accessibility/test_upload_markup.py`
- Modify: `config/urls.py`
- Modify: `templates/base_app.html`

**Interfaces:**
- Consumes: Task 3 upload service and authenticated request patient.
- Produces: `/uploads/new/`, `/api/upload-batches/`, per-file upload/finalize endpoints, and `/api/upload-batches/<uuid>/status/`.

- [ ] **Step 1: Write failing authorization and validation tests**

Cover unauthenticated redirect, CSRF rejection, account A cannot write into B's batch, mixed valid/invalid files do not block valid rows, batch limits, and exact-duplicate response containing only the current patient's existing document.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_upload_views.py -q`

Expected: missing routes.

- [ ] **Step 3: Implement endpoints with stable JSON error codes**

All IDs are resolved through SQL tenant filters. File names are sanitized for display and never logged. A response distinguishes bytes still uploading from original durably saved. Status polling uses ETag/`updated_at` to avoid unnecessary payloads.

- [ ] **Step 4: Implement P03 drag/drop and keyboard flow**

The page shows thumbnail/PDF icon, display filename, pages, size, remove control and status per row. Upload valid files independently with bounded concurrency three. Announce status changes through an `aria-live=polite` region. Only after server `saved=true` show the PRD leave-page message.

- [ ] **Step 5: Verify UI and view GREEN**

Run: `python -m pytest tests/documents/test_upload_views.py tests/accessibility/test_upload_markup.py -q`

Expected: all request, isolation, copy and markup tests pass.

### Task 5: Idempotent processing runner, retry policy, and stale-job recovery

**Files:**
- Create: `apps/processing/__init__.py`
- Create: `apps/processing/apps.py`
- Create: `apps/processing/tasks.py`
- Create: `apps/processing/runner.py`
- Create: `apps/processing/errors.py`
- Create: `tests/processing/test_runner.py`
- Create: `tests/processing/test_tasks.py`
- Modify: `config/settings/base.py`

**Interfaces:**
- Consumes: queued `ProcessingRun` UUID.
- Produces: `run_processing(run_id, pipeline)`, Celery `process_document`, and `recover_stale_runs`.

- [ ] **Step 1: Write failing idempotency tests**

Deliver the same task twice and assert the pipeline commits one result set. Assert a retryable failure schedules delays of 60, 300 and 1,800 seconds, then exposes `处理失败`; a no-structured-result outcome exposes `仅原件`; a success exposes `已整理`.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_runner.py tests/processing/test_tasks.py -q`

Expected: missing processing implementation.

- [ ] **Step 3: Implement database-locked runner and exact error mapping**

Lock `ProcessingRun`, compare idempotency key and terminal state, update heartbeats between stages, and use typed exceptions rather than message matching. Never change the original object key.

- [ ] **Step 4: Implement stale recovery and verify GREEN**

A periodic task finds nonterminal runs whose heartbeat is older than 15 minutes and requeues only if no newer active run exists.

Run: `python -m pytest tests/processing -q`

Expected: all duplicate-delivery, retries, status and recovery tests pass.

### Task 6: P02 task cards, recent documents, and possible-duplicate warning

**Files:**
- Create: `apps/documents/selectors.py`
- Create: `apps/documents/similarity.py`
- Create: `templates/patients/home.html`
- Create: `templates/documents/_task_card.html`
- Create: `templates/documents/_recent_document.html`
- Create: `static/css/home.css`
- Create: `static/js/task-status.js`
- Create: `tests/documents/test_home.py`
- Create: `tests/documents/test_similarity.py`
- Modify: `apps/patients/views.py`

**Interfaces:**
- Consumes: UploadBatch/Document and patient context.
- Produces: recent five documents, batches ordered newest first, seven-day completed-card retention, and patient-scoped possible-duplicate hints.

- [ ] **Step 1: Write failing selector and similarity tests**

Test five-result limit, order rules, completed-card cutoff at exactly seven days, no medical importance sorting, and that perceptual similarity never crosses patient boundaries or blocks creation.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_home.py tests/documents/test_similarity.py -q`

Expected: missing selectors.

- [ ] **Step 3: Implement P02 data and warm-professional layout**

Render fixed left navigation, upload primary action, task counts, recent five documents and PRD empty state. At widths below the desktop two-column breakpoint, use one column without horizontal scrolling; below supported width show the desktop-access notice.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/documents/test_home.py tests/documents/test_similarity.py -q`

Expected: all selection, tenant and view behaviors pass.

### Task 7: Upload acceptance, privacy scan, and verification record

**Files:**
- Create: `tests/acceptance/test_ac02_ac07.py`
- Create: `tests/security/test_upload_isolation.py`
- Create: `tests/privacy/test_upload_logs.py`
- Create: `docs/verification/ac02-ac07.md`

**Interfaces:**
- Consumes: Tasks 1–6 and the onboarding foundation.
- Produces: repeatable evidence for PRD AC-02 through AC-07 and the upload portions of AC-18/AC-19.

- [ ] **Step 1: Write integrated failing acceptance tests**

Cover 20 files/60 pages, leaving after durable save, failed upload with no document, parsing downgrade with original readable, exact duplicate link, date-unrecognized placeholder, retry, and two-account upload isolation.

- [ ] **Step 2: Verify RED and close only real integration gaps**

Run: `python -m pytest tests/acceptance/test_ac02_ac07.py tests/security/test_upload_isolation.py tests/privacy/test_upload_logs.py -q`

Expected before final wiring: at least one behavior assertion fails for a missing integration, not a broken fixture.

- [ ] **Step 3: Run complete verification**

Run: `python manage.py check`

Run: `python -m pytest -q`

Expected: all Python tests pass with no warnings and no sample medical content in captured logs.

- [ ] **Step 4: Record evidence**

Write commands, versions, counts, UTC timestamp and any skipped external-object-store checks to `docs/verification/ac02-ac07.md`. A skipped check remains an explicit release blocker and may not be described as passed.

## Plan self-review

- Every document is created only after durable original storage.
- Exact duplicate, possible duplicate and cross-account behavior are distinct and tested.
- Upload/browser states and processing/internal stages are not conflated.
- This plan produces a usable P02/P03 vertical slice, not empty endpoints.
- OCR extraction, search, detail/viewer, trend, notifications and deletion remain in later plans and are not claimed complete here.
