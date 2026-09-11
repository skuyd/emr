"""Private original transcriptions must not become exception diagnostics."""
from django.views.debug import ExceptionReporter


class FactExceptionReporter(ExceptionReporter):
    def get_traceback_data(self):
        data = super().get_traceback_data()
        data.update(request_GET_items=[], filtered_POST_items=[], request_FILES_items=[],
                    request_COOKIES_items=[], request_meta={}, settings={}, unicode_hint='',
                    template_info=None, postmortem=[], template_does_not_exist=False,
                    exception_notes='', exception_value='fact_request_failed', user_str='[private actor]')
        match = getattr(self.request, 'resolver_match', None)
        route = match.view_name if match and match.namespace == 'facts' else 'facts:request'
        method = self.request.method if self.request.method in {'GET', 'HEAD', 'POST'} else 'OTHER'
        data['request'] = {'method': method, 'path_info': route}
        data['request_insecure_uri'] = route
        causes = {}
        for frame in data['frames']:
            frame['vars'] = [(name, '[private value]') for name, _ in frame.get('vars', [])]
            cause = frame.get('exc_cause')
            if cause is not None:
                if id(cause) not in causes:
                    causes[id(cause)] = f'{type(cause).__name__} [cause {len(causes) + 1}]: fact_request_failed'
                frame['exc_cause'] = causes[id(cause)]
            explicit = frame.get('exc_cause_explicit')
            frame['exc_cause_explicit'] = isinstance(explicit, BaseException) or explicit is True
        from apps.operations.audit import current_audit_request
        state = current_audit_request.get()
        if state:
            data['request_meta'] = {'route_name': state.route_name, 'request_id': str(state.request_id)}
        return data


class FactPrivacyMiddleware:
    """Run before session, CSRF and authorization can fail with field inputs."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        protected = request.path_info.startswith('/facts/')
        if protected:
            request.sensitive_post_parameters = '__ALL__'
            request.exception_reporter_class = FactExceptionReporter
        response = self.get_response(request)
        if protected:
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Referrer-Policy'] = 'same-origin'
            response['Cross-Origin-Opener-Policy'] = 'same-origin'
        return response
