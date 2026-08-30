from datetime import timedelta
import uuid

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.utils import timezone
import pytest

from apps.operations.permissions import Action, Role, authorize, provision_role_groups


def operator(django_user_model, role=None, *, is_staff=True):
    account = django_user_model.objects.create(
        phone_hash=uuid.uuid4().hex * 2,
        phone_encrypted="ciphertext",
        is_staff=is_staff,
    )
    if role is not None:
        provision_role_groups()
        account.groups.add(Group.objects.get(name=role.value))
    return account


ROLE_MATRIX = {
    Role.SUPPORT: {Action.VIEW_DELETION_STATUS, Action.VIEW_SUPPORT_METADATA, Action.VIEW_HEALTH},
    Role.PROCESSOR_OPERATOR: {
        Action.REQUEUE_PROCESSING,
        Action.ACTIVATE_PARSING_VERSION,
        Action.VIEW_METRICS,
        Action.VIEW_HEALTH,
    },
    Role.DICTIONARY_MANAGER: {Action.PUBLISH_DICTIONARY, Action.VIEW_HEALTH},
    Role.PRIVACY_ADMIN: {
        Action.CHANGE_QUOTA,
        Action.GRANT_SUPPORT_ACCESS,
        Action.VIEW_DELETION_STATUS,
        Action.VIEW_METRICS,
        Action.VIEW_HEALTH,
        Action.REPLAY_TOMBSTONES,
    },
}


@pytest.mark.django_db
@pytest.mark.parametrize("role", list(Role))
def test_role_action_matrix_is_deny_by_default(django_user_model, role):
    account = operator(django_user_model, role)
    verified_at = timezone.now()

    for action in Action:
        if action in {Action.VIEW_ORIGINAL, Action.VIEW_OCR}:
            expected = False
        else:
            expected = action in ROLE_MATRIX[role]
        if expected:
            assert authorize(account, action, totp_verified_at=verified_at).role == role
        else:
            with pytest.raises(PermissionDenied):
                authorize(account, action, totp_verified_at=verified_at)


@pytest.mark.django_db
def test_staff_flag_and_explicit_role_are_both_required(django_user_model):
    no_role = operator(django_user_model)
    not_staff = operator(django_user_model, Role.SUPPORT, is_staff=False)

    for account in (no_role, not_staff):
        with pytest.raises(PermissionDenied):
            authorize(account, Action.VIEW_HEALTH)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "action,role",
    [
        (Action.ACTIVATE_PARSING_VERSION, Role.PROCESSOR_OPERATOR),
        (Action.PUBLISH_DICTIONARY, Role.DICTIONARY_MANAGER),
        (Action.CHANGE_QUOTA, Role.PRIVACY_ADMIN),
        (Action.GRANT_SUPPORT_ACCESS, Role.PRIVACY_ADMIN),
        (Action.REPLAY_TOMBSTONES, Role.PRIVACY_ADMIN),
    ],
)
def test_sensitive_actions_require_recent_totp(django_user_model, action, role):
    account = operator(django_user_model, role)

    for value in (None, timezone.now() - timedelta(minutes=6)):
        with pytest.raises(PermissionDenied):
            authorize(account, action, totp_verified_at=value)
    assert authorize(account, action, totp_verified_at=timezone.now()).requires_totp is True

