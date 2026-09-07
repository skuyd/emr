from django.apps import AppConfig


class TreatmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.treatments"
    verbose_name = "治疗记录与周期"

    def ready(self):
        from . import lifecycle  # noqa: F401
