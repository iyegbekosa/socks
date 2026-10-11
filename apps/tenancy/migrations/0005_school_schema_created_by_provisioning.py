from django.db import migrations, models


def mark_ready_schools_owned(apps, schema_editor):
    """Schools already provisioned got their schema from us (signup task or auto_create_schema),
    so a later re-run may re-use it. Pending/failed rows stay False: we cannot prove ownership."""
    School = apps.get_model("tenancy", "School")
    School.objects.filter(provisioning_status="ready").exclude(schema_name="public").update(
        schema_created_by_provisioning=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0004_school_signup_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="school",
            name="schema_created_by_provisioning",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(mark_ready_schools_owned, migrations.RunPython.noop),
    ]
