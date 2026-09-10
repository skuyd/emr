"""Invalid report associations never expose an unrelated private identity."""
from copy import deepcopy
import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.cancer_ordering.models import CancerCandidate
from apps.cancer_ordering.services import collect_current
from apps.facts.models import Fact, ClinicalReport
from tests.cancer_ordering.test_typed_pathology_sources import typed_fixture

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('scope', ['other_patient', 'other_document', 'other_report', 'missing_report'])
def test_invalid_report_scope_cannot_display_identity_or_broken_link(django_user_model, scope):
    client, patient, _, _, field, _ = typed_fixture(django_user_model, name='typed-report-owner-' + scope)
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(source_fact=field)
    foreign_id = None
    if scope in {'other_patient', 'other_document'}:
        _, _, document, _, foreign, _ = typed_fixture(django_user_model, name='typed-report-other-' + scope)
        if scope == 'other_document':
            type(document).objects.filter(pk=document.pk).update(patient=patient)
        foreign_id = foreign.clinical_report_id
        ClinicalReport.objects.filter(pk=foreign_id).update(title='SYNTHETIC-UNRELATED-REPORT')
    elif scope == 'other_report':
        report = field.clinical_report
        material = {f.attname: deepcopy(getattr(report, f.attname)) for f in report._meta.fields
                    if f.name not in {'id', 'created_at'}}
        material.update(ordinal=report.ordinal + 20, title='SYNTHETIC-UNRELATED-REPORT')
        other = ClinicalReport.objects.create(id=uuid.uuid4(), **material)
        foreign_id = other.pk
    else:
        # The database rejects a NULL FIELD report FK. A stale/nonexistent
        # declared context is the persisted invalid-association HTTP boundary.
        content = deepcopy(field.automatic_content)
        content['entity_context']['report_id'] = str(uuid.uuid4())
        Fact.objects.filter(pk=field.pk).update(automatic_content=content)
        foreign_id = field.clinical_report_id
    Fact.objects.filter(pk=field.pk).update(clinical_report_id=foreign_id)
    client.raise_request_exception = False
    response = client.get(f'/cancer-ordering/candidates/{candidate.pk}/', {'patient': patient.pk})
    assert response.status_code == 200
    body = response.content.decode()
    assert 'SYNTHETIC-UNRELATED-REPORT' not in body
    if foreign_id:
        assert f'/facts/reports/{foreign_id}/' not in body
    assert '/facts/reports/None/' not in body and '/facts/reports//' not in body
    assert not response.context['row']['source_valid']
    assert '报告关联待核对' in body


def test_database_rejects_a_field_without_its_report(django_user_model):
    _, _, _, _, field, _ = typed_fixture(django_user_model, name='typed-null-report-constraint')
    with pytest.raises(IntegrityError), transaction.atomic():
        Fact.objects.filter(pk=field.pk).update(clinical_report_id=None)
