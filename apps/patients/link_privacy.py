class FamilyLinkPrivacyMiddleware:
    """Apply redaction before session, CSRF or audit lookups can fail."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        parts = request.path_info.strip('/').split('/')
        share_selection = len(parts) == 3 and parts[0] == 'patients' and parts[2] == 'shares'
        selected_output = request.path_info.startswith('/visit/') or share_selection
        shared_cloud = request.path_info.startswith('/shared/') and '/cloud-imaging/' in request.path_info
        cloud_source = shared_cloud or request.path_info.startswith('/cloud-imaging/') or (
            request.path_info.startswith('/records/') and '/cloud-imaging' in request.path_info)
        request.sensitive_post_parameters = '__ALL__' if cloud_source or selected_output else ("token", "recipient_phone")
        controlled_open = ((shared_cloud or request.path_info.startswith('/cloud-imaging/'))
                           and request.path_info.endswith(('/visit/', '/open/')))
        if controlled_open:
            from apps.cloud_imaging.privacy import CloudOpenExceptionReporter
            request.exception_reporter_class = CloudOpenExceptionReporter
        elif selected_output:
            from apps.cloud_imaging.privacy import SelectedOutputExceptionReporter
            request.exception_reporter_class = SelectedOutputExceptionReporter
        response = self.get_response(request)
        if controlled_open or selected_output:
            # Also protect early CSRF/authentication/method/error responses.
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Referrer-Policy'] = 'no-referrer' if controlled_open else 'same-origin'
            response['Cross-Origin-Opener-Policy'] = 'same-origin'
        return response
