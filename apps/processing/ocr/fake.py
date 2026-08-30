from .base import OcrContractError, validate_ocr_page
from apps.processing.value_objects import OcrPage


class FixtureOcrProvider:
    """Deterministic complete-page fixtures for tests and local demonstrations."""

    def __init__(self, pages, *, validate_fixture=True):
        if isinstance(pages, dict):
            self._pages = dict(pages)
        else:
            self._pages = {page.page_number: page for page in pages}
        if validate_fixture and any(not isinstance(page, OcrPage) for page in self._pages.values()):
            raise OcrContractError()
        self._validate_fixture = validate_fixture

    def recognize(self, page):
        try:
            result = self._pages[page.page_number]
        except KeyError:
            raise OcrContractError() from None
        return validate_ocr_page(result, page) if self._validate_fixture else result
