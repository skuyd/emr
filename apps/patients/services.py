import hashlib
import hmac
import ipaddress
import unicodedata

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from apps.accounts.models import ConsentRecord

from .models import Patient
from .policies import ConsentPolicyConflict, REQUIRED_CONSENT_TYPES, consent_policies


MAX_DISPLAY_NAME_CODEPOINTS = 80


class MissingRequiredConsent(ValueError):
    pass


MissingConsent = MissingRequiredConsent


def normalize_display_name(value):
    if not isinstance(value, str):
        raise ValueError("Invalid display name")
    normalized_input = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized_input):
        raise ValueError("Invalid display name")
    normalized = normalized_input.strip()
    if not normalized:
        raise ValueError("Invalid display name")
    if len(normalized) > MAX_DISPLAY_NAME_CODEPOINTS:
        raise ValueError("Invalid display name")
    visible_count = sum(not unicodedata.category(character).startswith("M") for character in normalized)
    if not 1 <= visible_count <= 20:
        raise ValueError("Invalid display name")
    return normalized


def _request_evidence_hash(domain, value):
    secret = settings.ACCOUNTS_CRYPTO_SECRET.encode("utf-8")
    message = f"family-phr/consent-evidence/{domain}/v1:{value}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _hash_request_evidence(request_evidence):
    try:
        canonical_ip = ipaddress.ip_address(request_evidence.get("ip", "")).compressed
    except ValueError as error:
        raise ValueError("Invalid request evidence") from error
    user_agent = request_evidence.get("user_agent", "")
    if not isinstance(user_agent, str):
        raise ValueError("Invalid request evidence")
    return _request_evidence_hash("ip", canonical_ip), _request_evidence_hash("user-agent", user_agent)


def _policy_definitions():
    policies = consent_policies()
    return {consent_type: policies[consent_type] for consent_type in REQUIRED_CONSENT_TYPES}


def _missing_current_consents(account, policies):
    active_records = ConsentRecord.objects.filter(account=account, withdrawn_at__isnull=True)
    records_by_type_version = {}
    for record in active_records:
        records_by_type_version.setdefault((record.consent_type, record.policy_version), []).append(record)
    missing = []
    for consent_type, policy in policies.items():
        records = records_by_type_version.get((consent_type, policy["version"]), [])
        if any(record.policy_digest != policy["digest"] for record in records):
            raise ConsentPolicyConflict("Consent policy configuration conflicts with active records")
        if not records:
            missing.append(consent_type)
    return missing


def missing_current_consents(account):
    return _missing_current_consents(account, _policy_definitions())


def account_needs_onboarding(account):
    policies = _policy_definitions()
    try:
        account.patient
    except Patient.DoesNotExist:
        return True
    return bool(_missing_current_consents(account, policies))


def create_patient_space(account, display_name, confirmations, request_evidence):
    policies = _policy_definitions()

    with transaction.atomic():
        locked_account = get_user_model().objects.select_for_update().get(pk=account.pk)
        patient = Patient.objects.filter(account=locked_account).first()
        missing = _missing_current_consents(locked_account, policies)
        if patient is not None and not missing:
            return patient
        if any(confirmations.get(consent_type) is not True for consent_type in missing):
            raise MissingRequiredConsent("Missing required consent")
        if patient is None:
            patient = Patient.objects.create(
                account=locked_account,
                display_name=normalize_display_name(display_name),
            )
        request_ip_hash, user_agent_hash = _hash_request_evidence(request_evidence)
        for consent_type in missing:
            policy = policies[consent_type]
            ConsentRecord.objects.create(
                account=locked_account,
                consent_type=consent_type,
                policy_version=policy["version"],
                policy_digest=policy["digest"],
                request_ip_hash=request_ip_hash,
                user_agent_hash=user_agent_hash,
            )
    return patient
