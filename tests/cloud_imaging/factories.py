"""Synthetic source bytes, never a real external target or patient."""

import hashlib
import io
from uuid import uuid4

from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from apps.documents.models import Document, DocumentPage, UploadBatch
from tests.documents.fakes import InMemoryObjectStore
from .test_decoding import png_bytes, qr_image


def stored_document(patient, *, pdf=False, exif=False):
    picture = qr_image()
    width, height = picture.size
    if pdf:
        output = io.BytesIO()
        canvas = Canvas(output, pagesize=(600, 800))
        canvas.drawString(40, 740, 'SYNTHETIC text-layer imaging report')
        canvas.drawImage(ImageReader(picture), 160, 260, width=260, height=260)
        canvas.showPage()
        canvas.save()
        payload, kind, width, height = output.getvalue(), 'application/pdf', 600, 800
    elif exif:
        output = io.BytesIO()
        metadata = picture.getexif()
        metadata[274] = 6
        picture.save(output, format='JPEG', quality=100, exif=metadata)
        payload, kind = output.getvalue(), 'image/jpeg'
    else:
        payload, kind = png_bytes(picture), 'image/png'
    batch = UploadBatch.objects.create(patient=patient, file_count=1, page_count=1, byte_size=len(payload))
    document = Document.objects.create(
        patient=patient, batch=batch, display_filename='synthetic-cloud-source.' + ('pdf' if pdf else 'jpg' if exif else 'png'),
        content_type=kind, byte_size=len(payload), page_count=1, sha256=hashlib.sha256(payload).hexdigest(),
        original_object_key='originals/cloud-synthetic-' + uuid4().hex, status='ORGANIZED',
    )
    page = DocumentPage.objects.create(document=document, page_number=1, width=width, height=height)
    store = InMemoryObjectStore()
    store.objects[document.original_object_key] = payload
    return document, page, store
