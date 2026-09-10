"""Finite source categories do not derive clinical meaning from arbitrary words."""
import re

from django.core.exceptions import ValidationError

from .molecular_assertion_source import normalized, validate_original_assertion

TERMS = {
    'assay.msi_category': {'msi-h': 'MSI_H', 'msi-l': 'MSI_L', 'mss': 'MSS'},
    'assay.tmb_qualitative': {'高': 'HIGH', '中': 'INTERMEDIATE', '低': 'LOW', 'high': 'HIGH', 'intermediate': 'INTERMEDIATE', 'low': 'LOW'},
    'drug_evidence.direction': {'可能获益': 'REPORT_BENEFIT', '耐药': 'REPORT_RESISTANCE', '未获益': 'REPORT_NO_BENEFIT',
        '不确定': 'UNCERTAIN', '未说明': 'NOT_STATED', 'possible benefit': 'REPORT_BENEFIT', 'resistance': 'REPORT_RESISTANCE',
        'no benefit': 'REPORT_NO_BENEFIT', 'uncertain': 'UNCERTAIN', 'not stated': 'NOT_STATED'},
}
LABELS = {'assay.msi_category': (r'MSI类别|MSI分类', r'MSI category'),
          'assay.tmb_qualitative': (r'TMB定性', r'TMB qualitative'),
          'drug_evidence.direction': (r'依据方向|药物依据方向', r'direction')}


def canonical(key, text):
    text = normalized(text)
    chinese, english = LABELS[key]
    prefix = r'^(?:(?:' + chinese + r'|原报告|原文|检测结果|结果)\s*[:：]?\s*|(?:' + english + r'|result)(?:\s*[:：]\s*|\s+))'
    for _ in range(2):
        text, count = re.subn(prefix, '', text, count=1, flags=re.I)
        if not count:
            break
    return text


def requirement(key, value):
    code, raw = value['code'], value['raw']
    if code == 'UNKNOWN':
        # This is the application's unclassified state, not an assertion that
        # the report says uncertain. The complete original phrase must survive.
        return 'UNCLASSIFIED:' + canonical(key, raw), lambda text: {'UNCLASSIFIED:' + canonical(key, text)}
    if TERMS[key].get(canonical(key, raw)) != code:
        raise ValidationError('类别或方向代码不符合完整原词；无法确定的药物方向请保留在原文陈述中。')
    return code, lambda text: {TERMS[key].get(canonical(key, text))} - {None}


def validate_coded_source(fact, content, pieces):
    code, classify = requirement(fact.field_key, content['value'])
    if fact.origin == 'AUTOMATIC':
        from .clinical_segments import Piece
        from .molecular_segments import MolecularSegment
        segment = MolecularSegment(fact.clinical_report.title, [Piece(s.ocr_block, s.start_offset, s.end_offset)
            for s in fact.clinical_report.spans.select_related('ocr_block__document_page').all()])
        actual = [Piece(p.ocr_block, p.start_offset, p.end_offset) for p in pieces if p.ocr_block_id]
        windows = table_windows(segment, fact.field_key, content['value']['raw'], actual)
        if windows is not None:
            if not all(classify(window) == {code} for window in windows):
                raise ValidationError('完整原始表格单元格不能支持此分类。')
            return
    validate_original_assertion(fact, code, content['value']['raw'], pieces, classify=classify)


def table_windows(segment, key, raw, pieces):
    """Reconstruct real headers/cells; typed source slices cannot invent a cell."""
    from .molecular_extraction import _records
    position = lambda p: (p.block.pk, p.start, p.end)
    relevant = {position(p) for p in pieces if normalized(raw) in normalized(p.text)}
    if not relevant:
        return None
    windows, covered = [], set()
    for _, cells, table in _records(segment):
        cell = cells.get(key) if table else None
        if cell is None:
            continue
        whole_cell = {position(p) for p in cell.pieces}
        if whole_cell & relevant:
            # Require the entire original cell, including every source piece.
            # A shortened fragment cannot be upgraded into a table boundary.
            if not whole_cell <= relevant or covered & whole_cell:
                return None
            covered.update(whole_cell)
            windows.append(cell.raw)
    return windows if covered == relevant else None
