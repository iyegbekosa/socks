from django.db import migrations

GIB = 1024**3

# Storage and SMS numbers are provisional (open question Q3).
PLANS = [
    ("basic", "Basic", 300, 2 * GIB, 500),
    ("standard", "Standard", 1000, 10 * GIB, 2000),
    ("premium", "Premium", None, 50 * GIB, 5000),
]


def seed(apps, schema_editor):
    Plan = apps.get_model("tenancy", "Plan")
    for code, name, max_students, storage, sms in PLANS:
        Plan.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "max_students": max_students,
                "storage_bytes": storage,
                "sms_credits_per_term": sms,
            },
        )


def unseed(apps, schema_editor):
    apps.get_model("tenancy", "Plan").objects.filter(code__in=[p[0] for p in PLANS]).delete()


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0002_plan_school_plan")]
    operations = [migrations.RunPython(seed, unseed)]
