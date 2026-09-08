"""Grouping references must preserve files and explicitly retain ambiguity."""
from copy import deepcopy


def cycle_row(identity, anchor, *, regimen="scheme-a", status="CONFIRMED", precision="DAY"):
    return {"id": identity, "regimen_id": regimen, "status": status, "source_valid": True,
            "usable": status == "CONFIRMED", "content": {"anchor": anchor, "anchor_precision": precision,
            "ordinal": None, "end": None}, "event_links": []}


def record(identity, day, *, kind="document", document=None, precision="DAY", **extra):
    return {"id": identity, "kind": kind, "document_id": document or identity, "date": day,
            "date_precision": precision, "source_valid": True, "label": identity, **extra}


def material(cycles=(), records=(), **extra):
    return {"cycles": list(cycles), "records": list(records), "regimens": [], "events": [], "record_associations": [], **extra}


def test_organization_is_half_open_and_last_cycle_is_not_an_actual_end():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c1", "2024-02-29"), cycle_row("c2", "2024-03-21")],
                      [record("a", "2024-02-29"), record("b", "2024-03-20"), record("c", "2024-03-21"), record("d", "2024-06-01")])
    before = deepcopy(source)
    result = build_cycle_timeline(source)
    assert {row["source_id"]: row["cycle_id"] for row in result["links"]} == {"a": "c1", "b": "c1", "c": "c2", "d": "c2"}
    assert result["cycles"][0]["group_end_exclusive"] == "2024-03-21"
    assert result["cycles"][1]["boundary_open"] and result["cycles"][1]["content"]["end"] is None
    assert source == before


def test_same_day_anchors_keep_every_record_unassigned_with_candidate_ids():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c1", "2024-02-29"), cycle_row("c2", "2024-02-29")], [record("r", "2024-03-01")])
    row = build_cycle_timeline(source)["links"][0]
    assert row["cycle_id"] is None and row["reason"] == "ambiguous_cycles" and set(row["candidate_cycle_ids"]) == {"c1", "c2"}


def test_unknown_or_mixed_document_date_never_uses_upload_day():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c1", "2024-02-29")], [record("unknown", None, precision="UNKNOWN", uploaded_at="2024-03-02"),
        record("month", "2024-03", precision="MONTH"), record("whole", None, precision="UNKNOWN"),
        record("r1", "2024-03-01", kind="report", document="whole"), record("r2", "2024-04-01", kind="report", document="whole")])
    result = build_cycle_timeline(source)
    links = {row["source_id"]: row for row in result["links"]}
    assert all(links[key]["reason"] == "date_not_exact" for key in ["unknown", "month", "whole"])
    assert links["r1"]["cycle_id"] == links["r2"]["cycle_id"] == "c1"
    assert result["document_ids"] == ["month", "unknown", "whole"]
    assert len(result["records"]) == 5


def test_pause_breaks_automatic_grouping_until_a_new_anchor_and_never_sets_treatment_end():
    from apps.treatments.timeline import build_cycle_timeline
    event = {"id": "pause", "usable": True, "source_valid": True, "status": "CONFIRMED",
             "content": {"kind": "PAUSE", "occurrence": "OCCURRED", "date": "2024-03-05", "date_precision": "DAY"}}
    source = material([cycle_row("c1", "2024-02-29"), cycle_row("c2", "2024-03-21")],
        [record("before", "2024-03-04"), record("during", "2024-03-06"), record("after", "2024-03-22")], events=[event])
    result = build_cycle_timeline(source)
    assert [row["cycle_id"] for row in result["links"]] == ["c1", None, "c2"]
    assert result["links"][1]["reason"] == "explicit_pause_boundary"
    assert result["cycles"][0]["content"]["end"] is None


def test_regimen_change_does_not_extend_prior_cycle_into_new_scheme():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c1", "2024-02-29"), cycle_row("c2", "2024-03-21", regimen="scheme-b")],
                      [record("before", "2024-03-20"), record("after", "2024-03-22")])
    result = build_cycle_timeline(source)
    assert [row["cycle_id"] for row in result["links"]] == ["c1", "c2"]
    assert result["cycles"][0]["boundary_reason"] == "regimen_change"


def test_pending_preview_is_explicit_and_invalid_or_rejected_cycles_never_group():
    from apps.treatments.timeline import build_cycle_timeline
    pending, rejected = cycle_row("p", "2024-02-29", status="PENDING"), cycle_row("r", "2024-04-01", status="REJECTED")
    source = material([pending, rejected], [record("one", "2024-03-01")])
    assert build_cycle_timeline(source)["links"][0]["cycle_id"] is None
    preview = build_cycle_timeline(source, {"include_pending_cycles": True})
    assert preview["links"][0]["cycle_id"] == "p" and preview["cycles"][0]["preview"]
    assert len(preview["cycles"]) == 1


def test_manual_unassigned_and_changed_source_override_automatic_links_without_losing_original_reason():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c", "2024-02-29")], [record("one", "2024-03-01"), record("two", "2024-03-02")],
        record_associations=[{"id": "l1", "kind": "document", "source_id": "one", "cycle_id": "c", "source_valid": True,
                              "assigned": False, "origin": "USER"},
                             {"id": "l2", "kind": "document", "source_id": "two", "cycle_id": "c", "source_valid": False,
                              "assigned": True, "origin": "USER"}])
    result = build_cycle_timeline(source)
    assert [row["cycle_id"] for row in result["links"]] == [None, None]
    assert [row["reason"] for row in result["links"]] == ["user_left_unassigned", "source_changed"]
    assert all(row["automatic_cycle_ids"] == ["c"] for row in result["links"])


def test_selecting_cycles_does_not_reassign_data_from_an_omitted_anchor():
    from apps.treatments.timeline import build_cycle_timeline
    source = material([cycle_row("c1", "2024-02-29"), cycle_row("c2", "2024-03-21")], [record("r", "2024-03-22")])
    result = build_cycle_timeline(source, {"cycle_ids": ["c1"]})
    assert result["links"][0]["cycle_id"] is None and result["links"][0]["reason"] == "cycle_not_selected"
    assert result["cycles"][0]["group_end_exclusive"] == "2024-03-21"
