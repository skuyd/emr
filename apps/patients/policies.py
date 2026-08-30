import hashlib
import re

from django.conf import settings


REQUIRED_CONSENT_TYPES = ("privacy", "sensitive_data", "upload_authority")
POLICY_LABELS = {
    "privacy": "隐私政策",
    "sensitive_data": "敏感个人信息处理规则",
    "upload_authority": "上传管理授权",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ConsentPolicyConflict(ValueError):
    pass


def consent_policies():
    policies = settings.CONSENT_POLICIES
    if policy_configuration_errors(policies):
        raise ConsentPolicyConflict("Consent policy configuration is unavailable")
    return policies


def policy_items(consent_types=None):
    policies = consent_policies()
    return [
        {"consent_type": consent_type, "label": POLICY_LABELS[consent_type], **policies[consent_type]}
        for consent_type in (consent_types or REQUIRED_CONSENT_TYPES)
    ]


def policy_configuration_errors(policies=None):
    policies = settings.CONSENT_POLICIES if policies is None else policies
    if not isinstance(policies, dict) or set(policies) != set(REQUIRED_CONSENT_TYPES):
        return ["required consent policy keys are missing or unexpected"]
    errors = []
    for consent_type in REQUIRED_CONSENT_TYPES:
        policy = policies[consent_type]
        if not isinstance(policy, dict) or set(policy) != {"version", "digest", "content"}:
            errors.append(f"{consent_type} policy must define version, digest, and content")
            continue
        version = policy["version"]
        digest = policy["digest"]
        content = policy["content"]
        if not isinstance(version, str) or not version or len(version) > 32:
            errors.append(f"{consent_type} policy has an invalid version")
        if not isinstance(content, str) or not content:
            errors.append(f"{consent_type} policy has empty canonical content")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            errors.append(f"{consent_type} policy digest is not lowercase SHA-256 hex")
        elif isinstance(content, str) and hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            errors.append(f"{consent_type} policy digest does not match canonical content")
    return errors


def policy_unavailable_response(request):
    from django.shortcuts import render

    return render(request, "patients/policy_unavailable.html", status=503)
