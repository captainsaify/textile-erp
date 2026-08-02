#!/usr/bin/env bash
#
# Restore a migration package onto a fresh host.
# See docs/30_VpsMigration.md. Run from the repo root on the NEW host.
#
#   ./scripts/migrate-import.sh /path/to/textile-erp-<stamp>.tar.gz
#
# Refuses to run against a database that already holds business data:
# importing twice would not merge, it would fail halfway and leave a
# half-populated schema that looks fine until someone reads a total.

set -euo pipefail

cd "$(dirname "$0")/.."

ARCHIVE="${1:-}"
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -n "$ARCHIVE" ] || die "usage: $0 /path/to/textile-erp-<stamp>.tar.gz"
[ -f "$ARCHIVE" ] || die "no such file: $ARCHIVE"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say "Unpacking"
tar xzf "$ARCHIVE" -C "$WORK"
STAGE="$(find "$WORK" -maxdepth 1 -type d -name 'textile-erp-*' | head -1)"
[ -d "$STAGE" ] || die "the archive does not look like a migration package."
cat "$STAGE/MANIFEST.txt"

# --- 1. secrets first: the stack cannot start without them ----------
say "Installing secrets"
[ -f .env ] && cp .env ".env.before-import.$(date +%s)"
cp "$STAGE/secrets/.env" .env
chmod 600 .env
if [ -d "$STAGE/secrets/cloudflared" ]; then
  mkdir -p docker
  cp -R "$STAGE/secrets/cloudflared" docker/cloudflared
  chmod 600 docker/cloudflared/credentials.json 2>/dev/null || true
fi
if [ -d "$STAGE/secrets/certs" ]; then
  mkdir -p docker
  cp -R "$STAGE/secrets/certs" docker/certs
fi

# --- 2. datastores up, app down -------------------------------------
# The app must not run yet: a worker connecting mid-restore would read
# a half-populated schema and could write against it.
say "Starting postgres and redis only"
docker compose up -d postgres redis
until docker compose exec -T postgres sh -lc 'pg_isready -U "$POSTGRES_USER"' >/dev/null 2>&1; do
  sleep 2
done

EXISTING="$(docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
     SELECT count(*) FROM information_schema.tables WHERE table_schema = '"'"'public'"'"'"' \
  2>/dev/null | tr -d '\r\n ' || echo 0)"
if [ "${EXISTING:-0}" -gt 0 ]; then
  ROWS="$(docker compose exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM users" ' \
    2>/dev/null | tr -d '\r\n ' || echo 0)"
  [ "${ROWS:-0}" -eq 0 ] || die \
    "this database already holds ${ROWS} user(s). Importing would not merge -- it would
  fail partway and leave a half-populated schema. Drop it deliberately first:
    docker compose down -v      # destroys the new host's data, not the old host's"
fi

# --- 3. the database ------------------------------------------------
say "Restoring the database"
docker compose exec -T postgres sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges' \
  < "$STAGE/db/textile_erp.dump" 2>&1 | grep -vE "does not exist, skipping" || true

# --- 4. the file volumes --------------------------------------------
for volume in attachments reports backups; do
  archive="$STAGE/volumes/${volume}.tar.gz"
  [ -f "$archive" ] || continue
  say "Restoring volume: ${volume}"
  docker volume create "textile-erp_${volume}" >/dev/null
  docker run --rm \
    -v "textile-erp_${volume}:/to" \
    -v "$(dirname "$(realpath "$archive")"):/from:ro" \
    alpine:3.20 tar xzf "/from/$(basename "$archive")" -C /to
done

# --- 5. prove it ----------------------------------------------------
say "Verifying against the source row counts"
docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
     SELECT tablename FROM pg_tables WHERE schemaname = '"'"'public'"'"' ORDER BY tablename"' \
  | tr -d '\r' | awk 'NF' \
  | awk '{printf "%sSELECT %c%s%c, count(*) FROM %s", (NR>1 ? " UNION ALL " : ""), 39, $0, 39, $0}' \
  | docker compose exec -T postgres sh -lc \
      'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F, -f -' \
  | tr -d '\r' | sort > "$WORK/row-counts-after.csv"

python3 - "$STAGE/db/row-counts.csv" "$WORK/row-counts-after.csv" <<'PY'
import sys, pathlib

def load(path):
    out = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if "," in line:
            name, _, count = line.rpartition(",")
            out[name] = int(count or 0)
    return out

before, after = load(sys.argv[1]), load(sys.argv[2])
# Exact counts on both sides, so anything short is a real loss. Missing
# entirely is worse than short and is reported as such.
problems = []
for table, count in sorted(before.items()):
    if not count:
        continue
    if table not in after:
        problems.append(f"  {table}: {count} before, table absent after")
    elif after[table] < count:
        problems.append(f"  {table}: {count} before, {after[table]} after")
if problems:
    print("\033[31mROW COUNTS DO NOT MATCH:\033[0m")
    print("\n".join(problems))
    raise SystemExit(1)
print(f"row counts match across {sum(1 for v in before.values() if v)} populated table(s)")
PY

cat <<'DONE'

==> Imported.

Nothing is serving yet, on purpose. Before starting the app:

  1. Make sure the OLD host's tunnel is stopped. Two cloudflared
     instances sharing one credentials.json will both answer, and
     roughly half of Meta's webhooks will hit the host you are trying
     to retire.

       # on the old host
       docker compose stop cloudflared

  2. Then bring this one up and check it:

       docker compose up -d
       ./scripts/migrate-verify.sh

DONE
