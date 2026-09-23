"""Explicit per-report phase statements, independent of reference-range lists."""

import re
import unicodedata

from .catalog import PHASES


def explicit_phase(text):
    text = unicodedata.normalize('NFKC', text)
    match = re.search(r'(?:本次|月经|生理|患者)阶段\s*:\s*(.*)', text)
    if not match:
        return None
    value = match[1].strip()
    return value if value in PHASES else ''


def report_phase(report):
    evidence = tuple(report.fields.get('physiological_phase', ()))
    raw = '；'.join(dict.fromkeys(item['raw_text'] for item in evidence))
    phases = {item['value'] for item in evidence}
    phase = next(iter(phases)) if len(phases) == 1 and all(item['confidence'] >= .95 for item in evidence) else ''
    return phase, raw, evidence
