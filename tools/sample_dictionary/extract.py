import argparse
import json
import os
from pathlib import Path
import tempfile

from apps.labs.candidates import extract_lab_candidates
from apps.processing.ocr.base import recognize_page
from apps.processing.ocr.paddle import PaddleOcrProvider
from apps.processing.ocr.text_layer import TextLayerOcrProvider
from apps.processing.preparation import PreparedPageKind, prepare_document
from apps.processing.value_objects import OcrPage, OcrRegion

from .discover import discover_samples
from .report import write_candidate_report


_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "heic": "image/heic",
}
CACHE_SCHEMA_VERSION = "1.0"


def _page_record(page):
    return {
        "height": page.height,
        "page_number": page.page_number,
        "provider": page.provider,
        "provider_metadata": dict(page.provider_metadata),
        "provider_version": page.provider_version,
        "regions": [
            {
                "confidence": region.confidence,
                "polygon": [[x, y] for x, y in region.polygon],
                "reading_order": region.reading_order,
                "text": region.text,
            }
            for region in page.regions
        ],
        "width": page.width,
    }


def _page_from_record(value):
    return OcrPage(
        page_number=value["page_number"],
        width=value["width"],
        height=value["height"],
        provider=value["provider"],
        provider_version=value["provider_version"],
        provider_metadata=value.get("provider_metadata", {}),
        regions=tuple(
            OcrRegion(
                text=region["text"],
                polygon=region["polygon"],
                confidence=region["confidence"],
                reading_order=region["reading_order"],
            )
            for region in value["regions"]
        ),
    )


def _write_cache(path, source_hash, pages):
    payload = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "pages": [_page_record(page) for page in pages],
        "source_file_hash": source_hash,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ocr-cache-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _load_cache(path, source_hash):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("cache_schema_version") != CACHE_SCHEMA_VERSION or value.get("source_file_hash") != source_hash:
            return None
        return tuple(_page_from_record(page) for page in value["pages"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def ocr_sample(sample, cache_root, raster_provider=None, *, force_raster=False):
    cache_suffix = "-raster" if force_raster else ""
    cache_path = Path(cache_root) / f"{sample.source_hash}{cache_suffix}.json"
    cached = _load_cache(cache_path, sample.source_hash)
    if cached is not None:
        return cached
    raster_provider = raster_provider or PaddleOcrProvider()
    text_provider = TextLayerOcrProvider()
    try:
        with sample.path.open("rb") as source, prepare_document(
            source,
            _CONTENT_TYPES[sample.extension],
            force_raster=force_raster,
        ) as prepared:
            pages = tuple(
                recognize_page(text_provider if page.kind == PreparedPageKind.TEXT_LAYER else raster_provider, page)
                for page in prepared.pages
            )
    except OSError:
        raise RuntimeError("sample_unreadable") from None
    _write_cache(cache_path, sample.source_hash, pages)
    return pages


def extract_from_samples(sample_root, cache_root, *, raster_provider=None, force_raster_hashes=()):
    candidates = []
    samples = discover_samples(sample_root)
    force_raster_hashes = frozenset(force_raster_hashes)
    discovered_hashes = {sample.source_hash for sample in samples}
    if not force_raster_hashes <= discovered_hashes:
        raise ValueError("force_raster_source_unavailable")
    raster_provider = raster_provider or PaddleOcrProvider()
    for sample in samples:
        pages = ocr_sample(
            sample,
            cache_root,
            raster_provider=raster_provider,
            force_raster=sample.source_hash in force_raster_hashes,
        )
        candidates.extend(extract_lab_candidates(pages, sample.source_hash))
    return samples, tuple(sorted(candidates, key=lambda candidate: candidate._sort_key))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a redacted local laboratory-indicator candidate report")
    parser.add_argument("--samples", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ocr-cache", default=".runtime/sample-ocr")
    parser.add_argument("--force-raster-source-hash", action="append", default=[])
    arguments = parser.parse_args(argv)
    samples, candidates = extract_from_samples(
        arguments.samples,
        arguments.ocr_cache,
        force_raster_hashes=arguments.force_raster_source_hash,
    )
    report_hash = write_candidate_report(arguments.output, candidates)
    print(f"sources={len(samples)} candidates={len(candidates)} report_sha256={report_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
