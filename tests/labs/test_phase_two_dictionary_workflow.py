from datetime import date
import json

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
import pytest

from apps.labs.dictionary import current_dictionary, default_dictionary
from apps.labs.review import create_review_task, transition_review_task
from apps.operations.permissions import Role
from tests.documents.test_detail_viewer import _patient
from tests.labs.test_trends import _observation
from tests.operations.test_services import staff


pytestmark = pytest.mark.django_db


def _publish(actor, **kwargs):
    from apps.labs.dictionary_workflow import preview_dictionary, publish_dictionary
    if "expected_preview_hash" not in kwargs:
        kwargs["expected_preview_hash"] = preview_dictionary(actor, candidate_ids=kwargs["candidate_ids"], version=kwargs["version"])["preview_hash"]
    return publish_dictionary(actor, **kwargs)


@pytest.fixture
def candidate_case(django_user_model):
    from apps.labs.dictionary_workflow import collect_dictionary_candidates

    manager = staff(django_user_model, Role.DICTIONARY_MANAGER)
    manager.user_permissions.add(Permission.objects.get(codename="review_labobservation"))
    _client, patient = _patient(django_user_model, "p2-dictionary")
    document, row = _observation(patient, date(2026, 8, 20), "5.2", code="CANDIDATE_ABC", raw_name="合成新白细胞")
    candidate = collect_dictionary_candidates(row.parsing_version)[0]
    task = create_review_task(patient.account, row.pk, reviewer=manager)
    definition = next(item for item in json.loads(current_dictionary().source_path.read_text(encoding="utf-8"))["indicators"] if item["code"] == "LAB_WBC")
    definition["aliases"].append("合成新白细胞")
    return manager, patient, document, row, candidate, task, definition


@pytest.mark.parametrize("withdrawal", ["is_staff", "role", "is_active"])
@pytest.mark.parametrize("operation", ["review", "publish", "rollback"])
def test_dictionary_mutations_recheck_authority_after_lock(candidate_case, monkeypatch, withdrawal, operation):
    from apps.labs import dictionary_workflow as workflow
    from apps.operations.models import DictionaryRelease
    from tests.operations.test_services import _withdraw_operator_authority

    manager, _patient, _document, _row, candidate, _task, definition = candidate_case
    expected_hash = current_dictionary().content_hash
    preview = None
    release = None
    if operation != "review":
        workflow.review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition,
                                  rationale="synthetic source checked", expected_revision=0, totp_verified_at=timezone.now())
        preview = workflow.preview_dictionary(manager, candidate_ids=[candidate.pk], version="revoked-publication")
    if operation == "rollback":
        release = workflow.publish_dictionary(
            manager, version="revoked-publication", candidate_ids=[candidate.pk], expected_active_hash=expected_hash,
            expected_preview_hash=preview["preview_hash"], totp_verified_at=timezone.now(),
        )
        expected_hash = release.content_hash
    lock_name = "_lock_candidates" if operation == "review" else "_publication_lock"
    real_lock = getattr(workflow, lock_name)

    def revoke_after_lock(*args, **kwargs):
        locked = real_lock(*args, **kwargs)
        _withdraw_operator_authority(manager, withdrawal)
        return locked

    monkeypatch.setattr(workflow, lock_name, revoke_after_lock)
    with pytest.raises(PermissionDenied):
        if operation == "review":
            workflow.review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition,
                                      rationale="synthetic source checked", expected_revision=0, totp_verified_at=timezone.now())
        elif operation == "publish":
            workflow.publish_dictionary(
                manager, version="revoked-publication", candidate_ids=[candidate.pk], expected_active_hash=expected_hash,
                expected_preview_hash=preview["preview_hash"], totp_verified_at=timezone.now(),
            )
        else:
            workflow.rollback_dictionary(manager, release.previous_release_id, expected_active_hash=expected_hash,
                                         totp_verified_at=timezone.now())
    candidate.refresh_from_db()
    assert candidate.revision_number == (0 if operation == "review" else 1)
    assert candidate.events.count() == (0 if operation == "review" else 1)
    assert current_dictionary().content_hash == expected_hash
    if operation != "rollback":
        assert not DictionaryRelease.objects.exists()


@pytest.mark.parametrize("withdrawal", ["is_staff", "role", "is_active", "review_permission"])
def test_dictionary_publication_rechecks_authority_after_regression(candidate_case, monkeypatch, withdrawal):
    from apps.labs import dictionary_workflow as workflow
    from apps.operations.models import DictionaryRelease
    from tests.operations.test_services import _withdraw_operator_authority

    manager, _patient, _document, _row, candidate, _task, definition = candidate_case
    workflow.review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition,
                              rationale="synthetic source checked", expected_revision=0, totp_verified_at=timezone.now())
    preview = workflow.preview_dictionary(manager, candidate_ids=[candidate.pk], version="revoked-during-regression")
    real_regression = workflow._regression_report

    def revoke_after_regression(*args, **kwargs):
        report = real_regression(*args, **kwargs)
        if withdrawal == "review_permission":
            manager.user_permissions.clear()
        else:
            _withdraw_operator_authority(manager, withdrawal)
        return report

    monkeypatch.setattr(workflow, "_regression_report", revoke_after_regression)
    with pytest.raises(PermissionDenied):
        workflow.publish_dictionary(
            manager, version="revoked-during-regression", candidate_ids=[candidate.pk],
            expected_active_hash=preview["expected_active_hash"], expected_preview_hash=preview["preview_hash"],
            totp_verified_at=timezone.now(),
        )
    assert not DictionaryRelease.objects.exists()
    assert current_dictionary().content_hash == preview["expected_active_hash"]


def test_candidate_deduplication_is_patient_scoped_and_never_maps_automatically(candidate_case):
    from apps.labs.dictionary_workflow import collect_dictionary_candidates

    _manager, _patient, _document, row, candidate, _task, _definition = candidate_case
    again = collect_dictionary_candidates(row.parsing_version)
    assert again[0].pk == candidate.pk
    assert candidate.sources.count() == 1
    assert current_dictionary().match("合成新白细胞") is None


def test_candidate_source_access_requires_explicit_review_grant(candidate_case, django_user_model):
    from apps.labs.dictionary_workflow import get_dictionary_candidate

    manager, patient, _document, _row, candidate, task, _definition = candidate_case
    outsider = staff(django_user_model, Role.DICTIONARY_MANAGER)
    with pytest.raises(PermissionDenied):
        get_dictionary_candidate(outsider, candidate.pk)
    assert get_dictionary_candidate(manager, candidate.pk).pk == candidate.pk
    transition_review_task(patient.account, task.pk, action="REVOKE", expected_revision=0)
    with pytest.raises(PermissionDenied):
        get_dictionary_candidate(manager, candidate.pk)


def test_pending_candidate_cannot_publish_and_review_needs_explicit_reason(candidate_case):
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, publish_dictionary, review_candidate

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    with pytest.raises(DictionaryWorkflowError):
        _publish(manager, version="test-p2-1", candidate_ids=[candidate.pk],
                           expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    with pytest.raises(ValidationError):
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="",
                         expected_revision=0, totp_verified_at=timezone.now())


def test_dictionary_publication_has_diff_regression_immutable_snapshot_and_rollback(candidate_case):
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, publish_dictionary, review_candidate, rollback_dictionary

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    baseline = current_dictionary()
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="已对照本任务原件核实别名",
                     expected_revision=0, totp_verified_at=timezone.now())
    assert current_dictionary().match("合成新白细胞") is None
    release = _publish(manager, version="test-p2-1", candidate_ids=[candidate.pk],
                                 expected_active_hash=baseline.content_hash, totp_verified_at=timezone.now())
    assert current_dictionary().match("合成新白细胞").code == "LAB_WBC"
    assert release.regression_report["passed"] is True
    assert release.regression_report['fixed_synthetic']['counts']['target_positive']['passed'] == 125
    assert release.diff["changed"] == ["LAB_WBC"]
    assert release.payload["dictionary_version"] == "test-p2-1"
    assert release.previous_release_id is not None
    with pytest.raises(DictionaryWorkflowError):
        _publish(manager, version="test-p2-1", candidate_ids=[candidate.pk],
                           expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    rollback_dictionary(manager, release.previous_release_id, expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    assert current_dictionary().content_hash == baseline.content_hash
    assert current_dictionary().match("合成新白细胞") is None


def test_parser_regression_blocks_publication_even_when_aliases_are_unchanged(candidate_case, monkeypatch):
    from apps.labs import dictionary_workflow as regression
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, review_candidate
    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rationale='checked source',
                     expected_revision=0, totp_verified_at=timezone.now())
    monkeypatch.setattr(regression, 'evaluate_publication', lambda *args, **kwargs: {'passed': False, 'failures': ['fixed_value_regressed']})
    with pytest.raises(DictionaryWorkflowError, match='固定回归'):
        _publish(manager, version='test-block-parser', candidate_ids=[candidate.pk],
                 expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())


def test_publishing_cannot_remove_known_aliases_or_skip_recent_second_factor(candidate_case):
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, publish_dictionary, review_candidate

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    with pytest.raises(PermissionDenied):
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition,
                         rationale="source checked", expected_revision=0)
    definition["aliases"] = ["合成新白细胞"]
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="source checked",
                     expected_revision=0, totp_verified_at=timezone.now())
    with pytest.raises(DictionaryWorkflowError):
        _publish(manager, version="test-p2-bad", candidate_ids=[candidate.pk],
                           expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    assert current_dictionary().match("WBC").code == "LAB_WBC"


def test_dictionary_review_rejects_alias_collision_before_release(candidate_case):
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, review_candidate

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    definition["aliases"].append("HGB")
    with pytest.raises(DictionaryWorkflowError):
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="source checked",
                         expected_revision=0, totp_verified_at=timezone.now())


def test_deleting_last_candidate_source_removes_its_access_and_review_history(candidate_case):
    from apps.documents.deletion import request_document_deletion
    from apps.labs.dictionary_workflow import get_dictionary_candidate
    from apps.labs.models import DictionaryCandidate

    manager, patient, document, _row, candidate, _task, _definition = candidate_case
    request_document_deletion(patient, document.pk, dispatch=lambda _job: None)
    assert not DictionaryCandidate.objects.filter(pk=candidate.pk).exists()
    with pytest.raises(PermissionDenied):
        get_dictionary_candidate(manager, candidate.pk)


def test_explicit_preview_becomes_stale_when_candidate_changes(candidate_case):
    from apps.labs.dictionary_workflow import preview_dictionary, publish_dictionary, review_candidate
    from apps.labs.revisions import RevisionConflict

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="source checked",
                     expected_revision=0, totp_verified_at=timezone.now())
    preview = preview_dictionary(manager, candidate_ids=[candidate.pk], version="p2-preview")
    definition["aliases"].append("合成另一个别名")
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="source rechecked",
                     expected_revision=1, totp_verified_at=timezone.now())
    with pytest.raises(RevisionConflict):
        _publish(manager, version="p2-preview", candidate_ids=[candidate.pk],
                           expected_active_hash=preview["expected_active_hash"], expected_preview_hash=preview["preview_hash"],
                           totp_verified_at=timezone.now())
    assert current_dictionary().match("合成新白细胞") is None


def test_historical_dictionary_loads_immutable_release_even_after_rollback(candidate_case):
    from apps.labs.dictionary import dictionary_for_version
    from apps.labs.dictionary_workflow import publish_dictionary, review_candidate, rollback_dictionary

    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="source checked",
                     expected_revision=0, totp_verified_at=timezone.now())
    release = _publish(manager, version="p2-history", candidate_ids=[candidate.pk],
                                 expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    rollback_dictionary(manager, release.previous_release_id, expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    assert dictionary_for_version("p2-history").match("合成新白细胞").code == "LAB_WBC"
    assert current_dictionary().match("合成新白细胞") is None
    release.payload["dictionary_version"] = "overwritten"
    with pytest.raises(ValidationError):
        release.save()


def test_review_definition_must_resolve_the_actual_candidate(candidate_case):
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, review_candidate
    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    definition["aliases"].remove(candidate.raw_term)
    with pytest.raises(DictionaryWorkflowError):
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="checked",
                         expected_revision=0, totp_verified_at=timezone.now())


@pytest.mark.parametrize("broken", [
    {"source_unit": None}, {"target_unit": ""}, {"source_unit": "unreviewed/unit"},
    {"kind": "report_sum", "unit": "10^9/L", "absolute_tolerance": "0", "component_codes": None},
    {"kind": "report_sum", "unit": "10^9/L", "absolute_tolerance": "0", "component_codes": ["MISSING_CODE"]},
    {"kind": "history_ratio", "unit": "", "minimum_ratio": "10"},
    {"factor": "1e1000000"}, {"factor": "1e-1000000"},
    {"kind": "history_ratio", "unit": "10^9/L", "minimum_ratio": "1e1000000"},
    {"kind": "report_sum", "unit": "10^9/L", "absolute_tolerance": "1e1000000", "component_codes": ["LAB_RBC"]},
])
def test_rule_preconditions_are_typed_and_resolve_known_units_and_components(candidate_case, broken):
    from apps.labs.dictionary_workflow import review_candidate
    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    rule = dict(id="fixture-conversion", version="1", kind="conversion", code="LAB_WBC", specimen="BLOOD",
                method="synthetic-method", evidence="synthetic source-checked rule", source_unit="10^9/L", target_unit="10^9/L", factor="1")
    rule.update(broken)
    with pytest.raises(ValidationError):
        review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rules=[rule], rationale="checked",
                         expected_revision=0, totp_verified_at=timezone.now())


def test_publication_requires_reviewed_preview_and_rollback_requires_current_intent(candidate_case):
    from apps.labs.dictionary_workflow import publish_dictionary, review_candidate, rollback_dictionary
    from apps.labs.revisions import RevisionConflict
    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="checked",
                     expected_revision=0, totp_verified_at=timezone.now())
    original_hash = current_dictionary().content_hash
    with pytest.raises(RevisionConflict):
        publish_dictionary(manager, version="p2-required-preview", candidate_ids=[candidate.pk], expected_active_hash=original_hash,
                           expected_preview_hash=None, totp_verified_at=timezone.now())
    release = _publish(manager, version="p2-required-preview", candidate_ids=[candidate.pk], expected_active_hash=original_hash,
                       totp_verified_at=timezone.now())
    with pytest.raises(RevisionConflict):
        rollback_dictionary(manager, release.previous_release_id, expected_active_hash=original_hash, totp_verified_at=timezone.now())
    assert current_dictionary().version == release.version


def test_release_keeps_review_identity_and_protects_rules_and_audit_fields(candidate_case):
    from apps.documents.deletion import request_document_deletion
    from apps.labs.dictionary_workflow import review_candidate
    from apps.labs.dictionary import dictionary_for_release, DictionaryError
    from apps.operations.models import DictionaryRelease
    manager, patient, document, _row, candidate, _task, definition = candidate_case
    review_candidate(manager, candidate.pk, decision="ACCEPT", definition=definition, rationale="checked",
                     expected_revision=0, totp_verified_at=timezone.now())
    release = _publish(manager, version="p2-provenance", candidate_ids=[candidate.pk],
                       expected_active_hash=current_dictionary().content_hash, totp_verified_at=timezone.now())
    request_document_deletion(patient, document.pk, dispatch=lambda _job: None)
    assert release.candidate_reviews[0]["candidate_id"] == str(candidate.pk)
    assert release.candidate_reviews[0]["revision"] == 1
    for field, value in (("indicator_count", 1), ("published_by_id", None), ("published_at", timezone.now())):
        item = DictionaryRelease.objects.get(pk=release.pk)
        setattr(item, field, value)
        with pytest.raises(ValidationError):
            item.save()
    release.rules = [{"id": "tampered-rule"}]
    with pytest.raises(DictionaryError):
        dictionary_for_release(release)


def test_unit_candidate_cannot_be_approved_for_another_project(candidate_case):
    from apps.labs.dictionary_workflow import collect_dictionary_candidates, DictionaryWorkflowError, review_candidate
    manager, _patient, _doc, row, _candidate, _task, definition = candidate_case
    row.standard_code, row.raw_unit = 'LAB_HGB', 'cells/L'
    row.save()
    unit_candidate = next(item for item in collect_dictionary_candidates(row.parsing_version) if item.kind == 'UNIT')
    definition['unit_forms'].append('cells/L')
    with pytest.raises(DictionaryWorkflowError):
        review_candidate(manager, unit_candidate.pk, decision='ACCEPT', definition=definition, rationale='checked',
                         expected_revision=0, totp_verified_at=timezone.now())


def test_project_definition_context_must_match_authorized_source(candidate_case, monkeypatch):
    from apps.labs.dictionary import load_dictionary
    from apps.labs import dictionary_workflow as workflow
    manager, _patient, _doc, row, candidate, _task, _definition = candidate_case
    row.specimen = 'URINE'
    row.save()
    dictionary = load_dictionary('apps/labs/dictionaries/phase-two.json')
    definition = next(item for item in json.loads(dictionary.source_path.read_text(encoding='utf-8'))['indicators'] if item['code'] == 'LAB_WBC')
    definition['aliases'].append(candidate.raw_term)
    monkeypatch.setattr(workflow, 'current_dictionary', lambda: dictionary)
    with pytest.raises(workflow.DictionaryWorkflowError):
        workflow.review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rationale='checked',
                                  expected_revision=0, totp_verified_at=timezone.now())


def test_reviewed_rules_are_in_preview_identity_and_cannot_change_without_a_new_version(candidate_case):
    from copy import deepcopy
    from apps.labs.dictionary_workflow import DictionaryWorkflowError, preview_dictionary, review_candidate
    from apps.labs.dictionary import rules_for_version, rules_digest
    manager, _patient, _doc, _row, candidate, _task, definition = candidate_case
    definition['unit_forms'].append('cells/L')
    rule = dict(id='fixture-count-unit', version='1', kind='conversion', code='LAB_WBC', specimen='BLOOD',
                method='synthetic-method', evidence='source-checked synthetic acceptance rule',
                source_unit='10^9/L', target_unit='cells/L', factor='1e9')
    with pytest.raises(ValidationError):
        review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rules=[rule, rule],
                         rationale='checked', expected_revision=0, totp_verified_at=timezone.now())
    review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rules=[rule],
                     rationale='checked', expected_revision=0, totp_verified_at=timezone.now())
    preview = preview_dictionary(manager, candidate_ids=[candidate.pk], version='p2-rules-1')
    assert preview['diff']['rules']['added'] == ['fixture-count-unit']
    release = _publish(manager, version='p2-rules-1', candidate_ids=[candidate.pk],
                       expected_active_hash=preview['expected_active_hash'], expected_preview_hash=preview['preview_hash'],
                       totp_verified_at=timezone.now())
    assert rules_for_version(release.version)[0]['factor'] == '1e9'
    assert release.rules_hash == rules_digest(release.rules)
    changed = deepcopy(rule)
    changed['factor'] = '1e8'
    review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rules=[changed],
                     rationale='rechecked', expected_revision=1, totp_verified_at=timezone.now())
    with pytest.raises(DictionaryWorkflowError):
        preview_dictionary(manager, candidate_ids=[candidate.pk], version='p2-rules-2')


def test_publication_evaluates_rules_and_ignores_wall_time_in_preview_identity(candidate_case, monkeypatch):
    from apps.labs import dictionary_workflow as workflow
    from apps.labs.dictionary import rules_digest
    manager, _patient, _document, _row, candidate, _task, definition = candidate_case
    rule = dict(id='synthetic-rule-snapshot', version='1', kind='conversion', code='LAB_WBC', specimen='BLOOD',
                method='合成方法A', evidence='fixed synthetic source', source_unit='10^9/L', target_unit='10^9/L', factor='1')
    workflow.review_candidate(manager, candidate.pk, decision='ACCEPT', definition=definition, rules=[rule],
                              rationale='source-checked synthetic rule', expected_revision=0, totp_verified_at=timezone.now())
    first = workflow.preview_dictionary(manager, candidate_ids=[candidate.pk], version='snapshot-evaluated')
    second = workflow.preview_dictionary(manager, candidate_ids=[candidate.pk], version='snapshot-evaluated')
    assert first['regression_report']['fixed_synthetic']['identity']['rules_sha256'] == rules_digest(first['rules'])
    assert first['preview_hash'] == second['preview_hash']
    release = workflow.publish_dictionary(manager, version='snapshot-evaluated', candidate_ids=[candidate.pk],
        expected_active_hash=first['expected_active_hash'], expected_preview_hash=first['preview_hash'], totp_verified_at=timezone.now())
    assert release.evaluation_events.get().action == 'PUBLISH'
    frozen = release.regression_report
    workflow.rollback_dictionary(manager, release.pk, expected_active_hash=release.content_hash, totp_verified_at=timezone.now())
    release.refresh_from_db()
    assert release.evaluation_events.count() == 2
    assert release.regression_report == frozen
    event = release.evaluation_events.get(action='ROLLBACK')
    event.report = {}
    with pytest.raises(ValueError):
        event.save()
