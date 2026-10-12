# P3 Results and Report Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teachers enter continuous-assessment and exam scores in a spreadsheet-like grid. The school reviews, approves and publishes each class. The system computes totals, grades and positions, which must match a hand-calculated broadsheet exactly. Students then download branded PDF report cards.

**Architecture:** A new tenant app, `apps.results`, holds:
- assessment components and grading scales (per section);
- elective choices;
- scores with optimistic versioning;
- sheet and class workflow states;
- comments, ratings and attendance totals;
- computed summaries and report-card records.

Supporting pieces:
- **Computation:** pure functions in `apps/results/compute.py`, run by a Celery `TenantTask` that writes summaries atomically.
- **PDFs:** rendered with WeasyPrint from a Django template, on the `reports` queue, and stored as private raw Cloudinary files.
- **Score grid:** a server-rendered page plus a small vanilla-JS module that saves cells through the JSON API.

**Tech Stack:** The P2 stack, plus `weasyprint` (system Pango in Docker/CI; per-user Pango for local development via micromamba).

**Spec:**
- `School Management Platform — System Design.pdf`: §4.3 (results and report cards), §5.2 (`assessment_component`, `score`, `result_summary`), §5.3/§5.4 (score index, optimistic locking), §6.2 (`/scores`, `/results/{class_arm}/approve`, `/results/{student}/{term}/report-card`), §7.1 matrix, §7.5 audit, §8.1 (`reports` queue), §8.2 concurrency.
- Requirements: FR-06, FR-07, FR-08, FR-09; UC-07, UC-08, UC-09.
- `docs/ROADMAP.md` phase P3. Note roadmap §1.2: students see their own results; there is no parent account, and guardians are notified by email/SMS.
- Out of scope: offline score entry (P9), fee-based result blocking UC-17 (P4), attendance capture (P6).

## Global Constraints

All P0–P2 Global Constraints still hold. Added for P3:

**Score structure (design §4.3)**
- **Assessment components are defined per section** (`nursery`, `primary`, `jss`, `sss`) with a name, a max score and an order.
  - A section's components must total **exactly 100** before any sheet in that section can be submitted.
  - Default for every section: `CA1 10`, `CA2 10`, `Assignment 10`, `Test 10`, `Exam 60`.
- **Grading scales are per section**: contiguous whole-number bands covering 0–100, each with a grade and a remark.
  - Default for `sss` (WAEC style): `A1 75–100 Excellent`, `B2 70–74 Very good`, `B3 65–69 Good`, `C4 60–64 Credit`, `C5 55–59 Credit`, `C6 50–54 Credit`, `D7 45–49 Pass`, `E8 40–44 Pass`, `F9 0–39 Fail`.
  - Default for `nursery`, `primary`, `jss`: `A 70–100 Excellent`, `B 60–69 Very good`, `C 50–59 Good`, `D 45–49 Fair`, `E 40–44 Poor`, `F 0–39 Fail`.
- **A score** belongs to one student, subject offering, term and component.
  - It is a non-negative decimal with at most **1 decimal place**, no more than the component's max.
  - `null` means "not entered". The teacher enters `0` for an absent student.
  - Unique `(student, offering, term, component)`.

**Who takes a subject**
- A **compulsory** offering covers every student with an active enrolment in the arm for that session.
- An **elective** (`is_compulsory=False`) covers only students with an `ElectiveChoice` for it.

**Computation rules (FR-07)** — must match the accuracy dataset in Task 7 exactly:
- **Subject total:** the sum of the component scores, kept to 1 decimal place.
- **Grade and remark:** from the band containing the total rounded **half-up to a whole number**.
- **Subject position:** competition ranking (`1, 2, 2, 4`) by total among students taking that subject in the arm.
- **Subject statistics:** highest, lowest and class average (2 dp, half-up) of that subject's totals.
- **Student total:** the sum of their subject totals.
- **Student average:** that sum ÷ the number of subjects taken, rounded **half-up to 2 dp**.
- **Class position:** competition ranking by the rounded average, among active students in the arm. "Out of" is the number of ranked students.
- **Cumulative (3rd term only):**
  - per subject: the mean of the term totals the student has for that subject in the session (1st/2nd/3rd, only the terms present), 2 dp;
  - cumulative average: the mean of those per-subject cumulatives, 2 dp.
- Withdrawn students are excluded from ranking, the "out of" count and statistics.

**Workflow (design §4.3, FR-08)**
1. A **score sheet** `(offering, term)` is `draft` → `submitted`. The assigned teacher submits, which requires every cell for every student taking the subject to be filled. The teacher can recall the sheet while the class is still `draft`.
2. A **class result** `(arm, term)` is `draft` → `reviewed` → `approved` → `published`.
   - **Reviewed:** done by the form teacher. Requires every sheet in the arm to be `submitted` and a class-teacher comment for every active student.
   - **Approved:** done by the principal. Triggers computation.
   - **Published:** done by the principal or school admin. Triggers report-card PDFs and guardian notifications.
3. Scores are **editable only while their sheet is `draft` and the class is `draft`**. Any other edit is refused with **409** `RESULTS_LOCKED` and audited as `results.edit_refused`.
4. **Unlock** is done by a school admin and requires a reason. It returns the class to `draft` and the chosen sheets to `draft`, and is audited as `results.unlocked` with the reason.
5. Every score change after a sheet's first submission is audited as `results.score_changed`. Before/after hold component id, student id and values only.

**Permissions (FR-06, §7.1)**
- **Score entry:** only the teacher assigned to the offering. Any other staff member gets **403** (FR-06); students get 403.
- **Comments:** the form teacher writes the class-teacher comment; the principal writes the principal comment.
- **Ratings and attendance totals:** the form teacher.
- **Broadsheet:** visible to school admin, principal, proprietor and the arm's form teacher.
- **Report cards:**
  - a student sees their own, only once their class is `published`;
  - staff see them through `students_visible_to`.

**Concurrency (§5.4, §8.2)**
- Each score cell has a `version`. A save sends the version it read. On a mismatch the whole request is refused with **409** `VERSION_CONFLICT`, listing each conflicting cell's current value and version. Nothing in that request is saved.
- Result summaries are written in **one transaction per class** (delete and insert), so readers never see a half-computed class.

**Report cards (FR-09)**
- PDFs show:
  - school branding (logo, colours, motto, address);
  - student name, admission number, class, session, term;
  - attendance (days present / days school opened);
  - a subject table: components, total, grade, remark, subject position, highest/lowest/class average;
  - total, average, class position "out of N";
  - cumulative columns in the 3rd term;
  - affective and psychomotor ratings;
  - both comments;
  - the next term's start date (if a later term exists);
  - the grading key.
- Generated in the background on the `reports` queue and stored with `type="authenticated"` at `report_cards/<session>/<term>/<student pk>.pdf`, overwritten on regeneration.
- **A class of 50 must finish in under 2 minutes.**
- PDFs must never fetch arbitrary URLs. The renderer's URL fetcher allows only the school's own public logo URL.

**Notifications**
- On publish, email each student's guardian(s), and send SMS if configured, the message: "Results for {term} are ready. Log in to the student portal to view them."
- **No scores in emails or SMS.**
- Sent through a `TenantTask` with redacted args.

**New audit actions**
- `results.components_changed`, `results.scale_changed`, `results.elective_changed`
- `results.score_changed`, `results.edit_refused`
- `results.sheet_submitted`, `results.sheet_recalled`
- `results.class_reviewed`, `results.class_approved`, `results.class_published`, `results.unlocked`
- `results.comment_changed`, `results.ratings_changed`
- `results.report_cards_generated`, `results.published_notified`

## Review Focus

1. **Ties.**
   - Equal subject totals or equal rounded averages share a position, and the next position is skipped (`2nd, 2nd, 4th`).
   - Tested by the accuracy dataset (Task 7).
2. **Two teachers or tabs saving the same cell.**
   - The second gets 409 with the current value, and nothing is silently overwritten.
   - Test in Task 4.
3. **Bad cell input in one save:** a value above the max, negative, non-numeric, or with 2 decimals.
   - That save is refused with a per-cell message, and nothing in the request is saved.
   - Test in Task 4.
4. **A student withdrawn or admitted mid-term.**
   - A withdrawn student is excluded from positions and "out of".
   - A newly enrolled student's blank cells block submission until filled.
   - Test in Tasks 4/7.
5. **Editing after publish through the grid, the API or a CSV upload.**
   - Refused with 409 and audited.
   - Test in Tasks 4/5/8.

---

## File Structure

```
scripts/dev_pango.sh, scripts/with-pango                    # local WeasyPrint toolchain
apps/core/pdf.py                                            # render_pdf(html, *, allowed_urls) -> bytes
apps/results/
  models.py      # AssessmentComponent, GradeBand, ElectiveChoice, Score, ScoreSheet, ClassResult,
                 # ReportComment, Trait, Rating, SubjectResult, ResultSummary, ReportCard
  seeding.py     # default components, scales, traits
  scope.py       # students_taking(offering, term), can_enter_scores(user, offering)
  scores.py      # save_cells(), sheet_rows()
  compute.py     # pure computation (no ORM writes) + compute_class(arm, term)
  workflow.py    # submit/recall sheet, review/approve/publish/unlock class
  cards.py       # build context, render, store report cards
  tasks.py       # compute_class_task, generate_report_cards_task, notify_published_task
  views/  settings.py grid.py comments.py workflow.py broadsheet.py cards.py portal.py
  templates/results/*, static/results/scoregrid.js
apps/api/        # + scores, results approve, report-card endpoints
```

---

### Task 1: Carry-overs and PDF toolchain

**Files:**
- Create: `scripts/dev_pango.sh`, `scripts/with-pango`, `apps/core/pdf.py`
- Modify: `pyproject.toml` (`weasyprint`), `Dockerfile` (apt `libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core`), `.github/workflows/ci.yml` (apt the same), `README.md`, `.gitignore` (`.devlibs/`), `apps/imports/tasks.py`, `docs/superpowers/plans/2026-10-11-p2-academics-students.md` (add `import.failed_stale` to the audit list)
- Test: `tests/core/test_pdf.py`, `tests/imports/test_fail_stale.py`

**Interfaces:**
- Produces:
  - `scripts/dev_pango.sh`: installs Pango per user via micromamba into `.devlibs/pango` (idempotent, prints what it did).
  - `scripts/with-pango CMD…`: runs CMD with `DYLD_FALLBACK_LIBRARY_PATH` set to `.devlibs/pango/lib` on macOS (a no-op elsewhere). From this task on, local test commands are `scripts/with-pango .venv/bin/pytest …`.
  - `render_pdf(html: str, *, allowed_urls: frozenset[str] = frozenset()) -> bytes`. It uses WeasyPrint with a `url_fetcher` that raises for any URL not in `allowed_urls`. `data:` URIs are allowed.
  - `run_import`'s final save becomes conditional: `ImportJob.objects.filter(pk=…, status="running").update(...)`. If `fail_stale_imports` already failed the job, the worker leaves it failed.
- Tests are marked `pdf`; the marker is registered. CI runs them. Locally they need `with-pango`, and a missing Pango makes them **fail** with a clear message, never skip silently.

- [ ] **Step 1: Write failing tests:**
  - `render_pdf("<h1>Hi</h1>")` returns bytes starting with `%PDF`.
  - An `<img src="https://evil.example/x.png">` makes the fetch raise. Assert the fetcher was asked and refused, and the PDF still renders without the image.
  - An allowed URL is fetched (patch the network fetch).
  - Conditional final save: a job failed by `fail_stale_imports` while "running" stays `failed` after `run_import` finishes.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest tests/core/test_pdf.py tests/imports -W error` → FAIL.
- [ ] **Step 3: Implement**, including README local and production notes.
- [ ] **Step 4: Run** → PASS; full suite green under `with-pango`.
- [ ] **Step 5: Commit** `chore: weasyprint toolchain, safe pdf renderer, import final-save guard`.

### Task 2: Assessment components and grading scales

**Files:**
- Create: `apps/results/{__init__,apps,models,seeding}.py`, migration, `apps/results/views/settings.py`, `apps/results/forms.py`, `apps/results/urls.py`, templates `results/settings.html`
- Modify: `config/settings.py` (`TENANT_APPS`), `config/urls_tenant.py` (`results/`, namespace `results`)
- Test: `tests/results/test_settings.py`, `tests/results/test_seeding.py`, `tests/results/factories.py`

**Interfaces:**
- Produces:
  - `AssessmentComponent(section, name, max_score int 1–100, order)`, unique `(section, name)`.
  - `GradeBand(section, lower int, upper int, grade, remark)`.
  - `components_for(section) -> list[AssessmentComponent]`.
  - `components_complete(section) -> bool`: true when the section's max scores total exactly 100.
  - `grade_for(section, total: Decimal) -> GradeBand`: rounds half-up to an integer, then finds the containing band.
  - `validate_scale(bands) -> None`: bands must be contiguous, cover 0–100, with no overlap.
  - Seeder `seed_results(school)`, registered with `register_seeder`. It is one-time like academics: defaults are created only when none exist. Covers components and scales for the school's `levels_offered`, plus default traits (Task 6 defines `Trait`; seed traits there).
  - Page `results:settings` (school_admin edits; principal and proprietor read). It shows components and the scale per section.
    - **Components can't be changed for a section once any score exists in the current session** (message explains why).
    - Saving audits `results.components_changed` or `results.scale_changed` with names and numbers only.
  - Management command `seed_results_defaults [--schema X]`, mirroring `seed_academics_defaults`.

- [ ] **Step 1: Write failing tests:**
  - The defaults exactly as in Global Constraints.
  - `components_complete` is true for the default and false for 10+60.
  - `grade_for(sss, 74.5)` → `A1`; `grade_for(sss, 74.4)` → `B2`; `grade_for(jss, 69.5)` → `A`; `grade_for(jss, 0)` → `F`.
  - Scales with a gap, an overlap, or one not reaching 100 → `ValidationError`.
  - A teacher → 403.
  - Editing components after a score exists is refused. Use a `Score` created via the Task 4 model? No: this task has no Score model. Gate the check with a `has_scores(section)` hook that returns False until Task 4 replaces it, and test the gate by patching the hook.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest tests/results -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): assessment components and grading scales per section`.

### Task 3: Elective choices and who takes a subject

**Files:**
- Modify: `apps/results/models.py`, migration; `apps/academics/templates/academics/arm_detail.html`, `apps/academics/views.py` (an "Electives" link per non-compulsory offering)
- Create: `apps/results/scope.py`, `apps/results/views/electives.py`, template `results/electives.html`
- Test: `tests/results/test_electives.py`

**Interfaces:**
- Produces:
  - `ElectiveChoice(student FK, offering FK)`, unique `(student, offering)`. The offering must be non-compulsory, and the student must have an active enrolment in the offering's arm for the offering's session.
  - `students_taking(offering) -> QuerySet[Student]`, active students only:
    - compulsory: active enrolment in `offering.arm` for `offering.session`;
    - elective: those with an `ElectiveChoice`.
  - `results:electives/<offering_pk>` (school_admin or the arm's form teacher): checkbox list of the arm's active students. Saving is atomic and audits `results.elective_changed` with ids only.

- [ ] **Step 1: Write failing tests:**
  - A compulsory offering covers all 3 active students and excludes a withdrawn one.
  - An elective covers only the 2 chosen.
  - A choice for a student in another arm → `ValidationError`.
  - A choice on a compulsory offering → `ValidationError`.
  - A teacher who is not the form teacher → 403.
  - The form teacher can save; the save is audited.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): elective choices and students taking a subject`.

### Task 4: Scores, versioned saves and the scores API

**Files:**
- Modify: `apps/results/models.py` (+ `Score`, `ScoreSheet`, `ClassResult`), migration; `apps/results/scope.py` (`can_enter_scores`); replace Task 2's `has_scores` hook
- Create: `apps/results/scores.py`; `apps/api/` additions (views, serializers, urls)
- Modify: `tests/isolation/registry.py`
- Test: `tests/results/test_scores.py`, `tests/api/test_scores_api.py`

**Interfaces:**
- Produces:
  - Models:
    - `Score(student, offering, term, component, value Decimal(4,1) null, version int default 0, entered_by FK, updated_at)`, unique `(student, offering, term, component)`, index `(offering, term)`.
    - `ScoreSheet(offering, term, status ∈ {draft, submitted}, first_submitted_at null, submitted_by, submitted_at)`, unique `(offering, term)`.
    - `ClassResult(arm, term, status ∈ {draft, reviewed, approved, published}, timestamps + actor FKs per step)`, unique `(arm, term)`. Rows are created on demand.
  - `can_enter_scores(user, offering) -> bool`: true when the user is active staff with the `teacher` role and is `offering.teacher`.
  - `sheet_rows(offering, term) -> SheetData`, holding:
    - the components;
    - one row per student taking the subject (student id, name, admission number, and per component a `value` and `version`; missing cells show `value=None`, `version=0`);
    - the sheet status and class status;
    - `editable: bool`.
  - `save_cells(offering, term, cells: list[CellInput(student_id, component_id, value: str | None, version: int)], *, by, request=None) -> SaveResult`:
    1. Inside `school_lock(f"scores:{offering.pk}:{term.pk}")` + `atomic()`.
    2. Refuse with `ResultsLocked` (→ 409 `RESULTS_LOCKED`, and audit `results.edit_refused`) unless the sheet is `draft` and the class is `draft`.
    3. Validate every cell before writing anything:
       - the student is in `students_taking`;
       - the component belongs to the arm's section;
       - the value parses as a Decimal with ≤ 1 dp, 0 ≤ value ≤ max, or is `null`.
       - Invalid → `ValidationError` keyed `"<student_id>:<component_id>"` (→ 400 with per-cell `details`).
    4. Check versions: any mismatch → `VersionConflict` (→ 409 `VERSION_CONFLICT`, details list `{student, component, value, version}` current). Nothing is saved.
    5. Upsert the changed cells and increment the version.
    6. If the sheet was ever submitted, audit `results.score_changed` per changed cell (ids and old/new values only).
    7. Return the new versions.
  - API:
    - `GET /api/v1/scores?offering=&term=` (`api:scores`) → `sheet_rows` as JSON.
    - `PUT` on the same URL with `{"cells": [...]}` → 200 `{"versions": [...]}`, or the 400/409 bodies.
    - Only `can_enter_scores` users may use either; other staff get 403 (FR-06), students get 403, and an unknown offering gets 404.
  - Isolation cases: `api:scores GET` and `api:scores PUT` (refused), each with an offering created in school B.

- [ ] **Step 1: Write failing tests:**
  - A GET lists exactly the students taking the subject, with blank cells at version 0.
  - A PUT of 3 valid cells saves them, with versions 1.
  - *(Review Focus 2)* Two clients read version 1. The first PUT succeeds (version 2). The second, sending version 1, gets 409 with the current value, and its other cells in the same request are not saved.
  - *(Review Focus 3)* One PUT containing `11` for a /10 component, `-1`, `"abc"` and `"7.25"` → 400 with 4 per-cell details, and nothing saved, including the valid cells in that request.
  - A teacher not assigned to the offering → 403 for both GET and PUT (FR-06). A principal → 403 too, because score entry is the assigned teacher's.
  - With the sheet `submitted` → 409 `RESULTS_LOCKED`, plus a `results.edit_refused` audit.
  - *(Review Focus 4)* A student enrolled after scores exist appears with blank cells. A withdrawn student is not listed and their saves are refused.
  - The isolation guard passes.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest tests/results/test_scores.py tests/api/test_scores_api.py tests/isolation -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): versioned score saves and scores API`.

### Task 5: Score grid page and CSV round-trip

**Files:**
- Create: `apps/results/views/grid.py`, template `results/grid.html`, `apps/results/static/results/scoregrid.js`, `apps/results/csvsheet.py`
- Modify: `apps/results/urls.py`; home/teacher links ("My score sheets")
- Test: `tests/results/test_grid.py`, `tests/results/test_csvsheet.py`

**Interfaces:**
- Produces:
  - `results:my_sheets`: the teacher's offerings for the current session and term, each with its sheet and class status.
  - `results:grid/<offering_pk>` (`can_enter_scores`, else 403):
    - **Layout:** a table with one row per student and one column per component, plus a live total column.
    - **Editing:** inputs only when `editable`. Enter or the down arrow moves down; Tab moves right.
    - **Saving:** a cell saves on blur via `PUT /api/v1/scores` with the version, sending the CSRF header.
    - **Errors:** 409 or 400 marks the cell with the message, and on a version conflict shows the server's current value.
    - **Status:** a "not yet saved" indicator per row.
    - **Assets:** `scoregrid.js` is vanilla JS, ≤ 8 KB, no framework. The page works at 360 px with horizontal scroll.
  - `results:grid_csv/<offering_pk>` (GET): downloads the sheet as CSV with the columns `admission_number, name, <component names…>`, guarded against formula injection.
  - `results:grid_upload/<offering_pk>` (POST):
    - accepts a CSV in the same shape and parses it into cells using the **current** versions;
    - calls `save_cells` once (all-or-nothing);
    - shows per-row errors;
    - rows are matched by admission number;
    - unknown admission numbers or extra columns are errors.

- [ ] **Step 1: Write failing tests:**
  - The grid renders inputs for each student and component when editable, and read-only cells when the sheet is submitted.
  - An unassigned teacher → 403.
  - A CSV download/upload round trip with changed values saves them.
  - An upload with an unknown admission number or a value of 11 for a /10 component → errors, and nothing saved.
  - *(Review Focus 5)* An upload after publish → refused, plus a `results.edit_refused` audit.
  - CSV values starting with `=` are written prefixed with `'`.
  - A JS smoke test is not required. Assert the script tag and the `data-` attributes the script needs (API URL, CSRF token, offering, term).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): score grid page and CSV round trip`.

### Task 6: Comments, ratings and attendance totals

**Files:**
- Modify: `apps/results/models.py` (+ `ReportComment`, `Trait`, `Rating`), migration, `apps/results/seeding.py` (traits)
- Create: `apps/results/views/comments.py`, templates `results/class_comments.html`
- Test: `tests/results/test_comments.py`

**Interfaces:**
- Produces:
  - `ReportComment(student, term, class_teacher_comment ≤ 500, principal_comment ≤ 500, days_present int null, days_open int null, updated_by, updated_at)`, unique `(student, term)`. Constraint: `0 ≤ days_present ≤ days_open ≤ 200`.
  - `Trait(kind ∈ {affective, psychomotor}, name, order)`, seeded:
    - affective: Punctuality, Neatness, Politeness, Honesty, Attentiveness, Relationship with others;
    - psychomotor: Handwriting, Sports, Drawing, Musical skills, Verbal fluency.
  - `Rating(student, term, trait, value 1–5)`, unique `(student, term, trait)`.
  - `results:class_comments/<arm_pk>`:
    - the form teacher edits class-teacher comments, ratings, days present/open (with one "days school opened" value applied to all), for the current term;
    - the principal edits principal comments on the same page;
    - edits are allowed only while the class is `draft` (the principal comment also while `reviewed`);
    - saving is atomic and audits `results.comment_changed` / `results.ratings_changed` with ids only (no comment text).
  - Other staff → 403. Students → 403.

- [ ] **Step 1: Write failing tests:**
  - The form teacher saves comments, ratings and attendance for 3 students.
  - The principal saves principal comments.
  - A teacher who is not the form teacher → 403.
  - `days_present > days_open` → form error.
  - A rating of 6 → form error.
  - Edits after `approved` → refused.
  - The audit holds no comment text.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): comments, ratings and attendance totals`.

### Task 7: Computation engine and accuracy check

**Files:**
- Create: `apps/results/compute.py`; modify `apps/results/models.py` (+ `SubjectResult`, `ResultSummary`), migration
- Test: `tests/results/test_compute_unit.py`, `tests/results/test_accuracy.py`

**Interfaces:**
- Produces:
  - Pure functions (no DB):
    - `subject_total(values: list[Decimal]) -> Decimal` (1 dp);
    - `competition_rank(values: dict[K, Decimal]) -> dict[K, int]` (higher is better; ties share a position; the next position is skipped);
    - `round_avg(total: Decimal, n: int) -> Decimal` (2 dp half-up);
    - `ordinal(n) -> str` (`1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st, 22nd, 101st, 111th`).
  - `SubjectResult(student, offering, term, total, grade, remark, position, highest, lowest, class_average, cumulative null)`.
  - `ResultSummary(student, term, arm, subjects_count, total, average, position, out_of, cumulative_average null, computed_at)`.
  - `compute_class(arm, term) -> None`:
    - reads scores for all offerings of the arm in the term's session;
    - computes per the Global Constraints rules;
    - in **one** `atomic()`, deletes and recreates this class's `SubjectResult` and `ResultSummary` rows;
    - raises `ValidationError` if any offering's sheet is not `submitted` or any section's components are incomplete.
  - In the 3rd term, cumulative values come from the same student's `SubjectResult.total` for the session's other terms. Where an earlier term has no computed result, use the scores directly.

- [ ] **Step 1: Write failing tests:**
  - **Unit tests:**
    - `competition_rank({a:80,b:80,c:60})` → `{a:1,b:1,c:3}`;
    - `round_avg(Decimal(230),3)` → `76.67`;
    - `round_avg(Decimal("119"),3)` → `39.67`;
    - the ordinal cases above.
  - **Accuracy dataset (`test_accuracy.py`)** *(Review Focus 1)*:
    - **Setup:** JSS arm, components `CA 40` + `Exam 60` (a valid test override of the default), the JSS default scale. Students A–F take Mathematics, English Language and Basic Science. Scores (CA, Exam):

      | Student | Mathematics | English Language | Basic Science |
      |---|---|---|---|
      | A | 30, 50 | 28, 42 | 32, 48 |
      | B | 35, 45 | 30, 40 | 30, 40 |
      | C | 20, 40 | 35, 50 | 30, 45 |
      | D | 25, 30 | 20, 25 | 30, 30 |
      | E | 10, 20 | 15, 30 | 20, 24 |
      | F | 40, 58 | 25, 40 | 38, 55 |

    - **Expected subject totals / grade / position:**

      | Student | Mathematics | English Language | Basic Science |
      |---|---|---|---|
      | A | 80 A 2nd | 70 A 2nd | 80 A 2nd |
      | B | 80 A 2nd | 70 A 2nd | 70 A 4th |
      | C | 60 B 4th | 85 A 1st | 75 A 3rd |
      | D | 55 C 5th | 45 D 5th | 60 B 5th |
      | E | 30 F 6th | 45 D 5th | 44 E 6th |
      | F | 98 A 1st | 65 B 4th | 93 A 1st |

    - **Expected subject statistics:**
      - Mathematics: highest 98, lowest 30, average 67.17.
      - English Language: highest 85, lowest 45, average 63.33.
      - Basic Science: highest 93, lowest 44, average 70.33.
    - **Expected student total / average / position (out of 6):**
      - F 256 / 85.33 / 1st
      - A 230 / 76.67 / 2nd
      - B 220 / 73.33 / 3rd
      - C 220 / 73.33 / 3rd
      - D 160 / 53.33 / 5th
      - E 119 / 39.67 / 6th
    - **Withdrawal** *(Review Focus 4)*: withdraw E and recompute → out of 5. E has no summary. Statistics exclude E (Mathematics lowest becomes 55, average 74.60).
    - **Cumulative**, on a 3rd-term arm with one student:
      - Mathematics term totals 60/70/80 → cumulative 70.00;
      - English Language for 2nd/3rd only, 50/60 → cumulative 55.00;
      - cumulative average 62.50.
  - **Atomicity:** patch the insert to fail half-way. The previous summaries remain intact.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): computation engine matching the hand-calculated broadsheet`.

### Task 8: Workflow, approval API and unlock

**Files:**
- Create: `apps/results/workflow.py`, `apps/results/tasks.py` (`compute_class_task`), `apps/results/views/workflow.py`, template `results/class_status.html`
- Modify: `apps/api` (approve endpoint), `tests/isolation/registry.py`
- Test: `tests/results/test_workflow.py`, `tests/api/test_results_api.py`

**Interfaces:**
- Produces:
  - `submit_sheet(offering, term, *, by, request=None)`:
    - assigned teacher only;
    - every cell for every student taking the subject must be filled (*Review Focus 4*);
    - the section's components must be complete;
    - sets `first_submitted_at` on the first submit;
    - audit `results.sheet_submitted`.
  - `recall_sheet(...)`: assigned teacher, only while the class is `draft`; audit `results.sheet_recalled`.
  - `review_class(arm, term, *, by, request=None)`:
    - the arm's form teacher;
    - all sheets in the arm `submitted`;
    - every active student has a class-teacher comment;
    - → `reviewed`; audit `results.class_reviewed`.
  - `approve_class(...)`: principal; `reviewed` → `approved`; enqueue `compute_class_task` on commit (queue `default`); audit `results.class_approved`.
  - `publish_class(...)`:
    - principal or school admin;
    - requires `approved` and computed summaries present;
    - → `published`;
    - enqueue `generate_report_cards_task` (Task 9) and `notify_published_task` (Task 10) on commit;
    - audit `results.class_published`.
  - `unlock_class(arm, term, *, sheets: list[ScoreSheet], reason: str, by, request=None)`:
    - school admin;
    - `reason` is required (≤ 300 characters);
    - class → `draft`, the chosen sheets → `draft`;
    - delete the existing report-card records for that class (files deleted on commit);
    - audit `results.unlocked` with the reason and sheet ids.
  - Every transition takes `school_lock(f"results:{arm.pk}:{term.pk}")` + `atomic()` and checks the current state. A wrong state → `ValidationError` (409 at the API).
  - `results:class_status/<arm_pk>` shows each sheet's status and the class status, with the buttons valid for the current user and state.
  - `POST /api/v1/results/<class_arm>/approve` (`api:results_approve`): principal only, using the current term. Returns 202 `{"status": "approved"}` or §6.5 errors. Isolation case: refused.

- [ ] **Step 1: Write failing tests:**
  - **Happy path:** submit 3 sheets → review → approve (eager compute creates summaries) → publish.
  - Submitting with a blank cell → error listing the count of blank cells.
  - Recall after review → refused.
  - Review by a non-form teacher → 403.
  - Approve by a school admin who is not principal → 403.
  - Publish without summaries → error.
  - *(Review Focus 5)* After publish, the API PUT and the CSV upload → 409 + audit. Then unlock with a reason → the sheet is editable again, the edit is audited as `results.score_changed`, and the report cards are gone.
  - Unlock without a reason → error.
  - API approve: principal → 202; teacher → 403. The isolation guard passes.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): review, approval, publishing and unlock workflow`.

### Task 9: Report-card PDFs

**Files:**
- Create: `apps/results/cards.py`, template `results/report_card.html` (print CSS, A4), `apps/results/views/cards.py`
- Modify: `apps/results/models.py` (+ `ReportCard`), migration; `apps/results/tasks.py` (`generate_report_cards_task`, queue `reports`); `apps/api` (report-card endpoint); `tests/isolation/registry.py`
- Test: `tests/results/test_cards.py` (marked `pdf`), `tests/api/test_report_card_api.py`

**Interfaces:**
- Produces:
  - `ReportCard(student, term, file_name, generated_at, version)`, unique `(student, term)`.
  - `card_context(student, term) -> dict`: everything listed under Global Constraints "Report cards". The logo is passed as a `data:` URI, fetched once per batch through the storage and resized. Fallback: only the school's own public logo URL is in `allowed_urls`.
  - `render_card(student, term) -> bytes` via `render_pdf`.
  - `generate_report_cards(arm, term) -> int`:
    - renders and saves each published student's card to `report_cards/<session pk>/<term pk>/<student pk>.pdf` (authenticated, overwrite);
    - upserts `ReportCard`;
    - audits `results.report_cards_generated` with the count;
    - returns the count;
    - the storage quota applies; on `PlanLimitExceeded` it stops and logs, and the class page shows "Report cards could not be generated: storage limit".
  - `results:report_card/<student_pk>/<term_pk>`: 302 to a 10-minute signed URL, `no-store`.
    - Students: their own card only, and only when their class for that term is `published` (otherwise 404).
    - Staff: through `students_visible_to` (otherwise 404). A missing card → 404.
  - `GET /api/v1/results/<student>/<term>/report-card` (`api:report_card`): the same rules, returning 302 or §6.5 404. Isolation case: refused.
  - Template: A4 portrait, school colours from branding; the subject table repeats its header across pages. The 3rd term adds 1st/2nd/3rd term total columns and the cumulative.

- [ ] **Step 1: Write failing tests** (`pdf` marker; Cloudinary upload patched):
  - A generated card's PDF text contains:
    - the school name, the student name, the admission number;
    - "2nd" for a subject position and "3rd" with "out of 6" from the accuracy dataset;
    - each grade;
    - the principal comment.
  - Extract text with `pypdf` (dev dependency).
  - The PDF fetches no external URL (assert the fetcher saw no disallowed URL).
  - A student downloads their own card after publish (302 to a signed URL). Before publish → 404. Another student's card → 404. A teacher of another arm → 404.
  - **Performance:** 50 students in one class render and store in under 120 s (`@pytest.mark.slow`, `pdf`). Report the measured time.
  - The isolation guard passes.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest tests/results/test_cards.py tests/api/test_report_card_api.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(results): branded report-card PDFs and secure downloads`.

### Task 10: Broadsheet, publish notifications and the student results view

**Files:**
- Create: `apps/results/views/broadsheet.py`, `apps/results/views/portal.py`, templates `results/broadsheet.html`, `results/my_results.html`; `apps/results/tasks.py` (`notify_published_task`)
- Modify: `apps/accounts/views/home.py` (student portal: "My results" link), `apps/core/logging.py` (redaction list for the new task)
- Test: `tests/results/test_broadsheet.py`, `tests/results/test_notify.py`, `tests/results/test_portal.py`

**Interfaces:**
- Produces:
  - `results:broadsheet/<arm_pk>/<term_pk>` (school admin, principal, proprietor, the arm's form teacher; others 403):
    - one row per ranked student: admission number, name, each subject's total, total, average, position;
    - footer rows with each subject's highest, lowest and average;
    - available once the class is `approved`.
  - `results:broadsheet_csv/...`: the same data as CSV, guarded against formula injection.
  - `notify_published_task(arm_id, term_id)` (`TenantTask`, queue `notifications`, args redacted):
    - for each student in the published class, email each guardian email ("Results for {term} are ready. Log in to the student portal to view them." — no scores) and send SMS if configured;
    - audits `results.published_notified` with counts only;
    - send failures are logged (type only) and never raised.
  - `results:my_results` (students only): lists the terms whose class is published, each with a total, average, position "out of", a per-subject table and a "Download report card" link. It shows nothing for unpublished terms.

- [ ] **Step 1: Write failing tests:**
  - The broadsheet for the accuracy dataset shows the expected totals and positions, plus the footer statistics.
  - A teacher who is not the form teacher → 403.
  - Before approval → a "not ready" message.
  - The CSV matches.
  - Publish → one email per guardian email, containing no digits from any score (assert none of the totals appear), with a locmem SMS when configured.
  - Mail failure → logged, publish unaffected.
  - The student's `my_results` shows their position after publish and nothing before. It never shows another student's data.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Then run the full CI set: `ruff check`, `ruff format --check`, `mypy apps config`, `makemigrations --check --dry-run`, `scripts/with-pango .venv/bin/pytest -W error -m "not cloudinary"`.
- [ ] **Step 5: Commit** `feat(results): broadsheet, publish notifications and student results view`.

---

## Exit criteria for P3 (roadmap)

- One real past class reproduces the school's hand-calculated broadsheet exactly. The accuracy dataset in Task 7 is the automated stand-in; the pilot comparison script is run per pilot school before go-live (P9 checklist).
- Teachers enter scores safely: versioned, validated, locked after submission.
- The school reviews, approves and publishes.
- Students download branded report cards.
- Every new `api/v1/` route has an authenticated cross-school isolation case.

## Decisions left for later phases

- Offline score entry with IndexedDB drafts → P9.
- Blocking results for unpaid fees (UC-17) → P4.
- Attendance captured daily (replacing manual days present/open) → P6.
- Configurable ranking rules (e.g. dense ranking, position by total instead of average): **confirm with pilot schools**. The current rules are fixed in Global Constraints.
- Sending report cards to guardians as attachments → never, because of children's data. Guardians log in through the student account.
- Tailwind/HTMX build → still deferred. The score grid uses a small vanilla JS module.
