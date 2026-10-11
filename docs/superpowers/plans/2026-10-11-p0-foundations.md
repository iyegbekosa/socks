# P0 Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An empty but deployable multi-tenant Django project in which a request, a cached value, a background task and a stored file can each only ever touch the school the request came from.

**Architecture:** One Django project (`config/`) with apps under `apps/`. `django-tenants` gives each school a PostgreSQL schema chosen from the request hostname; `apps.tenancy` holds `School`/`Domain` in the public schema; `apps.core` holds cross-cutting pieces (Cloudinary storage, tenant-aware Celery task base, money helpers, error format, request ID, logging). Everything runs in Docker Compose locally and in GitHub Actions.

**Tech Stack:** Python 3.12, Django 5.2 LTS, Django REST Framework, django-tenants, PostgreSQL 16, Redis 7, Celery 5, cloudinary (Python SDK), django-environ, python-json-logger, sentry-sdk, pytest + pytest-django + factory_boy, ruff, mypy + django-stubs.

**Spec:** `School Management Platform — System Design.pdf` plus the changes in `docs/ROADMAP.md` §1 (Cloudinary replaces S3; no parent accounts). This plan implements roadmap phase **P0**.

**Prerequisite (human):** Docker Desktop (or OrbStack) installed and running. The Mac currently has no Docker, Postgres or Redis. Every test command below runs inside the `web` container.

## Global Constraints

- Backend is Django; pin `Django>=5.2,<5.3` (current LTS).
- PostgreSQL 16+; multi-tenancy is schema-per-tenant via `django-tenants`.
- The school is resolved **only** from the request hostname; no endpoint accepts a school ID (§6.1, §7.2).
- Unknown hostnames show the platform landing page, never another school's data (FR-01).
- Celery tasks carry the schema name and run inside `schema_context()`; a task without one fails (§7.2).
- Every cache key is prefixed with the school's schema name (§5.6).
- File `public_id`s are prefixed `{CLOUDINARY_ENV_PREFIX}/{schema_name}/`; child-related files use Cloudinary `type="private"`; private download links expire after **600 seconds** (roadmap §1.1).
- Money is stored as integer kobo, never floats; displayed as `₦150,000.00` (§5.4, §6.1).
- Time zone `Africa/Lagos`; display dates `DD/MM/YYYY`; API dates ISO 8601 (§2.3, §6.1).
- Error body is `{"error": {"code", "message", "details", "request_id"}}` with the status codes in §6.5.
- Logs are JSON with request ID, school schema, user ID and role on every line; never passwords, OTPs, tokens or card data (§11.1).
- Celery queues: `payments` (highest), `notifications`, `default`, `reports` (§8.1).
- Settings come from environment variables via `django-environ` (§9.6).

## Review Focus

1. **Storage used outside a school** (e.g. during signup, in the public schema): must raise `NoTenantError`, not upload to a `public/` folder. Test in Task 4.
2. **A hostile `X-Request-ID` header** (newlines, 10 KB string): must be replaced by a generated ID, never echoed into logs or responses. Test in Task 3.
3. **Non-ASCII or spaced filenames** (`Adébáyọ̀ photo 1.JPG`): must become a safe, stable `public_id` and keep the right resource type. Test in Task 4.
4. **A tenant task that retries**: the retry must run in the same schema, not public. Test in Task 5.
5. **Hostname with port or capitals** (`Greenfield.localhost:8000`): must resolve to the same school as `greenfield.localhost`. Test in Task 2.

---

## File Structure

```
.gitignore  .dockerignore  .env.example  pyproject.toml  Dockerfile  docker-compose.yml  manage.py
.github/workflows/ci.yml
config/
  __init__.py          # exposes celery_app
  settings.py          # all settings, env-driven
  celery.py            # Celery app, queues
  urls_public.py       # landing page, healthz (public schema / unknown hosts)
  urls_tenant.py       # healthz, api/v1/ (per school)
  wsgi.py
apps/tenancy/
  models.py            # School(TenantMixin), Domain(DomainMixin)
  views.py             # landing page
  templates/tenancy/landing.html
  management/commands/seed_demo_schools.py
apps/core/
  money.py             # kobo helpers
  errors.py            # ApiError, DRF exception handler
  request_id.py        # RequestContextMiddleware, contextvars
  logging.py           # JSON formatter, context + redaction filters
  storage.py           # TenantCloudinaryStorage
  tasks.py             # TenantTask base, MissingTenantError
  views.py             # healthz
tests/
  conftest.py          # two schools created once per session; clients per school
  isolation/           # cross-school tests + route registry
  core/ tenancy/
```

---

### Task 1: Repository, dev stack and project skeleton

**Files:**
- Create: `.gitignore`, `.dockerignore`, `.env.example`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `manage.py`, `config/{__init__,settings,urls_public,urls_tenant,wsgi}.py`, `apps/__init__.py`, `apps/tenancy/{__init__,apps,models}.py` (models empty for now), `apps/core/{__init__,apps,views}.py`
- Test: `tests/core/test_healthz.py`

**Interfaces:**
- Produces: `GET /healthz` → `200 {"status": "ok", "db": "ok", "cache": "ok"}` or `503` with the failing part set to `"error"`; settings module `config.settings`; compose services `web`, `db`, `redis`, `worker`, `beat`, `mailpit`.

- [ ] **Step 1: `git init`**, `.gitignore` (Python, `.env`, `media/`, `.venv/`, `.claude/settings.local.json`), first commit containing `docs/` and the design PDF.
- [ ] **Step 2: `pyproject.toml`** with the stack above (floors: `djangorestframework>=3.15`, `django-tenants>=3.7`, `celery[redis]>=5.4`, `psycopg[binary]>=3.2`, `cloudinary>=1.41`, `django-environ>=0.11`, `python-json-logger>=3`, `sentry-sdk>=2`, `gunicorn>=23`); dev extras `pytest`, `pytest-django`, `factory-boy`, `ruff`, `mypy`, `django-stubs`. Ruff line length 100; pytest `DJANGO_SETTINGS_MODULE=config.settings`, `testpaths=["tests"]`.
- [ ] **Step 3: `Dockerfile`** on `python:3.12-slim` (non-root user, `pip install -e .[dev]`); **`docker-compose.yml`** with `db: postgres:16`, `redis: redis:7`, `mailpit: axllent/mailpit`, and `web` (`runserver 0.0.0.0:8000`), `worker` (`celery -A config worker -Q payments,notifications,default,reports`), `beat`; source mounted as a volume.
- [ ] **Step 4: `config/settings.py`** — env-driven (`DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CLOUDINARY_URL`, `CLOUDINARY_ENV_PREFIX` default `"dev"`, `SENTRY_DSN`); DB engine `django_tenants.postgresql_backend`; `DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]`; `TENANT_MODEL = "tenancy.School"`, `TENANT_DOMAIN_MODEL = "tenancy.Domain"`; `SHARED_APPS` = django_tenants, tenancy, core, contenttypes, auth, sessions, messages, staticfiles, rest_framework; `TENANT_APPS` = contenttypes, auth, sessions, messages; `TenantMainMiddleware` first; `PUBLIC_SCHEMA_URLCONF = "config.urls_public"`, `ROOT_URLCONF = "config.urls_tenant"`; `SHOW_PUBLIC_IF_NO_TENANT_FOUND = True`; `TIME_ZONE = "Africa/Lagos"`, `USE_TZ = True`, `DATE_INPUT_FORMATS`/`DATE_FORMAT` for `DD/MM/YYYY`; `CACHES` on Redis.
- [ ] **Step 5: Write the failing test** `tests/core/test_healthz.py::test_healthz_reports_db_and_cache_ok` — `client.get("/healthz")` on the public host → `status_code == 200`, `json() == {"status": "ok", "db": "ok", "cache": "ok"}`. Add `test_healthz_returns_503_when_cache_down` patching `django.core.cache.cache.set` to raise → `503`, `json()["cache"] == "error"`.
- [ ] **Step 6: Run** `docker compose run --rm web pytest tests/core/test_healthz.py -v` → FAIL (404).
- [ ] **Step 7: Implement** `apps.core.views.healthz(request) -> JsonResponse` (`SELECT 1` + cache set/get round-trip); route it in **both** URLConfs.
- [ ] **Step 8: Run** the same command → PASS. Also `docker compose up` and `curl localhost:8000/healthz` → `{"status": "ok", ...}`.
- [ ] **Step 9: Commit** `chore: project skeleton, docker dev stack, healthz`.

### Task 2: Schools, domains and hostname routing

**Files:**
- Modify: `apps/tenancy/models.py`, `config/urls_public.py`, `config/settings.py`
- Create: `apps/accounts/{__init__,apps,models}.py`, `apps/tenancy/views.py`, `apps/tenancy/templates/tenancy/landing.html`, `apps/tenancy/management/commands/seed_demo_schools.py`, `tests/conftest.py`
- Test: `tests/tenancy/test_routing.py`

**Interfaces:**
- Produces: `School(TenantMixin)` with `name: CharField(200)`, `created_at`, `is_active: bool = True`, `auto_create_schema = True`; `Domain(DomainMixin)`. Pytest fixtures (session-scoped, created once): `school_a` (schema `school_a`, domain `school-a.localhost`), `school_b` (schema `school_b`, domain `school-b.localhost`), public tenant on `localhost`; function fixtures `client_a`, `client_b` (`django_tenants.test.client.TenantClient`). Command `python manage.py seed_demo_schools` (idempotent) → public `localhost`, `greenfield.localhost`, `bluewater.localhost`.

- [ ] **Step 1: Write failing tests** in `tests/tenancy/test_routing.py`:
  - `test_school_host_selects_its_schema` — a `RequestFactory` request with `HTTP_HOST="school-a.localhost"` passed through the tenant middleware → `request.tenant.schema_name == "school_a"`.
  - `test_custom_user_model_is_active` — `get_user_model() is apps.accounts.models.User`, and a user created in `school_a` is not visible in `school_b`.
  - `test_unknown_host_gets_landing_page` — `HTTP_HOST="nosuch.localhost"`, `GET /` → 200, contains `"School Management Platform"`, `request.tenant.schema_name == "public"`.
  - `test_host_with_port_and_capitals_resolves` *(Review Focus 5)* — `HTTP_HOST="School-A.localhost:8000"` → `request.tenant.schema_name == "school_a"`.
  - `test_seed_demo_schools_is_idempotent` — call the command twice → exactly 3 `School` rows, 3 `Domain` rows.
- [ ] **Step 2: Run** `docker compose run --rm web pytest tests/tenancy -v` → FAIL.
- [ ] **Step 3: Implement** the models + migrations, a placeholder `apps/accounts/models.py::User(AbstractUser)` with `AUTH_USER_MODEL = "accounts.User"` and `accounts` in `TENANT_APPS` (P1 fills it in; it must exist before the first migration because swapping user models later breaks existing migrations), the landing view at `/` in `urls_public`, the seed command, and `tests/conftest.py` (override `django_db_setup` to create the public tenant and both schools once per session using `django_db_blocker.unblock()`). If django-tenants does not lowercase the host, add lowercasing in a `TenantMainMiddleware` subclass `apps.tenancy.middleware.HostTenantMiddleware` and use it in settings.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(tenancy): school and domain models with hostname routing`.

### Task 3: Request ID, error format, JSON logging and money helpers

**Files:**
- Create: `apps/core/request_id.py`, `apps/core/errors.py`, `apps/core/logging.py`, `apps/core/money.py`
- Modify: `config/settings.py` (middleware after `TenantMainMiddleware`, `REST_FRAMEWORK["EXCEPTION_HANDLER"]`, `LOGGING`, Sentry init)
- Test: `tests/core/test_request_id.py`, `tests/core/test_errors.py`, `tests/core/test_logging.py`, `tests/core/test_money.py`

**Interfaces:**
- Produces:
  - `RequestContextMiddleware` — sets contextvars `request_id`, `schema_name`, `user_id`, `role`; adds response header `X-Request-ID`. `get_request_id() -> str` (`"-"` outside a request).
  - `ApiError(Exception)(code: str, message: str, status: int = 400, details: list[dict] | None = None)`; `api_exception_handler(exc, context) -> Response` producing the §6.5 body for `ApiError` and DRF's own exceptions (`ValidationError` → 400 `"VALIDATION_ERROR"`, `NotAuthenticated` → 401 `"NOT_AUTHENTICATED"`, `PermissionDenied` → 403 `"FORBIDDEN"`, `NotFound` → 404 `"NOT_FOUND"`, `Throttled` → 429 `"RATE_LIMITED"`).
  - `JsonFormatter` (python-json-logger), `ContextFilter` (adds `request_id`, `schema`, `user_id`, `role` to every record), `RedactFilter` (replaces values of any `extra` key containing `password`, `otp`, `token`, `secret`, `card`, `pin`, `authorization` with `"[REDACTED]"`).
  - `kobo_from_naira(value: str | int | Decimal) -> int` (rejects `float` with `TypeError`, more than 2 decimal places with `ValueError`); `naira_display(kobo: int) -> str`; `money_fields(kobo: int) -> dict[str, int | str]` → `{"amount_kobo": ..., "amount_display": ...}`.

- [ ] **Step 1: Write failing tests:**
  - money: `naira_display(15_000_000) == "₦150,000.00"`, `naira_display(5) == "₦0.05"`, `naira_display(-250_00) == "-₦250.00"`; `kobo_from_naira("150000") == 15_000_000`; `kobo_from_naira("0.1") == 10`; `kobo_from_naira(0.1)` raises `TypeError`; `kobo_from_naira("1.005")` raises `ValueError`; `money_fields(15_000_000) == {"amount_kobo": 15_000_000, "amount_display": "₦150,000.00"}`.
  - request ID: a valid incoming `X-Request-ID: abc_123` is echoed; none → generated, matches `^req_[0-9a-f]{12}$`; *(Review Focus 2)* `"evil\nheader"` and a 10,000-char value are each replaced by a generated `req_…` ID.
  - errors: a test DRF view raising `ApiError("SCORE_EXCEEDS_MAX", "CA1 score cannot be more than 10", 400, [{"student": "GFS/2026/0042", "field": "ca1"}])` → body equals the §6.5 example with `request_id` equal to the response's `X-Request-ID`; `PermissionDenied` → 403 with `code == "FORBIDDEN"`.
  - logging: with `caplog`, a log line emitted during a request to `school-a.localhost` has `schema == "school_a"` and the request's `request_id`; `logger.info("login", extra={"password": "x", "otp": "123456"})` formats with both values `"[REDACTED]"`.
- [ ] **Step 2: Run** `docker compose run --rm web pytest tests/core -v` → FAIL.
- [ ] **Step 3: Implement** the four modules with the signatures above. Request ID accepted only if it matches `^[A-Za-z0-9_-]{1,64}$`. Sentry is initialised only when `SENTRY_DSN` is set, with `send_default_pii=False` and a `before_send` that tags `schema`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(core): request id, error format, json logging, kobo money helpers`.

### Task 4: Tenant-scoped Cloudinary storage

**Files:**
- Create: `apps/core/storage.py`
- Modify: `config/settings.py` (`STORAGES`, `PRIVATE_URL_TTL_SECONDS = 600`)
- Test: `tests/core/test_storage.py`

**Interfaces:**
- Produces:
  - `NoTenantError(RuntimeError)`
  - `TenantCloudinaryStorage(django.core.files.storage.Storage)(delivery_type: Literal["upload", "private"] = "private")`
  - `STORAGES = {"default": {"BACKEND": "apps.core.storage.TenantCloudinaryStorage", "OPTIONS": {"delivery_type": "private"}}, "public": {... "delivery_type": "upload"}, "staticfiles": {...whitenoise or default...}}` — later phases use `storages["public"]` for school logos and the default (private) for everything about a child.
  - `resource_type_for(name: str) -> Literal["image", "raw"]` — `image` for `.jpg .jpeg .png .webp .gif`, else `raw`.
  - `public_id_for(name: str) -> str` — `f"{CLOUDINARY_ENV_PREFIX}/{schema}/{path}"`; for `image` the extension is dropped (Cloudinary stores format separately), for `raw` it is kept.
- Behaviour that matters: the **stored name is relative** (`report_cards/2026-1/gfs-2026-0042.pdf`); the schema prefix is added at every access from the *current* connection. So a name copied from school B and used in school A resolves inside A's folder and can never reach B's file.

- [ ] **Step 1: Write failing tests** (patch `cloudinary.uploader.upload`, `cloudinary.uploader.destroy`, `cloudinary.api.resource`; no network):
  - `test_save_prefixes_env_and_schema` — in `schema_context("school_a")`, `default_storage.save("report_cards/t1/a.pdf", ContentFile(b"%PDF"))` → `upload` called with `public_id="dev/school_a/report_cards/t1/a.pdf"`, `resource_type="raw"`, `type="private"`, `overwrite=True`; returned name is `"report_cards/t1/a.pdf"`.
  - `test_image_drops_extension_and_uses_image_type` — `"photos/x.jpg"` → `public_id="dev/school_a/photos/x"`, `resource_type="image"`.
  - `test_private_url_is_signed_and_expires` — patch `time.time` to `1_000_000`; `default_storage.url("report_cards/t1/a.pdf")` equals `cloudinary.utils.private_download_url("dev/school_a/report_cards/t1/a", "pdf", resource_type="raw", type="private", expires_at=1_000_600)`.
  - `test_public_url_is_plain_https` — `storages["public"].url("branding/logo.png")` starts with `https://res.cloudinary.com/` and contains `/image/upload/` and `dev/school_a/branding/logo`.
  - `test_same_name_in_other_school_points_to_other_folder` — `url()` for one name under `school_a` vs `school_b` contain `school_a/` and `school_b/` respectively.
  - `test_public_schema_refuses_storage` *(Review Focus 1)* — in `schema_context("public")`, `save`, `url`, `delete`, `exists` each raise `NoTenantError`.
  - `test_unicode_filename_is_slugified` *(Review Focus 3)* — `"photos/Adébáyọ̀ photo 1.JPG"` → `public_id == "dev/school_a/photos/adebayo-photo-1"`, `resource_type == "image"`.
  - `test_exists_false_on_not_found` — `cloudinary.api.resource` raising `cloudinary.exceptions.NotFound` → `exists()` is `False`.
- [ ] **Step 2: Run** `docker compose run --rm web pytest tests/core/test_storage.py -v` → FAIL.
- [ ] **Step 3: Implement** `TenantCloudinaryStorage` with `_save`, `_open` (download via the signed URL), `url`, `delete`, `exists`, `size`, and `get_available_name` returning the name unchanged (callers own uniqueness; overwrite is wanted for regenerated PDFs). Slugify each path segment with `django.utils.text.slugify` after `unicodedata` NFKD ASCII folding; lowercase the extension.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Live check (optional, needs `CLOUDINARY_URL` in `.env`)** — `tests/core/test_storage_live.py` marked `@pytest.mark.cloudinary`, skipped when the env var is absent: upload a 1-page PDF under `school_a`, fetch the signed URL → 200 with `%PDF`; fetch the same URL with `expires_at` in the past → not 200; delete. *Requires "Allow delivery of PDF and ZIP files" enabled in the Cloudinary console.*
- [ ] **Step 6: Commit** `feat(core): tenant-scoped cloudinary storage with signed private urls`.

### Task 5: Tenant-aware Celery tasks and schema-prefixed cache

**Files:**
- Create: `config/celery.py`, `apps/core/tasks.py`
- Modify: `config/__init__.py`, `config/settings.py` (`CELERY_*`, `CACHES["default"]["KEY_FUNCTION"] = "django_tenants.cache.make_key"`)
- Test: `tests/core/test_tasks.py`, `tests/isolation/test_cache_isolation.py`

**Interfaces:**
- Produces:
  - `config.celery.app`; queues `payments`, `notifications`, `default`, `reports`; `task_default_queue = "default"`; `task_acks_late = True`; `worker_prefetch_multiplier = 1`.
  - `MissingTenantError(RuntimeError)`
  - `TenantTask(celery.Task)` — `apply_async` injects header `schema_name` from `connection.schema_name` when the caller is in a school schema; refuses (raises `MissingTenantError`) when enqueued from the public schema without an explicit `schema_name=` header. On run, reads the header and executes inside `schema_context(schema_name)`; a message without the header raises `MissingTenantError` and is not retried. Later phases declare tasks as `@app.task(base=TenantTask, queue="reports")`.

- [ ] **Step 1: Write failing tests** (`CELERY_TASK_ALWAYS_EAGER = True` in a test settings fixture):
  - `test_task_runs_in_enqueuing_school` — a test task returning `connection.schema_name`, enqueued with `.delay()` inside `schema_context("school_b")` → result `"school_b"`.
  - `test_task_without_schema_fails` — `.apply_async()` from the public schema → raises `MissingTenantError`.
  - `test_explicit_schema_from_public` — `.apply_async(headers={"schema_name": "school_a"})` from public → `"school_a"` (for Celery Beat jobs that fan out per school).
  - `test_retry_keeps_schema` *(Review Focus 4)* — a task that calls `self.retry(countdown=0)` on its first run and returns `connection.schema_name` on the second, enqueued in `school_a` → `"school_a"`.
  - `test_same_cache_key_is_separate_per_school` — `cache.set("branding", "A")` in `school_a`, `cache.set("branding", "B")` in `school_b`; `cache.get("branding")` returns `"A"` in A and `"B"` in B.
- [ ] **Step 2: Run** `docker compose run --rm web pytest tests/core/test_tasks.py tests/isolation/test_cache_isolation.py -v` → FAIL.
- [ ] **Step 3: Implement** `config/celery.py`, `TenantTask`, and the cache key function setting. Retries must carry the header forward (override `retry` or `signature_from_request` if eager mode drops headers).
- [ ] **Step 4: Run** → PASS. Then with the stack up: `docker compose exec web python manage.py shell -c "..."` enqueueing the test task in `greenfield` shows `greenfield` in the `worker` logs.
- [ ] **Step 5: Commit** `feat(core): tenant-aware celery tasks and schema-prefixed cache keys`.

### Task 6: Isolation route registry

**Files:**
- Create: `tests/isolation/registry.py`, `tests/isolation/test_routes.py`
- Test: same

**Interfaces:**
- Produces: `IsolationCase(name: str, method: str, url: Callable[[obj], str], make_obj: Callable[[], Any], body: dict | None = None)`; `ISOLATION_CASES: list[IsolationCase]`; `EXEMPT_ROUTES: set[str]` (route names, starting with `{"healthz"}`). Every later phase appends a case for each tenant API route it adds, in that phase's own task.

- [ ] **Step 1: Write the tests:**
  - `test_cross_school_access_is_refused[case]` — parametrized over `ISOLATION_CASES`: create the object in `school_b` via `make_obj`, then request `case.url(obj)` with `client_a` (school A host) using `case.method` → status in `{401, 403, 404}`, and the object is unchanged in `school_b`. With an empty registry this collects zero cases; that is expected in P0.
  - `test_every_tenant_api_route_is_registered` — walk `config.urls_tenant` resolver; every named route under `api/v1/` must appear in `ISOLATION_CASES` names or `EXEMPT_ROUTES`; failure message lists the missing route names.
- [ ] **Step 2: Run** `docker compose run --rm web pytest tests/isolation -v` → `test_every_tenant_api_route_is_registered` PASS; add a temporary unregistered dummy route to confirm it FAILS with that route's name, then remove it.
- [ ] **Step 3: Commit** `test(isolation): cross-school route registry and guard`.

### Task 7: Continuous integration

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: a `ci` workflow on pull requests and pushes to `main`, with services `postgres:16` and `redis:7`, running on Python 3.12.

- [ ] **Step 1: Write the workflow** with steps: install `.[dev]`; `ruff check .`; `ruff format --check .`; `mypy apps config`; `python manage.py makemigrations --check --dry-run`; `pytest -m "not cloudinary"`.
- [ ] **Step 2: Run the same commands locally** inside `web` → all pass, no output from `makemigrations --check`.
- [ ] **Step 3: Commit** `ci: lint, types, migration check and tests on github actions`. (The workflow first runs once a GitHub remote exists; adding the remote is the human's call.)

---

## Exit criteria for P0 (roadmap)

- `docker compose up` then `python manage.py seed_demo_schools`; `http://greenfield.localhost:8000/healthz` and `http://bluewater.localhost:8000/healthz` return ok; `http://nosuch.localhost:8000/` shows the landing page.
- A file saved while in `greenfield` resolves only inside `dev/greenfield/` (Task 4 tests).
- `docker compose run --rm web pytest` is green; CI workflow passes the same commands.

## Deferred on purpose (owned by later phases)

- `StoredFile` byte tracking and `PlanLimits` → P1.
- User fields (phone, admission-number login), roles, auth endpoints, MFA → P1. P0 only creates the empty custom `User` so P1 can extend it with normal migrations.
- Queue routing for real tasks → each phase declares `queue=` on its tasks.
- Coverage threshold (75% on results and fees) → enforced in CI from P3/P4.
