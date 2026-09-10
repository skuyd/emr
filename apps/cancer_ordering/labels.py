"""Explanations shared by the current view and explicitly selected output."""

REASONS = {
    'collection_incomplete': '部分当前报告还未完成表述收集。请重新收集后查看；暂用通用顺序。',
    'no_reported_diagnosis': '当前没有可用于排列的明确报告表述。',
    'reported_diagnoses_differ': '报告表述存在不同癌种，请核对原件或明确选择显示顺序。',
    'unsupported_reported_diagnosis': '当前表述尚无对应的指标顺序。',
    'original_review_required': '部分表述或来源仍需对照原件核对，暂用通用顺序。',
    'reported_diagnosis': '根据当前报告中的明确表述自动排列。',
    'selected_reported_diagnosis': '使用你明确选择的报告表述。',
    'manual_display_preference': '使用你手动选择的显示顺序。',
    'explicit_general': '使用你选择的通用顺序。',
    'selection_source_changed': '原先选择的来源已经变化，暂用通用顺序。请核对后重新选择。',
    'selection_author_or_configuration_changed': '原先选择的记录已失效，暂用通用顺序。请重新选择。',
}
