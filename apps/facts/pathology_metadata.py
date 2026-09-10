"""Explicit label/value cells within one already bounded report lane."""
from .clinical_segments import ReportText, _box


IDENTITY_LABELS = {"标本编号", "标本号", "样本编号", "肿瘤样本编号", "蜡块编号", "组织块号"}
METADATA_LABELS = IDENTITY_LABELS | {
    "标本类型", "样本类型", "送检材料", "标本名称", "标本描述", "取材部位", "送检部位", "取材方式",
    "检测项目", "检测名称", "检测方法", "抗体克隆号", "抗体克隆", "克隆号",
    "采样日期", "采集日期", "取材日期", "收样日期", "接收日期", "接收时间",
    "样本接收日期", "样本接收时间", "报告日期", "报告时间",
}
CELL_BOUNDARIES = METADATA_LABELS | {
    "检测结果", "染色结果", "免疫组化结果", "免疫组织化学结果", "检测抗体", "抗体名称", "标记物",
    "受检者基本信息", "样本基本信息", "质量控制", "样本质控结果", "阳性对照", "阴性对照",
    "检测图谱", "染色图像", "染色图谱", "检测说明", "说明", "备注",
}


def split_metadata(pieces):
    """Return only unique adjacent horizontal cells; never insert punctuation.

    Missing geometry, intersecting boxes, more than one value or another
    printed label ends the association. Reading order is not an ownership key.
    Vertical continuation and unlabeled date lines remain outside this rule.
    """
    located = [(piece, _box(piece.block), ReportText([piece])) for piece in pieces
               if piece.start == 0 and piece.end == len(piece.block.text)
               and len(piece.text.splitlines()) == 1 and _box(piece.block)]
    for label_piece, label_box, label_view in located:
        label = label_view.text.rstrip(":：")
        if label not in METADATA_LABELS:
            continue
        row = [(piece, box, view) for piece, box, view in located
               if piece.page == label_piece.page
               and min(box[3], label_box[3]) - max(box[1], label_box[1])
               >= min(box[3] - box[1], label_box[3] - label_box[1]) / 2]
        row.sort(key=lambda item: item[1][0])
        if any(a[1][2] >= b[1][0] for a, b in zip(row, row[1:])):
            continue
        position = next(i for i, item in enumerate(row) if item[0] is label_piece)
        values = []
        for item in row[position + 1:]:
            if item[2].text.rstrip(":：") in CELL_BOUNDARIES:
                break
            values.append(item)
        if len(values) != 1:
            continue
        value_piece, _, value_view = values[0]
        if not value_view.text:
            continue
        view = ReportText([label_piece, value_piece])
        yield label, view, len(label_view.text)
