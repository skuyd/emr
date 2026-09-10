from copy import deepcopy

import pytest

from apps.glucose.source_context import report_context


def block(text, y, *, x=.1, width=.8, height=.025, order=0, key=None):
    polygon = [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]
    return {'id': key or f'{x}-{y}-{order}', 'text': text, 'polygon': polygon,
            'layout_polygon': polygon, 'reading_order': order}


def panel(*, start=0, x=.1, width=.8, title='合成医院检验报告单', sample='2026-08-02 06:12:34', report='2026-08-02 09:24:56'):
    return [block(title, start + .02, x=x, width=width, order=0),
            block('标本类型：血清', start + .07, x=x, width=width, order=1),
            block('葡萄糖 GLU 8.20 mmol/L', start + .13, x=x, width=width, order=2),
            block('采样时间：' + sample, start + .21, x=x, width=width, order=3),
            block('报告日期：' + report, start + .26, x=x, width=width, order=4)]


def context(rows, index=2):
    return report_context(rows, anchor_polygon=rows[index]['polygon'])


def test_sample_seconds_report_seconds_and_raw_fragments_are_separate():
    rows = panel()
    result = context(rows)
    assert result['sample_time']['local'] == '2026-08-02T06:12:34'
    assert result['sample_time']['precision'] == 'SECOND'
    assert result['report_time']['local'] == '2026-08-02T09:24:56'
    assert result['sample_time']['timezone_origin'] == 'UNCONFIRMED'
    assert result['sample_time']['timezone'] == ''
    assert result['sample_time']['evidence'][0]['text'] == '采样时间：2026-08-02 06:12:34'
    assert result['sample_time']['evidence'][0]['block_id'] == rows[3]['id']
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['specimen_raw'] == '血清'


def test_fasting_is_only_from_explicit_request_context_not_clock_or_dictionary():
    rows = panel(title='合成医院检验报告单 生化检查（空腹血糖）', sample='2026-08-02 18:12:34')
    assert context(rows)['time_slot'] == 'FASTING'
    rows[0]['text'] = '合成医院检验报告单 生化检查'
    rows[2]['text'] = '葡萄糖 LAB_FASTING_GLUCOSE 8.20 mmol/L'
    assert context(rows)['time_slot'] == 'UNSPECIFIED'
    rows.insert(3, block('医生建议下次空腹测血糖', .18, order=3))
    assert context(rows)['time_slot'] == 'UNSPECIFIED'


def test_vertically_stacked_reports_do_not_borrow_sample_or_fasting_context():
    rows = panel(sample='2026-08-02 06:12:34') + panel(start=.45, title='检验报告单 空腹血糖', sample='2026-08-09 05:11:22')
    first = context(rows)
    second = context(rows, 7)
    assert first['sample_time']['local'] == '2026-08-02T06:12:34'
    assert first['time_slot'] == 'UNSPECIFIED'
    assert second['sample_time']['local'] == '2026-08-09T05:11:22'
    assert second['time_slot'] == 'FASTING'
    rows.pop(3)
    assert context(rows)['sample_time']['precision'] == 'UNKNOWN'


def test_side_by_side_reports_keep_their_own_sample_time():
    left = panel(x=.02, width=.43)
    right = panel(x=.55, width=.43, sample='2026-08-09 05:11:22')
    rows = left + right
    assert context(rows)['sample_time']['local'] == '2026-08-02T06:12:34'
    assert context(rows, 7)['sample_time']['local'] == '2026-08-09T05:11:22'


def test_cropped_upper_panel_stops_before_next_report_header():
    rows = panel()[1:] + panel(start=.45, sample='2026-09-03 01:02:03')
    assert context(rows, 1)['sample_time']['local'] == '2026-08-02T06:12:34'
    assert context(rows, 1)['time_slot'] == 'UNSPECIFIED'


def test_geometry_is_required_to_bind_a_report_context():
    rows = panel()
    assert report_context(rows, anchor_polygon=None)['sample_time']['precision'] == 'UNKNOWN'
    rows[0]['polygon'] = rows[0]['layout_polygon'] = None
    assert context(rows)['sample_time']['precision'] == 'UNKNOWN'


def test_conflicting_sample_times_remain_unknown_and_keep_both_evidence_fragments():
    rows = panel()
    rows.append(block('采样时间：2026-08-03 06:12:34', .31, order=5))
    result = context(rows)['sample_time']
    assert result['precision'] == 'UNKNOWN' and result['local'] == ''
    assert len(result['evidence']) == 2
    assert result['reason'] == 'conflicting_sample_time'


def test_date_only_preserves_day_without_midnight_or_report_time():
    result = context(panel(sample='2026-08-02'))['sample_time']
    assert result['precision'] == 'DAY' and result['local'] == '2026-08-02'


def test_invalid_calendar_and_clock_are_not_silently_shortened_to_dates():
    for sample in ('2026-02-30 06:12:34', '2026-08-02 25:12:34', '2026-08-02 06:12:99'):
        result = context(panel(sample=sample))['sample_time']
        assert result['precision'] == 'UNKNOWN' and result['local'] == ''


def test_split_ocr_label_and_timestamp_retain_each_actual_block_and_fullwidth_text():
    rows = panel()
    rows[3] = block('采样时间：', .21, x=.1, width=.16, order=3)
    rows.append(block('２０２６－０８－０２　０６：１２：３４', .21, x=.3, width=.5, order=4))
    result = context(rows)['sample_time']
    assert result['local'] == '2026-08-02T06:12:34'
    assert len(result['evidence']) == 2
    assert result['evidence'][1]['text'] == '２０２６－０８－０２　０６：１２：３４'


def test_report_print_order_and_plan_clocks_are_not_measurements():
    rows = panel()
    rows[3]['text'] = '打印时间：2026-08-02 06:12:34；医嘱：23:00监测血糖'
    result = context(rows)
    assert result['sample_time']['precision'] == 'UNKNOWN'
    assert result['report_time']['local'] == '2026-08-02T09:24:56'


def test_timezone_is_not_inferred_from_hospital_or_date_but_printed_utc_is_retained():
    rows = panel(title='上海合成医院检验报告单')
    assert context(rows)['sample_time']['timezone_origin'] == 'UNCONFIRMED'
    rows[3]['text'] += 'Z'
    result = context(rows)['sample_time']
    assert result['timezone'] == 'UTC' and result['timezone_origin'] == 'SOURCE_EXPLICIT'


def test_conflicting_specimens_do_not_claim_a_unique_serum_method():
    rows = panel()
    rows.append(block('标本类型：血浆', .3, order=5))
    assert context(rows)['specimen_raw'] == ''


def test_separate_request_header_is_evidence_but_a_future_instruction_is_not():
    rows = panel()
    rows.append(block('检验项目：肝功能＋空腹血糖', .055, order=1))
    assert context(rows)['time_slot'] == 'FASTING'
    rows[-1]['text'] = '建议下次检查项目：空腹血糖'
    assert context(rows)['time_slot'] == 'UNSPECIFIED'
    rows[3]['text'] = '预计采样时间：2026-08-02 06:12:34'
    assert context(rows)['sample_time']['precision'] == 'UNKNOWN'


def test_one_ocr_box_containing_two_report_headings_cannot_prove_a_panel_boundary():
    rows = panel()
    rows[0]['text'] += ' 空腹血糖 另一份检验报告单'
    result = context(rows)
    assert result['sample_time']['precision'] == 'UNKNOWN'
    assert result['time_slot'] == 'UNSPECIFIED'


def test_raw_time_role_and_absent_utc_are_explicit_separate_values():
    rows = panel(sample='２０２６－０８－０２　０６：１２：３４')
    result = context(rows)
    assert result['sample_time']['raw'] == '２０２６－０８－０２　０６：１２：３４'
    assert result['sample_time']['role'] == 'SPECIMEN_SAMPLING'
    assert result['report_time']['role'] == 'LAB_REPORT_ISSUANCE'
    assert result['sample_time']['utc_datetime'] is None and result['report_time']['utc_datetime'] is None
    assert result['sample_time']['raw_label'] == '采样时间'


def request_panel(*fragments):
    rows = panel()
    rows[2] = block('葡萄糖 GLU 8.20 mmol/L', .30, key='measurement')
    return rows + list(fragments)


@pytest.mark.parametrize('heading', ['request', 'title'])
@pytest.mark.parametrize('reverse', [False, True])
def test_same_line_fasting_fragments_keep_original_evidence_in_geometric_order(heading, reverse):
    rows = request_panel()
    y = .115 if heading == 'request' else .02
    label = block('检验项目：' if heading == 'request' else '检验报告单', y,
                  x=.1, width=.18, key='request-label')
    project = block('空腹血糖', y, x=.30, width=.22, key='request-project')
    if heading == 'title':
        rows[0] = label
    else:
        rows.append(label)
    rows.append(project)
    anchor = rows[2]['polygon']
    if reverse:
        rows.reverse()
    original = deepcopy(rows)
    result = report_context(rows, anchor_polygon=anchor)
    assert result['time_slot'] == 'FASTING'
    assert result['time_slot_evidence'] == [
        {'block_id': item['id'], 'text': item['text'], 'polygon': item['polygon']}
        for item in (label, project)
    ]
    assert result['sample_time']['local'] == '2026-08-02T06:12:34'
    assert rows == original


@pytest.mark.parametrize('label,project,project_y,prefix', [
    ('检验项目：', '空腹血糖', .17, ''),
    ('计划下次检验项目：', '空腹血糖', .115, ''),
    ('检验项目：', '空腹血糖', .115, '计划下次'),
    ('检验项目：', '非空腹血糖', .115, ''),
    ('检验项目：非', '空腹血糖', .115, ''),
    ('检验项目：', '空腹血糖', .115, '非'),
])
def test_fasting_request_does_not_join_rows_or_drop_cross_block_modifiers(label, project, project_y, prefix):
    # A standalone 非 is immediately before the project; a future qualifier
    # instead governs the request from the start of the same printed line.
    fragments = [block(label, .115, x=.23, width=.28, key='label'),
                 block(project, project_y, x=.62, width=.25, key='project')]
    if prefix:
        fragments.append(block(prefix, .115, x=.54 if prefix == '非' else .1,
                               width=.05 if prefix == '非' else .12, key='modifier'))
    result = context(request_panel(*fragments))
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


@pytest.mark.parametrize('text', [
    '检验项目：\n空腹血糖',
    '计划下次\n检验项目：空腹血糖',
    '检验项目：非\n空腹血糖',
])
def test_internal_ocr_line_breaks_cannot_supply_fasting_or_erase_a_modifier(text):
    result = context(request_panel(block(text, .115, key='request')))
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


@pytest.mark.parametrize('y,project_x', [(.35, .3), (.115, .2)])
def test_after_measurement_or_overlapping_request_fragments_do_not_prove_fasting(y, project_x):
    result = context(request_panel(block('检验项目：', y, x=.1, width=.18),
                                   block('空腹血糖', y, x=project_x, width=.22)))
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


def test_two_requests_on_one_line_do_not_prove_a_unique_project_association():
    result = context(request_panel(block('检验项目：', .115, x=.1, width=.18),
                                   block('检查项目：', .115, x=.35, width=.18),
                                   block('空腹血糖', .115, x=.6, width=.22)))
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


@pytest.mark.parametrize('adjacent', ['column', 'panel'])
def test_split_fasting_request_in_another_report_cannot_supply_this_measurement(adjacent):
    if adjacent == 'column':
        rows = panel(x=.02, width=.43) + panel(x=.55, width=.43)
        rows.extend([block('检验项目：', .10, x=.56, width=.15),
                     block('空腹血糖', .10, x=.74, width=.20)])
    else:
        rows = panel() + panel(start=.45)
        rows.extend([block('检验项目：', .55, x=.1, width=.18),
                     block('空腹血糖', .55, x=.30, width=.22)])
    result = context(rows)
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


@pytest.mark.parametrize('second,expected', [
    ('检查项目：空腹血糖', 'UNSPECIFIED'),
    ('计划下次检查项目：空腹血糖', 'FASTING'),
])
def test_only_one_qualifying_fasting_request_line_can_supply_the_measurement(second, expected):
    first = block('检验项目：空腹血糖', .115, key='first-request')
    result = context(request_panel(first, block(second, .17, key='second-request')))
    assert result['time_slot'] == expected
    assert result['time_slot_evidence'] == ([
        {'block_id': first['id'], 'text': first['text'], 'polygon': first['polygon']}
    ] if expected == 'FASTING' else [])


@pytest.mark.parametrize('layout', ['same_block', 'split', 'project_before_request'])
def test_request_label_is_the_direct_project_label_when_a_title_shares_its_line(layout):
    rows = request_panel()
    if layout == 'split':
        rows[0] = block('检验报告单', .02, x=.1, width=.22, key='title')
        request = block('检验项目：', .02, x=.36, width=.18, key='request')
        project = block('空腹血糖', .02, x=.61, width=.20, key='project')
        rows.extend([request, project])
        expected = [request, project]
    else:
        rows[0] = block('检验报告单 检验项目：空腹血糖' if layout == 'same_block'
                        else '检验报告单 空腹血糖 检验项目：肝功能', .02, key='title-request')
        expected = [rows[0]] if layout == 'same_block' else []
    result = context(rows)
    assert result['time_slot'] == ('FASTING' if expected else 'UNSPECIFIED')
    assert result['time_slot_evidence'] == [
        {'block_id': item['id'], 'text': item['text'], 'polygon': item['polygon']} for item in expected
    ]


def prefixed_request_panel(prefix=None, *, qualifier_y=13 / 64, qualifier_x=1 / 8):
    rows = request_panel()
    rows[2] = block('葡萄糖 GLU 8.20 mmol/L', .6, key='measurement')
    rows[3] = block('采样时间：2026-08-02 06:12:34', .4, key='sampling')
    rows[4] = block('报告日期：2026-08-02 09:24:56', .7, key='report-time')
    rows.extend([
        block('检验项目：', 1 / 4, x=1 / 8, width=3 / 16, height=1 / 32, key='request-label'),
        block('空腹血糖', 1 / 4, x=3 / 8, width=1 / 4, height=1 / 32, key='request-project'),
    ])
    if prefix is not None:
        rows.append(block(prefix, qualifier_y, x=qualifier_x, width=1 / 8,
                          height=1 / 32, key='qualifier'))
    return rows


@pytest.mark.parametrize('prefix', ['计划', '下次'])
@pytest.mark.parametrize('reverse', [False, True])
def test_fasting_preceding_future_line_is_not_lost(prefix, reverse):
    rows = prefixed_request_panel(prefix)
    anchor = rows[2]['polygon']
    if reverse:
        rows.reverse()
    original = deepcopy(rows)
    result = report_context(rows, anchor_polygon=anchor)
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []
    assert result['sample_time']['local'] == '2026-08-02T06:12:34'
    assert result['report_time']['local'] == '2026-08-02T09:24:56'
    assert rows == original


@pytest.mark.parametrize('prefix', [None, '本次'])
def test_fasting_preceding_current_or_absent_line_is_not_a_veto(prefix):
    rows = prefixed_request_panel(prefix)
    result = context(rows)
    assert result['time_slot'] == 'FASTING'
    assert result['time_slot_evidence'] == [
        {'block_id': row['id'], 'text': row['text'], 'polygon': row['polygon']}
        for row in rows if row['id'] in ('request-label', 'request-project')
    ]


@pytest.mark.parametrize('prefix', ['预计', '预约', '建议，下次：', '拟于', '将于'])
def test_fasting_preceding_defined_future_words_and_punctuation(prefix):
    result = context(prefixed_request_panel(prefix))
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []


@pytest.mark.parametrize('placement', ['complete_sentence', 'intervening_row', 'far', 'other_horizontal_region', 'after_request'])
def test_fasting_preceding_unrelated_future_text_does_not_veto_current_request(placement):
    rows = prefixed_request_panel('计划复查其他项目' if placement == 'complete_sentence' else '计划',
        qualifier_y={'intervening_row': 3 / 16, 'far': 1 / 8, 'after_request': 5 / 16}.get(placement, 13 / 64),
        qualifier_x=.7 if placement == 'other_horizontal_region' else 1 / 8)
    if placement == 'intervening_row':
        # The qualifier alone is within one line-height of the request; this
        # separate intervening line, not distance, must break its attachment.
        rows.append(block('本次申请说明', .223, x=1 / 8, width=.4, height=.01, key='intervening'))
    result = context(rows)
    assert result['time_slot'] == 'FASTING'
    assert [part['block_id'] for part in result['time_slot_evidence']] == ['request-label', 'request-project']


@pytest.mark.parametrize('axis,outside,expected', [
    ('vertical', False, 'UNSPECIFIED'), ('vertical', True, 'FASTING'),
    ('horizontal', False, 'UNSPECIFIED'), ('horizontal', True, 'FASTING'),
])
def test_fasting_preceding_attachment_geometry_has_bounded_edges(axis, outside, expected):
    # Binary fractions fix inclusive boundaries without decimal rounding:
    # vertical gap <= shorter line-height; left-edge delta <= half that height.
    step = 1 / 1024 if outside else 0
    rows = prefixed_request_panel('计划',
        qualifier_y=3 / 16 - step if axis == 'vertical' else 13 / 64,
        qualifier_x=1 / 8 + 1 / 64 + step if axis == 'horizontal' else 1 / 8)
    result = context(rows)
    assert result['time_slot'] == expected
    assert bool(result['time_slot_evidence']) == (expected == 'FASTING')


@pytest.mark.parametrize('planned', ['first', 'second', None])
def test_fasting_preceding_qualifier_is_local_and_global_uniqueness_is_preserved(planned):
    rows = prefixed_request_panel('计划' if planned == 'first' else None)
    second = block('检查项目：空腹葡萄糖', .5, x=1 / 8, width=.5, height=1 / 32, key='second-request')
    rows.append(second)
    if planned == 'second':
        rows.append(block('下次', .5 - 3 / 64, x=1 / 8, width=1 / 8, height=1 / 32, key='second-qualifier'))
    result = context(rows)
    expected_ids = {'first': ['second-request'], 'second': ['request-label', 'request-project'], None: []}[planned]
    assert result['time_slot'] == ('FASTING' if expected_ids else 'UNSPECIFIED')
    assert [part['block_id'] for part in result['time_slot_evidence']] == expected_ids


def test_fasting_preceding_qualifier_also_governs_a_single_block_request():
    rows = prefixed_request_panel('计划')
    rows = [row for row in rows if row['id'] != 'request-project']
    next(row for row in rows if row['id'] == 'request-label')['text'] = '检验项目：空腹血糖'
    result = context(rows)
    assert result['time_slot'] == 'UNSPECIFIED'
    assert result['time_slot_evidence'] == []
