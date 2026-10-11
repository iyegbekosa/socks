from django.db import migrations, models

FORWARD = """
CREATE FUNCTION audit_event_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only' USING ERRCODE = 'internal_error';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_event_append_only
BEFORE UPDATE OR DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();
"""

REVERSE = """
DROP TRIGGER audit_event_append_only ON audit_auditevent;
DROP FUNCTION audit_event_append_only();
"""


class Migration(migrations.Migration):
    initial = True

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("actor_id", models.BigIntegerField(blank=True, null=True)),
                ("actor_label", models.CharField(blank=True, default="", max_length=255)),
                ("action", models.CharField(db_index=True, max_length=64)),
                ("object_type", models.CharField(blank=True, default="", max_length=100)),
                ("object_id", models.CharField(blank=True, default="", max_length=64)),
                ("before", models.JSONField(blank=True, null=True)),
                ("after", models.JSONField(blank=True, null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("request_id", models.CharField(blank=True, default="", max_length=64)),
            ],
            options={"ordering": ["at", "id"]},
        ),
        migrations.RunSQL(FORWARD, REVERSE),
    ]
