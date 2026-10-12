from django.apps import AppConfig


class AgroAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agro_app"

    def ready(self):
        try:
            from agro_app.views import start_scheduled_campaign_checker_daemon
            start_scheduled_campaign_checker_daemon()
        except Exception:
            pass

