from typing import Any

from rest_framework import serializers

from apps.core.money import money_fields
from apps.imports.models import ImportJob
from apps.imports.services import ERRORS_SHOWN
from apps.students.models import Student


class StudentListSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Only these four fields: never date of birth, photo or guardian data."""

    id = serializers.IntegerField()
    admission_number = serializers.CharField(source="user.admission_number")
    full_name = serializers.CharField()
    # The queryset prefetches `current_enrolments` (current session, active).
    class_ = serializers.SerializerMethodField()

    def get_class_(self, student: Student) -> str | None:
        rows = student.current_enrolments  # type: ignore[attr-defined]
        return rows[0].arm.display_name if rows else None

    def to_representation(self, instance: Any) -> dict[str, Any]:
        data = super().to_representation(instance)
        data["class"] = data.pop("class_")
        return data


class ImportJobSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    errors = serializers.SerializerMethodField()

    class Meta:
        model = ImportJob
        fields = [
            "id",
            "kind",
            "status",
            "total_rows",
            "ok_rows",
            "error_rows",
            "errors",
            "message",
        ]
        read_only_fields = fields

    def get_errors(self, job: ImportJob) -> list[Any]:
        return job.errors[:ERRORS_SHOWN]


class ImportUploadSerializer(serializers.Serializer):  # type: ignore[type-arg]
    file = serializers.FileField()


def sheet_data_json(data: Any) -> dict[str, Any]:
    """The score grid (``apps.results.scores.SheetData``) as the API returns it."""
    return {
        "offering": data.offering.pk,
        "term": data.term.pk,
        "sheet_status": data.sheet_status,
        "class_status": data.class_status,
        "editable": data.editable,
        "components": [
            {"id": c.pk, "name": c.name, "max_score": c.max_score, "order": c.order}
            for c in data.components
        ],
        "rows": data.rows,
    }


class InvoiceSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """An invoice with money in ``money_fields`` shape.

    ``context["lines_for"]`` is the set of student pks whose invoice lines may be shown (the
    caller themself, or every student for fee staff). Other invoices (a student's siblings')
    omit ``lines`` entirely.
    """

    def to_representation(self, invoice: Any) -> dict[str, Any]:
        student = invoice.student
        term = invoice.term
        data: dict[str, Any] = {
            "id": invoice.pk,
            "student": {
                "id": student.pk,
                "admission_number": student.user.admission_number,
                "first_name": student.first_name,
            },
            "term": {
                "id": term.pk,
                "name": term.get_number_display(),
                "session": term.session.name,
            },
            "status": invoice.status,
            "total": money_fields(invoice.total_kobo),
            "paid": money_fields(invoice.paid_kobo),
            "balance": money_fields(invoice.balance_kobo),
        }
        lines_for = self.context.get("lines_for")
        if lines_for is None or student.pk in lines_for:
            data["lines"] = [
                {
                    "kind": line.kind,
                    "description": line.description,
                    "amount": money_fields(line.amount_kobo),
                }
                for line in invoice.lines.all()
            ]
        return data
