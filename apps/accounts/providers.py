import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import time
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings

from apps.core.trial import synthetic_trial_enabled


class SmsProvider(Protocol):
    def send_otp(self, phone: str, code: str, purpose: str) -> None: ...


class NullSmsProvider:
    def send_otp(self, phone, code, purpose):
        return None


class DevelopmentSmsProvider:
    def send_otp(self, phone: str, code: str, purpose: str) -> None:
        return None


class SmsGatewayUnavailable(RuntimeError):
    pass


class HttpsSmsGatewayProvider:
    """Bounded, signed adapter for a private production SMS gateway."""

    MAX_RESPONSE_BYTES = 4096

    def __init__(
        self,
        url,
        api_key,
        signing_secret,
        template_id,
        allowed_hosts,
        *,
        timeout=10,
        connection_factory=http.client.HTTPSConnection,
        clock=time.time,
        nonce_factory=lambda: secrets.token_urlsafe(18),
    ):
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except (TypeError, ValueError):
            raise SmsGatewayUnavailable("SMS gateway configuration is unavailable") from None
        try:
            ipaddress.ip_address(host)
        except ValueError:
            is_ip_address = False
        else:
            is_ip_address = True
        allowed_values = allowed_hosts if isinstance(allowed_hosts, (list, tuple, set, frozenset)) else ()
        allowed = {
            candidate.lower().rstrip(".")
            for candidate in allowed_values
            if isinstance(candidate, str) and candidate
        }
        if (
            parsed.scheme != "https"
            or not host
            or is_ip_address
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or port not in (None, 443)
            or host not in allowed
            or not parsed.path.startswith("/")
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in (api_key, signing_secret, template_id)
            )
            or type(timeout) is not int
            or not 0 < timeout <= 10
        ):
            raise SmsGatewayUnavailable("SMS gateway configuration is unavailable")
        self._host = host
        self._path = parsed.path
        self._api_key = api_key
        self._signing_secret = signing_secret.encode("utf-8")
        self._template_id = template_id
        self._timeout = timeout
        self._connection_factory = connection_factory
        self._clock = clock
        self._nonce_factory = nonce_factory

    def __repr__(self):
        return "HttpsSmsGatewayProvider(configured=True)"

    def send_otp(self, phone, code, purpose):
        return self._send_otp(phone, code, purpose)

    def send_otp_once(self, phone, code, purpose, *, delivery_id):
        try:
            delivery_id = str(UUID(delivery_id))
        except (TypeError, ValueError, AttributeError):
            raise SmsGatewayUnavailable("SMS delivery is unavailable") from None
        return self._send_otp(phone, code, purpose, delivery_id=delivery_id)

    def _send_otp(self, phone, code, purpose, *, delivery_id=None):
        timestamp = str(int(self._clock()))
        nonce = self._nonce_factory()
        if (
            not isinstance(nonce, str)
            or not 16 <= len(nonce) <= 64
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in nonce)
        ):
            raise SmsGatewayUnavailable("SMS delivery is unavailable")
        payload = {"phone": phone, "code": code, "purpose": purpose, "template_id": self._template_id}
        if delivery_id is not None:
            payload["delivery_id"] = delivery_id
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signed = b"\n".join((timestamp.encode("ascii"), nonce.encode("ascii"), body))
        signature = hmac.new(self._signing_secret, signed, hashlib.sha256).hexdigest()
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-PHR-Nonce": nonce,
            "X-PHR-Signature": f"v1={signature}",
            "X-PHR-Timestamp": timestamp,
        }
        if delivery_id is not None:
            headers["Idempotency-Key"] = delivery_id
        connection = self._connection_factory(self._host, 443, timeout=self._timeout)
        try:
            connection.request(
                "POST",
                self._path,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            payload = response.read(self.MAX_RESPONSE_BYTES + 1)
        except Exception:
            raise SmsGatewayUnavailable("SMS delivery is unavailable") from None
        finally:
            try:
                connection.close()
            except Exception:
                pass
        if response.status not in {200, 202} or len(payload) > self.MAX_RESPONSE_BYTES:
            raise SmsGatewayUnavailable("SMS delivery is unavailable")
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SmsGatewayUnavailable("SMS delivery is unavailable") from None
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise SmsGatewayUnavailable("SMS delivery is unavailable")


def get_sms_provider():
    if synthetic_trial_enabled():
        return NullSmsProvider()
    if settings.DEBUG is True and getattr(settings, "OTP_PROVIDER", None) == "development":
        return DevelopmentSmsProvider()
    if getattr(settings, "OTP_PROVIDER", None) == "https_gateway":
        return HttpsSmsGatewayProvider(
            settings.SMS_GATEWAY_URL,
            settings.SMS_GATEWAY_API_KEY,
            settings.SMS_GATEWAY_SIGNING_SECRET,
            settings.SMS_GATEWAY_TEMPLATE_ID,
            settings.SMS_GATEWAY_ALLOWED_HOSTS,
            timeout=settings.SMS_GATEWAY_TIMEOUT_SECONDS,
        )
    class FailingProvider:
        def send_otp(self, phone, code, purpose):
            raise RuntimeError("OTP provider unavailable")
    return FailingProvider()
