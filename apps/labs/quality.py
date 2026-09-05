"""Conservative admission thresholds shared by extraction and read paths."""

from decimal import Decimal

from django.db.models import Q


MIN_OBSERVATION_CONFIDENCE = Decimal("0.80")
MIN_STANDARD_NAME_CONFIDENCE = Decimal("0.90")
MIN_TREND_CONFIDENCE = Decimal("0.95")
QUALITY_POLICY_VERSION = "ocr-source-confidence-v1"


def unreliable_selected_date_q():
    from apps.processing.models import MetadataKind

    return Q(kind=MetadataKind.DOCUMENT_DATE, selected=True) & (
        Q(confidence__lt=MIN_OBSERVATION_CONFIDENCE)
        | Q(evidence__confidence__lt=MIN_OBSERVATION_CONFIDENCE)
        | Q(evidence__confidence__isnull=True)
    )
