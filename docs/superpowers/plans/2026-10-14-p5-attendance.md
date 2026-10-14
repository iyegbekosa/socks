# P5 Attendance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A form teacher marks the daily register for a 40-student class on a phone in under a minute. Guardians of absent students get an email. Term totals (days present / days opened, and late count past a threshold) appear on the P3 report card automatically.

**Architecture:**
- A new tenant app, `apps.attendance`, holds `AttendanceSettings`, `Holiday` and `AttendanceRecord`. There is one record per (student, date), as the spec says.
- A register is the set of records for one arm on one date. It is saved in one atomic request under a per-arm/date advisory lock.
- `apps/attendance/totals.py` computes term totals from records. The P3 report card and the comments page read those totals instead of the hand-typed numbers whenever records exist.
- Absence emails go through the existing guardian-message pattern from `apps/results/notify.py`, on the `notifications` queue.

**Tech Stack:** The P4 stack. There are no new dependencies. The register page is server-rendered, with radio buttons that work without JS and a small vanilla-JS enhancement for tap-to-toggle.

**Spec and inputs:**
- `School Management Platform — System Design.pdf`:
  - §4.7: daily register per arm with statuses present/absent/late/excused, taken by the class teacher on a phone in under a minute for 40 students; term totals feed the report card;
  - §5.2: `attendance_record(student_id, date, status, recorded_by)`, unique per (student, date);
  - §6.2: `POST /attendance/{class_arm}/{date}`;
  - UC-06.
- `docs/ROADMAP.md` P5, as re-planned 14 Oct 2026: absence alerts go by email, and there is no SMS in the MVP.

**User decisions (14 Oct 2026):**
- Attendance is taken **once a day**.
- **Late counts as present.** The report card shows the late count only when it reaches a school-set threshold.

**Out of scope:**
- per-lesson or per-period attendance;
- SMS (after the pilot);
- staff attendance (version 3);
- offline register entry (P9).

## Global Constraints

All P0–P4 Global Constraints still hold. Added for P5:

**Records**
- `AttendanceRecord(student, date, arm, status, recorded_by, recorded_at, updated_at)` is unique on `(student, date)`. It has an index on `(arm, date)` and an index on `(student, date)`.
- `status` is one of `present`, `absent`, `late`, `excused`.
  - `present` and `late` count as present.
  - `absent` and `excused` are not present.
  - `excused` never triggers an absence email.
- `arm` is the student's arm on that date. It is taken from their active enrolment in that term's session at the moment the record is saved, and stored so totals stay correct after a later move between arms.

**School days**
- A date is a school day for a term when all of these hold:
  - `term.start_date <= date <= term.end_date`;
  - it is Monday to Friday;
  - it is not a `Holiday`.
- A register may be taken only on a school day of the **current** term, and never in the future (Africa/Lagos date).
- `Holiday(date unique, name)` is set by school_admin.

**Totals for a student and term**
- `days_opened` is the number of distinct dates in that term with a record for that student.
- `days_present` is the number of those records with status `present` or `late`.
- `times_late` is the number of records with status `late`.
- `times_absent` is the number with status `absent`; `times_excused` is the number with status `excused`.
- Days on which no register was taken do not count as opened.

**Report card**
- When a student has at least one record in the term, the card's attendance line uses the computed `days_present / days_opened`. Otherwise it falls back to the hand-typed `ReportComment` fields from P3, for schools that start mid-term.
- When `times_late >= AttendanceSettings.late_threshold`, the card adds "Late {n} times". The default threshold is **3**. A threshold of **0** means never show it.
- The P3 comments page shows the computed totals read-only for students with records, and keeps the manual inputs only for students without records.

**Who may take and edit a register**
- **Take a register:** the arm's form teacher for the term's session, the principal, or the school_admin. Everyone else gets 403.
- **Edit by the form teacher:** allowed only within `AttendanceSettings.edit_window_days` (default **7**) days of the date.
- **Edit by the principal or school_admin:** allowed for any date in the current term.
- **Locked by results:** a register for a term is read-only for everyone (409 `ATTENDANCE_LOCKED`) once that arm's P3 `ClassResult` for the term has left `draft`. A results unlock re-opens it.

**Absence email**
- **When:** on commit of a register save, for each student whose status became `absent` in that save (from no record, or from any other status), and only if `AttendanceSettings.absence_email` is on (default **on**).
- **To:** each guardian email, de-duplicated case-insensitively.
- **Text:** exactly `"{first name} was marked absent from school on {date: j F Y}. Please contact the school if this is unexpected."`
- **Limits:**
  - At most one email per student per date. Record `absence_emailed_at` on the record and never send again for that date, even if the status flips back and forth.
  - Failures are logged by type only and never raised.
- **Task:** `send_absence_emails_task(record_ids)`, a TenantTask on queue `notifications`, added to `SECRET_ARG_TASKS`.

**Audit**
- `attendance.register_saved`: `{arm_id, date, counts: {present, absent, late, excused}}`.
- `attendance.register_changed`: only on edits after the first save, with `before`/`after` as `{student_id: status}` for changed students only.
- `attendance.settings_saved`: before/after of the changed fields.
- `attendance.holiday_saved`.
- No names anywhere in the audit.

**API (§6.2)**
- `GET /api/v1/attendance/<arm>/<date>` (`api:attendance_register`) returns `{arm, date, editable, records: [{student: {id, admission_number, name}, status}]}`. Students in the arm with no record get `status: null`.
- `POST` to the same route takes `{"records": [{"student": id, "status": "present"|"absent"|"late"|"excused"}]}` and returns `200` with the saved register.
  - Every student in the arm must appear exactly once. Unknown keys or statuses give 400 `VALIDATION_ERROR`.
  - A non-school day gives 400 `NOT_A_SCHOOL_DAY`. A future date gives 400 `FUTURE_DATE`.
  - Locked gives 409 `ATTENDANCE_LOCKED`. Outside the edit window gives 403.
- `date` is ISO `YYYY-MM-DD`. A bad date gives 404. Ids are parsed with `parse_id`.
- Isolation cases are required for both methods.

**Performance**
- The register page and the POST each use a bounded number of queries, independent of class size. Assert ≤ 12 queries for 40 students.
- The page renders in one round trip. Every student defaults to `present` on a new register, so the teacher taps only the exceptions.

## Review Focus

1. **Moving arms mid-term.** A student moves arm mid-term. Records stay with the arm they were taken in. Totals per student are still correct, and the new arm's register lists them from the move date only. *Task 2*: `test_moved_student_keeps_old_records_and_appears_in_new_arm`.
2. **Repeated absence emails.** A teacher saves a register, flips a student absent → present → absent in two edits. Exactly one absence email is sent for that date. *Task 3*: `test_absence_email_sent_once_per_student_per_date`.
3. **Locked registers.** Results are approved for the class, then someone edits a register for that term. It gives 409, and the card's numbers can't drift after approval. *Task 2*: `test_register_locked_once_results_leave_draft`.
4. **Mixed card sources.** A school starts registers mid-term. Students with records show computed totals; students without show the typed numbers. Neither is silently blank. *Task 4*: `test_card_uses_computed_totals_or_falls_back`.
5. **Holidays and weekends.** Taking a register on a Saturday or a holiday is refused with a clear message. Adding a holiday after a register exists for that date is refused with "A register was already taken on this date." *Tasks 1 and 2*: `test_holiday_conflicts_with_existing_register`, `test_weekend_and_holiday_refused`.

---

### Task 1: Attendance app, settings, holidays and school days

**Files:**
- Create: `apps/attendance/` (`apps.py`, `models.py`, `days.py`, `forms.py`, `views/settings.py`, `urls.py`, templates), migration `0001_initial`, `tests/attendance/`
- Modify: `config/settings.py` (`TENANT_APPS` gets `"apps.attendance"` after `apps.fees`), `config/urls_tenant.py` (`path("attendance/", include("apps.attendance.urls"))`)
- Test: `tests/attendance/test_days.py`, `tests/attendance/test_settings.py`

**Interfaces:**
- Produces:
  - `AttendanceSettings`, a singleton with `load()` like `FeeSettings`. Fields: `late_threshold=3` (0–60), `edit_window_days=7` (0–60), `absence_email=True`.
  - `Holiday(date unique, name)`.
  - `AttendanceRecord` and its `Status` choices, as in Global Constraints, plus `absence_emailed_at` (nullable).
  - `days.is_school_day(term, d) -> bool`.
  - `days.school_days(term, *, until=None) -> list[date]`.
  - `days.check_register_date(d) -> Term`. It raises `RegisterDateError(code, message)` with codes `NOT_A_SCHOOL_DAY`, `FUTURE_DATE` or `NO_CURRENT_TERM`.
  - Routes `attendance:settings` and `attendance:holidays` (school_admin edits; principal and proprietor read).

- [ ] **Step 1: Write failing tests.**
  - With a term from Mon 5 Jan to Fri 16 Jan 2027 and a holiday on Wed 7 Jan, `school_days` returns 9 dates.
  - `check_register_date`:
    - Saturday → `NOT_A_SCHOOL_DAY`;
    - tomorrow → `FUTURE_DATE`;
    - a date in a past term → `NOT_A_SCHOOL_DAY`.
  - Settings and holidays:
    - an admin saves the settings and a holiday, and both are audited;
    - a teacher gets 403;
    - `test_holiday_conflicts_with_existing_register`: a holiday on a date that already has a record gives a form error with the exact message.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest -W error tests/attendance` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS, and run `makemigrations --check`.
- [ ] **Step 5: Commit** `feat(attendance): settings, holidays and school days`.

### Task 2: Taking and editing the register (HTML and API)

**Files:**
- Create: `apps/attendance/register.py`, `apps/attendance/views/register.py`, template `attendance/register.html`, `apps/attendance/static/attendance/register.js`
- Modify: `apps/api/urls.py`, `apps/api/views.py`, `tests/isolation/registry.py`, the academics arm page and the teacher's home (a "Take register" link for the arm's form teacher)
- Test: `tests/attendance/test_register.py`, `tests/api/test_attendance_api.py`

**Interfaces:**
- Consumes: Task 1; `apps.results.models.ClassResult` (lock check); `FormTeacher`; `Enrolment`.
- Produces:
  - `register.students_for(arm, d) -> list[Student]`: students with an active enrolment in that arm for the term's session on date `d`. Use enrolment dates if they exist; otherwise the current enrolment. Ordered by surname, then first name.
  - `register.can_take(user, arm, term) -> bool` and `register.can_edit(user, arm, d) -> bool`, per Global Constraints.
  - `register.is_locked(arm, term) -> bool`: true when a `ClassResult(arm, term)` exists with a status other than draft.
  - `register.save_register(arm, d, statuses: dict[int, str], *, by) -> SaveResult(created: int, changed: dict[int, tuple[str | None, str]], newly_absent: list[int])`.
    - Runs inside `atomic()` with `school_lock(f"attendance:{arm.pk}:{d.isoformat()}")`.
    - The keys must equal `students_for(arm, d)`; otherwise it raises a `ValidationError`.
    - Upserts the records and audits.
    - Registers `transaction.on_commit` for `hooks.on_register_saved(record_ids_newly_absent)`. The hook is a no-op in Task 2; Task 3 fills it in.
  - Routes:
    - `attendance:register <arm_pk> <date>`: GET shows the form, defaulting to present for unrecorded students; POST saves and redirects with "Register saved for {date}".
    - `attendance:today <arm_pk>` redirects to today's register.
  - The API GET and POST are as described in Global Constraints.

- [ ] **Step 1: Write failing tests.**
  - The form teacher saves a 40-student register with 3 absent and 2 late. That creates 40 records, writes one `register_saved` audit with the right counts, and uses ≤ 12 queries for both GET and POST.
  - Re-saving with one change writes `register_changed` containing only that student.
  - Another arm's teacher gets 403. The principal and admin get 200.
  - With `edit_window_days=7`, the form teacher editing a date 8 days ago gets 403, and the principal gets 200.
  - `test_register_locked_once_results_leave_draft`: set `ClassResult` to `reviewed`, then the POST gets 409 and the GET shows read-only.
  - `test_weekend_and_holiday_refused`: HTML shows the message; the API returns `NOT_A_SCHOOL_DAY`.
  - A missing student or an extra student id in the POST gives 400.
  - `test_moved_student_keeps_old_records_and_appears_in_new_arm`.
  - Isolation cases for `api:attendance_register` GET and POST: a school A form teacher with school B's arm id → refused, plus an anonymous case.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
  - The page is a list of students, each with four radio buttons (P / A / L / E). It works without JS.
  - `register.js` adds tap-the-name to cycle present → absent → late → excused, and shows sticky counts and one Save button.
  - Make it mobile-first and usable at 360 px.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(attendance): daily register for form teachers, with API`.

### Task 3: Absence emails to guardians

**Files:**
- Create: `apps/attendance/notify.py`, `apps/attendance/tasks.py`, `apps/attendance/hooks.py` (fill in)
- Modify: `apps/core/logging.py` (`SECRET_ARG_TASKS`)
- Test: `tests/attendance/test_absence_email.py`

**Interfaces:**
- Consumes: `SaveResult.newly_absent` record ids; `AttendanceSettings.absence_email`; the guardian dedupe pattern from `apps/results/notify.py`.
- Produces:
  - `send_absence_emails_task(record_ids: list[int])`.
  - For each record still `absent` with `absence_emailed_at` null: set `absence_emailed_at` with a conditional `update(... absence_emailed_at__isnull=True)`, and send only if that update changed a row. Then email the guardians with the exact text.
  - `hooks.on_register_saved` enqueues the task when the setting is on. Enqueue failures are caught and logged by type.

- [ ] **Step 1: Write failing tests.**
  - Two absent students, one with two guardian emails that differ only in letter case, give 2 emails with the exact text and date format.
  - An `excused` student gets no email.
  - With the setting off, no emails are sent.
  - `test_absence_email_sent_once_per_student_per_date`: absent → present → absent in edits gives one email in total.
  - A student with no guardian email is skipped silently.
  - An SMTP failure is logged by type, and the register save is unaffected.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(attendance): absence emails to guardians`.

### Task 4: Term totals on the report card and comments page

**Files:**
- Create: `apps/attendance/totals.py`
- Modify:
  - `apps/results/cards.py` (`card_context`);
  - `apps/results/templates/results/report_card.html`;
  - `apps/results/views/comments.py` and its template (read-only totals);
  - `apps/results/workflow.py`, only if the "reviewed" requirement checks attendance;
  - the P3 tests that assert the attendance line.
- Test: `tests/attendance/test_totals.py`, `tests/results/test_cards.py` (new cases)

**Interfaces:**
- Produces:
  - `totals.term_totals(student_ids: list[int], term) -> dict[int, Totals(days_opened, days_present, times_late, times_absent, times_excused)]`. It is one aggregate query. Students with no records are absent from the dict.
  - `totals.attendance_for_card(student, term) -> CardAttendance(days_present: int | None, days_opened: int | None, times_late_shown: int | None, source: "register" | "manual" | None)`.
  - `card_context` uses `attendance_for_card`. The template prints "{present} / {opened} days present", and "Late {n} times" when `times_late_shown` is set.
  - The comments page shows "From register: 52 / 55" read-only for students with records.
    - Its POST ignores `dp_<id>` for those students.
    - It shows the `days_open` input only when at least one active student has no records. That input applies only to those students.

- [ ] **Step 1: Write failing tests.**
  - Records over 10 school days give 7 present, 2 late, 1 absent. Totals are present 9 / opened 10, `times_late` 2.
  - With threshold 3 nothing is shown. At 2 the card shows "Late 2 times". At 0 it is never shown.
  - `test_card_uses_computed_totals_or_falls_back`: one student with records shows "9 / 10 days present", and one without records shows the typed `ReportComment` numbers.
  - The comments page shows the read-only totals. A forged `dp_<id>` POST for a student with records changes nothing.
  - `term_totals` for 40 students is one query.
  - The pypdf text of a generated card contains "9 / 10 days present" and "Late 2 times".
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Also run `tests/results` in full.
- [ ] **Step 5: Commit** `feat(attendance): term totals on report cards`.

### Task 5: Attendance views for staff and students

**Files:**
- Create: `apps/attendance/views/reports.py`, templates `attendance/class.html`, `attendance/school.html`, `attendance/my_attendance.html`
- Modify: the student portal home (an attendance summary line and link), staff home (an "Attendance" link)
- Test: `tests/attendance/test_reports.py`

**Interfaces:**
- Produces:
  - `attendance:class <arm_pk>`, visible to form teacher, principal, admin and proprietor.
    - A term grid with students as rows and school days so far as columns, cells P / A / L / E or "·" for no register.
    - A totals column.
    - Missing registers highlighted.
    - Term selectable among the session's terms.
    - CSV export with `safe_cell`, attachment, `no-store`.
  - `attendance:school`, visible to principal, admin and proprietor.
    - For a chosen date (default today), each arm with registered / absent / late counts, and arms with no register yet listed first.
    - A term-to-date attendance % per arm.
  - `attendance:me` (students only).
    - The current term's totals.
    - The dates absent or late.
    - No other student's data.

- [ ] **Step 1: Write failing tests.**
  - The class grid shows the right cell letters and totals. Another arm's teacher gets 403. A bad arm pk gives 404.
  - The school page lists the arm with no register today first, and the counts match.
  - The student page shows their own totals and absent dates. Another student's records are not present in the HTML.
  - The CSV guards a student named `=cmd`.
  - Query count is bounded on the class grid: ≤ 10 queries for 40 students × 60 days.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Then run the full CI set: `ruff check`, `ruff format --check`, `mypy apps config`, `makemigrations --check --dry-run`, `scripts/with-pango .venv/bin/pytest -W error -m "not cloudinary"`.
- [ ] **Step 5: Commit** `feat(attendance): class, school and student attendance views`.

---

## Exit criteria for P5 (roadmap)

- **Speed:** a form teacher marks a 40-student class on a phone in under a minute. This is a manual check per pilot school on a low-end Android device. The page defaults everyone to present, so only exceptions are tapped.
- **Report card:** report cards for the term show the computed attendance, and the late count when it is past the threshold.
- **Isolation:** every new `api/v1/` route has an authenticated cross-school isolation case.

## Decisions left for later phases

- Absence SMS: after the pilot, alongside the SMS option.
- Per-period (subject) attendance: not planned. Revisit if pilot secondary schools ask.
- Attendance on the proprietor dashboard (UC-19): P8.
- Offline register entry: P9, alongside offline score entry.
