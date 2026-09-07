from typing import Protocol, runtime_checkable
from dataclasses import replace

from apps.processing.errors import NonRetryableProcessingError, RetryableProcessingError
from apps.processing.preparation import PreparedPage
from apps.processing.value_objects import OcrPage


class OcrContractError(NonRetryableProcessingError):
    def __init__(self):
        super().__init__("invalid_ocr_result")


class OcrProviderUnavailable(NonRetryableProcessingError):
    def __init__(self):
        super().__init__("ocr_provider_unavailable")


class OcrInferenceError(RetryableProcessingError):
    def __init__(self):
        super().__init__("ocr_inference_failed")


@runtime_checkable
class OcrProvider(Protocol):
    def recognize(self, page: PreparedPage) -> OcrPage: ...


def validate_ocr_page(result, prepared_page):
    if (
        not isinstance(result, OcrPage)
        or result.page_number != prepared_page.page_number
        or result.width != prepared_page.width
        or result.height != prepared_page.height
    ):
        raise OcrContractError()
    return result


def recognize_page(provider, prepared_page):
    if not isinstance(prepared_page, PreparedPage) or not callable(getattr(provider, "recognize", None)):
        raise OcrContractError()
    try:
        result = provider.recognize(prepared_page)
    except (OcrContractError, OcrProviderUnavailable, OcrInferenceError):
        raise
    except Exception:
        raise OcrInferenceError() from None
    validated = validate_ocr_page(result, prepared_page)
    return replace(validated, source_transform=prepared_page.source_transform,
                   preparation_metadata=prepared_page.preparation_metadata)
