from django.apps import AppConfig


class AcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.academics"
    label = "academics"

    def ready(self) -> None:
        from apps.academics.seeding import seed_academics
        from apps.tenancy.seeding import register_seeder

        register_seeder(seed_academics)
