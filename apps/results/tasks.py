from apps.academics.models import ClassArm, Term
from apps.core.tasks import TenantTask
from apps.results.cards import generate_report_cards
from apps.results.notify import notify_published
from apps.results.workflow import run_compute
from config.celery import app


@app.task(base=TenantTask, queue="default")
def compute_class_task(arm_id: int, term_id: int) -> None:
    """Compute an approved class's results (FR-07); see ``workflow.run_compute``."""
    arm = ClassArm.objects.select_related("level").filter(pk=arm_id).first()
    term = Term.objects.filter(pk=term_id).first()
    if arm is None or term is None:
        return
    run_compute(arm, term)


@app.task(base=TenantTask, queue="reports")
def generate_report_cards_task(arm_id: int, term_id: int) -> None:
    """Render and store a published class's report cards (FR-09); see
    ``cards.generate_report_cards``. A class no longer published is left alone."""
    arm = ClassArm.objects.select_related("level").filter(pk=arm_id).first()
    term = Term.objects.filter(pk=term_id).first()
    if arm is None or term is None:
        return
    generate_report_cards(arm, term)


@app.task(base=TenantTask, queue="notifications")
def notify_published_task(arm_id: int, term_id: int) -> None:
    """Tell a published class's guardians its results are ready; see
    ``notify.notify_published``. A class no longer published is left alone. Its args are
    redacted from Celery logs (``SECRET_ARG_TASKS``)."""
    arm = ClassArm.objects.filter(pk=arm_id).first()
    term = Term.objects.filter(pk=term_id).first()
    if arm is None or term is None:
        return
    notify_published(arm, term)
