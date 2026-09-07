from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from apps.labs.comparison import comparison_view
from apps.labs.revisions import revise_observation
from apps.labs.trends import trend_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def observations(patient, values=('2', '4', '6', '12'), **kwargs):
    return [_observation(patient, date(2026, 8, i + 1), value, **kwargs)[1] for i, value in enumerate(values)]


def cells(view):
    return [cell for row in view.rows for entries in row.cells for cell in entries]


def test_effective_comparison_and_trend_share_the_same_baseline_and_sources(django_user_model):
    _, patient = _patient(django_user_model, 'personal-baseline')
    rows = observations(patient)
    table = cells(comparison_view(patient))[-1].change
    chart = trend_view(patient, 'LAB_WBC').series[0].points[-1].change
    for result in (table, chart):
        assert result.baseline_mean == Decimal('4') and result.baseline_percentage == Decimal('200')
        assert [c.observation.pk for c in result.baseline] == [row.pk for row in rows[:3]]
        assert result.previous.observation.pk == rows[-2].pk
        assert result.previous_percentage == Decimal('100')


def test_date_filter_keeps_referenced_prior_baseline_outside_visible_columns(django_user_model):
    _, patient = _patient(django_user_model, 'filtered-baseline')
    observations(patient)
    view = comparison_view(patient, start=date(2026, 8, 4))
    assert len(view.columns) == 1
    assert cells(view)[0].change.baseline_mean == Decimal('4')
    assert len(cells(view)[0].change.baseline) == 3


def test_correction_recomputes_baseline_and_reported_error_removes_its_eligibility(django_user_model):
    _, patient = _patient(django_user_model, 'revised-baseline')
    rows = observations(patient)
    revise_observation(patient.account, rows[0].pk, action='CORRECT', changes={'raw_value': '8'}, expected_revision=0)
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean == Decimal('6') and change.baseline_percentage == Decimal('100')
    assert rows[0].raw_value == '2'
    revise_observation(patient.account, rows[0].pk, action='REPORT_ERROR', changes={}, expected_revision=1)
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean is None and change.baseline_reason


def test_actual_quality_and_method_exclusions_do_not_supply_three_point_baseline(django_user_model):
    _, patient = _patient(django_user_model, 'quality-baseline')
    rows = observations(patient)
    rows[0].evidence.confidence = Decimal('0.2')
    rows[0].evidence.save(update_fields=['confidence'])
    _observation(patient, date(2026, 8, 2), '999', method='合成方法B')
    current = next(cell for cell in cells(comparison_view(patient)) if cell.observation.pk == rows[-1].pk)
    assert current.change.baseline_mean is None
    assert current.change.previous_percentage == Decimal('100')


def test_tumor_markers_use_fifty_percent_descriptive_boundary_from_dictionary(django_user_model):
    _, patient = _patient(django_user_model, 'tumor-baseline')
    observations(patient, ('4', '4', '4', '6'), code='LAB_CEA', standard_name='癌胚抗原', raw_name='CEA', raw_unit='ng/mL')
    current = cells(comparison_view(patient))[-1]
    assert current.change.threshold_percent == 50
    assert current.change.baseline_percentage == Decimal('50') and not current.change.highlight


def test_removed_document_cannot_remain_a_baseline_source(django_user_model):
    from django.utils import timezone
    _, patient = _patient(django_user_model, 'removed-baseline')
    rows = observations(patient)
    document = rows[0].parsing_version.document
    document.deleted_at = timezone.now()
    document.save(update_fields=['deleted_at'])
    assert cells(comparison_view(patient))[-1].change.baseline_mean is None


def test_comparison_exposes_grouped_rows_and_sparkline_points_without_losing_cells(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-sparkline')
    rows = observations(patient)
    view = comparison_view(patient)
    assert sum(len(group.rows) for group in view.groups) == len(view.rows)
    assert [point.observation.pk for point in view.rows[0].sparkline] == [row.pk for row in rows]
    assert len(cells(view)) == 4


def test_multi_indicator_get_has_independent_units_and_patient_scoped_sources(django_user_model):
    client, patient = _patient(django_user_model, 'multi-indicator')
    _, other = _patient(django_user_model, 'multi-indicator-other')
    rows = observations(patient)
    observations(patient, ('100', '120', '110', '130'), code='LAB_HGB', raw_unit='g/L', standard_name='血红蛋白')
    hidden = observations(other, ('900', '999'))
    response = client.get('/trends/compare/', {'code': ['LAB_WBC', 'LAB_HGB'], 'start': '2026-08-01', 'end': '2026-08-04'})
    assert response.status_code == 200
    content = response.content.decode()
    assert '10^9/L' in content and 'g/L' in content
    assert all(str(row.pk) in content for row in rows)
    assert all(str(row.pk) not in content for row in hidden)
    assert '各图使用独立数轴' in content


def test_multi_indicator_filter_validation_and_single_available_point_remain_explicit(django_user_model):
    client, patient = _patient(django_user_model, 'multi-filter')
    observations(patient)
    response = client.get('/trends/compare/', {'code': ['LAB_WBC'], 'start': '2026-09-01', 'end': '2026-08-01'})
    assert response.status_code == 400
    response = client.get('/trends/compare/', {'code': ['LAB_WBC'], 'start': '2026-08-04'})
    assert response.status_code == 200 and '不足两个不同日期的可比结果' in response.content.decode()


@pytest.mark.parametrize('module_name,build_name,path', [
    ('apps.documents.views.records', 'joint_trend_views', '/trends/compare/'),
    ('apps.documents.views.records', 'trend_view', '/trends/LAB_WBC/'),
    ('apps.documents.views.records', 'trend_summaries', '/trends/'),
    ('apps.labs.views', 'comparison_view', '/labs/compare/'),
])
def test_trends_recheck_membership_after_building_the_view(django_user_model, monkeypatch, module_name, build_name, path):
    from importlib import import_module
    from apps.patients.access import change_membership
    from tests.patients.test_family_access import family
    module = import_module(module_name)
    _, patient, client, _, membership = family(django_user_model, 'joint-revoke', 'VIEWER')
    observations(patient)
    original = getattr(module, build_name)
    def revoke_after_build(*args, **kwargs):
        value = original(*args, **kwargs)
        change_membership(patient, patient.account, membership.pk, revoke=True, expected_revision=0)
        return value
    monkeypatch.setattr(module, build_name, revoke_after_build)
    response = client.get(path, {'patient': str(patient.pk), 'code': 'LAB_WBC'})
    assert response.status_code == 403
    assert '较近三次均值' not in response.content.decode()


def test_joint_trend_get_forms_and_baseline_links_keep_explicit_patient_after_switch(django_user_model):
    class PageTags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inputs = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == 'input':
                self.inputs.append(attributes)
            if tag == 'a' and attributes.get('href'):
                self.links.append(attributes['href'])

    client, first = _patient(django_user_model, 'joint-old-tab')
    rows = observations(first)
    assert client.post('/patients/new/', {'display_name': '另一位家人', 'upload_authority': 'on'}).status_code == 302
    response = client.get('/trends/compare/', {'patient': str(first.pk), 'code': 'LAB_WBC'})
    assert response.status_code == 200
    tags = PageTags()
    tags.feed(response.content.decode())
    assert any(field.get('name') == 'patient' and field.get('value') == str(first.pk) for field in tags.inputs)
    source_links = [link for link in tags.links if '/source/raw_value/' in link]
    assert source_links
    for link in source_links:
        source = client.get(link)
        assert source.status_code == 200
        assert source.context['request'].patient.pk == first.pk
    assert str(rows[0].pk) in response.content.decode()


def test_readonly_member_sees_changes_but_cannot_post_new_route_or_revision(django_user_model):
    from tests.patients.test_family_access import family
    _, patient, client, _, _ = family(django_user_model, 'joint-readonly', 'VIEWER')
    rows = observations(patient)
    assert client.get('/trends/compare/', {'patient': str(patient.pk), 'code': 'LAB_WBC'}).status_code == 200
    assert client.post('/trends/compare/', {'patient_id': str(patient.pk)}).status_code == 403
    assert client.post(f'/labs/observations/{rows[0].pk}/', {'patient_id': str(patient.pk), 'action': 'CORRECT'}).status_code == 403
    rows[0].refresh_from_db()
    assert rows[0].raw_value == '2' and rows[0].revision_number == 0


def test_large_finite_chart_scale_labels_remain_finite_without_float_conversion(django_user_model):
    client, patient = _patient(django_user_model, 'chart-scale-limits')
    observations(patient, ('-9e1000', '9e1000'))
    response = client.get('/trends/LAB_WBC/')
    assert response.status_code == 200
    content = response.content.decode()
    assert '<span class="trend-axis-high">9E+1000</span>' in content
    assert '<span class="trend-axis-low">-9E+1000</span>' in content


def test_same_day_duplicates_keep_every_point_and_break_lines_across_ambiguous_day(django_user_model):
    _, patient = _patient(django_user_model, 'chart-duplicate-dates')
    rows = observations(patient, ('2', '4', '6', '8', '10'))
    _, duplicate = _observation(patient, date(2026, 8, 3), '6.5')
    chart = trend_view(patient, 'LAB_WBC').series[0]
    table = comparison_view(patient).rows[0]
    assert {point.observation.pk for point in chart.points} == {row.pk for row in [*rows, duplicate]}
    assert len(chart.segments) == len(table.sparkline_segments) == 2
    assert all(len(segment.split()) == 2 for segment in chart.segments)
    assert chart.points[-1].change.baseline_mean is None


def test_active_reparse_changes_baseline_sources_and_switching_back_rebuilds_them(django_user_model):
    from apps.processing.models import ParsingVersion
    from tests.labs.test_phase_two_workflows import _new_version

    _, patient = _patient(django_user_model, 'chart-reparse-baseline')
    rows = observations(patient)
    original = rows[0]
    previous_version = original.parsing_version
    new = _new_version(previous_version.document, original, raw_value='8')
    from apps.processing.models import DocumentMetadataCandidate, SourceEvidence
    new.parsing_version.diagnostics = previous_version.diagnostics
    new.parsing_version.save(update_fields=['diagnostics'])
    date_source = SourceEvidence.objects.create(
        parsing_version=new.parsing_version, document_page=new.document_page,
        source_text='采样日期：2026-08-01', confidence='0.98',
    )
    DocumentMetadataCandidate.objects.create(
        parsing_version=new.parsing_version, kind='DOCUMENT_DATE', raw_text=date_source.source_text,
        normalized_value='2026-08-01', precision='DAY', confidence='0.98', evidence=date_source, selected=True,
    )
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean == Decimal('6')
    assert new.pk in {cell.observation.pk for cell in change.baseline}
    assert original.pk not in {cell.observation.pk for cell in change.baseline}
    ParsingVersion.objects.activate(previous_version)
    change = cells(comparison_view(patient))[-1].change
    assert change.baseline_mean == Decimal('4')
    assert original.pk in {cell.observation.pk for cell in change.baseline}
    assert new.pk not in {cell.observation.pk for cell in change.baseline}


def test_reviewed_unit_conversion_uses_one_basis_for_changes_and_preserves_raw_sources(django_user_model):
    import json
    from django.utils import timezone
    from apps.labs.dictionary import default_dictionary, load_dictionary_content, rules_digest, release_digest
    from apps.operations.models import DictionaryRelease

    _, patient = _patient(django_user_model, 'personal-change-conversion')
    payload = json.loads(default_dictionary().source_path.read_text(encoding='utf-8'))
    payload['dictionary_version'] = 'personal-change-conversion'
    next(item for item in payload['indicators'] if item['code'] == 'LAB_WBC')['unit_forms'].append('cells/uL')
    dictionary = load_dictionary_content(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    rule = {'id': 'synthetic-wbc-conversion', 'version': 'rule-1', 'kind': 'conversion', 'code': 'LAB_WBC',
            'specimen': 'BLOOD', 'method': '合成方法A', 'source_unit': 'cells/uL', 'target_unit': '10^9/L',
            'factor': '0.001', 'reviewed_by': 'synthetic-reviewer', 'rationale': 'synthetic conversion fixture',
            'evidence': 'synthetic conversion fixture'}
    DictionaryRelease.objects.create(version=dictionary.version, content_hash=dictionary.content_hash, artifact_name='',
        indicator_count=len(dictionary.indicators), payload=payload, rules=[rule], published_at=timezone.now(),
        rules_hash=rules_digest([rule]), release_hash=release_digest(dictionary.content_hash, [rule]))
    for day, value, unit in ((1, '2000', 'cells/uL'), (2, '4', '10^9/L'), (3, '6', '10^9/L'), (4, '12000', 'cells/uL')):
        _, observation = _observation(patient, date(2026, 8, day), value, raw_unit=unit)
        observation.dictionary_version = dictionary.version
        observation.save(update_fields=['dictionary_version'])
    table = cells(comparison_view(patient))[-1]
    chart = trend_view(patient, 'LAB_WBC').series[0].points[-1]
    for point in (table, chart):
        assert point.numeric_value == Decimal('12')
        assert point.change.baseline_mean == Decimal('4') and point.change.baseline_percentage == Decimal('200')
        assert point.change.absolute_change == Decimal('6') and point.change.daily_change == Decimal('6')
        assert point.observation.raw_value == '12000' and point.observation.raw_unit == 'cells/uL'
        assert point.change.baseline[0].observation.raw_value == '2000'
        assert point.change.baseline[0].rule['version'] == 'rule-1'
