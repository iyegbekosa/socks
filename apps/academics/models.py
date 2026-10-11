from django.db import models
from django.db.models import Q


class AcademicSession(models.Model):
    name = models.CharField(max_length=9, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()

    class Meta:
        ordering = ["-start_date"]

    def __str__(self) -> str:
        return self.name


class Term(models.Model):
    FIRST, SECOND, THIRD = 1, 2, 3
    NUMBER_CHOICES = [(FIRST, "1st Term"), (SECOND, "2nd Term"), (THIRD, "3rd Term")]

    session = models.ForeignKey(AcademicSession, on_delete=models.CASCADE, related_name="terms")
    number = models.PositiveSmallIntegerField(choices=NUMBER_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["session__start_date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["session", "number"], name="academics_term_unique"),
            models.UniqueConstraint(
                fields=["is_current"],
                condition=Q(is_current=True),
                name="academics_one_current_term",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.session.name} {self.get_number_display()}"
