from uuid import uuid4

import pytest

from apps.cloud_imaging.output_forms import choice_material
from apps.cloud_imaging.readmodels import document_snapshot
from apps.cloud_imaging.services import add_manual_source
from apps.exports.models import ExportJob
from tests.documents.test_detail_viewer import _document
from .test_controlled_open import confirmed
from .test_source_services import FIRST_URL, SECOND_URL, _decide

pytestmark=pytest.mark.django_db


def two_similar_sources(django_user_model, *, same_page=False):
    client,patient,document,source=confirmed(django_user_model)
    if same_page:
        other,pages=document,list(document.pages.all())
    else:
        other,pages=_document(patient,page_count=1)
        assert other.display_filename==document.display_filename
    original=document_snapshot(patient,actor=patient.account,document_id=other.pk)
    second=add_manual_source(patient,actor=patient.account,document_id=other.pk,page_id=pages[0].pk,
        url=SECOND_URL,title=source.title,expected_source=original['input_token'],operation_id=uuid4())
    second=_decide(patient,second,'CONFIRM')
    return client,patient,source,second


@pytest.mark.parametrize('consumer',['export','share'])
@pytest.mark.parametrize('same_page',[False,True])
def test_same_site_page_revision_and_names_have_distinct_safe_choices_and_internal_review(django_user_model,consumer,same_page):
    client,patient,first,second=two_similar_sources(django_user_model,same_page=same_page)
    choices=choice_material(patient,patient.account)
    assert len(choices)==2 and choices[0][1]!=choices[1][1]
    if same_page:
        assert '本页第 1 个来源' in choices[0][1]
        assert '本页第 2 个来源' in choices[1][1]
    for value,label in choices:
        assert value.split(':',1)[0][:8] in label
        assert 'SYNTHETIC_FIRST' not in label and 'SYNTHETIC_CORRECTION' not in label
        assert FIRST_URL not in label and SECOND_URL not in label
    path='/visit/' if consumer=='export' else f'/patients/{patient.pk}/shares/'
    page=client.get(path);assert page.status_code==200
    content=page.content.decode()
    for source in (first,second):
        review=f'/cloud-imaging/{source.pk}/?patient={patient.pk}'
        assert review in content and client.get(review).status_code==200
    assert FIRST_URL not in content and SECOND_URL not in content
    selected=next(value for value,_ in choices if value.startswith(str(second.pk)+':'))
    data={'cloud_source_ids':[selected],'sections':['cloud_imaging']}
    if consumer=='export':data.update(mode='documents',nickname=patient.display_name,action='preview')
    response=client.post(path,data)
    assert response.status_code==(302 if consumer=='export' else 201)
    output=ExportJob.objects.get(patient=patient) if consumer=='export' else patient.shares.get()
    assert output.snapshot['selection']['cloud_source_ids']==[str(second.pk)]
    assert output.cloud_sources.get().source_id==second.pk
    assert [row['id'] for row in output.snapshot['cloud_imaging_sources']]==[str(second.pk)]
    if consumer=='export':
        from apps.exports.formats import json_bytes
        payload=json_bytes(output.snapshot).decode()
        assert SECOND_URL in payload and FIRST_URL not in payload
    else:
        from urllib.parse import parse_qs, urlsplit
        from tests.documents.test_detail_viewer import _patient
        from tests.patients.test_family_shares import exchange
        from .test_controlled_open import payload
        reader,_=_patient(django_user_model,'same-page-selection-reader')
        token=parse_qs(urlsplit(response.context['share_link']).fragment)['token'][0]
        share_id=exchange(reader,token)
        data=payload(patient,second);data.pop('patient_id')
        opened=reader.post(f'/shared/{share_id}/cloud-imaging/{second.pk}/open/',data)
        assert opened.status_code==303 and opened['Location']==SECOND_URL
        assert reader.get(f'/shared/{share_id}/cloud-imaging/{first.pk}/visit/').status_code==404


def test_access_strings_in_choice_context_are_omitted(django_user_model):
    _,patient,source,second=two_similar_sources(django_user_model)
    source=_decide(patient,source,'CORRECT',changes={'title':FIRST_URL+' '+SECOND_URL})
    choices=choice_material(patient,patient.account)
    labels=' '.join(label for _,label in choices)
    assert FIRST_URL not in labels and SECOND_URL not in labels
    assert 'SYNTHETIC_FIRST' not in labels and 'SYNTHETIC_CORRECTION' not in labels
