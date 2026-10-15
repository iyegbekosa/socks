from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from apps.comms import reminders
from apps.comms.service import release_and_enqueue_held, send_batch
from apps.core.tasks import TenantTask
from apps.tenancy.models import School
from config.celery import app


@app.task(base=TenantTask, queue="notifications")
def send_email_batch_task(delivery_ids: list[int]) -> dict[str, int]:
    """Send up to 100 queued, due email deliveries; see ``service.send_batch``. Args are
    redacted from Celery logs (``SECRET_ARG_TASKS``)."""
    return send_batch(delivery_ids)


@app.task(base=TenantTask, queue="notifications")
def send_held_school_task() -> int:
    """This school's 5-minute held-send job; see ``service.release_and_enqueue_held``."""
    return release_and_enqueue_held()


@app.task(queue="notifications")
def send_held_all_schools() -> int:
    """Beat entry (every 5 minutes), run in the public schema: queue one held-send job per
    active, fully provisioned school, with an explicit schema header. Returns the count."""
    schools = School.objects.filter(is_active=True, provisioning_status="ready").exclude(
        schema_name=get_public_schema_name()
    )
    count = 0
    for school in schools:
        send_held_school_task.apply_async(
            queue="notifications", headers={"schema_name": school.schema_name}
        )
        count += 1
    return count


@app.task(base=TenantTask, queue="notifications")
def send_reminders_school_task() -> bool:
    """This school's fee reminder job for today (Africa/Lagos date); see
    ``reminders.send_due_reminders``. Returns whether a reminder went out."""
    return reminders.send_due_reminders(timezone.localdate()) is not None


@app.task(queue="notifications")
def send_reminders_all_schools() -> int:
    """Beat entry (daily at 08:00 Africa/Lagos), run in the public schema: queue one reminder
    job per active, fully provisioned school, with an explicit schema header."""
    schools = School.objects.filter(is_active=True, provisioning_status="ready").exclude(
        schema_name=get_public_schema_name()
    )
    count = 0
    for school in schools:
        send_reminders_school_task.apply_async(
            queue="notifications", headers={"schema_name": school.schema_name}
        )
        count += 1
    return count
