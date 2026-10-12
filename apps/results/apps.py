from django.apps import AppConfig


class ResultsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.results"
    label = "results"

    def ready(self) -> None:
        from apps.results.seeding import seed_results
        from apps.tenancy.seeding import register_seeder

        register_seeder(seed_results)
