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
  # The trailing `/.` copies the *contents*. `cp -R src dst` nests src
  # inside dst whenever dst already exists -- and it always does here,
  # because config.example.yml is tracked and arrives with the clone.
  # That silently produced docker/cloudflared/cloudflared/config.yml and
  # a tunnel that restart-looped on "no such file or directory".
  mkdir -p docker/cloudflared
  # A previous run of this script left credentials.json owned by uid
  # 65532 and unwritable by us, so the copy below fails outright on a
  # second import of the same host. Take ownership back first.
  sudo -n chown -R "$(id -u):$(id -g)" docker/cloudflared 2>/dev/null || true
  cp -R "$STAGE/secrets/cloudflared/." docker/cloudflared/
  # A macOS-built archive carries AppleDouble siblings (._config.yml).
  # Noise on Linux, and enough to keep a stray directory un-rmdir-able.
  find docker/cloudflared -name '._*' -delete 2>/dev/null || true
  # cloudflared's image runs as uid 65532, so a 0600 file owned by the
  # invoking user is unreadable to it. On a laptop the VM's uid
  # remapping hides this; on a real Linux host the tunnel dies with
  # "couldn't read tunnel credentials: permission denied".
  #
  # Set the mode *before* handing the file over. The other order cannot
  # work: the chown succeeds via sudo, and the chmod then runs as a user
  # who no longer owns the file, fails EPERM, and takes the whole import
  # down under `set -e` -- after .env has already been installed.
  chmod 600 docker/cloudflared/credentials.json 2>/dev/null || true
  if chown 65532:65532 docker/cloudflared/credentials.json 2>/dev/null ||
    sudo -n chown 65532:65532 docker/cloudflared/credentials.json 2>/dev/null; then
    :
  else
    chmod 644 docker/cloudflared/credentials.json
    say "  note: could not chown credentials.json to uid 65532 -- left world-readable so the tunnel can start"
  fi
fi
if [ -d "$STAGE/secrets/certs" ]; then
  mkdir -p docker/certs
  cp -R "$STAGE/secrets/certs/." docker/certs/
  find docker/certs -name '._*' -delete 2>/dev/null || true
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
# redis_data included: it carries the demo-mode flags, so the partners
# stay on whichever books they were on rather than being silently
# returned to the real ones. Redis must be down while its volume is
# replaced, or it will overwrite the restored files on shutdown.
say "Stopping redis to replace its data"
docker compose stop redis >/dev/null 2>&1 || true

# letsencrypt is restored here rather than with the other secrets: it is
# certbot's own state (account key, renewal config, the archive the live
# symlinks point at), and it has to land as a volume because that is
# where renew-cert.sh mounts it from. Without it TLS still serves -- the
# certificate is a plain file in docker/certs -- but it can never be
# renewed, and that is invisible until the day it expires.
for volume in attachments reports backups redis_data letsencrypt; do
  archive="$STAGE/volumes/${volume}.tar.gz"
  [ -f "$archive" ] || continue
  say "Restoring volume: ${volume}"
  docker volume create "textile-erp_${volume}" >/dev/null
  docker run --rm \
    -v "textile-erp_${volume}:/to" \
    -v "$(dirname "$(realpath "$archive")"):/from:ro" \
    alpine:3.20 tar xzf "/from/$(basename "$archive")" -C /to
done
docker compose up -d redis >/dev/null

# Host-side data/: the manual pg_dumps taken before each destructive
# change. Merged rather than replaced, so a package restored twice does
# not discard anything the new host has already written.
if [ -f "$STAGE/volumes/host-data.tar.gz" ]; then
  say "Restoring host data/"
  mkdir -p data
  tar xzf "$STAGE/volumes/host-data.tar.gz" -C data
fi

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

  1. Build the images now, not during the outage. On 2 vCPU this takes
     a few minutes, and there is no reason for it to happen while the
     business is down.

       docker compose build

  2. Stop the OLD host's whole stack -- not just its front door.
     beat reaches Meta outbound and needs no inbound route at all, so
     an old stack left running keeps firing check-ins and partner
     notices at real phones from books that stopped being true at the
     cutover. `stop` preserves every volume, so this costs nothing in
     rollback terms.

       # on the old host
       docker compose stop

  3. Move the Elastic IP to this host, then bring it up and check it:

       docker compose up -d
       ./scripts/migrate-verify.sh

     The address is what the DNS record and Meta's webhook URL both
     point at, so nothing else needs reconfiguring -- but until it
     moves, this host answers on its own public IP and not on the
     hostname the certificate is issued for.

DONE
