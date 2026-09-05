"""Process-wide serialization for PDFium, including native resource cleanup.

Callers must hold this reentrant lock from construction through explicit close
of the document and its pages, bitmaps, and text pages. No native handle or
bitmap-backed PIL image may escape the protected lifetime for later GC.
"""

from threading import RLock


PDFIUM_LOCK = RLock()
