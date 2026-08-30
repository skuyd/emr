def protect_sensitive_html(response, *, embeddable=False):
    frame_ancestors = "'self'" if embeddable else "'none'"
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["X-Frame-Options"] = "SAMEORIGIN"
    response["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
        f"frame-ancestors {frame_ancestors}; base-uri 'none'; form-action 'self'"
    )
    return response
