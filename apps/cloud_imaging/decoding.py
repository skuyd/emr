"""Bounded, local-only QR and literal OCR candidates with original geometry."""

import hashlib
import io
import math
import re
import warnings

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError
from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables

from apps.processing.geometry import source_polygon

from .models import MAX_PAYLOAD_LENGTH
from .url_policy import validate_url


RULES_VERSION = 'cloud-local-scan-v1'
DECODER_VERSION = f'opencv-{cv2.__version__}'
MAX_PIXELS = 20_000_000
DECODE_PIXELS = 4_000_000
MAX_IMAGE_BYTES = 100 * 1024 * 1024
_ADDRESS = re.compile(r'https?://[^\s<>"\'\u3002\uff0c\uff1b\uff01\uff1f\u3001]+', re.IGNORECASE)
_TRANSFORMS = (
    [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
    [[0., -1., 1.], [1., 0., 0.], [0., 0., 1.]],
    [[-1., 0., 1.], [0., -1., 1.], [0., 0., 1.]],
    [[0., 1., 0.], [-1., 0., 1.], [0., 0., 1.]],
)


@sensitive_variables()
def _payload(value):
    payload_hash = hashlib.sha256(value.encode()).hexdigest()
    if len(value) > MAX_PAYLOAD_LENGTH:
        # Do not turn a truncated payload into a complete address. The scanner
        # retains this hash/length as an unjudged page result, not fake evidence.
        return {'payload': '', 'payload_sha256': hashlib.sha256(b'').hexdigest(),
                'payload_type': 'UNSUPPORTED', 'reason_code': 'payload_too_long',
                'unavailable_payload_sha256': payload_hash, 'unavailable_payload_length': len(value)}
    result = {'payload': value, 'payload_sha256': payload_hash, 'payload_type': 'URL', 'reason_code': ''}
    if not value:
        result.update(payload_type='UNDECODED', reason_code='qr_undecoded')
    else:
        try:
            validate_url(value)
        except ValidationError:
            result.update(payload_type='UNSUPPORTED', reason_code='invalid_external_url')
    return result


@sensitive_variables()
def text_candidates(raw_text):
    result = []
    for match in _ADDRESS.finditer(raw_text):
        start, end = match.span()
        payload = raw_text[start:end]
        # A single matching surrounding parenthesis is a proved wrapper.
        if payload.endswith(')') and start and raw_text[start - 1] == '(' and payload.count('(') + 1 == payload.count(')'):
            end -= 1
            payload = raw_text[start:end]
        row = _payload(payload)
        if row['reason_code'] == 'payload_too_long':
            row.update(start_offset=None, end_offset=None)
        else:
            row.update(start_offset=start, end_offset=end)
        following = raw_text[end:]
        if following.startswith(('\r', '\n')):
            next_line = following.lstrip('\r\n').splitlines()[0] if following.lstrip('\r\n') else ''
            if payload.endswith(('=', '/', '?', '&', '%', '-')) or next_line.startswith(('/', '?', '&', '=', '%')):
                row.update(payload_type='UNSUPPORTED', reason_code='ambiguous_line_break')
        result.append(row)
    return result


def _same_region(left, right):
    if left['payload_sha256'] != right['payload_sha256'] or not left['polygon'] or not right['polygon']:
        return False
    def box(row):
        points = row['polygon']
        return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)
    a, b = box(left), box(right)
    overlap = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return bool(union and overlap / union > .5)


@sensitive_variables()
def _detected(image):
    detector = cv2.QRCodeDetector()
    found, payloads, points, _ = detector.detectAndDecodeMulti(image)
    if found and points is not None:
        return list(zip(payloads, points))
    payload, points, _ = detector.detectAndDecode(image)
    return [(payload, points.reshape((4, 2)))] if points is not None else []


@sensitive_variables()
def decode_page(png):
    try:
        if not isinstance(png, bytes) or not png or len(png) > MAX_IMAGE_BYTES:
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(png)) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError
                source.load()
                gray = np.array(source.convert('L'))
        original_height, original_width = gray.shape
        if gray.size > DECODE_PIXELS:
            scale = math.sqrt(DECODE_PIXELS / gray.size)
            gray = cv2.resize(gray, (max(1, int(original_width * scale)), max(1, int(original_height * scale))), interpolation=cv2.INTER_AREA)
        candidates, attempts, failures = [], 0, 0
        for scale in (1., .65, 1.5):
            if gray.size * scale * scale > DECODE_PIXELS:
                continue
            resized = gray if scale == 1 else cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
            for rotation in range(4):
                image = np.ascontiguousarray(np.rot90(resized, rotation))
                attempts += 1
                try:
                    detected = _detected(image)
                    encoded, input_png = cv2.imencode('.png', image)
                    if not encoded:
                        raise ValueError
                    input_hash = hashlib.sha256(input_png.tobytes()).hexdigest()
                    height, width = image.shape
                    for payload, points in detected:
                        mapped = source_polygon(_TRANSFORMS[rotation], [
                            [float(x) / max(1, width - 1), float(y) / max(1, height - 1)] for x, y in points
                        ])
                        row = {**_payload(payload), 'polygon': [list(point) for point in mapped] if mapped else None,
                               'transform': _TRANSFORMS[rotation], 'input_sha256': input_hash,
                               'render_profile': f'qr-gray-v1:{original_width}x{original_height}:{width}x{height}:r{rotation * 90}:s{scale}',
                               'decoder_version': DECODER_VERSION}
                        if not any(_same_region(prior, row) for prior in candidates):
                            candidates.append(row)
                except (cv2.error, UnicodeError, ValueError, TypeError, OverflowError):
                    failures += 1
        if candidates:
            status = 'UNDECODED' if all(row['payload_type'] == 'UNDECODED' for row in candidates) else 'FOUND'
        else:
            status = 'FAILED' if failures else 'NO_QR'
        return {'status': status, 'candidates': candidates, 'attempts': attempts, 'failed_attempts': failures,
                'reason_code': 'qr_decode_failed' if failures else ''}
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        return {'status': 'FAILED', 'candidates': [], 'attempts': 0, 'failed_attempts': 0, 'reason_code': 'page_decode_failed'}
