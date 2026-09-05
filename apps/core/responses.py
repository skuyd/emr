from .csp import content_security_policy


def protect_sensitive_html(response, *, embeddable=False):
    frame_ancestors = "'self'" if embeddable else "'none'"
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    # Same-origin forms need an origin for Django's CSRF check. Cross-origin
    # navigations still receive no sensitive path or query information.
    response["Referrer-Policy"] = "same-origin"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Content-Security-Policy"] = content_security_policy(frame_ancestors=frame_ancestors)
    return response
