from django.contrib import admin

from .models import Account, ConsentRecord, OtpChallenge


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "is_active", "is_staff", "date_joined")
    readonly_fields = ("id", "phone_hash", "phone_encrypted", "date_joined", "updated_at")


@admin.register(OtpChallenge)
class OtpChallengeAdmin(admin.ModelAdmin):
    list_display = ("id", "delivery_status", "attempts", "created_at", "expires_at")
    exclude = ("phone_hash", "phone_encrypted", "ip_hash", "otp_hash")
    readonly_fields = (
        "id",
        "delivery_status",
        "attempts",
        "created_at",
        "expires_at",
        "locked_at",
        "consumed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "consent_type", "policy_version", "granted_at", "withdrawn_at")
    exclude = ("request_ip_hash", "user_agent_hash")
    readonly_fields = ("id", "account", "consent_type", "policy_version", "policy_digest", "granted_at", "withdrawn_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
