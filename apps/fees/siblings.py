"""Siblings (§7.1): other active students sharing a guardian phone or email.

A sibling is another ``active`` student of the school sharing at least one non-empty guardian
phone or guardian email (case-insensitive) across both guardian slots. Blank contacts never
match. Phones are stored E.164 (or None) and emails lower-cased by ``Student._normalise``;
emails are still compared with ``Lower`` so rows written around ``save()`` match too.
"""

from django.db.models import Q
from django.db.models.functions import Lower

from apps.students.models import Student


def siblings_of(student: Student) -> list[Student]:
    """``student``'s siblings, ordered by admission number, in one query."""
    phones = {p for p in (student.guardian_phone, student.guardian2_phone) if p}
    emails = {e.lower() for e in (student.guardian_email, student.guardian2_email) if e}
    if not phones and not emails:
        return []
    match = Q()
    if phones:
        match |= Q(guardian_phone__in=phones) | Q(guardian2_phone__in=phones)
    if emails:
        match |= Q(_g1_email__in=emails) | Q(_g2_email__in=emails)
    return list(
        Student.objects.filter(status=Student.Status.ACTIVE)
        .exclude(pk=student.pk)
        .annotate(_g1_email=Lower("guardian_email"), _g2_email=Lower("guardian2_email"))
        .filter(match)
        .select_related("user")
        .order_by("user__admission_number", "pk")
    )
