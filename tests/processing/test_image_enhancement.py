"""Observable image and original-coordinate contracts; all images are synthetic."""
import hashlib
import io

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

from apps.processing.images import prepare_image


def encoded(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def paper_photo(*, perspective=False, shadow=False):
    paper = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(paper)
    font = ImageFont.truetype("DejaVuSans.ttf", 26) if __import__("os").name != "nt" else ImageFont.truetype("arial.ttf", 26)
    for y in range(90, 650, 60):
        draw.text((40, y), "LAB RESULT 123.45 mg/L", fill="black", font=font)
    draw.rectangle((40, 710, 120, 730), fill=(220, 20, 20))
    pixels = np.asarray(paper).copy()
    if shadow:
        illumination = np.linspace(.42, 1.0, pixels.shape[1])[None, :, None]
        pixels = (pixels * illumination).astype(np.uint8)
    corners = np.float32([[100, 100], [700, 100], [700, 900], [100, 900]])
    if perspective:
        corners = np.float32([[140, 75], [725, 150], [660, 915], [70, 830]])
    transform = cv2.getPerspectiveTransform(np.float32([[0, 0], [600, 0], [600, 800], [0, 800]]), corners)
    photo = cv2.warpPerspective(pixels, transform, (800, 1000), borderValue=(35, 40, 45))
    return Image.fromarray(photo), transform


def red_bounds(image):
    pixels = np.asarray(image.convert("RGB"))
    ys, xs = np.where((pixels[:, :, 0] > 100) & (pixels[:, :, 0] > pixels[:, :, 1] * 1.5) & (pixels[:, :, 1] < 100))
    assert len(xs) > 50, "Enhancement lost the source marker"
    return ((xs.min()/image.width, ys.min()/image.height), (xs.max()/image.width, ys.min()/image.height),
            (xs.max()/image.width, ys.max()/image.height), (xs.min()/image.width, ys.max()/image.height))


@pytest.mark.parametrize("perspective", [False, True])
@pytest.mark.parametrize("pixel_budget", [400_000, 40_000_000])
def test_paper_edges_are_cropped_and_marker_maps_back_to_original(perspective, pixel_budget, monkeypatch):
    monkeypatch.setattr("apps.processing.images.MAX_OCR_PAGE_PIXELS", pixel_budget)
    original, _ = paper_photo(perspective=perspective)
    payload = encoded(original)
    digest = hashlib.sha256(payload).hexdigest()
    with prepare_image(io.BytesIO(payload), "image/png") as prepared:
        page = prepared.pages[0]
        assert page.width < 750 and page.height < 930, "The dark desktop was not removed"
        assert page.width*page.height <= pixel_budget
        from apps.processing.geometry import source_polygon
        with Image.open(page.raster_path) as image:
            mapped = source_polygon(page.source_transform, red_bounds(image))
        actual = red_bounds(original)
        assert mapped is not None
        assert min(x for x, _ in mapped) == pytest.approx(min(x for x, _ in actual), abs=.012)
        assert max(x for x, _ in mapped) == pytest.approx(max(x for x, _ in actual), abs=.012)
        assert min(y for _, y in mapped) == pytest.approx(min(y for _, y in actual), abs=.012)
        assert max(y for _, y in mapped) == pytest.approx(max(y for _, y in actual), abs=.012)
        assert "paper_rectified" in page.preparation_metadata["steps"]
    assert hashlib.sha256(payload).hexdigest() == digest


def test_shadow_is_reduced_without_erasing_dark_text_or_coloured_marks():
    original, _ = paper_photo(shadow=True)
    with prepare_image(io.BytesIO(encoded(original)), "image/png") as prepared:
        page = prepared.pages[0]
        with Image.open(page.raster_path) as image:
            pixels = np.asarray(image.convert("L"))
            interior = pixels[int(.12*image.height):int(.65*image.height), :]
            left, right = np.percentile(interior[:, int(.1*image.width):int(.25*image.width)], 90), np.percentile(interior[:, int(.75*image.width):int(.9*image.width)], 90)
            assert abs(float(left)-float(right)) < 22, "Uneven illumination still dominates the page"
            assert np.percentile(interior, 3) < 110, "Text was washed out"
            red_bounds(image)
        assert "illumination_normalized" in page.preparation_metadata["steps"]


def test_already_cropped_scan_retains_edge_text_and_source_geometry():
    source = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(source)
    for x in (0, 586):
        draw.rectangle((x, 180, x+13, 230), fill="black")
    draw.rectangle((180, 0, 230, 13), fill="black")
    payload = encoded(source)
    with prepare_image(io.BytesIO(payload), "image/png") as prepared:
        page = prepared.pages[0]
        assert (page.width, page.height) == (600, 800)
        with Image.open(page.raster_path) as raster:
            assert raster.convert("L").getpixel((0, 200)) < 30
            assert raster.convert("L").getpixel((599, 200)) < 30
            assert raster.convert("L").getpixel((200, 0)) < 30
        assert page.source_transform == ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


def test_perspective_text_rows_are_straightened_for_layout_association():
    original, _ = paper_photo(perspective=True)
    with prepare_image(io.BytesIO(encoded(original)), "image/png") as prepared:
        with Image.open(prepared.pages[0].raster_path) as raster:
            pixels = np.asarray(raster.convert("L"))
        # The same first line's ink baseline should agree across the two halves.
        band = pixels[int(.08*raster.height):int(.17*raster.height)]
        left = np.where(band[:, int(.07*raster.width):int(.3*raster.width)] < 90)[0]
        right = np.where(band[:, int(.4*raster.width):int(.65*raster.width)] < 90)[0]
        assert len(left) and len(right)
        assert abs(float(np.percentile(left, 90))-float(np.percentile(right, 90))) < 5


def test_skewed_scan_is_straightened_without_cropping_the_expanded_canvas():
    photo, _ = paper_photo()
    scan = photo.crop((100, 100, 700, 900)).rotate(6, expand=True, fillcolor="white")
    with prepare_image(io.BytesIO(encoded(scan)), "image/png") as prepared:
        page = prepared.pages[0]
        assert "deskewed" in page.preparation_metadata["steps"]
        assert abs(page.preparation_metadata["deskew_degrees"]) == pytest.approx(6., abs=1.)
        assert page.width*page.height <= scan.width*scan.height
        with Image.open(page.raster_path) as image:
            red_bounds(image)


def test_nearly_straight_text_is_not_resampled_for_a_subdegree_estimate():
    source = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(source)
    for y in range(90, 650, 60):
        for x in range(40, 520, 8):
            draw.rectangle((x, y, x+4, y+16), fill="black")
    scan = source.rotate(.8, expand=True, fillcolor="white")
    with prepare_image(io.BytesIO(encoded(scan)), "image/png") as prepared:
        page = prepared.pages[0]
        assert "deskewed" not in page.preparation_metadata["steps"]
        with Image.open(page.raster_path) as image:
            assert image.tobytes() == scan.tobytes()


@pytest.mark.parametrize("failed_step", ["_paper_quad", "_photometric"])
def test_failed_enhancement_uses_original_pixels_and_only_a_stable_reason(monkeypatch, failed_step):
    def fail(*_):
        raise RuntimeError("private source text must never appear in diagnostics")
    monkeypatch.setattr("apps.processing.image_enhancement." + failed_step, fail)
    original, _ = paper_photo()
    if failed_step == "_photometric":
        original = original.crop((100, 100, 700, 900)).rotate(6, expand=True, fillcolor="white")
    with prepare_image(io.BytesIO(encoded(original)), "image/png") as prepared:
        page = prepared.pages[0]
        with Image.open(page.raster_path) as image:
            assert image.tobytes() == original.tobytes()
        assert page.preparation_metadata["steps"] == []
        assert page.preparation_metadata["warnings"] == ["enhancement_failed"]
        assert "deskew_degrees" not in page.preparation_metadata


@pytest.mark.parametrize("orientation", [2, 6, 7, 8])
def test_source_coordinates_follow_the_same_exif_orientation_as_the_original_viewer(orientation):
    from PIL import ImageOps
    from apps.processing.geometry import source_polygon
    original, _ = paper_photo()
    exif = Image.Exif()
    exif[274] = orientation
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG", quality=98, exif=exif)
    payload = buffer.getvalue()
    with Image.open(io.BytesIO(payload)) as decoded:
        oriented = ImageOps.exif_transpose(decoded)
        expected = red_bounds(oriented)
    with prepare_image(io.BytesIO(payload), "image/jpeg") as prepared:
        page = prepared.pages[0]
        with Image.open(page.raster_path) as image:
            actual = source_polygon(page.source_transform, red_bounds(image))
        assert actual is not None
        assert min(x for x, _ in actual) == pytest.approx(min(x for x, _ in expected), abs=.012)
        assert min(y for _, y in actual) == pytest.approx(min(y for _, y in expected), abs=.012)


def test_transparent_scan_keeps_visible_ink_on_white_background():
    source = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((0, 100, 45, 140), fill=(0, 0, 0, 255))
    with prepare_image(io.BytesIO(encoded(source)), "image/png") as prepared:
        with Image.open(prepared.pages[0].raster_path) as image:
            assert image.getpixel((20, 120)) == (0, 0, 0)
            assert image.getpixel((599, 799)) == (255, 255, 255)


def test_low_contrast_scan_gets_stronger_text_contrast():
    source = Image.new("RGB", (600, 800), (190, 190, 190))
    draw = ImageDraw.Draw(source)
    for y in range(100, 650, 50):
        for x in range(40, 500, 30):
            draw.rectangle((x, y, x+12, y+18), fill=(160, 160, 160))
    with prepare_image(io.BytesIO(encoded(source)), "image/png") as prepared:
        with Image.open(prepared.pages[0].raster_path) as image:
            assert image.getpixel((30, 90))[0]-image.getpixel((45, 110))[0] > 60


def test_dark_interface_bands_are_not_treated_as_paper_shadows():
    source = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 599, 110), fill=(41, 41, 41))
    for y in range(180, 700, 50):
        for x in range(40, 500, 30):
            draw.rectangle((x, y, x+12, y+18), fill=(160, 160, 160))
    with prepare_image(io.BytesIO(encoded(source)), "image/png") as prepared:
        page = prepared.pages[0]
        with Image.open(page.raster_path) as image:
            assert image.tobytes() == source.tobytes()
        assert page.preparation_metadata["steps"] == []


def test_antialiased_black_text_does_not_trigger_faint_ink_contrast():
    source = Image.new("RGB", (600, 800), "white")
    font = ImageFont.truetype("arial.ttf" if __import__("os").name == "nt" else "DejaVuSans.ttf", 26)
    draw = ImageDraw.Draw(source)
    for y in range(90, 650, 60):
        draw.text((40, y), "LAB RESULT 123.45 mg/L", fill="black", font=font)
    with prepare_image(io.BytesIO(encoded(source)), "image/png") as prepared:
        assert "contrast_enhanced" not in prepared.pages[0].preparation_metadata["steps"]


def test_raster_pdf_uses_enhancement_but_text_page_remains_exact():
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader
    from apps.processing.preparation import prepare_document, PreparedPageKind
    original, _ = paper_photo(perspective=True)
    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(800, 1000))
    canvas.drawImage(ImageReader(original), 0, 0, width=800, height=1000)
    canvas.showPage()
    canvas.drawString(30, 800, "A synthetic PDF text layer stays directly extractable 12345")
    canvas.save()
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    with prepare_document(io.BytesIO(payload), "application/pdf") as prepared:
        raster, text = prepared.pages
        assert raster.kind == PreparedPageKind.RASTER
        assert "paper_rectified" in raster.preparation_metadata["steps"]
        from apps.processing.geometry import source_polygon
        with Image.open(raster.raster_path) as image:
            mapped = source_polygon(raster.source_transform, red_bounds(image))
        expected = red_bounds(original)
        assert mapped is not None
        assert min(x for x, _ in mapped) == pytest.approx(min(x for x, _ in expected), abs=.012)
        assert min(y for _, y in mapped) == pytest.approx(min(y for _, y in expected), abs=.012)
        assert text.kind == PreparedPageKind.TEXT_LAYER
        assert "synthetic PDF text layer" in text.full_text
        assert text.preparation_metadata["steps"] == []
        assert raster.page_number == 1 and text.page_number == 2
    assert hashlib.sha256(payload).hexdigest() == digest
