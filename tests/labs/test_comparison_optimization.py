from datetime import date
from decimal import Decimal

import pytest

from apps.labs.comparison import comparison_view
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation


pytestmark = pytest.mark.django_db


def test_reference_under_indicator_shares_only_reliable_identical_visible_report_ranges(django_user_model):
    client, patient = _patient(django_user_model, 'comparison-reference-column')
    rows = [_observation(patient, date(2026, 8, day), '5')[1] for day in (1, 2)]
    for row in rows:
        row.reference_range_raw = '1-10'
        row.save(update_fields=['reference_range_raw'])
    view = comparison_view(patient)
    assert view.rows[0].shared_reference == '1-10'
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert 'class="comparison-unit-column"' in response.content.decode()
    html = response.content.decode()
    assert 'class="comparison-reference-column"' not in html
    import re
    name = re.search(r'<th scope="row">.*?</th>', html, re.S).group()
    assert '参考：1-10' in name
    assert html.count('<h1>检验对比</h1>') == 1
    navigation = re.search(r'<nav aria-label="检验工作区">.*?</nav>', html, re.S).group()
    assert '>检验对比</a>' not in navigation
    rows[1].reference_range_raw = '2-9'
    rows[1].save(update_fields=['reference_range_raw'])
    assert comparison_view(patient).rows[0].shared_reference == ''
    assert comparison_view(patient, end=date(2026, 8, 1)).rows[0].shared_reference == '1-10'
    rows[1].reference_range_raw = ''
    rows[1].save(update_fields=['reference_range_raw'])
    assert comparison_view(patient).rows[0].shared_reference == ''
    rows[1].reference_range_raw = '1-10'
    rows[1].quality_issues = [{'code': 'association_conflict', 'fields': ['reference_range_raw']}]
    rows[1].save(update_fields=['reference_range_raw', 'quality_issues'])
    assert comparison_view(patient).rows[0].shared_reference == ''
    rows[1].quality_issues = []
    rows[1].raw_unit = '%'
    rows[1].save(update_fields=['quality_issues', 'raw_unit'])
    assert comparison_view(patient).rows[0].shared_reference == ''


@pytest.mark.parametrize('code,fields,expected_status', [
    ('date_conflict', ['observation_date'], 'above'),
    ('mapping_unknown', ['raw_name'], 'unavailable'),
    ('specimen_conflict', ['specimen'], 'unavailable'),
    ('source_policy_unknown', ['raw_name', 'raw_value', 'raw_unit'], 'unavailable'),
    ('normalization_uncertain', ['raw_name'], 'unavailable'),
    ('recognition_uncertain', ['raw_name'], 'unavailable'),
    ('association_conflict', ['method_raw'], 'unavailable'),
])
def test_metadata_quality_stays_in_details_without_cell_review_badge(django_user_model, code, fields, expected_status):
    import re
    client, patient = _patient(django_user_model, 'comparison-metadata-badge')
    row = _observation(patient, date(2026, 8, 1), '12')[1]
    row.quality_issues = [{'code': code, 'fields': fields}]
    row.reference_range_raw = '1-10'
    if code == 'mapping_unknown':
        row.standard_code = 'CANDIDATE_UNMAPPED'
    row.save()
    response = client.get('/labs/compare/', {'patient': patient.pk})
    assert response.status_code == 200
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert not cell.review_required
    assert cell.abnormal.status == expected_status
    assert not cell.trend_eligible
    article = re.search(r'<article>.*?</article>', response.content.decode(), re.S).group()
    assert '待核对' not in article
    detail = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk})
    assert detail.status_code == 200
    assert code in {item['code'] for item in detail.context['issues']}


@pytest.mark.parametrize('code,fields', [
    ('recognition_uncertain', ['raw_value']),
    ('association_conflict', ['raw_unit']),
    ('reference_conflict', ['reference_range_raw']),
    ('association_conflict', ['report_flag_raw']),
    ('normalization_uncertain', ['raw_value']),
    ('source_unavailable', ['raw_value']),
    ('recognition_uncertain', []),
])
def test_direct_result_quality_uses_color_without_badge_and_blocks_arrow(django_user_model, code, fields):
    client, patient = _patient(django_user_model, 'comparison-value-badge')
    row = _observation(patient, date(2026, 8, 1), '12')[1]
    row.quality_issues = [{'code': code, 'fields': fields}]
    row.reference_range_raw = '1-10'
    row.save(update_fields=['quality_issues', 'reference_range_raw'])
    response = client.get('/labs/compare/', {'patient': patient.pk})
    cell = response.context['comparison'].rows[0].cells[0][0]
    assert cell.review_required
    assert cell.abnormal.status == 'review'
    assert cell.abnormal.symbol == ''
    html = response.content.decode()
    assert '待核对' not in html
    assert 'comparison-value--review' in html
    assert '结果依据需确认' in html


@pytest.mark.parametrize('return_to', ['http://[', '//[', 'https://example.invalid/labs/compare/', '/labs/compare/?patient=another'])
def test_result_ignores_invalid_or_foreign_return_address(django_user_model, return_to):
    client, patient = _patient(django_user_model, 'comparison-return')
    row = _observation(patient, date(2026, 8, 1), '2')[1]
    response = client.get(f'/labs/observations/{row.pk}/', {'patient': patient.pk, 'return_to': return_to})
    assert response.status_code == 200
    assert 'comparison_return' not in response.context


def test_display_identity_is_independent_of_method_unit_and_value_quality(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-identity')
    first = _observation(patient, date(2026, 8, 1), '1', method='')[1]
    second = _observation(patient, date(2026, 8, 2), '2', method='方法B')[1]
    third = _observation(patient, date(2026, 8, 3), '3', raw_unit='×109/L')[1]
    second.evidence.confidence = Decimal('0.2')
    second.evidence.save(update_fields=['confidence'])
    view = comparison_view(patient)
    assert len(view.rows) == 1
    cells = [cell for entries in view.rows[0].cells for cell in entries]
    assert {cell.observation.pk for cell in cells} == {first.pk, second.pk, third.pk}
    assert not any(cell.trend_eligible for cell in cells)
    assert [cell.observation.pk for cell in cells if cell.plot_eligible] == [first.pk]
    assert view.rows[0].shared_unit == ''


def test_alias_search_selects_identity_before_filtering_history(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-alias')
    _observation(patient, date(2026, 8, 1), '1', raw_name='特定报告别名')
    second = _observation(patient, date(2026, 8, 2), '2', raw_name='白细胞')[1]
    view = comparison_view(patient, project=' 特定报告别名 ', start=date(2026, 8, 2))
    assert len(view.rows) == 1
    assert view.rows[0].cells[0][0].observation.pk == second.pk


def test_report_flags_and_ranges_are_independent_of_missing_method(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-abnormal')
    row = _observation(patient, date(2026, 8, 1), '12', method='')[1]
    row.report_flag_raw = 'H'
    row.save(update_fields=['report_flag_raw'])
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert cell.abnormal.status == 'above'
    assert cell.abnormal.source == '报告原标记'
    row.reference_range_raw = '1-15'
    row.save(update_fields=['reference_range_raw'])
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert cell.abnormal.status == 'review'


def test_invalid_dates_preserve_filter_inputs_inline(django_user_model):
    client, patient = _patient(django_user_model, 'comparison-date-error')
    response = client.get('/labs/compare/', {'patient': patient.pk, 'start': '2026-09-30', 'end': '2026-09-01', 'project': '白细胞'})
    assert response.status_code == 400
    assert '开始日期不能晚于结束日期' in response.content.decode()
    assert 'value="2026-09-30"' in response.content.decode()


@pytest.mark.parametrize('value,reference,flag,result_type,expected', [
    ('12', '1-10', '', 'NUMERIC', 'above'),
    ('0.5', '1-10', '', 'NUMERIC', 'below'),
    ('1', '1-10', '', 'NUMERIC', 'within'),
    ('10', '1-10', '', 'NUMERIC', 'within'),
    ('10', '<10', '', 'NUMERIC', 'above'),
    ('>10', '1-10', '', 'COMPARATOR', 'above'),
    ('>=10', '1-10', '', 'COMPARATOR', 'unavailable'),
    ('<1', '1-10', '', 'COMPARATOR', 'below'),
    ('阳性', '阴性', '', 'QUALITATIVE', 'different'),
    ('阳性', '', '', 'QUALITATIVE', 'unavailable'),
    ('阳性', '', 'H', 'QUALITATIVE', 'different'),
    ('阳性', '阴性', 'H', 'QUALITATIVE', 'different'),
    ('12', '', 'A', 'NUMERIC', 'different'),
    ('12', '1-10', 'L', 'NUMERIC', 'review'),
    ('12', '1-10', 'N', 'NUMERIC', 'review'),
    ('5', '1-10', '*', 'NUMERIC', 'review'),
])
def test_abnormal_direction_uses_own_report_boundaries(django_user_model, value, reference, flag, result_type, expected):
    _, patient = _patient(django_user_model, 'comparison-boundary')
    row = _observation(patient, date(2026, 8, 1), value, method='', result_type=result_type)[1]
    row.reference_range_raw, row.report_flag_raw = reference, flag
    row.save(update_fields=['reference_range_raw', 'report_flag_raw'])
    cell = comparison_view(patient).rows[0].cells[0][0]
    assert cell.abnormal.status == expected
    assert not cell.trend_eligible


def test_general_report_flag_does_not_claim_to_supply_range_direction(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-flag-source')
    row = _observation(patient, date(2026, 8, 1), '12')[1]
    row.reference_range_raw, row.report_flag_raw = '1-10', 'A'
    row.save(update_fields=['reference_range_raw', 'report_flag_raw'])
    abnormal = comparison_view(patient).rows[0].cells[0][0].abnormal
    assert abnormal.status == 'above'
    assert abnormal.source == '按本报告参考范围对照'


def test_missing_method_policy_is_reviewed_and_scope_specific(django_user_model):
    from apps.labs.comparison import comparable_cell
    from apps.labs.dictionary import default_dictionary
    from apps.labs.readmodels import effective_rows

    _, patient = _patient(django_user_model, 'comparison-policy')
    _observation(patient, date(2026, 8, 1), '2', method='')
    _observation(patient, date(2026, 8, 2), '4', method='合成方法A')
    _observation(patient, date(2026, 8, 3), '6', method='不同方法B')
    _observation(patient, date(2026, 8, 4), '8', method='', institution='范围外机构')
    rows = sorted(effective_rows(patient), key=lambda row: row.observation_date)
    rule = {'kind': 'method_comparability', 'id': 'synthetic-wbc-method', 'version': '1',
            'code': 'LAB_WBC', 'specimen': 'BLOOD', 'unit': '10^9/L',
            'institutions': ['合成检验中心'], 'methods': ['合成方法A'], 'allow_missing_method': True,
            'reviewed_by': 'synthetic-reviewer', 'rationale': '仅用于规则机制测试', 'evidence': 'synthetic fixture'}
    cells = [comparable_cell(row, dictionary=default_dictionary(), rules=[rule]) for row in rows]
    assert cells[0].trend_eligible and cells[0].group_key == cells[1].group_key
    assert cells[2].group_key != cells[0].group_key
    assert not cells[3].trend_eligible and cells[3].plot_eligible
    from apps.labs.change_metrics import changes_for_cells
    change = changes_for_cells(cells)[str(rows[1].pk)]
    assert change.previous_percentage == Decimal('100')
    for missing in ('institutions', 'evidence', 'reviewed_by', 'version', 'specimen', 'unit'):
        incomplete = {key: value for key, value in rule.items() if key != missing}
        assert not comparable_cell(rows[0], dictionary=default_dictionary(), rules=[incomplete]).trend_eligible
    assert not comparable_cell(rows[0], dictionary=default_dictionary(), rules=[rule, {**rule, 'id': 'conflict'}]).trend_eligible


def test_history_points_and_distinct_method_series_are_accessible_without_false_lines(django_user_model):
    client, patient = _patient(django_user_model, 'comparison-history')
    first = _observation(patient, date(2026, 8, 1), '2', method='')[1]
    second = _observation(patient, date(2026, 8, 2), '4', method='')[1]
    _observation(patient, date(2026, 8, 3), '6', method='B')
    bad = _observation(patient, date(2026, 8, 4), '999', method='')[1]
    bad.quality_issues = [{'code': 'association_conflict', 'fields': ['raw_value']}]
    bad.save(update_fields=['quality_issues'])
    response = client.get('/trends/LAB_WBC/', {'patient': patient.pk, 'history': '1', 'end': '2026-08-02'})
    assert response.status_code == 200
    trend = response.context['trend']
    assert len(trend.series) == 1
    assert {point.observation.pk for point in trend.series[0].points} == {first.pk, second.pk}
    assert not trend.series[0].segments
    assert all(point.change.previous_percentage is None for point in trend.series[0].points)
    from urllib.parse import parse_qs, urlsplit
    for point in trend.series[0].points:
        query = parse_qs(urlsplit(point.source_url).query)
        assert query.get('patient') == [str(patient.pk)]
        assert query.get('evidence') == [str(point.observation.evidence_id)]
    response = client.get('/trends/LAB_WBC/', {'patient': patient.pk, 'history': '1', 'start': '2026-08-02', 'end': '2026-08-02'})
    assert response.status_code == 200 and len(response.context['trend'].series[0].points) == 1


def test_multiple_categories_alias_history_and_unknown_candidates(django_user_model):
    from apps.labs.dictionary import phase_two_dictionary
    client, patient = _patient(django_user_model, 'comparison-categories')
    _observation(patient, date(2026, 8, 1), '2')
    modern = _observation(patient, date(2026, 8, 2), '4')[1]
    modern.dictionary_version = phase_two_dictionary().version
    modern.save(update_fields=['dictionary_version'])
    alt = _observation(patient, date(2026, 8, 2), '20', code='LAB_ALT', standard_name='丙氨酸氨基转移酶', raw_unit='U/L')[1]
    alt.dictionary_version = phase_two_dictionary().version
    alt.save(update_fields=['dictionary_version'])
    for day in (1, 2):
        _observation(patient, date(2026, 8, day), '9', code='CANDIDATE_TEST', raw_name='未知指标')
    view = comparison_view(patient, categories=['CBC', 'LIVER_FUNCTION'])
    assert {group.category for group in view.groups} == {'CBC', 'LIVER_FUNCTION'}
    assert len(view.rows) == 2 and len(view.columns) == 3
    assert len(comparison_view(patient, category='HEMATOLOGY').rows) == 1
    unknown = comparison_view(patient, category='未归类')
    assert len(unknown.rows) == 2
    response = client.get('/labs/compare/', {'patient': patient.pk, 'category': ['CBC', 'LIVER_FUNCTION'], 'project': '无命中'})
    assert response.status_code == 200
    assert response.context['selected_categories'] == ('CBC', 'LIVER_FUNCTION')
    assert len(response.context['categories']) == 3


def test_same_report_duplicates_and_unknown_dates_keep_all_values(django_user_model):
    from copy import copy
    from uuid import uuid4
    _, patient = _patient(django_user_model, 'comparison-duplicates')
    first = _observation(patient, date(2026, 8, 1), '2')[1]
    duplicate = copy(first)
    duplicate.pk, duplicate.reading_order, duplicate.raw_value = uuid4(), 2, '3'
    duplicate.save(force_insert=True)
    unknown = _observation(patient, date(2026, 8, 2), '4')[1]
    unknown.observation_date = None
    unknown.save(update_fields=['observation_date'])
    view = comparison_view(patient)
    assert len(view.rows) == 1
    assert {cell.observation.raw_value for cell in view.rows[0].cells[0]} == {'2', '3'}
    assert view.columns[-1].date_label == '日期未识别'
    assert view.report_count == 2
    assert not view.rows[0].sparkline_segments


def test_units_are_deduplicated_only_with_a_reliable_visible_basis(django_user_model):
    _, patient = _patient(django_user_model, 'comparison-units')
    _observation(patient, date(2026, 8, 1), '2', raw_unit='10^9/L')
    _observation(patient, date(2026, 8, 2), '4', raw_unit='10^9/l')
    _observation(patient, date(2026, 8, 3), '6', raw_unit='×109/L')
    assert comparison_view(patient).rows[0].shared_unit == ''
    assert comparison_view(patient, end=date(2026, 8, 2)).rows[0].shared_unit.lower() == '10^9/l'


def test_report_institutions_use_current_evidence_and_keep_ambiguity(django_user_model):
    from apps.processing.models import DocumentMetadataCandidate, DocumentSummary, SourceEvidence
    _, patient = _patient(django_user_model, 'comparison-institutions')
    row = _observation(patient, date(2026, 8, 1), '2', page_count=2)[1]
    view = comparison_view(patient)
    assert view.columns[0].institution == '合成检验中心'
    DocumentSummary.objects.filter(parsing_version=row.parsing_version).update(institution_raw='已更正机构')
    assert comparison_view(patient).columns[0].institution == '已更正机构'
    pages = list(row.parsing_version.document.pages.order_by('page_number'))
    for page, name in zip(pages, ('机构甲', '机构乙')):
        source = SourceEvidence.objects.create(parsing_version=row.parsing_version, document_page=page, source_text=name, confidence='0.98')
        DocumentMetadataCandidate.objects.create(parsing_version=row.parsing_version, kind='INSTITUTION', raw_text=name,
            normalized_value=name, evidence=source, confidence='0.98', selected=True)
    assert comparison_view(patient).columns[0].institution == '机构甲'
    # Two institutions on the same page cannot be arbitrarily assigned.
    source = SourceEvidence.objects.create(parsing_version=row.parsing_version, document_page=pages[0], source_text='机构乙', confidence='0.98')
    DocumentMetadataCandidate.objects.create(parsing_version=row.parsing_version, kind='INSTITUTION', raw_text='机构乙',
        normalized_value='机构乙', evidence=source, confidence='0.98', selected=True)
    assert comparison_view(patient).columns[0].institution == '多机构，待核对'
    row.institution_raw = '机构甲'
    row.field_evidence = {'institution_raw': {'page_number': 1, 'precision': 'region'}}
    row.save(update_fields=['institution_raw', 'field_evidence'])
    assert comparison_view(patient).columns[0].institution == '机构甲'


def test_report_columns_distinguish_dates_and_institutions_without_overcounting_documents(django_user_model):
    from copy import copy
    from uuid import uuid4

    _, patient = _patient(django_user_model, 'comparison-report-units')
    first = _observation(patient, date(2026, 8, 1), '2', institution='机构甲')[1]
    first.institution_raw = '机构甲'
    first.field_evidence = {**first.field_evidence, 'institution_raw': {'page_number': 1, 'precision': 'region'}}
    first.save(update_fields=['institution_raw', 'field_evidence'])
    for order, (day, institution, value) in enumerate(((1, '机构乙', '3'), (2, '机构甲', '4')), start=2):
        row = copy(first)
        row.pk, row.observation_date, row.institution_raw, row.raw_value = uuid4(), date(2026, 8, day), institution, value
        row.reading_order = order
        row.save(force_insert=True)
    _observation(patient, date(2026, 8, 1), '5', institution='机构甲')

    view = comparison_view(patient)
    assert view.report_count == 2
    assert len(view.columns) == 4
    assert len(view.rows) == 1
    assert {cell.observation.raw_value for entries in view.rows[0].cells for cell in entries} == {'2', '3', '4', '5'}
    assert len({column.report_label for column in view.columns if column.observation_date == date(2026, 8, 1) and column.institution == '机构甲'}) == 2
    assert any(column.observation_date == date(2026, 8, 2) and column.institution == '机构甲' for column in view.columns)


def test_baso_count_percentage_and_specimens_never_merge_by_name(django_user_model):
    from apps.labs.dictionary import phase_two_dictionary
    _, patient = _patient(django_user_model, 'comparison-baso')
    for index, (code, specimen, unit) in enumerate((('LAB_BASO_COUNT', 'BLOOD', '×109/L'),
            ('LAB_BASO_COUNT', 'BLOOD', '10^9/L'), ('LAB_BASO_PERCENT', 'BLOOD', '%'),
            ('LAB_BASO_COUNT', 'URINE', '10^9/L'), ('LAB_BASO_COUNT', '', '10^9/L'))):
        row = _observation(patient, date(2026, 8, index + 1), '0.02', code=code, standard_name='同名原文', raw_unit=unit)[1]
        row.specimen, row.dictionary_version = specimen, phase_two_dictionary().version
        row.save(update_fields=['specimen', 'dictionary_version'])
    view = comparison_view(patient)
    assert len(view.rows) == 4
    assert {row.specimen_label for row in view.rows if row.standard_code == 'LAB_BASO_COUNT'} == {'血液', '尿液', '标本待确认'}
    assert sum(len(entries) for row in view.rows for entries in row.cells) == 5
