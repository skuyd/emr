"""Validate a private target locally; never resolve or request its host."""

from dataclasses import dataclass, field
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit

import idna

from django.core.exceptions import ValidationError
from django.views.decorators.debug import sensitive_variables


MAX_URL_LENGTH = 8192
_LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_NUMERIC_HOST = re.compile(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*\Z')
_BROKEN_ESCAPE = re.compile(r'%(?![0-9a-fA-F]{2})')


@dataclass(frozen=True)
class ValidatedURL:
    value: str = field(repr=False)
    site_label: str = field(repr=False)


def _invalid():
    return ValidationError('地址不符合外部访问规则，请重新核对原页。', code='invalid_external_url')


@sensitive_variables()
def validate_url(value):
    if (not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH
            or '\\' in value or _BROKEN_ESCAPE.search(value)
            or any(char.isspace() or unicodedata.category(char) in {'Cc', 'Cf'} for char in value)):
        raise _invalid()
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {'http', 'https'} or not parts.netloc or parts.username is not None or parts.password is not None:
            raise _invalid()
        hostname, port = parts.hostname, parts.port
        if not hostname or '%' in hostname or port == 0 or parts.netloc.endswith(':'):
            raise _invalid()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            # Browsers use UTS 46 without transitional sharp-s/final-sigma
            # mappings. IDNA 2003 can display a different site than navigation.
            host = idna.encode(hostname, uts46=True, std3_rules=True, transitional=False).decode('ascii').lower().rstrip('.')
            labels = host.split('.')
            if (len(host) > 253 or len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels)
                    or _NUMERIC_HOST.fullmatch(host) or labels[-1] in {'local', 'localhost', 'internal', 'home', 'lan'}):
                raise _invalid()
        else:
            if not address.is_global:
                raise _invalid()
            host = f'[{address.compressed}]' if address.version == 6 else address.compressed
        default_port = 443 if parts.scheme.lower() == 'https' else 80
        site = host + (f':{port}' if port is not None and port != default_port else '')
    except (UnicodeError, ValueError):
        raise _invalid() from None
    # Query/fragment and percent escapes are deliberately never rewritten.
    return ValidatedURL(value=value, site_label=site)
