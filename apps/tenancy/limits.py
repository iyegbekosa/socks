"""Plan limits for the current school (students, storage)."""

from django.db import connection
from django.db.models import Sum

from apps.core.errors import ApiError
from apps.tenancy.models import Plan, School

DEFAULT_PLAN_CODE = "basic"


class PlanLimitExceeded(ApiError):
    def __init__(self, message: str):
        super().__init__("PLAN_LIMIT_EXCEEDED", message, status=403)


def _current_school() -> School | None:
    tenant = getattr(connection, "tenant", None)
    if isinstance(tenant, School):
        return tenant
    schema = getattr(connection, "schema_name", None)
    return School.objects.filter(schema_name=schema).select_related("plan").first()


class PlanLimits:
    def __init__(self, plan: Plan):
        self.plan = plan

    @classmethod
    def for_current_school(cls) -> "PlanLimits":
        school = _current_school()
        plan = school.plan if school is not None else None
        if plan is None:
            plan = Plan.objects.get(code=DEFAULT_PLAN_CODE)
        return cls(plan)

    def storage_used_bytes(self) -> int:
        from apps.files.models import StoredFile

        return StoredFile.objects.aggregate(total=Sum("bytes"))["total"] or 0

    def check_can_store(self, extra_bytes: int) -> None:
        if extra_bytes <= 0:
            return
        if self.storage_used_bytes() + extra_bytes > self.plan.storage_bytes:
            raise PlanLimitExceeded("Storage limit for your plan has been reached.")

    def student_count(self) -> int:
        # Lazy: apps.students is tenant-only, this module is also imported in the public schema.
        from apps.students.models import Student

        return Student.objects.filter(status=Student.Status.ACTIVE).count()

    def check_can_add_students(self, count: int = 1) -> None:
        maximum = self.plan.max_students
        if maximum is None:
            return
        if self.student_count() + count > maximum:
            raise PlanLimitExceeded(f"Your plan allows up to {maximum} students.")
