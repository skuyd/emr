"""Actual local OpenCV decoding, exclusively synthetic payloads and pixels."""

import hashlib
import io

import cv2
import numpy as np
from PIL import Image
import pytest


URL = 'https://images.example.invalid/view?token=SYNTHETIC_QR'


def qr_image(payload=URL, *, scale=8):
    encoded = cv2.QRCodeEncoder_create().encode(payload)
    bordered = np.pad(encoded, 4, constant_values=255)
    return Image.fromarray(bordered).resize((bordered.shape[1] * scale, bordered.shape[0] * scale), Image.Resampling.NEAREST).convert('RGB')


def png_bytes(image):
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('angle', [0, 90, 180, 270])
def test_real_qr_rotation_preserves_payload_and_original_region(angle):
    from apps.cloud_imaging.decoding import decode_page

    canvas = Image.new('RGB', (1100, 850), 'white')
    encoded = qr_image().rotate(angle, expand=True)
    canvas.paste(encoded, (520, 330))
    result = decode_page(png_bytes(canvas))
    urls = [row for row in result['candidates'] if row['payload_type'] == 'URL']
    assert result['status'] == 'FOUND' and len(urls) == 1
    row = urls[0]
    assert row['payload'] == URL and row['payload_sha256'] == hashlib.sha256(URL.encode()).hexdigest()
    assert row['input_sha256'] and row['decoder_version'] and row['render_profile']
    assert len(row['transform']) == 3
    points = row['polygon']
    assert points and all(520 / 1100 <= x <= (520 + encoded.width) / 1100 for x, _ in points)
    assert all(330 / 850 <= y <= (330 + encoded.height) / 850 for _, y in points)


def test_identical_payloads_in_separate_qr_regions_keep_separate_evidence():
    from apps.cloud_imaging.decoding import decode_page

    canvas = Image.new('RGB', (1300, 800), 'white')
    encoded = qr_image()
    canvas.paste(encoded, (100, 100))
    canvas.paste(encoded, (750, 320))
    result = decode_page(png_bytes(canvas))
    assert len(result['candidates']) == 2
    left, right = sorted(result['candidates'], key=lambda row: min(point[0] for point in row['polygon']))
    assert left['payload'] == right['payload'] == URL
    assert max(point[0] for point in left['polygon']) < min(point[0] for point in right['polygon'])


def test_non_url_qr_is_a_preserved_unsupported_payload_and_blank_page_is_no_qr():
    from apps.cloud_imaging.decoding import decode_page

    result = decode_page(png_bytes(qr_image('SYNTHETIC unsupported card')))
    assert result['candidates'][0]['payload_type'] == 'UNSUPPORTED'
    assert result['candidates'][0]['payload'] == 'SYNTHETIC unsupported card'
    assert result['candidates'][0]['reason_code'] == 'invalid_external_url'
    empty = decode_page(png_bytes(Image.new('RGB', (600, 500), 'white')))
    assert empty['status'] == 'NO_QR' and empty['candidates'] == []


def test_invalid_page_bytes_are_failed_not_a_negative_qr_result():
    from apps.cloud_imaging.decoding import decode_page

    result = decode_page(b'SYNTHETIC invalid image')
    assert result['status'] == 'FAILED' and result['reason_code'] == 'page_decode_failed'
    assert result['candidates'] == []


def test_ocr_candidates_use_exact_unicode_slice_and_do_not_join_different_lines():
    from apps.cloud_imaging.decoding import text_candidates

    raw = '核对😀 云影像：' + URL + '。下一行\nhttps://images.example.invalid/path?key=\nCONTINUATION'
    result = text_candidates(raw)
    first, broken = result
    assert first['payload'] == URL
    assert raw[first['start_offset']:first['end_offset']] == URL
    assert first['payload_type'] == 'URL' and first['reason_code'] == ''
    assert broken['payload_type'] == 'UNSUPPORTED' and broken['reason_code'] == 'ambiguous_line_break'
    assert 'CONTINUATION' not in broken['payload']
    assert raw[broken['start_offset']:broken['end_offset']] == broken['payload']
