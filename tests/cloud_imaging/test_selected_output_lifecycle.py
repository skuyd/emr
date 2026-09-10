import io

from django.core.exceptions import PermissionDenied
import pytest

from apps.core.streams import GuardedStream
from apps.exports import services
from apps.exports.errors import ExportUnavailable
from tests.documents.fakes import InMemoryObjectStore
from .test_controlled_open import confirmed
from .test_selected_output_bindings import job_for
from .test_source_services import SECOND_URL, _decide

pytestmark=pytest.mark.django_db


def queued_job(client, patient, source):
    job=job_for(client,patient,source)
    services.request_generation(patient,client.session.session_key,job.pk,{'format':'json'},dispatch=lambda _:None,actor=patient.account)
    return job


@pytest.mark.parametrize('phase',['queued','build','promote'])
def test_source_only_generation_change_never_publishes_old_artifact(django_user_model,monkeypatch,phase):
    client,patient,_,source=confirmed(django_user_model)
    job=queued_job(client,patient,source)
    store=InMemoryObjectStore()
    if phase=='queued':
        _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
    elif phase=='build':
        real=services.build_artifact
        def changed(*args,**kwargs):
            artifact=real(*args,**kwargs)
            assert artifact.byte_size>0
            _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
            return artifact
        monkeypatch.setattr(services,'build_artifact',changed)
    else:
        real=store.promote_immutable
        def changed(*args,**kwargs):
            result=real(*args,**kwargs)
            assert store.objects[result.key]
            _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
            return result
        monkeypatch.setattr(store,'promote_immutable',changed)
    services.generate_export(job.pk,store)
    job.refresh_from_db()
    assert job.status=='INVALIDATED' and job.snapshot=={} and job.cleanup_pending
    assert not job.cloud_sources.exists()
    assert services.cleanup_export(job.pk,store)
    assert store.objects=={}


def test_source_only_download_storage_read_rechecks_source_before_return(django_user_model,monkeypatch):
    client,patient,_,source=confirmed(django_user_model)
    job=queued_job(client,patient,source);store=InMemoryObjectStore()
    services.generate_export(job.pk,store)
    job.refresh_from_db()
    assert job.status=='READY' and job.byte_size>0
    real=store.open_private
    def changed(*args,**kwargs):
        stream=real(*args,**kwargs)
        _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
        return stream
    monkeypatch.setattr(store,'open_private',changed)
    with pytest.raises(ExportUnavailable):
        services.download_export(patient,client.session.session_key,job.pk,store,actor=patient.account)
    job.refresh_from_db();assert job.status=='INVALIDATED' and job.snapshot=={}


@pytest.mark.parametrize('inside_read',[False,True])
def test_selected_stream_fences_committed_buffer_before_release(django_user_model,inside_read):
    client,patient,_,source=confirmed(django_user_model)
    job=queued_job(client,patient,source);store=InMemoryObjectStore()
    services.generate_export(job.pk,store)
    artifact=services.download_export(patient,client.session.session_key,job.pk,store,actor=patient.account)
    payload=artifact.stream.read();artifact.close();assert len(payload)>32
    class Stream(io.BytesIO):
        changed=False
        def read(self,size=-1):
            result=super().read(size)
            if inside_read and self.tell()>8 and not self.changed:
                self.changed=True
                _decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
            return result
    raw=Stream(payload)
    stream=GuardedStream(raw,lambda:services.validate_export_stream(job.pk,patient.account,client.session.session_key))
    assert stream.read(8)==payload[:8]
    if not inside_read:_decide(patient,source,'CORRECT',changes={'url':SECOND_URL})
    assert stream.read(8)==b'' and stream.denied
    assert raw.closed


def test_source_only_trash_invalidates_and_cleanup_failure_can_retry(django_user_model):
    from apps.documents.lifecycle import move_to_trash, restore_document
    client,patient,document,source=confirmed(django_user_model)
    job=queued_job(client,patient,source);store=InMemoryObjectStore()
    services.generate_export(job.pk,store)
    job.refresh_from_db();key=job.object_key;assert store.objects[key]
    move_to_trash(patient,document.pk,actor=patient.account)
    job.refresh_from_db();assert job.status=='INVALIDATED' and job.snapshot=={}
    store.fail_delete=True
    assert not services.cleanup_export(job.pk,store)
    job.refresh_from_db();assert job.cleanup_pending and key in store.objects
    store.fail_delete=False
    assert services.cleanup_export(job.pk,store)
    assert key not in store.objects
    restore_document(patient,document.pk,actor=patient.account)
    with pytest.raises(ExportUnavailable):services.get_preview(patient,client.session.session_key,job.pk,actor=patient.account)


def test_source_only_hard_delete_discovers_export_object_and_retries_before_deleting_parent(django_user_model):
    from apps.documents.deletion import DeletionOutcome, request_document_deletion, purge_document_deletion
    from apps.documents.models import Document
    from apps.patients.sharing import create_share
    from .test_selected_output import source_selection
    client,patient,document,source=confirmed(django_user_model)
    job=queued_job(client,patient,source);store=InMemoryObjectStore()
    services.generate_export(job.pk,store)
    job.refresh_from_db();key=job.object_key;assert store.objects[key]
    share=create_share(patient,patient.account,source_selection(source, sections=[])).share
    assert not share.source_bindings.exists()
    deletion=request_document_deletion(patient,document.pk,dispatch=lambda _:None,actor=patient.account)
    share.refresh_from_db();assert share.snapshot=={} and not share.cloud_sources.exists()
    store.fail_delete=True
    assert purge_document_deletion(deletion.pk,store).outcome==DeletionOutcome.RETRY_SCHEDULED
    assert Document.objects.filter(pk=document.pk).exists() and key in store.objects
    store.fail_delete=False
    assert purge_document_deletion(deletion.pk,store).outcome==DeletionOutcome.PURGED
    assert not Document.objects.filter(pk=document.pk).exists() and key not in store.objects
    job.refresh_from_db();assert job.snapshot=={} and not job.cleanup_pending and not job.cloud_sources.exists()


@pytest.mark.parametrize('change',['report_exclude','reassign','parse','exclude_undo'])
def test_source_only_existing_export_and_share_follow_actual_report_and_parse_lifecycle(django_user_model,change):
    from django.test import Client
    from apps.cloud_imaging.scan_services import run_scan
    from apps.patients.sharing import create_share
    from tests.documents.test_detail_viewer import _patient
    from tests.patients.test_family_shares import exchange
    from .test_error_form_freshness import exclude_report
    from .test_report_ownership import two_reports, FIRST
    from .test_scans import queued
    from .test_selected_output import source_selection
    patient,document,_,store,_,reports=two_reports(django_user_model)
    run_scan(queued(patient,document).pk,store)
    source=_decide(patient,document.cloud_imaging_sources.get(evidence__payload=FIRST),'CONFIRM')
    client=Client();client.force_login(patient.account)
    job=job_for(client,patient,source)
    reader,_=_patient(django_user_model,'cloud-report-lifecycle-'+change)
    created=create_share(patient,patient.account,source_selection(source,sections=[]))
    share=created.share;share_id=exchange(reader,created.token)
    path=f'/shared/{share_id}/cloud-imaging/{source.pk}/visit/'
    assert reader.get(path).status_code==200
    if change=='report_exclude':exclude_report(patient,reports[0])
    elif change=='reassign':_decide(patient,source,'REASSIGN',changes={'report_id':str(reports[1].pk)})
    elif change=='parse':
        from tests.facts.factories import parsed_facts
        parsed_facts(patient,['new synthetic published parse'],document=document)
    else:
        source=_decide(patient,source,'EXCLUDE')
        _decide(patient,source,'UNDO')
    with pytest.raises(ExportUnavailable):services.get_preview(patient,client.session.session_key,job.pk,actor=patient.account)
    assert reader.get(path).status_code==410
    job.refresh_from_db();share.refresh_from_db()
    assert job.snapshot==share.snapshot=={} and not job.cloud_sources.exists() and not share.cloud_sources.exists()
