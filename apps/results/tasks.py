from apps.academics.models import ClassArm, Term
from apps.core.tasks import TenantTask
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
