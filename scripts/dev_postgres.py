"""Start a local PostgreSQL 16 server for development without Docker.

Uses the embedded server from the `pgserver` package; data lives in `.pgdata/`.
Prints DATABASE_URL and PGHOST (the socket directory) for `.env`. The server keeps running after this script exits.

    .venv/bin/python scripts/dev_postgres.py
"""

from pathlib import Path

import pgserver

DATA_DIR = Path(__file__).resolve().parent.parent / ".pgdata"
DB_NAME = "build2learn"

server = pgserver.get_server(DATA_DIR, cleanup_mode=None)
exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}';")
if "1 row" not in exists:
    server.psql(f"CREATE DATABASE {DB_NAME};")
socket_dir = server.get_uri().split("host=", 1)[1]
print(f"DATABASE_URL=postgres://postgres@/{DB_NAME}")
print(f"PGHOST={socket_dir}")
