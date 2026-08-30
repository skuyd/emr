# PHR V1 OCR, Sample Dictionary, and Versioned Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn saved PDF/image originals into versioned OCR, conservative document metadata, source-linked lab fields, and a reproducible dictionary containing at least 125 indicators extracted from `示例/`.

**Architecture:** A provider-neutral pipeline turns each immutable original into normalized pages and `OcrBlock` records. Deterministic parsers classify documents and associate lab table cells; a versioned local dictionary maps only sufficiently supported names, and a transactional publisher activates a complete parsing run without overwriting prior results.

**Tech Stack:** Python 3.11, Django 5.2, Celery 5.6, pypdf, pypdfium2/PDFium, Pillow/pillow-heif, PaddleOCR 3.x, PostgreSQL 18, pytest

**Spec:** `docs/superpowers/specs/2026-08-30-phr-v1-system-design.md`

## Global Constraints

- Originals are immutable; preprocessing outputs use separate private object keys.
- PDF text layers are preferred only when readable and sufficiently complete; otherwise pages are rendered and OCR'd.
- Every OCR block and extracted field retains page and source coordinates when available.
- Failed OCR or structure extraction retains the original and yields `仅原件` or `处理失败` according to typed failure.
- No missing date component may be invented.
- Low-confidence or ambiguously associated lab content remains searchable OCR and does not become a structured field.
- The dictionary must contain at least 125 unique canonical indicators extracted from `示例/`; no invented medical term may be used to reach the count.
- Local dictionary codes use `PHR-LAB-0001` order and do not claim an external code mapping.
- Results such as 未检出/阳性/阴性/溶血/拒收/未报告 remain text states and never become numeric zero.
- A new parse creates a new version; activation is transactional and previous versions remain traceable.

---

### Task 1: OCR and parsing-version persistence model

**Files:**
- Create: `apps/processing/models.py`
- Create: `apps/processing/value_objects.py`
- Create: `apps/processing/migrations/0001_initial.py`
- Create: `tests/processing/test_models.py`
- Create: `tests/processing/test_value_objects.py`

**Interfaces:**
- Consumes: `documents.Document`, `DocumentPage`, and `ProcessingRun`.
- Produces: `ParsingVersion`, `OcrBlock`, `SourceEvidence`, `DocumentMetadataCandidate`, and immutable `OcrPage`/`OcrRegion` value objects.

- [ ] **Step 1: Write failing coordinate and version tests**

```python
def test_normalized_region_rejects_coordinates_outside_page():
    with pytest.raises(InvalidRegion):
        OcrRegion(text="白细胞", polygon=((0.1, 0.1), (1.2, 0.1), (1.2, 0.2), (0.1, 0.2)), confidence=0.98)


def test_document_has_only_one_active_parsing_version(document, version_factory):
    first = version_factory(document=document, active=True)
    second = version_factory(document=document, active=True)
    first.refresh_from_db()
    assert second.active is True
    assert first.active is False
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_models.py tests/processing/test_value_objects.py -q`

Expected: missing models/value objects.

- [ ] **Step 3: Implement normalized evidence and conditional active-version constraint**

Store polygon coordinates as validated JSON in source page coordinates normalized to `[0,1]`. `ParsingVersion` records parser semantic version, OCR provider/version, dictionary version/hash, creation time and active state. Activation uses a service transaction, not a model `save()` side effect.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/processing/test_models.py tests/processing/test_value_objects.py -q`

Expected: all coordinate, ordering and uniqueness behavior passes.

### Task 2: PDF/image page preparation and text-layer policy

**Files:**
- Create: `apps/processing/preparation.py`
- Create: `apps/processing/pdf.py`
- Create: `apps/processing/images.py`
- Create: `tests/processing/test_pdf_preparation.py`
- Create: `tests/processing/test_image_preparation.py`

**Interfaces:**
- Consumes: private original stream and canonical content type.
- Produces: `PreparedDocument(pages, extracted_text_layer, warnings)` with stable page dimensions and orientation.

- [ ] **Step 1: Write failing PDF fixtures and policy tests**

Create programmatic fixtures for a valid text-layer PDF, image-only PDF, rotated page, gibberish text layer, encrypted PDF and multi-page PDF. Assert text layer is used only when at least 70% of visible text characters are printable and each nonblank page has at least 20 meaningful characters; otherwise render at 200 DPI for OCR.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_pdf_preparation.py -q`

Expected: preparation module missing.

- [ ] **Step 3: Implement PDF preparation with resource bounds**

Limit 60 pages per accepted batch, 40 megapixels per rendered page and 300 megapixels per document preparation. Preserve PDF page indices and character bounding boxes from trustworthy text layers.

- [ ] **Step 4: Write failing JPEG/PNG/HEIC orientation tests and implement image preparation**

EXIF rotation is applied to the derived page only; the original stream hash remains unchanged. Very large images are downscaled only for OCR while evidence coordinates map back to original dimensions.

- [ ] **Step 5: Verify GREEN**

Run: `python -m pytest tests/processing/test_pdf_preparation.py tests/processing/test_image_preparation.py -q`

Expected: all text-layer, fallback, orientation and bound cases pass.

### Task 3: Provider-neutral OCR and PaddleOCR adapter

**Files:**
- Create: `apps/processing/ocr/__init__.py`
- Create: `apps/processing/ocr/base.py`
- Create: `apps/processing/ocr/paddle.py`
- Create: `apps/processing/ocr/text_layer.py`
- Create: `apps/processing/ocr/fake.py`
- Create: `tests/processing/test_ocr_contract.py`
- Create: `tests/processing/test_text_layer_ocr.py`
- Create: `tests/processing/test_paddle_adapter.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: prepared page.
- Produces: `OcrProvider.recognize(page) -> OcrPage` with ordered regions, text, normalized polygon, confidence and provider metadata.

- [ ] **Step 1: Add an `ocr` optional dependency group and failing contract tests**

Pin bounded PaddleOCR 3.x/PaddlePaddle-compatible dependencies in an optional group so ordinary web tests do not download models. Contract tests run every provider against a complete fake page and require deterministic reading order and valid coordinates.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_ocr_contract.py tests/processing/test_text_layer_ocr.py -q`

Expected: missing provider classes.

- [ ] **Step 3: Implement text-layer and fake providers first**

The text-layer provider converts PDF character/word boxes without changing text. The fake provider consumes a full `OcrPage` fixture and is used only in tests/development demos.

- [ ] **Step 4: Implement Paddle adapter behind lazy imports**

Production startup selects `PHR_OCR_PROVIDER`. Importing Django without the `ocr` extra must still succeed when Paddle is not selected. The adapter disables provider telemetry, records exact model names/versions, and maps polygons to normalized page coordinates.

- [ ] **Step 5: Verify contract GREEN**

Run: `python -m pytest tests/processing/test_ocr_contract.py tests/processing/test_text_layer_ocr.py tests/processing/test_paddle_adapter.py -q`

Expected: non-model tests pass; model smoke test is marked `ocr_model` and passes only in the prepared OCR environment, never silently skipped in release verification.

### Task 4: Reproducible sample candidate extraction

**Files:**
- Create: `tools/sample_dictionary/__init__.py`
- Create: `tools/sample_dictionary/discover.py`
- Create: `tools/sample_dictionary/extract.py`
- Create: `tools/sample_dictionary/normalize.py`
- Create: `tools/sample_dictionary/report.py`
- Create: `apps/labs/__init__.py`
- Create: `apps/labs/candidates.py`
- Create: `tests/tools/test_sample_discovery.py`
- Create: `tests/tools/test_candidate_normalization.py`

**Interfaces:**
- Consumes: explicit local sample root, never a hard-coded patient name.
- Produces: deterministic candidate records `{raw_name, normalized_name, source_file_hash, page, region, context_hash}` without copying patient identifiers into reports.

- [ ] **Step 1: Write failing discovery and normalization tests**

Test recursive PDF/JPEG/PNG discovery, exclusion of unrelated files, path redaction to a SHA-256 alias, Unicode NFKC, full/half-width normalization, whitespace collapse, unit/result stripping and preservation of meaningful Latin abbreviations.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/tools/test_sample_discovery.py tests/tools/test_candidate_normalization.py -q`

Expected: missing tools.

- [ ] **Step 3: Implement candidate row heuristics**

Identify table-like rows containing a name region plus a result/status and optional unit/reference range. Reject headers, patient demographics, identifiers, dates and institution names. Store only hashed source aliases in generated reports; coordinates allow local trace-back.

- [ ] **Step 4: Add deterministic CLI and verify GREEN**

Run: `python -m tools.sample_dictionary.extract --samples 示例 --output .runtime/generated/lab-candidates.json`

Run twice and assert byte-identical output for unchanged OCR cache and parser version.

Run: `python -m pytest tests/tools -q`

Expected: all discovery, redaction and determinism tests pass.

### Task 5: Publish at least 125 sample-derived dictionary entries

**Files:**
- Create: `apps/labs/models.py`
- Create: `apps/labs/dictionary.py`
- Create: `apps/labs/migrations/0001_initial.py`
- Create: `resources/dictionaries/lab-indicators.v1.json`
- Create: `docs/dictionaries/lab-indicators-v1.md`
- Create: `docs/verification/sample-dictionary-v1.md`
- Create: `tests/labs/test_dictionary.py`
- Create: `tests/labs/test_dictionary_provenance.py`

**Interfaces:**
- Consumes: Task 4 candidates and locally traceable source coordinates.
- Produces: versioned `LabDictionaryEntry`/`LabAlias`, JSON release and readable review list.

- [ ] **Step 1: Write failing dictionary schema and provenance tests**

Assert at least 125 unique canonical entries, contiguous codes beginning `PHR-LAB-0001`, nonempty standard Chinese name, at least one alias, at least one sample source hash/page, unique normalized aliases within an entry, no claimed external code without explicit evidence, semantic version, and whole-file SHA-256.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/labs/test_dictionary.py tests/labs/test_dictionary_provenance.py -q`

Expected: release file/model missing or entry count below 125.

- [ ] **Step 3: Generate, consolidate and review candidates**

Run the sample extractor, group exact normalized terms, merge only clear abbreviation/full-name synonyms supported by the same report context, split ambiguous aliases, assign broad categories such as blood-count, chemistry, coagulation, tumor-marker, immunity, infection and other. Do not add a term that has no source candidate.

- [ ] **Step 4: Publish immutable V1 artifacts and import command**

`dictionary.py` validates the JSON and imports by `(version, content_hash)` idempotently. The Markdown list shows code, canonical name, aliases, category, capability level and redacted source count, never patient/file names.

- [ ] **Step 5: Verify GREEN and count evidence**

Run: `python -m pytest tests/labs/test_dictionary.py tests/labs/test_dictionary_provenance.py -q`

Expected: all schema/provenance tests pass and report the exact count, which is at least 125.

### Task 6: Conservative document type, date, and institution candidates

**Files:**
- Create: `apps/processing/classification.py`
- Create: `apps/processing/dates.py`
- Create: `apps/processing/institutions.py`
- Create: `tests/processing/test_classification.py`
- Create: `tests/processing/test_dates.py`
- Create: `tests/processing/test_institutions.py`

**Interfaces:**
- Consumes: ordered OCR pages.
- Produces: PRD document type and candidate metadata with raw text, precision, page, region and confidence.

- [ ] **Step 1: Write failing literal classification/date fixtures**

Cover all eight PRD types, unknown/other fallback, lab sampling/report-date precedence, imaging examination/report precedence, year-month precision, conflicting dates, and no invented component.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_classification.py tests/processing/test_dates.py -q`

Expected: missing parsers.

- [ ] **Step 3: Implement explainable weighted evidence**

Rules return candidates and evidence rather than a bare label. If the top conflicting date scores differ by less than 0.10, user document date is unset. Institution extraction requires an institution keyword/context and preserves raw text without global normalization.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/processing/test_classification.py tests/processing/test_dates.py tests/processing/test_institutions.py -q`

Expected: all type, precedence, precision and conflict tests pass.

### Task 7: Layout-linked lab extraction and result typing

**Files:**
- Create: `apps/labs/extraction.py`
- Create: `apps/labs/mapping.py`
- Create: `apps/labs/result_types.py`
- Create: `apps/labs/services.py`
- Create: `apps/labs/migrations/0002_extractedfield.py`
- Create: `tests/labs/test_result_types.py`
- Create: `tests/labs/test_extraction.py`
- Create: `tests/labs/test_mapping.py`

**Interfaces:**
- Consumes: OcrPage list and exact dictionary version.
- Produces: candidate/accepted `ExtractedField` records with raw name/value/unit/range/flag, result type, date and evidence.

- [ ] **Step 1: Write failing result-type table tests**

Use literal expected objects for ordinary numbers, decimals, `<`/`>`, positive/negative/not-detected, plus grades, hemolysis/rejected/not-reported, and invalid mixed text. Explicitly assert every state retains raw text and none becomes numeric zero.

- [ ] **Step 2: Verify RED and implement result typing**

Run: `python -m pytest tests/labs/test_result_types.py -q`

Expected RED before implementation; GREEN afterward.

- [ ] **Step 3: Write failing spatial-association tests**

Cover single-row, double-column, wrapped name, reference range on same row, misaligned value/unit, multi-table page and cross-page headers. A wrong-row association must be rejected even when all tokens individually have high OCR confidence.

- [ ] **Step 4: Implement row/column grouping and thresholds**

Structured acceptance requires composite confidence at least 0.80. Dictionary mapping below 0.90 keeps raw field without standard name. Persist evidence for every accepted field; rejected candidates remain only in internal run diagnostics without medical text logs.

- [ ] **Step 5: Verify extraction GREEN**

Run: `python -m pytest tests/labs/test_result_types.py tests/labs/test_extraction.py tests/labs/test_mapping.py -q`

Expected: all result, layout, ambiguity and threshold tests pass.

### Task 8: Transactional pipeline publication and historical reprocessing

**Files:**
- Create: `apps/processing/pipeline.py`
- Create: `apps/processing/publication.py`
- Create: `tests/processing/test_pipeline.py`
- Create: `tests/processing/test_publication.py`
- Create: `tests/acceptance/test_ac11_ac12_ac20.py`
- Create: `docs/verification/ac11-ac12-ac20.md`

**Interfaces:**
- Consumes: immutable document, OCR provider, parser version and dictionary release.
- Produces: complete inactive parse, atomic activation, rebuild trigger and rollback to an older successful version.

- [ ] **Step 1: Write failing end-to-end pipeline tests**

Test successful activation, OCR-only downgrade, retryable provider error, parse error, failure after fields but before publication, duplicate task delivery, reparse with new version, and rollback. Readers must see all-old or all-new fields, never a mixture.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/processing/test_pipeline.py tests/processing/test_publication.py -q`

Expected: missing orchestration/publication.

- [ ] **Step 3: Implement staged parse and atomic activation**

Create version, OCR and fields as inactive; transactionally deactivate prior version and activate new version only after all stages and search payload generation succeed. A rollback toggles active version but never deletes runs.

- [ ] **Step 4: Run acceptance and full regression GREEN**

Run: `python -m pytest tests/acceptance/test_ac11_ac12_ac20.py tests/processing tests/labs -q`

Expected: all original-value, state-result and historical-version tests pass.

- [ ] **Step 5: Record verification**

Document exact dictionary count/hash, OCR provider/model, parser version, test commands/counts, sample coverage and any model-smoke limitation. A model smoke test not run remains a release blocker.

## Plan self-review

- The 125-entry threshold is proven by released sample-derived data and provenance tests, not a hard-coded assertion alone.
- OCR provider details do not leak into domain consumers.
- Type/date/institution extraction remains conservative and explainable.
- Version publication prevents half-updated detail/search results and supports AC-20.
- Search UI, archive, viewer, trends, notification and deletion are implemented by later plans and are not claimed complete here.
