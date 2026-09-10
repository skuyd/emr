class FamilyLinkPrivacyMiddleware:
    """Apply redaction before session, CSRF or audit lookups can fail."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cloud_source = request.path_info.startswith('/cloud-imaging/') or (
            request.path_info.startswith('/records/') and '/cloud-imaging' in request.path_info)
        request.sensitive_post_parameters = '__ALL__' if cloud_source else ("token", "recipient_phone")
        controlled_open = (request.path_info.startswith('/cloud-imaging/')
                           and request.path_info.endswith(('/visit/', '/open/')))
        if controlled_open:
            from apps.cloud_imaging.privacy import CloudOpenExceptionReporter
            request.exception_reporter_class = CloudOpenExceptionReporter
        response = self.get_response(request)
        if controlled_open:
            # Also protect early CSRF/authentication/method/error responses.
            response['Cache-Control'] = 'private, no-store, max-age=0'
            response['Pragma'] = 'no-cache'
            response['Referrer-Policy'] = 'no-referrer'
            response['Cross-Origin-Opener-Policy'] = 'same-origin'
        return response
