#!/usr/bin/env bash
# Start a project-local Postgres (data in ./.pgdata, gitignored) and apply the schema.
# Usage: scripts/db_up.sh        (idempotent)
set -euo pipefail
cd "$(dirname "$0")/.."
# Use a full Postgres server install (Homebrew libpq alone ships initdb without the server).
for d in "${PG_BIN:-}" /opt/homebrew/opt/postgresql@16/bin /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@16/bin; do
  if [ -n "$d" ] && [ -x "$d/postgres" ]; then export PATH="$d:$PATH"; break; fi
done
export LC_ALL="${LC_ALL:-en_US.UTF-8}"   # macOS: postmaster dies without a valid locale
PGDATA=./.pgdata
PORT="${PGPORT:-5432}"
DB=pizza
if [ ! -d "$PGDATA" ]; then
  initdb -D "$PGDATA" -U "$USER" --auth=trust >/dev/null
fi
if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" -l .pgdata.log -o "-p $PORT -k /tmp" start >/dev/null
  sleep 1
fi
createdb -h /tmp -p "$PORT" "$DB" 2>/dev/null || true
psql -h /tmp -p "$PORT" -d "$DB" -q -v ON_ERROR_STOP=1 -f app/db/schema.sql
echo "Postgres up: postgresql://localhost:$PORT/$DB"
