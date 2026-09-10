import json
from uuid import uuid4

import pytest

from apps.exports.errors import ExportUnavailable
from apps.exports.services import create_preview, get_preview
from apps.patients.sharing import create_share
from tests.documents.test_detail_viewer import _patient
from tests.patients.test_family_shares import exchange
from .test_controlled_open import confirmed, payload
from .test_source_services import FIRST_URL, SECOND_URL, _decide
from .test_selected_output import source_selection

pytestmark=pytest.mark.django_db


def job_for(client, patient, source):
    assert client.get('/visit/').status_code==200
    return create_preview(patient, client.session.session_key, source_selection(source), actor=patient.account)


@pytest.mark.parametrize('mutation',['missing','null_source','null_evidence','null_document','wrong_source','revision','token','correction'])
def test_source_only_job_true_binding_and_revision_invalidation(django_user_model, mutation):
    client,patient,_,source=confirmed(django_user_model)
    job=job_for(client,patient,source)
    assert job.cloud_sources.get().source_id==source.pk
    if mutation=='missing':job.cloud_sources.all().delete()
    elif mutation=='null_source':job.cloud_sources.update(source=None)
    elif mutation=='null_evidence':job.cloud_sources.update(evidence=None)
    elif mutation=='null_document':job.cloud_sources.update(document=None)
    elif mutation=='wrong_source':job.cloud_sources.update(source_identity=uuid4())
    elif mutation=='revision':job.cloud_sources.update(revision_number=999)
    elif mutation=='token':job.cloud_sources.update(source_token='f'*64)
    else:_decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
    with pytest.raises(ExportUnavailable):get_preview(patient,client.session.session_key,job.pk,actor=patient.account)
    job.refresh_from_db()
    assert job.status=='INVALIDATED' and job.snapshot=={} and not job.cloud_sources.exists()


def test_source_only_share_real_exchange_safe_html_and_exact_open(django_user_model):
    _,patient,document,source=confirmed(django_user_model)
    reader,own=_patient(django_user_model,'cloud-output-reader')
    created=create_share(patient,patient.account,source_selection(source,sections=[]))
    share=created.share
    assert not share.source_bindings.exists() and share.cloud_sources.get().source_id==source.pk
    assert FIRST_URL not in json.dumps(share.snapshot)
    assert 'history' not in json.dumps(share.snapshot)
    share_id=exchange(reader,created.token)
    detail=reader.get(f'/shared/{share_id}/')
    assert detail.status_code==200 and FIRST_URL not in detail.content.decode()
    notice=f'/shared/{share_id}/cloud-imaging/{source.pk}/visit/'
    opened=f'/shared/{share_id}/cloud-imaging/{source.pk}/open/'
    assert reader.get(notice).status_code==200
    assert reader.get(f'/shared/{share_id}/documents/{document.pk}/').status_code==404
    assert reader.get(f'/cloud-imaging/{source.pk}/').status_code in (403,404)
    data=payload(patient,source);data.pop('patient_id')
    response=reader.post(opened,data)
    assert response.status_code==303 and response['Location']==FIRST_URL
    assert response['Referrer-Policy']=='no-referrer'
    _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
    assert reader.post(opened,data).status_code==410
    share.refresh_from_db()
    assert share.snapshot=={} and not share.cloud_sources.exists()


def test_owner_without_share_grant_cannot_open_even_with_correct_source_token(django_user_model):
    owner,patient,_,source=confirmed(django_user_model)
    created=create_share(patient,patient.account,source_selection(source,sections=[]))
    data=payload(patient,source);data.pop('patient_id')
    response=owner.post(f'/shared/{created.share.pk}/cloud-imaging/{source.pk}/open/',data)
    assert response.status_code==404 and 'Location' not in response


@pytest.mark.parametrize('change', ['missing_binding','null_evidence','null_document','wrong_revision','wrong_token','scope_removed','scope_added','expiry','revoke','creator','grant','session','unselected'])
def test_shared_cloud_never_bypasses_any_grant_or_binding_boundary(django_user_model, change):
    from datetime import timedelta
    from django.utils import timezone
    from apps.patients.sharing import revoke_share
    _,patient,_,source=confirmed(django_user_model)
    reader,own=_patient(django_user_model,'cloud-share-boundary-'+change)
    created=create_share(patient,patient.account,source_selection(source,sections=[]))
    share=created.share;identity=exchange(reader,created.token)
    path=f'/shared/{identity}/cloud-imaging/{source.pk}/open/'
    data=payload(patient,source);data.pop('patient_id')
    assert reader.post(path,data).status_code==303
    if change=='missing_binding':share.cloud_sources.all().delete()
    elif change=='null_evidence':share.cloud_sources.update(evidence=None)
    elif change=='null_document':share.cloud_sources.update(document=None)
    elif change=='wrong_revision':share.cloud_sources.update(revision_number=999)
    elif change=='wrong_token':share.cloud_sources.update(source_token='0'*64)
    elif change in {'scope_removed','scope_added'}:
        scope={**share.scope,'cloud_source_ids':[] if change=='scope_removed' else [str(source.pk),str(uuid4())]}
        type(share).objects.filter(pk=share.pk).update(scope=scope)
    elif change=='expiry':type(share).objects.filter(pk=share.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
    elif change=='revoke':revoke_share(patient,patient.account,share.pk)
    elif change=='creator':type(share).objects.filter(pk=share.pk).update(creator_revision=999)
    elif change=='grant':share.viewer_grants.all().delete()
    elif change=='session':reader.logout();reader.force_login(own.account)
    else:path=f'/shared/{identity}/cloud-imaging/{uuid4()}/open/'
    response=reader.post(path,data)
    assert response.status_code in (404,410) and 'Location' not in response
    assert b'SYNTHETIC_FIRST' not in response.content
    if change in {'scope_removed','scope_added'}:
        share.refresh_from_db();assert share.snapshot=={} and not share.cloud_sources.exists()


def test_shared_open_real_csrf_origin_and_invalid_form_never_reflect_target(django_user_model):
    from django.test import Client
    _,patient,_,source=confirmed(django_user_model)
    reader,_=_patient(django_user_model,'cloud-share-csrf')
    created=create_share(patient,patient.account,source_selection(source,sections=[]))
    identity=exchange(reader,created.token)
    secure=Client(enforce_csrf_checks=True);secure.cookies=reader.cookies
    path=f'/shared/{identity}/cloud-imaging/{source.pk}/open/'
    data=payload(patient,source);data.pop('patient_id')
    assert secure.get(path).status_code==405
    assert secure.post(path,data).status_code==403
    assert secure.get(f'/shared/{identity}/cloud-imaging/{source.pk}/visit/',secure=True).status_code==200
    data['csrfmiddlewaretoken']=secure.cookies['csrftoken'].value
    rejected=secure.post(path,data,secure=True,HTTP_ORIGIN='https://evil.example.invalid')
    assert rejected.status_code==403 and 'Location' not in rejected
    invalid=secure.post(path,{**data,'expected_source':FIRST_URL},secure=True,HTTP_ORIGIN='https://testserver')
    assert invalid.status_code==400 and b'SYNTHETIC_FIRST' not in invalid.content
    valid=secure.post(path,data,secure=True,HTTP_ORIGIN='https://testserver',HTTP_X_CLOUD_OPEN='navigate')
    assert valid.status_code==200 and valid.content==b'' and valid['Location']==FIRST_URL
