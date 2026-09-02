# Health Trend Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a patient-scoped `/trends/` overview and make “健康趋势” a real primary-navigation destination without changing the existing conservative trend rules.

**Architecture:** Extend `apps.labs.trends` with a summary read model derived from the same `TrendView` objects used by detail pages, so eligibility cannot diverge and all summaries are built from one patient-scoped observation query. Add a server-rendered Django index view and semantic warm-paper cards, then simplify navigation state to the stable `current_section="trends"` contract.

**Tech Stack:** Python 3.11, Django 5.2, Django templates, CSS, pytest/pytest-django

**Spec:** `docs/superpowers/specs/2026-09-02-health-trend-index-design.md`

## Global Constraints

- `GET /trends/` is the only new URL; existing `/trends/<standard_code>/` URLs remain compatible.
- Total and detail views reuse the same conservative eligibility and grouping logic.
- Every observation query is filtered in SQL by the authenticated account's patient and excludes deleted documents and inactive parsing versions.
- Cards preserve the latest comparable observation's raw value precision and raw unit; missing units are labeled, never inferred.
- Trends never convert units, compute percentage changes, fill missing points, or generate diagnosis, treatment, normal/abnormal, improvement, deterioration, or efficacy language.
- The overview performs one candidate-observation query and no per-indicator queries.
- Product name remains `健康之家`; desktop navigation remains `首页 / 健康档案 / 健康趋势 / 我的`, and mobile remains `首页 / 档案 / 上传 / 趋势 / 我的`.
- `/trends/` and `/trends/<standard_code>/` mark only the trends destination current; archive and document pages mark only records current.
- JavaScript is not required for overview navigation or content.
- At 390px, 768px, and 1440px there is no page-level horizontal overflow; focus is visible, touch targets are at least 44px, and forced-colors mode remains usable.
- No database migration, dependency, analytics-event, or unrelated-file change is permitted.

---

### Task 1: Shared patient trend-summary selector

**Files:**
- Modify: `apps/labs/trends.py`
- Modify: `tests/labs/test_trends.py`

**Interfaces:**
- Consumes: the existing `_candidate_observations`, `_trend_views`, `TrendView`, and conservative series eligibility rules.
- Produces: immutable `TrendSummary(standard_code, standard_name, latest_observation, point_count)` and `trend_summaries(patient) -> tuple[TrendSummary, ...]`.

- [ ] **Step 1: Write failing behavior tests**

Import the module rather than the missing function so RED is an assertion failure at call time. Add tests equivalent to:

```python
from apps.labs import trends


def test_trend_summaries_include_only_current_patients_eligible_codes(django_user_model):
    _client, patient = _patient(django_user_model, "summary-owner")
    _other_client, other = _patient(django_user_model, "summary-other")
    _observation(patient, date(2026, 7, 1), "4.200")
    _document, latest = _observation(patient, date(2026, 8, 20), "5.0", raw_name="WBC")
    _observation(patient, date(2026, 8, 21), "88", code="LAB_SINGLE", standard_name="单次指标")
    _observation(other, date(2026, 7, 1), "99.1", code="LAB_OTHER")
    _observation(other, date(2026, 8, 1), "99.2", code="LAB_OTHER")

    summaries = trends.trend_summaries(patient)

    assert [(item.standard_code, item.standard_name) for item in summaries] == [("LAB_WBC", "白细胞计数")]
    assert summaries[0].latest_observation == latest
    assert summaries[0].point_count == 2


def test_trend_summaries_use_one_candidate_query(django_user_model, django_assert_num_queries):
    _client, patient = _patient(django_user_model, "summary-query")
    _observation(patient, date(2026, 7, 1), "4.2")
    _observation(patient, date(2026, 8, 1), "4.6")

    with django_assert_num_queries(1):
        summaries = trends.trend_summaries(patient)

    assert len(summaries) == 1
```

Also cover newest-first deterministic ordering and a multi-series code whose point count is the sum of included points.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/labs/test_trends.py -q`

Expected: the new tests fail because `trend_summaries` does not exist; all existing detail behavior remains green.

- [ ] **Step 3: Implement the minimal shared selector**

Add:

```python
@dataclass(frozen=True)
class TrendSummary:
    standard_code: str
    standard_name: str
    latest_observation: LabObservation
    point_count: int


def trend_summaries(patient):
    summaries = []
    for trend in _trend_views(patient).values():
        included = tuple(point.observation for series in trend.series for point in series.points)
        latest = max(included, key=lambda item: (item.observation_date, item.created_at, str(item.pk)))
        summaries.append(TrendSummary(trend.standard_code, trend.standard_name, latest, len(included)))
    return tuple(
        sorted(
            summaries,
            key=lambda item: (
                -item.latest_observation.observation_date.toordinal(),
                item.standard_name.casefold(),
                item.standard_code,
            ),
        )
    )
```

Change `_candidate_observations(patient, codes=None)` to build the existing patient-scoped queryset and apply `standard_code__in=codes` only when codes is not `None`. Change `_trend_views(patient, codes=None)` accordingly. Preserve `eligible_trend_codes` and `trend_view` public behavior by continuing to pass explicit code tuples.

- [ ] **Step 4: Verify GREEN and refactor only while green**

Run: `python -m pytest tests/labs/test_trends.py -q`

Expected: all selector, eligibility, detail, isolation, ordering, point-count, and one-query tests pass with pristine output.

- [ ] **Step 5: Commit**

```text
feat(trends): add patient trend summaries
```

### Task 2: Trend overview route, cards, empty state, and navigation

**Files:**
- Modify: `apps/documents/urls.py`
- Modify: `apps/documents/views.py`
- Create: `templates/documents/trends.html`
- Modify: `templates/documents/trend.html`
- Modify: `templates/components/_app_navigation.html`
- Modify: `static/css/trend.css`
- Create: `tests/documents/test_trend_index.py`
- Modify: `tests/accessibility/test_shell_markup.py`
- Modify: `tests/accessibility/test_detail_trend_markup.py`

**Interfaces:**
- Consumes: Task 1's `trend_summaries(patient)` tuple and existing `TrendSummary` fields.
- Produces: named route `documents:trend_index`, `trend_index(request)`, a semantic responsive index, stable `current_section="trends"`, and a detail-to-index return path.

- [ ] **Step 1: Write failing route and rendering tests**

Create `tests/documents/test_trend_index.py` with real database observations and assertions equivalent to:

```python
def test_trend_index_lists_only_eligible_current_patient_summaries(django_user_model):
    client, patient = _patient(django_user_model, "index-owner")
    _observation(patient, date(2026, 7, 1), "4.200")
    _observation(patient, date(2026, 8, 20), "5.0")
    _observation(patient, date(2026, 8, 21), "88", code="LAB_SINGLE", standard_name="单次指标")

    response = client.get("/trends/")
    content = response.content.decode()

    assert response.status_code == 200
    assert 'href="/trends/LAB_WBC/"' in content
    assert "白细胞计数" in content and "5.0" in content and "10^9/L" in content
    assert "2026年8月20日" in content and "2 次可比较记录" in content
    assert "单次指标" not in content
    assert response["Cache-Control"] == "private, no-store, max-age=0"


def test_trend_index_empty_state_explains_requirement_and_next_actions(django_user_model):
    client, _patient = _patient(django_user_model, "index-empty")
    response = client.get("/trends/")
    content = response.content.decode()

    assert response.status_code == 200
    assert "暂时没有可生成趋势的指标" in content
    assert "同一指标至少需要两个可比较记录" in content
    assert 'href="/records/"' in content
    assert 'href="/uploads/new/"' in content
```

Also test anonymous redirect, another patient's values never appearing, neutral-copy forbidden words, one `h1`, semantic list/time markup, route reversing, and the detail page's “返回趋势总览” link.

Update shell tests so both navigation variants link to `/trends/`, `/trends/` is included in the current-destination matrix, and both `trend_index` and `indicator_trend` resolve to exactly one trends current item. Extend the CSS contract test for 44px summary actions, visible focus, narrow single-column layout, and forced-colors support.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/documents/test_trend_index.py tests/accessibility/test_shell_markup.py tests/accessibility/test_detail_trend_markup.py -q`

Expected: failures show `/trends/` is missing, navigation still targets `/records/`, and overview markup/styles do not exist.

- [ ] **Step 3: Add route and protected view**

Register the static route before the dynamic one:

```python
path("trends/", views.trend_index, name="trend_index"),
path("trends/<str:standard_code>/", views.indicator_trend, name="indicator_trend"),
```

Import `trend_summaries` and add a `@patient_required`, `@require_GET` view rendering `documents/trends.html` with `{"trends": trend_summaries(request.patient), "current_section": "trends"}` through `protect_sensitive_html`. Set the existing detail view's current section to `trends`; do not add an analytics event for the overview.

- [ ] **Step 4: Build semantic overview and stable navigation**

Render one `h1`, an ordered summary list, per-card `h2`, latest raw result, raw unit or “报告未列单位”, `<time datetime="YYYY-MM-DD">`, “N 次可比较记录”, and a 44px “查看趋势” button. Render the exact empty-state heading/message and both next-action links from the spec. Include the existing neutral disclaimer and no conversion/percentage rule.

Point both trends navigation links to `{% url 'documents:trend_index' %}`. Base current state only on `current_section`: `records` for archive/document pages and `trends` for both trend routes. Change the detail back link to `{% url 'documents:trend_index' %}` with label “返回趋势总览”.

- [ ] **Step 5: Add responsive styles and verify GREEN**

Extend `trend.css` with `.trend-summary-list`, `.trend-summary-card`, metadata/result/action, and empty-action rules. Use existing tokens only, prevent child overflow with `min-width: 0` and `overflow-wrap: anywhere`, stack card content at `max-width: 48rem`, add `min-height: 2.75rem` and a 3px `var(--color-focus)` focus outline, and retain borders in forced colors.

Run: `python -m pytest tests/documents/test_trend_index.py tests/labs/test_trends.py tests/accessibility/test_shell_markup.py tests/accessibility/test_detail_trend_markup.py -q`

Expected: all overview, detail, navigation, privacy, responsive-contract, and accessibility tests pass with pristine output.

- [ ] **Step 6: Run integration checks and commit**

Run:

```text
python manage.py check
python manage.py makemigrations --check --dry-run
git diff --check
```

Expected: Django reports no issues, no model changes are detected, and the diff has no whitespace errors.

Commit:

```text
feat(trends): add health trend overview
```

## Plan self-review

- Spec coverage: Task 1 owns shared eligibility, patient isolation, deterministic summary data, count semantics, and bounded queries. Task 2 owns route, rendering, empty state, navigation behavior, detail return path, privacy headers, responsive layout, and accessibility.
- Shared interfaces: Task 2 consumes exactly the four fields produced by Task 1; no later task depends on an undeclared field.
- File ownership: both tasks touch different production/test files except through the declared `trend_summaries` interface.
- Placeholder scan: the plan contains no deferred implementation, unspecified error handling, or “similar to” steps.
- Scope: no migration, dependency, analytics schema, JavaScript, or unrelated refactor is included.
