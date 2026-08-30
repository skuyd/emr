import hashlib
import io
import json

from PIL import Image

from apps.processing.ocr.fake import FixtureOcrProvider
from apps.processing.value_objects import OcrPage, OcrRegion
from tools.sample_dictionary.discover import discover_samples
from tools.sample_dictionary.extract import extract_from_samples
from tools.sample_dictionary.report import render_candidate_json, write_candidate_report
from apps.labs.candidates import LabCandidate


def test_discovery_is_recursive_supported_only_deduplicated_and_path_redacted(tmp_path):
    nested = tmp_path / "private-person-name"
    nested.mkdir()
    supported = {
        tmp_path / "sensitive-report.pdf": b"pdf-fixture",
        nested / "private-photo.JPG": b"jpeg-fixture",
        nested / "scan.png": b"png-fixture",
        nested / "duplicate.png": b"png-fixture",
    }
    for path, payload in supported.items():
        path.write_bytes(payload)
    (nested / "notes.txt").write_text("not a sample", encoding="utf-8")

    discovered = discover_samples(tmp_path)

    assert len(discovered) == 3
    assert {sample.extension for sample in discovered} == {"pdf", "jpg", "png"}
    assert {sample.source_hash for sample in discovered} == {
        hashlib.sha256(payload).hexdigest() for payload in set(supported.values())
    }
    serialized = repr(discovered) + json.dumps([sample.public_record() for sample in discovered])
    for private_part in ("private-person-name", "sensitive-report", "private-photo", str(tmp_path)):
        assert private_part not in serialized


def test_discovery_order_is_hash_deterministic_not_filename_order(tmp_path):
    for name, payload in (("z.pdf", b"first"), ("a.png", b"second"), ("m.jpg", b"third")):
        (tmp_path / name).write_bytes(payload)

    first = discover_samples(tmp_path)
    second = discover_samples(tmp_path)

    assert [sample.source_hash for sample in first] == sorted(sample.source_hash for sample in first)
    assert first == second


def test_candidate_report_is_byte_deterministic_and_contains_hash_aliases_not_paths(tmp_path):
    candidate = LabCandidate(
        raw_name="白细胞计数",
        normalized_name="白细胞计数",
        source_file_hash="a" * 64,
        page=1,
        region=((0.1, 0.1), (0.4, 0.1), (0.4, 0.2), (0.1, 0.2)),
        context_hash="b" * 64,
    )
    expected = render_candidate_json([candidate])
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    first_hash = write_candidate_report(first_path, [candidate])
    second_hash = write_candidate_report(second_path, [candidate])

    assert first_path.read_bytes() == second_path.read_bytes() == expected
    assert first_hash == second_hash == hashlib.sha256(expected).hexdigest()
    assert b"source_file_hash" in expected
    assert str(tmp_path).encode() not in expected


def test_extraction_cache_makes_repeated_run_byte_deterministic_without_reinvoking_provider(tmp_path):
    samples = tmp_path / "samples"
    samples.mkdir()
    output = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(output, format="PNG")
    private_name = samples / "private-person-report.png"
    private_name.write_bytes(output.getvalue())
    polygon_name = ((0.1, 0.2), (0.4, 0.2), (0.4, 0.3), (0.1, 0.3))
    polygon_value = ((0.6, 0.2), (0.8, 0.2), (0.8, 0.3), (0.6, 0.3))
    page = OcrPage(
        1,
        100,
        100,
        (
            OcrRegion("血红蛋白", polygon_name, 0.99, 1),
            OcrRegion("120", polygon_value, 0.98, 2),
        ),
        "fixture",
        "1.0",
    )
    cache = tmp_path / "cache"
    first_samples, first_candidates = extract_from_samples(
        samples,
        cache,
        raster_provider=FixtureOcrProvider((page,)),
    )

    class MustNotRun:
        def recognize(self, _page):
            raise AssertionError("cache was not used")

    second_samples, second_candidates = extract_from_samples(samples, cache, raster_provider=MustNotRun())

    assert render_candidate_json(first_candidates) == render_candidate_json(second_candidates)
    assert [sample.source_hash for sample in first_samples] == [sample.source_hash for sample in second_samples]
    assert [candidate.normalized_name for candidate in first_candidates] == ["血红蛋白"]
    cache_names = [path.name for path in cache.iterdir()]
    assert cache_names == [f"{first_samples[0].source_hash}.json"]
    assert "private-person" not in cache_names[0]
