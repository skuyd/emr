# PDF components and release notices

The V1 PDF adapter intentionally uses permissively licensed components:

- `pypdf>=6.16,<7`: BSD-3-Clause, used for structural and security inspection.
- `pypdfium2>=5.13,<6`: Apache-2.0/BSD-3-Clause Python bindings and packaging.
- PDFium: BSD-3-Clause with bundled third-party notices, used through the
  `pypdfium2_raw` binary wheel for rendering and text/coordinate access.

## Third-phase PDF generation dependencies

Source version [v1.2.0](../releases/v1.2.0.md) adds `reportlab>=4.4,<5` for card generation.
The resolved package is recorded in `requirements-prod.lock`; retain its installed
license metadata in delivered images. CI verifies that the image builds with the bundled font license.

Chinese text uses a regular-weight instance of the pinned Noto Sans SC font at
`static/fonts/noto-sans-sc.ttf`. The font is supplied under SIL Open Font License
1.1; its complete copyright and license text is retained in
[noto-sans-sc-ofl.txt](noto-sans-sc-ofl.txt). Upstream source revision, download URLs,
SHA-256 values and file sizes are recorded in
[noto-sans-sc-provenance.json](noto-sans-sc-provenance.json). Images that include the
font must include this license text. The derived font family is named PHR Sans SC.
Generated documents embed the glyphs needed
for their selected content.

The source is the [Google Fonts Noto Sans SC directory](https://github.com/google/fonts/tree/5e35378e6bda803962ee6fd257e444a7d459660d/ofl/notosanssc).
Embedding uses the [ReportLab TrueType font API](https://docs.reportlab.com/reportlab/userguide/ch3_fonts/).
The unmodified upstream font SHA-256 is
`a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`.
The generated artifact hash is recorded in the provenance JSON. Reproduce it using
`fonttools==4.64.0` and `python tools/prepare_pdf_font.py --source <upstream.ttf>`.
The build script pins weight 400 through the
[fontTools instancer](https://fonttools.readthedocs.io/en/latest/varLib/instancer.html),
preserves glyph coverage and license metadata, and renames the derived family.
fontTools is a build-only tool and is not required in the application image.

PyMuPDF is not a project dependency because its open-source distribution is
AGPL and its publisher offers a separate commercial license.

## Release requirement

Production images and offline installers must retain the complete
`pypdfium2-*.dist-info/licenses/` directory installed by the selected wheel,
including its `licenses/LICENSES/` texts and `licenses/data/` notice bundle. Packaging or
image-minification steps must not strip Python distribution license metadata.
The release SBOM must identify the resolved pypdf and pypdfium2 versions.

Before publishing an artifact, run the dependency/license acceptance check in
`tests/documents/test_inspection.py` inside that final artifact. If the selected
wheel changes its notice layout, release is blocked until this record and the
acceptance check are updated against the new upstream package.

Upstream references:

- https://pypi.org/project/pypdf/
- https://pypdfium2-team.github.io/pypdfium2/readme.html#licensing
- https://pymupdf.io/pymupdf
