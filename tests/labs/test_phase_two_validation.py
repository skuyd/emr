from datetime import date

import pytest

from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('value,kind', [
    ('1e1000000', 'NUMERIC'), ('1e-1000000', 'NUMERIC'),
    ('>1e100000000000000000000', 'COMPARATOR'),
])
def test_unsupported_numbers_keep_raw_but_cannot_break_reference_or_trends(django_user_model, value, kind):
    from apps.labs.comparison import comparable_cell
    from apps.labs.revisions import effective_observation, revise_observation
    from apps.labs.trends import trend_view
    from apps.labs.validation import reference_comparison, validate_observation
    _client, patient = _patient(django_user_model, 'p2-numeric-limits')
    _observation(patient, date(2026, 8, 1), '1')
    _document, row = _observation(patient, date(2026, 8, 20), '5.2')
    row.specimen, row.reference_range_raw = 'BLOOD', '3.5-9.5'
    row.save()
    revise_observation(patient.account, row.pk, action='CORRECT', changes={'raw_value': value}, expected_revision=0)
    row.refresh_from_db()
    effective = effective_observation(row)
    assert effective.raw_value == value and effective.result_type == kind
    assert 'numeric_unsupported' in {item['code'] for item in validate_observation(effective)}
    assert reference_comparison(effective)['status'] == 'unavailable'
    assert not comparable_cell(effective).trend_eligible
    assert trend_view(patient, 'LAB_WBC') is None


def test_unrepresentable_reference_bound_is_not_an_unbounded_valid_range(django_user_model):
    from apps.labs.validation import parse_reference_range, reference_comparison
    _client, patient = _patient(django_user_model, 'p2-reference-limits')
    _document, row = _observation(patient, date(2026, 8, 20), '5.2')
    row.reference_range_raw = '<1e100000000000000000000'
    assert parse_reference_range(row.reference_range_raw)['kind'] == 'unknown'
    assert reference_comparison(row)['status'] == 'unavailable'


def test_kept_fields_retain_original_association_issues_until_explicit_review(django_user_model):
    from apps.labs.comparison import comparable_cell
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import effective_observation, revise_observation
    from apps.labs.validation import validate_observation
    from tests.labs.test_phase_two_workflows import _new_version, _reviewer
    _client, patient = _patient(django_user_model, 'p2-inherited-layout')
    document, row = _observation(patient, date(2026, 8, 20), '62')
    row.specimen = 'BLOOD'
    row.quality_issues = [{'code': 'association_conflict', 'fields': ['raw_value'], 'rule_version': 'layout-v2'}]
    row.save()
    revise_observation(patient.account, row.pk, action='DEFER', changes={}, expected_revision=0)
    newer = _new_version(document, row)
    newer.quality_issues = []
    newer.save()
    newer.parsing_version.diagnostics = row.parsing_version.diagnostics
    newer.parsing_version.save(update_fields=['diagnostics'])
    revise_observation(patient.account, newer.pk, action='KEEP_REVISION', changes={}, expected_revision=0)
    newer.refresh_from_db()
    effective = effective_observation(newer)
    assert effective.raw_value == '62'
    assert 'association_conflict' in {item['code'] for item in validate_observation(effective)}
    assert not comparable_cell(effective).trend_eligible
    reviewer = _reviewer(django_user_model)
    task = create_review_task(patient.account, newer.pk, reviewer=reviewer)
    transition_review_task(reviewer, task.pk, action='START', expected_revision=0)
    transition_review_task(reviewer, task.pk, action='CONFIRM', expected_revision=1, resolved_issues=['association_conflict'])
    newer.refresh_from_db()
    assert 'association_conflict' not in {item['code'] for item in validate_observation(effective_observation(newer))}
    revise_observation(patient.account, newer.pk, action='UNDO', changes={}, expected_revision=2)
    newer.refresh_from_db()
    assert 'association_conflict' in {item['code'] for item in validate_observation(effective_observation(newer))}
    revise_observation(patient.account, newer.pk, action='UNDO', changes={}, expected_revision=3)
    newer.refresh_from_db()
    assert 'association_conflict' not in {item['code'] for item in validate_observation(effective_observation(newer))}


@pytest.mark.parametrize('reason,changes', [
    ('unit_unknown', {'raw_unit': '10^9/L'}), ('mapping_unknown', {'standard_code': 'LAB_WBC'}),
    ('recognition_uncertain', None),
])
def test_effective_corrections_recompute_derived_gates_and_dictionary_capability(django_user_model, reason, changes):
    from apps.labs.comparison import comparable_cell
    from apps.labs.review import create_review_task, transition_review_task
    from apps.labs.revisions import effective_observation, revise_observation
    from tests.labs.test_phase_two_workflows import _reviewer
    _client, patient = _patient(django_user_model, 'p2-recovered-candidate')
    _document, row = _observation(patient, date(2026, 8, 20), '5.2')
    row.specimen, row.capability_level = 'BLOOD', 'SEARCH_ONLY'
    row.quality_issues = [{'code': reason, 'fields': ['raw_unit' if reason == 'unit_unknown' else 'raw_name']}]
    if reason == 'unit_unknown':
        row.raw_unit = ''
    if reason == 'mapping_unknown':
        row.standard_code = 'CANDIDATE_SYNTHETIC'
    if reason == 'recognition_uncertain':
        row.evidence.confidence = '0.5000'
        row.evidence.save()
    row.save()
    assert not comparable_cell(effective_observation(row)).trend_eligible
    if changes:
        revise_observation(patient.account, row.pk, action='CORRECT', changes=changes, expected_revision=0)
    else:
        reviewer = _reviewer(django_user_model)
        task = create_review_task(patient.account, row.pk, reviewer=reviewer)
        transition_review_task(reviewer, task.pk, action='START', expected_revision=0)
        transition_review_task(reviewer, task.pk, action='CONFIRM', expected_revision=1, resolved_issues=[reason])
    row.refresh_from_db()
    effective = effective_observation(row)
    cell = comparable_cell(effective)
    assert reason not in {item['code'] for item in cell.quality_issues}
    assert effective.capability_level == 'STABLE' and cell.trend_eligible
    revise_observation(patient.account, row.pk, action='UNDO', changes={}, expected_revision=1)
    row.refresh_from_db()
    assert not comparable_cell(effective_observation(row)).trend_eligible


def test_validation_uses_historical_mapping_dictionary_and_allowed_value_types(django_user_model):
    from apps.labs.validation import validate_observation

    _client, patient = _patient(django_user_model, "p2-versioned-validation")
    _document, row = _observation(patient, date(2026, 8, 20), "0.20")
    row.raw_unit = "%"
    row.standard_code = "LAB_PLATELETCRIT"
    row.mapping_dictionary_version = "phase-two-1"
    assert "mapping_unknown" not in {i["code"] for i in validate_observation(row)}
    row.standard_code = "LAB_HGB"
    row.raw_value, row.result_type, row.raw_unit = "阴性", "QUALITATIVE", "g/L"
    assert "type_conflict" in {i["code"] for i in validate_observation(row)}
    row.mapping_dictionary_version = "absent-version"
    assert "mapping_unknown" in {i["code"] for i in validate_observation(row)}


def test_explicit_specimen_conflict_cannot_enter_reference_or_trend(django_user_model):
    from apps.labs.validation import reference_comparison, validate_observation
    _client, patient = _patient(django_user_model, 'p2-specimen-conflict')
    _document, row = _observation(patient, date(2026, 8, 20), '130')
    row.standard_code, row.raw_unit, row.reference_range_raw = 'LAB_HGB', 'g/L', '115-150'
    row.mapping_dictionary_version, row.specimen = 'phase-two-1', 'URINE'
    assert 'specimen_conflict' in {item['code'] for item in validate_observation(row)}
    assert reference_comparison(row)['status'] == 'unavailable'


def test_contextual_numeric_project_without_specimen_or_reviewed_unit_is_uncertain(django_user_model):
    from apps.labs.validation import validate_observation
    _client, patient = _patient(django_user_model, 'p2-missing-unit-definition')
    _document, row = _observation(patient, date(2026, 8, 20), '1')
    row.standard_code, row.raw_unit, row.specimen = 'LAB_CA', '', ''
    row.mapping_dictionary_version = 'phase-two-1'
    codes = {item['code'] for item in validate_observation(row)}
    assert {'specimen_unknown', 'unit_unknown'} <= codes
    row.raw_unit, row.specimen = 'synthetic/L', 'BLOOD'
    assert 'unit_unknown' in {item['code'] for item in validate_observation(row)}


def test_inherited_value_uses_its_own_source_confidence(django_user_model):
    from apps.labs.revisions import effective_observation, revise_observation
    from apps.labs.validation import validate_observation
    from tests.labs.test_phase_two_workflows import _new_version

    _client, patient = _patient(django_user_model, "p2-carried-quality")
    document, row = _observation(patient, date(2026, 8, 20), "62")
    row.evidence.confidence = "0.8200"
    row.evidence.save()
    revise_observation(patient.account, row.pk, action="CORRECT", changes={"raw_value": "6.2"}, expected_revision=0)
    newer = _new_version(document, row)
    revise_observation(patient.account, newer.pk, action="KEEP_REVISION", changes={}, expected_revision=0)
    newer.refresh_from_db()
    assert "recognition_uncertain" in {i["code"] for i in validate_observation(effective_observation(newer))}


def test_validation_preserves_reliable_controls_and_explains_conflicts(django_user_model):
    from apps.labs.validation import validate_observation

    _client, patient = _patient(django_user_model, "p2-validation")
    _document, row = _observation(patient, date(2026, 8, 20), "5.2")
    row.reference_range_raw = "3.5-9.5"
    assert not validate_observation(row)
    row.quality_issues = [{"code": "association_conflict", "fields": ["raw_name", "raw_value"], "rule_version": "layout-v2", "details": "跨栏关联无法确认"}]
    row.raw_unit = "mg/dL"
    row.observation_date = date(2026, 8, 21)
    codes = {item["code"] for item in validate_observation(row)}
    assert {"association_conflict", "unit_unknown", "date_conflict"} <= codes


def test_reference_interpretation_uses_only_explicit_report_range(django_user_model):
    from apps.labs.validation import reference_comparison

    _client, patient = _patient(django_user_model, "p2-reference")
    _document, row = _observation(patient, date(2026, 8, 20), "5.2")
    assert reference_comparison(row)["label"] == "无法对照"
    row.reference_range_raw = "3.5-9.5"
    assert reference_comparison(row)["label"] == "范围内"
    row.raw_value = "2.0"
    assert reference_comparison(row)["label"] == "低于"
    row.raw_value = "10.0"
    assert reference_comparison(row)["label"] == "高于"
    row.reference_range_raw = "9.5-3.5"
    assert reference_comparison(row)["label"] == "无法对照"


@pytest.mark.parametrize("value,kind,reference,label", [
    ("<3", "COMPARATOR", "3.5-9.5", "低于"),
    ("<5", "COMPARATOR", "3.5-9.5", "无法对照"),
    ("阴性", "QUALITATIVE", "阴性", "范围内"),
    ("阳性", "QUALITATIVE", "阴性", "与参考不一致"),
    ("++", "SEMI_QUANTITATIVE", "-", "与参考不一致"),
    ("溶血", "STATUS", "3.5-9.5", "无法对照"),
])
def test_special_values_keep_their_own_semantics(django_user_model, value, kind, reference, label):
    from apps.labs.validation import reference_comparison

    _client, patient = _patient(django_user_model, "p2-reference-special")
    _document, row = _observation(patient, date(2026, 8, 20), value, result_type=kind)
    row.reference_range_raw = reference
    assert reference_comparison(row)["label"] == label


def test_history_rules_require_review_and_comparable_sources(django_user_model):
    from apps.labs.validation import validate_observation

    _client, patient = _patient(django_user_model, "p2-history")
    _doc, previous = _observation(patient, date(2026, 8, 1), "5.2")
    _doc, current = _observation(patient, date(2026, 8, 20), "52")
    rule = dict(id="synthetic-decimal-shift", version="fixture-v1", kind="history_ratio", code="LAB_WBC",
                unit="10^9/L", minimum_ratio="10", specimen="BLOOD", method="合成方法A",
                reviewed_by="isolated-reviewer", rationale="合成验收规则，不用于生产默认校验")
    previous.specimen = current.specimen = "BLOOD"
    assert "magnitude_suspect" not in {i["code"] for i in validate_observation(current, previous=(previous,))}
    issues = validate_observation(current, previous=(previous,), rules=(rule,))
    assert "magnitude_suspect" in {i["code"] for i in issues}
    assert all(i["rule_version"] for i in issues)
    previous.raw_unit = "mg/dL"
    assert "magnitude_suspect" not in {i["code"] for i in validate_observation(current, previous=(previous,), rules=(rule,))}


def test_history_keeps_each_older_observations_own_unit_definition(django_user_model):
    from dataclasses import replace
    from apps.labs.dictionary import default_dictionary
    from apps.labs.validation import validate_observation
    _client, patient = _patient(django_user_model, 'historical-rule-snapshot')
    _, previous = _observation(patient, date(2026, 8, 1), '5.2', raw_unit='synthetic/L')
    _, current = _observation(patient, date(2026, 8, 20), '52', raw_unit='synthetic/L')
    previous.specimen = current.specimen = 'BLOOD'
    baseline = default_dictionary()
    snapshot = replace(baseline, version='unpublished-history', indicators=tuple(
        replace(item, unit_forms=(*item.unit_forms, 'synthetic/L')) if item.code == 'LAB_WBC' else item
        for item in baseline.indicators))
    current.mapping_dictionary_version = snapshot.version
    rule = dict(id='synthetic-history-version', version='1', kind='history_ratio', code='LAB_WBC',
                unit='synthetic/L', minimum_ratio='10', specimen='BLOOD', method='合成方法A',
                reviewed_by='fixture', rationale='synthetic')
    assert 'unit_unknown' in {item['code'] for item in validate_observation(previous)}
    assert 'magnitude_suspect' not in {item['code'] for item in validate_observation(
        current, previous=(previous,), dictionary=snapshot, rules=(rule,))}


def test_review_cannot_resolve_unrelated_or_unverified_source_reasons(django_user_model):
    from apps.labs.revisions import effective_observation
    from apps.labs.validation import validate_observation

    _client, patient = _patient(django_user_model, "p2-source")
    _document, row = _observation(patient, date(2026, 8, 20), "5.2")
    row.evidence.confidence = "0.8300"
    row.evidence.save()
    effective = effective_observation(row)
    codes = {i["code"] for i in validate_observation(effective)}
    assert "recognition_uncertain" in codes


def test_internal_sum_rule_only_runs_for_reviewed_complete_same_report(django_user_model):
    from copy import copy
    import uuid
    from apps.labs.validation import validate_observation

    _client, patient = _patient(django_user_model, "p2-internal")
    _document, row = _observation(patient, date(2026, 8, 20), "5")
    row.specimen = "BLOOD"
    other = copy(row)
    other.pk = uuid.uuid4()
    other.standard_code = "LAB_NEUT_COUNT"
    other.raw_value = "6"
    rule = dict(id="synthetic-row-sum", version="fixture-v1", kind="report_sum", code="LAB_WBC",
                component_codes=["LAB_NEUT_COUNT"], unit="10^9/L", specimen="BLOOD", method="合成方法A",
                absolute_tolerance="0", reviewed_by="isolated-reviewer", rationale="固定合成转录一致性规则")
    assert "internal_conflict" in {i["code"] for i in validate_observation(row, previous=(other,), rules=(rule,))}
    assert "internal_conflict" not in {i["code"] for i in validate_observation(row, rules=(rule,))}
    other.raw_value = "5"
    assert "internal_conflict" not in {i["code"] for i in validate_observation(row, previous=(other,), rules=(rule,))}
    other.raw_value = "6"
    assert "internal_conflict" not in {i["code"] for i in validate_observation(row, previous=(other,), rules=({**rule, "reviewed_by": ""},))}


def test_internal_rule_can_use_an_explicitly_reviewed_component(django_user_model):
    from copy import copy
    import uuid
    from apps.labs.validation import validate_observation
    _client, patient = _patient(django_user_model, 'internal-reviewed-component')
    _, total = _observation(patient, date(2026, 8, 20), '5')
    total.specimen = 'BLOOD'
    component = copy(total)
    component.pk, component.raw_value = uuid.uuid4(), '6'
    component.standard_code = 'LAB_NEUT_COUNT'
    component.quality_issues = [{'code': 'association_conflict', 'fields': ['raw_value']}]
    rule = dict(id='synthetic-reviewed-sum', version='1', kind='report_sum', code='LAB_WBC',
                component_codes=['LAB_NEUT_COUNT'], unit='10^9/L', specimen='BLOOD', method='合成方法A',
                absolute_tolerance='0', reviewed_by='fixture', rationale='synthetic')
    assert 'internal_conflict' not in {item['code'] for item in validate_observation(total, previous=(component,), rules=(rule,))}
    component.resolved_issues = ['association_conflict']
    assert 'internal_conflict' in {item['code'] for item in validate_observation(total, previous=(component,), rules=(rule,))}
