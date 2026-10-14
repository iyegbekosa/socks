# P6 Communication (Email and In-App) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Schools send announcements and fee reminders to families by email and in-app. Results, receipt and absence messages move onto one queued, delivery-logged `notify` service. Staff can see what was sent and whether it was delivered.

**Architecture:**
- **New app.** A new tenant app, `apps.comms`, holds:
  - `Message`: one announcement, reminder or automatic notice;
  - `Delivery`: one row per recipient and channel, with its status;
  - `CommsSettings`;
  - `FeeReminderDate`.
- **`notify()`.** `apps/comms/service.py` provides `notify(...)`. It resolves the audience to guardian emails (family email) and student users (in-app). It writes the `Message` and `Delivery` rows in one transaction. On commit, it enqueues batched send tasks on the `notifications` queue, holding bulk email until 07:00 during quiet hours.
- **Moved senders.** The existing guardian senders call `notify` with their exact current texts and keep their once-only guards:
  - results published: `apps/results/notify.py`;
  - receipts: `apps/fees/tasks.py`;
  - absence: `apps/attendance/notify.py`.

**Tech stack:** the P5 stack. There are no new dependencies. Email goes through Django's email backend: SMTP to SES or Postmark in production, the console backend in development.

**Spec and inputs:**
- `School Management Platform — System Design.pdf`:
  - §4.6, communication;
  - §5.2, `message` / `delivery_log` (channel, audience, body, status, provider_id);
  - §6.2, `POST /messages` (Admin; bursar for fees only);
  - §8.1, the `notifications` queue;
  - UC-14 reminders and UC-15 announcements.
- `docs/ROADMAP.md` P6, as re-planned on 14 Oct 2026: email and in-app only, with no SMS before the pilot.

**Decision proposed in this plan (confirm when reviewing):**
- **Delayed absence emails.** Absence emails are held for `AttendanceSettings.absence_email_delay_minutes`, default **10**. A teacher who corrects a mistaken "absent" within that time therefore sends nothing. A school can set it to 0.

**Out of scope:**
- SMS and WhatsApp (after the pilot);
- staff-to-parent replies and threads (version 3);
- unsubscribe links (school notices to guardians are service messages);
- scheduling announcements for a future date.

## Global Constraints

All P0–P5 Global Constraints still hold. Added for P6:

**Models**
- `Message(kind, subject, body, audience_label, created_by null, created_at, related_type, related_id)`.
  - `kind` is one of `announcement`, `fee_reminder`, `results_published`, `receipt`, `absence`.
  - `audience_label` is a short description such as "JSS 1A" or "Debtors ≥ ₦10,000 — 1st Term", and never contains names.
  - `related_*` optionally points at the arm/term, payment or record.
- `Delivery(message, channel, student, email blank, user null, status, error_type blank, queued_at, sent_at null, read_at null, send_after null)`.
  - `channel` is `email` or `inapp`.
  - `status` is one of `queued`, `sent`, `failed`, `skipped`.
  - It is unique on `(message, channel, email)` for email and `(message, channel, user)` for in-app, using conditional unique constraints.
  - It is indexed on `(status, send_after)` and `(user, read_at)`.
- `CommsSettings` is a singleton, `load()`, holding:
  - `email_quiet_start = 21:00` and `email_quiet_end = 07:00` (Africa/Lagos);
  - `reminder_min_balance_kobo = 100_000`, which is ₦1,000;
  - `reminder_template`, defaulting to the exact text below.
- `FeeReminderDate(term, date)` is unique on `(term, date)`. It holds up to **6 per term** and is set by the bursar or admin.

**Audiences**
- These resolve to students:
  - `school`: all active students with an active enrolment in the current session;
  - `level <pk>`;
  - `arm <pk>`;
  - `debtors(term, min_balance_kobo)`: uses `apps.fees.debtors.debtors`, active students only;
  - `students [pk…]`: at most 50.
- **Family email.** For each student, the family email recipients are their guardian emails. These are de-duplicated case-insensitively across the whole message, so a guardian of two siblings in the audience gets **one** email.
  - For a message whose body is per-student, such as a fee reminder, de-duplicate per (email, student) instead.
- **In-app.** One in-app delivery per student user.
- A student with no guardian email gets in-app only, and no error.

**Placeholders**
- Bodies may use only `{school_name}`, `{student_first_name}`, `{term}`, `{balance}` and `{date}`.
- Rendering uses a strict formatter. An unknown placeholder or an unbalanced brace is refused when the message is composed or saved, with the error "Unknown placeholder {x}. Use only: …".
- `{student_first_name}` and `{balance}` are allowed only in per-student kinds: `fee_reminder`, `receipt` and `absence`. The announcement composer refuses them, because announcements are de-duplicated per family.
- Values are plain text, and emails are sent as plain text only. No HTML email in the MVP.

**Delivery**
- **Batching.** Email is sent in batches of up to **100** per task, using one SMTP connection per batch (`get_connection()` and `send_messages`).
- **Quiet hours.** Email for `announcement`, `fee_reminder` and `results_published` created inside quiet hours gets `send_after` set to the next 07:00 Africa/Lagos. `receipt` and `absence` ignore quiet hours.
- **Failures.** A failed email is marked `failed` and logged by type only, and the batch continues. Failed emails are not retried automatically. A "Retry failed" action re-queues them (Task 4).
- **Holding task.** A Beat job every **5 minutes** sends `queued` email deliveries whose `send_after` has passed, fanning out per school.
- **In-app.** In-app deliveries are `sent` as soon as they are created.
- **Logging and audit.** Log ids and types only. `comms.message_sent` records `{message_id, kind, email_count, inapp_count}`. The body is never put in the audit.

**Texts**
These stay exactly as they are today:
- results: P3 text;
- receipt: P4 text;
- absence: P5 text.

The fee reminder default template is exactly:

`"Dear parent, {student_first_name}'s school fees balance for {term} is {balance}. Please pay through the student portal or at the school bursary. Thank you."`

**Permissions (§6.2, §7.1)**
- **Announcements:**
  - school_admin and principal may use every audience;
  - the bursar may use only the `debtors` audience (and fee reminders);
  - the proprietor may read message history;
  - everyone else gets 403.
- **Fee reminder dates and settings:** bursar and school_admin.
- **In-app inbox:** students see only their own deliveries.

**API**
- `POST /api/v1/messages` (`api:messages`). The body is `{"audience": {"type": "school"|"level"|"arm"|"debtors"|"students", "id"?: int, "ids"?: [int], "term"?: int, "min_balance_kobo"?: int}, "subject": str (≤ 120), "body": str (≤ 2000)}`.
  - It returns `201` with `{id, email_count, inapp_count}`.
  - Errors use `VALIDATION_ERROR`; 403 is returned per the permissions above.
- `GET /api/v1/notifications` (`api:notifications`). Students only. It returns their in-app deliveries, newest first, paginated, as `{id, subject, body, created_at, read}`.
- `POST /api/v1/notifications/<id>/read` (`api:notification_read`).
- Every route and method needs isolation cases.

## Review Focus

1. **Siblings.** Two siblings in one school announcement give their shared guardian **one** email but **two** in-app deliveries. Task 1: `test_family_email_deduplicated_across_siblings`.
2. **Quiet hours.** A message composed at 22:30 Lagos time leaves its emails queued with `send_after` at the next 07:00. The 5-minute Beat job sends nothing before then and everything after. A receipt at 22:30 goes immediately. Task 1: `test_quiet_hours_hold_bulk_email_only`.
3. **Re-run of the reminder job.** If the fee reminder job runs twice on the same day, each debtor's guardians get one reminder, not two. Task 5: `test_reminders_idempotent_per_day`.
4. **Bursar's audience.** A bursar posting a `school` audience gets 403 through both HTML and API. Task 4: `test_bursar_limited_to_debtors`.
5. **Moved senders.** After the move, the results, receipt and absence messages still send exactly once with exactly the old text, and their old once-only guards still hold. Task 3: one test per sender.

---

### Task 1: The comms app and the `notify` service

**Files:**
- Create: `apps/comms/` (`apps.py`, `models.py`, `audience.py`, `render.py`, `service.py`, `tasks.py`), migration `0001_initial`, and `tests/comms/`.
- Modify:
  - `config/settings.py`: add `"apps.comms"` to `TENANT_APPS`, and add a Beat entry `"comms-send-held": every 300 s, queue notifications`;
  - `apps/core/logging.py`: add `SECRET_ARG_TASKS`.
- Test: `tests/comms/test_render.py`, `tests/comms/test_notify.py`.

**Interfaces:**
- Produces:
  - `render.check_template(body, *, per_student: bool) -> None`, which raises `ValidationError` with the exact message.
  - `render.render(body, ctx: dict[str, str]) -> str`.
  - `audience.Audience`, a dataclass with `type`, `id`, `ids`, `term` and `min_balance_kobo`, plus `audience.resolve(a) -> list[Student]` and `audience.label(a) -> str`.
  - `service.notify(kind, *, students: list[Student], subject: str, body: str, per_student: bool, context_for: Callable[[Student], dict] | None = None, channels=("email", "inapp"), by=None, related=None) -> Message`.
    - It renders, de-duplicates and creates the rows in one `atomic()`.
    - It sets `send_after` for quiet hours.
    - It enqueues `send_email_batch_task(delivery_ids)` in chunks of 100 on commit, for deliveries that are due now.
  - `tasks.send_email_batch_task(ids)`: a TenantTask on queue `notifications`. It sends only deliveries that are still `queued` and due. It claims each batch with a conditional update to a transient `sending` status, then sets `sent` or `failed`.
  - `tasks.send_held_all_schools()`: Beat fan-out with explicit schema headers.

- [ ] **Step 1: Write failing tests.**
  - The strict renderer refuses `{name}`, `{`, and `{balance}` in an announcement.
  - A school announcement to 3 students, two of them siblings sharing a guardian email (differing only in case), gives 2 emails and 3 in-app deliveries. This is `test_family_email_deduplicated_across_siblings`.
  - A per-student message to the same three gives 3 emails, because each body differs.
  - `test_quiet_hours_hold_bulk_email_only`: freeze the Lagos time by injecting `now`.
  - An SMTP failure marks only that delivery as failed, and the others are sent.
  - Running the batch task twice sends each email once.
  - The audit holds counts only.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest -W error tests/comms` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS, plus `makemigrations --check`.
- [ ] **Step 5: Commit** `feat(comms): notify service with audiences, batching, quiet hours and delivery log`.

### Task 2: In-app inbox for students (HTML and API)

**Files:**
- Create: `apps/comms/views/inbox.py` and template `comms/inbox.html`.
- Modify:
  - the student portal home: an unread badge and an "Inbox" link;
  - `apps/api/urls.py` and `apps/api/views.py`;
  - `tests/isolation/registry.py`.
- Test: `tests/comms/test_inbox.py`, `tests/api/test_notifications_api.py`.

**Interfaces:**
- Produces:
  - `comms:inbox` (students only), newest first, paginated at 20. Opening a message, or "Mark all read", sets `read_at`.
  - `comms:inbox_item <pk>`: returns 404 unless it is the student's own delivery.
  - API `api:notifications` (GET) and `api:notification_read` (POST), as in Global Constraints.
  - `unread_count(user) -> int`, used by the portal badge.

- [ ] **Step 1: Write failing tests.**
  - A student sees only their own messages. Another student's delivery id returns 404 on both HTML and API.
  - Opening a message marks it read, and the unread count drops.
  - A staff user gets 403 on `api:notifications`.
  - Isolation cases for all three methods: authenticated and anonymous.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(comms): student in-app inbox and notifications API`.

### Task 3: Move the existing guardian messages onto `notify`, and delay absence emails

**Files:**
- Modify:
  - `apps/results/notify.py`: results published;
  - `apps/fees/tasks.py` (`send_receipt_task`) and `apps/fees/receipts.py` where needed;
  - `apps/attendance/notify.py`, `apps/attendance/hooks.py`, `apps/attendance/models.py` (`absence_email_delay_minutes`, default 10, range 0–120, with a migration) and the attendance settings form.
- Test: the existing results, fees and attendance message tests, updated, plus new ones in `tests/comms/test_moved_senders.py`.

**Interfaces:**
- Consumes: `service.notify`.
- The three senders build their audience and context, then call `notify`. Each keeps its own once-only guard:
  - results: published re-check and the per-run audit;
  - receipt: the atomic "first PDF" update;
  - absence: the `absence_emailed_at` conditional update.
- **SMS removal.** `apps/results/notify.py` stops sending SMS, so the results message is email and in-app only. The `sms_configured` and `send_sms` calls are removed from the results path. `apps/core/sms.py` stays for after the pilot.
- **Absence delay.** The absence hook enqueues with `countdown = absence_email_delay_minutes * 60`. When the task runs, it emails only records that are still `absent`. The existing guard already filters status.

- [ ] **Step 1: Write failing tests.**
  - Each sender produces one `Message` of the right kind, with exactly the old text, and with email and in-app deliveries.
  - A repeat trigger, such as publishing again or regenerating a receipt, adds no second message.
  - With the delay set to 10 minutes, a student marked absent then corrected to present before the task runs gets no email. With 0, the email is sent immediately.
  - No SMS is sent from the results path even when an SMS backend is configured.
  - The existing P3, P4 and P5 tests are updated only where they asserted direct `mail.outbox` details that now flow through `notify`. Their expectations must stay the same.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS, then run `tests/results`, `tests/fees` and `tests/attendance` in full.
- [ ] **Step 5: Commit** `refactor(comms): route results, receipt and absence messages through notify; delay absence emails`.

### Task 4: Announcements and message history (HTML and API)

**Files:**
- Create: `apps/comms/views/compose.py`, `apps/comms/views/history.py`, templates `comms/compose.html`, `comms/history.html`, `comms/message.html`.
- Modify:
  - `apps/api/urls.py`, `apps/api/views.py` (`api:messages`);
  - `tests/isolation/registry.py`;
  - the staff home: a "Messages" link.
- Test: `tests/comms/test_compose.py`, `tests/comms/test_history.py`, `tests/api/test_messages_api.py`.

**Interfaces:**
- Produces:
  - `comms:compose`:
    - choose an audience: school, level, arm, debtors (term and minimum ₦) or up to 50 students by admission number;
    - enter a subject and body;
    - "Preview" shows the recipient counts (emails and in-app) and the rendered body;
    - "Send" calls `notify(kind="announcement", per_student=False)` and redirects to the message page.
  - `comms:history`: messages newest first, with kind, audience label, sender, time and the counts sent, queued, failed and read. It is filterable by kind.
  - `comms:message <pk>`: per-delivery status, showing the masked email (`a***@domain`), status and error type. "Retry failed" re-queues the failed email deliveries. Admin and principal can use it, and so can the bursar for their own messages.
  - API `api:messages` (POST), as in Global Constraints.

- [ ] **Step 1: Write failing tests.**
  - Admin sends an announcement to one arm. The counts match, and the history and message pages show them.
  - `test_bursar_limited_to_debtors` covers both HTML and API.
  - A teacher gets 403. The proprietor can view history but gets 403 on compose.
  - An unknown placeholder gives a form error, and the API returns 400.
  - More than 50 students, or an unknown admission number, gives a form error.
  - "Retry failed" re-queues only the failed deliveries.
  - The message page never shows a full guardian email.
  - Isolation: an `api:messages` POST case where school A's admin uses school B's arm id is refused, with `count_model` on Message. There is also an anonymous case.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(comms): announcements, message history and delivery status`.

### Task 5: Fee reminders

**Files:**
- Create: `apps/comms/reminders.py`, `apps/comms/views/reminders.py`, template `comms/reminders.html`.
- Modify: `config/settings.py`, adding the Beat entry `"comms-fee-reminders"`: daily at 08:00 Africa/Lagos (crontab), queue notifications.
- Test: `tests/comms/test_reminders.py`.

**Interfaces:**
- Consumes: `apps.fees.debtors.debtors`, `apps.fees.invoicing.outstanding_for` and `service.notify`.
- Produces:
  - `reminders.send_due_reminders(today) -> Message | None`.
    - If `today` is a `FeeReminderDate` of the current term, it sends one `fee_reminder` message to debtors with a balance of at least `reminder_min_balance_kobo`.
    - It uses `per_student=True` and context `{student_first_name, term, balance: naira_display(outstanding)}`.
    - It runs at most once per school per day, guarded by `school_lock("comms:reminders")` and an existing `Message(kind="fee_reminder", created_at::date == today)` check.
  - `send_reminders_all_schools()`: Beat fan-out.
  - `comms:reminders` (bursar and admin):
    - manage the term's reminder dates (up to 6, school days in the current term, not in the past);
    - set the minimum balance and the template (strict placeholders);
    - "Send reminders now" sends immediately and counts for today.

- [ ] **Step 1: Write failing tests.**
  - On a reminder date, a debtor owing ₦50,000 is reminded with the exact default text, and a ₦500 debtor is not (minimum ₦1,000).
  - On a non-reminder date nothing is sent.
  - `test_reminders_idempotent_per_day`: run the job twice, or press "Send now" and then the job runs, and only one reminder goes out.
  - A seventh reminder date is refused. A past date is refused.
  - A template with `{name}` is refused.
  - Withdrawn students are not reminded.
  - Beat fan-out sends schema headers for active schools only.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Then run the full CI set:
  - `ruff check`
  - `ruff format --check`
  - `mypy apps config`
  - `makemigrations --check --dry-run`
  - `scripts/with-pango .venv/bin/pytest -W error -m "not cloudinary"`
- [ ] **Step 5: Commit** `feat(comms): scheduled fee reminders by email and in-app`.

---

## Exit criteria for P6 (roadmap)

- A results-published notice reaches every guardian email in a test class and appears in-app, and its delivery status is visible on the message page.
- Every new `api/v1/` route has an authenticated cross-school isolation case.
- **The MVP feature set is complete (P0–P6).** Next come the pilot checklist and the pending P4/P5 release items, recorded in project memory.

## Decisions left for later phases

- **SMS channel:** after the pilot, as a paid option for each school. The `notify` channels list is ready for an `sms` channel.
- **WhatsApp:** version 2.
- **Scheduled announcements, and replies or threads:** later, if pilots ask.
- **Email bounce handling via provider webhooks (SES/Postmark):** P9 hardening.
