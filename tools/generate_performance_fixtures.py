from __future__ import annotations

import argparse
import json
from pathlib import Path

from pypdf import PdfWriter


CONFIRMATION = "SYNTHETIC-PERFORMANCE-FIXTURES"
FILES_PER_BATCH = 20
PAGES_PER_FILE = 3
DEFAULT_BATCHES = 10


def _outside_repository(value):
    destination = Path(value).expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    try:
        destination.relative_to(root)
    except ValueError:
        return destination
    raise ValueError("fixture output must be outside the repository")


def generate(destination, *, batches=DEFAULT_BATCHES):
    destination = _outside_repository(destination)
    if destination.exists():
        raise FileExistsError("fixture output already exists and will not be overwritten")
    if not 1 <= batches <= DEFAULT_BATCHES:
        raise ValueError(f"batches must be between 1 and {DEFAULT_BATCHES}")
    destination.mkdir(parents=False)
    manifest_batches = []
    fixture_number = 0
    for batch_number in range(1, batches + 1):
        files = []
        for file_number in range(1, FILES_PER_BATCH + 1):
            fixture_number += 1
            filename = f"synthetic-b{batch_number:02d}-f{file_number:02d}.pdf"
            path = destination / filename
            writer = PdfWriter()
            for _ in range(PAGES_PER_FILE):
                writer.add_blank_page(width=612, height=792)
            writer.add_metadata(
                {
                    "/Title": "Synthetic performance fixture",
                    "/SyntheticFixtureId": f"PHR-V1-{fixture_number:04d}",
                }
            )
            with path.open("xb") as target:
                writer.write(target)
            files.append(
                {
                    "name": filename,
                    "path": str(path),
                    "pages": PAGES_PER_FILE,
                }
            )
        manifest_batches.append({"total_pages": 60, "files": files})
    manifest = {
        "schema_version": 1,
        "synthetic_only": True,
        "batches": manifest_batches,
    }
    manifest_path = destination / "fixture-manifest.json"
    with manifest_path.open("x", encoding="utf-8", newline="\n") as target:
        json.dump(manifest, target, ensure_ascii=True, indent=2, sort_keys=True)
        target.write("\n")
    return manifest_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate non-medical three-page k6 PDF fixtures")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    if args.confirm != CONFIRMATION:
        parser.error(f"--confirm must be exactly {CONFIRMATION}")
    try:
        manifest = generate(args.output_dir)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Created 200 synthetic PDF fixtures and manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
