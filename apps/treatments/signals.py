"""Deterministic literal signals; Unicode offsets always address input text."""

from copy import deepcopy
from datetime import date
import hashlib
import json
import re
import unicodedata


RULE_VERSION = "treatment-proposals-2"
_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2})\s*日?)?\s*月?|年)(?(3)(?=\D|$|\d{2}[:：]\d{2})|(?!\d|[./-]\s*\d))")
_NUMBER = r"[0-9零〇一二两三四五六七八九十百千]+"
_LABEL = re.compile(
    r"(?<![A-Za-z0-9])C\s*(?P<c>[0-9]{1,4})(?P<c_range>\s*(?:至|到|[-~～])\s*C?\s*[0-9]{1,4})?(?:\s*D\s*(?P<d>[0-9]{1,4}))?"
    + r"|第\s*(?P<cn>" + _NUMBER + r")\s*(?P<range>(?:至|到|[-~～])\s*" + _NUMBER + r")?\s*(?:周\s*期|疗\s*程)"
    + r"(?:\s*(?:第\s*(?P<dn>" + _NUMBER + r")\s*天|D\s*(?P<dx>[0-9]{1,4})))?"
    + r"|(?<![A-Za-z0-9])D\s*(?P<standalone_day>[0-9]{1,4})", re.I)
_PLAN = re.compile(r"计划|拟(?:于|行|予|给予|定|用)|建议|推荐|可能获益|临床试验|待(?:结果|评估|完善)|可考虑|考虑使用")
_NEGATED = re.compile(r"未(?:行|予|接受|进行|实施|用)|没有(?:接受|进行)|否认(?:治疗|化疗)|取消")
_EXECUTION = re.compile(r"予以|给予|接受|采用|改为|使用|施行|实施|完成|曾|再次|行|治疗后|化疗后")
_THERAPY = re.compile(r"化疗|放疗|放射治疗|细胞.{0,12}治疗|靶向|免疫治疗|维持治疗|手术|切除|消融|介入|支架|引流")
_BREAK = re.compile(r"[。；;]|\n[ \t]*\n")
_CONCURRENT = re.compile(r"[，,]\s*(?=同时|并行|并予|联合给予)")
_CLAUSE_PREFIX = re.compile(r"但(?:是)?|然而|随后|此后|最终|实际(?:于)?|已(?:经)?(?:于)?")
_ACTUAL = re.compile(r"实际|已(?:经)?")
_ACTUAL_ACTION = re.compile(r"^(?:予以|给予|接受|采用|使用|施行|实施|完成|行(?=方案|化疗|放疗|手术|切除|消融|介入|支架|引流))")
_ENUMERATION_END = re.compile(r"(?:、|及|和|与)\s*$")
_GROUP_TAIL = re.compile(
    r"(?:^|[，,])\s*(?P<scope>(?P<count>" + _NUMBER + r")次|以上|上述|这些)?"
    r"\s*(?:均|都|全部|皆)\s*(?:为|已(?:经)?)?\s*"
    r"(?:取消|计划|未(?:行|予|接受|进行|实施|用))\s*[。；;]?\s*$")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def normalized_regimen(value):
    # Only representational normalization. No dose deletion or drug equivalence.
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold()
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _number(value):
    if value is None:
        return None
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    total, current = 0, 0
    for character in value:
        if character in digits:
            current = digits[character]
        elif character in "十百千":
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[character]
            current = 0
        else:
            return None
    return total + current


def dates(text):
    output = []
    for match in _DATE.finditer(text):
        year, month, day = match.group(1, 2, 3)
        try:
            date(int(year), int(month or 1), int(day or 1))
        except ValueError:
            continue
        output.append({"value": year + (f"-{int(month):02d}" if month else "") + (f"-{int(day):02d}" if day else ""),
                       "precision": "DAY" if day else "MONTH" if month else "YEAR", "raw": match.group(),
                       "start": match.start(), "end": match.end()})
    return output


def _segments(text):
    start = 0
    for match in _BREAK.finditer(text):
        if text[start:match.end()].strip():
            yield start, match.end()
        start = match.end()
    if text[start:].strip():
        yield start, len(text)


def _date_clauses(text, begin, end):
    found = dates(text[begin:end])
    starts = [begin]
    for left, right in zip(found, found[1:]):
        separator = text[begin + left["end"]:begin + right["start"]]
        if not re.fullmatch(r"[\s、,，及和与至到~～-]*", separator):
            # A prefix immediately before the next date belongs to that next
            # assertion, e.g. "already treated, planned for <date> ...".
            boundary = begin + right["start"]
            modifiers = sorted([match for pattern in (_PLAN, _NEGATED, _CLAUSE_PREFIX)
                                for match in pattern.finditer(separator)], key=lambda match: match.start())
            for modifier in modifiers:
                tail = separator[modifier.start():]
                if (len(tail) <= 40 and not _THERAPY.search(tail) and "方案" not in tail
                        and not re.search(r"[，,、。；;]", tail)):
                    boundary = begin + left["end"] + modifier.start()
                    break
            starts.append(boundary)
    for index, start in enumerate(starts):
        yield start, starts[index + 1] if index + 1 < len(starts) else end


def _kind(text):
    compact = re.sub(r"\s", "", text)
    for pattern, kind in [
        (r"入院(?:日期|时间|于|：|:)", "ADMISSION"), (r"出院(?:日期|时间|于|：|:)", "DISCHARGE"),
        (r"暂停.{0,12}(?:治疗|化疗|方案)|(?:治疗|化疗)暂停", "PAUSE"),
        (r"延迟.{0,12}(?:治疗|化疗)|延期.{0,12}(?:治疗|化疗)", "DELAY"),
        (r"停止.{0,12}(?:治疗|化疗|方案)|结束.{0,12}(?:治疗|化疗)", "STOP"),
        (r"细胞.{0,12}治疗", "CELL_THERAPY"), (r"放疗|放射治疗", "RADIOTHERAPY"),
        (r"手术|切除术", "SURGERY"), (r"消融|介入|支架|引流", "LOCAL_PROCEDURE"),
        (r"疗效评估|治疗评估|复查评估", "ASSESSMENT"),
        (r"化疗|靶向治疗|免疫治疗|维持治疗|药物治疗|方案", "SYSTEMIC_TREATMENT"),
    ]:
        if re.search(pattern, compact):
            return kind
    return None


def _occurrence(text, kind):
    if _NEGATED.search(text):
        return "NEGATED"
    if _PLAN.search(text):
        return "PLANNED"
    if kind in {"ADMISSION", "DISCHARGE"} or _EXECUTION.search(text):
        return "OCCURRED"
    return "UNKNOWN"


def _nonoccurrence(text, offset):
    for pattern, state in ((_NEGATED, "NEGATED"), (_PLAN, "PLANNED")):
        if match := pattern.search(text):
            return {"state": state, "start": offset + match.start(), "end": offset + match.end()}
    return None


def _actual_assertion(text):
    # Ordering words and nominal descriptions (e.g. actual body weight) do not
    # cancel a governing plan. Require an explicit performed-action assertion.
    for marker in _ACTUAL.finditer(text):
        tail = text[marker.end():].lstrip()
        if tail.startswith("于"):
            tail = tail[1:].lstrip()
        found = dates(tail)
        if found and found[0]["start"] == 0:
            tail = tail[found[0]["end"]:].lstrip()
        if _ACTUAL_ACTION.match(tail):
            return True
    return False


def _apply_group_tail(source, items, begin, end):
    """A terminal, explicit whole-list modifier retains its original proof."""
    match = _GROUP_TAIL.search(source["text"][begin:end])
    if not match or (match.group("count") and _number(match.group("count")) != len(items)):
        return items
    basis = _nonoccurrence(match.group(), begin + match.start())
    if not basis:
        return items
    output = []
    for item in items:
        content = item["content"]
        content["occurrence"] = basis["state"]
        if "governing_occurrence_same_sentence" not in content["limitations"]:
            content["limitations"].append("governing_occurrence_same_sentence")
        proof = item["sources"][0]
        updated = _signal(source, content, min(proof["start_offset"], begin + match.start()),
                          max(proof["end_offset"], end))
        updated["sources"][0]["occurrence_basis"] = {
            "occurrence": basis["state"], "start_offset": begin + match.start(), "end_offset": end,
            "raw_text": source["text"][begin + match.start():end], "association": "governing_same_sentence"}
        output.append(updated)
    return output


def _regimen(text, kind):
    if kind not in {"SYSTEMIC_TREATMENT", "CELL_THERAPY", "RADIOTHERAPY"}:
        return ""
    quoted = re.search(r'[“"「](.{1,1000}?)[”"」]', text, re.S)
    if quoted:
        return quoted.group(1).strip()
    text = _LABEL.sub("", text)
    verb = re.search(r"(?:予以|给予|接受|采用|改为|使用|施行|实施|完成|行)\s*(.+?)(?=(?:姑息|维持|辅助|新辅助)?化疗|治疗|[，,。；;]|$)", text, re.S)
    name = verb.group(1).strip() if verb else ""
    if name.endswith("方案"):
        name = name[:-2].strip()
    return name[:1000] if name not in {"", "该方案", "同方案", "上述方案", "下一周期"} else ""


def _proof(source, start, end):
    return {**deepcopy(source.get("source", {})), "source_id": source["id"],
            "source_kind": source.get("source_kind", "FACT"), "text_basis": source.get("text_basis", "ORIGINAL_FACT"),
            "source_token": source["source_token"], "source_revision": source.get("source_revision", 0),
            "lifecycle_revision": source.get("lifecycle_revision", 0), "material_revision": source.get("material_revision", 0),
            "start_offset": start, "end_offset": end, "raw_text": source["text"][start:end]}


def _signal(source, content, start, end):
    key = digest({"rule": RULE_VERSION, "patient": source["patient_id"], "source": source["id"],
                  "source_token": source["source_token"], "revision": source.get("source_revision"),
                  "lifecycle": source.get("lifecycle_revision"), "material": source.get("material_revision"),
                  "span": [start, end], "content": content})
    return {"id": key, "patient_id": source["patient_id"], "source_key": key,
            "content": content, "sources": [_proof(source, start, end)], "rule_version": RULE_VERSION}


def _content(text, kind, occurrence, *, day=None, ordinal=None, cycle_day=None, limitations=()):
    regimen = _regimen(text, kind)
    return {"kind": kind, "title": regimen or {"ADMISSION": "入院线索", "DISCHARGE": "出院线索"}.get(kind, text.strip()[:200]),
            "occurrence": occurrence, "date": day["value"] if day else None,
            "date_precision": day["precision"] if day else "UNKNOWN", "date_raw": day["raw"] if day else "",
            "regimen_text": regimen, "cycle_ordinal": ordinal, "cycle_day": cycle_day, "note": "",
            "status": "PENDING", "recorded_as": "SOURCE_REPORTED", "limitations": list(limitations)}


def _order(source):
    text = source["text"]
    content = _content(text, "MEDICATION_ORDER", "ORDER")
    content["title"] = "药疗医嘱记录"
    for label, key in [("开始时间", "order_start"), ("停止时间", "order_stop"), ("核对时间", "order_checked")]:
        match = re.search(label + r"\s*[:：]([^\n]+)", text)
        found = dates(match.group(1)) if match else []
        content[key] = {k: found[0][k] for k in ("value", "precision", "raw")} if len(found) == 1 else None
    status = re.search(r"执行状态\s*[:：]([^\n]+)", text)
    content["execution_status"] = status.group(1).strip() if status else ""
    content["limitations"] = ["order_is_not_administration", "antitumor_intent_not_established"]
    return _signal(source, content, 0, len(text))


def extract_treatment_signals(material):
    signals, labels, exclusions = [], [], []
    for source in material.get("sources", []):
        if not source.get("eligible", False) or source.get("status") in {"EXCLUDED", "DEFERRED"}:
            exclusions.append({"source_id": source["id"], "reason": "source_not_eligible"})
            continue
        if source.get("category") != "TREATMENT" and source.get("source_kind") != "ADMISSION_EVIDENCE":
            exclusions.append({"source_id": source["id"], "reason": "not_treatment_evidence"})
            continue
        text = source["text"]
        if source.get("medication_order") or ("开始时间" in text and "执行状态" in text):
            signals.append(_order(source))
            continue
        for begin, end in _segments(text):
            sentence_scope = None
            enumerated_scope = None
            sentence_signals = []
            for start, finish in _date_clauses(text, begin, end):
                clause = text[start:finish]
                clause_dates = dates(clause)
                split_points = [0, *[m.end() for m in _CONCURRENT.finditer(clause)], len(clause)]
                clause_scope = None
                for index, (part_start, part_end) in enumerate(zip(split_points, split_points[1:])):
                    part = clause[part_start:part_end]
                    kind = _kind(part)
                    local_dates = dates(part)
                    limitations = []
                    if index and not local_dates:
                        if len(clause_dates) == 1:
                            local_dates = clause_dates
                            limitations.append("single_date_same_sentence_association")
                        else:
                            limitations.append("concurrent_plural_date_unknown")
                    if len(local_dates) > 1 and any(re.search(r"至|到|~|～", part[a["end"]:b["start"]])
                                                    for a, b in zip(local_dates, local_dates[1:])):
                        local_dates = []
                        limitations.append("date_range_is_not_administration_list")
                    found_labels = []
                    for match in _LABEL.finditer(part):
                        ordinal = _number(match.group("c") or match.group("cn"))
                        cycle_day = _number(match.group("d") or match.group("dn") or match.group("dx") or match.group("standalone_day"))
                        valid = bool((ordinal is not None or cycle_day is not None)
                                     and (ordinal is None or 1 <= ordinal <= 9999)
                                     and (cycle_day is None or 1 <= cycle_day <= 9999)
                                     and not (match.group("range") or match.group("c_range")))
                        label = {"source_id": source["id"], "patient_id": source["patient_id"],
                                 "ordinal": ordinal if valid else None, "cycle_day": cycle_day if valid else None,
                                 "event_date": local_dates[0]["value"] if valid and len(local_dates) == 1 else None,
                                 "raw": match.group(), "start_offset": start + part_start + match.start(),
                                 "end_offset": start + part_start + match.end(),
                                 "reason": "" if valid else "invalid_or_ranged_label"}
                        labels.append(label)
                        found_labels.append(label)
                    if kind is None or (source.get("source_kind") == "ADMISSION_EVIDENCE" and kind not in {"ADMISSION", "DISCHARGE"}):
                        continue
                    local_scope = _nonoccurrence(part, start + part_start)
                    inherited_scope = clause_scope if index else sentence_scope or enumerated_scope
                    if local_scope:
                        occurrence, occurrence_basis = local_scope["state"], local_scope
                        # A modifier preceding the first date governs a dated
                        # enumeration; a modifier after a date remains local.
                        own_dates = dates(part)
                        if not index and (not own_dates or local_scope["start"] <= start + own_dates[0]["start"]):
                            sentence_scope = local_scope
                    elif _actual_assertion(part):
                        occurrence, occurrence_basis = _occurrence(part, kind), None
                        sentence_scope = None
                    elif inherited_scope:
                        occurrence, occurrence_basis = inherited_scope["state"], inherited_scope
                        limitations.append("governing_occurrence_same_sentence")
                    else:
                        occurrence, occurrence_basis = _occurrence(part, kind), None
                    clause_scope = occurrence_basis
                    valid_labels = [l for l in found_labels if not l["reason"]]
                    label = valid_labels[0] if len(valid_labels) == 1 and len(local_dates) <= 1 else None
                    if found_labels and label is None:
                        limitations.append("ordinal_date_association_unknown")
                    for day in local_dates or [None]:
                        content = _content(part, kind, occurrence, day=day,
                                           ordinal=label["ordinal"] if label else None,
                                           cycle_day=label["cycle_day"] if label else None, limitations=limitations)
                        # Concurrent dates are supported by the whole sentence, including the governing date.
                        evidence_start = start if index and "single_date_same_sentence_association" in limitations else start + part_start
                        if occurrence_basis:
                            evidence_start = min(evidence_start, occurrence_basis["start"])
                        signal = _signal(source, content, evidence_start, start + part_end)
                        if occurrence_basis:
                            a, b = occurrence_basis["start"], occurrence_basis["end"]
                            signal["sources"][0]["occurrence_basis"] = {
                                "occurrence": occurrence, "start_offset": a, "end_offset": b,
                                "raw_text": text[a:b], "association": "local_modifier" if local_scope else "governing_same_sentence"}
                        sentence_signals.append(signal)
                # A local modifier also governs an explicit enumeration, but
                # an ordinary comma alone does not establish that association.
                enumerated_scope = clause_scope if _ENUMERATION_END.search(clause) else None
            signals.extend(_apply_group_tail(source, sentence_signals, begin, end))
    return {"rule_version": RULE_VERSION, "signals": signals, "labels": labels, "excluded": exclusions,
            "input_source_count": len(material.get("sources", []))}
