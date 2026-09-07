"""Material suggestions are conservative and never replace OCR/source evidence."""
import io

import numpy as np
from PIL import Image, ImageDraw
import pytest

from apps.processing.preparation import PreparedPage, PreparedPageKind, PreparedTextSpan
from apps.processing.value_objects import OcrPage, OcrRegion


def synthetic_scene():
    """Deterministic coloured scene, unrelated to medical samples."""
    rng = np.random.default_rng(813)
    height, width = 480, 640
    yy, xx = np.mgrid[:height, :width]
    pixels = np.stack((60 + xx * .16, 75 + yy * .24, 205 - yy * .25), axis=-1)
    pixels += rng.normal(0, 16, pixels.shape)
    image = Image.fromarray(np.clip(pixels, 0, 255).astype('uint8'))
    drawing = ImageDraw.Draw(image)
    drawing.polygon([(0, 420), (120, 220), (290, 380), (460, 190), (640, 420), (640, 480), (0, 480)], fill=(51, 117, 55))
    drawing.ellipse((465, 50, 560, 145), fill=(243, 188, 52))
    return image


def page(tmp_path, image, texts=(), *, kind=PreparedPageKind.RASTER, preparation_metadata=None):
    regions = tuple(OcrRegion(text, ((.08,.1+i*.08),(.9,.1+i*.08),(.9,.15+i*.08),(.08,.15+i*.08)), .98, i) for i,text in enumerate(texts))
    ocr = OcrPage(1, image.width, image.height, regions, 'fixture', '1')
    if kind == PreparedPageKind.TEXT_LAYER:
        prepared = PreparedPage(1, kind, image.width, image.height, image.width, image.height,
            text_spans=tuple(PreparedTextSpan(region.text,region.polygon) for region in regions))
    else:
        path = tmp_path / 'synthetic.png'
        image.save(path)
        prepared = PreparedPage(1, kind, image.width, image.height, image.width, image.height,
            raster_path=path, preparation_metadata=preparation_metadata or {})
    return prepared, ocr


def classify(prepared, ocr):
    from apps.processing.material import classify_material
    return classify_material((prepared,), (ocr,))


def test_coloured_scene_gets_recoverable_page_level_suggestion(tmp_path):
    prepared, ocr = page(tmp_path, synthetic_scene())
    result = classify(prepared, ocr)
    assert result['status'] == 'NON_DOCUMENT'
    assert result['version']
    assert result['pages'][0]['page_number'] == 1
    assert result['pages'][0]['precision'] == 'page'
    assert result['pages'][0]['reason_codes']
    assert 'polygon' not in result['pages'][0]


@pytest.mark.parametrize('texts', [
    ('合成医院 检验报告', '白细胞 4.20 10^9/L'),
    ('MRI 检查所见：合成观察内容',),
    ('2026年8月20日', 'WBC 4.20 10^9/L', 'PLT 200 10^9/L'),
])
def test_report_and_partial_screenshot_text_veto_photo_suggestion(tmp_path, texts):
    prepared, ocr = page(tmp_path, synthetic_scene(), texts)
    assert classify(prepared, ocr)['status'] == 'DOCUMENT'


@pytest.mark.parametrize('kind', ['blank', 'handwriting', 'grayscale-photo', 'colour-patch'])
def test_insufficient_image_or_ocr_evidence_remains_uncertain(tmp_path, kind):
    image = Image.new('RGB', (640,480), 'white')
    if kind == 'handwriting':
        ImageDraw.Draw(image).line([(50,80),(95,120),(125,75),(160,140),(200,60),(240,145)], fill=(12,40,100), width=4)
    elif kind == 'grayscale-photo':
        image = synthetic_scene().convert('L').convert('RGB')
    elif kind == 'colour-patch':
        image = Image.new('RGB', (640,480), (220,60,70))
    prepared, ocr = page(tmp_path,image)
    assert classify(prepared,ocr)['status'] == 'UNCERTAIN'


def test_text_pdf_and_detected_paper_remain_documents(tmp_path):
    image = Image.new('RGB',(640,480),'white')
    prepared,ocr = page(tmp_path,image,('合成资料原文',),kind=PreparedPageKind.TEXT_LAYER)
    assert classify(prepared,ocr)['status'] == 'DOCUMENT'
    prepared,ocr = page(tmp_path,image,preparation_metadata={'steps':['paper_rectified']})
    assert classify(prepared,ocr)['status'] == 'DOCUMENT'


def test_analysis_failure_does_not_fail_the_page_or_invent_a_non_document(tmp_path):
    prepared,ocr = page(tmp_path,synthetic_scene())
    prepared.raster_path.unlink()
    result = classify(prepared,ocr)
    assert result['status'] == 'UNCERTAIN'
    assert 'image_analysis_unavailable' in result['pages'][0]['reason_codes']


def test_mixed_pdf_keeps_document_scope_and_separate_photo_page(tmp_path):
    from dataclasses import replace
    from apps.processing.material import classify_material
    photo,photo_ocr = page(tmp_path,synthetic_scene())
    photo,photo_ocr = replace(photo,page_number=2),replace(photo_ocr,page_number=2)
    document,document_ocr = page(tmp_path,Image.new('RGB',(640,480),'white'),('检验报告',),kind=PreparedPageKind.TEXT_LAYER)
    result = classify_material((document,photo),(document_ocr,photo_ocr))
    assert result['status'] == 'DOCUMENT'
    assert [row['status'] for row in result['pages']] == ['DOCUMENT','NON_DOCUMENT']
