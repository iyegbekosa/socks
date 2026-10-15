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

    The full shape is shown only when ``context["all_lines"]`` is true (fee staff) or the
    invoice's student pk is in ``context["lines_for"]`` (a student caller themself). Every other
    invoice (a student's siblings') fails closed to the narrow shape of Ruling N:
    ``{id, student: {id, first_name}, term, balance}``.
    """

    def to_representation(self, invoice: Any) -> dict[str, Any]:
        student = invoice.student
        term = invoice.term
        term_data = {
            "id": term.pk,
            "name": term.get_number_display(),
            "session": term.session.name,
        }
        full = self.context.get("all_lines") is True or student.pk in self.context.get(
            "lines_for", frozenset()
        )
        if not full:
            return {
                "id": invoice.pk,
                "student": {"id": student.pk, "first_name": student.first_name},
                "term": term_data,
                "balance": money_fields(invoice.balance_kobo),
            }
        data: dict[str, Any] = {
            "id": invoice.pk,
            "student": {
                "id": student.pk,
                "admission_number": student.user.admission_number,
                "first_name": student.first_name,
            },
            "term": term_data,
            "status": invoice.status,
            "total": money_fields(invoice.total_kobo),
            "paid": money_fields(invoice.paid_kobo),
            "balance": money_fields(invoice.balance_kobo),
        }
        data["lines"] = [
            {
                "kind": line.kind,
                "description": line.description,
                "amount": money_fields(line.amount_kobo),
            }
            for line in invoice.lines.all()
        ]
        return data


class NotificationSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """A student's in-app delivery; ``id`` is the Delivery id."""

    id = serializers.IntegerField()
    subject = serializers.CharField(source="message.subject")
    body = serializers.CharField()
    created_at = serializers.DateTimeField(source="message.created_at")
    read = serializers.SerializerMethodField()

    def get_read(self, obj: Any) -> bool:
        return obj.read_at is not None
