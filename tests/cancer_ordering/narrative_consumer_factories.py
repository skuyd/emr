"""Synthetic original dependencies shared by ordinary and committed consumers."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace

from django.core.cache import cache

from apps.accounts.deletion import purge_account_deletion, request_account_deletion
from apps.cancer_ordering.models import CancerCandidate, CollectionRun, DisplaySelection, NarrativeSource
from apps.cancer_ordering.readmodels import candidate_rows
from apps.cancer_ordering.services import collect_current, revise_candidate
from apps.exports import services
from apps.facts.models import Fact
from apps.facts.revisions import revise_fact
from apps.patients.access import Capability, authorize_patient, change_membership
from apps.patients.models import PatientMembership
from apps.patients.sharing import create_share
from tests.cancer_ordering.test_export_selection import selected_body
from tests.cancer_ordering.test_views import _decision, _url
from tests.documents.test_detail_viewer import _patient
from tests.facts.factories import parsed_facts
from tests.patients.test_family_access import family
from tests.patients.test_family_shares import exchange


CONTEXT = '现病史：患者诊断为肺癌，已行化疗。'
CORRECTION = '现病史：患者诊断为肺癌，已行化疗，门诊随访。'


def row(case):
    return next(item for item in candidate_rows(case.patient) if item['id'] == str(case.candidate.pk))


def consumer_case(model, marker, *, history=False):
    cache.clear()
    owner, patient, client, actor, membership = family(model, marker, role='ADMIN')
    document, version = parsed_facts(patient, [CONTEXT])
    parent = Fact.objects.get(parsing_version=version)
    assert parent.category == 'TREATMENT' and parent.representation == 'EXCERPT'
    reviewer = model.objects.create(phone_hash=hashlib.sha256((marker + '-reviewer').encode()).hexdigest(),
                                    phone_encrypted='synthetic')
    PatientMembership.objects.create(patient=patient, account=reviewer, role='EDITOR')
    historical = None
    if history:
        historical = model.objects.create(phone_hash=hashlib.sha256((marker + '-history').encode()).hexdigest(),
                                          phone_encrypted='synthetic')
        PatientMembership.objects.create(patient=patient, account=historical, role='EDITOR')
        revise_fact(patient, parent.pk, actor=historical, action='DEFER', expected_revision=0)
        revise_fact(patient, parent.pk, actor=reviewer, action='CONFIRM', expected_revision=1, checked_original=True)
        assert len({patient.account_id, actor.pk, reviewer.pk, historical.pk}) == 4
    collect_current(patient, actor=patient.account)
    candidate = CancerCandidate.objects.get(patient=patient)
    assert candidate.source_narrative_id and candidate.source_fact_id is None and candidate.source_report_id is None
    assert candidate.source_narrative.dependencies.filter(fact=parent, original_fact_id=parent.pk).exists()
    case = SimpleNamespace(owner=owner, patient=patient, client=client, actor=actor, membership=membership,
        document=document, version=version, parent=parent, candidate=candidate, reviewer=reviewer, historical=historical)
    if history:
        confirm_candidate(case)
    return case


def confirm_candidate(case):
    current = row(case)
    return revise_candidate(case.patient, case.candidate.pk, actor=case.reviewer, action='CONFIRM',
        expected_revision=current['revision_number'], expected_source=current['current_source_token'], checked_original=True)


def selected(case):
    return selected_body(case.patient, cancer_candidate_ids=[str(case.candidate.pk)])


def form_request(case, page, method):
    from apps.cancer_ordering import views as candidate_views
    from apps.exports import views as export_views
    from apps.patients import share_views
    if page == 'candidate':
        path, module = _url(row(case)), candidate_views
        data = _decision(case.patient, row(case), checked_original='')
    else:
        path = '/visit/' if page == 'prepare' else f'/patients/{case.patient.pk}/shares/'
        module = export_views if page == 'prepare' else share_views
        # The family actor can access multiple patients: mutation scope must be explicit.
        data = {'patient_id': str(case.patient.pk)}
    operation = (lambda: case.client.get(path)) if method == 'get' else (lambda: case.client.post(path, data))
    return module, operation


def preview(case, *, selection=None):
    job = services.create_preview(case.patient, case.client.session.session_key, selection or selected(case), actor=case.actor)
    assert job.snapshot['documents'] == []
    excluded = {item['id'] for key in ('excluded_documents', 'uncertain_documents') for item in job.snapshot[key]}
    assert str(case.document.pk) in excluded
    # Excluded metadata is directly bound even though no original was selected.
    assert job.source_bindings.filter(document=case.document).exists()
    assert job.snapshot['cancer_candidates'][0]['source'] == {
        'state': 'OMITTED', 'reason': 'SOURCE_CONTENT_NOT_SELECTED'}
    return job


def share(case, model, marker):
    created = create_share(case.patient, case.actor, selected(case))
    assert created.share.snapshot['documents'] == [] and not created.share.source_bindings.exists()
    reader, _ = _patient(model, marker)
    share_id = exchange(reader, created.token)
    return created.share, reader, f'/shared/{share_id}/'


def stored_candidates(case):
    return (list(CancerCandidate.objects.filter(patient=case.patient).order_by('pk').values()),
            list(DisplaySelection.objects.filter(patient=case.patient).values()))


def change(case, kind, *, job=None):
    original = stored_candidates(case)
    if kind == 'parent':
        case.parent.refresh_from_db()
        revise_fact(case.patient, case.parent.pk, actor=case.reviewer, action='CORRECT',
                    expected_revision=case.parent.revision_number, checked_original=True, changes={'text': CORRECTION})
    elif kind == 'new_input':
        document, _ = parsed_facts(case.patient, ['主诉：胰腺癌治疗后不适。'], document_type='UNKNOWN')
        assert not Fact.objects.filter(document=document).exists()
        assert not NarrativeSource.objects.filter(document=document).exists()
        assert not CollectionRun.objects.filter(document=document).exists()
        if job is not None:
            assert not job.source_bindings.filter(document=document).exists()
    elif kind == 'history':
        assert case.historical is not None
        identity = case.historical.pk
        deletion = request_account_deletion(identity, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
        assert purge_account_deletion(deletion.pk).outcome == 'PURGED'
        assert not type(case.historical).objects.filter(pk=identity).exists()
        assert case.parent.revisions.order_by('sequence').first().author_id is None
        assert case.parent.revisions.order_by('-sequence').first().author_id == case.reviewer.pk
        assert case.candidate.revisions.order_by('-sequence').first().author_id == case.reviewer.pk
        authorize_patient(case.patient, case.actor, Capability.EXPORT)
        assert case.patient.account.is_active and case.patient.deleted_at is None
    elif kind == 'membership':
        case.membership.refresh_from_db()
        change_membership(case.patient, case.patient.account, case.membership.pk,
                          revoke=True, expected_revision=case.membership.revision)
    elif kind != 'none':
        raise AssertionError(kind)
    assert stored_candidates(case) == original
    if job is not None and kind in {'parent', 'new_input', 'history'}:
        job.refresh_from_db()
        # These EXCERPT/history/new-input changes have not eagerly hidden the job.
        assert job.status in {'PREVIEW', 'GENERATING', 'READY'} and job.snapshot


def assert_hidden(job):
    job.refresh_from_db()
    assert job.status == 'INVALIDATED' and job.snapshot == {} and job.options == {} and job.cleanup_pending


def ready(case, store, *, kind='json'):
    job = preview(case)
    options = {'format': kind, 'parts': ['json', 'csv']} if kind == 'zip' else {'format': kind}
    services.request_generation(case.patient, case.client.session.session_key, job.pk, options,
                                actor=case.actor, dispatch=lambda _: None)
    services.generate_export(job.pk, store)
    job.refresh_from_db()
    assert job.status == 'READY' and job.byte_size > 0 and job.object_key
    attempt = job.attempts.get()
    assert attempt.object_key == job.object_key
    payload = store.objects[job.object_key]
    assert len(payload) == job.byte_size and hashlib.sha256(payload).hexdigest() == job.sha256
    return job, deepcopy({'object_key': job.object_key, 'byte_size': job.byte_size, 'sha256': job.sha256})


def cleanup_ready(job, store, original):
    assert original['byte_size'] > 0 and original['object_key'] in store.objects
    assert services.cleanup_export(job.pk, store)
    job.refresh_from_db()
    assert not job.cleanup_pending and job.byte_size == 0
    assert job.object_key == job.filename == job.content_type == job.sha256 == ''
    assert not job.attempts.filter(cleaned_at__isnull=True).exists()
    assert original['object_key'] not in store.objects
    assert services.cleanup_export(job.pk, store) and original['object_key'] not in store.objects
