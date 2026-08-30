from apps.processing.ocr.base import recognize_page
from apps.processing.ocr.text_layer import TextLayerOcrProvider
from apps.processing.preparation import PreparedPage, PreparedPageKind, PreparedTextSpan


def test_text_layer_provider_preserves_text_and_coordinates_exactly():
    first_polygon = ((0.1, 0.1), (0.4, 0.1), (0.4, 0.2), (0.1, 0.2))
    second_polygon = ((0.5, 0.1), (0.8, 0.1), (0.8, 0.2), (0.5, 0.2))
    prepared = PreparedPage(
        page_number=3,
        kind=PreparedPageKind.TEXT_LAYER,
        width=612,
        height=792,
        source_width=612,
        source_height=792,
        text_spans=(
            PreparedTextSpan("白细胞", first_polygon),
            PreparedTextSpan("4.2", second_polygon),
        ),
    )

    page = recognize_page(TextLayerOcrProvider(provider_version="pdfium-5.13.0"), prepared)

    assert page.page_number == 3
    assert page.full_text == "白细胞\n4.2"
    assert page.regions[0].text == "白细胞"
    assert page.regions[0].polygon == first_polygon
    assert page.regions[0].confidence == 1.0
    assert page.provider == "pdf-text-layer"
    assert page.provider_version == "pdfium-5.13.0"


def test_text_layer_provider_rejects_raster_page(tmp_path):
    from apps.processing.ocr.base import OcrContractError
    from apps.processing.preparation import PreparedPage
    import pytest

    raster = tmp_path / "page.png"
    raster.write_bytes(b"fixture")
    prepared = PreparedPage(1, PreparedPageKind.RASTER, 10, 10, 10, 10, raster_path=raster)

    with pytest.raises(OcrContractError):
        TextLayerOcrProvider().recognize(prepared)
