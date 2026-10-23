#!/usr/bin/env bash
# Local setup without containers: Homebrew PostgreSQL + uv.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }
command -v psql >/dev/null || { echo "PostgreSQL 16 is required: brew install postgresql@16 && brew services start postgresql@16"; exit 1; }
pg_isready -q || { echo "PostgreSQL is not accepting connections (brew services start postgresql@16)"; exit 1; }

for db in rootsignal_dev rootsignal_test; do
  if psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
    echo "database $db exists"
  else
    createdb "$db" && echo "created database $db"
  fi
done

[ -f .env ] || { cp .env.example .env && echo "created .env from .env.example"; }

uv sync
(cd server && uv run alembic upgrade head)
echo "setup complete"
