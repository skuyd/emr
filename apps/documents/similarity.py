import math
import re
from statistics import median

from PIL import Image


_PERCEPTUAL_HASH = re.compile(r"[0-9a-f]{16}\Z")
MAX_POSSIBLE_DUPLICATE_DISTANCE = 8
_HASH_IMAGE_SIZE = 32
_HASH_COEFFICIENTS = 8
_COSINES = tuple(
    tuple(
        math.cos(math.pi * (2 * position + 1) * frequency / (2 * _HASH_IMAGE_SIZE))
        for frequency in range(_HASH_COEFFICIENTS)
    )
    for position in range(_HASH_IMAGE_SIZE)
)


def valid_perceptual_hash(value):
    return isinstance(value, str) and _PERCEPTUAL_HASH.fullmatch(value) is not None


def perceptual_hash_distance(left, right):
    if not valid_perceptual_hash(left) or not valid_perceptual_hash(right):
        return None
    return (int(left, 16) ^ int(right, 16)).bit_count()


def perceptual_hash_for_image(source_image):
    """Return a deterministic 64-bit DCT pHash for a trusted Pillow image."""

    grayscale = source_image.convert("L").resize(
        (_HASH_IMAGE_SIZE, _HASH_IMAGE_SIZE),
        resample=Image.Resampling.LANCZOS,
    )
    pixels = grayscale.tobytes()
    coefficients = []
    for vertical_frequency in range(_HASH_COEFFICIENTS):
        vertical_scale = 1 / math.sqrt(2) if vertical_frequency == 0 else 1
        for horizontal_frequency in range(_HASH_COEFFICIENTS):
            horizontal_scale = 1 / math.sqrt(2) if horizontal_frequency == 0 else 1
            value = 0.0
            for y in range(_HASH_IMAGE_SIZE):
                vertical = _COSINES[y][vertical_frequency]
                offset = y * _HASH_IMAGE_SIZE
                for x in range(_HASH_IMAGE_SIZE):
                    value += pixels[offset + x] * _COSINES[x][horizontal_frequency] * vertical
            coefficients.append(round(0.25 * horizontal_scale * vertical_scale * value, 6))
    threshold = median(coefficients[1:])
    bits = 0
    for coefficient in coefficients:
        bits = (bits << 1) | int(coefficient > threshold)
    return f"{bits:016x}"


def find_possible_duplicate(patient, perceptual_hash, *, exclude_document_id=None):
    """Return only an opaque same-patient hint; similarity never mutates or blocks."""

    if not valid_perceptual_hash(perceptual_hash):
        return None
    from .models import Document

    candidates = Document.objects.filter(
        patient=patient,
        deleted_at__isnull=True,
    ).exclude(perceptual_hash="")
    if exclude_document_id is not None:
        candidates = candidates.exclude(pk=exclude_document_id)
    best_id = None
    best_distance = MAX_POSSIBLE_DUPLICATE_DISTANCE + 1
    for document_id, candidate_hash in candidates.order_by("-created_at", "-pk").values_list(
        "pk", "perceptual_hash"
    ):
        distance = perceptual_hash_distance(perceptual_hash, candidate_hash)
        if distance is not None and distance <= MAX_POSSIBLE_DUPLICATE_DISTANCE and distance < best_distance:
            best_id = document_id
            best_distance = distance
    return best_id
