"""A final read in one domain cannot leave another domain's rendered values stale."""
import pytest

from tests.exports.test_molecular_combined_domains import four_domains
from tests.exports.test_lesion_output_bindings import authenticated
from tests.facts.pathology_factories import review
from tests.cancer_ordering.test_services import _select


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("route", ["export", "share"])
@pytest.mark.parametrize("change", ["molecular_after_cancer", "cancer_after_molecular", "none"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_last_domain_read_cannot_return_other_domain_stale_controls(django_user_model, monkeypatch, route, change, method):
    from apps.exports import molecular, views as export_views
    from apps.patients import share_views
    from apps.cancer_ordering import output_forms

    patient, _, _, _, _, metric, _ = four_domains(django_user_model)
    client=authenticated(patient)
    module=export_views if route=="export" else share_views
    template="exports/prepare.html" if route=="export" else "patients/shares.html"
    render=module.render; cancer_read=output_forms.resolve_ordering; molecular_read=molecular.selection_unchanged
    observed={"rendered":False,"changed":False}
    def capture(*args,**kwargs):
        response=render(*args,**kwargs)
        if args[1]==template:
            assert "01.20" in response.content.decode() and "肺癌" in response.content.decode()
            observed["rendered"]=True
        return response
    def cancer(*args,**kwargs):
        result=cancer_read(*args,**kwargs)
        if observed["rendered"] and not observed["changed"] and change=="molecular_after_cancer":
            review(patient,metric,"EXCLUDE")
            observed["changed"]=True
        return result
    def molecular_guard(*args,**kwargs):
        result=molecular_read(*args,**kwargs)
        if observed["rendered"] and not observed["changed"] and change=="cancer_after_molecular":
            _select(patient,"MANUAL_PROFILE",profile="PANCREAS")
            observed["changed"]=True
        return result
    monkeypatch.setattr(module,"render",capture)
    monkeypatch.setattr(output_forms,"resolve_ordering",cancer)
    monkeypatch.setattr(molecular,"selection_unchanged",molecular_guard)
    response=getattr(client,method.lower())("/visit/" if route=="export" else f"/patients/{patient.pk}/shares/",{"patient":str(patient.pk),"patient_id":str(patient.pk)})
    assert observed["rendered"]
    if change=="none":
        assert response.status_code==(200 if method=="GET" else 400) and "01.20" in response.content.decode()
    else:
        assert observed["changed"]
        assert response.status_code in (409,410)
        assert "01.20" not in response.content.decode() and "肺癌" not in response.content.decode()
