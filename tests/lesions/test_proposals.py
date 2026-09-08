from copy import deepcopy

from apps.lesions.proposals import propose_matches


def observed(identity, *, report=None, patient="patient-a", day="2026-08-01", site="左肺上叶",
             side="LEFT", body="胸部", method="CT", confirmed=True, reference=None, comparison=None):
    report = report or "report-" + identity
    def field(key, value):
        return {"id": identity + ":" + key, "field_key": key, "usable": confirmed, "conflict": False,
                "content": {"value": value, "raw_value": value.get("raw", value.get("text", ""))}}
    fields = [field("lesion.site", {"text": site}), field("lesion.laterality", {"code": side, "raw": site})]
    context = [field("report.exam_date", {"value": day, "precision": "DAY"}),
               field("imaging.modality", {"code": method, "raw": method}),
               field("imaging.body_site", {"text": body})]
    if reference:
        context.append(field("comparison.reference_date", {"value": reference, "precision": "DAY"}))
    if comparison:
        context.append(field("comparison.statement", {"text": comparison}))
    return {"id": identity, "patient_id": patient, "report_id": report, "site": [site],
            "date": {"value": day, "precision": "DAY"}, "source_usable": confirmed,
            "fields": fields, "context_fields": context, "source_token": "source-" + identity,
            "source_binding": {"report": {"id": report}, "field_ids": [row["id"] for row in fields + context]}}


def test_exact_source_location_and_reference_make_only_a_pending_explained_proposal():
    first = observed("one")
    second = observed("two", day="2026-09-01", reference="2026-08-01", comparison="对比既往检查，原文描述有变化。")
    original = deepcopy([first, second])
    proposals = propose_matches([first, second])
    assert len(proposals) == 1
    proposal = proposals[0]
    assert (proposal.first_id, proposal.second_id, proposal.status) == ("one", "two", "PENDING")
    assert {reason["code"] for reason in proposal.reasons} >= {"explicit_location_equal", "explicit_side_equal", "reference_date_equal"}
    reference = next(reason for reason in proposal.reasons if reason["code"] == "reference_date_equal")
    assert set(reference["field_ids"]) == {"two:comparison.reference_date", "one:report.exam_date"}
    assert proposal.first_binding == first["source_binding"] and proposal.second_binding == second["source_binding"]
    assert [first, second] == original


def test_similar_observations_in_same_report_are_all_candidates_not_nearest_size_matching():
    first = observed("one")
    later_a = observed("two", report="report-later", day="2026-09-01")
    later_b = observed("three", report="report-later", day="2026-09-01")
    for row, size in ((first, "12"), (later_a, "12"), (later_b, "28")):
        row["fields"].append({"id": row["id"] + ":dimensions", "field_key": "lesion.dimensions",
                              "usable": True, "conflict": False,
                              "content": {"value": {"components": [{"value": size, "unit": "mm"}]}}})
    proposals = propose_matches([first, later_a, later_b])
    assert {(row.first_id, row.second_id) for row in proposals} == {("one", "two"), ("one", "three")}
    assert all("multiple_candidates" in row.blockers for row in proposals)


def test_other_patient_opposite_side_other_organ_and_date_only_reference_do_not_form_proposals():
    first = observed("one")
    others = [observed("other-patient", patient="patient-b", day="2026-09-01"),
              observed("opposite", side="RIGHT", site="右肺上叶", day="2026-09-01"),
              observed("other-organ", site="左肾", body="腹部", day="2026-09-01"),
              observed("wrong-exam", body="颅脑", method="MR", day="2026-09-01", reference="2026-08-01")]
    for other in others:
        assert propose_matches([first, other]) == []


def test_unknown_date_unconfirmed_and_uncertain_comparison_are_visible_limitations():
    first = observed("one")
    second = observed("two", confirmed=False, day="2026-09-01", comparison="不能除外与既往所见相关。")
    date_field = next(row for row in second["context_fields"] if row["field_key"] == "report.exam_date")
    date_field["content"]["value"] = {"value": None, "precision": "UNKNOWN"}
    second["date"] = None
    rows = propose_matches([first, second])
    assert len(rows) == 1
    assert {"unconfirmed_source", "date_unreliable", "uncertain_comparison"} <= set(rows[0].blockers)


def test_proposal_fingerprint_is_order_independent_and_changes_with_either_source_binding():
    first, second = observed("one"), observed("two", day="2026-09-01")
    initial = propose_matches([first, second])
    assert len(initial) == 1
    reversed_input = propose_matches([second, first])
    assert initial[0].fingerprint == reversed_input[0].fingerprint
    second["source_binding"]["report"]["revision_number"] = 2
    changed = propose_matches([first, second])
    assert changed[0].fingerprint != initial[0].fingerprint


def test_new_ambiguous_candidate_does_not_replace_same_source_pair_decision_identity():
    first, second = observed("one"), observed("two", report="later", day="2026-09-01")
    original = propose_matches([first, second])[0]
    another = observed("three", report="later", day="2026-09-01")
    changed = next(row for row in propose_matches([first, second, another]) if row.second_id == "two")
    assert "multiple_candidates" in changed.blockers and "multiple_candidates" not in original.blockers
    assert changed.fingerprint == original.fingerprint
