from copy import deepcopy

import pytest

from tests.facts.test_molecular_schema import SAMPLES, negative


@pytest.mark.parametrize("key,value", [*SAMPLES, ("assay.negative_statement", negative())])
def test_original_value_controls_roundtrip_each_registered_molecular_kind(key, value):
    from apps.facts.molecular_forms import MolecularValueForm
    original = deepcopy(value)
    form = MolecularValueForm(key, value=value, initial={"raw_value": "SYN original transcription"})
    assert all(field.widget.__class__.__name__ != "HiddenInput" for field in form.fields.values())
    data = {k: v for k, v in form.initial.items() if k in form.fields and v is not None}
    bound = MolecularValueForm(key, data, value=value)
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["value"] == original
    assert value == original


def test_optional_identity_component_unknown_is_not_treated_as_reviewed_absence():
    from apps.facts.molecular_forms import MolecularValueForm
    form = MolecularValueForm("variant.identity")
    data = {k: v for k, v in form.initial.items() if v is not None}
    data.update(raw_value="SYN1 c.1A>T", value_raw="SYN1 c.1A>T", kind="SMALL_VARIANT", status="COMPLETE", scope="SOMATIC",
                gene_state="PRINTED", gene_raw="SYN1", expression_state="PRINTED", expression_raw="c.1A>T")
    bound = MolecularValueForm("variant.identity", data)
    assert not bound.is_valid()
    data["status"] = "INCOMPLETE"
    bound = MolecularValueForm("variant.identity", data)
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data["value"]["transcripts"] == {"state": "UNKNOWN", "values": []}


@pytest.mark.parametrize('kind', ['variant', 'drug', 'negative'])
def test_list_controls_preserve_original_items_and_do_not_restore_actual_edits(kind):
    from apps.facts.molecular_forms import MolecularValueForm
    from tests.facts.test_molecular_contracts import variant
    items = [' SYN first\nprinted wrap ', ' SYN second ']
    if kind == 'variant':
        key, value, control = 'variant.identity', variant(), 'transcripts_raw'
        value['transcripts']['values'] = items
        selected = lambda v: v['transcripts']['values']
    elif kind == 'drug':
        key, control = 'drug_evidence.drugs', 'names'
        value = {'names': items, 'relation': 'OR', 'raw': ' SYN original OR statement '}
        selected = lambda v: v['names']
    else:
        key, value, control = 'assay.negative_statement', negative(), 'scope_targets'
        value['scope']['targets'] = items
        selected = lambda v: v['scope']['targets']
    form = MolecularValueForm(key, value=value, initial={'raw_value': ' SYN original '})
    data = {k: v.replace('\n', '\r\n') if isinstance(v, str) else v for k, v in form.initial.items()}
    bound = MolecularValueForm(key, data, value=value, initial={'raw_value': ' SYN original '})
    assert bound.is_valid(), bound.errors
    assert bound.cleaned_data['value'] == value
    assert bound.cleaned_data['raw_value'] == ' SYN original '
    data[control] = ' new first \r\n new second '
    changed = MolecularValueForm(key, data, value=value)
    assert changed.is_valid(), changed.errors
    assert selected(changed.cleaned_data['value']) == [' new first ', ' new second ']
