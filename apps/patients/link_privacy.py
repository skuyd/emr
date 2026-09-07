class FamilyLinkPrivacyMiddleware:
    """Apply redaction before session, CSRF or audit lookups can fail."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.sensitive_post_parameters = ("token", "recipient_phone")
        return self.get_response(request)
