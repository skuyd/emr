# PDF components and release notices

The V1 PDF adapter intentionally uses permissively licensed components:

- `pypdf>=6.16,<7`: BSD-3-Clause, used for structural and security inspection.
- `pypdfium2>=5.13,<6`: Apache-2.0/BSD-3-Clause Python bindings and packaging.
- PDFium: BSD-3-Clause with bundled third-party notices, used through the
  `pypdfium2_raw` binary wheel for rendering and text/coordinate access.

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
