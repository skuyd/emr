"""Explicit derived selections, calculated once from the full authorized context.

Filtering never recomputes a different previous result, baseline or cycle window.
Missing selected dependencies redact the affected value and its hidden identities.
"""

from copy import deepcopy

from django.core.exceptions import PermissionDenied

from apps.treatments.overlays import METRICS, NODE_LABELS, build_cycle_overlays
from apps.treatments.signals import RULE_VERSION, digest
from apps.treatments.timeline import build_cycle_timeline

from .errors import ExportInputError, SnapshotChanged
from .selection import identifiers


SELECTION_KEYS = ("treatment_event_ids", "regimen_ids", "cycle_ids", "personal_change_ids")
ARRAYS = ("treatment_events", "treatment_regimens", "treatment_cycles", "cycle_links", "cycle_points",
          "cycle_key_nodes", "personal_changes", "derived_sources")
MISSING = "missing_selected_source"
BASE_FIELDS = ("id", "origin", "status", "revision_number")
EVENT_FIELDS = ("kind", "title", "occurrence", "date", "date_precision", "date_raw", "regimen_text",
                "cycle_ordinal", "cycle_day", "note", "recorded_as", "limitations", "order_start", "order_stop",
                "order_checked", "execution_status")
CYCLE_FIELDS = ("anchor", "anchor_precision", "ordinal", "end", "end_precision", "anchor_role",
                "true_d1_claimed", "note", "basis", "hospital_interval")
SOURCE_FIELDS = ("document_id", "page", "page_id", "parsing_version", "fact_id", "source_evidence_id",
                 "evidence_id", "source_kind", "text_basis", "source_revision", "lifecycle_revision",
                 "material_revision", "polygon", "location", "source_token", "current_source_token")
CHANGE_VALUES = ("absolute_change", "daily_change", "previous_percentage", "baseline_mean", "baseline_percentage")
CSV_BASE = [*BASE_FIELDS, "rule_version", "source_complete", "reason"]
CSV_FIELDS = {
    "treatment_events": [*CSV_BASE, "content", "source_ids", "dependency_only"],
    "treatment_regimens": [*CSV_BASE, "content", "event_ids"],
    "treatment_cycles": [*CSV_BASE, "regimen_id", "content", "event_ids", "preview", "boundary_open"],
    "cycle_links": ["id", "cycle_id", "kind", "source_id", "document_id", "origin", "role", "source_token", "reason"],
    "cycle_points": ["id", "cycle_id", "regimen_id", "observation_id", "document_id", "date", "relative_day", "value", "unit",
                     "standard_code", "group_key", "source_token", "source_ids", "labels", "label_text", "preview", "context_only", "reason"],
    "cycle_key_nodes": ["id", "point_id", "cycle_id", "observation_id", "kind", "label", "event_ids", "group_key", "reason"],
    "personal_changes": ["id", "observation_id", "document_id", "standard_code", "label", "date", "unit", "current_value",
                         "previous_observation_id", "baseline_observation_ids", "elapsed_days", *CHANGE_VALUES, "previous_reason",
                         "baseline_reason", "threshold_percent", "highlight", "source_ids", "source_complete", "reason"],
    "derived_sources": ["id", "kind", "source_id", "event_id", *SOURCE_FIELDS, "revision_number", "revision_id", "date",
                        "date_precision", "numeric_value", "unit", "standard_code", "group_key", "source_context_omitted"],
}


def normalized_selection(selection):
    result = {key: identifiers(selection.get(key, [])) for key in SELECTION_KEYS}
    result["include_pending_cycles"] = selection.get("include_pending_cycles", False)
    if type(result["include_pending_cycles"]) is not bool:
        raise ExportInputError("请明确是否纳入候选周期。")
    result["cycle_mode"] = selection.get("cycle_mode", "key")
    if result["cycle_mode"] not in ("key", "full"):
        raise ExportInputError("周期明细模式无效。")
    metrics = selection.get("cycle_metric_codes", list(METRICS))
    if not isinstance(metrics, list) or any(not isinstance(key, str) or key not in METRICS for key in metrics):
        raise ExportInputError("周期指标选择无效。")
    result["cycle_metric_codes"] = list(dict.fromkeys(metrics))
    return result


def has_selection(selection):
    return any(normalized_selection(selection)[key] for key in SELECTION_KEYS)


def selected_material(patient, selection):
    """Caller already holds the actual actor's Patient access guard."""
    if not has_selection(selection):
        return None
    from apps.documents.models import Document
    from apps.treatments.sources import lock_source_documents
    from apps.treatments.workspace import trusted_workspace_material

    # Even a user-only cycle may depend on current laboratory or boundary context.
    lock_source_documents(patient, Document.objects.filter(patient=patient, deleted_at__isnull=True).values_list("pk", flat=True))
    return trusted_workspace_material(patient, include_history=True)


def assert_current(patient, snapshot):
    try:
        material = selected_material(patient, snapshot.get("selection", {}))
    except (PermissionDenied, ExportInputError, ValueError, TypeError):
        raise SnapshotChanged("治疗或个人变化来源已不可用，请重新选择并生成。") from None
    expected = snapshot.get("treatment_fingerprint")
    actual = material["fingerprint"] if material else None
    if actual != expected:
        raise SnapshotChanged("治疗决定、周期边界或个人变化来源已变化，请重新选择并生成。")


def _chosen(rows, ids, *, pending=False):
    selected = {row["id"]: row for row in rows if row["id"] in ids}
    if set(selected) != set(ids):
        raise PermissionDenied("选定派生记录不可用。")
    if any(not row.get("source_valid", True) or row.get("status", "CONFIRMED") not in
           ({"CONFIRMED", "PENDING"} if pending else {"CONFIRMED"}) for row in selected.values()):
        raise ExportInputError("部分治疗尚待确认或已失效；请先核对，或明确纳入候选附页。")
    return selected


def _event_documents(row):
    return {source["document_id"] for source in row["sources"]}


def _take(row, keys):
    return {key: deepcopy(row[key]) for key in keys if key in row}


def _base(row, complete):
    return {**_take(row, BASE_FIELDS), "rule_version": row.get("rule_version", RULE_VERSION),
            "source_complete": complete, "reason": "" if complete else MISSING}


def _closure(material, scope):
    pending = scope["include_pending_cycles"]
    all_events = {row["id"]: row for row in material["events"]}
    all_regimens = {row["id"]: row for row in material["regimens"]}
    cycles = _chosen(material["cycles"], scope["cycle_ids"], pending=pending)
    regimens = _chosen(material["regimens"], scope["regimen_ids"], pending=pending)
    events = _chosen(material["events"], scope["treatment_event_ids"], pending=pending)
    # Selecting a regimen includes its current selected-status organization;
    # selecting one cycle only includes that cycle's necessary event witnesses.
    for row in material["cycles"]:
        if row.get("regimen_id") in regimens and row["source_valid"] and (row["usable"] or pending and row["status"] == "PENDING"):
            cycles[row["id"]] = row
    for row in list(regimens.values()):
        events.update({key: all_events[key] for key in row["content"].get("event_tokens", {})})
    for row in cycles.values():
        events.update({link["event_id"]: all_events[link["event_id"]] for link in row["event_links"]})
        if row.get("regimen_id"):
            regimens[row["regimen_id"]] = all_regimens[row["regimen_id"]]
    return events, regimens, cycles


def selection_dependencies(material, selection):
    """Private preparation view only; never part of the portable/share payload."""
    scope = normalized_selection(selection)
    if material is None:
        return []
    events, regimens, cycles = _closure(material, scope)
    required = set().union(*(_event_documents(row) for row in events.values())) if events else set()
    all_events = {row["id"]: row for row in material["events"]}
    for row in regimens.values():
        for key in row["content"].get("event_tokens", {}):
            required.update(_event_documents(all_events[key]))
        required.update(proof.get("document_id") for auxiliary in row["content"].get("lab_periodicity", [])
                        for proof in auxiliary.get("sources", []) if proof.get("document_id"))
    records = {row["id"]: row for row in material["records"] if row["kind"] == "observation"}
    for change in material["personal_changes"]:
        if change["id"] in scope["personal_change_ids"]:
            required.update(records[key]["document_id"] for key in
                            [change["observation_id"], change["previous_observation_id"], *change["baseline_observation_ids"]]
                            if key in records)
    timeline = build_cycle_timeline(material, {**scope, "cycle_ids": list(cycles)})
    for point in build_cycle_overlays(timeline, material, scope)["points"]:
        required.add(point["document_id"])
    selected = set(selection.get("document_ids", []))
    return [{"document_id": identity, "selected": identity in selected} for identity in sorted(required)]


def treatment_projection(material, selection):
    result = {key: [] for key in ARRAYS}
    scope = normalized_selection(selection)
    if material is None:
        return result
    documents = set(selection.get("document_ids", []))
    events, regimens, cycles = _closure(material, scope)
    all_events = {row["id"]: row for row in material["events"]}
    all_records = {(row["kind"], row["id"]): row for row in material["records"]}
    observations = {row["id"]: row for row in material["records"] if row["kind"] == "observation"}
    sources = {}

    def add_observation(row):
        identity = "observation:" + row["id"]
        sources[identity] = {"id": identity, "kind": "observation", "source_id": row["id"],
                            **_take(row, ("document_id", "date", "date_precision", "numeric_value", "unit", "standard_code",
                                          "group_key", "source_token", "revision_number", "revision_id", "parsing_version", "page", "evidence_id"))}

    event_complete = {}
    for row in events.values():
        complete = _event_documents(row) <= documents
        event_complete[row["id"]] = complete
        content = _take(row["content"], EVENT_FIELDS) if complete else {key: None for key in EVENT_FIELDS}
        if complete and row["origin"] == "AUTOMATIC":
            # A source clause may describe several unselected dates or events.
            content["title"] = content.get("regimen_text") or content["kind"]
        ids = []
        if complete:
            for proof in row["sources"]:
                identity = "treatment_evidence:" + proof["id"]
                sources[identity] = {"id": identity, "kind": "treatment_evidence", "source_id": proof["id"],
                                    "event_id": row["id"], **_take(proof, SOURCE_FIELDS), "source_context_omitted": True}
                ids.append(identity)
            if not row["sources"]:
                identity = "treatment_event:" + row["id"]
                sources[identity] = {"id": identity, "kind": "user_treatment", "source_id": row["id"],
                                    "event_id": row["id"], "revision_number": row["revision_number"]}
                ids.append(identity)
        result["treatment_events"].append({**_base(row, complete), "content": content, "source_ids": ids,
                                            "dependency_only": row["id"] not in scope["treatment_event_ids"]})

    for row in regimens.values():
        content = row["content"]
        required = set().union(*(_event_documents(all_events[key]) for key in content.get("event_tokens", {}))) if content.get("event_tokens") else set()
        complete = required <= documents
        auxiliary = deepcopy(content.get("lab_periodicity", []))
        for item in auxiliary:
            proof_ids = [proof.get("id") for proof in item.get("sources", [])]
            if any(key not in observations or observations[key]["document_id"] not in documents for key in proof_ids):
                item.clear()
                item.update(reason=MISSING, minima_days=None, cadence=None, source_ids=[])
            else:
                for key in proof_ids:
                    add_observation(observations[key])
                item.pop("sources", None)
                item["source_ids"] = ["observation:" + key for key in proof_ids]
        result["treatment_regimens"].append({**_base(row, complete),
            "content": {"text": content["text"] if complete else None, "note": content.get("note", "") if complete else None,
                        "actual_start": content.get("actual_start") if complete else None, "actual_end": content.get("actual_end") if complete else None,
                        "cadence": deepcopy(content.get("cadence")) if complete else None, "lab_periodicity": auxiliary if complete else []},
            "event_ids": [key for key in content.get("event_tokens", {}) if key in events and event_complete[key]]})

    timeline = build_cycle_timeline(material, {**scope, "cycle_ids": list(cycles)})
    organized = {row["id"]: row for row in timeline["cycles"]}
    complete_cycles = set()
    for row in cycles.values():
        complete = all(event_complete[link["event_id"]] for link in row["event_links"])
        content = _take(row["content"], CYCLE_FIELDS) if complete else {key: None for key in CYCLE_FIELDS}
        if complete:
            complete_cycles.add(row["id"])
        result["treatment_cycles"].append({**_base(row, complete), "regimen_id": row.get("regimen_id"),
            "content": content, "event_ids": [link["event_id"] for link in row["event_links"]] if complete else [],
            "preview": row["status"] == "PENDING", "boundary_open": organized.get(row["id"], {}).get("boundary_open", True)})
        if complete:
            for link in row["event_links"]:
                result["cycle_links"].append({"id": digest({"cycle": row["id"], "event": link["event_id"]}),
                    "cycle_id": row["id"], "kind": "event", "source_id": link["event_id"], "origin": row["origin"],
                    "role": link["role"], "source_token": link["source_token"]})
    for link in timeline["links"]:
        record = all_records[(link["kind"], link["source_id"])]
        if link["cycle_id"] in complete_cycles and record["document_id"] in documents:
            result["cycle_links"].append({"id": digest(link), **_take(link, ("cycle_id", "kind", "source_id", "origin", "reason")),
                                          "document_id": record["document_id"], "source_token": record.get("source_token")})
    overlays = build_cycle_overlays(timeline, material, scope)
    # A minimum/latest label depends on every point in that cycle/group. Keep the
    # original choice or redact it; filtering cannot promote a different minimum.
    points_by_group = {}
    for point in overlays["points"]:
        if not point["context_only"]:
            points_by_group.setdefault((point["cycle_id"], tuple(point["group_key"])), []).append(point)
    for point in overlays["points"]:
        if point["cycle_id"] not in complete_cycles or point["document_id"] not in documents:
            continue
        group = points_by_group.get((point["cycle_id"], tuple(point["group_key"])), [])
        group_complete = all(other["document_id"] in documents for other in group)
        labels = [label for label in point["labels"] if label not in {"OBSERVED_MIN", "LATEST"} or group_complete]
        if scope["cycle_mode"] == "key" and not labels:
            continue
        public = {key: deepcopy(value) for key, value in point.items() if key not in {"url", "raw_value"}}
        public.update(labels=labels, label_text="、".join(NODE_LABELS[label] for label in labels),
                      source_ids=["observation:" + point["observation_id"]], reason="" if group_complete else MISSING)
        result["cycle_points"].append(public)
        add_observation(observations[point["observation_id"]])
        result["cycle_key_nodes"].extend(deepcopy(node) for node in overlays["key_nodes"]
                                         if node["point_id"] == point["id"] and node["kind"] in labels)

    visible_groups = {(point["cycle_id"], tuple(point["group_key"])) for point in overlays["points"]
                      if point["cycle_id"] in complete_cycles and point["document_id"] in documents}
    missing = [row for row in overlays["missing_nodes"] if row["cycle_id"] in complete_cycles
               and ("group_key" not in row or (row["cycle_id"], tuple(row["group_key"])) in visible_groups)]
    node_ids = {row["id"] for row in result["cycle_key_nodes"]}
    overlay_points = {row["id"]: row for row in overlays["points"]}
    for node in overlays["key_nodes"]:
        point = overlay_points[node["point_id"]]
        if node["id"] not in node_ids and (point["cycle_id"], tuple(point["group_key"])) in visible_groups:
            missing.append({"cycle_id": point["cycle_id"], "kind": node["kind"], "label": node["label"],
                            "group_key": point["group_key"], "reason": MISSING})
    missing_rows = {}
    for row in missing:
        row = _take(row, ("cycle_id", "kind", "label", "group_key", "reason"))
        identity = digest(row)
        missing_rows[identity] = {"id": identity, **row, "point_id": None, "observation_id": None, "event_ids": []}
    result["cycle_key_nodes"].extend(missing_rows.values())

    changes = _chosen(material["personal_changes"], scope["personal_change_ids"])
    for change in changes.values():
        row = observations[change["observation_id"]]
        complete = row["document_id"] in documents
        public = deepcopy(change)
        public.update(standard_code=row["standard_code"] if complete else None, label=row["label"] if complete else None,
                      date=row["date"] if complete else None, unit=row["unit"] if complete else None,
                      current_value=row["numeric_value"] if complete else None, source_ids=[], source_complete=complete,
                      reason="" if complete else MISSING)
        if complete:
            add_observation(row)
            public["source_ids"].append("observation:" + row["id"])
        else:
            public["document_id"] = None
        previous = observations.get(change["previous_observation_id"])
        baseline = [observations[key] for key in change["baseline_observation_ids"]]
        if not complete or previous and previous["document_id"] not in documents:
            public.update(previous_observation_id=None, elapsed_days=None, absolute_change=None,
                          daily_change=None, previous_percentage=None, previous_reason=MISSING)
        elif previous:
            add_observation(previous)
            public["source_ids"].append("observation:" + previous["id"])
        if not complete or any(item["document_id"] not in documents for item in baseline):
            public.update(baseline_observation_ids=[], baseline_mean=None, baseline_percentage=None,
                          baseline_reason=MISSING, highlight=False)
        else:
            for item in baseline:
                add_observation(item)
                public["source_ids"].append("observation:" + item["id"])
        public["source_ids"] = list(dict.fromkeys(public["source_ids"]))
        result["personal_changes"].append(public)
    result["derived_sources"] = list(sources.values())
    return result


def has_independent_source(projection):
    return any(row["origin"] == "USER" and row["source_complete"] for row in projection["treatment_events"])


def binding_ids(material, projection):
    if material is None:
        return {}
    return {"event": [row["id"] for row in projection["treatment_events"]],
            "regimen": [row["id"] for row in projection["treatment_regimens"]],
            "cycle": [row["id"] for row in projection["treatment_cycles"]],
            "document": [row["id"] for row in material["records"] if row["kind"] == "document"],
            "observation": sorted({row["source_id"] for row in projection["derived_sources"] if row["kind"] == "observation"}
                                  | {row["observation_id"] for row in projection["personal_changes"]})}


def bind_output(output, snapshot, *, sharing=False):
    from apps.treatments.models import TreatmentExportSource, TreatmentShareSource

    model = TreatmentShareSource if sharing else TreatmentExportSource
    target = "share" if sharing else "job"
    rows = []
    for kind, ids in snapshot.get("treatment_binding_ids", {}).items():
        for identity in ids:
            row = model(**{target: output, kind + "_id": identity})
            row.clean()
            rows.append(row)
    model.objects.bulk_create(rows)


def bindings_current(output, snapshot):
    from apps.treatments.models import OUTPUT_TARGETS
    expected = {key: sorted(ids) for key, ids in snapshot.get("treatment_binding_ids", {}).items()}
    actual = {key: [] for key in expected}
    for row in output.treatment_sources.all():
        for key in OUTPUT_TARGETS:
            identity = getattr(row, key + "_id")
            if identity is not None:
                actual.setdefault(key, []).append(str(identity))
    return expected == {key: sorted(ids) for key, ids in actual.items()}
