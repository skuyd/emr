"""Conservative local raster enhancement. Only private derived pixels are changed."""
from dataclasses import dataclass
import math

from PIL import Image

from .geometry import IDENTITY_TRANSFORM, normalized_transform


PREPARATION_VERSION = "document-image-1"
_ANALYSIS_SIDE = 1200


@dataclass(frozen=True)
class EnhancedRaster:
    image: Image.Image
    source_transform: tuple
    metadata: dict


def _small(pixels, cv2):
    height, width = pixels.shape[:2]
    ratio = min(1., _ANALYSIS_SIDE / max(height, width))
    if ratio < 1:
        return cv2.resize(pixels, (max(1, round(width*ratio)), max(1, round(height*ratio))), interpolation=cv2.INTER_AREA)
    return pixels


def _paper_quad(pixels, cv2, np):
    """Require a dominant, enclosed sheet with four supported edges and dark surround."""
    sample = _small(pixels, cv2)
    gray = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    border = np.concatenate((gray[:3].ravel(), gray[-3:].ravel(), gray[:, :3].ravel(), gray[:, -3:].ravel()))
    dark, light = float(np.median(border)), float(np.percentile(gray, 90))
    if dark > 170 or light-dark < 45:
        return None
    threshold = dark + max(22., .22*(light-dark))
    mask = (cv2.GaussianBlur(gray, (5, 5), 0) > threshold).astype(np.uint8)*255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), dtype=np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    if not contours:
        return None
    contour = contours[0]
    area = cv2.contourArea(contour)
    if not .35*width*height < area < .94*width*height:
        return None
    if len(contours) > 1 and cv2.contourArea(contours[1]) > .08*width*height:
        return None
    corners = cv2.approxPolyDP(contour, .018*cv2.arcLength(contour, True), True)
    if len(corners) != 4 or not cv2.isContourConvex(corners):
        return None
    corners = corners[:, 0, :].astype(np.float64)
    if np.any(corners < 4) or np.any(corners[:, 0] > width-5) or np.any(corners[:, 1] > height-5):
        return None
    center = corners.mean(axis=0)
    corners = corners[np.argsort(np.arctan2(corners[:, 1]-center[1], corners[:, 0]-center[0]))]
    corners = np.roll(corners, -int(np.argmin(corners.sum(axis=1))), axis=0)
    for i in range(4):
        first, second = corners[(i-1) % 4]-corners[i], corners[(i+1) % 4]-corners[i]
        length = np.linalg.norm(first)*np.linalg.norm(second)
        if length < .03*width*height or abs(float(np.dot(first, second)/length)) > .7:
            return None
    inside = np.zeros_like(mask)
    cv2.fillConvexPoly(inside, corners.astype(np.int32), 255)
    if np.mean(mask[inside > 0] > 0) < .88:
        return None
    ring = cv2.dilate(inside, np.ones((13, 13), dtype=np.uint8)) > inside
    if not ring.any() or float(np.median(gray[inside > 0]))-float(np.median(gray[ring])) < 35:
        return None
    # Keep a margin outside the detected sheet, including print touching its physical edge.
    corners = center + (corners-center)*1.012
    corners[:, 0] *= pixels.shape[1]/width
    corners[:, 1] *= pixels.shape[0]/height
    if np.any(corners < 0) or np.any(corners[:, 0] >= pixels.shape[1]) or np.any(corners[:, 1] >= pixels.shape[0]):
        return None
    return corners.astype(np.float32)


def _deskew_angle(pixels, cv2, np):
    sample = _small(pixels, cv2)
    gray = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    if min(height, width) < 120:
        return None
    foreground = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    foreground[:max(2, height//40)] = 0
    foreground[-max(2, height//40):] = 0
    foreground[:, :max(2, width//40)] = 0
    foreground[:, -max(2, width//40):] = 0
    coverage = float(np.mean(foreground > 0))
    if not .004 < coverage < .3:
        return None
    joined = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, np.ones((3, max(15, width//28)), dtype=np.uint8))
    contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    for contour in contours:
        (_cx, _cy), (w, h), angle = cv2.minAreaRect(contour)
        if w < h:
            angle -= 90
            w, h = h, w
        if w > width*.18 and 3 < h < height*.09 and w/max(h, 1) > 4 and abs(angle) < 12:
            angles.append(angle)
    if len(angles) < 3:
        return None
    angle = float(np.median(angles))
    # Subdegree estimates do not justify resampling already readable small glyphs.
    if not 1. <= abs(angle) <= 10 or np.mean(np.abs(np.array(angles)-angle) < .8) < .75:
        return None
    return angle


def _photometric(pixels, rectified, cv2, np):
    sample = _small(pixels, cv2)
    gray = cv2.cvtColor(sample, cv2.COLOR_RGB2GRAY)
    neutral = sample.max(axis=2).astype(np.int16)-sample.min(axis=2)
    if not rectified and (np.mean(neutral < 45) < .85 or np.percentile(gray, 60) < 135):
        return pixels, []
    if not rectified:
        dark = (cv2.GaussianBlur(gray, (5, 5), 0) < 100).astype(np.uint8)*255
        regions, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if any(cv2.contourArea(region) > .05*gray.size for region in regions):
            # A broad dark panel may be screen UI or a photo, not sheet illumination.
            return pixels, []
    # Closing removes thin ink from the illumination estimate; full-size float planes are avoided.
    kernel = max(15, min(gray.shape)//25) | 1
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel)))
    background = cv2.GaussianBlur(background, (0, 0), max(3., kernel/3))
    inset = max(1, min(gray.shape)//30)
    interior = background[inset:-inset, inset:-inset]
    low, high = np.percentile(interior, [10, 90])
    steps = []
    if high-low >= 18 or high < 225:
        background = cv2.resize(background, (pixels.shape[1], pixels.shape[0]), interpolation=cv2.INTER_LINEAR)
        background = np.maximum(background, 60)
        channels = [cv2.divide(pixels[:, :, i], background, scale=255) for i in range(3)]
        pixels = cv2.merge(channels)
        steps.append("illumination_normalized")
    # Faint, neutral text benefits from a bounded contrast stretch; never binarize or erase colour.
    gray = cv2.cvtColor(_small(pixels, cv2), cv2.COLOR_RGB2GRAY)
    ink = gray[(gray < 225) & (gray > 20)]
    if len(ink) > gray.size*.008 and float(np.percentile(ink, 20)) > 100:
        black = min(100, int(np.percentile(ink, 10))//2)
        table = np.clip((np.arange(256, dtype=np.float32)-black)*255/(255-black), 0, 255).astype(np.uint8)
        pixels = cv2.LUT(pixels, table)
        steps.append("contrast_enhanced")
    return pixels, steps


def enhance_raster(image, *, enabled=True):
    """Return original pixels on uncertainty/failure; source geometry is never discarded."""
    metadata = {"version": PREPARATION_VERSION, "steps": [], "warnings": [], "source_size": list(image.size)}
    if not enabled:
        metadata["warnings"].append("enhancement_disabled")
        return EnhancedRaster(image, IDENTITY_TRANSFORM, metadata)
    if min(image.size) < 120:
        metadata["warnings"].append("enhancement_image_too_small")
        return EnhancedRaster(image, IDENTITY_TRANSFORM, metadata)
    try:
        import cv2
        import numpy as np

        pixels = np.asarray(image.convert("RGB"))
        height, width = pixels.shape[:2]
        forward = np.eye(3, dtype=np.float64)
        corners = _paper_quad(pixels, cv2, np)
        if corners is not None:
            target_width = max(1, math.ceil(max(np.linalg.norm(corners[1]-corners[0]), np.linalg.norm(corners[2]-corners[3]))))
            target_height = max(1, math.ceil(max(np.linalg.norm(corners[3]-corners[0]), np.linalg.norm(corners[2]-corners[1]))))
            forward = cv2.getPerspectiveTransform(corners, np.float32([[0, 0], [target_width-1, 0], [target_width-1, target_height-1], [0, target_height-1]]))
            pixels = cv2.warpPerspective(pixels, forward, (target_width, target_height), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
            metadata["steps"].append("paper_rectified")
        else:
            metadata["warnings"].append("paper_edges_unreliable")
        angle = _deskew_angle(pixels, cv2, np)
        if angle is not None:
            h, w = pixels.shape[:2]
            rotation = cv2.getRotationMatrix2D((w/2, h/2), angle, 1.)
            cosine, sine = abs(rotation[0, 0]), abs(rotation[0, 1])
            target_width, target_height = math.ceil(w*cosine+h*sine), math.ceil(h*cosine+w*sine)
            rotation[0, 2] += (target_width-w)/2
            rotation[1, 2] += (target_height-h)/2
            if target_width*target_height > width*height:
                scale = math.sqrt(width*height/(target_width*target_height))
                bounded_width, bounded_height = max(1, math.floor(target_width*scale)), max(1, math.floor(target_height*scale))
                rotation[0] *= bounded_width/target_width
                rotation[1] *= bounded_height/target_height
                target_width, target_height = bounded_width, bounded_height
                metadata["steps"].append("enhancement_downscaled")
            pixels = cv2.warpAffine(pixels, rotation, (target_width, target_height), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))
            forward = np.vstack((rotation, [0., 0., 1.])) @ forward
            metadata["steps"].append("deskewed")
            metadata["deskew_degrees"] = round(angle, 4)
        # Rotation expands the canvas. Respect the caller's already-enforced pixel budget.
        h, w = pixels.shape[:2]
        if h*w > width*height:
            scale = math.sqrt(width*height/(w*h))
            target = (max(1, math.floor(w*scale)), max(1, math.floor(h*scale)))
            pixels = cv2.resize(pixels, target, interpolation=cv2.INTER_AREA)
            forward = np.diag([target[0]/w, target[1]/h, 1.]) @ forward
            metadata["steps"].append("enhancement_downscaled")
        pixels, steps = _photometric(pixels, corners is not None, cv2, np)
        metadata["steps"].extend(steps)
        h, w = pixels.shape[:2]
        inverse = np.diag([1/width, 1/height, 1.]) @ np.linalg.inv(forward) @ np.diag([w, h, 1.])
        transform = normalized_transform(inverse.tolist())
        metadata["output_size"] = [w, h]
        metadata["source_transform"] = [list(row) for row in transform]
        return EnhancedRaster(Image.fromarray(pixels), transform, metadata)
    except Exception:
        # No image, path or exception text enters diagnostics; the unmodified raster remains usable.
        metadata = {"version": PREPARATION_VERSION, "steps": [], "warnings": ["enhancement_failed"],
                    "source_size": list(image.size)}
        return EnhancedRaster(image, IDENTITY_TRANSFORM, metadata)
