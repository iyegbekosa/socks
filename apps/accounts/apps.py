from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    label = "accounts"

    def ready(self) -> None:
        from apps.accounts.roles import seed_roles
        from apps.tenancy.seeding import register_seeder

        register_seeder(seed_roles)
