from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.utils import timezone


class Role(str, Enum):
    SUPPORT = "operations_support"
    PROCESSOR_OPERATOR = "operations_processor_operator"
    DICTIONARY_MANAGER = "operations_dictionary_manager"
    PRIVACY_ADMIN = "operations_privacy_admin"


class Action(str, Enum):
    VIEW_HEALTH = "view_health"
    VIEW_METRICS = "view_metrics"
    VIEW_DELETION_STATUS = "view_deletion_status"
    VIEW_SUPPORT_METADATA = "view_support_metadata"
    REQUEUE_PROCESSING = "requeue_processing"
    ACTIVATE_PARSING_VERSION = "activate_parsing_version"
    PUBLISH_DICTIONARY = "publish_dictionary"
    CHANGE_QUOTA = "change_quota"
    GRANT_SUPPORT_ACCESS = "grant_support_access"
    REPLAY_TOMBSTONES = "replay_tombstones"
    VIEW_ORIGINAL = "view_original"
    VIEW_OCR = "view_ocr"


ROLE_ACTIONS = {
    Role.SUPPORT: frozenset(
        {Action.VIEW_DELETION_STATUS, Action.VIEW_SUPPORT_METADATA, Action.VIEW_HEALTH}
    ),
    Role.PROCESSOR_OPERATOR: frozenset(
        {
            Action.REQUEUE_PROCESSING,
            Action.ACTIVATE_PARSING_VERSION,
            Action.VIEW_METRICS,
            Action.VIEW_HEALTH,
        }
    ),
    Role.DICTIONARY_MANAGER: frozenset({Action.PUBLISH_DICTIONARY, Action.VIEW_HEALTH}),
    Role.PRIVACY_ADMIN: frozenset(
        {
            Action.CHANGE_QUOTA,
            Action.GRANT_SUPPORT_ACCESS,
            Action.VIEW_DELETION_STATUS,
            Action.VIEW_METRICS,
            Action.VIEW_HEALTH,
            Action.REPLAY_TOMBSTONES,
        }
    ),
}
SENSITIVE_ACTIONS = frozenset(
    {
        Action.ACTIVATE_PARSING_VERSION,
        Action.PUBLISH_DICTIONARY,
        Action.CHANGE_QUOTA,
        Action.GRANT_SUPPORT_ACCESS,
        Action.REPLAY_TOMBSTONES,
    }
)
TOTP_FRESHNESS = timedelta(minutes=5)


@dataclass(frozen=True)
class Authorization:
    role: Role
    action: Action
    requires_totp: bool


def provision_role_groups():
    return tuple(Group.objects.get_or_create(name=role.value)[0] for role in Role)


def _recent_totp(value, now):
    if value is None or timezone.is_naive(value):
        return False
    age = now - value
    return timedelta(0) <= age <= TOTP_FRESHNESS


def authorize(account, action, *, totp_verified_at=None, now=None):
    try:
        action = Action(action)
    except (TypeError, ValueError):
        raise PermissionDenied("Operation is not permitted") from None
    if not getattr(account, "is_active", False) or not getattr(account, "is_staff", False):
        raise PermissionDenied("Operation is not permitted")
    if action in {Action.VIEW_ORIGINAL, Action.VIEW_OCR}:
        raise PermissionDenied("Operation is not permitted")
    names = set(account.groups.filter(name__in=[role.value for role in Role]).values_list("name", flat=True))
    roles = [role for role in Role if role.value in names and action in ROLE_ACTIONS[role]]
    if not roles:
        raise PermissionDenied("Operation is not permitted")
    requires_totp = action in SENSITIVE_ACTIONS
    if requires_totp and not _recent_totp(totp_verified_at, now or timezone.now()):
        raise PermissionDenied("Recent second-factor verification is required")
    return Authorization(roles[0], action, requires_totp)
