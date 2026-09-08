class FamilyLinkPrivacyMiddleware:
    """Apply redaction before session, CSRF or audit lookups can fail."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        cloud_source = request.path_info.startswith('/cloud-imaging/') or (
            request.path_info.startswith('/records/') and '/cloud-imaging' in request.path_info)
        request.sensitive_post_parameters = '__ALL__' if cloud_source else ("token", "recipient_phone")
        return self.get_response(request)
