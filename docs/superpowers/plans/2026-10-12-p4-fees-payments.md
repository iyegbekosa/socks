# P4 Fees and Payments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every naira paid, whether online through Paystack or by cash or teller, is recorded exactly once against the right student's invoices. It comes with a numbered PDF receipt and an accurate debtors list.

**Architecture:**
- **New tenant app `apps.fees`.** It holds:
  - fee items and per-level, per-term structures;
  - discounts;
  - invoices with lines and balance brought forward;
  - payments with allocations;
  - receipts from a locked per-school counter;
  - manual-payment approval;
  - payment attempts.
- **Gateways.** The `PaymentGateway` protocol has two implementations: `PaystackGateway`, an `httpx` client tested with `httpx.MockTransport`, and `FakeGateway`, used for DEBUG and tests.
- **Confirming money.** One service, `apps/fees/ledger.py` `confirm_payment`, does all money confirmation in one `transaction.atomic()` block with `select_for_update` on the invoices. It is called by the webhook task, the re-verification job and manual-payment approval.
- **Webhook routing.** Paystack calls one platform-wide webhook on the public host. A public-schema `PaymentRoute` row maps each payment reference to its school's schema.

**Tech Stack:**
- The P3 stack, plus `httpx` (runtime).
- Celery Beat schedule for re-verification.
- WeasyPrint receipts through `apps.core.pdf.render_pdf`.

**Spec:**
- `School Management Platform — System Design.pdf`:
  - §4.5 fees and payments: the online flow steps 1–7 and manual payments;
  - §5.2 `fee_item`/`invoice`/`payment_attempt`/`payment`/`receipt`;
  - §5.3 indexes;
  - §5.4 kobo, atomic confirm, locked counters;
  - §6.1 money format, §6.2 `/invoices`, `/payments/initialize`, `/payments/webhook/{gateway}`, `/payments/manual`;
  - §8.1 `payments` queue and the 15-minute re-verify;
  - §8.2 duplicate webhooks;
  - §8.3 "Payment gateway down".
- Requirements: FR-10 to FR-15, and UC-11 to UC-14 and UC-17.
- `docs/ROADMAP.md`:
  - phase P4;
  - §1.2: no parent accounts. Parents pay through the student login. Siblings are found by matching guardian contacts. The gateway customer email is the guardian email, or a placeholder.

**User decisions (12 Oct 2026):**
- **Gateway:** Paystack first.
- **Charges:** the school absorbs gateway charges, using Paystack `bearer: "subaccount"`. There is no platform fee.
- **Remita:** not in v1. Remita schools record those payments manually.
- **Part-payment online:** a per-school setting, on by default.

**Out of scope:**
- fee reminders by SMS/email (P5);
- Flutterwave or Remita adapters;
- refunds through the gateway (the bursar handles overpayments offline);
- platform subscription billing (P8).

## Global Constraints

All P0–P3 Global Constraints still hold. Added for P4:

**Money**
- **Units.** Amounts are integer kobo everywhere: `BigIntegerField`, never float or Decimal in models.
  - Naira text from forms goes through `apps.core.money.kobo_from_naira`.
  - API responses use `money_fields(kobo)`: `{"amount_kobo": …, "amount_display": "₦150,000.00"}`.
- **Cap.** `kobo_from_naira` rejects amounts above **₦100,000,000.00** (`10_000_000_000` kobo) and below 0, with `ValueError("Amount is too large.")` and `ValueError("Amount cannot be negative.")`. Every money field also has DB `CheckConstraint`s (`>= 0`), except `InvoiceLine.amount_kobo`, which may be negative for discounts and credits.
- **Currency.** Only `NGN`. A gateway result in any other currency is a mismatch.

**Invoices (FR-10, FR-13)**
- **One invoice per (student, term).** Unique `(student, term)`.
- **Totals.** `total_kobo` is the sum of the line amounts, and `balance_kobo = total_kobo − paid_kobo`. A `CheckConstraint` enforces `paid_kobo <= total_kobo`. `total_kobo` is never negative: discounts are capped so the total is ≥ 0.
- **Status:**
  - `unpaid` when paid is 0 and the balance is > 0;
  - `part_paid` when 0 < paid < total;
  - `paid` when the balance is 0, which includes a total of 0;
  - `carried` when the balance moved into a later invoice as balance brought forward.
- **Line kinds:**
  - `fee`, from a structure line;
  - `optional`, an optional item the bursar added;
  - `discount`, negative;
  - `brought_forward`;
  - `adjustment`, which needs a reason.
- **Discounts.** A `Discount` is either `percent` (whole number 1–100, applied to the sum of `fee` lines) or `fixed` (kobo). A student can hold several for a session. They are applied in pk order, and the running total never goes below 0.
- **Balance brought forward.** It applies when generating a student's invoice for term T. Every earlier invoice of that student with a balance > 0 and status ≠ `carried` adds its balance as one `brought_forward` line ("Balance brought forward from {term} {session}"). That earlier invoice is then set to `carried`.
  - Once carried, an invoice can't be paid: payments go to the newest invoice.

**Payments (FR-11, FR-12, §4.5)**
- **Recording a payment.** A `Payment` is created only by `ledger.confirm_payment`. It holds `amount_kobo`, `method` (`online`, `cash`, `teller`, `pos`, `transfer`), `gateway_reference` (unique and nullable; set for online payments), `recorded_by`, `approved_by`, `unallocated_kobo` and `student` (the payer's student).
  - Allocation follows the attempt's planned lines, or the manual form's invoices, in the order given. Each is capped at that invoice's current balance.
  - Any excess becomes `unallocated_kobo > 0` and the payment is `flagged` for the bursar.
- **Confirming online.** An online payment is confirmed only after both of these:
  1. the webhook signature checks out (HMAC-SHA512 of the raw body with `PAYSTACK_SECRET_KEY`, compared to the `x-paystack-signature` header with `hmac.compare_digest`);
  2. a server-side `verify(reference)` returns `status == "success"`, `amount ==` the attempt's `amount_kobo`, `currency == "NGN"`, and the attempt's school subaccount.
  - The redirect/callback page never confirms anything. It only reads the status.
- **References.** Payment references are `B2L-` followed by 20 uppercase base32 characters from `secrets`. They are unique on `PaymentAttempt.reference` and on the public `PaymentRoute.reference`.
- **Idempotency.** Confirmation is idempotent:
  - the attempt row is locked `FOR UPDATE`;
  - an attempt already `success` returns the existing Payment;
  - a unique `gateway_reference` makes a duplicate insert impossible. On an `IntegrityError`, re-read and return the existing payment.
- **Attempt status:** `pending` → `success`, `failed` or `abandoned`, or `flagged` (mismatch). Attempts store `raw_response` with Paystack's `customer` and `authorization` keys removed, so no card or PII data is kept.

**Part payments**
- `FeeSettings.allow_part_payment` (default `True`) and `FeeSettings.min_part_payment_kobo` (default `0`).
- When part payment is on, the student enters an amount per invoice: at least the minimum (or that invoice's whole balance, if smaller) and at most its balance.
- When it is off, every selected invoice is paid in full.
- Manual payments may always be partial.

**Manual payments (UC-13)**
- **Who:** recorded by a bursar or school admin, with method, amount, invoices, date paid, teller/reference number (required for `teller`, `pos` and `transfer`) and an optional photo.
  - The photo is private Cloudinary, `default_storage` under `fees/tellers/`. JPEG, PNG or PDF, at most 5 MB, checked with `PlanLimits.check_can_store`.
- **Approval:** at or above `FeeSettings.manual_approval_threshold_kobo` (default `5_000_000`, i.e. ₦50,000), the record is `pending_approval` and credits nothing.
  - Approval comes from a different bursar or school admin (approver ≠ recorder, else 403). It then calls `confirm_payment`.
  - Rejection needs a reason.
  - Below the threshold, the record confirms immediately with `approved_by = recorded_by`.

**Receipts (FR-14)**
- **One receipt per confirmed Payment.**
- **Number format:** `{admission_prefix}/RCT/{year}/{n:06d}`.
  - `n` comes from `ReceiptCounter(year unique, last)`, incremented under `select_for_update` inside the confirm transaction, so a rollback leaves no gap in committed numbers.
  - `year` is the Africa/Lagos calendar year of confirmation.
- **PDF:**
  - The PDF is rendered on commit on the `reports` queue and stored at `receipts/<year>/<receipt pk>.pdf`, authenticated.
  - Contents:
    - school branding;
    - receipt number and date;
    - the student's name and admission number;
    - method, and reference or teller number;
    - an amount line per allocated invoice (term and session) with the remaining balance;
    - the total paid;
    - "Recorded by" (staff full name), or "Paid online".
- **Notifying guardians:** after the PDF is stored, the guardians' email addresses get "Payment of {amount} received for {student first name}. Receipt {number}." and SMS goes to guardian phones when SMS is configured. Neither carries the PDF or the balances. Recipients are de-duplicated case-insensitively, as in `apps/results/notify.py`.

**Permissions (§7.1)**
- **Fee setup, invoices, manual payments, approvals, reconciliation, debtors:** `bursar` and `school_admin`. Both are MFA roles, enforced by middleware.
- **Read-only fee pages and debtors:** `proprietor`.
- **Everyone else on staff:** 403.
- **Online payment settings (bank account and subaccount):** `school_admin` only.
- **Students:**
  - their own invoices and receipts;
  - pay their own and their siblings' invoices;
  - see each sibling's first name, class and balance only.
- **Siblings:** other `active` students of the school who share at least one non-empty guardian phone or guardian email (case-insensitive), across both guardian slots. Blank contacts never match.

**Results blocking (UC-17)**
- `FeeSettings.block_results_when_owing` (default `False`).
- When it is on, a student whose invoices up to and including term T have a combined balance > 0 cannot see term T on My results or download its report card. They get the message "Your results for this term are withheld until outstanding fees are paid. Please contact the school bursar."
- Staff views are unaffected, and guardians are still notified that results are ready.

**Paystack (adapter facts)**
- **Base URL:** `https://api.paystack.co`, with `Authorization: Bearer <PAYSTACK_SECRET_KEY>`.
- **Timeout:** connect 5 s, read 15 s.
- **Endpoints:**
  - initialize: `POST /transaction/initialize` with `email`, `amount` (kobo, int), `reference`, `currency: "NGN"`, `subaccount`, `bearer: "subaccount"`, `callback_url`, `metadata: {"schema": …, "attempt_id": …}`;
  - verify: `GET /transaction/verify/{reference}`;
  - banks: `GET /bank?country=nigeria&currency=NGN`;
  - resolve: `GET /bank/resolve?account_number=&bank_code=`;
  - subaccount: `POST /subaccount` (`business_name`, `settlement_bank`, `account_number`, `percentage_charge: 0`) and `PUT /subaccount/{code}`.
- **Gateway email:** the main guardian email, else the second guardian email, else `pay+{schema}-{student_pk}@{PAYMENT_PLACEHOLDER_EMAIL_DOMAIN}` (setting, default `build2learn.ng`).
- **Gateway errors:** any network error, 5xx or `status: false` raises `GatewayUnavailable`. Its message is never shown verbatim to users.

**Queues and jobs**
- Webhook processing, confirmation and re-verification run on `payments`.
- Receipt PDFs run on `reports`.
- Receipt messages run on `notifications`.
- **Re-verification:** a Beat job every **15 minutes** fans out per school and re-verifies attempts `pending` for more than **30 minutes**. Attempts pending for more than **24 hours** that verify as not successful become `abandoned`.

**Audit events**
- `fees.item_saved`, `fees.structure_saved`, `fees.discount_saved`, `fees.student_discount_set`
- `fees.invoices_generated` (counts), `fees.invoice_adjusted`
- `fees.manual_recorded`, `fees.manual_approved`, `fees.manual_rejected`
- `fees.payment_confirmed`, `fees.payment_flagged`, `fees.attempt_mismatch`
- `fees.settings_saved`, `fees.subaccount_saved`
- Metadata holds ids and kobo amounts only: no names, phones, emails or account numbers. Account numbers are masked to their last 4 digits.

**Logging**
- New tasks that take ids go in `SECRET_ARG_TASKS`.
- Log only exception types, never gateway response bodies.

**API (§6.2)**
- `GET /api/v1/invoices?student=&term=`
- `POST /api/v1/payments/initialize`
- `POST /api/v1/payments/manual`
- The tenant API needs an isolation case per route and method.
- The public webhook `POST /payments/webhook/paystack/` is on the public URLconf. It is CSRF-exempt and has no session.

## Review Focus

1. **Webhook ordering.** A webhook arrives twice, or out of order with the student's return to the callback page, or while re-verification runs. Expect exactly one Payment, one Receipt and one set of allocations. Covered by Task 8 (`test_duplicate_and_concurrent_confirmation_credit_once`).
2. **Double payment.** A bursar records a manual payment for an invoice while an online payment for the same invoice is pending, and both succeed. Expect the online payment to allocate only the remaining balance, with the excess as `unallocated_kobo` and the payment `flagged`, and never `paid_kobo > total_kobo`. Covered by Task 8 (`test_overpayment_after_manual_payment_is_flagged_not_lost`).
3. **Verify mismatch.** Verify returns a different amount, currency or subaccount, or `status` not `success`, while the webhook claims success. Expect no credit, the attempt marked `flagged` or `failed`, and an audit `fees.attempt_mismatch`. Covered by Task 8 (`test_verify_mismatch_never_credits`).
4. **Re-generating invoices.** Invoices are generated again after the fee structure changed or a student was admitted mid-term. Expect no duplicate invoice, existing invoices untouched, and new students invoiced with the current structure. Covered by Task 2 (`test_generation_is_idempotent_and_picks_up_new_students`).
5. **Sibling matching.** One sibling has a blank guardian email, or contacts that differ only in letter case. Expect blanks never to match, and case-insensitive email matching to work. A student whose guardian phone matches a withdrawn student does not see them. Covered by Task 10 (`test_sibling_matching_rules`).

---

### Task 1: Fees app, settings, fee items, structures and discounts (UC-11)

**Files:**
- Create:
  - `apps/fees/` (`apps.py`, `models.py`, `forms.py`, `services.py`, `urls.py`, `views/setup.py`, `templates/fees/…`)
  - `apps/fees/migrations/0001_initial.py`
- Modify:
  - `apps/core/money.py`: the cap and the negative check
  - `config/settings.py`: add `"apps.fees"` to `TENANT_APPS` after `apps.results`; add `PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", default="")`, `PAYMENT_GATEWAY = env("PAYMENT_GATEWAY", default="apps.fees.gateways.fake.FakeGateway" if DEBUG else "apps.fees.gateways.paystack.PaystackGateway")` and `PAYMENT_PLACEHOLDER_EMAIL_DOMAIN`
  - `config/urls_tenant.py`: `path("fees/", include("apps.fees.urls"))`
  - `pyproject.toml`: `httpx>=0.27`
  - `apps/accounts/templates/accounts/home.html`: a "Fees" link for bursar, admin and proprietor
- Test:
  - `tests/fees/test_money.py`
  - `tests/fees/test_setup.py`
  - `tests/fees/factories.py`

**Interfaces:**
- Produces:
  - `FeeSettings` (singleton via `FeeSettings.load()`), with:
    - `allow_part_payment=True`
    - `min_part_payment_kobo=0`
    - `manual_approval_threshold_kobo=5_000_000`
    - `block_results_when_owing=False`
    - `bank_transfer_note` (text shown when the gateway is down; may be blank)
  - `FeeItem(name unique, description)`.
  - `FeeStructure(level FK ClassLevel, term FK Term)`, unique `(level, term)`.
  - `FeeStructureLine(structure, item, amount_kobo, is_optional=False)`, unique `(structure, item)`.
  - `Discount(name unique, kind in {"percent","fixed"}, percent null, amount_kobo null)`. A check constraint ensures exactly one of percent and amount is set, and that the percent is in 1..100.
  - `StudentDiscount(student, discount, session)`, unique `(student, discount, session)`.
  - `services.copy_structures(from_term, to_term, by) -> int`.
  - Role helpers `can_manage_fees(user)` (bursar or school_admin) and `can_view_fees(user)` (those plus proprietor) in `apps/fees/scope.py`.
  - Routes:
    - `fees:settings`, `fees:items`, `fees:structures`, `fees:structure_edit <level_pk> <term_pk>`, `fees:discounts`
    - `fees:student_discounts <student_pk>`, linked from the student detail page for fee managers

- [ ] **Step 1: Write failing tests**
  - Money:
    - `kobo_from_naira("100000000.00") == 10_000_000_000`;
    - `"100000000.01"` raises "Amount is too large.";
    - `"-1"` raises "Amount cannot be negative.".
  - Setup pages:
    - A bursar creates an item, a structure for JSS1 / 1st term with two lines (one optional), a 10% discount and a ₦5,000 fixed discount, then assigns a discount to a student. Each step is audited with ids only.
    - A teacher gets 403 on every `fees:` setup route.
    - A proprietor GETs with 200 but POST returns 403.
    - Saving a line with amount `"12.345"` gives a form error.
    - A duplicate item name gives a form error, not a 500.
    - `copy_structures` copies lines to the next term and skips levels that already have a structure.
- [ ] **Step 2: Run** `scripts/with-pango .venv/bin/pytest -W error tests/fees` → FAIL.
- [ ] **Step 3: Implement.** Use `require_roles` as `apps/academics/views.py` does, and `never_cache` on every page. Structure edit is a single form with one row per fee item (amount blank = not in the structure). Saves happen under `school_lock(f"fees:structure:{level.pk}:{term.pk}")` inside `atomic()`.
- [ ] **Step 4: Run** the tests → PASS. Run `makemigrations --check` after creating 0001.
- [ ] **Step 5: Commit** `feat(fees): fee settings, items, structures and discounts`.

### Task 2: Invoices: generation, balance brought forward, adjustments (FR-10, FR-13)

**Files:**
- Create: `apps/fees/invoicing.py`, `apps/fees/views/invoices.py`, templates `fees/invoices.html`, `fees/invoice_detail.html`, migration `0002_invoices`
- Test: `tests/fees/test_invoicing.py`, `tests/fees/test_invoice_views.py`

**Interfaces:**
- Consumes: Task 1 models; `Enrolment` (active enrolment in the term's session gives a student's level); `school_lock`.
- Produces:
  - `Invoice(student, term, total_kobo, paid_kobo, status, created_at, updated_at)`
    - `balance_kobo` is a property;
    - indexes `(term, status)`;
    - unique `(student, term)`.
  - `InvoiceLine(invoice, kind, description, item null, amount_kobo, reason, created_by null)`.
  - `invoicing.generate_invoices(term, *, levels=None, by) -> GenerationResult(created: int, skipped_existing: int, skipped_no_structure: int)`.
    - It covers students with an active enrolment in `term.session` whose level has a structure for `term`.
    - Each student is handled in its own `atomic()` under `school_lock(f"fees:student:{student.pk}")`.
    - Lines: the structure's non-optional lines, then discounts (capped), then brought forward.
  - `invoicing.add_line(invoice, *, kind, description, amount_kobo, reason, item=None, by) -> InvoiceLine`.
    - It recomputes the total under `select_for_update`.
    - It refuses if the new total would fall below the paid amount (`ValidationError`), and refuses on `carried` invoices.
  - `invoicing.recompute(invoice) -> None`: sets the total and status from lines and paid. Only callers that hold the invoice row lock may call it.
  - `invoicing.outstanding_for(student, *, up_to_term=None) -> int`: the sum of balances of non-`carried` invoices, used by Tasks 10 and 11.
  - Routes:
    - `fees:invoices` (term filter, level and arm filters, "Generate invoices" POST)
    - `fees:invoice <pk>` (detail with lines, payments and allocations; POST "Add optional item" or "Add adjustment")

- [ ] **Step 1: Write failing tests**
  - Generation: JSS1 1st term has Tuition ₦50,000 and PTA ₦5,000, plus optional Bus ₦20,000. A student holds 10% and ₦2,000 discounts. The invoice total is `55_000_00 − 5_500_00 − 2_000_00 = 47_500_00` kobo, with lines in the stated order.
  - Discount cap: a 100% plus ₦1,000 discount gives a total of 0 and status `paid`.
  - Brought forward: the 1st term invoice is part-paid with ₦10,000 left. Generating the 2nd term gives a `brought_forward` line of `1_000_000` and sets the 1st term invoice to `carried`. `outstanding_for` counts it once.
  - `test_generation_is_idempotent_and_picks_up_new_students`:
    - Generate twice: the second run creates 0 invoices.
    - Admit a new student and change the structure amount, then generate: the new student is invoiced at the new amount and the old invoices are unchanged.
  - A level without a structure is counted in `skipped_no_structure`.
  - Adjustments:
    - A ₦-3,000 adjustment with a reason recalculates the total and status, and is audited.
    - An adjustment that would put the total below the paid amount gives a form error.
    - An adjustment on a `carried` invoice gives 409.
  - The views follow the Task 1 permissions. A teacher gets 403 on `fees:invoice` for any pk, and a foreign or bad pk gives 404 via `parse_id`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): invoice generation, balance brought forward and adjustments`.

### Task 3: Ledger: confirm_payment, allocations, receipt numbers (§5.4)

**Files:**
- Create: `apps/fees/ledger.py`, migration `0003_payments`
- Test: `tests/fees/test_ledger.py`

**Interfaces:**
- Consumes: `Invoice` and `invoicing.recompute`.
- Produces:
  - `Payment(student, amount_kobo, method, gateway_reference unique null, paid_at, recorded_by null, approved_by null, unallocated_kobo=0, flagged=False, flag_reason, attempt OneToOne null, manual OneToOne null, created_at)`.
  - `Allocation(payment, invoice, amount_kobo>0)`, unique `(payment, invoice)`.
  - `Receipt(payment OneToOne, number unique, year, pdf_name blank, created_at)`.
  - `ReceiptCounter(year unique, last)`.
  - `ledger.PlannedLine(invoice_id: int, amount_kobo: int)`, a frozen dataclass.
  - `ledger.confirm_payment(*, student, amount_kobo, method, plan: list[PlannedLine], paid_at, gateway_reference=None, recorded_by=None, approved_by=None, attempt=None, manual=None) -> Payment`:
    1. Opens one `atomic()`.
    2. Locks the invoices `FOR UPDATE` in ascending pk order.
    3. Returns the existing Payment if `gateway_reference`, `attempt` or `manual` already has one.
    4. Allocates by plan order, each line capped at the invoice's balance. `carried` invoices count as balance 0.
    5. Sets the excess to `unallocated_kobo`, with `flagged=True` and `flag_reason="Paid more than the balance"`.
    6. Calls `recompute` on each invoice.
    7. Allocates the next receipt number and creates the `Receipt`.
    8. Audits `fees.payment_confirmed` (ids and kobo), plus `fees.payment_flagged` when flagged.
    9. Registers `transaction.on_commit(lambda: on_payment_confirmed(payment.pk))`. In Task 3 this is a no-op hook in `apps/fees/hooks.py`; Task 4 fills it.
  - `ledger.next_receipt_number(year: int) -> str`, called only inside `confirm_payment`'s transaction.

- [ ] **Step 1: Write failing tests**
  - Two invoices are ₦30,000 and ₦20,000, with a plan for both at full value. The result is two allocations, both invoices `paid`, and one receipt `GFS/RCT/2026/000001` (prefix from `AcademicSettings`).
  - A second payment gets receipt `000002`. A payment confirmed in 2027 gets `…/2027/000001`.
  - A plan of ₦30,000 against an invoice with ₦10,000 left allocates ₦10,000. `unallocated_kobo` is `2_000_000`, `flagged` is true and an audit event is written.
  - Calling twice with the same `gateway_reference` returns the same Payment, with one allocation set and one receipt.
  - A rolled-back transaction (raise after confirm inside an outer `atomic`) leaves the counter unchanged, so the next number is still `000001`.
  - A `carried` invoice in the plan allocates 0 and the excess is flagged.
  - Asserting through `CaptureQueriesContext` shows the invoice rows are locked `FOR UPDATE` in pk order.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): atomic payment confirmation, allocation and receipt numbering`.

### Task 4: Receipt PDFs, downloads and guardian messages (FR-14)

**Files:**
- Create: `apps/fees/receipts.py`, `apps/fees/tasks.py`, `apps/fees/templates/fees/receipt.html`, `apps/fees/views/receipts.py`
- Modify: `apps/fees/hooks.py`, `apps/core/logging.py` (`SECRET_ARG_TASKS`)
- Test: `tests/fees/test_receipts.py`

**Interfaces:**
- Consumes:
  - `render_pdf(html, allowed_urls=[])`;
  - the logo loader `apps.results.cards.load_logo`. Move it to `apps/branding/pdf.py` if importing it from results would be circular, and keep the results import working;
  - the `default_storage` save pattern from `apps/results/cards.py`.
- Produces:
  - `generate_receipt_task(payment_id)` (`TenantTask`, queue `reports`): renders, stores at `receipts/<year>/<receipt pk>.pdf`, sets `pdf_name`, then enqueues `send_receipt_task(payment_id)` (queue `notifications`). The send task emails and texts guardians with the exact text from Global Constraints. Failures are logged by type only and never raised.
  - `on_payment_confirmed(payment_id)` enqueues `generate_receipt_task`. If enqueueing fails, it logs and leaves `pdf_name` blank, and "Regenerate" covers it.
  - Routes:
    - `fees:receipt_download <receipt_pk>`: 302 to the signed URL with `no-store`. Students may only fetch a receipt whose payment `student` is them or one of their siblings in the allocation. Staff with `can_view_fees` may fetch any. Everything else gets 404.
    - `fees:receipt_regenerate <receipt_pk>` (POST, fee managers).

- [ ] **Step 1: Write failing tests** (marker `pdf`, Cloudinary patched as in `tests/results/test_cards.py`)
  - The pypdf text contains the receipt number, the student's full name and admission number, `₦30,000.00`, each invoice's term name, and the remaining balance.
  - The guardian email body is exactly `"Payment of ₦30,000.00 received for Ada. Receipt GFS/RCT/2026/000001."` and includes no balance.
  - Duplicate guardian emails differing only in letter case send one email.
  - Download rules:
    - the paying student → 302;
    - another student → 404;
    - a teacher → 403/404;
    - the bursar → 302;
    - a missing PDF → a 200 "being prepared" page.
  - A storage failure leaves `pdf_name` blank, logs the type only, and Regenerate fixes it.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): receipt PDFs, downloads and guardian receipt messages`.

### Task 5: Manual payments with two-person approval (UC-13)

**Files:**
- Create: `apps/fees/manual.py`, `apps/fees/views/manual.py`, templates, migration `0004_manual`
- Modify: `apps/api/urls.py`, `apps/api/views.py` (`POST payments/manual`, name `api:payments_manual`), `tests/isolation/registry.py`
- Test: `tests/fees/test_manual.py`, `tests/api/test_payments_manual_api.py`

**Interfaces:**
- Consumes: `ledger.confirm_payment`, `PlannedLine`, `PlanLimits.check_can_store`, and the `unique_upload_name` storage pattern.
- Produces:
  - `ManualPayment(student, method, amount_kobo, plan JSON [{"invoice_id", "amount_kobo"}], paid_on date, reference_no, photo_name, status in {"pending_approval","approved","rejected"}, recorded_by, decided_by null, decided_at null, reject_reason)`.
  - `manual.record(*, student, method, amount_kobo, plan, paid_on, reference_no, photo=None, by) -> ManualPayment`. It validates:
    - the plan's invoices belong to the student, or to their siblings (Task 10 helper; until then, the student only);
    - the plan amounts sum to `amount_kobo`;
    - each line is ≤ its balance at the time of recording;
    - `reference_no` is required for teller, pos and transfer;
    - `paid_on` is not in the future.

    Below the threshold it confirms immediately.
  - `manual.approve(mp, *, by)` and `manual.reject(mp, *, reason, by)`:
    - the approver must not be the recorder (`PermissionDenied`);
    - an already decided record raises `ResultsStateError`-style `ManualStateError` (409);
    - approval rechecks balances only through `confirm_payment`'s cap and flag.
  - Routes: `fees:manual_new <student_pk>`, `fees:manual_pending`, `fees:manual_decide <pk>` (POST approve/reject), `fees:teller_photo <pk>` (302 signed, fee viewers only).
  - API: `POST /api/v1/payments/manual` with JSON `{student, method, amount_kobo, invoices:[{invoice, amount_kobo}], paid_on, reference_no}`. It returns `201` with `{id, status, payment: {...money_fields} | null}`. Errors use the standard error format with `VALIDATION_ERROR`, 403 or 404.

- [ ] **Step 1: Write failing tests**
  - ₦20,000 cash, below the ₦50,000 threshold: confirms at once, gets a receipt, and has `approved_by == recorded_by`.
  - ₦60,000 teller with a photo: `pending_approval`, invoice unchanged.
    - The same bursar approving gets 403.
    - The admin approving creates the Payment and receipt.
    - Approving again gives 409.
  - Rejecting with a reason leaves the invoice unchanged and is audited without the reason text in the metadata. The reason is stored on the row only.
  - Validation errors:
    - a teller payment without `reference_no`;
    - a future `paid_on`;
    - plan amounts not summing to the amount;
    - another student's invoice (unless a sibling);
    - a line above its balance.
  - Uploads: a photo larger than 5 MB, or of the wrong type, gives a form error. `PlanLimitExceeded` gives a form error.
  - Isolation: an `api:payments_manual POST` case with `expect="refused"` (school A bursar posting school B's student and invoice ids gets 404/403), plus an anonymous case.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): manual payments with two-person approval`.

### Task 6: Gateway adapter, Paystack client and school payout account (FR-11)

**Files:**
- Create:
  - `apps/fees/gateways/__init__.py` (protocol, errors, `get_gateway()`)
  - `apps/fees/gateways/paystack.py`
  - `apps/fees/gateways/fake.py`
  - `apps/fees/views/payout.py`, template `fees/payout.html`
- Modify: `apps/tenancy/models.py` (adds `paystack_subaccount_code`, `payout_bank_name`, `payout_account_last4` to `School`), plus the tenancy migration.
- Test: `tests/fees/test_paystack.py`, `tests/fees/test_payout.py`

**Interfaces:**
- Produces:
  - `class PaymentGateway(Protocol)`, with these methods:
    - `initialize(*, email, amount_kobo, reference, subaccount, callback_url, metadata) -> InitResult(authorization_url: str)`
    - `verify(reference) -> VerifyResult(status: str, amount_kobo: int, currency: str, subaccount: str | None, paid_at: datetime | None, raw: dict)`
    - `banks() -> list[Bank(code, name)]`
    - `resolve_account(account_number, bank_code) -> str`, returning the account name
    - `save_subaccount(*, code: str | None, business_name, bank_code, account_number) -> str`, returning the code
    - `check_signature(raw_body: bytes, header: str) -> bool`
  - Errors: `GatewayUnavailable` and `GatewayRejected(message)`. The latter is for a resolve failure such as "account not found" and carries a safe, user-facing message.
  - `get_gateway() -> PaymentGateway` loads `settings.PAYMENT_GATEWAY`.
  - `PaystackGateway` uses an `httpx.Client` that can be injected for `MockTransport`.
    - `raw` drops the `customer` and `authorization` keys.
    - The `subaccount` in a verify result comes from `data.subaccount.subaccount_code` when present.
  - `FakeGateway` is for DEBUG and tests only and refuses to construct when `DEBUG` is false.
    - It keeps state in the cache.
    - `initialize` returns a URL to `fees:fake_checkout <reference>` (Task 8).
    - `verify` returns whatever the fake checkout recorded.
    - Its signature uses the HMAC of `PAYSTACK_SECRET_KEY or "fake"`.
  - Route `fees:payout` (school_admin): pick a bank from `banks()`, enter a 10-digit account number, then "Check account". The page shows the resolved name, and "Confirm" saves the subaccount and stores the code, bank name and last 4 digits. It is audited as `fees.subaccount_saved` with the last 4 digits only.
  - `School.online_payments_ready` (property) is true when a subaccount code is set.

- [ ] **Step 1: Write failing tests** (all over `MockTransport`, no network)
  - Initialize sends `bearer: "subaccount"`, `currency: "NGN"`, the integer `amount` and the reference, and returns the URL.
  - Verify maps a success payload.
    - `raw` contains no `customer` or `authorization` key.
    - A 500 response or a timeout raises `GatewayUnavailable`.
    - `status: false` raises `GatewayUnavailable`.
  - `check_signature` is true for the right HMAC-SHA512 and false for a tampered body or a missing header.
  - Payout page:
    - a bursar gets 403;
    - the admin flow (resolve → confirm) stores the code, and an update calls `PUT`;
    - account number `"12345"` gives a form error;
    - a resolve rejection shows the safe message;
    - with the gateway down, the page shows "The payment provider is not responding. Please try again shortly." and returns 200.
  - `FakeGateway()` raises `ImproperlyConfigured` with `DEBUG=False`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): payment gateway adapter, Paystack client and school payout account`.

### Task 7: Online checkout: attempts, routing, initialize (§4.5 steps 1–3)

**Files:**
- Create: `apps/fees/checkout.py`, `apps/fees/views/pay.py`, templates `fees/pay.html`, `fees/pay_return.html`, `apps/tenancy` model `PaymentRoute` (shared app, public schema) plus migration, fees migration `0005_attempts`
- Modify: `apps/api/urls.py`, `apps/api/views.py` (`POST payments/initialize`, `api:payments_initialize`), `tests/isolation/registry.py`
- Test: `tests/fees/test_checkout.py`, `tests/api/test_payments_initialize_api.py`

**Interfaces:**
- Consumes: Task 6 gateway; `FeeSettings`; `siblings_of` (Task 10). Until Task 10 exists, use a local stub that returns `[]`, and replace it in Task 10.
- Produces:
  - `PaymentAttempt(reference unique, student, amount_kobo, status, gateway="paystack", raw_response JSON, created_at, updated_at, flag_reason)`
  - `PaymentAttemptLine(attempt, invoice, amount_kobo)`
  - `PaymentRoute(reference unique, schema_name, created_at)` in `apps.tenancy`.
  - `checkout.start(student, lines: list[PlannedLine], *, callback_url) -> PaymentAttempt`:
    - it validates invoice ownership (own or sibling), status not `carried`, the part-payment rules, and a total > 0;
    - it requires `School.online_payments_ready`;
    - it creates the attempt, its lines and the `PaymentRoute` in one `atomic()`, then calls `gateway.initialize` outside the transaction;
    - on `GatewayUnavailable` it marks the attempt `failed` and re-raises.
  - Routes:
    - `fees:pay` (student, GET and POST): lists own and sibling payable invoices, with amount inputs when part payment is allowed.
      - On success it returns 302 to `authorization_url`.
      - When the gateway is down it shows "Online payment is not available right now. You can pay by bank transfer and give the teller to the school bursar." and `bank_transfer_note`.
      - When the school has no payout account it shows "This school has not set up online payments yet."
    - `fees:pay_return` (`?reference=`): read-only status page ("We are confirming your payment…" / "Payment received" / "Payment was not completed"). It never confirms anything; it calls `checkout.status_for(student, reference)`.
  - API: `POST /api/v1/payments/initialize` with `{invoices:[{invoice, amount_kobo}]}` returns `201` with `{reference, authorization_url}`. A student caller only. A gateway outage returns 503 `GATEWAY_UNAVAILABLE`.

- [ ] **Step 1: Write failing tests** (FakeGateway)
  - A student paying own + sibling invoices in full creates an attempt totalling both balances, two lines and a `PaymentRoute` with the school's schema, and returns a 302 to the fake URL.
  - Part payment on with a minimum of ₦5,000:
    - ₦4,000 on an invoice with a ₦50,000 balance is refused;
    - ₦3,000 on an invoice with only ₦3,000 left is accepted;
    - an amount above the balance is refused.
  - With part payment off, any amount ≠ the balance is refused.
  - A non-sibling's invoice id gives 404. A `carried` invoice is refused.
  - With the gateway patched to raise `GatewayUnavailable`, the message and bank note are shown and the attempt is `failed`.
  - `pay_return` with someone else's reference gives 404. A pending reference shows "confirming", and the invoice is still unpaid.
  - Isolation cases:
    - `api:payments_initialize POST` refused for a school A student posting school B invoice ids;
    - an anonymous case;
    - a staff caller gets 403.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): online checkout with payment attempts and routing`.

### Task 8: Webhook, verification and confirmation (§4.5 steps 4–6, FR-12)

**Files:**
- Create: `apps/fees/webhooks.py` (public view), `apps/fees/confirm.py`, `apps/fees/views/fake_checkout.py` (DEBUG only)
- Modify: `config/urls_public.py` (`payments/webhook/paystack/`), `apps/fees/tasks.py`, `apps/core/logging.py`
- Test: `tests/fees/test_webhook.py`, `tests/fees/test_confirm.py`

**Interfaces:**
- Consumes: `PaymentRoute`; `get_gateway()`; `ledger.confirm_payment`.
- Produces:
  - `webhooks.paystack_webhook(request)`, `csrf_exempt` and POST only. Processing order:
    1. Reject bodies over 64 KB (413).
    2. If `check_signature` fails, return 401 with no body and log a warning with no payload.
    3. Parse the JSON. For anything other than event `charge.success`, return 200 and ignore it.
    4. Look up `PaymentRoute` by `data.reference`. If there is none, return 200 and log `unknown reference` by type only.
    5. Enqueue `process_attempt_task.apply_async(args=[reference], queue="payments", headers={"schema_name": route.schema_name})`.
    6. Return 200.
  - It never confirms inline.
  - `confirm.process_attempt(reference) -> str`, which runs inside the school schema:
    1. Lock the attempt `FOR UPDATE`. If it is already final, return its status.
    2. Call `gateway.verify(reference)` **outside** the lock. Re-lock before writing.
    3. If verify reports success and the amount, currency `NGN` and subaccount (`School.paystack_subaccount_code`) all match: call `confirm_payment(method="online", gateway_reference=reference, attempt=attempt, plan=lines, paid_at=verify.paid_at or now)` and set the attempt to `success`.
    4. If it reports success but anything mismatches: set the attempt to `flagged` with `flag_reason` and audit `fees.attempt_mismatch`, with no Payment.
    5. If it reports `failed`/`abandoned`: set the matching attempt status.
    6. If it is still pending, leave it.
    7. On `GatewayUnavailable`, the task retries with backoff (`countdown` 60, 300, 900; max 3), carrying the schema header.
  - `process_attempt_task(reference)` (`TenantTask`, queue `payments`).
  - `fees:fake_checkout <reference>` (DEBUG only, 404 otherwise). It offers "Pay (test)" and "Fail (test)", records the result in FakeGateway, and POSTs a correctly signed webhook body to the public webhook handler in-process, so local testing runs the real path.

- [ ] **Step 1: Write failing tests**
  - A signed `charge.success` with a matching verify gives one Payment, a `success` attempt, a `paid` invoice and one receipt.
  - A bad signature gives 401 and no task.
  - An unknown reference gives 200 and no task.
  - A non-charge event gives 200 and is ignored.
  - `test_duplicate_and_concurrent_confirmation_credit_once`: deliver the same webhook 3 times, then run `process_attempt` directly twice more. The result is exactly 1 Payment, 1 Receipt and the allocations once.
  - Out of order: the student loads `pay_return` before the webhook arrives and sees "confirming", with no credit. After the webhook, it shows "Payment received".
  - `test_verify_mismatch_never_credits`: verify returns amount −1 kobo, then currency `USD`, then another subaccount, then `status="failed"` while the webhook says success. Each gives no Payment, the attempt `flagged` (or `failed` for the last), and one `fees.attempt_mismatch` audit for the mismatches.
  - `test_overpayment_after_manual_payment_is_flagged_not_lost`: the attempt is pending for the full ₦50,000. The bursar records a manual ₦50,000. Then the webhook confirms. The online Payment has `unallocated_kobo == 5_000_000`, is `flagged`, and `invoice.paid_kobo == total_kobo`.
  - A forged return URL (`pay_return?reference=…&status=success`) never changes the invoice.
  - Verify raising `GatewayUnavailable` schedules a retry with the schema header, and the attempt stays `pending`.
  - The webhook view runs on the public host. Requesting the path on a tenant host returns 404.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): signed webhooks with server verification and idempotent confirmation`.

### Task 9: Re-verification job and reconciliation page (§4.5 step 7, §8.1)

**Files:**
- Create: `apps/fees/reconcile.py`, `apps/fees/views/reconcile.py`, template `fees/reconcile.html`
- Modify: `config/settings.py` (`CELERY_BEAT_SCHEDULE`), `apps/fees/tasks.py`
- Test: `tests/fees/test_reconcile.py`

**Interfaces:**
- Produces:
  - `reverify_all_schools()`: a plain Celery task on the public schema, queue `payments`. It fans out `reverify_school_task` per active school with an explicit schema header.
  - `reverify_school_task()` (`TenantTask`): calls `process_attempt` for every `pending` attempt older than 30 minutes, oldest first, capped at 200 per run. Attempts pending for more than 24 hours whose verify result is not success become `abandoned`.
  - Beat schedule entry `"fees-reverify"`: every 900 seconds.
  - `fees:reconcile` (fee viewers): shows flagged attempts, flagged payments (unallocated amount), pending manual approvals, and receipts with no PDF.
    - A **"Mark resolved"** POST (fee managers) on a flagged payment clears `flagged`. It needs a note, keeps `unallocated_kobo` for the record, and is audited.
    - A **"Re-check now"** POST on a flagged or pending attempt runs `process_attempt` synchronously through the task.

- [ ] **Step 1: Write failing tests**
  - Fan-out enqueues one task per active school, each with the right `schema_name` header, and none for inactive schools.
  - Attempt ages:
    - an attempt 31 minutes old that verifies as success is confirmed;
    - one 10 minutes old is untouched;
    - one 25 hours old that verifies as failed becomes `abandoned`.
  - The reconcile page lists a flagged payment's unallocated ₦ amount.
  - "Mark resolved" without a note gives a form error. With a note it clears the flag and writes an audit without the note text.
  - A teacher gets 403.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): pending payment re-verification and reconciliation page`.

### Task 10: Student fees portal, siblings, invoices API (FR-15, UC-12)

**Files:**
- Create: `apps/fees/siblings.py`, `apps/fees/views/portal.py`, template `fees/my_fees.html`
- Modify:
  - `apps/fees/checkout.py`: replace the sibling stub;
  - `apps/fees/manual.py`: allow sibling invoices;
  - `apps/accounts/views/home.py` and the portal template: a "Fees" link, plus the current balance on the profile card;
  - `apps/api/views.py`, `apps/api/urls.py` and `apps/api/serializers.py`: `GET invoices` (`api:invoices`) and balances in `api:me`;
  - `tests/isolation/registry.py`
- Test: `tests/fees/test_siblings.py`, `tests/fees/test_portal.py`, `tests/api/test_invoices_api.py`

**Interfaces:**
- Produces:
  - `siblings.siblings_of(student) -> list[Student]`: active students, excluding self, matched by the Global Constraints rule, ordered by admission number. A single query on normalised contacts: phones are already E.164, and emails are compared with `Lower`.
  - `fees:my_fees` (students only). It shows:
    - each own invoice, newest first, with lines, paid amount, balance and status;
    - "Pay now" (to `fees:pay`) when the balance is > 0;
    - a siblings section listing each sibling's first name, class and outstanding balance only (no lines, receipts or payments), with "Pay for siblings too" taking the student into `fees:pay` with them selected;
    - own receipts, with download links.
  - `GET /api/v1/invoices?student=&term=`:
    - **Students:** their own invoices and those of their siblings. The `student` filter must be self or a sibling, otherwise 404.
    - **Fee viewers:** any student, with `student` required, otherwise 400.
    - Response: the standard pagination with items `{id, student: {id, admission_number, first_name}, term: {id, name, session}, status, total: money_fields, paid: money_fields, balance: money_fields, lines: [{kind, description, amount: money_fields}]}`. Sibling items omit `lines`.
  - `api:me` gains `"balance": money_fields(outstanding_for(student))` for students.

- [ ] **Step 1: Write failing tests**
  - `test_sibling_matching_rules`:
    - same guardian phone → match;
    - main guardian email matching a second guardian's email in different letter case → match;
    - both guardian emails blank and phones different → no match;
    - a withdrawn student with the same phone → no match;
    - self excluded.
  - The portal shows own lines. Siblings show only the first name, class and balance: assert that a sibling's last name and line descriptions are absent.
  - API:
    - a student fetching a non-sibling's invoices with `?student=` gets 404;
    - the bursar without `student` gets 400;
    - money fields are in the exact `{"amount_kobo", "amount_display"}` shape.
  - `api:me` balance is equal to `outstanding_for`.
  - Isolation: an `api:invoices GET` case with `expect="absent"` (a school A student listing must not contain school B's invoice marker), plus an anonymous case.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(fees): student fees portal, sibling checkout and invoices API`.

### Task 11: Debtors list, results blocking and fees dashboard (UC-14, UC-17)

**Files:**
- Create: `apps/fees/debtors.py`, `apps/fees/views/debtors.py`, templates `fees/debtors.html`, `fees/dashboard.html`
- Modify:
  - `apps/results/views/portal.py` (My results) and `apps/results/views/cards.py`;
  - `apps/api/views.py` `ReportCardView`;
  - `apps/fees/urls.py` (`fees:dashboard` as the `fees/` index).
- Test: `tests/fees/test_debtors.py`, `tests/fees/test_results_blocking.py`

**Interfaces:**
- Consumes: `invoicing.outstanding_for`; `FeeSettings.block_results_when_owing`.
- Produces:
  - `debtors.debtors(term, *, level=None, arm=None, min_balance_kobo=1) -> list[DebtorRow(student, arm, total_kobo, paid_kobo, balance_kobo, last_payment_at)]`.
    - It covers students with balance ≥ `min_balance_kobo` on non-`carried` invoices up to and including `term`.
    - It includes withdrawn students, marked as such.
    - It is ordered by balance descending, then admission number.
    - It uses the `invoice(term, status)` index, with no per-student queries (assert with a loose query-count bound).
  - `fees:debtors`: filters for term, level, arm and minimum ₦ amount. The CSV export uses `safe_cell` on every text cell and is served as an attachment with `no-store`. Columns: Admission no., Name, Class, Total, Paid, Balance, Last payment.
  - `fees:dashboard`: term totals (invoiced, collected, outstanding), today's collections, and counts of pending approvals and flagged items, with links.
  - `results_withheld(student, term) -> bool` in `apps/fees/blocking.py`: true when `block_results_when_owing` is on and `outstanding_for(student, up_to_term=term) > 0`.
    - My results shows the exact withheld message for that term instead of its data.
    - The student report-card HTML and API downloads return 403 with the same message (`RESULTS_WITHHELD` in the API).
    - Staff are unaffected.

- [ ] **Step 1: Write failing tests**
  - Debtors:
    - three students owe ₦50,000, ₦10,000 and ₦0, plus a withdrawn student owing ₦5,000. The list has 3 rows in the order 50k, 10k, 5k, with the withdrawn one marked;
    - a ₦20,000 minimum leaves 1 row;
    - a carried invoice's balance is counted once, via the brought-forward line.
  - The CSV guards a student named `=cmd`.
  - Results blocking:
    - with blocking on, a student owing ₦1 sees the withheld message on My results and gets 403 on the report card;
    - after the balance is paid, they see results;
    - with blocking off, a debtor sees results;
    - the principal can still open the student's card.
  - The dashboard totals equal the sum of invoices and payments for the term.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS. Then run the full CI set:
  - `ruff check`
  - `ruff format --check`
  - `mypy apps config`
  - `makemigrations --check --dry-run`
  - `scripts/with-pango .venv/bin/pytest -W error -m "not cloudinary"`
- [ ] **Step 5: Commit** `feat(fees): debtors list, results blocking and fees dashboard`.

---

## Exit criteria for P4 (roadmap)

- A ₦100 test payment in Paystack **test mode** settles to a pilot school's test subaccount. This is a manual check per pilot, with real `PAYSTACK_SECRET_KEY` test keys and the webhook URL set in the Paystack dashboard to `https://<platform host>/payments/webhook/paystack/`.
- Duplicate, delayed, out-of-order and failed webhooks cannot double-credit or credit wrongly. Covered by Task 8.
- Every new `api/v1/` route has an authenticated cross-school isolation case.

## Decisions left for later phases

- Fee reminders to guardians by SMS or email, on school-set days: **P5**.
- Flutterwave as a second gateway behind the same adapter, and Remita: later, on demand.
- Refunding overpayments through the gateway: never automatic. The bursar resolves flagged payments offline (Task 9).
- Sibling discount applied automatically from sibling matching: for now the bursar assigns discounts by hand. Revisit with pilot schools.
- Moving a student between arms or levels mid-term does not re-price their invoice. The bursar adjusts it manually.
