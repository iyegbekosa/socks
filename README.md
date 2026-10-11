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
- **`CLIENT_IP_HEADER` is required in production** behind Cloudflare or any proxy (e.g.
  `HTTP_CF_CONNECTING_IP`). Without it every visitor shares the proxy's IP, and the per-IP
  login limit (`LOGIN_IP_FAILURE_LIMIT`, 50 failures / 15 min) would hit the whole platform.
- **Cloudinary console:** turn on *Settings → Security → "Allow delivery of PDF and ZIP
  files"*. Report cards and receipts are PDFs, and new accounts block them.
- **Environment:** set `CLOUDINARY_URL`, `CLOUDINARY_ENV_PREFIX` (e.g. `prod`, which keeps
  environments apart in one Cloudinary account) and `SENTRY_DSN`, along with `SECRET_KEY`,
  `DEBUG=false`, `ALLOWED_HOSTS`, `DATABASE_URL` and `REDIS_URL`.
