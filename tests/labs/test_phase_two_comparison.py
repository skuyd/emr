from copy import copy
from datetime import date
from decimal import Decimal
import uuid

import pytest

from apps.labs.revisions import revise_observation
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.labs.test_phase_two_workflows import _new_version

pytestmark = pytest.mark.django_db


def row(patient, day=date(2026, 8, 20), value="5.2", **kwargs):
    document, observation = _observation(patient, day, value, **kwargs)
    observation.specimen = "BLOOD"
    observation.save(update_fields=["specimen"])
    return document, observation


@pytest.mark.parametrize('value,factor', [
    ('5.2', '1e1000000'), ('9e1000', '1e1000'), ('1e-1000', '1e-1000'),
])
def test_unusable_legacy_conversion_or_product_is_explicitly_uncertain(django_user_model, monkeypatch, value, factor):
    from apps.labs.comparison import comparable_cell
    _, patient = _patient(django_user_model, 'conversion-limits')
    _, observation = row(patient, value=value)
    rule = {'id': 'synthetic-limits', 'version': 'fixture', 'kind': 'conversion', 'code': 'LAB_WBC',
            'specimen': 'BLOOD', 'method': '合成方法A', 'source_unit': '10^9/L', 'target_unit': '10^9/L',
            'factor': factor, 'reviewed_by': 'fixture', 'rationale': 'synthetic', 'evidence': 'synthetic'}
    monkeypatch.setattr('apps.labs.comparison.rules_for_version', lambda version: (rule,))
    cell = comparable_cell(observation)
    assert cell.comparability == 'insufficient' and not cell.trend_eligible
    assert 'numeric_unsupported' in {item['code'] for item in cell.quality_issues}
    assert cell.observation.raw_value == value
    assert cell.rule is None and cell.unit == observation.raw_unit


def test_chart_calculations_do_not_depend_on_callers_decimal_context(django_user_model):
    from decimal import localcontext
    from apps.labs.trends import trend_view
    _, patient = _patient(django_user_model, 'numeric-context')
    row(patient, date(2026, 7, 1), '-9e1000')
    row(patient, date(2026, 8, 1), '9e1000')
    with localcontext() as context:
        context.Emax, context.Emin = 2, -2
        result = trend_view(patient, 'LAB_WBC')
    assert result is not None
    assert {point.y for point in result.series[0].points} == {14.0, 82.0}


def test_unpublished_snapshot_is_used_for_every_comparison_and_reference_check(django_user_model, monkeypatch):
    from dataclasses import replace
    from apps.labs.comparison import comparable_cell
    from apps.labs.dictionary import default_dictionary
    _, patient = _patient(django_user_model, 'unpublished-snapshot')
    _, observation = row(patient)
    observation.reference_range_raw = '3.5-9.5'
    snapshot = replace(default_dictionary(), version='not-published-yet')
    observation.dictionary_version = snapshot.version
    def no_implicit_lookup(*args):
        raise AssertionError('A snapshot evaluation cannot read the active release')
    for path in ('apps.labs.comparison.dictionary_for_version', 'apps.labs.comparison.rules_for_version',
                 'apps.labs.validation.dictionary_for_version', 'apps.labs.validation.rules_for_version'):
        monkeypatch.setattr(path, no_implicit_lookup)
    rules = [{'id': 'synthetic-snapshot', 'version': 'fixture', 'kind': 'conversion', 'code': 'LAB_WBC',
              'specimen': 'BLOOD', 'method': '合成方法A', 'source_unit': '10^9/L', 'target_unit': '10^9/L',
              'factor': '2', 'reviewed_by': 'fixture', 'rationale': 'synthetic', 'evidence': 'synthetic'}]
    cell = comparable_cell(observation, dictionary=snapshot, rules=rules)
    assert cell.numeric_value == Decimal('10.4')
    assert cell.rule['id'] == rules[0]['id']
    assert cell.reference_label == '范围内'


def test_table_preserves_same_day_reports_duplicates_specials_and_unknown_dates(django_user_model):
    from apps.labs.comparison import comparison_view

    _, patient = _patient(django_user_model, "comparison")
    _, first = row(patient)
    _, second = row(patient, value="<6", result_type="COMPARATOR")
    _, unknown = row(patient, value="溶血", result_type="STATUS")
    unknown.observation_date = None
    unknown.save(update_fields=["observation_date"])
    duplicate = copy(first)
    duplicate.pk, duplicate.reading_order, duplicate.raw_value = uuid.uuid4(), 2, "5.3"
    duplicate.save(force_insert=True)
    view = comparison_view(patient)
    assert len(view.columns) == 3
    assert any(column.date_label == "日期未识别" for column in view.columns)
    values = [cell for group in view.rows for entries in group.cells for cell in entries]
    assert {cell.observation.raw_value for cell in values} == {"5.2", "5.3", "<6", "溶血"}
    assert any(len(entries) == 2 for group in view.rows for entries in group.cells)
    assert all(not cell.trend_eligible for cell in values if cell.observation.pk in {second.pk, unknown.pk})
    assert all(cell.reference_label == "无法对照" for cell in values)


def test_grouping_requires_specimen_known_unit_method_and_quality(django_user_model):
    from apps.labs.comparison import comparison_view

    _, patient = _patient(django_user_model, "grouping")
    for field, value in (("specimen", ""), ("raw_unit", "unknown"), ("method_raw", "")):
        _, observation = row(patient)
        setattr(observation, field, value)
        observation.save(update_fields=[field])
    _, good = row(patient)
    good.reference_range_raw = "4-10"
    good.save(update_fields=["reference_range_raw"])
    view = comparison_view(patient)
    values = [cell for group in view.rows for entries in group.cells for cell in entries]
    assert sum(cell.comparability == "direct" for cell in values) == 1
    assert next(cell for cell in values if cell.observation.pk == good.pk).reference_label == "范围内"


def test_effective_fields_drive_comparison_archive_detail_and_trend_filters(django_user_model):
    from apps.labs.comparison import comparison_view
    from apps.labs.trends import trend_view
    from apps.documents.archive import records_context

    client, patient = _patient(django_user_model, "effective-reads")
    document, first = row(patient, date(2026, 7, 1), "62", code="CANDIDATE_UNKNOWN")
    row(patient, date(2026, 9, 2), "7.2")
    revise_observation(patient.account, first.pk, action="CORRECT", expected_revision=0,
                       changes={"raw_value": "6.2", "standard_code": "LAB_WBC", "observation_date": "2026-09-01"})
    view = comparison_view(patient, start=date(2026, 9, 1), project="LAB_WBC")
    assert len(view.columns) == 2
    assert "6.2" in client.get(f"/records/{document.pk}/").content.decode()
    context = records_context(patient, {"q": "6.2", "year": "2026", "month": "9"})
    assert context["page_obj"].paginator.count == 1
    assert records_context(patient, {"q": "2026-09-01"})["page_obj"].paginator.count == 1
    trend = trend_view(patient, "LAB_WBC")
    assert trend is not None
    assert [point.numeric_value for point in trend.series[0].points] == [Decimal("6.2"), Decimal("7.2")]


def test_unmatched_human_revision_remains_visible_after_reparse(django_user_model):
    from apps.labs.comparison import comparison_view

    client, patient = _patient(django_user_model, "reconciliation")
    document, original = row(patient)
    revise_observation(patient.account, original.pk, action="CORRECT", changes={"raw_value": "6.7"}, expected_revision=0)
    new = _new_version(document, original)
    new.standard_code = "LAB_HGB"
    new.evidence.polygon = [[0.2, 0.5], [0.8, 0.5], [0.8, 0.6], [0.2, 0.6]]
    new.evidence.save(update_fields=["polygon"])
    new.save(update_fields=["standard_code"])
    view = comparison_view(patient)
    assert view.reconciliation[0].raw_value == "6.7"
    assert "6.7" in client.get(f"/records/{document.pk}/").content.decode()


def test_reviewed_conversion_preserves_raw_and_rule_versions_and_drives_same_trend(django_user_model):
    import json
    from django.utils import timezone
    from apps.labs.comparison import comparison_view
    from apps.labs.dictionary import default_dictionary, load_dictionary_content, rules_digest, release_digest
    from apps.labs.trends import trend_view
    from apps.operations.models import DictionaryRelease

    client, patient = _patient(django_user_model, "conversion")
    payload = json.loads(default_dictionary().source_path.read_text(encoding="utf-8"))
    definition = next(item for item in payload["indicators"] if item["code"] == "LAB_WBC")
    definition["unit_forms"].append("cells/uL")
    payload["dictionary_version"] = "conversion-tested"
    dictionary = load_dictionary_content(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    rule = {"id": "wbc-conversion", "version": "rule-1", "kind": "conversion", "code": "LAB_WBC", "specimen": "BLOOD",
            "method": "合成方法A", "source_unit": "cells/uL", "target_unit": "10^9/L", "factor": "0.001",
            "reviewed_by": "fixture-reviewer", "rationale": "人工审核量纲", "evidence": "固定换算用例"}
    DictionaryRelease.objects.create(version=dictionary.version, content_hash=dictionary.content_hash, artifact_name="",
                                     indicator_count=len(dictionary.indicators), payload=payload, rules=[rule], published_at=timezone.now(),
                                     rules_hash=rules_digest([rule]), release_hash=release_digest(dictionary.content_hash, [rule]))
    _, first = row(patient, date(2026, 7, 1), "5200", raw_unit="cells/uL")
    _, second = row(patient, date(2026, 8, 1), "5.4")
    for observation in (first, second):
        observation.dictionary_version = dictionary.version
        observation.save(update_fields=["dictionary_version"])
    view = comparison_view(patient)
    assert len(view.rows) == 1
    converted = view.rows[0].cells[0][0]
    assert converted.comparability == "converted"
    assert converted.numeric_value == Decimal("5.2")
    assert converted.observation.raw_value == "5200"
    assert converted.rule["version"] == "rule-1"
    assert len(trend_view(patient, "LAB_WBC").series) == 1
    content = client.get("/trends/LAB_WBC/").content.decode()
    assert "wbc-conversion" in content and "rule-1" in content
    assert "5200" in content and "5.200" in content
    assert f"/labs/observations/{first.pk}/source/raw_value/" in content
    assert "系统不做单位换算" not in content


def test_comparison_includes_low_confidence_rows_as_uncertain_not_silent_loss(django_user_model):
    from apps.labs.comparison import comparison_view

    _, patient = _patient(django_user_model, "low-confidence-visible")
    _, observation = row(patient)
    observation.evidence.confidence = "0.20"
    observation.evidence.save(update_fields=["confidence"])
    view = comparison_view(patient)
    assert len(view.rows) == 1
    cell = view.rows[0].cells[0][0]
    assert cell.comparability == "insufficient"
    assert not cell.trend_eligible


def test_reconciliation_does_not_resurface_earlier_revisions_already_carried_forward(django_user_model):
    from apps.labs.comparison import comparison_view

    _, patient = _patient(django_user_model, "reconciliation-chain")
    document, original = row(patient)
    revise_observation(patient.account, original.pk, action="CORRECT", changes={"raw_value": "6.7"}, expected_revision=0)
    newer = _new_version(document, original, raw_value="5.2")
    revise_observation(patient.account, newer.pk, action="CORRECT", changes={"raw_value": "6.8"}, expected_revision=0)
    latest = _new_version(document, newer, raw_value="5.2")
    assert comparison_view(patient).reconciliation == ()


def test_same_document_distinct_exam_dates_have_independent_columns(django_user_model):
    from apps.labs.comparison import comparison_view
    from apps.documents.archive import records_context

    _, patient = _patient(django_user_model, "multi-exam-document")
    _, original = row(patient)
    later = copy(original)
    later.pk, later.reading_order = uuid.uuid4(), 2
    later.observation_date = date(2026, 8, 21)
    later.save(force_insert=True)
    assert len(comparison_view(patient).columns) == 2
    revise_observation(patient.account, later.pk, action="CORRECT", expected_revision=0, changes={"observation_date": "2026-09-01"})
    assert records_context(patient, {"month": "8"})["page_obj"].paginator.count == 1
    assert records_context(patient, {"month": "9"})["page_obj"].paginator.count == 1


def test_internal_validation_consistent_between_comparison_detail_and_trends(django_user_model):
    from django.utils import timezone
    from apps.labs.comparison import comparison_view
    from apps.labs.dictionary import default_dictionary, release_digest, rules_digest
    from apps.labs.trends import trend_view
    from apps.operations.models import DictionaryRelease

    client, patient = _patient(django_user_model, "internal-consistent-reads")
    rule = dict(id="fixture-sum", version="rule-1", kind="report_sum", code="LAB_WBC",
                specimen="BLOOD", method="合成方法A", unit="10^9/L", component_codes=["LAB_NEUT_COUNT", "LAB_LYMPH_COUNT"],
                absolute_tolerance="0", reviewed_by="fixture", rationale="固定合成一致性规则", evidence="fixture")
    baseline = default_dictionary()
    DictionaryRelease.objects.create(version=baseline.version, content_hash=baseline.content_hash,
                                     artifact_name=baseline.source_path.name, indicator_count=len(baseline.indicators),
                                     rules=[rule], rules_hash=rules_digest([rule]), release_hash=release_digest(baseline.content_hash, [rule]), published_at=timezone.now())
    for day in (date(2026, 8, 20), date(2026, 8, 21)):
        document, original = row(patient, day, "5")
        original.reference_range_raw = "4-10"
        original.save(update_fields=["reference_range_raw"])
        for order, code, value in ((2, "LAB_NEUT_COUNT", "6"), (3, "LAB_LYMPH_COUNT", "1")):
            component = copy(original)
            component.pk, component.reading_order, component.standard_code, component.raw_value = uuid.uuid4(), order, code, value
            component.save(force_insert=True)
    view = comparison_view(patient)
    wbc = next(group for group in view.rows if group.standard_code == "LAB_WBC")
    assert all(not cell.trend_eligible for entries in wbc.cells for cell in entries)
    assert all(cell.reference_label == "无法对照" for entries in wbc.cells for cell in entries)
    assert trend_view(patient, "LAB_WBC") is None
    detail = client.get(f"/records/{document.pk}/")
    assert "报告内部不一致" in detail.content.decode()
    assert "报告内部不一致" in client.get(f"/labs/observations/{original.pk}/").content.decode()
