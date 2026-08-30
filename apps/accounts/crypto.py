import base64
import hashlib
import hmac
import ipaddress

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


class InvalidCiphertext(ValueError):
    pass


_VERSION = "v1"


def _secret_bytes():
    secret = settings.ACCOUNTS_CRYPTO_SECRET
    if not isinstance(secret, str) or not secret:
        raise ValueError("Account cryptography secret is required.")
    return secret.encode("utf-8")


def _derive_key(purpose):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"family-phr/accounts/crypto/v1",
        info=purpose.encode("ascii"),
    ).derive(_secret_bytes())


def _identifier_hash(value, domain):
    if not isinstance(value, str) or not value:
        raise ValueError("Identifier is required.")
    return hmac.new(
        _derive_key(f"identifier:{domain}"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hash_phone(phone):
    return _identifier_hash(phone, "phone")


def hash_ip(ip_address):
    try:
        canonical_ip = ipaddress.ip_address(ip_address).compressed
    except ValueError as exc:
        raise ValueError("A valid IP address is required.") from exc
    return _identifier_hash(canonical_ip, "ip")


def encrypt_phone(phone):
    token = Fernet(base64.urlsafe_b64encode(_derive_key("phone-encryption"))).encrypt(
        phone.encode("utf-8")
    )
    return f"{_VERSION}:{token.decode('ascii')}"


def decrypt_phone(ciphertext):
    if not isinstance(ciphertext, str) or not ciphertext.startswith(f"{_VERSION}:"):
        raise InvalidCiphertext("Invalid encrypted phone value.")
    try:
        token = ciphertext.split(":", 1)[1].encode("ascii")
        return Fernet(base64.urlsafe_b64encode(_derive_key("phone-encryption"))).decrypt(
            token
        ).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise InvalidCiphertext("Invalid encrypted phone value.") from exc
