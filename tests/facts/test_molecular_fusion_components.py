import pytest

from apps.facts.clinical_readmodels import effective_field
from tests.facts.molecular_factories import add, graph, variant_source
from tests.facts.pathology_factories import review
from tests.facts.test_molecular_contracts import component, variant

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('key,raw', [('gene', 'SYNA'), ('transcript', 'NM_B.3'), ('location', 'intron 2')])
@pytest.mark.parametrize('wrong_component', [False, True])
def test_fusion_components_require_membership_in_actual_ordered_partners(django_user_model, key, raw, wrong_component):
    _, patient, _, report, fields = graph(django_user_model)
    parents = {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']}
    identity = add(patient, report, 'variant.identity', 'variant:fusion', variant('FUSION'), parents)
    text = 'SYN_UNKNOWN_COMPONENT' if wrong_component else raw
    fact = add(patient, report, 'variant.' + key, 'variant:fusion', component(text), {**parents, 'VARIANT': identity},
        raw='标本甲；检测甲；' + variant_source('FUSION') + '；' + text)
    for item in [fields['specimen'], fields['assay'], identity, fact]:
        review(patient, item)
    result = effective_field(fact)
    assert result['status'] == 'CONFIRMED'
    assert result['usable'] is not wrong_component
