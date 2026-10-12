from typing import Any

from rest_framework import serializers

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
