import re


_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ProcessingError(RuntimeError):
    """Base class whose persisted value is a stable, non-sensitive code."""

    def __init__(self, code):
        if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("Processing error codes must be stable lowercase identifiers")
        self.code = code
        super().__init__(code)


class RetryableProcessingError(ProcessingError):
    pass


class NonRetryableProcessingError(ProcessingError):
    pass


class ProcessingLeaseLost(RuntimeError):
    pass


class ProcessingContractError(NonRetryableProcessingError):
    def __init__(self, detail="Processing pipeline contract violation"):
        self.detail = detail
        super().__init__("invalid_processing_contract")
