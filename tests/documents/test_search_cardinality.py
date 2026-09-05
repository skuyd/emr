from django.db.models import Count
import pytest

from apps.documents.archive import _apply_search, _base_queryset
from apps.labs.models import LabObservation
from apps.processing.models import OcrBlock
from tests.documents.test_records import _patient, _record


@pytest.mark.django_db
def test_search_does_not_multiply_ocr_and_observation_rows(django_user_model):
    _, patient = _patient(django_user_model, "s")
    document = _record(patient, "synthetic.pdf", ocr_text="needle",
                       observation=("needle", "needle", "4.2", "unit"))
    block = OcrBlock.objects.get(parsing_version__document=document)
    observation = LabObservation.objects.get(parsing_version__document=document)
    for position in range(2, 31):
        block.pk = None
        block.reading_order = position
        block.save(force_insert=True)
        observation.pk = None
        observation.reading_order = position
        observation.save(force_insert=True)

    rows = list(_apply_search(_base_queryset(patient, "needle"), "needle")
                .values("pk").annotate(matched_rows=Count("pk")))

    assert rows == [{"pk": document.pk, "matched_rows": 1}]
