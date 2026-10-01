"""Retired treatment data cannot be selected or returned through exports/shares."""

import json

import pytest
from django.urls import Resolver404, resolve

from apps.exports.content import assert_snapshot_current, build_snapshot
from apps.exports.errors import ExportInputError, SnapshotChanged
from apps.exports.formats import read_structured_data, structured_data
from apps.exports.retired import ARRAY_KEYS, SELECTION_KEYS
from apps.patients.sharing_content import normalize_scope, project_snapshot
from tests.exports.test_jobs import _preview


pytestmark = pytest.mark.django_db


def test_treatment_routes_are_removed():
    with pytest.raises(Resolver404):
        resolve('/treatments/')


def test_retained_export_and_share_omit_retired_tables(django_user_model):
    _, patient, document, _, job = _preview(django_user_model, 'retired-treatments-output')
    assert not set(ARRAY_KEYS).intersection(job.snapshot)
    portable = structured_data(job.snapshot)
    assert 'treatment_events' not in read_structured_data(json.dumps(portable))
    scope = normalize_scope({'document_ids': [str(document.pk)], 'sections': ['diagnosis']})
    shared = project_snapshot(job.snapshot, scope)
    assert shared['facts']
    assert 'treatment_events' not in shared


@pytest.mark.parametrize('key', SELECTION_KEYS)
def test_removed_selection_is_rejected_without_broadening_scope(django_user_model, key):
    _, patient, document, _, _ = _preview(django_user_model, 'retired-treatments-selection')
    selection = {'mode': 'documents', 'document_ids': [str(document.pk)], 'sections': ['diagnosis'],
                 key: ['00000000-0000-0000-0000-000000000001']}
    with pytest.raises(ExportInputError, match='已移除'):
        build_snapshot(patient, selection)
    with pytest.raises(ExportInputError, match='已移除'):
        normalize_scope(selection)


def test_legacy_snapshot_and_portable_treatment_content_is_unavailable(django_user_model):
    _, patient, document, _, job = _preview(django_user_model, 'retired-treatments-snapshot')
    portable = structured_data(job.snapshot)
    job.snapshot['treatment_events'] = [{'id': 'retired-event', 'content': {'title': 'retired'}}]
    with pytest.raises(SnapshotChanged, match='已移除'):
        assert_snapshot_current(patient, job.snapshot)
    scope = normalize_scope({'document_ids': [str(document.pk)], 'sections': ['diagnosis']})
    with pytest.raises(SnapshotChanged, match='已移除'):
        project_snapshot(job.snapshot, scope)
    portable['treatment_events'] = job.snapshot['treatment_events']
    with pytest.raises(ExportInputError, match='已移除'):
        read_structured_data(json.dumps(portable))


def test_legacy_empty_retired_tables_preserve_other_exports(django_user_model):
    from apps.facts.readmodels import digest

    _, patient, _, _, job = _preview(django_user_model, 'retired-empty-output')
    job.snapshot.update({key: [] for key in ARRAY_KEYS})
    job.snapshot['selection'].update({key: [] for key in SELECTION_KEYS})
    job.snapshot['glucose_fingerprint'] = digest([])
    job.snapshot['treatment_fingerprint'] = None
    job.snapshot['treatment_binding_ids'] = {}
    assert_snapshot_current(patient, job.snapshot)
    portable = structured_data(job.snapshot)
    portable.update({key: [] for key in ARRAY_KEYS})
    restored = read_structured_data(json.dumps(portable))
    assert restored['facts'] == portable['facts']
    assert not set(ARRAY_KEYS).intersection(restored)


def test_frozen_retired_export_is_hidden_and_existing_file_is_cleaned(django_user_model):
    from apps.exports.errors import ExportUnavailable
    from apps.exports.services import cleanup_export, download_export
    from apps.facts.readmodels import digest
    from tests.documents.fakes import InMemoryObjectStore
    from tests.exports.test_jobs import _ready

    client, patient, _, _, job = _preview(django_user_model, 'retired-ready-output')
    store = InMemoryObjectStore()
    _ready(patient, client, job, store)
    job.snapshot['treatment_events'] = [{'content': {'title': 'retired'}}]
    job.snapshot_digest = digest(job.snapshot)
    job.save(update_fields=['snapshot', 'snapshot_digest'])
    with pytest.raises(ExportUnavailable):
        download_export(patient, client.session.session_key, job.pk, store)
    job.refresh_from_db()
    assert job.snapshot == {} and job.status == 'INVALIDATED'
    assert cleanup_export(job.pk, store)
    assert not store.objects


def test_removed_portable_scope_cannot_hide_behind_empty_snapshot_selection():
    from apps.exports.retired import check_snapshot

    with pytest.raises(SnapshotChanged, match='已移除'):
        check_snapshot({'selection': {}, 'scope': {'cycle_ids': ['retired-cycle']}})
