import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_real_0003_to_0004_preserves_legacy_rows_and_confirmations(django_user_model):
    from apps.facts import models
    from apps.facts.clinical_readmodels import effective_field, report_source_token
    from apps.facts.clinical_services import add_manual_clinical_field
    from tests.facts import pathology_factories as pf
    from tests.facts import test_pathology_pipeline as pipeline
    from tests.facts import test_clinical_foundation as imaging

    old_target = [('facts', '0003_pathology_context_anchors')]
    new_target = [('facts', '0004_molecular_context_anchors')]
    MigrationExecutor(connection).migrate(old_target)
    try:
        _, patient, _, report, fields = pf.ihc_fixture(django_user_model, 'permanent-old-ihc')
        fields['date'] = pf.add_field(patient, report, 'assay.report_date', 'assay:a',
                                     {'value': '2026-09', 'precision': 'MONTH'},
                                     {'SPECIMEN': fields['specimen'], 'ASSAY': fields['assay']})
        for key in ['specimen', 'assay', 'clone', 'date', 'marker', 'tps', 'cps']:
            pf.review(patient, fields[key])
        _, old_patient, old_document, _, old_run = imaging.clinical_fixture(django_user_model, name='permanent-old-imaging')
        old_report = old_document.clinical_reports.get()
        old_date = old_report.fields.get(field_key='report.exam_date')
        old_comparison = add_manual_clinical_field(old_patient, actor=old_patient.account, report_id=old_report.pk,
            entity_key='comparison:old', field_key='comparison.statement', value={'text': 'synthetic comparison'},
            fragments=[{'page_number': 1, 'raw_text': 'synthetic comparison'}],
            expected_report_source=report_source_token(old_report))
        pf.review(old_patient, old_date)
        pf.review(old_patient, old_comparison)
        _, _, _, _, completed = pipeline.fixture(django_user_model, name='permanent-old-completed')
        assert completed.status == old_run.status == 'EXTRACTED'
        assert old_date.schema_version == '1.0' and old_comparison.schema_version == '1.1'
        watched = [*fields.values(), old_date, old_comparison]
        before_tokens = {str(f.pk): effective_field(f)['current_source_token'] for f in watched}
        assert all(effective_field(f)['usable'] for f in watched)

        tables = [models.Fact, models.FactRevision, models.FactSourceFragment, models.ClinicalReport,
                  models.ClinicalReportRevision, models.ClinicalReportSpan, models.ClinicalExtraction]
        def rows():
            return json.dumps({m._meta.label: list(m.objects.order_by('pk').values()) for m in tables},
                              ensure_ascii=True, sort_keys=True, default=str)
        before = rows()
        MigrationExecutor(connection).migrate(new_target)
        assert rows() == before
        for f in watched:
            f = models.Fact.objects.get(pk=f.pk)
            assert effective_field(f)['usable']
            assert effective_field(f)['current_source_token'] == before_tokens[str(f.pk)]
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
