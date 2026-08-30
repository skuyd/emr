from importlib.metadata import PackageNotFoundError, version

from apps.processing.preparation import PreparedPageKind
from apps.processing.value_objects import OcrPage, OcrRegion

from .base import OcrContractError


class TextLayerOcrProvider:
    def __init__(self, provider_version=None):
        if provider_version is None:
            try:
                provider_version = f"pdfium-{version('pypdfium2')}"
            except PackageNotFoundError:
                raise OcrContractError() from None
        self.provider_version = provider_version

    def recognize(self, page):
        if page.kind != PreparedPageKind.TEXT_LAYER:
            raise OcrContractError()
        regions = tuple(
            OcrRegion(
                text=span.text,
                polygon=span.polygon,
                confidence=1.0,
                reading_order=index,
            )
            for index, span in enumerate(page.text_spans, start=1)
        )
        return OcrPage(
            page_number=page.page_number,
            width=page.width,
            height=page.height,
            regions=regions,
            provider="pdf-text-layer",
            provider_version=self.provider_version,
            provider_metadata={"source": "embedded-text"},
        )
