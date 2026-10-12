# build2learn

Multi-tenant school management SaaS (Django + DRF, one PostgreSQL schema per school, chosen
from the hostname). Plan and design changes: [docs/ROADMAP.md](docs/ROADMAP.md). Full design:
[School Management Platform — System Design.pdf](<School Management Platform — System Design.pdf>).

## Native quick-start (no Docker)

Needs [uv](https://docs.astral.sh/uv/). No Redis is needed locally: the cache falls back to
LocMem and the Celery broker to `memory://`.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python scripts/dev_postgres.py
# prints two lines, DATABASE_URL=... and PGHOST=...; copy them into .env
```

`.env`:

```sh
SECRET_KEY=change-me
DEBUG=true
ALLOWED_HOSTS=localhost,.localhost
DATABASE_URL=postgres://postgres@/build2learn
PGHOST=/path/printed/by/dev_postgres.py
# Optional. Format: cloudinary://<api_key>:<api_secret>@<cloud_name>
# Empty means file storage calls fail; everything else works.
CLOUDINARY_URL=
```

Then:

```sh
.venv/bin/python manage.py migrate_schemas --shared
.venv/bin/python manage.py ensure_public_tenant
.venv/bin/python manage.py seed_demo_schools   # also calls ensure_public_tenant; adds greenfield + bluewater
.venv/bin/python manage.py runserver
```

Visit <http://greenfield.localhost:8000/healthz>. It should return
`{"status": "ok", "db": "ok", "cache": "ok"}`. <http://localhost:8000/> shows the platform
landing page.

`dev_postgres.py` leaves the server running. Run it again after a reboot.

## Tests and checks

The commands CI runs (`.github/workflows/ci.yml`):

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy apps config
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/pytest -W error -m "not cloudinary"
```

`pytest -m cloudinary` runs the live Cloudinary round-trip. It needs `CLOUDINARY_URL`.

## Docker quick-start

Not tested on the maintainer's machine.

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec web python manage.py migrate_schemas --shared
docker compose exec web python manage.py ensure_public_tenant
docker compose exec web python manage.py seed_demo_schools
```

Then visit <http://greenfield.localhost:8000/healthz>. Compose starts PostgreSQL 16, Redis,
Mailpit (<http://localhost:8025>), the web server, a Celery worker and Celery Beat.

## Deploy prerequisites

- **The public School row must exist** (schema `public`, with the platform's domain).
  Run `python manage.py ensure_public_tenant` (idempotent; uses `PLATFORM_DOMAIN`, default
  `localhost`). Without it, unknown hosts cannot fall back to the landing page.
- **`CLIENT_IP_HEADER` is required in production** behind Cloudflare or any proxy, and it
  must name a header **the proxy overwrites** on every request, e.g. `HTTP_CF_CONNECTING_IP`
  behind Cloudflare. Never point it at `HTTP_X_FORWARDED_FOR` as-is: clients can send their
  own `X-Forwarded-For`, and a proxy that appends to it leaves the client's forged values in
  front. For a comma-separated value the app takes the **rightmost** valid IP (the one the
  nearest proxy added), never the first. Without the setting every visitor shares the
  proxy's IP, and the per-IP login limit (`LOGIN_IP_FAILURE_LIMIT`, 50 failures / 15 min)
  would hit the whole platform.
- **`SECURE_PROXY_SSL=true`** when TLS ends at a proxy that sets `X-Forwarded-Proto`.
  Without it, Django sees plain http and every link the app builds from the request is
  `http://` instead of `https://`: staff invite links in emails and SMS (including invites
  sent by staff imports), password-reset links, and the portal URL printed on student
  password sheets.
- **Cloudinary console:** turn on *Settings → Security → "Allow delivery of PDF and ZIP
  files"*. Report cards and receipts are PDFs, and new accounts block them.
- **Environment:** set `CLOUDINARY_URL`, `CLOUDINARY_ENV_PREFIX` (e.g. `prod`, which keeps
  environments apart in one Cloudinary account) and `SENTRY_DSN`, along with `SECRET_KEY`,
  `DEBUG=false`, `ALLOWED_HOSTS`, `DATABASE_URL` and `REDIS_URL`.
- **`REDIS_URL` is mandatory when `DEBUG=false`.** Settings refuse to load without it
  (`ImproperlyConfigured`), because Celery would otherwise run every task inline in the web
  request.
- **Platform URLs:** `PLATFORM_DOMAIN` is the bare domain schools live under (e.g.
  `build2learn.ng`, so a school is `greenfield.build2learn.ng`); `ensure_public_tenant` and
  signup use it. `PLATFORM_URL_TEMPLATE` builds the links in invite emails and on the signup
  status page, e.g. `PLATFORM_URL_TEMPLATE=https://{subdomain}.build2learn.ng`.
  `python manage.py check --deploy` fails (`core.E001`) if it contains `localhost` or does
  not start with `https://`.
- **Email (invites, signup, password reset) is required:** `EMAIL_HOST`, `EMAIL_PORT`
  (default 587), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` (default true)
  and `DEFAULT_FROM_EMAIL`. With `DEBUG=false` the SMTP backend is used. Do not deploy
  without working SMTP: an emailed reset link is how staff recover their accounts and how
  **parents recover student accounts** (the link goes to the main guardian's email).
- **Password reset works without an SMS provider.** "Forgot password" sends a reset link by
  email. `SMS_BACKEND` is unset in production until the real provider lands in P5; until
  then reset codes by SMS are unavailable (the reset page says so) and invites go by email
  only. SMS codes are added as a second channel once a provider is configured. Do not set
  it to the console backend in production.
- **Celery worker queues:** the worker must consume all four queues, highest priority first:
  `celery -A config worker -Q payments,notifications,default,reports` (as in
  `docker-compose.yml`). Tasks are routed to these queues (SMS goes to `notifications`); a
  worker pool that misses one leaves those tasks queued forever.
- **Imports need a worker on `default`.** Student and staff import jobs run as Celery tasks
  on the `default` queue; without a worker consuming it, uploads stay "Queued" forever.
- **Fail stale imports from cron:** run `python manage.py fail_stale_imports` periodically
  (e.g. every 15 minutes; `--older-than-minutes`, default 60). For every school it marks
  import jobs still queued or running after that long as failed with a generic message,
  deletes their uploaded files (which hold children's data) and audits the counts. It never
  resumes a job; the school uploads the file again.
- **Seed academics for schools created before P2:** run
  `python manage.py seed_academics_defaults` once after deploying P2 (or
  `--schema <name>` for one school). It adds the default class levels and subjects for each
  school's levels offered. Levels are added only where a school has none, and subjects
  likewise, so it never undoes a school's own changes and is safe to re-run.
- **`SIGNUP_GLOBAL_DAILY_LIMIT`** (default 50): platform-wide cap on valid self-service
  signups per 24 hours. When it is hit, signup shows a "try again later" page and logs a
  WARNING (`Global signup cap reached`); raise it if real demand needs more.
