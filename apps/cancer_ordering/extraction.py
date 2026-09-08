"""Collect a complete source scope under an actual processing lease."""

import logging

from django.db import connection, transaction

from apps.facts.readmodels import digest
from apps.processing.errors import ProcessingContractError, ProcessingLeaseLost
from apps.processing.models import ParsingVersion

from .services import collect_scope
from .sources import SOURCE_VERSION, SourceContext, SourceScope


def collect_processing_version(context, version):
    if not connection.in_atomic_block:
        raise ProcessingContractError('Candidate collection requires the publication transaction')
    # The processing context locks Patient, batches, Document and the actual run,
    # in that order, and verifies the lease and initiating membership. In the
    # pipeline these locks are already held by its initial assert_current().
    run = context.assert_current()
    current = ParsingVersion.objects.select_related('document__patient').filter(pk=version.pk).first()
    if (current is None or current.processing_run_id != run.pk or current.document_id != context.document_id
            or current.status != 'READY' or current.active):
        raise ProcessingContractError('Candidate source does not belong to this complete unpublished run')
    patient = current.document.patient
    try:
        # A capture/database failure cannot leave a partial receipt or make the
        # outer archive transaction unusable. collect_scope has its own nested
        # savepoint for candidate extraction failures.
        with transaction.atomic():
            scopes = SourceContext().scopes(patient, version=current.pk)
            return tuple(collect_scope(scope, patient=patient, author=None) for scope in scopes)
    except ProcessingLeaseLost:
        raise
    except Exception:
        logging.getLogger(__name__).warning('Cancer candidate source capture failed; original retained',
                                            extra={'error_code': 'candidate_source_capture_failed'})
        # This deliberately incomplete identity is an attempt record, never a
        # fabricated complete snapshot. A later explicit WRITE captures the
        # actual input and appends a new result under the same version key.
        key = 'version:' + str(current.pk)
        snapshot = {'contract': SOURCE_VERSION, 'key': key,
                    'document': SourceContext.document_input(current.document),
                    'capture': 'FAILED', 'reason': 'candidate_source_capture_failed'}
        scope = SourceScope(key, current.document, current, None, snapshot, digest(snapshot), (),
                            False, 'candidate_source_capture_failed')
        return (collect_scope(scope, patient=patient, author=None),)
