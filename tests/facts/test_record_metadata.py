from apps.facts.metadata import page_record_dates


def lines(pages):
    return [(number,text,[]) for number, texts in enumerate(pages,1) for text in texts]


def test_report_date_precedes_sampling_date_and_never_leaks_to_other_bundle_report():
    result=page_record_dates(lines([["采样时间：2026-08-01", "报告时间：2026-08-03"],
                                   ["采样时间：2026-08-10", "报告日期：2026年8月11日"],
                                   ["本页没有日期"]]))
    assert result[1]["value"]=="2026-08-03"
    assert result[2]["value"]=="2026-08-11"
    assert result[3]["value"] is None
    assert result[3]["precision"]=="UNKNOWN"


def test_one_explicit_record_date_can_apply_to_continuation_pages_but_conflicts_stay_unknown():
    result=page_record_dates(lines([["记录时间：2026年8月"], ["无日期的续页"]]))
    assert result[2]["value"]=="2026-08"
    assert result[2]["precision"]=="MONTH"
    result=page_record_dates(lines([["报告日期：2026-08-03", "报告时间：2026-08-04"]]))
    assert result[1]["value"] is None
    assert result[1]["conflict"] is True
    assert "2026-08-03" in result[1]["raw"] and "2026-08-04" in result[1]["raw"]


def test_event_and_admission_dates_are_not_treated_as_report_creation_dates():
    result=page_record_dates(lines([["手术日期：2026-07-01", "入院日期：2026-06-30"]]))
    assert result=={}


def test_ocr_date_touching_clock_time_retains_day_not_an_invented_month():
    result=page_record_dates(lines([["报告时间：2026-08-0314:25:59"]]))
    assert result[1]["value"]=="2026-08-03"
    assert result[1]["precision"]=="DAY"


def test_literal_treatment_date_parser_rejects_partial_match_of_malformed_full_date():
    from apps.facts.extraction import explicit_dates
    assert explicit_dates("2026-08-0314:25")[0]["value"]=="2026-08-03"
    assert explicit_dates("2026-08-03142")==[]


def test_blank_report_date_does_not_adopt_later_event_date_on_same_line():
    result = page_record_dates(lines([["报告日期：未注明 手术日期：2026-07-01"]]))
    assert result[1]["value"] is None
    assert result[1]["precision"] == "UNKNOWN"
