"""Normalized OCR-layout to oriented-original geometry, independent of image libraries."""
import math
from numbers import Real

from .value_objects import InvalidRegion, normalized_polygon


IDENTITY_TRANSFORM = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


def normalized_transform(value):
    if value is None:
        return None
    try:
        rows = tuple(tuple(row) for row in value)
        if len(rows) != 3 or any(len(row) != 3 for row in rows):
            raise ValueError
        if any(isinstance(x, bool) or not isinstance(x, Real) or not math.isfinite(float(x)) for row in rows for x in row):
            raise ValueError
        a, b, c = rows
        determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
        if abs(determinant) < 1e-12:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Source transform must be a finite invertible 3 by 3 matrix") from None
    return tuple(tuple(float(x) for x in row) for row in rows)


def source_polygon(transform, polygon):
    """Never clamp invented/out-of-page geometry into a plausible source highlight."""
    if transform is None or polygon is None:
        return None
    try:
        matrix = normalized_transform(transform)
        mapped = []
        for x, y in normalized_polygon(polygon):
            w = matrix[2][0]*x + matrix[2][1]*y + matrix[2][2]
            if abs(w) < 1e-9:
                return None
            point = tuple((row[0]*x + row[1]*y + row[2])/w for row in matrix[:2])
            if any(not math.isfinite(v) or v < -1e-6 or v > 1+1e-6 for v in point):
                return None
            mapped.append(tuple(max(0., min(1., v)) for v in point))
        return normalized_polygon(mapped)
    except (InvalidRegion, ValueError, TypeError, OverflowError):
        return None
