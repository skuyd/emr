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
