from collections import Counter
from copy import copy
from datetime import date
import uuid

import pytest

from apps.cancer_ordering.readmodels import resolve_ordering
from apps.cancer_ordering.services import collect_current
from apps.labs.comparison import comparison_view
from apps.labs.dictionary import phase_two_dictionary
from apps.labs.models import LabObservation
from apps.labs.trends import joint_trend_views, trend_summaries
from apps.processing.models import ParsingVersion
from tests.cancer_ordering.test_services import _collect, _select
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def labs(patient):
    dictionary = phase_two_dictionary()
    definitions = {item.code: item for item in dictionary.indicators}
    rows = []
    for code in ('LAB_WBC', 'LAB_CEA', 'LAB_CA19_9', 'LAB_NSE', 'LAB_CYFRA21_1', 'LAB_PROGRP'):
        definition = definitions[code]
        for day, value in ((date(2026, 7, 1), '4'), (date(2026, 8, 1), '5')):
            _, observation = _observation(patient, day, value, code=code, standard_name=definition.standard_name,
                raw_name=definition.standard_name, raw_unit=definition.unit_forms[0] if definition.unit_forms else '')
            observation.dictionary_version = dictionary.version
            observation.save(update_fields=['dictionary_version'])
            ParsingVersion.objects.filter(pk=observation.parsing_version_id).update(
                dictionary_version=dictionary.version, dictionary_hash=dictionary.content_hash)
            rows.append(observation)
    return rows


def row_key(row):
    return row.standard_code, row.category, row.basis_label


@pytest.mark.parametrize('profile,codes', [
    ('LUNG', ['LAB_CEA', 'LAB_CYFRA21_1', 'LAB_NSE', 'LAB_PROGRP']),
    ('PANCREAS', ['LAB_CA19_9', 'LAB_CEA']),
])
def test_comparison_reorders_complete_groups_without_changing_cells_or_calculations(django_user_model, profile, codes):
    _, patient = _patient(django_user_model, 'cancer-labs-' + profile)
    rows = labs(patient)
    # Separate specimen/method groups, same-column duplicates and nonnumeric,
    # unknown indicators all remain real rows of the existing comparison.
    duplicate = copy(rows[2])
    duplicate.pk, duplicate.raw_value, duplicate.reading_order = uuid.uuid4(), '5.5', 3
    duplicate.save(force_insert=True)
    _observation(patient, date(2026, 8, 1), '<3', code='LAB_CEA', raw_unit='ng/mL', method='另一方法', result_type='COMPARATOR')
    _observation(patient, date(2026, 8, 1), '溶血', code='UNKNOWN_SYNTHETIC', result_type='STATUS')
    _collect(patient)
    _select(patient, 'GENERAL')
    baseline = comparison_view(patient)
    values_before = list(LabObservation.objects.order_by('pk').values())
    _select(patient, 'MANUAL_PROFILE', profile=profile)
    current = comparison_view(patient)
    unique_codes = list(dict.fromkeys(row.standard_code for row in current.rows))
    assert unique_codes[:len(codes)] == codes
    assert current.groups[0].category == 'TUMOR_MARKER'
    assert list(dict.fromkeys(row.standard_code for row in current.groups[0].rows))[:len(codes)] == codes
    assert Counter(map(row_key, current.rows)) == Counter(map(row_key, baseline.rows))
    expected = {row_key(row): row for row in baseline.rows}
    assert all(row == expected[row_key(row)] for row in current.rows)
    assert current.columns == baseline.columns and current.reconciliation == baseline.reconciliation
    assert list(LabObservation.objects.order_by('pk').values()) == values_before
    baseline_groups = {group.category: group for group in baseline.groups}
    assert all(set(map(row_key, group.rows)) == set(map(row_key, baseline_groups[group.category].rows)) for group in current.groups)


def test_auto_ordering_then_conflict_preserves_existing_filtered_comparison(django_user_model):
    _, patient = _patient(django_user_model, 'cancer-labs-auto')
    labs(patient)
    _collect(patient)
    automatic = comparison_view(patient)
    assert automatic.rows[0].standard_code == 'LAB_CEA'
    _select(patient, 'GENERAL')
    baseline = comparison_view(patient, start=date(2026, 8, 1), end=date(2026, 8, 1), project='LAB_WBC')
    _select(patient, 'AUTO')
    assert comparison_view(patient, start=date(2026, 8, 1), end=date(2026, 8, 1), project='LAB_WBC') == baseline
    parsed_facts(patient, ['出院诊断：胰腺癌。'])
    assert resolve_ordering(patient)['reason'] == 'collection_incomplete'
    collect_current(patient, actor=patient.account)
    assert resolve_ordering(patient)['reason'] == 'reported_diagnoses_differ'
    conflict = comparison_view(patient)
    _select(patient, 'GENERAL')
    assert comparison_view(patient) == conflict


@pytest.mark.parametrize('profile,first,label', [('LUNG', 'LAB_CEA', '肺癌指标顺序'), ('PANCREAS', 'LAB_CA19_9', '胰腺癌指标顺序')])
def test_trend_summaries_and_selector_prioritize_but_joint_graphs_keep_explicit_order(django_user_model, profile, first, label):
    client, patient = _patient(django_user_model, 'cancer-labs-trend-' + profile)
    labs(patient)
    _observation(patient, date(2026, 9, 1), '6')
    _collect(patient)
    _select(patient, 'GENERAL')
    baseline = trend_summaries(patient)
    assert baseline[0].standard_code == 'LAB_WBC'
    explicit = ('LAB_WBC', 'LAB_CA19_9', 'LAB_CEA')
    graphs_before = joint_trend_views(patient, explicit)
    _select(patient, 'MANUAL_PROFILE', profile=profile)
    summaries = trend_summaries(patient)
    codes = [row.standard_code for row in summaries]
    assert codes[0] == first
    assert set(codes) == {'LAB_CEA', 'LAB_CA19_9', 'LAB_WBC'}
    # The shipped dictionary has no unit rules for the remaining lung-profile
    # codes. Sorting must not invent eligibility for those real comparison rows.
    assert {row.standard_code: row for row in summaries} == {row.standard_code: row for row in baseline}
    graphs_after = joint_trend_views(patient, explicit)
    assert graphs_after == graphs_before
    assert tuple(graph.standard_code for graph in graphs_after[0]) == explicit
    index = client.get('/trends/')
    assert [row.standard_code for row in index.context['trends']] == codes
    joint = client.get('/trends/compare/', {'code': list(explicit)})
    assert joint.status_code == 200
    assert [value for value, _ in joint.context['form'].fields['code'].choices] == codes
    assert tuple(graph.standard_code for graph in joint.context['trends']) == explicit
    assert label in joint.content.decode() and '/cancer-ordering/' in index.content.decode()


@pytest.mark.parametrize('path', ['/labs/compare/', '/trends/', '/trends/compare/'])
def test_source_change_during_actual_ordered_response_rejects_initial_body(django_user_model, monkeypatch, path):
    from apps.documents.views import records
    from apps.labs import views

    client, patient = _patient(django_user_model, 'cancer-labs-response-' + path)
    labs(patient)
    _collect(patient)
    module = views if path.startswith('/labs/') else records
    original = module.render
    def add_source(*args, **kwargs):
        response = original(*args, **kwargs)
        parsed_facts(patient, ['出院诊断：胰腺癌。'])
        return response
    monkeypatch.setattr(module, 'render', add_source)
    response = client.get(path)
    assert response.status_code == 409
    assert b'LAB_' not in response.content and '白细胞' not in response.content.decode()
