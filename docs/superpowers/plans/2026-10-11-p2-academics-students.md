# P2 Academics and Students Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A school can set up its academic structure (sessions, terms, classes, arms, subjects, teachers), admit students one by one or in bulk from a spreadsheet, record guardians and consent, and give every student a working portal login. Teachers see only their own classes.

**Architecture:** Three new tenant apps on top of P1.
- `apps.academics`: sessions, terms, levels, arms, subjects, subject offerings, form teachers, academic settings and the admission-number counter.
- `apps.students`: students (with guardian contact fields), enrolments, photos and one-time passwords.
- `apps.imports`: import jobs, CSV/XLSX parsing, and the student and staff importers, run as a Celery `TenantTask`.

Each student's login is the P1 `User(kind="student")`. Admin screens are server-rendered. JSON endpoints follow §6.2.

**Tech Stack:** The P1 stack plus `openpyxl` (XLSX reading).

**Spec:**
- Design sections: §4.2 (academics and students), §5.2 (tables), FR-03/04/05, UC-03/04/05 and the §7.1 access matrix in `School Management Platform — System Design.pdf`.
- `docs/ROADMAP.md`: §1.2 (no parent accounts; guardians are contacts; consent at admission) and phase **P2**.
- Items carried over from P1:
  - authenticated isolation cases;
  - `check_can_add_students`;
  - admission numbers must not look like phones;
  - student lockout DoS.

## Global Constraints

Everything in the P0 and P1 plans' Global Constraints still holds. Added for P2:

**Sessions and terms (FR-03)**
- One session = 3 terms (1st, 2nd, 3rd). Session name `YYYY/YYYY+1`, e.g. `2026/2027`.
- Exactly **one current term per school**. Its session is the current session.
- Score entry and attendance (later phases) attach to the current term.

**Class structure**
- Levels belong to a section: `nursery`, `primary`, `jss` or `sss`.
- Arms are groups within a level, displayed as `JSS 1A` or `JSS 1 Gold`.
- Only `sss` arms may have a stream: `science`, `arts` or `commercial`.

**Enrolment**
- A student's class is an **Enrolment row per session**. History is never overwritten (§4.2).

**Admission numbers (FR-05)**
- Generated from a school-defined pattern with a counter row locked during increment. Never duplicated.
- Must never parse as a Nigerian phone number (P1 login treats phone-shaped identifiers as phones).

**Bulk import (FR-04)**
- From CSV or XLSX, run as a background job.
- Good rows are saved and bad rows are listed with a reason.
- 500 rows import in under 1 minute.
- Uploaded files are stored privately (`type="authenticated"`).

**Plan limits**
- Admission and import respect `PlanLimits.check_can_add_students`. Active students are counted from `Student` records from this phase on.

**Guardians and consent (roadmap §1.2, simplified by the user on 11 Oct 2026)**
- Guardians are contacts, not accounts, stored as plain fields on the student: a main guardian (required) and an optional second guardian. There is no separate guardian table and no family linking. P4 finds siblings by matching guardian contacts when it needs them.
- The main guardian needs a phone or an email; an email is strongly encouraged because it is the password-recovery channel.
- Guardian consent is recorded by the school at admission: who recorded it, when, and by what method.

**Student logins (user decision, 11 Oct 2026)**
- Students log in with their **admission number** (unchanged from P1).
- Admission **generates a one-time password**, shown once on the confirmation page (to print or hand over) and never stored in plain text. The student must change it at first login.
- Staff can re-issue a one-time password for one student or a whole class (printable sheet).
- **Forgot password** sends a reset **link by email**: to the main guardian's email for students, to the staff member's own email for staff. SMS codes (P1) remain as a second channel once an SMS provider exists (P5). P1's `User.recovery_phone` is replaced by reading the student's guardian fields.

**Access (§7.1, roles from P1)**

| Role | What they see |
|---|---|
| `school_admin` | Full create/read/update on students and academics |
| `proprietor`, `principal` | Read everything |
| `bursar` | Basic student fields only (name, admission number, class, guardian contacts) |
| `teacher` | Only students enrolled this session in arms where they are the form teacher or teach a subject offering |
| Student | Only themselves |

- Out-of-scope objects return **404**, not 403, so existence isn't leaked.

**Children's data**
- Date of birth, photo and guardian contacts are sensitive. Never log them.
- Photos are private (`authenticated`) and served only through a permission-checked view that redirects to a 10-minute signed link.
- Photos are re-encoded to JPEG, at most 600×600, quality 80.

**Audit actions added:**
- `academics.session_created`, `academics.term_set_current`, `academics.structure_changed`, `academics.offering_assigned`, `academics.form_teacher_assigned`, `academics.admission_pattern_changed`
- `student.admitted`, `student.updated`, `student.withdrawn`, `student.photo_changed`
- `student.password_issued`, `auth.password_changed`, `auth.password_reset_link_sent`
- `import.started`, `import.completed`, `import.failed_stale`

**Dates**
- Form and CSV dates are `DD/MM/YYYY`; ISO `YYYY-MM-DD` and native Excel dates are also accepted. Display is always `DD/MM/YYYY`.

## Review Focus

1. **Excel cells that aren't text.**
   - A phone typed as a number (`8031234567`, or `8031234567.0` when read as float) must normalise to `+2348031234567`.
   - A date stored as an Excel date must import as that date.
   - Test in Task 10.
2. **Two admins admitting at the same moment.** No duplicate admission numbers. Test in Task 5.
3. **A teacher opening a student outside their classes by ID**, through a page or the API: 404, no data. Test in Tasks 7 and 12.
4. **The plan's student limit reached part-way through an import.** Earlier rows are saved; rows beyond the limit are reported as errors with a clear reason, and the job still completes. Test in Task 10.
5. **Siblings sharing one guardian email.** A reset requested for one child's admission number sends a link that resets only that child's password, and the other siblings' passwords are untouched. Test in Task 8.

---

## File Structure

```
apps/core/images.py                 # shared validate + re-encode (logo, photos)
apps/academics/
  models.py      # AcademicSession, Term, ClassLevel, ClassArm, Subject, SubjectOffering,
                 # FormTeacher, AcademicSettings, AdmissionCounter
  services.py    # create_session, set_current_term, current_term(), admission numbers,
                 # assign_offering, assign_form_teacher
  scope.py       # arms_taught_by(user), students_visible_to(user)
  seeding.py     # default levels and subjects per levels_offered
  forms.py, views.py, urls.py, templates/academics/*
apps/students/
  models.py      # Student (incl. guardian fields), Enrolment
  services.py    # admit_student, update_student, withdraw_student, photo
  passwords.py   # issue_one_time_passwords
  forms.py, views.py, urls.py, templates/students/*
apps/accounts/   # + must_change_password, forced change, email reset links, student lockout rules
apps/imports/
  models.py      # ImportJob
  parsing.py     # read_rows(file) for CSV/XLSX, cell normalisers
  students.py, staff.py   # row validators + importers
  tasks.py       # run_import TenantTask
  forms.py, views.py, urls.py, templates/imports/*
apps/api/        # api/v1 routes moved here: me, students, imports
tests/isolation/registry.py   # actor-aware cases
```

---

### Task 1: Authenticated isolation cases

**Files:**
- Modify: `tests/isolation/registry.py`, `tests/isolation/test_routes.py`
- Test: `tests/isolation/test_routes.py`

**Interfaces:**
- Produces: `IsolationCase` gains `actor: Callable[[], User] | None = default_actor`. `default_actor()` creates a staff user with `school_admin` in the active (school A) schema.
  - The test logs the actor into `client_a` with `tests.accounts.helpers.login_verified` before requesting school B's object.
  - `actor=None` means anonymous, which keeps today's `api:me` case.
  - New fields `expect: Literal["refused", "absent"] = "refused"` and `marker: Callable[[obj], str] | None`. `"refused"`: status 401/403/404, never 2xx. `"absent"` (list endpoints): any status, but `marker(obj)` (e.g. school B's admission number) must not appear in the response body.
  - Either way the object is unchanged in school B.

- [ ] **Step 1: Write the failing tests.**
  - In a test-only urlconf, a deliberately leaky DRF view takes `?schema=` and reads `Student`-like data from that schema with `schema_context`. An authenticated case against it must FAIL the isolation assertion; assert this with `pytest.raises(AssertionError)` around the shared check helper.
  - A well-behaved view returning 404 passes.
  - An `expect="absent"` case against a test-only list view that includes school B's marker fails; a list view without it passes.
  - The existing `api:me` case keeps passing with `actor=None`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/isolation -W error` → FAIL.
- [ ] **Step 3: Implement.** Extract the cross-school check into `assert_cross_school_refused(case, school_a, school_b, client_a)` so it can be tested directly.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `test(isolation): authenticated cross-school cases`.

### Task 2: Sessions and terms

**Files:**
- Create: `apps/academics/{__init__,apps,models,services,forms,views,urls}.py`, templates `academics/sessions.html`, `academics/session_form.html`, migration
- Modify: `config/settings.py` (`TENANT_APPS`), `config/urls_tenant.py` (`academics/`, namespace `academics`)
- Test: `tests/academics/test_sessions.py`

**Interfaces:**
- Produces:
  - `AcademicSession(name unique, start_date, end_date)`.
  - `Term(session FK, number 1–3, start_date, end_date, is_current)`.
    - Unique `(session, number)`.
    - Partial unique constraint: at most one `is_current=True` row.
  - `create_session(*, name: str, terms: list[tuple[date, date]], by, request=None) -> AcademicSession`:
    - `name` must match `^(\d{4})/(\d{4})$` with second = first + 1;
    - exactly 3 term date pairs, each `start < end`, non-overlapping and in order;
    - session dates = first start / last end;
    - audits `academics.session_created`.
  - `set_current_term(term, *, by, request=None) -> None` — atomic: clears the old current term, sets the new one, audits `academics.term_set_current`.
  - `current_term() -> Term | None`; `current_session() -> AcademicSession | None`.
  - Pages (`require_roles(SCHOOL_ADMIN)` to change; proprietor and principal read):
    - `academics:sessions` lists sessions with terms and the current term badge;
    - `academics:session_create`;
    - `academics:set_current` is POST only.

- [ ] **Step 1: Write failing tests:**
  - creating `2026/2027` with 3 valid terms works;
  - `2026/2028`, `2026-2027`, 2 terms, overlapping terms and a term ending before it starts each give `ValidationError` with a clear message;
  - `set_current_term` twice leaves exactly one current term, and the DB constraint rejects a second `is_current=True` via `update()`;
  - `current_session()` follows the current term;
  - a teacher gets 403 on create;
  - a verified school_admin can create and set current, and both are audited;
  - date inputs accept `DD/MM/YYYY`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/academics/test_sessions.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(academics): sessions and terms with one current term`.

### Task 3: Levels, arms, subjects and default seeding

**Files:**
- Modify: `apps/academics/models.py`, `services.py`, `forms.py`, `views.py`, `urls.py`; templates `academics/structure.html`, `academics/subjects.html`
- Create: `apps/academics/seeding.py`, migration
- Test: `tests/academics/test_structure.py`, `tests/academics/test_seeding.py`

**Interfaces:**
- Produces:
  - `ClassLevel(name unique, section ∈ {nursery, primary, jss, sss}, order)`.
  - `ClassArm(level FK, suffix, stream ∈ {None, science, arts, commercial})`.
    - Unique `(level, suffix)`.
    - A stream is only allowed when `level.section == "sss"`.
    - `display_name` = `f"{level.name}{suffix}"` when the suffix is one character, else `f"{level.name} {suffix}"`.
    - Classmethod `find_by_display(text) -> ClassArm | None`: case-insensitive, whitespace-normalised. `"jss 1a"`, `"JSS1A"` and `"JSS 1 A"` all find `JSS 1A`.
  - `Subject(name unique, code unique slug, sections: list[str])`.
  - Seeder `seed_academics(school)`, registered with `register_seeder`:
    - for each section in `school.levels_offered`, create its levels and subjects (idempotent by name);
    - no arms.
    - Levels:

      | Section | Levels |
      |---|---|
      | nursery | `Nursery 1`, `Nursery 2` |
      | primary | `Primary 1`…`Primary 6` |
      | jss | `JSS 1`…`JSS 3` |
      | sss | `SSS 1`…`SSS 3` |

    - Default subjects, all editable:

      | Section | Subjects |
      |---|---|
      | nursery | Numeracy, Literacy, Basic Science, Social Habits, Creative Arts, Rhymes |
      | primary | English Language, Mathematics, Basic Science and Technology, Social Studies, Civic Education, Christian Religious Studies, Islamic Religious Studies, Cultural and Creative Arts, Physical and Health Education, Nigerian Language, Computer Studies |
      | jss | English Language, Mathematics, Basic Science, Basic Technology, Social Studies, Civic Education, Business Studies, Computer Studies, Christian Religious Studies, Islamic Religious Studies, Cultural and Creative Arts, Physical and Health Education, French, Nigerian Language, Agricultural Science, Home Economics |
      | sss | English Language, Mathematics, Civic Education, Biology, Physics, Chemistry, Further Mathematics, Agricultural Science, Economics, Government, Literature in English, Geography, Christian Religious Studies, Islamic Religious Studies, Financial Accounting, Commerce, Computer Studies, Data Processing |

    - A subject shared by several sections gets the union of those sections.
  - Pages (school_admin changes; proprietor and principal read):
    - `academics:structure`: levels with their arms; add, rename and delete arms. Deleting an arm with enrolments is refused.
    - `academics:subjects`: add, edit, delete. Deleting a subject with offerings is refused.
    - Every change audits `academics.structure_changed`.

- [ ] **Step 1: Write failing tests:**
  - a school with `levels_offered=["jss","sss"]` gets 6 levels and the jss/sss subjects, and a second run adds nothing;
  - `English Language` has `sections ⊇ {"jss","sss"}`;
  - a stream on a JSS arm gives `ValidationError`;
  - `find_by_display` handles the 3 spellings;
  - a duplicate `(level, suffix)` is refused;
  - delete refusals;
  - role checks;
  - audits.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/academics -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(academics): levels, arms, subjects and default seeding`.

### Task 4: Subject offerings, form teachers and teaching scope

**Files:**
- Modify: `apps/academics/models.py`, `services.py`, `views.py`, `urls.py`; template `academics/arm_detail.html`
- Create: `apps/academics/scope.py`, migration
- Test: `tests/academics/test_offerings.py`, `tests/academics/test_scope.py`

**Interfaces:**
- Produces:
  - `SubjectOffering(subject FK, arm FK, session FK, teacher FK User null, is_compulsory bool default True)`.
    - Unique `(subject, arm, session)`.
    - The teacher must be active staff with the `teacher` role.
    - The subject's sections must include the arm's level section.
  - `FormTeacher(arm FK, session FK, teacher FK User)` — unique `(arm, session)`.
  - `assign_offering(*, subject, arm, session, teacher, is_compulsory=True, by, request=None) -> SubjectOffering` — upsert; audits `academics.offering_assigned`.
  - `assign_form_teacher(*, arm, session, teacher, by, request=None) -> FormTeacher` — upsert; audits `academics.form_teacher_assigned`.
  - `scope.arms_taught_by(user, session) -> QuerySet[ClassArm]` — arms where the user is the form teacher or teaches an offering in that session.
  - `academics:arm_detail` page: a school_admin assigns the form teacher and the offerings (teacher per subject) for the current session.

- [ ] **Step 1: Write failing tests:**
  - assigning a JSS-only subject to an SSS arm, or a non-teacher, gives `ValidationError`;
  - re-assigning replaces the teacher, and both assignments are audited;
  - `arms_taught_by` returns the form arm plus the offering arms for that session only, not a previous session's;
  - a teacher can't open the arm detail page to change it (403);
  - a verified admin can.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/academics -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(academics): subject offerings, form teachers and teaching scope`.

### Task 5: Admission-number pattern and counter

**Files:**
- Modify: `apps/academics/models.py`, `services.py`, `forms.py`, `views.py`, `urls.py`; template `academics/settings.html`
- Modify: `apps/accounts/identifiers.py` (export `looks_like_phone(raw) -> bool`)
- Test: `tests/academics/test_admission_numbers.py`

**Interfaces:**
- Produces:
  - `AcademicSettings` singleton (pk = 1):
    - `admission_prefix` (default: the school's initials, upper-case, max 8 chars, letters only);
    - `admission_pattern` (default `"{PREFIX}/{YEAR}/{SEQ:4}"`).
  - `AdmissionCounter(year int unique, last int)`.
  - `validate_admission_pattern(pattern: str, prefix: str) -> None`:
    - tokens are `{PREFIX}`, `{YEAR}` and `{SEQ:n}` (n = 3–6); `{SEQ:n}` is required, exactly once;
    - literal characters allowed: `A–Z 0–9 / -`;
    - the rendered sample for year 2026, seq 1 must be at most 32 characters and must not `looks_like_phone`.
  - `next_admission_number(year: int) -> str` — `select_for_update` on the counter row (created with `get_or_create` first). Increments, renders, and loops past any number already in use, e.g. one taken by an import. Must be called inside the caller's transaction.
  - `validate_manual_admission_number(raw) -> str` — normalises it with `normalise_admission_number`. Rejects it if it is phone-shaped, longer than 32, contains characters outside `A–Z 0–9 / -`, or is already used.
  - `academics:settings` page (school_admin): edit the prefix and pattern, with a live sample. Audits `academics.admission_pattern_changed`.

- [ ] **Step 1: Write failing tests:**
  - With prefix `GFS`, 3 calls for 2026 give `GFS/2026/0001`, `0002`, `0003`; 2027 restarts at `0001`.
  - Patterns:
    - `"{YEAR}{SEQ:6}"` with year 2026 renders `2026000001`, a 10-digit number. That isn't phone-shaped, so it is allowed.
    - `"08031{SEQ:6}"` renders `08031000001`, a valid Nigerian mobile number, so it is rejected.
    - A pattern without `{SEQ}` is rejected.
    - A pattern with `{SEQ}` twice is rejected.
  - Manual numbers: `08031234567` and `gfs/2026/0001` (once used) are rejected; `" gfs/2026/0099 "` → `GFS/2026/0099`.
  - *(Review Focus 2)* Two threads with separate DB connections (`threading` + `connections.close_all()` in each), each calling `next_admission_number(2026)` 20 times in its own transaction inside `schema_context("school_a")`, give 40 distinct numbers. This needs committed data: run it against a dedicated throwaway schema created and dropped by the test with raw SQL, **not** `transaction=True`. If that proves infeasible, test the lock with two connections and `SELECT … FOR UPDATE NOWAIT` raising, and explain.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/academics/test_admission_numbers.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(academics): admission-number pattern and locked counter`.

### Task 6: Students, guardian contacts and enrolment

**Files:**
- Create: `apps/students/{__init__,apps,models,services}.py`, migration; `apps/core/images.py`
- Modify: `config/settings.py` (`TENANT_APPS`), `apps/tenancy/limits.py` (`student_count` counts active `Student`), `apps/branding/forms.py` (use `apps/core/images.py`)
- Test: `tests/students/test_admission.py`, `tests/core/test_images.py`, `tests/students/factories.py`

**Interfaces:**
- Produces:
  - `images.clean_image(upload, *, max_bytes: int, allowed: dict[str, str], max_pixels: int, output: Literal["same", "jpeg"], max_side: int | None, quality: int) -> ContentFile`. This is the shared validate + re-encode step, holding the P1 logo rules (extension ↔ format match, header-size cap, polyglot-free re-encode). Branding switches to it with no behaviour change.
  - `Student`:
    - `user` (OneToOne `User(kind="student")`, PROTECT); property `admission_number` → `user.admission_number`;
    - `first_name`, `middle_name`, `last_name`, `sex` ∈ {`M`, `F`}, `date_of_birth`;
    - `photo` (FileField, default private storage, `unique_upload_name("students/photos", …)`);
    - `status` ∈ {`active`, `withdrawn`, `graduated`}, `admitted_at`;
    - main guardian: `guardian_name` (required), `guardian_relationship` ∈ {mother, father, guardian, other}, `guardian_phone` (E.164, nullable), `guardian_email` (blank allowed). A check constraint requires a phone or an email.
    - second guardian (all optional): `guardian2_name`, `guardian2_relationship`, `guardian2_phone`, `guardian2_email`;
    - `consent_recorded_at`, `consent_recorded_by` FK User, `consent_method` ∈ {`paper_form`, `in_person`, `digital`}.
  - `Enrolment(student FK, arm FK, session FK, status ∈ {active, withdrawn})`, unique `(student, session)`.
  - `GuardianData` dataclass (name, relationship, phone, email) used by forms and import.
  - `admit_student(*, first_name, middle_name="", last_name, sex, date_of_birth, arm, session, guardian: GuardianData, guardian2: GuardianData | None = None, consent_method, admission_number: str | None = None, by, request=None) -> tuple[Student, str]`. Returns the student and the **one-time password**. In one `transaction.atomic()`:
    1. `PlanLimits.check_can_add_students(1)`.
    2. Admission number = manual (validated) or `next_admission_number(session start year)`.
    3. Create the student user with a generated one-time password (see Task 8's generator) and `must_change_password=True`.
    4. Create the student (with guardian fields) and the enrolment.
    5. Audit `student.admitted` (no DOB, no guardian contacts, no password).
    - The date of birth must be in the past, and the age must be 1–25.
  - `update_student(student, data, *, by, request=None)` — updates profile and guardian fields; audits `student.updated` with changed field **names** only.
  - `set_student_photo(student, upload, *, by, request=None)`:
    - `clean_image(..., max_bytes=2 MiB, allowed={.jpg,.jpeg,.png,.webp}, max_pixels=4096², output="jpeg", max_side=600, quality=80)`;
    - deletes the old photo on commit;
    - audits `student.photo_changed`.
  - `withdraw_student(student, *, by, request=None)` — status `withdrawn`, the current enrolment is withdrawn, the user is deactivated, audits `student.withdrawn`.
  - Remove P1's `User.recovery_phone` (migration) and change `reset_phone_for(user)` to read the student's `guardian_phone` (staff: their own phone).

- [ ] **Step 1: Write failing tests:**
  - Admission creates the student, user and enrolment:
    - the admission number is `GFS/2026/0001`;
    - the returned password logs the student in, and `must_change_password` is set;
    - the audit has no DOB, guardian contact or password.
  - At the plan limit, `PlanLimitExceeded` is raised and nothing is created.
  - A manual phone-shaped admission number is refused.
  - A main guardian with neither phone nor email is refused (form and DB constraint).
  - A photo upload stores a JPEG of at most 600 px under `dev/school_a/students/photos/` with `type="authenticated"`; a GIF is refused.
  - `withdraw_student` deactivates the login.
  - `PlanLimits.student_count()` counts active `Student` rows.
  - `reset_phone_for` returns the guardian phone for a student; the existing P1 reset tests still pass after `recovery_phone` is removed.
  - Branding tests still pass.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/students tests/core/test_images.py tests/branding tests/accounts -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** `feat(students): students with guardian contacts, enrolment and one-time passwords`.

### Task 7: Student admin pages

**Files:**
- Create: `apps/students/{forms,views,urls}.py`, templates `students/list.html`, `students/admit.html`, `students/detail.html`, `students/edit.html`, `students/guardian_form.html`
- Modify: `apps/academics/scope.py` (`students_visible_to`), `config/urls_tenant.py` (`students/`, namespace `students`)
- Test: `tests/students/test_pages.py`, `tests/academics/test_scope.py`

**Interfaces:**
- Produces:
  - `scope.students_visible_to(user) -> QuerySet[Student]`:
    - school_admin, proprietor, principal and bursar: all students;
    - teacher: active enrolments in the current session in `arms_taught_by(user, current_session())`;
    - students and anything else: none.
  - Pages:
    - `students:list` (`?q=` name or admission number, `?arm=`; 50 per page; columns name, admission number, class).
    - `students:admit` (school_admin; GET/POST):
      - the arm choices are the current session's arms;
      - the form takes the main guardian (required) and an optional second guardian, a consent checkbox plus method (required), and an optional manual admission number;
      - on success it shows a **confirmation page** with the admission number and the one-time password (`Cache-Control: no-store`, printable, shown once; reloading does not show it again), linking to `students:detail`.
    - `students:detail/<pk>`:
      - a 404 if the student is not in `students_visible_to(user)`;
      - bursars see only basic fields: no DOB, no photo, no consent details;
      - teachers see name, class, guardian names and phones, and the photo.
    - `students:edit/<pk>` (school_admin): profile and guardian fields.
    - `students:withdraw/<pk>` (school_admin, POST only).
    - `students:photo/<pk>`: a permission-checked GET that 302-redirects to a signed 10-minute URL (`storage.url(name)`), with `Cache-Control: no-store`.

- [ ] **Step 1: Write failing tests** (use `login_verified` for admins):
  - list search by name and by admission number;
  - an admin can admit through the form, and the student appears in the list;
  - the consent box is required;
  - *(Review Focus 3)* a teacher of JSS 1A sees JSS 1A students but gets **404** for a JSS 2B student's `detail`, `photo` and `edit`;
  - a bursar's detail page has no DOB;
  - a student user gets 403 or 404 on every staff page;
  - withdraw via GET → 405;
  - every write is audited;
  - the photo URL redirects to a `res.cloudinary.com`/`api.cloudinary.com` download URL containing `expires_at`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/students tests/academics/test_scope.py -W error` → FAIL.
- [ ] **Step 3: Implement.** The layout must stay usable at 360 px; tables scroll horizontally inside a wrapper.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(students): admin pages for admission, profiles, guardians and withdrawal`.

### Task 8: One-time passwords, forced change and email reset links

**Files:**
- Create: `apps/students/passwords.py`, template `students/passwords.html`; `apps/accounts/views/password.py`, templates `accounts/password_change.html`, `accounts/reset_link_confirm.html`, email templates `accounts/email/reset_link_subject.txt`, `accounts/email/reset_link_body.txt`
- Modify: `apps/accounts/models.py` (`must_change_password` bool), `apps/accounts/middleware.py` (`ForcePasswordChangeMiddleware`), `apps/accounts/views/reset.py`, `apps/accounts/services.py`, `apps/students/views.py`, `urls.py`, `apps/accounts/urls.py`, `apps/accounts/templates/accounts/login.html`, settings middleware
- Test: `tests/students/test_passwords.py`, `tests/accounts/test_password_change.py`, `tests/accounts/test_reset_link.py`

**Interfaces:**
- Produces:
  - `generate_one_time_password() -> str` — 10 characters from `ABCDEFGHJKLMNPQRSTUVWXYZ23456789` using `secrets` (used by `admit_student` too).
  - `issue_one_time_passwords(students: Iterable[Student], *, by, request=None) -> list[IssuedPassword(student, admission_number, password)]` — sets a new password and `must_change_password=True` per student; audits `student.password_issued` per student without the password; nothing but the hash is stored.
  - `students:passwords` (school_admin; POST only, with selected students or `arm=`): a printable sheet (school name, logo, student name, class, admission number, password, portal URL), `Cache-Control: no-store`. Re-issuing invalidates earlier passwords.
  - `accounts:password_change` (any logged-in user): current password, new password twice, validators; on success `update_session_auth_hash`, clear `must_change_password`, audit `auth.password_changed`.
  - `ForcePasswordChangeMiddleware`: a logged-in user with `must_change_password` is redirected to `accounts:password_change` on every request except that page, logout and static. `/api/` requests get 403 with the §6.5 body, code `PASSWORD_CHANGE_REQUIRED`.
  - **Email reset links** (extends P1's reset flow):
    - `reset_email_for(user) -> str | None` — the student's `guardian_email` (falling back to `guardian2_email`), or the staff member's own email.
    - On a reset request, if the matched active user has a reset email, send a link `{school base URL}/accounts/reset/link/<uidb64>/<token>/` using Django's `default_token_generator` (one-use; expires with `PASSWORD_RESET_TIMEOUT`, which for reset links is **1 hour** via a dedicated token generator subclass with `timeout = 3600`). Audit `auth.password_reset_link_sent`. If SMS is configured and a phone exists, also send the P1 code.
    - The reset request page is now always available (email works without an SMS provider); the login page always shows "Forgot password?". The response stays identical whether or not an account or contact exists, and P1's rate limits still apply.
    - `accounts:reset_link_confirm` follows the P1 invite pattern: token moved to the session, redirect to a token-less URL, `Referrer-Policy: no-referrer`; set a new password; clears `must_change_password`; audits `auth.password_reset`; redirects to login (never logs in).
    - Email subject: `"{school name}: reset the password for {first name}"` — the body names the student and admission number so a parent with several children knows which one.

- [ ] **Step 1: Write failing tests:**
  - issuing passwords for 2 students renders 2 passwords from the alphabet; the student logs in, is forced to the change page, changes it, then `home` loads; the old password fails; `no-store`; audit without the password; GET → 405; teacher → 403.
  - the API returns 403 `PASSWORD_CHANGE_REQUIRED` while the flag is set.
  - reset link for a student goes to the guardian email, names the student, and resets only that student;
  - *(Review Focus 5)* two siblings share a guardian email: a reset for child A's admission number changes only child A's password; child B still logs in with the old one;
  - reset link for staff goes to their own email; an unknown identifier and a student with no guardian email get the identical page and no email;
  - the link works once, fails after use, and fails after 1 hour (patch time);
  - with no SMS backend the reset page still works and the login page shows the link.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/students/test_passwords.py tests/accounts -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(accounts): one-time passwords, forced change and email reset links`.

### Task 9: Student lockout protection

Admission numbers are sequential and guessable, so anyone could lock out any student with 5 bad passwords every 15 minutes (P1 review).

**Files:**
- Modify: `apps/accounts/views/login.py`, `config/settings.py`
- Test: `tests/accounts/test_login.py`

**Interfaces:**
- Produces:
  - For identifiers that classify as `admission`, the per-identifier lockout key becomes per **(identifier, IP)**: `login:id-ip:<norm>:<ip>`, at 5 / 900 s.
  - A second, looser per-identifier limit applies: `STUDENT_IDENTIFIER_FAILURE_LIMIT = 20` / 900 s.
  - Staff identifiers keep the P1 rules unchanged.
  - Audit `auth.lockout` with `by` = `identifier_ip`, `identifier` or `ip`.

- [ ] **Step 1: Write failing tests:**
  - 5 bad attempts on `GFS/2026/0001` from IP X block X;
  - the real student from IP Y still logs in;
  - 20 failures across many IPs block the identifier everywhere;
  - staff behaviour is unchanged (existing tests still pass).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/accounts/test_login.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `fix(accounts): per-IP lockout for student identifiers`.

### Task 10: Import jobs and student import

**Files:**
- Create: `apps/imports/{__init__,apps,models,parsing,students,tasks,forms,views,urls}.py`, templates `imports/new.html`, `imports/detail.html`, migration, static template files `apps/imports/static/imports/students_template.csv` and `.xlsx`
- Modify: `config/settings.py` (`TENANT_APPS`), `config/urls_tenant.py` (`imports/`), `pyproject.toml` (`openpyxl`)
- Test: `tests/imports/test_parsing.py`, `tests/imports/test_student_import.py`

**Interfaces:**
- Produces:
  - `ImportJob`:
    - `kind` ∈ {`students`, `staff`};
    - `status` ∈ {`queued`, `running`, `completed`, `failed`};
    - `file_name` (private storage, `imports/…`);
    - `total_rows`, `ok_rows`, `error_rows`;
    - `errors` (JSON list of `{"row": int, "column": str, "message": str}`; no cell values for sensitive columns);
    - `created_by`, `created_at`, `finished_at`.
  - `parsing.read_rows(fileobj, filename) -> Iterator[tuple[int, dict[str, str]]]`:
    - CSV (UTF-8 with or without BOM) and XLSX (first sheet, read-only mode);
    - the header row maps case- and space-insensitively to canonical column names;
    - row numbers are 1-based and include the header.
    - Cell normalisers:
      - `cell_text(value) -> str`: floats like `8031234567.0` → `"8031234567"`; `None` → `""`.
      - `cell_date(value) -> date`: Excel `datetime`/`date`, `DD/MM/YYYY`, `D/M/YYYY`, `YYYY-MM-DD`; anything else raises.
      - `cell_phone(value) -> str`: `cell_text`, then if the result is 10 digits starting 7/8/9, prefix `0`, then `normalise_ng_phone`.
    - Limits: at most **2,000 rows**, file at most **5 MB**, extensions `.csv` and `.xlsx` only.
  - Student columns:

    | Column | Required? |
    |---|---|
    | `first_name`, `last_name`, `sex`, `date_of_birth`, `class`, `guardian_name` | required |
    | `guardian_phone`, `guardian_email` | at least one |
    | `middle_name`, `admission_number`, `guardian_relationship`, `guardian2_name`, `guardian2_phone`, `guardian2_email`, `guardian2_relationship` | optional |

    - Imported students get one-time passwords; the job keeps no passwords. After the import, staff use `students:passwords` for the class to print them.

    - `class` is matched with `ClassArm.find_by_display` against the current session.
    - The consent method for imported rows is `paper_form`, recorded by the importer.
  - `students.import_students(job) -> None`:
    - validates each row;
    - admits good rows through `admit_student`, each row in its own savepoint;
    - a row that would exceed the plan limit becomes an error row: "Plan limit reached (N students)";
    - duplicate admission numbers within the file are errors after the first.
  - `tasks.run_import` (`TenantTask`, queue `default`, `job_id: int`):
    - running → completed (or failed on an unexpected error, logged);
    - audits `import.started` and `import.completed` (counts only).
  - Pages (school_admin):
    - `imports:new`: upload, kind selection, template download links;
    - `imports:detail/<pk>`: status (meta refresh while running), counts, an errors table, and an "errors.csv" download.

- [ ] **Step 1: Write failing tests:**
  - CSV with a BOM, XLSX built in the test with `openpyxl`, header variants (`First Name`, `first_name`, ` FIRST_NAME `);
  - *(Review Focus 1)* XLSX phone cells `8031234567` (int) and `8031234567.0` (float) → `+2348031234567`; an XLSX date cell imports as that date; `5/9/2014` → 5 September 2014;
  - 2,001 rows → the job fails with "too many rows";
  - a 6 MB file is rejected at upload;
  - a `.xls` file is rejected;
  - a mixed file with 3 good rows and 2 bad (unknown class, bad phone) → 3 students, 2 errors with row numbers 4 and 6 and column names;
  - *(Review Focus 4)* a plan with 2 slots left and 4 good rows → 2 saved, 2 errors "Plan limit reached", status completed;
  - **performance**: 500 valid rows complete in under 60 s (`time.monotonic`; mark `@pytest.mark.slow` but run it in CI);
  - the uploaded file goes to `type="authenticated"` storage;
  - a teacher gets 403 on the pages.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/imports -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(imports): background CSV/XLSX student import with row-level errors`.

### Task 11: Staff import

**Files:**
- Create: `apps/imports/staff.py`, static `staff_template.csv` and `.xlsx`
- Modify: `apps/imports/tasks.py`, `views.py`
- Test: `tests/imports/test_staff_import.py`

**Interfaces:**
- Consumes: `invite_staff(..., verified=...)` from P1.
- Produces:
  - Staff columns:

    | Column | Required? |
    |---|---|
    | `full_name` | required |
    | `email`, `phone` | at least one |
    | `roles` | optional; comma-separated role names, default `teacher` |

  - `import_staff(job, *, verified: bool)`:
    - each good row is invited with `base_url` from `PLATFORM_URL_TEMPLATE`;
    - rows asking for MFA-required roles are errors when the job was started from a session that was not OTP-verified. `verified` is stored on the job at upload.
    - duplicate email or phone gives a row error.
    - SMS invites are sent only when SMS is configured, as in P1.

- [ ] **Step 1: Write failing tests:**
  - 3 rows → 3 invites (emails in outbox);
  - unknown role → row error;
  - a bursar row from an unverified proprietor's upload → row error; from a verified admin → invited;
  - a duplicate phone → row error;
  - the row numbers are right.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/imports/test_staff_import.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(imports): staff import via invites`.

### Task 12: API endpoints and the student portal page

**Files:**
- Create: `apps/api/{__init__,apps,urls,views,serializers}.py` (move `me` here from `apps/accounts/api.py`), template `students/portal_profile.html`
- Modify: `config/urls_tenant.py` (`api/v1/` → `apps.api.urls`, namespace `api`), `apps/accounts/views/home.py` (students see their profile card), `tests/isolation/registry.py`
- Test: `tests/api/test_students_api.py`, `tests/api/test_imports_api.py`, `tests/api/test_me_api.py` (moved), `tests/students/test_portal.py`

**Interfaces:**
- Produces:
  - `GET /api/v1/me` (`api:me`) — P1 fields plus `"current_class": {"arm": "JSS 1A", "session": "2026/2027"} | null` for students.
  - `GET /api/v1/students?class_arm=<id>&q=` (`api:students`) — the §6.2 list over `students_visible_to`:
    - paginated `?page=&page_size=` (max 100);
    - items `{"id", "admission_number", "full_name", "class": "JSS 1A" | null}`;
    - never DOB, photo or guardian data.
  - `POST /api/v1/students/import` (`api:students_import`):
    - school_admin; multipart file;
    - responds 202 `{"job_id"}`.
  - `GET /api/v1/imports/<id>` (`api:import_detail`):
    - school_admin;
    - `{"id", "kind", "status", "total_rows", "ok_rows", "error_rows", "errors": [...]}`;
    - 404 for another school's job (separate schema) and for unknown ids.
  - Isolation cases (Task 1's authenticated actor), one per route + method:
    - `api:students GET` with `expect="absent"` and `marker=lambda s: s.admission_number`;
    - `api:students_import POST`;
    - `api:import_detail GET`;
    - `api:me GET`.
  - Student portal: a student's `home` shows their name, admission number, class, current term, school branding, and a "Change password" link. Guardian contacts are not shown to students.

- [ ] **Step 1: Write failing tests:**
  - the students list as admin, teacher (own arm only) and student (403);
  - *(Review Focus 3)* the teacher's `?class_arm=` for an arm they don't teach → empty list;
  - `page_size=1000` → capped at 100;
  - import POST as admin → 202 and a job exists; as teacher → 403;
  - import detail as admin; another school's job id → 404;
  - `/me` for a student includes `current_class`;
  - the portal home shows the class and admission number;
  - the isolation guard passes with every route registered;
  - the authenticated cross-school cases pass.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/api tests/students tests/isolation -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; then the full CI set: `ruff check`, `ruff format --check`, `mypy apps config`, `makemigrations --check --dry-run`, `pytest -W error -m "not cloudinary"`.
- [ ] **Step 5: Commit** `feat(api): students, imports and me endpoints; student portal profile`.

---

## Exit criteria for P2 (roadmap)

- A pilot school's real class lists import cleanly: students, guardian contacts and classes.
- Staff hand out one-time passwords, and each student can log in, change their password and see their profile and class; a forgotten password is reset by an emailed link to the guardian.
- Teachers see only their own classes, in pages and the API.
- Every `api/v1/` route has an **authenticated** cross-school isolation case.

## Decisions left for later phases

- Promotion, graduation and alumni (UC-16, FR-17) → P8 (must ship before July 2027).
- Electives per student (choosing a subset of non-compulsory offerings) → P3, where score sheets need it.
- Timetables → Version 2.
- Teacher scope by role for `principal` and `bursar` beyond read-all → revisit with the P3 results workflow.
- Tailwind/HTMX build → still deferred. P2 keeps the small stylesheet, adding a table wrapper and form grid. Revisit when P3's score grid needs it.
