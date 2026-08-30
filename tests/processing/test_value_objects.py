from dataclasses import FrozenInstanceError
import math

import pytest

from apps.processing.value_objects import InvalidOcrPage, InvalidRegion, OcrPage, OcrRegion


VALID_POLYGON = ((0.1, 0.1), (0.9, 0.1), (0.9, 0.2), (0.1, 0.2))


def test_region_is_immutable_and_normalizes_values():
    region = OcrRegion(
        text=" 白细胞 ",
        polygon=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]],
        confidence=0.98,
        reading_order=2,
    )

    assert region.text == "白细胞"
    assert region.polygon == VALID_POLYGON
    assert region.confidence == 0.98
    with pytest.raises(FrozenInstanceError):
        region.text = "changed"


@pytest.mark.parametrize(
    "polygon",
    [
        ((0.1, 0.1), (1.2, 0.1), (1.2, 0.2), (0.1, 0.2)),
        ((-0.1, 0.1), (0.9, 0.1), (0.9, 0.2), (0.1, 0.2)),
        ((0.1, 0.1), (0.9, 0.1)),
        ((0.1, 0.1), (0.9, math.nan), (0.9, 0.2)),
        ((True, 0.1), (0.9, 0.1), (0.9, 0.2)),
    ],
)
def test_region_rejects_invalid_normalized_coordinates(polygon):
    with pytest.raises(InvalidRegion):
        OcrRegion(text="白细胞", polygon=polygon, confidence=0.98)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_region_rejects_missing_text(text):
    with pytest.raises(InvalidRegion):
        OcrRegion(text=text, polygon=VALID_POLYGON, confidence=0.98)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan, True, "0.9"])
def test_region_rejects_invalid_confidence(confidence):
    with pytest.raises(InvalidRegion):
        OcrRegion(text="白细胞", polygon=VALID_POLYGON, confidence=confidence)


def test_page_orders_regions_deterministically_and_builds_full_text():
    later = OcrRegion("4.2", VALID_POLYGON, 0.97, reading_order=2)
    earlier = OcrRegion("白细胞", VALID_POLYGON, 0.99, reading_order=1)

    page = OcrPage(
        page_number=1,
        width=2480,
        height=3508,
        regions=(later, earlier),
        provider="fixture",
        provider_version="1.0",
    )

    assert page.regions == (earlier, later)
    assert page.full_text == "白细胞\n4.2"


@pytest.mark.parametrize(
    "changes",
    [
        {"page_number": 0},
        {"width": 0},
        {"height": -1},
        {"provider": ""},
        {"provider_version": ""},
        {"regions": (OcrRegion("a", VALID_POLYGON, 1.0, 1), OcrRegion("b", VALID_POLYGON, 1.0, 1))},
    ],
)
def test_page_rejects_invalid_identity_dimensions_and_duplicate_order(changes):
    values = {
        "page_number": 1,
        "width": 100,
        "height": 200,
        "regions": (),
        "provider": "fixture",
        "provider_version": "1.0",
    }
    values.update(changes)
    with pytest.raises(InvalidOcrPage):
        OcrPage(**values)
