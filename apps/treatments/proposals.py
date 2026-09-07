"""Unconfirmed organization hypotheses, never claims of medical cycle truth."""

from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from .signals import RULE_VERSION, digest, extract_treatment_signals, normalized_regimen


ANCHOR_KINDS = {"SYSTEMIC_TREATMENT", "CELL_THERAPY", "RADIOTHERAPY", "SURGERY", "LOCAL_PROCEDURE"}
REGIMEN_KINDS = {"SYSTEMIC_TREATMENT", "CELL_THERAPY", "RADIOTHERAPY"}


def _events(extraction):
    grouped = {}
    for signal in extraction["signals"]:
        content = signal["content"]
        # Unknown dates cannot prove that two similar mentions describe one event.
        key = digest({"patient": signal["patient_id"], "kind": content["kind"], "date": content["date"],
                      "precision": content["date_precision"], "occurrence": content["occurrence"],
                      "regimen": normalized_regimen(content["regimen_text"]), "ordinal": content["cycle_ordinal"],
                      "day": content["cycle_day"], "unknown_identity": signal["id"] if content["date"] is None else None})
        if key not in grouped:
            grouped[key] = {**deepcopy(signal), "id": key, "source_key": key}
        else:
            grouped[key]["sources"].extend(deepcopy(signal["sources"]))
    for event in grouped.values():
        event["sources"] = list({digest(source): source for source in event["sources"]}.values())
        # Source or rule changes create a fresh input identity; an old rejection
        # therefore cannot silently decide an updated candidate.
        event["id"] = event["source_key"] = digest({"semantic": event["id"], "sources": event["sources"], "rule": RULE_VERSION})
    return sorted(grouped.values(), key=lambda e: (e["patient_id"], e["content"]["date"] or "9999", e["id"]))


def _cadence(days):
    actual = sorted({date.fromisoformat(day) for day in days})
    if len(actual) < 3:
        return None
    intervals = [(right - left).days for left, right in zip(actual, actual[1:])]
    middle = median(intervals)
    threshold = max(2, middle * 0.20)
    return {"anchor_count": len(actual), "actual_intervals": intervals, "median_days": middle,
            "maximum_deviation": max(abs(n - middle) for n in intervals),
            "similar_intervals": all(abs(n - middle) <= threshold for n in intervals),
            "tolerance_days": threshold, "medical_cycle_length": False}


def _regimens(events):
    regimens, active = [], {}
    for event in events:
        content, patient = event["content"], event["patient_id"]
        if content["occurrence"] != "OCCURRED" or content.get("status") not in {"PENDING", "CONFIRMED"}:
            continue
        if content["kind"] in {"PAUSE", "DELAY", "STOP"}:
            for context, current in active.items():
                if context[0] == patient:
                    current["context_end"] = {"event_id": event["id"], "date": content["date"],
                                              "date_precision": content["date_precision"]}
            active = {key: value for key, value in active.items() if key[0] != patient}
            continue
        if content["kind"] not in REGIMEN_KINDS:
            continue
        text = content["regimen_text"]
        normalized = normalized_regimen(text)
        key = digest(normalized)
        context = (patient, content["kind"])
        current = active.get(context)
        if current is None or current["normalized_key"] != key:
            if current is not None:
                current["context_end"] = {"event_id": event["id"], "date": content["date"],
                                          "date_precision": content["date_precision"]}
            episode = digest({"patient": patient, "normalized": key, "first_event": event["id"], "rule": RULE_VERSION})
            current = {"id": episode, "source_key": episode, "patient_id": patient,
                       "normalized_key": key, "episode_key": episode,
                       "content": {"text": text, "status": "PENDING", "actual_start": None, "actual_end": None,
                                   "note": "原文相同方案的发生段提议；首末记录不表示治疗起止。"},
                       "event_ids": [], "cadence": None, "context_end": None}
            regimens.append(current)
            active[context] = current
        current["event_ids"].append(event["id"])
        event["regimen_id"] = current["id"]
    return regimens


def _hospital_intervals(events):
    intervals = []
    for event in events:
        content = event["content"]
        if (content["kind"] != "ADMISSION" or content["date_precision"] != "DAY" or content["occurrence"] != "OCCURRED"
                or content.get("status") not in {"PENDING", "CONFIRMED"}):
            continue
        documents = {s["document_id"] for s in event["sources"]}
        ends = [other for other in events if other["patient_id"] == event["patient_id"]
                and other["content"]["kind"] == "DISCHARGE" and other["content"]["date_precision"] == "DAY"
                and other["content"]["occurrence"] == "OCCURRED" and other["content"]["date"] >= content["date"]
                and other["content"].get("status") in {"PENDING", "CONFIRMED"}
                and any(s["document_id"] in documents for s in other["sources"])]
        end_dates = {other["content"]["date"] for other in ends}
        intervals.append({"patient_id": event["patient_id"], "admission": event, "discharges": ends if len(end_dates) == 1 else [],
                          "start": content["date"], "end": next(iter(end_dates)) if len(end_dates) == 1 else None,
                          "documents": documents, "used": False})
    return intervals


def _cycles(events):
    groups, intervals = {}, _hospital_intervals(events)
    for event in events:
        content = event["content"]
        if (content["kind"] not in ANCHOR_KINDS or content["occurrence"] != "OCCURRED"
                or content.get("status") not in {"PENDING", "CONFIRMED"}):
            continue
        anchor = content["date"] if content["date_precision"] == "DAY" else None
        role = "REPORTED_EVENT_CLUE" if anchor else "DATE_UNKNOWN"
        if anchor and content["cycle_day"]:
            try:
                anchor = (date.fromisoformat(anchor) - timedelta(days=content["cycle_day"] - 1)).isoformat()
                role = "EXPLICIT_DAY_OFFSET"
            except (OverflowError, ValueError):
                anchor, role = None, "DAY_OFFSET_OUT_OF_RANGE"
        matches = [interval for interval in intervals if interval["patient_id"] == event["patient_id"]
                   and interval["end"] and anchor and interval["start"] <= anchor <= interval["end"]
                   and any(s["document_id"] in interval["documents"] for s in event["sources"])]
        stay = matches[0] if len(matches) == 1 and content["cycle_ordinal"] is None else None
        grouping = ("stay", stay["admission"]["id"]) if stay else ("anchor", anchor or event["id"])
        key = digest({"patient": event["patient_id"], "regimen": event.get("regimen_id"),
                      "group": grouping, "ordinal": content["cycle_ordinal"], "role": role})
        if key not in groups:
            groups[key] = {"patient_id": event["patient_id"], "regimen_id": event.get("regimen_id"), "event_ids": [],
                           "content": {"anchor": anchor, "anchor_precision": "DAY" if anchor else "UNKNOWN",
                                       "ordinal": content["cycle_ordinal"], "end": None, "end_precision": "UNKNOWN",
                                       "anchor_role": role, "true_d1_claimed": False, "status": "PENDING", "note": "",
                                       "basis": ["组织候选，尚待核对；原文发生日期不等于已证实的周期起点。"],
                                       "hospital_interval": {"start": stay["start"], "end": stay["end"]} if stay else None}}
        group = groups[key]
        group["event_ids"].append(event["id"])
        if stay:
            stay["used"] = True
            group["event_ids"].extend([stay["admission"]["id"], *[e["id"] for e in stay["discharges"]]])
            group["content"]["anchor"] = min(group["content"]["anchor"], anchor)
            group["content"]["basis"] = ["同一资料所述住院区间内的治疗线索，以最早明确治疗日提出组织锚点。"]
    for stay in intervals:
        if stay["used"]:
            continue
        groups["admission:" + stay["admission"]["id"]] = {
            "patient_id": stay["patient_id"], "regimen_id": None,
            "event_ids": [stay["admission"]["id"], *[e["id"] for e in stay["discharges"]]],
            "content": {"anchor": stay["start"], "anchor_precision": "DAY", "ordinal": None, "end": None,
                        "end_precision": "UNKNOWN", "status": "PENDING", "anchor_role": "ADMISSION_CLUE", "note": "",
                        "true_d1_claimed": False, "hospital_interval": {"start": stay["start"], "end": stay["end"]},
                        "basis": ["住院线索锚点；不表示已发生化疗。"]}}
    for item in groups.values():
        item["event_ids"] = sorted(set(item["event_ids"]))
        item["id"] = item["source_key"] = digest({"rule": RULE_VERSION, **item})
        item["conflicts"] = []
    cycles = list(groups.values())
    for item in cycles:
        for other in cycles:
            if item is other or item["patient_id"] != other["patient_id"]:
                continue
            same_day = item["content"]["anchor"] and item["content"]["anchor"] == other["content"]["anchor"]
            same_ordinal = item["content"]["ordinal"] and item["regimen_id"] == other["regimen_id"] and item["content"]["ordinal"] == other["content"]["ordinal"]
            if same_day or same_ordinal:
                item["conflicts"].append({"cycle_id": other["id"], "reason": "same_day_or_ordinal_requires_review"})
    return cycles


def _lab_periodicity(context, regimens, cycles):
    # Numerical context is auxiliary. It cannot create a treatment event or anchor.
    groups = defaultdict(list)
    for point in context:
        if not point.get("eligible", False) or not point.get("group_key") or point.get("comparator"):
            continue
        try:
            day, value = date.fromisoformat(point["day"]), Decimal(str(point["value"]))
        except (ValueError, InvalidOperation, KeyError):
            continue
        if value.is_finite():
            groups[(point.get("patient_id"), point["group_key"])].append((day, value, point))
    for regimen in regimens:
        regimen["lab_periodicity"] = []
        # Multiple D1/D8/D15 administrations can be one source-supported anchor.
        days = sorted({row["content"]["anchor"] for row in cycles if row["regimen_id"] == regimen["id"]
                       and row["content"]["anchor_precision"] == "DAY"})
        regimen["cadence"] = _cadence(days)
        cadence = regimen["cadence"]
        if not cadence or not cadence["similar_intervals"]:
            continue
        boundary = regimen["context_end"]
        if boundary and boundary["date_precision"] != "DAY":
            continue
        for (patient, key), points in groups.items():
            if patient != regimen["patient_id"]:
                continue
            counts = defaultdict(int)
            for day, _value, _proof in points:
                counts[day] += 1
            # The final observed cycle can have measurements after its anchor;
            # an explicit switch/pause limits context, never an invented end day.
            unique = sorted((day, value, proof) for day, value, proof in points if counts[day] == 1
                            and min(days) <= day.isoformat() and (not boundary or day.isoformat() < boundary["date"]))
            triples = [(previous, current, following) for previous, current, following in zip(unique, unique[1:], unique[2:])
                       if previous[0] < current[0] < following[0] and current[1] < previous[1] and current[1] < following[1]]
            minima = [current[0].isoformat() for _previous, current, _following in triples]
            auxiliary = _cadence(minima)
            if auxiliary and auxiliary["similar_intervals"] and abs(auxiliary["median_days"] - cadence["median_days"]) <= cadence["tolerance_days"]:
                regimen["lab_periodicity"].append({"group_key": key, "minima_days": minima, "cadence": auxiliary,
                                                    "sources": [deepcopy(row[2]) for row in unique
                                                                if any(row in triple for triple in triples)],
                                                    "basis": "检验变化呈相近间隔，仅作为已有锚点的辅助线索。"})


def _apply_event_decisions(events, decisions):
    overrides = {row["source_key"]: row for row in decisions if row["origin"] == "AUTOMATIC" and row["source_valid"]}
    result = []
    for event in events:
        row = overrides.get(event["source_key"])
        if row is None:
            result.append(event)
            continue
        result.append({**event, "id": digest({"model": row["id"], "state": row}), "model_id": row["id"],
                       "content": deepcopy(row["content"]), "origin": row["origin"]})
    for row in decisions:
        if row["origin"] == "USER" and row["source_valid"]:
            key = digest({"model": row["id"], "state": row})
            result.append({"id": key, "source_key": row["source_key"], "model_id": row["id"], "patient_id": row["patient_id"],
                           "content": deepcopy(row["content"]), "origin": "USER", "sources": deepcopy(row["sources"]),
                           "rule_version": RULE_VERSION})
    return sorted(result, key=lambda e: (e["patient_id"], e["content"]["date"] or "9999", e["id"]))


def propose_cycles(extraction, comparable_lab_context=(), *, event_decisions=()):
    events = _events(extraction)
    events = _apply_event_decisions(events, event_decisions)
    regimens = _regimens(events)
    cycles = _cycles(events)
    _lab_periodicity(comparable_lab_context, regimens, cycles)
    result = {"rule_version": RULE_VERSION, "events": events, "regimens": regimens, "cycles": cycles,
              "labels": deepcopy(extraction["labels"]), "excluded": deepcopy(extraction["excluded"]),
              "input_source_count": extraction["input_source_count"]}
    result["fingerprint"] = digest(result)
    return result
