"""High entropy, purpose-bound links. Raw values are never database identifiers."""

import hashlib
import re
import secrets

from django.views.decorators.debug import sensitive_variables


@sensitive_variables()
def token_digest(token, purpose):
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        return ""
    return hashlib.sha256(f"phr/{purpose}/v1:{token}".encode("ascii")).hexdigest()


@sensitive_variables()
def create_token(purpose):
    token = secrets.token_urlsafe(32)
    return token, token_digest(token, purpose)
