# P1 Tenancy, Onboarding and Accounts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A school can sign up on the platform, get its own branded portal on a subdomain, and its staff (invited, MFA-protected where required) and students (admission-number login) can log in, with every sensitive account event audited.

**Architecture:** Builds on P0. New tenant apps: `apps.audit` (append-only audit log), `apps.files` (stored-file sizes for quotas), `apps.branding` (per-school look). `apps.accounts` grows from a placeholder into users, roles (Django groups), login, MFA (django-otp TOTP), invites and OTP password reset. `apps.tenancy` gains plans, plan limits, a seeding registry and self-service signup with background provisioning. Pages are server-rendered Django templates; one JSON endpoint (`GET /api/v1/me`).

**Tech Stack:** P0 stack + `argon2-cffi` (via `django[argon2]`), `django-otp`, `segno` (QR as SVG, pure Python), `Pillow` (logo validation).

**Spec:** `School Management Platform — System Design.pdf` (§1.4, §2.1 UC-01/UC-02, FR-01/FR-02, §4.1, §5.1, §6.3, §6.4, §7.1–7.5) plus `docs/ROADMAP.md` §1 (Cloudinary; no parent accounts — students log in with admission number, OTP goes to the primary guardian's phone) and roadmap phase **P1**. P0 is merged; read `README.md` for how to run things.

## Global Constraints

Everything in the P0 plan's Global Constraints still holds (hostname-only tenancy, schema-prefixed cache, `TenantTask`, Cloudinary prefixes and `type="authenticated"` for child files, kobo money, §6.5 error body, JSON logs without secrets, `Africa/Lagos`, `DD/MM/YYYY`). Added for P1:

- Passwords hashed with **Argon2** (`django.contrib.auth.hashers.Argon2PasswordHasher` first in `PASSWORD_HASHERS`) (§7.3).
- **MFA required** for roles `school_admin`, `principal`, `bursar` (§6.3, §7.3). TOTP app in P1; SMS second factor is out of scope (SIM-swap risk, §7.6).
- **Login / OTP limits:** 5 per 15 minutes (900 s) per identifier and per IP (§6.4); lockout after 5 failed logins (§7.3).
- **Staff session idle timeout 30 minutes** (1800 s); students are not idle-timed-out; `SESSION_COOKIE_AGE` = 14 days (§7.3).
- **Staff are invited by link, never given passwords by email** (§7.3). Invite links expire after **72 hours** (`PASSWORD_RESET_TIMEOUT = 259200`).
- **Phones stored in +234 E.164 form** (e.g. `+2348031234567`) (§5.2).
- **Students log in with admission number + password**; password reset OTP goes to the student's `recovery_phone` (the primary guardian's phone, kept in sync by P2 admission) (roadmap §1.2).
- **OTP codes:** 6 digits, valid **10 minutes**, at most **5 verification attempts** per code, stored only as an HMAC.
- **No user enumeration:** login failure and reset-request messages are identical whether or not the account exists.
- **Audit log is append-only**, enforced in the database (§5.2 `audit_event`, §7.5).
- Logged actions in P1: `auth.login`, `auth.login_failed`, `auth.lockout`, `auth.logout`, `auth.password_reset`, `auth.mfa_enrolled`, `auth.mfa_reset`, `user.invited`, `user.invite_accepted`, `user.deactivated`, `user.reactivated`, `role.added`, `role.removed`, `branding.updated`, `school.provisioned` (§7.5).
- **No parent accounts** anywhere (roadmap §1.2).
- Pages work at **360 px** width and stay light (no JS framework; no Tailwind build in P1 — one small hand-written stylesheet). Ruling, revisit in P2 when UI grows.
- Plan limits per §6.4: students Basic 300 / Standard 1,000 / Premium unlimited. Storage and SMS numbers are **provisional placeholders** until open question Q3.

## Review Focus

1. **Admission number typed loosely** (`" gfs/2026/0042 "`): must log the student in exactly like `GFS/2026/0042`. Test in Task 3.
2. **A session cookie from school B presented on school A's host**: must not authenticate (sessions live per schema). Test in Task 9.
3. **The same phone in different formats** (`0803 123 4567` at invite, `+2348031234567` at login): must find the same user. Test in Task 3.
4. **Bad subdomains at signup** (`Admin`, `-abc`, `public`, `9lives`, an existing one, `www`): field error, and no schema or school row is left behind. Test in Task 11.
5. **A user deactivated while logged in**: the next request is treated as logged out. Test in Task 9.

---

## File Structure

```
apps/core/
  ratelimit.py          # RateLimit (cache counters, tenant-prefixed)
  sms.py                # send_sms(), backends (console, locmem), deliver_sms TenantTask
  templates/core/base.html, static/core/app.css
config/formats/en_GB/formats.py        # DD/MM/YYYY for templates
apps/tenancy/
  models.py             # + Plan, School fields (plan, levels_offered, contact_*, provisioning_status)
  limits.py             # PlanLimits, PlanLimitExceeded
  seeding.py            # register_seeder(), seed_school()
  signup.py             # SignupForm, subdomain rules
  provisioning.py       # provision_school task
  views.py / templates  # landing, signup, signup status
  management/commands/ensure_public_tenant.py
apps/files/             # tenant app: StoredFile
apps/audit/             # tenant app: AuditEvent, record(), audit log page
apps/accounts/
  models.py             # User fields
  roles.py              # role constants, set_roles(), require_roles, HasRole
  identifiers.py        # normalise_ng_phone(), classify_identifier()
  backends.py           # IdentifierBackend
  services.py           # create_student_user(), invite_staff(), reset OTP helpers
  middleware.py         # StaffIdleTimeoutMiddleware, MfaEnforcementMiddleware
  views/ login.py mfa.py reset.py invite.py staff.py
  api.py                # GET /api/v1/me
  forms.py, urls.py, templates/accounts/*
apps/branding/          # tenant app: Branding singleton, context processor, settings page
```

---

### Task 1: Carried-over fixes from P0

**Files:**
- Modify: `pyproject.toml`, `README.md`, `apps/core/logging.py`, `apps/core/errors.py`, `apps/core/tasks.py`, `config/settings.py`, `apps/tenancy/management/commands/seed_demo_schools.py`
- Create: `apps/tenancy/management/commands/ensure_public_tenant.py`, `config/formats/__init__.py`, `config/formats/en_GB/__init__.py`, `config/formats/en_GB/formats.py`
- Test: `tests/core/test_logging.py`, `tests/core/test_errors.py`, `tests/core/test_tasks.py`, `tests/core/test_formats.py`, `tests/tenancy/test_public_tenant.py`

**Interfaces:**
- Produces: `apps.core.logging.is_sensitive_key(key: str) -> bool`; command `ensure_public_tenant` (idempotent; public `School(name="Platform")` + `Domain(settings.PLATFORM_DOMAIN)`); settings `PLATFORM_DOMAIN` (env, default `"localhost"`), `PLATFORM_URL_TEMPLATE` (env, default `"http://{subdomain}.localhost:8000"`); `CELERY_TASK_ALWAYS_EAGER = True` and `CELERY_TASK_EAGER_PROPAGATES = True` **only when `REDIS_URL` is unset** (local dev without a worker); `FORMAT_MODULE_PATH = ["config.formats"]`.

- [ ] **Step 1: Write failing tests:**
  - `is_sensitive_key` — key split on `_`, `-`, `.` and lowercased; sensitive when a segment equals one of `password, otp, token, secret, card, pin, authorization` optionally followed by digits. True: `password`, `new_password`, `password1`, `otp`, `otp_code`, `card_number`, `user_pin`, `api_token`, `client_secret`, `Authorization`. False: `shipping`, `mapping`, `footprint`, `discard`, `pinned`, `tokenizer`. `RedactFilter` uses it (existing redaction test still passes).
  - Retry-After: a `Throttled(wait=7.2)` response has header `Retry-After: 8`.
  - Task context on failure: connect a `task_failure` receiver in the test that captures `schema_name_var.get()`; a `TenantTask` raising `ValueError` in `school_a` → captured `"school_a"`; after the task both vars are `"-"`. (Sentry's Celery integration captures inside the same window.)
  - `date_format(date(2026, 10, 11))` == `"11/10/2026"`; template `{{ d|date }}` renders `11/10/2026`; `DATETIME_FORMAT` renders `11/10/2026 14:05`.
  - `ensure_public_tenant` run twice → one public `School`, one `Domain` with `settings.PLATFORM_DOMAIN`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/core tests/tenancy -W error` → new tests FAIL.
- [ ] **Step 3: Implement.** Move the contextvar set/reset out of `TenantTask.__call__` into `task_prerun` / `task_postrun` receivers (in `apps/core/tasks.py`) that act only for `TenantTask` senders; postrun always resets. `Retry-After` uses `math.ceil`. Add `pgserver` to the `dev` extras and drop the separate install line from the README; README deploy section says "run `python manage.py ensure_public_tenant`". `seed_demo_schools` calls `ensure_public_tenant` first.
- [ ] **Step 4: Run** the same command → PASS; full suite `-W error -m "not cloudinary"` green.
- [ ] **Step 5: Commit** `fix: carry-over hardening from P0 (redaction, retry-after, task context, formats, public tenant)`.

### Task 2: Isolation harness upgrades

**Files:**
- Modify: `tests/isolation/registry.py`, `tests/isolation/test_routes.py`, `tests/isolation/urlconf_unregistered.py`

**Interfaces:**
- Produces: guard keyed on **(route name, HTTP method)**. `unregistered_api_routes(urlconf, registered: set[tuple[str, str]] | None = None) -> list[str]` returns entries like `"students:detail PATCH"` (unnamed routes still by route string). A route's methods are read from the view: DRF `view.cls` → those of `get, post, put, patch, delete` it defines; plain Django views → `GET`. `EXEMPT_ROUTES` stays a set of names (all methods exempt). `IsolationCase` unchanged.

- [ ] **Step 1: Write failing tests:**
  - test urlconf gains a DRF `APIView` with `get` and `patch` at `api/v1/things/<int:pk>/` named `things:detail`; with only `("things:detail", "GET")` registered the guard reports `"things:detail PATCH"`, and not `GET`.
  - `test_cross_school_access_is_refused` now compares every concrete field (`{f.attname: getattr(obj, f.attname) for f in obj._meta.concrete_fields}` after `refresh_from_db`) and builds request kwargs per method: GET/DELETE send no body; POST/PUT/PATCH send `json.dumps(case.body or {})` with `content_type="application/json"`.
  - `test_exempt_routes_start_with_healthz` replaced by `test_exempt_routes_exist` — every exempt name resolves somewhere in `config.urls_tenant`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/isolation -W error` → FAIL.
- [ ] **Step 3: Implement** the method-aware walk and the stricter comparison.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `test(isolation): method-aware route guard and stricter unchanged-object check`.

### Task 3: Users, identifiers, roles and seeding registry

**Files:**
- Modify: `apps/accounts/models.py`, `config/settings.py` (`PASSWORD_HASHERS`, `AUTHENTICATION_BACKENDS`), `pyproject.toml` (`Django[argon2]`)
- Create: `apps/accounts/identifiers.py`, `apps/accounts/roles.py`, `apps/accounts/backends.py`, `apps/accounts/services.py`, `apps/tenancy/seeding.py`, migration
- Test: `tests/accounts/test_identifiers.py`, `tests/accounts/test_users.py`, `tests/accounts/test_roles.py`, `tests/tenancy/test_seeding.py`, `tests/accounts/factories.py`

**Interfaces:**
- Produces:
  - `User` (still `AbstractUser`, `username` auto-set to `f"u_{uuid4().hex[:12]}"` when blank): `kind: "staff" | "student"`, `phone: str | None` (unique), `email` unique when non-empty (conditional `UniqueConstraint`), `admission_number: str | None` (unique, stored uppercase), `recovery_phone: str | None`; properties `roles -> frozenset[str]` (group names), `is_student -> bool`, `requires_mfa -> bool`; method `has_role(*roles: str) -> bool`.
  - `normalise_ng_phone(raw: str) -> str` (raises `ValueError`); `classify_identifier(raw: str) -> tuple[Literal["email", "phone", "admission"], str]`; `normalise_admission_number(raw: str) -> str`.
  - `roles.py`: `PROPRIETOR = "proprietor"`, `SCHOOL_ADMIN = "school_admin"`, `PRINCIPAL = "principal"`, `BURSAR = "bursar"`, `TEACHER = "teacher"`; `STAFF_ROLES` (that order); `MFA_REQUIRED_ROLES = frozenset({SCHOOL_ADMIN, PRINCIPAL, BURSAR})`; `seed_roles(school) -> None` (creates one `Group` per role); `set_roles(user, roles: Iterable[str], *, by: User | None, request=None) -> None` (audits `role.added` / `role.removed` per change — audit arrives in Task 4, so this task leaves a `# audit: Task 4` call site that Task 4 wires); `require_roles(*roles)` view decorator (403 for authenticated users without any of the roles, redirect to login when anonymous); DRF permission `HasRole.with_roles(*roles)`.
  - `IdentifierBackend(ModelBackend).authenticate(request, identifier: str | None = None, password: str | None = None) -> User | None` — email/phone match staff only, admission number matches students only.
  - `services.create_student_user(*, admission_number: str, full_name: str, recovery_phone: str | None, password: str) -> User` (for P2 admission).
  - `seeding.register_seeder(fn: Callable[[School], None]) -> Callable`; `seed_school(school: School) -> None` runs every registered seeder inside `schema_context(school.schema_name)`, in registration order, idempotently. `seed_roles` is registered by `apps.accounts`' `AppConfig.ready()`.

- [ ] **Step 1: Write failing tests:**
  - phones: `"0803 123 4567"`, `"+234 803 123 4567"`, `"2348031234567"`, `"08031234567"` → `"+2348031234567"`; `"0803123456"`, `"+44 7700 900123"`, `"01-2345678"`, `""` raise `ValueError`.
  - `classify_identifier`: `"Ada@School.NG"` → `("email", "ada@school.ng")`; `"0803 123 4567"` → `("phone", "+2348031234567")`; `" gfs/2026/0042 "` → `("admission", "GFS/2026/0042")`.
  - backend *(Review Focus 1, 3)*: staff created with phone `0803 123 4567` authenticates with identifier `+2348031234567`; student `GFS/2026/0042` authenticates with `" gfs/2026/0042 "`; a student cannot log in by recovery phone; staff cannot log in by an admission number; wrong password → `None`; inactive user → `None`.
  - Argon2: a new user's `password` starts with `argon2$`.
  - roles: `seed_school(school_a)` twice → exactly 5 groups; `has_role`; a student given a role → `ValidationError` from `set_roles`; `requires_mfa` true for `bursar`, false for `teacher` and `proprietor`.
  - email uniqueness: two staff with `""` email allowed; two with `a@b.ng` → `IntegrityError`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/accounts tests/tenancy -W error` → FAIL.
- [ ] **Step 3: Implement** models + migration, helpers, backend, roles, seeding, services. Factories (`StaffFactory`, `StudentFactory`) in `tests/accounts/factories.py` for later tasks.
- [ ] **Step 4: Run** → PASS; full suite green (the session schools now get seeded roles only when `seed_school` is called — conftest calls `seed_school` for both).
- [ ] **Step 5: Commit** `feat(accounts): staff and student users, identifiers, roles, seeding registry`.

### Task 4: Append-only audit log

**Files:**
- Create: `apps/audit/{__init__,apps,models,services}.py`, migration with `RunSQL`
- Modify: `config/settings.py` (`TENANT_APPS`), `apps/core/request_id.py` (`client_ip`), `apps/accounts/roles.py` (wire `set_roles` auditing)
- Test: `tests/audit/test_audit.py`

**Interfaces:**
- Produces:
  - `AuditEvent`: `at` (auto, indexed), `actor_id: int | None` (no FK — rows never change), `actor_label: str`, `action: str` (indexed), `object_type: str`, `object_id: str`, `before: dict | None`, `after: dict | None`, `ip: str | None`, `request_id: str`.
  - `record(action: str, *, request=None, actor=None, obj=None, before: dict | None = None, after: dict | None = None) -> AuditEvent` — actor defaults to `request.user` when authenticated; keys for which `is_sensitive_key` is true are dropped from `before`/`after`.
  - `client_ip(request) -> str | None` — `request.META[settings.CLIENT_IP_HEADER]` when that setting is set (e.g. `"HTTP_CF_CONNECTING_IP"` behind Cloudflare), else `REMOTE_ADDR`. Setting default `None`.

- [ ] **Step 1: Write failing tests:**
  - `record("auth.login", request=req)` stores actor id/label, ip, request id.
  - `record(..., after={"identifier": "x", "password": "p", "otp_code": "1"})` stores only `identifier`.
  - **append-only in the DB:** `AuditEvent.objects.filter(pk=e.pk).update(action="x")` and `.delete()` and a raw `UPDATE`/`DELETE` via `connection.cursor()` each raise `django.db.InternalError` (Postgres trigger); the row is unchanged.
  - `set_roles(user, {"bursar"}, by=admin)` then `set_roles(user, {"teacher"}, by=admin)` → events `role.added bursar`, `role.removed bursar`, `role.added teacher` in that order with `actor_id == admin.pk`.
  - events written in `school_a` are not visible in `school_b`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/audit -W error` → FAIL.
- [ ] **Step 3: Implement.** Migration creates `audit_event_append_only()` (raises `'audit_event is append-only'`) and a `BEFORE UPDATE OR DELETE` trigger, with reverse SQL dropping both. The function is created unqualified so it lands in each school's schema.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(audit): append-only audit log with role-change events`.

### Task 5: Plans, stored files and plan limits

**Files:**
- Modify: `apps/tenancy/models.py` (+ `Plan`, `School.plan`), `apps/core/storage.py` (quota check + `StoredFile` bookkeeping), `config/settings.py` (`TENANT_APPS` += `apps.files`)
- Create: `apps/files/{__init__,apps,models}.py`, `apps/tenancy/limits.py`, migrations (incl. data migration seeding plans)
- Test: `tests/tenancy/test_limits.py`, `tests/core/test_storage.py`

**Interfaces:**
- Produces:
  - `Plan` (public schema): `code` (unique slug), `name`, `max_students: int | None`, `storage_bytes: int`, `sms_credits_per_term: int`, `is_active`. Seeded: `basic` (300, 2 GiB, 500), `standard` (1000, 10 GiB, 2000), `premium` (None, 50 GiB, 5000) — storage/SMS **provisional (Q3)**. `School.plan` FK (`PROTECT`, nullable; null means `basic`).
  - `StoredFile` (tenant): `name` (unique), `bytes`, `delivery_type`, `created_at`, `updated_at`.
  - `PlanLimitExceeded(ApiError)` — code `"PLAN_LIMIT_EXCEEDED"`, status 403.
  - `PlanLimits.for_current_school() -> PlanLimits`; `.plan`; `.storage_used_bytes() -> int`; `.check_can_store(extra_bytes: int) -> None`; `.student_count() -> int` (active `kind="student"` users; P2 may switch to Student records); `.check_can_add_students(count: int = 1) -> None`.
  - Storage: `_save` calls `check_can_store(size)` **before** uploading (overwriting an existing name counts only the size difference), then `update_or_create`s the `StoredFile` row; `delete` removes it.

- [ ] **Step 1: Write failing tests:**
  - plans seeded with the values above; `PlanLimits` for a school with `plan=None` uses `basic`.
  - storage: with usage at `storage_bytes - 10`, saving 11 bytes raises `PlanLimitExceeded` and `cloudinary.uploader.upload` is **not** called; saving 10 bytes succeeds and a `StoredFile` with `bytes=10` exists; overwriting the same name with 4 bytes leaves `storage_used_bytes()` reduced accordingly; `delete` removes the row.
  - students: on `basic`, 300 active students → `check_can_add_students()` raises; 299 → passes; `premium` never raises.
  - usage in `school_a` does not count toward `school_b`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/tenancy/test_limits.py tests/core/test_storage.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** `feat(tenancy): plans, stored-file tracking and plan limits`.

### Task 6: Login, logout, lockout and staff idle timeout

**Files:**
- Create: `apps/core/ratelimit.py`, `apps/core/templates/core/base.html`, `apps/core/static/core/app.css`, `apps/accounts/forms.py`, `apps/accounts/views/__init__.py`, `apps/accounts/views/login.py`, `apps/accounts/urls.py`, `apps/accounts/middleware.py`, `apps/accounts/templates/accounts/login.html`, tenant home view + template
- Modify: `config/urls_tenant.py` (`accounts/` with namespace `accounts`, `/` home), `config/settings.py` (`LOGIN_URL = "accounts:login"`, `SESSION_COOKIE_AGE`, `STAFF_IDLE_TIMEOUT_SECONDS = 1800`, middleware), `apps/core/request_id.py` (role logging)
- Test: `tests/core/test_ratelimit.py`, `tests/accounts/test_login.py`, `tests/accounts/test_idle_timeout.py`

**Interfaces:**
- Produces:
  - `RateLimit(scope: str, limit: int = 5, window: int = 900)` with `hit(key: str) -> int`, `is_blocked(key: str) -> bool`, `retry_after(key: str) -> int`, `reset(key: str) -> None`. Keys are cache keys (tenant-prefixed by the cache key function).
  - Views: `accounts:login` (GET/POST `identifier`, `password`, optional `next`), `accounts:logout` (POST only), tenant home `home` (login required; "Welcome, {first name}" placeholder).
  - Session key `mfa_pending` = `{"user_id": int, "at": epoch}` — written instead of logging in when `user.requires_mfa`; Task 7 consumes it.
  - `StaffIdleTimeoutMiddleware` (after `AuthenticationMiddleware`).
  - Log `role` field: `"student"` for students, comma-joined sorted roles for staff, `"-"` when anonymous.

- [ ] **Step 1: Write failing tests:**
  - RateLimit: 5 hits → blocked; `retry_after` between 1 and 900; `reset` unblocks; same key in two schools counts separately.
  - login success (teacher) → redirect to `/`, session authenticated, audit `auth.login`; `next=https://evil.example/` is ignored (redirect to `/`).
  - wrong password, unknown identifier, inactive user → the **same** error text and status 200; audit `auth.login_failed` with the identifier in `after`.
  - 5 failures for one identifier → 6th attempt with the correct password gets status 429 and the "too many attempts" message; audit `auth.lockout` once. 5 failures from one IP across different identifiers → blocked too.
  - a bursar with the right password is **not** logged in; session has `mfa_pending` and the response redirects to `accounts:mfa` (URL name exists after Task 7; until then assert the session key and a 302).
  - logout via GET → 405; POST → logged out, audit `auth.logout`.
  - idle timeout: staff session with `last_seen` 1801 s ago → next request redirects to login and the session is flushed; 1799 s → passes and `last_seen` updates; a student at 1801 s stays logged in.
  - log lines during a request by a bursar+teacher have `role == "bursar,teacher"`; by a student `"student"`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/core/test_ratelimit.py tests/accounts -W error` → FAIL.
- [ ] **Step 3: Implement.** `base.html` shows the school name (from `request.tenant`) and loads `app.css` (≤ 10 KB, mobile-first, readable at 360 px).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(accounts): login, logout, lockout and staff idle timeout`.

### Task 7: TOTP multi-factor authentication

**Files:**
- Create: `apps/accounts/views/mfa.py`, templates `accounts/mfa_setup.html`, `accounts/mfa_verify.html`
- Modify: `pyproject.toml` (`django-otp`, `segno`), `config/settings.py` (`django_otp`, `django_otp.plugins.otp_totp` in `TENANT_APPS`; `django_otp.middleware.OTPMiddleware` after auth; `MfaEnforcementMiddleware`), `apps/accounts/urls.py`, `apps/accounts/middleware.py`
- Test: `tests/accounts/test_mfa.py`

**Interfaces:**
- Consumes: session `mfa_pending` (Task 6), `RateLimit`, `record`.
- Produces: `accounts:mfa` (verify), `accounts:mfa_setup` (enrol: unconfirmed `TOTPDevice(name="default")` created once; page shows an inline SVG QR via `segno` and the base32 secret); `MfaEnforcementMiddleware` — an authenticated user with `requires_mfa` whose request is not OTP-verified is logged out and redirected to login (covers roles granted mid-session); `reset_mfa(user, *, by, request) -> None` deletes the user's devices and audits `auth.mfa_reset` (used by Task 9). `mfa_pending` expires after **300 s**.

- [ ] **Step 1: Write failing tests** (tokens from `django_otp.oath.totp(device.bin_key)`):
  - bursar without a device → after password, `accounts:mfa_setup`; correct code → logged in, device confirmed, `request.user.is_verified()`, audits `auth.mfa_enrolled` and `auth.login`.
  - bursar with a device → `accounts:mfa`; correct code → logged in; wrong code 5 times → pending cleared, back to login, audit `auth.login_failed` per wrong code.
  - pending older than 300 s → redirected to login.
  - visiting `accounts:mfa` without a pending session → redirect to login.
  - teacher logged in, then given `bursar` → next request logs them out (MfaEnforcement).
  - `reset_mfa` removes devices and audits.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/accounts/test_mfa.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** `feat(accounts): TOTP MFA for admin, principal and bursar`.

### Task 8: SMS backend and OTP password reset

**Files:**
- Create: `apps/core/sms.py`, `apps/accounts/views/reset.py`, templates `accounts/reset_request.html`, `accounts/reset_verify.html`
- Modify: `apps/accounts/services.py` (OTP helpers), `apps/accounts/urls.py`, `config/settings.py` (`SMS_BACKEND`)
- Test: `tests/core/test_sms.py`, `tests/accounts/test_reset.py`

**Interfaces:**
- Produces:
  - `send_sms(to: str, body: str) -> None` — enqueues `deliver_sms` (`TenantTask`, queue `notifications`), which calls `settings.SMS_BACKEND` (dotted path) `.send(to, body)`. Backends: `ConsoleSmsBackend` (logs to logger `sms` with the phone masked to its last 4 digits; default only when `DEBUG`), `LocmemSmsBackend` (`outbox: list[SmsMessage(to, body)]`, used in tests). When `DEBUG` is off and `SMS_BACKEND` is unset, `send_sms` raises `ImproperlyConfigured` (real provider arrives in P5).
  - `issue_reset_code(user) -> str | None` (returns the code only for the caller to send; stores HMAC-SHA256 with `SECRET_KEY` in cache, TTL 600 s, attempts 0); `verify_reset_code(user, code: str) -> bool` (constant-time compare; 5 failed attempts invalidates); `reset_phone_for(user) -> str | None` (`phone` for staff, `recovery_phone` for students).
  - Views: `accounts:reset` (identifier; generic response always; limit 5/900 s per identifier and per IP; stores identifier in session `reset_identifier`), `accounts:reset_verify` (code + new password twice via `SetPasswordForm` validators; success sets the password, clears the code, audits `auth.password_reset`, redirects to login — never logs in).
  - SMS text: `"{school name}: your password reset code is {code}. It expires in 10 minutes."`

- [ ] **Step 1: Write failing tests:**
  - student with recovery phone → SMS in locmem outbox to `+2348031234567` containing a 6-digit code; verify with the code + valid new password → can log in with the new password; old password fails; audit `auth.password_reset`.
  - unknown identifier and a staff user with no phone → identical page text, nothing sent.
  - wrong code 5 times → the right code then fails; expired code (patch time +601 s) fails.
  - 6th reset request in 15 minutes for one identifier → 429, nothing sent.
  - console backend log line contains `***4567`, not the full number, and the record passes through `RedactFilter`.
  - `send_sms` with `DEBUG=False` and no backend → `ImproperlyConfigured`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/core/test_sms.py tests/accounts/test_reset.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(accounts): sms backend and otp password reset`.

### Task 9: Staff invites, staff management and `GET /api/v1/me`

**Files:**
- Create: `apps/accounts/views/invite.py`, `apps/accounts/views/staff.py`, `apps/accounts/api.py`, templates `accounts/invite_accept.html`, `accounts/invite_invalid.html`, `accounts/staff_list.html`, `accounts/staff_form.html`, email templates `accounts/email/invite_subject.txt`, `accounts/email/invite_body.txt`
- Modify: `apps/accounts/services.py`, `apps/accounts/urls.py`, `config/urls_tenant.py` (`api/v1/` include with namespace `api`; `staff/` namespace `staff`), `config/settings.py` (`PASSWORD_RESET_TIMEOUT = 259200`, `EMAIL_*` from env; console backend default in DEBUG), `tests/isolation/registry.py`
- Test: `tests/accounts/test_invites.py`, `tests/accounts/test_staff.py`, `tests/accounts/test_me_api.py`, `tests/isolation/test_routes.py`

**Interfaces:**
- Consumes: `set_roles`, `reset_mfa`, `record`, `send_sms`, `PlanLimits` not needed.
- Produces:
  - `invite_staff(*, full_name: str, email: str | None, phone: str | None, roles: Iterable[str], by: User | None, base_url: str, request=None) -> User` — needs email or phone; user has an unusable password; sends the invite link `"{base_url}/accounts/invite/{uidb64}/{token}/"` by email (if any) and SMS (if any); audits `user.invited`. Token from Django's `default_token_generator` (one-use: setting the password invalidates it).
  - `accounts:invite_accept` (valid → `SetPasswordForm` → audit `user.invite_accepted` → redirect to login; invalid/expired → `invite_invalid.html`, status 400).
  - Staff pages, `require_roles(SCHOOL_ADMIN, PROPRIETOR)`: `staff:list`, `staff:invite`, `staff:edit` (roles, deactivate/reactivate with audits `user.deactivated` / `user.reactivated`, resend invite, reset MFA). Guard: the last active `school_admin` cannot lose that role or be deactivated (form error).
  - `GET /api/v1/me` (route name `api:me`, DRF, `IsAuthenticated`, session auth) → `{"id", "kind", "full_name", "roles": [...], "admission_number": str | null, "school": {"name"}}`.
  - Registry: `IsolationCase("api:me", "GET", url=lambda u: "/api/v1/me", make_obj=StudentFactory.create)`.

- [ ] **Step 1: Write failing tests:**
  - invite by email and phone → `mail.outbox` and SMS outbox each contain the same link; accept with valid password → can log in; reusing the link → invalid page; link older than 72 h → invalid page.
  - non-admin (teacher) → `staff:list` 403; anonymous → redirect to login.
  - roles edit audits via `set_roles`; removing `school_admin` from the only admin → form error, role kept; deactivating the only admin → form error.
  - *(Review Focus 5)* user deactivated while logged in → next request to `home` redirects to login.
  - `/api/v1/me` as a student → 200 with `kind == "student"` and the admission number; anonymous → 401 with the §6.5 body.
  - *(Review Focus 2)* log in on school B with `client_b`, copy its `sessionid` cookie onto `client_a` → `GET /api/v1/me` on school A → 401.
  - **sentinel:** `test_tenant_api_has_routes` — the real tenant urlconf has at least one `api/v1/` route; the guard test passes with `api:me GET` registered.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/accounts tests/isolation -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; full suite green.
- [ ] **Step 5: Commit** `feat(accounts): staff invites, staff management and /api/v1/me`.

### Task 10: School branding

**Files:**
- Create: `apps/branding/{__init__,apps,models,forms,views,urls,context_processors}.py`, templates `branding/settings.html`, migration
- Modify: `config/settings.py` (`TENANT_APPS`, context processor), `config/urls_tenant.py` (`settings/branding/`, name `branding:settings`), `apps/core/storage.py` (`image_url`), `apps/core/templates/core/base.html`, `apps/accounts/templates/accounts/login.html`, `pyproject.toml` (`Pillow`)
- Test: `tests/branding/test_branding.py`

**Interfaces:**
- Produces:
  - `Branding` singleton per school (pk always 1): `motto` (200), `address` (300), `primary_color` (default `#1F4E79`), `accent_color` (default `#F2B705`), both validated `^#[0-9A-Fa-f]{6}$`; `logo` (`ImageField`, storage `storages["public"]`, names from `unique_upload_name("branding", filename)`); `updated_at`.
  - `get_branding() -> Branding` (cached under key `"branding"`, invalidated on save).
  - `TenantCloudinaryStorage.image_url(name: str, *, width: int) -> str` — public image URL with `w_{width},c_limit,f_auto,q_auto`.
  - Context processor adds `branding` on school hosts, `None` on the public host.
  - `branding:settings` page, `require_roles(SCHOOL_ADMIN, PROPRIETOR)`; saving audits `branding.updated` with changed fields.
  - Logo rules: `.png .jpg .jpeg .webp`, at most **1 MB** (1,048,576 bytes), must open with Pillow.

- [ ] **Step 1: Write failing tests** (Cloudinary upload patched):
  - login page on `school_a` shows the school name, motto and `--primary: #1F4E79` style; with a logo, the `<img>` URL contains `w_240,c_limit,f_auto,q_auto` and `dev/school_a/branding/`.
  - invalid colour, `.gif` logo, 1 MB + 1 byte logo, and a `.png` that is not an image → form errors, nothing uploaded.
  - teacher → 403; school admin save → audit `branding.updated`; cached branding refreshes after save.
  - public host landing page renders with `branding` = `None` (no error).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/branding -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(branding): per-school logo, colours and motto on the portal`.

### Task 11: Self-service school signup and provisioning

**Files:**
- Create: `apps/tenancy/signup.py`, `apps/tenancy/provisioning.py`, templates `tenancy/signup.html`, `tenancy/signup_status.html`, migration
- Modify: `apps/tenancy/models.py` (School fields), `apps/tenancy/views.py`, `config/urls_public.py`, `apps/tenancy/templates/tenancy/landing.html` (link to signup)
- Test: `tests/tenancy/test_signup.py`

**Interfaces:**
- Consumes: `seed_school`, `invite_staff`, `set_roles`, `Plan`, `RateLimit`, `record`.
- Produces:
  - `School` fields: `levels_offered: list[str]` (JSON; subset of `nursery, primary, jss, sss`), `contact_name`, `contact_phone` (E.164), `contact_email`, `provisioning_status: "pending" | "ready" | "failed"` (default `"ready"` so existing rows and test schools stay valid).
  - `validate_subdomain(value: str) -> str` (lowercased): 3–30 chars, `^[a-z][a-z0-9-]*[a-z0-9]$`, no `--`, not in `RESERVED_SUBDOMAINS = {"www", "api", "admin", "app", "mail", "static", "media", "public", "platform", "help", "support", "status"}`, not starting with `pg`, not already a schema or domain. Schema name = subdomain with `-` → `_`.
  - `SignupForm` (school_name, subdomain, levels_offered ≥1, contact_name, contact_phone, contact_email, plan code, accept_terms required).
  - View `signup` (public host; 5 POSTs per hour per IP) creates the `School` with `auto_create_schema=False` on the instance, `provisioning_status="pending"`, then `provision_school.delay(school.pk)` and redirects to `signup_status` with a signed token (`django.core.signing`, salt `"signup-status"`).
  - `provision_school(school_id: int)` — **platform task** (plain `@app.task`, not `TenantTask`; queue `default`; runs from the public schema): `create_schema(check_if_exists=True)`, `seed_school`, `Domain(f"{subdomain}.{PLATFORM_DOMAIN}", is_primary=True)`, first user (contact person; roles `proprietor` + `school_admin`) via `invite_staff` with `base_url = PLATFORM_URL_TEMPLATE.format(subdomain=...)`, audit `school.provisioned` inside the school, status `ready`. On any exception: status `failed`, log, re-raise. Safe to run twice.
  - `signup_status` page: pending → meta refresh every 3 s; ready → portal link and "check your email"; failed → "we'll contact you".

- [ ] **Step 1: Write failing tests** (eager Celery; schema creation is real and rolled back with the test transaction — confirm and say so in the report):
  - valid signup → school `ready`, domain `newschool.localhost` resolves to the new schema, roles seeded there, invite email to the contact with a link on `http://newschool.localhost:8000`, the contact user has `proprietor` and `school_admin`.
  - *(Review Focus 4)* `Admin`, `-abc`, `abc-`, `a`, `public`, `www`, `9lives`, `pgadmin`, `a--b`, an existing subdomain → field error on `subdomain`, no new `School` row, no new schema (`information_schema.schemata`).
  - no level ticked, bad phone, terms unticked → field errors.
  - provisioning failure (patch `seed_school` to raise) → status `failed`, no domain.
  - `provision_school` run twice on a ready school → no duplicate domain/user/groups.
  - 6th signup POST in an hour from one IP → 429.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/tenancy/test_signup.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; then manually: `runserver`, sign up `demo1` at `http://localhost:8000/signup/`, open the invite link printed by the console email backend, set a password, set up MFA with an authenticator app, land on the home page. Paste what you saw into the report.
- [ ] **Step 5: Commit** `feat(tenancy): self-service school signup with background provisioning`.

### Task 12: Audit log page

**Files:**
- Create: `apps/audit/views.py`, `apps/audit/urls.py`, template `audit/log.html`
- Modify: `config/urls_tenant.py` (`settings/audit/`, name `audit:log`)
- Test: `tests/audit/test_log_page.py`

**Interfaces:**
- Produces: `audit:log` page (`require_roles(SCHOOL_ADMIN, PROPRIETOR)`), newest first, 50 per page, optional `?action=` prefix filter (e.g. `auth.`), columns: time (`DD/MM/YYYY HH:MM`, Africa/Lagos), who, action, object, IP.

- [ ] **Step 1: Write failing tests:** 120 events → page 1 has 50 newest; `?action=auth.` shows only `auth.*`; times render `11/10/2026 14:05`; teacher → 403; events from `school_b` never appear on `school_a`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/audit/test_log_page.py -W error` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; full CI command set green (`ruff check`, `ruff format --check`, `mypy apps config`, `makemigrations --check --dry-run`, `pytest -W error -m "not cloudinary"`).
- [ ] **Step 5: Commit** `feat(audit): school audit log page`.

---

## Exit criteria for P1 (roadmap)

- Self-service signup creates a usable school (schema, roles, domain, invited first admin) in under 2 minutes; the first admin can set a password, enrol MFA and log in.
- Staff can be invited, managed and audited; students log in with admission numbers and reset passwords by OTP to the guardian phone.
- Branding shows on the login page.
- The isolation guard is no longer vacuous: every `api/v1/` route and method has a cross-school case.

## Decisions left for later phases

- Levels and grading-scale seeders → registered by P2/P3 via `register_seeder`.
- Student records, guardians, keeping `recovery_phone` in sync → P2.
- Real SMS provider, SMS credits per plan → P5.
- First-term payment / trials at signup, subscription states → P8 (Q3 pricing).
- Custom domains (UC-02) → P8.
- Token/JWT API auth (and filling log `user_id`/`role` from it) → when the mobile app starts (Version 2).
- Tailwind/HTMX build pipeline → revisit at P2.
