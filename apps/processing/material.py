"""Conservative document/photo suggestions, independent of OCR success and type.

This is a local appearance heuristic, not a semantic photograph classifier.
Only colour, texture and sparse text jointly support a non-document suggestion.
Ambiguity must retain UNCERTAIN; every input still goes through OCR/extraction.
"""
from collections import Counter

import numpy as np
from PIL import Image

from .preparation import PreparedPageKind


MATERIAL_POLICY_VERSION = "material-1"
DOCUMENT = "DOCUMENT"
NON_DOCUMENT = "NON_DOCUMENT"
UNCERTAIN = "UNCERTAIN"
_MEDICAL_TEXT = ("报告", "医院", "检验", "检查所见", "检查结论", "诊断", "病理", "医嘱", "处方", "超声", "影像", "白细胞", "血小板", "血红蛋白", "患者", "标本")


def _visual_signals(prepared):
    with prepared.open_raster() as stream, Image.open(stream) as image:
        image = image.convert("RGB")
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        pixels = np.asarray(image, dtype=np.int16)
        gray = np.asarray(image.convert("L"), dtype=np.float32)
    chroma = pixels.max(axis=2) - pixels.min(axis=2)
    bins = pixels // 32
    histogram = np.bincount((bins[:, :, 0] * 64 + bins[:, :, 1] * 8 + bins[:, :, 2]).ravel(), minlength=512)
    probabilities = histogram[histogram > 0] / gray.size
    entropy = -float(np.sum(probabilities * np.log2(probabilities)))
    gradients = np.abs(np.diff(gray, axis=0)[:, :-1]) + np.abs(np.diff(gray, axis=1)[:-1, :])
    return {
        "colour_fraction": round(float(np.mean(chroma > 45)), 4),
        "colour_entropy": round(entropy, 4),
        "texture_fraction": round(float(np.mean(gradients > 22)) if gradients.size else 0., 4),
        "gray_spread": round(float(np.std(gray)), 4),
        "bright_neutral_fraction": round(float(np.mean((gray > 170) & (chroma < 35))), 4),
    }


def _page(prepared, ocr):
    row = {"page_number": prepared.page_number, "status": UNCERTAIN, "precision": "page", "reason_codes": [], "signals": {}}
    if ocr is None:
        row["reason_codes"] = ["ocr_page_unavailable"]
        return row
    readable = [region for region in ocr.regions if region.confidence >= .8]
    text = " ".join(region.text for region in readable)
    row["signals"]["readable_characters"] = len(text)
    row["signals"]["readable_regions"] = len(readable)
    if prepared.kind == PreparedPageKind.TEXT_LAYER:
        row.update(status=DOCUMENT, reason_codes=["pdf_text_layer"])
    elif any(word in text for word in _MEDICAL_TEXT):
        row.update(status=DOCUMENT, reason_codes=["document_text_present"])
    elif len(text) >= 24 and len(readable) >= 2:
        row.update(status=DOCUMENT, reason_codes=["readable_text_layout"])
    elif "paper_rectified" in prepared.preparation_metadata.get("steps", ()):
        row.update(status=DOCUMENT, reason_codes=["paper_boundary_present"])
    else:
        try:
            signals = _visual_signals(prepared)
        except Exception:
            # A failed optional analysis must not fail OCR or disclose paths.
            row["reason_codes"] = ["image_analysis_unavailable"]
            return row
        row["signals"].update(signals)
        if (len(text) < 12 and signals["colour_fraction"] >= .55
                and signals["colour_entropy"] >= 4.
                and signals["texture_fraction"] >= .12
                and signals["gray_spread"] >= 20.
                and signals["bright_neutral_fraction"] < .3):
            row.update(status=NON_DOCUMENT, reason_codes=["colour_texture_with_sparse_text"])
        else:
            row["reason_codes"] = ["insufficient_material_evidence"]
    return row


def classify_material(prepared_pages, ocr_pages):
    """Return page-scoped evidence; mixed PDFs never inherit a photo-only label."""
    recognized = {page.page_number: page for page in ocr_pages}
    pages = [_page(page, recognized.get(page.page_number)) for page in prepared_pages]
    counts = Counter(page["status"] for page in pages)
    status = DOCUMENT if counts[DOCUMENT] else NON_DOCUMENT if pages and counts[NON_DOCUMENT] == len(pages) else UNCERTAIN
    return {"version": MATERIAL_POLICY_VERSION, "status": status, "pages": pages}
