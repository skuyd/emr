from django.contrib import admin

from .models import Account, OtpChallenge


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "is_active", "is_staff", "date_joined")
    readonly_fields = ("id", "date_joined", "updated_at")


@admin.register(OtpChallenge)
class OtpChallengeAdmin(admin.ModelAdmin):
    list_display = ("id", "delivery_status", "attempts", "created_at", "expires_at")
    readonly_fields = (
        "id",
        "delivery_status",
        "attempts",
        "created_at",
        "expires_at",
        "locked_at",
        "consumed_at",
    )
