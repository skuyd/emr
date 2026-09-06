"""Reproduce the bundled regular-weight PDF font from a verified upstream file.

Build-only dependency: fonttools==4.64.0. The application needs only the generated
TrueType file and its retained OFL notice, not fontTools at runtime.
"""

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_SHA256 = "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da"
FONTTOOLS_VERSION = "4.64.0"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Unmodified pinned Noto Sans SC variable TTF")
    args = parser.parse_args()
    source = args.source.resolve()
    if hashlib.sha256(source.read_bytes()).hexdigest() != UPSTREAM_SHA256:
        parser.error("Upstream font hash does not match the reviewed source")
    import fontTools
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont
    if fontTools.__version__ != FONTTOOLS_VERSION:
        parser.error("Use fonttools==" + FONTTOOLS_VERSION + " to reproduce these bytes")
    font = TTFont(source, recalcTimestamp=False)
    instance = instantiateVariableFont(font, {"wght": 400}, inplace=False, optimize=True)
    instance.recalcTimestamp = False
    names = {1: "PHR Sans SC", 2: "Regular", 3: "PHR Sans SC Regular 1.0", 4: "PHR Sans SC Regular",
             6: "PHRSansSC-Regular", 16: "PHR Sans SC", 17: "Regular"}
    for record in list(instance["name"].names):
        if record.nameID in names:
            instance["name"].setName(names[record.nameID], record.nameID, record.platformID, record.platEncID, record.langID)
    target = ROOT / "static/fonts/noto-sans-sc.ttf"
    if source == target.resolve():
        parser.error("Keep the upstream input separate from the generated output")
    instance.save(target)
    payload = target.read_bytes()
    provenance_path = ROOT / "docs/licenses/noto-sans-sc-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    item = next(item for item in provenance["files"] if item["path"] == "static/fonts/noto-sans-sc.ttf")
    item.update(upstream_sha256=UPSTREAM_SHA256, upstream_bytes=source.stat().st_size,
                sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload),
                transformation={"tool": "fonttools", "version": FONTTOOLS_VERSION, "axes": {"wght": 400},
                                "family": "PHR Sans SC", "script": "tools/prepare_pdf_font.py"})
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sha256": item["sha256"], "bytes": item["bytes"], "weight": 400}))


if __name__ == "__main__":
    main()
