import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


class InvalidSubscriptionCiphertext(ValueError):
    pass


_VERSION = "v1"


def _secret_bytes():
    secret = settings.NOTIFICATIONS_CRYPTO_SECRET
    if not isinstance(secret, str) or not secret:
        raise ValueError("Notification cryptography secret is required")
    return secret.encode("utf-8")


def _derive_key(purpose):
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"family-phr/notifications/crypto/v1",
        info=purpose.encode("ascii"),
    ).derive(_secret_bytes())


def endpoint_hash(endpoint):
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("Push endpoint is required")
    return hmac.new(
        _derive_key("endpoint-hash"),
        endpoint.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def encrypt_subscription_value(value, purpose):
    if not isinstance(value, str) or not value:
        raise ValueError("Push subscription value is required")
    token = Fernet(base64.urlsafe_b64encode(_derive_key(f"encrypt:{purpose}"))).encrypt(
        value.encode("utf-8")
    )
    return f"{_VERSION}:{token.decode('ascii')}"


def decrypt_subscription_value(ciphertext, purpose):
    if not isinstance(ciphertext, str) or not ciphertext.startswith(f"{_VERSION}:"):
        raise InvalidSubscriptionCiphertext("Invalid encrypted push subscription value")
    try:
        token = ciphertext.split(":", 1)[1].encode("ascii")
        return Fernet(base64.urlsafe_b64encode(_derive_key(f"encrypt:{purpose}"))).decrypt(
            token
        ).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise InvalidSubscriptionCiphertext("Invalid encrypted push subscription value") from exc

