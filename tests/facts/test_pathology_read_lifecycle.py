import pytest

from apps.accounts.deletion import AccountDeletionOutcome, purge_account_deletion, request_account_deletion
from apps.patients.models import PatientMembership
from tests.documents.test_detail_viewer import _patient
from tests.facts.pathology_factories import add_field, confirm_graph, ihc_fixture, review


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("author_kind", ["creator", "current_revision", "intermediate_revision"])
@pytest.mark.parametrize("route", ["field", "report"])
def test_actual_collaborator_purge_during_pathology_read_removes_stale_author_body(django_user_model, monkeypatch, author_kind, route):
    from apps.facts import views

    client, patient, _, report, fields = ihc_fixture(django_user_model, "pathology-author-" + author_kind + route)
    _, collaborator = _patient(django_user_model, "pathology-author-source-" + author_kind + route)
    actor = collaborator.account
    PatientMembership.objects.create(patient=patient, account=actor, role="EDITOR")
    if author_kind == "creator":
        add_field(patient, report, "assay.method", "assay:a", {"code": "IHC", "raw": "合成方法原词"},
                  {"SPECIMEN": fields["specimen"], "ASSAY": fields["assay"]}, actor=actor, source_role="PRIMARY_ASSAY_METADATA")
    confirm_graph(patient, fields)
    if author_kind != "creator":
        review(patient, fields["tps"], actor=actor)
        if author_kind == "intermediate_revision":
            review(patient, fields["tps"], actor=patient.account)
    job = request_account_deletion(actor.pk, document_dispatch=lambda _: None, account_dispatch=lambda _: None)
    original = views.render

    def purge(*args, **kwargs):
        response = original(*args, **kwargs)
        assert purge_account_deletion(job.pk).outcome == AccountDeletionOutcome.PURGED
        return response

    monkeypatch.setattr(views, "render", purge)
    url = f"/facts/{fields['tps'].pk}/" if route == "field" else f"/facts/reports/{report.pk}/"
    response = client.get(url)
    assert response.status_code == 410
    assert "TPS 13" not in response.content.decode() and str(actor.pk) not in response.content.decode()
    fields["tps"].refresh_from_db()
    assert fields["tps"].automatic_content["value"]["values"] == ["13"]
    assert not django_user_model.objects.filter(pk=actor.pk).exists()


def test_replacement_form_cannot_return_old_descendant_value_after_edit_during_render(django_user_model, monkeypatch):
    from apps.facts import views

    client, patient, _, _, fields = ihc_fixture(django_user_model, "pathology-replace-inflight")
    confirm_graph(patient, fields)
    original = views.render

    def change(*args, **kwargs):
        response = original(*args, **kwargs)
        review(patient, fields["cps"], "DEFER")
        return response

    monkeypatch.setattr(views, "render", change)
    response = client.get(f"/facts/{fields['marker'].pk}/", {"edit_context": "1"})
    assert response.status_code == 410 and "CPS 21" not in response.content.decode()
