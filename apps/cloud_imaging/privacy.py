"""Keep failed access requests diagnosable without serializing private inputs."""
from django.views.debug import ExceptionReporter


class CloudOpenExceptionReporter(ExceptionReporter):
    def get_traceback_data(self):
        data = super().get_traceback_data()
        # Django's standard reporter includes GET, the full request URI and
        # frame values even when sensitive_post_parameters covers every POST
        # value. Omit those complete channels, not just a presumed token key.
        data.update(request_GET_items=[], filtered_POST_items=[], request_FILES_items=[],
                    request_COOKIES_items=[], request_meta={}, settings={},
                    unicode_hint='', template_info=None, postmortem=[],
                    template_does_not_exist=False, exception_notes='',
                    exception_value='cloud_source_request_failed', user_str='[private actor]')
        match = getattr(self.request, 'resolver_match', None)
        route = match.view_name if match and match.namespace == 'cloud_imaging' else 'cloud_imaging:request'
        method = self.request.method if self.request.method in {'GET', 'HEAD', 'POST'} else 'OTHER'
        data['request'] = {'method': method, 'path_info': route}
        data['request_insecure_uri'] = route
        causes = {}
        for frame in data['frames']:
            frame['vars'] = [(name, '[private value]') for name, _ in frame.get('vars', [])]
            cause = frame.get('exc_cause')
            if cause is not None:
                # Both Django report templates render this object as text.
                # Keep distinct same-class causes distinct for their ifchanged
                # blocks, without calling the original exception's str/repr.
                key = id(cause)
                if key not in causes:
                    causes[key] = (f'{type(cause).__name__} [cause {len(causes) + 1}]: '
                                   'cloud_source_request_failed')
                frame['exc_cause'] = causes[key]
            # Django stores the actual __cause__ here, despite the flag name.
            explicit = frame.get('exc_cause_explicit')
            frame['exc_cause_explicit'] = isinstance(explicit, BaseException) or explicit is True
        # The stack, file/line, exception class and stable failure remain. No
        # access URL, Location, QR payload or arbitrary request field is stored.
        from apps.operations.audit import current_audit_request
        state = current_audit_request.get()
        if state:
            data['request_meta'] = {'route_name': state.route_name, 'request_id': str(state.request_id)}
        return data
