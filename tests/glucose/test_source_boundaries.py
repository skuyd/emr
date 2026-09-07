"""Report geometry and printed line boundaries must survive actual source import."""

from uuid import uuid4

import pytest

from apps.glucose.services import import_lab_record
from apps.glucose.source_context import report_context
from apps.glucose.sources import preview_lab
from apps.processing.models import OcrBlock
from tests.glucose.factories import lab_source
from tests.glucose.test_source_context import block, panel


def staggered_panels():
    left = panel(x=.02, width=.43)
    left.pop(3)
    right = panel(start=.07, x=.55, width=.43,
                  title='检验报告单 空腹血糖', sample='2026-08-09 05:11:22')
    return left + right, left[2]


def date_with_unrelated_next_clock():
    rows = panel(sample='2026-08-02')
    rows.insert(4, block('09:30 开放报告领取窗口', .245, order=4))
    return rows, rows[2]


def test_staggered_report_does_not_supply_a_missing_sampling_time_or_fasting_title():
    rows, anchor = staggered_panels()
    result = report_context(rows, anchor_polygon=anchor['polygon'])
    assert result['sample_time']['precision'] == 'UNKNOWN'
    assert result['time_slot'] == 'UNSPECIFIED'


def test_date_only_sampling_does_not_absorb_a_clock_from_the_next_line():
    rows, anchor = date_with_unrelated_next_clock()
    result = report_context(rows, anchor_polygon=anchor['polygon'])['sample_time']
    assert result['precision'] == 'DAY' and result['local'] == '2026-08-02'
    assert result['raw'] == '2026-08-02'
    assert all('09:30' not in item['text'][item['start']:item['end']] for item in result['evidence'])


def test_staggered_reports_keep_each_own_time_and_neither_column_truncates_the_other():
    left = panel(x=.02, width=.43)
    right = panel(start=.07, x=.55, width=.43, sample='2026-08-09 05:11:22')
    right += panel(start=.43, x=.55, width=.43, sample='2026-08-10 05:11:22')
    rows = left + right
    assert report_context(rows, anchor_polygon=left[2]['polygon'])['sample_time']['local'] == '2026-08-02T06:12:34'
    assert report_context(rows, anchor_polygon=right[2]['polygon'])['sample_time']['local'] == '2026-08-09T05:11:22'
    assert report_context(rows, anchor_polygon=right[7]['polygon'])['sample_time']['local'] == '2026-08-10T05:11:22'


def test_embedded_ocr_newline_is_also_a_boundary_between_date_and_unrelated_clock():
    rows = panel(sample='2026-08-02\n09:30 开放报告领取窗口')
    result = report_context(rows, anchor_polygon=rows[2]['polygon'])['sample_time']
    assert result['precision'] == 'DAY' and result['raw'] == '2026-08-02'


def test_ocr_box_crossing_two_report_columns_cannot_verify_which_time_belongs_to_either():
    rows, anchor = staggered_panels()
    rows.append(block('采样时间：2026-08-11 12:34:56', .35))
    result = report_context(rows, anchor_polygon=anchor['polygon'])['sample_time']
    assert result['precision'] == 'UNKNOWN'


def test_cropped_left_heading_cannot_bind_the_row_to_a_visible_right_report():
    rows, anchor = staggered_panels()
    rows.pop(0)
    result = report_context(rows, anchor_polygon=anchor['polygon'])
    assert result['sample_time']['precision'] == 'UNKNOWN'
    assert result['time_slot'] == 'UNSPECIFIED'


def test_same_line_split_label_and_timestamp_remains_supported():
    rows = panel()
    rows[3] = block('采样时间：', .21, x=.1, width=.16, order=3)
    rows.append(block('2026-08-02 06:12:34', .21, x=.3, width=.5, order=4))
    result = report_context(rows, anchor_polygon=rows[2]['polygon'])['sample_time']
    assert result['local'] == '2026-08-02T06:12:34'
    assert len(result['evidence']) == 2


def _install_blocks(version, observation, rows, anchor):
    version.ocr_blocks.all().delete()
    for index, item in enumerate(rows):
        OcrBlock.objects.create(
            parsing_version=version, document_page=observation.document_page,
            reading_order=index, text=item['text'], polygon=item['polygon'],
            layout_polygon=item['layout_polygon'], confidence='.99',
        )
    observation.evidence.polygon = anchor['polygon']
    observation.evidence.source_text = anchor['text']
    observation.evidence.save(update_fields=['polygon', 'source_text'])
    observation.field_evidence = {
        name: {'page_number': 1, 'polygon': anchor['polygon'], 'precision': 'region'}
        for name in ('raw_value', 'raw_unit', 'raw_name', 'observation_date', 'specimen')
    }
    observation.field_evidence['specimen']['polygon'] = rows[1]['polygon']
    observation.save(update_fields=['field_evidence'])


@pytest.mark.django_db
@pytest.mark.parametrize('fixture,expected_precision', [
    (staggered_panels, 'UNKNOWN'), (date_with_unrelated_next_clock, 'DAY'),
])
def test_confirming_timezone_cannot_turn_an_unproven_source_time_into_a_point(
        django_user_model, fixture, expected_precision):
    _, patient, _, version, observation = lab_source(django_user_model)
    rows, anchor = fixture()
    _install_blocks(version, observation, rows, anchor)
    preview = preview_lab(patient, patient.account, observation.pk)
    record = import_lab_record(
        patient, patient.account, observation.pk,
        expected_source=preview['source_fingerprint'], checked_original=True,
        creation_key=uuid4(), timezone_name='Asia/Shanghai', confirm_timezone=True,
    ).record
    assert record.time_precision == expected_precision
    assert record.measured_at is None
    assert not record.current_data['plot_eligible']
