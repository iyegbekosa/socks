from dataclasses import dataclass, field
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.academics.models import ClassArm, FormTeacher, Term
from apps.academics.services import current_term
from apps.accounts.roles import PRINCIPAL, TEACHER, require_roles
from apps.audit.services import record
from apps.core.locks import school_lock
from apps.core.parsing import parse_id
from apps.results.models import ClassResult, Rating, ReportComment, Trait
from apps.results.scope import can_open_comments
from apps.students.models import Enrolment, Student

MAX_COMMENT = 500
MAX_DAYS = 200
NO_TERM = "Set a current term first"
MOVED_ON = "This class has moved on, so these entries can no longer be changed."


@dataclass
class Parsed:
    errors: list[str] = field(default_factory=list)
    row_errors: dict[int, list[str]] = field(default_factory=dict)
    # student id -> new values; only keys that were posted are present
    comments: dict[int, str] = field(default_factory=dict)
    principal: dict[int, str] = field(default_factory=dict)
    present: dict[int, int | None] = field(default_factory=dict)
    ratings: dict[int, dict[int, int | None]] = field(default_factory=dict)
    days_open: int | None = None
    days_open_posted: bool = False

    def row_error(self, sid: int, msg: str) -> None:
        self.row_errors.setdefault(sid, []).append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.row_errors


def _days(raw: str) -> int | None | bool:
    """None for blank, False for invalid, else the whole number of days."""
    raw = raw.strip()
    if raw == "":
        return None
    if len(raw) > 3 or not all(c in "0123456789" for c in raw):
        return False
    value = int(raw)
    return value if value <= MAX_DAYS else False


def _comment(raw: str, label: str, sid: int, out: Parsed) -> str | None:
    text = raw.strip()
    if len(text) > MAX_COMMENT:
        out.row_error(sid, f"{label} is longer than {MAX_COMMENT} characters.")
        return None
    return text


def parse_post(post, student_ids: set[int], trait_ids: set[int], *, ft: bool, pr: bool) -> Parsed:
    out = Parsed()
    if ft and "days_open" in post:
        out.days_open_posted = True
        opened = _days(post.get("days_open", ""))
        if opened is False:
            out.errors.append(f"Days school opened must be a whole number from 0 to {MAX_DAYS}.")
        else:
            out.days_open = opened  # type: ignore[assignment]
    for key in post:
        prefix, _, rest = key.partition("_")
        if prefix not in ("ct", "dp", "pc", "rt"):
            continue
        if prefix == "rt":
            sraw, _, traw = rest.partition("_")
            sid, tid = parse_id(sraw), parse_id(traw)
            if sid not in student_ids or tid not in trait_ids:
                out.errors.append("The form contains a student or trait that is not in this class.")
                continue
            if not ft:
                continue
            val = post.get(key, "").strip()
            if val == "":
                out.ratings.setdefault(sid, {})[tid] = None  # type: ignore[index]
            elif val in ("1", "2", "3", "4", "5"):
                out.ratings.setdefault(sid, {})[tid] = int(val)  # type: ignore[index]
            else:
                out.row_error(sid, "Ratings must be 1 to 5.")  # type: ignore[arg-type]
            continue
        sid = parse_id(rest)
        if sid not in student_ids:
            out.errors.append("The form contains a student who is not in this class.")
            continue
        assert sid is not None
        raw = post.get(key, "")
        if prefix == "ct" and ft:
            text = _comment(raw, "The class teacher comment", sid, out)
            if text is not None:
                out.comments[sid] = text
        elif prefix == "pc" and pr:
            text = _comment(raw, "The principal comment", sid, out)
            if text is not None:
                out.principal[sid] = text
        elif prefix == "dp" and ft:
            days = _days(raw)
            if days is False:
                out.row_error(sid, f"Days present must be a whole number from 0 to {MAX_DAYS}.")
            else:
                out.present[sid] = days  # type: ignore[assignment]
    out.errors = list(dict.fromkeys(out.errors))
    return out


def _roles(request, arm: ClassArm, term: Term) -> tuple[bool, bool]:
    user = request.user
    if getattr(user, "kind", None) != "staff" or not user.is_active:
        return False, False
    is_ft = FormTeacher.objects.filter(arm=arm, session_id=term.session_id, teacher=user).exists()
    return is_ft, bool(user.has_role(PRINCIPAL))


def _editable(status: str, is_ft: bool, is_pr: bool) -> tuple[bool, bool]:
    draft = status == ClassResult.Status.DRAFT
    return (
        is_ft and draft,
        is_pr and status in (ClassResult.Status.DRAFT, ClassResult.Status.REVIEWED),
    )


def _students(arm: ClassArm, term: Term) -> list[Student]:
    return list(
        Student.objects.filter(
            status=Student.Status.ACTIVE,
            enrolments__arm=arm,
            enrolments__session_id=term.session_id,
            enrolments__status=Enrolment.Status.ACTIVE,
        )
        .distinct()
        .order_by("last_name", "first_name", "pk")
    )


def _rows(students, traits, term, parsed_post=None, errors=None) -> list[dict[str, Any]]:
    comments = {c.student_id: c for c in ReportComment.objects.filter(term=term)}
    ratings = {(r.student_id, r.trait_id): r.value for r in Rating.objects.filter(term=term)}
    rows = []
    for s in students:
        c = comments.get(s.pk)
        post = parsed_post
        if post is not None:
            ct = post.get(f"ct_{s.pk}", c.class_teacher_comment if c else "")
            pc = post.get(f"pc_{s.pk}", c.principal_comment if c else "")
            dp = post.get(f"dp_{s.pk}", "" if not c or c.days_present is None else c.days_present)
            rts = [
                {
                    "trait": t,
                    "value": post.get(f"rt_{s.pk}_{t.pk}", ratings.get((s.pk, t.pk), "")),
                }
                for t in traits
            ]
        else:
            ct = c.class_teacher_comment if c else ""
            pc = c.principal_comment if c else ""
            dp = "" if not c or c.days_present is None else c.days_present
            rts = [{"trait": t, "value": ratings.get((s.pk, t.pk), "")} for t in traits]
        for r in rts:
            r["value"] = str(r["value"])
        rows.append(
            {
                "student": s,
                "ct": ct,
                "pc": pc,
                "dp": dp,
                "ratings": rts,
                "errors": (errors or {}).get(s.pk, []),
            }
        )
    return rows


def _page(request, arm, term, is_ft, is_pr, status, *, post=None, parsed=None, code=200):
    students = _students(arm, term)
    traits = list(Trait.objects.all())
    rows = _rows(students, traits, term, post, parsed.row_errors if parsed else None)
    if post is not None and "days_open" in post:
        days_open = post.get("days_open", "")
    else:
        opened = (
            ReportComment.objects.filter(term=term, student__in=students, days_open__isnull=False)
            .values_list("days_open", flat=True)
            .first()
        )
        days_open = "" if opened is None else opened
    edit_ft, edit_pr = _editable(status, is_ft, is_pr)
    return render(
        request,
        "results/class_comments.html",
        {
            "arm": arm,
            "term": term,
            "rows": rows,
            "traits": traits,
            "days_open": days_open,
            "is_ft": is_ft,
            "is_pr": is_pr,
            "edit_ft": edit_ft,
            "edit_pr": edit_pr,
            "status": status,
            "errors": parsed.errors if parsed else [],
        },
        status=code,
    )


def _save(request, arm: ClassArm, term: Term, is_ft: bool, is_pr: bool):
    with transaction.atomic():
        school_lock(f"results:{arm.pk}:{term.pk}")
        cr, _ = ClassResult.objects.get_or_create(arm=arm, term=term)
        edit_ft, edit_pr = _editable(cr.status, is_ft, is_pr)
        if not edit_ft and not edit_pr:
            parsed = Parsed(errors=[MOVED_ON])
            return _page(
                request, arm, term, is_ft, is_pr, cr.status, post=request.POST, parsed=parsed,
                code=409,
            )  # fmt: skip
        students = _students(arm, term)
        parsed = parse_post(
            request.POST,
            {s.pk for s in students},
            {t.pk for t in Trait.objects.all()},
            ft=edit_ft,
            pr=edit_pr,
        )
        _check_attendance(parsed, term, [s.pk for s in students])
        if not parsed.ok:
            return _page(
                request, arm, term, is_ft, is_pr, cr.status, post=request.POST, parsed=parsed,
                code=400,
            )  # fmt: skip
        comment_ids, rating_ids = _write(request, students, term, parsed)
        if comment_ids:
            record(
                "results.comment_changed",
                request=request,
                obj=cr,
                after={"arm_id": arm.pk, "term_id": term.pk, "student_ids": comment_ids},
            )
        if rating_ids:
            record(
                "results.ratings_changed",
                request=request,
                obj=cr,
                after={"arm_id": arm.pk, "term_id": term.pk, "student_ids": rating_ids},
            )
    messages.success(request, "Saved")
    return redirect("results:class_comments", arm_pk=arm.pk)


def _check_attendance(parsed: Parsed, term: Term, student_ids: list[int]) -> None:
    stored = {
        sid: (dp, do)
        for sid, dp, do in ReportComment.objects.filter(
            term=term, student_id__in=student_ids
        ).values_list("student_id", "days_present", "days_open")
    }
    # Every student whose present/open pair may change must satisfy present <= open.
    check = student_ids if parsed.days_open_posted else list(parsed.present)
    for sid in check:
        s_present, s_open = stored.get(sid, (None, None))
        present = parsed.present[sid] if sid in parsed.present else s_present
        opened = parsed.days_open if parsed.days_open_posted else s_open
        if present is None:
            continue
        if opened is None:
            parsed.row_error(sid, "Enter the days school opened before days present.")
        elif present > opened:
            parsed.row_error(sid, "Days present cannot be more than days school opened.")


def _write(request, students, term, parsed: Parsed) -> tuple[list[int], list[int]]:
    existing = {c.student_id: c for c in ReportComment.objects.filter(term=term)}
    old_ratings = {(r.student_id, r.trait_id): r for r in Rating.objects.filter(term=term)}
    comment_ids: list[int] = []
    rating_ids: list[int] = []
    for s in students:
        sid = s.pk
        obj = existing.get(sid)
        new = {
            "class_teacher_comment": obj.class_teacher_comment if obj else "",
            "principal_comment": obj.principal_comment if obj else "",
            "days_present": obj.days_present if obj else None,
            "days_open": obj.days_open if obj else None,
        }
        before = dict(new)
        if sid in parsed.comments:
            new["class_teacher_comment"] = parsed.comments[sid]
        if sid in parsed.principal:
            new["principal_comment"] = parsed.principal[sid]
        if sid in parsed.present:
            new["days_present"] = parsed.present[sid]
        if parsed.days_open_posted:
            new["days_open"] = parsed.days_open
        if new != before:
            if (
                new["class_teacher_comment"] != before["class_teacher_comment"]
                or new["principal_comment"] != before["principal_comment"]
            ):
                comment_ids.append(sid)
            if obj is None:
                ReportComment.objects.create(student=s, term=term, updated_by=request.user, **new)
            else:
                for k, v in new.items():
                    setattr(obj, k, v)
                obj.updated_by = request.user
                obj.save()
        changed = False
        for tid, val in parsed.ratings.get(sid, {}).items():
            old = old_ratings.get((sid, tid))
            if val is None:
                if old is not None:
                    old.delete()
                    changed = True
            elif old is None:
                Rating.objects.create(student=s, term=term, trait_id=tid, value=val)
                changed = True
            elif old.value != val:
                old.value = val
                old.save(update_fields=["value"])
                changed = True
        if changed:
            rating_ids.append(sid)
    return comment_ids, rating_ids


@never_cache
@require_roles(TEACHER, PRINCIPAL)
@require_http_methods(["GET", "POST"])
def class_comments(request, arm_pk):
    pk = parse_id(str(arm_pk))
    arm = get_object_or_404(ClassArm.objects.select_related("level"), pk=pk if pk else 0)
    term = current_term()
    if not can_open_comments(request.user, arm, term):
        raise PermissionDenied
    if term is None:
        return render(request, "results/class_comments.html", {"arm": arm, "term": None})
    is_ft, is_pr = _roles(request, arm, term)
    if request.method == "POST":
        return _save(request, arm, term, is_ft, is_pr)
    cr = ClassResult.objects.filter(arm=arm, term=term).first()
    status = cr.status if cr else ClassResult.Status.DRAFT
    return _page(request, arm, term, is_ft, is_pr, status)
