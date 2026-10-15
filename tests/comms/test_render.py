import pytest
from django.core.exceptions import ValidationError

from apps.comms.render import check_template, render

ANNOUNCEMENT_LIST = "Use only: {school_name}, {term}, {date}"
PER_STUDENT_LIST = "Use only: {school_name}, {student_first_name}, {term}, {balance}, {date}"


def _message(exc: pytest.ExceptionInfo) -> str:
    return exc.value.messages[0]


def test_unknown_placeholder_refused_with_exact_message():
    with pytest.raises(ValidationError) as exc:
        check_template("Hello {name}", per_student=False)
    assert _message(exc) == f"Unknown placeholder {{name}}. {ANNOUNCEMENT_LIST}"


def test_unknown_placeholder_lists_per_student_fields_for_per_student_kinds():
    with pytest.raises(ValidationError) as exc:
        check_template("Hello {name}", per_student=True)
    assert _message(exc) == f"Unknown placeholder {{name}}. {PER_STUDENT_LIST}"


@pytest.mark.parametrize("body", ["Pay now {", "Pay now }", "Pay {term", "{ term}"])
def test_stray_brace_refused(body):
    with pytest.raises(ValidationError) as exc:
        check_template(body, per_student=False)
    assert _message(exc).startswith("Unknown placeholder ")
    assert _message(exc).endswith(ANNOUNCEMENT_LIST)


@pytest.mark.parametrize("field", ["balance", "student_first_name"])
def test_per_student_placeholder_refused_in_announcement(field):
    with pytest.raises(ValidationError) as exc:
        check_template(f"You owe {{{field}}}", per_student=False)
    assert _message(exc) == f"Unknown placeholder {{{field}}}. {ANNOUNCEMENT_LIST}"
    check_template(f"You owe {{{field}}}", per_student=True)


@pytest.mark.parametrize(
    "body", ["{term.session}", "{term[0]}", "{term:>20}", "{term!r}", "{}", "{0}"]
)
def test_only_plain_named_fields_allowed(body):
    with pytest.raises(ValidationError):
        check_template(body, per_student=True)
    with pytest.raises(ValidationError):
        render(body, {"term": "1st Term"})


def test_render_substitutes_plain_text_and_keeps_escaped_braces():
    out = render(
        "{school_name}: {{note}} {student_first_name} owes {balance} for {term}.",
        {
            "school_name": "Greenfield",
            "student_first_name": "Ada {x}",
            "balance": "₦1,000.00",
            "term": "1st Term",
        },
    )
    assert out == "Greenfield: {note} Ada {x} owes ₦1,000.00 for 1st Term."


def test_render_refuses_unknown_or_missing_value():
    with pytest.raises(ValidationError):
        render("Hi {name}", {"name": "x"})
    with pytest.raises(ValidationError):
        render("Hi {balance}", {})
