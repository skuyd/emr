from pathlib import Path

import pytest

from apps.processing.ocr.base import OcrContractError, recognize_page
from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.preparation import PreparedPage, PreparedPageKind
from apps.processing.value_objects import OcrPage, OcrRegion


POLYGON = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.2), (0.1, 0.2))


def _prepared_raster(tmp_path):
    path = Path(tmp_path) / "page.png"
    path.write_bytes(b"synthetic-raster-fixture")
    return PreparedPage(
        page_number=1,
        kind=PreparedPageKind.RASTER,
        width=100,
        height=200,
        source_width=100,
        source_height=200,
        raster_path=path,
    )


def _ocr_page():
    return OcrPage(
        page_number=1,
        width=100,
        height=200,
        regions=(
            OcrRegion("4.2", POLYGON, 0.97, reading_order=2),
            OcrRegion("白细胞", POLYGON, 0.99, reading_order=1),
        ),
        provider="fixture",
        provider_version="1.0",
        provider_metadata={"model": "synthetic-fixture"},
    )


def test_fixture_provider_returns_complete_page_in_deterministic_order(tmp_path):
    prepared = _prepared_raster(tmp_path)
    provider = FixtureOcrProvider((_ocr_page(),))

    first = recognize_page(provider, prepared)
    second = recognize_page(provider, prepared)

    assert first == second
    assert [region.text for region in first.regions] == ["白细胞", "4.2"]
    assert first.provider_metadata == (("model", "synthetic-fixture"),)


@pytest.mark.parametrize(
    "invalid_page",
    [
        OcrPage(2, 100, 200, (), "fixture", "1.0"),
        OcrPage(1, 101, 200, (), "fixture", "1.0"),
        "not-an-ocr-page",
    ],
)
def test_contract_rejects_wrong_page_identity_dimensions_or_type(tmp_path, invalid_page):
    prepared = _prepared_raster(tmp_path)
    provider = FixtureOcrProvider({1: invalid_page}, validate_fixture=False)

    with pytest.raises(OcrContractError) as error:
        recognize_page(provider, prepared)
    assert error.value.code == "invalid_ocr_result"


def test_fixture_provider_does_not_fall_back_to_another_page(tmp_path):
    prepared = _prepared_raster(tmp_path)
    provider = FixtureOcrProvider({2: OcrPage(2, 100, 200, (), "fixture", "1.0")})

    with pytest.raises(OcrContractError):
        recognize_page(provider, prepared)
