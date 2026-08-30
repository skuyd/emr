from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from .session_registry import register_account_session, unregister_account_session


@receiver(user_logged_in, dispatch_uid="accounts.register_login_session")
def register_login_session(sender, request, user, **kwargs):
    register_account_session(user.pk, request.session.session_key)


@receiver(user_logged_out, dispatch_uid="accounts.unregister_logout_session")
def unregister_logout_session(sender, request, user, **kwargs):
    unregister_account_session(request.session.session_key)
