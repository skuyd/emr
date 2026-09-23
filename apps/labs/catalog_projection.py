"""Apply catalog standards without replacing the effective report transcription."""

from dataclasses import dataclass
import re

from .catalog import load_catalog
from .comparison_policy import AbnormalResult, cell_review_required
from .validation import REFERENCE_BLOCKING_ISSUES


def _historical_panel(observation):
    from apps.processing.models import OcrBlock
    from .layout import PANELS
    from .report_identity import contains_source_location

    report = getattr(observation, 'report_identity', None)
    if report is None or not report.source_region:
        return ''
    panels = []
    for block in OcrBlock.objects.filter(parsing_version_id=observation.parsing_version_id,
                                         document_page_id=observation.document_page_id):
        text = re.sub(r'\s+', '', block.text)
        panel = next((value for label, value in PANELS.items()
                      if text in {label, label + '报告', label + '检验报告'}), '')
        if panel and block.polygon and contains_source_location(report.source_region, block.polygon):
            if block.confidence < .95:
                return ''
            panels.append(panel)
    return panels[0] if len(set(panels)) == 1 else ''


@dataclass(frozen=True)
class CatalogProjection:
    indicator: object
    value: object
    reference: object
    phase: str

    def comparison(self, issues):
        unavailable = {'label': '', 'status': 'unavailable'}
        if self.reference is None or not self.value.reliable:
            return unavailable
        # Catalog identity, units and references replace only those three bases.
        # OCR confidence, conflicting fields, report/source status and edits stay.
        blocking = REFERENCE_BLOCKING_ISSUES - {'mapping_unknown', 'unit_unknown', 'reference_unknown', 'reference_conflict'}
        if {item['code'] for item in issues} & blocking:
            return unavailable
        status = self.reference.compare(self.value.display_value)
        return {'label': {'above': '高于', 'below': '低于', 'within': '范围内',
                          'different': '与参考不一致'}.get(status, ''), 'status': status}

    def abnormal(self, issues):
        if cell_review_required(issues) or not self.value.reliable:
            return AbnormalResult('review', '待核对', source='结果依据需要核对')
        result = self.comparison(issues)
        status = result['status']
        return AbnormalResult(status, {'above': '偏高', 'below': '偏低', 'different': '与参考不一致'}.get(status, ''),
                              {'above': '↑', 'below': '↓'}.get(status, ''),
                              '标准参考范围' if result['label'] else '')


def project_catalog(observation):
    # Dictionary release evaluation intentionally supplies isolated observations
    # without patient records; its frozen transcription contract stays separate.
    patient = getattr(observation.parsing_version.document, 'patient', None)
    if patient is None:
        return None
    catalog = load_catalog()
    panel = observation.field_evidence.get('panel', {}).get('value', '')
    entry = catalog.match(observation.raw_name, specimen=observation.specimen, panel=panel)
    if entry is None and not panel and len(catalog.candidates(observation.raw_name)) > 1:
        panel = _historical_panel(observation)
        if panel:
            entry = catalog.match(observation.raw_name, specimen=observation.specimen, panel=panel)
    if getattr(observation, 'value_sources', {}).get('standard_code', {}).get('revision_id'):
        selected = [item for item in catalog.indicators if item.code == observation.standard_code]
        entry = (selected[0] if len(selected) == 1 else
                 catalog.match(selected[0].name, specimen=observation.specimen, panel=panel) if selected else None)
    if entry is None:
        return None
    report = getattr(observation, 'report_identity', None)
    sampled_on = report.sampled_at.date() if report and report.status == 'ACCEPTED' and report.sampled_at else None
    phase = getattr(observation, 'physiological_phase', '')
    reference = entry.reference_for(sex=patient.sex, birth_date=patient.birth_date,
                                    sampled_on=sampled_on, phase=phase)
    return CatalogProjection(entry, entry.standardize(observation.raw_value, observation.raw_unit), reference, phase)


def catalog_issues(projection, issues):
    if projection is None:
        return issues
    resolved = {'mapping_unknown'}
    if projection.value.reliable:
        resolved.add('unit_unknown')
    return tuple(item for item in issues if item['code'] not in resolved)
