"""Current reviewed laboratory identities for organization and derived data."""
from dataclasses import replace
from copy import deepcopy

from apps.labs.change_metrics import changes_for_cells
from apps.labs.comparison import comparable_cell
from apps.labs.models import LabObservation
from apps.labs.readmodels import effective_rows
from apps.labs.revisions import _snapshot

from .signals import digest


def laboratory_date(row):
    # Preserve the same source precision used by the existing export contract.
    if row.date_verified:
        return {"value": row.observation_date.isoformat() if row.observation_date else None,
                "precision": "DAY" if row.observation_date else "UNKNOWN", "raw": row.observation_date.isoformat() if row.observation_date else ""}
    source_id = row.value_sources.get("observation_date", {}).get("observation_id")
    original = LabObservation.objects.select_related("parsing_version").filter(
        pk=source_id, parsing_version__document_id=row.parsing_version.document_id,
    ).first() if source_id and source_id != str(row.pk) else row
    original = original or row
    candidates = original.parsing_version.metadata_candidates.filter(kind="DOCUMENT_DATE", selected=True)
    page = original.field_evidence.get("observation_date", {}).get("page_number")
    if page:
        candidates = candidates.filter(evidence__document_page__page_number=page)
    candidates = list(candidates.order_by("pk"))
    precision = candidates[0].precision if candidates and len({item.precision for item in candidates}) == 1 else "UNKNOWN"
    value = row.observation_date.isoformat() if row.observation_date else None
    if value and precision in {"MONTH", "YEAR"}:
        value = value[:7] if precision == "MONTH" else value[:4]
    return {"value": value, "precision": precision if value else "UNKNOWN", "raw": "\n".join(item.raw_text for item in candidates)}


def laboratory_source(row, *, document_identity=None):
    from .records import _document_identity
    document = row.parsing_version.document
    day = laboratory_date(row)
    token = digest({"document": document_identity or _document_identity(document), "observation": _snapshot(row),
                    "source": [row.evidence.source_text, row.evidence.polygon, row.evidence.confidence],
                    "field_evidence": row.field_evidence, "date": day,
                    "revision": str(row.applied_revision.pk) if row.applied_revision else None})
    return {"source_token": token, "date": day["value"], "date_precision": day["precision"], "date_raw": day["raw"],
            "source_valid": bool(document.deleted_at is None and row.parsing_version.active
                and row.document_page.document_id == document.pk and not row.reported_error and not row.revision_conflict)}


def _change(row, change):
    result = {"id": str(row.pk), "observation_id": str(row.pk), "document_id": str(row.parsing_version.document_id),
              "previous_observation_id": str(change.previous.observation.pk) if change.previous else None,
              "baseline_observation_ids": [str(cell.observation.pk) for cell in change.baseline],
              "elapsed_days": change.elapsed_days, "previous_reason": change.previous_reason,
              "baseline_reason": change.baseline_reason, "threshold_percent": change.threshold_percent, "highlight": change.highlight}
    for name in ["absolute_change", "daily_change", "previous_percentage", "baseline_mean", "baseline_percentage"]:
        value = getattr(change, name)
        result[name] = str(value) if value is not None else None
    return result


def trusted_laboratory(patient, *, include_changes=True):
    """Caller holds a patient access guard; qualification uses all effective rows."""
    from .records import _document_identity
    originals = effective_rows(patient, include_uncertain=True)
    identities, records, cells = {}, [], []
    for row in originals:
        document = row.parsing_version.document
        if document.pk not in identities:
            identities[document.pk] = _document_identity(document)
        source = laboratory_source(row, document_identity=identities[document.pk])
        cell = comparable_cell(row, previous=originals)
        if not source["source_valid"] or source["date_precision"] != "DAY":
            cell = replace(cell, trend_eligible=False)
        cells.append(cell)
        records.append({"id": str(row.pk), "kind": "observation", "document_id": str(document.pk), **source,
            "label": row.standard_name or row.raw_name, "url": row.source_url, "standard_code": row.standard_code,
            "numeric_value": str(cell.numeric_value) if cell.numeric_value is not None else None,
            "raw_value": row.raw_value, "unit": cell.unit, "group_key": tuple(cell.group_key), "trend_eligible": cell.trend_eligible,
            "comparability_label": cell.comparability_label, "quality_issues": deepcopy(list(cell.quality_issues)),
            "rule": deepcopy(cell.rule), "revision_number": row.revision_number,
            "revision_id": str(row.applied_revision.pk) if row.applied_revision else None,
            "parsing_version": str(row.parsing_version_id), "page": row.document_page.page_number,
            "evidence_id": str(row.evidence_id), "field_sources": deepcopy(row.value_sources)})
    changes = changes_for_cells(cells) if include_changes else {}
    return {"records": records, "personal_changes": [_change(row, changes[str(row.pk)]) for row in originals] if include_changes else [],
            "fingerprint": digest(records)}


def periodicity_context(patient, laboratory):
    return [{"id": row["id"], "patient_id": str(patient.pk), "day": row["date"], "value": row["numeric_value"],
             "group_key": digest(row["group_key"]), "eligible": row["trend_eligible"], "source_token": row["source_token"]}
            for row in laboratory["records"]]
