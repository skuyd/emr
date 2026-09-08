"""Pure reference organization. A display boundary is never a treatment end."""
from copy import deepcopy
from datetime import date

from .signals import digest


BOUNDARY_KINDS = {"PAUSE", "DELAY", "STOP", "COMPLETION"}
REASONS = {
    "date_not_exact": "日期不明或未精确到日", "source_changed": "来源已变化，需重新核对",
    "ambiguous_cycles": "多个周期都可能关联，请明确选择", "before_first_anchor": "早于当前可用锚点",
    "no_current_anchor": "没有可用的精确锚点", "explicit_pause_boundary": "暂停、延迟或停止边界后，归属待核对",
    "user_left_unassigned": "已明确保留未分组", "cycle_unavailable": "原关联周期已失效或被替代",
    "cycle_not_selected": "所属周期不在当前选择中", "unconfirmed_cycle": "所属候选周期尚未确认",
    "automatic_interval": "按锚点区间组织", "manual_assignment": "已人工核对归属",
    "document_assignment": "沿用整份资料的人工归属", "regimen_change": "另一方案的锚点边界",
}


def exact_day(value, precision):
    if precision != "DAY" or not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _boundaries(material):
    result = []
    for event in material.get("events", []):
        content = event["content"]
        day = exact_day(content.get("date"), content.get("date_precision"))
        if (day and event.get("source_valid") and event.get("status") not in {"REJECTED", "SUPERSEDED", "STALE"}
                and content.get("kind") in BOUNDARY_KINDS and content.get("occurrence") == "OCCURRED"):
            result.append((day, event["id"]))
    return sorted(result)


def _windows(material):
    # Even a pending next anchor constrains the earlier confirmed interval;
    # hiding candidates must not silently extend an earlier treatment group.
    current = [deepcopy(row) for row in material["cycles"] if row.get("source_valid") and row["status"] in {"CONFIRMED", "PENDING"}]
    precise = [(exact_day(row["content"].get("anchor"), row["content"].get("anchor_precision")), row) for row in current]
    precise = [(day, row) for day, row in precise if day]
    barriers = _boundaries(material)
    for row in current:
        anchor = exact_day(row["content"].get("anchor"), row["content"].get("anchor_precision"))
        following = sorted((day, other["id"], other.get("regimen_id")) for day, other in precise if anchor and day > anchor)
        end = following[0][0] if following else None
        reason = ("next_anchor" if following[0][2] == row.get("regimen_id") else "regimen_change") if following else "open_boundary"
        boundary_events = [(day, identity) for day, identity in barriers if anchor and day >= anchor and (end is None or day < end)]
        if boundary_events:
            end, _ = boundary_events[0]
            reason = "explicit_pause_boundary"
        row.update({"group_end_exclusive": end.isoformat() if end else None, "boundary_open": end is None,
                    "boundary_reason": reason, "boundary_event_ids": [identity for day, identity in boundary_events if day == end],
                    "preview": row["status"] == "PENDING", "exact_anchor": anchor is not None})
    return sorted(current, key=lambda row: (not row["exact_anchor"], row["content"].get("anchor") or "", row["id"]))


def build_cycle_timeline(material, selection=None):
    selection = selection or {}
    windows = _windows(material)
    allowed = {row["id"] for row in windows if not row["preview"] or selection.get("include_pending_cycles") is True}
    selected = set(selection["cycle_ids"]) if "cycle_ids" in selection else allowed
    visible = allowed & selected
    records = deepcopy(material.get("records", []))
    manual = {(row["kind"], row["source_id"]): row for row in material.get("record_associations", [])}
    links = []
    for record in records:
        day = exact_day(record.get("date"), record.get("date_precision"))
        candidates = [row for row in windows if day and row["exact_anchor"] and row["content"]["anchor"] <= day.isoformat()
                      and (row["group_end_exclusive"] is None or day.isoformat() < row["group_end_exclusive"])]
        automatic = [row["id"] for row in candidates]
        cycle_id = automatic[0] if len(automatic) == 1 else None
        if not record.get("source_valid"):
            cycle_id, reason = None, "source_changed"
        elif not day:
            reason = "date_not_exact"
        elif len(automatic) > 1:
            reason = "ambiguous_cycles"
        elif cycle_id:
            reason = "automatic_interval"
        elif any(row["boundary_reason"] == "explicit_pause_boundary" and row["group_end_exclusive"] <= day.isoformat() for row in windows):
            reason = "explicit_pause_boundary"
        else:
            reason = "before_first_anchor" if any(row["exact_anchor"] for row in windows) else "no_current_anchor"
        explicit = manual.get((record["kind"], record["id"]))
        inherited = False
        if explicit is None and record["kind"] != "document":
            explicit = manual.get(("document", record["document_id"]))
            inherited = explicit is not None
        if explicit:
            if not explicit["source_valid"] or not record.get("source_valid"):
                cycle_id, reason = None, "source_changed"
            elif not explicit["assigned"]:
                cycle_id, reason = None, "user_left_unassigned"
            elif explicit["cycle_id"] not in {row["id"] for row in windows}:
                cycle_id, reason = None, "cycle_unavailable"
            else:
                cycle_id, reason = explicit["cycle_id"], "document_assignment" if inherited else "manual_assignment"
        if cycle_id and cycle_id not in allowed:
            cycle_id, reason = None, "unconfirmed_cycle"
        elif cycle_id and cycle_id not in visible:
            cycle_id, reason = None, "cycle_not_selected"
        links.append({"id": digest({"kind": record["kind"], "id": record["id"], "cycle": cycle_id,
                                     "decision": explicit["id"] if explicit else None}),
                      "kind": record["kind"], "source_id": record["id"], "document_id": record["document_id"],
                      "cycle_id": cycle_id, "origin": "USER" if explicit else "AUTOMATIC", "reason": reason,
                      "reason_label": REASONS.get(reason, reason), "candidate_cycle_ids": automatic if len(automatic) > 1 else [],
                      "automatic_cycle_ids": automatic, "decision_id": explicit["id"] if explicit else None})
    return {"cycles": [row for row in windows if row["id"] in visible], "records": records, "links": links,
            "document_ids": sorted({row["document_id"] for row in records}),
            "unassigned": [row for row in links if row["cycle_id"] is None],
            "regimens": deepcopy(material.get("regimens", []))}
