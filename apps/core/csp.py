from django.conf import settings


def _push_sources():
    if not getattr(settings, "WEBPUSH_ENABLED", False):
        return ()
    sources = []
    for host in getattr(settings, "WEBPUSH_ALLOWED_ENDPOINT_HOSTS", ()):
        if not isinstance(host, str) or not host:
            continue
        if host.startswith("."):
            sources.append(f"https://*{host}")
        else:
            sources.append(f"https://{host}")
    return tuple(sorted(set(sources)))


def content_security_policy(*, frame_ancestors="'none'"):
    connect_sources = " ".join(("'self'", *_push_sources()))
    return (
        "default-src 'self'; "
        "base-uri 'none'; "
        f"connect-src {connect_sources}; "
        f"frame-ancestors {frame_ancestors}; "
        "frame-src 'self'; "
        "form-action 'self'; "
        "img-src 'self' data: blob:; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "worker-src 'self'"
    )
