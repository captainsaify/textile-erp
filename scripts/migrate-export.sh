#!/usr/bin/env bash
#
# Package everything the VPS needs that git does not carry.
# See docs/30_VpsMigration.md. Run from the repo root on the OLD host,
# with the stack up.
#
#   ./scripts/migrate-export.sh [output-dir]
#
# What goes in, and what deliberately does not, is the whole point of
# this script -- see the manifest it writes.

set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="${1:-data/migration}"
STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="${OUT_DIR}/textile-erp-${STAMP}"
ARCHIVE="${STAGE}.tar.gz"

compose() { docker compose "$@"; }

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -f .env ] || die ".env not found -- run this from the repo root on the old host."
compose ps --status running --format '{{.Service}}' | grep -qx postgres \
  || die "postgres is not running; start the stack first (docker compose up -d)."

mkdir -p "$STAGE"/{db,volumes,secrets}

# --- 1. the database ------------------------------------------------
# pg_dump, never a copy of pg_data: the volume's on-disk format is tied
# to the CPU architecture and the exact server build, and this host is
# arm64 while most VPSes are x86_64. A dump restores anywhere.
say "Dumping the database"
compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
  > "$STAGE/db/textile_erp.dump"
DB_BYTES=$(wc -c < "$STAGE/db/textile_erp.dump" | tr -d ' ')
[ "$DB_BYTES" -gt 1000 ] || die "the dump is suspiciously small (${DB_BYTES} bytes)."

# A plain-text schema alongside it, for reading and for diffing after
# the restore without needing pg_restore.
compose exec -T postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema-only --no-owner' \
  > "$STAGE/db/schema.sql"

# Row counts, so the import can prove it landed everything rather than
# assert it. Exact COUNT(*), not pg_stat_user_tables.n_live_tup: that
# column is a planner estimate that reads zero until ANALYZE has run,
# so a restore could "match" simply by both sides being unmeasured.
say "Recording row counts"
count_rows() {
  # Every layer of quoting between here and psql was a chance to mangle
  # the SQL, so the query is built on the host and piped in on stdin.
  compose exec -T postgres sh -lc \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "
       SELECT tablename FROM pg_tables WHERE schemaname = '"'"'public'"'"' ORDER BY tablename"' \
    | tr -d '\r' | awk 'NF' \
    | awk '{printf "%sSELECT %c%s%c, count(*) FROM %s", (NR>1 ? " UNION ALL " : ""), 39, $0, 39, $0}' \
    | compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F, -f -' \
    | tr -d '\r' | sort
}
count_rows > "$STAGE/db/row-counts.csv"
POPULATED=$(awk -F, '$2>0' "$STAGE/db/row-counts.csv" | wc -l | tr -d ' ')
[ "$POPULATED" -gt 0 ] || die "every table counted zero -- refusing to package an empty database."
say "  ${POPULATED} populated table(s)"

# --- 2. redis ---------------------------------------------------------
# Copied, not skipped. It is a cache in the sense that it rebuilds --
# but `wa:demo:<number>` is what puts a phone on the demo books, and
# `wa:msg:<id>` is what stops a redelivered webhook being processed
# twice. Dropping it would land the partners back on the real business
# mid-demonstration, which is the exact accident demo mode exists to
# prevent. SAVE first so the snapshot on disk is current.
say "Snapshotting redis"
REDIS_PASS="$(grep -E '^REDIS_PASSWORD=' .env | cut -d= -f2- | tr -d '\r\n')"
compose exec -T redis sh -lc \
  "redis-cli --no-auth-warning -a '${REDIS_PASS}' SAVE" >/dev/null 2>&1 \
  || say "  (SAVE refused; the append-only log is still captured)"

# --- 3. the volumes that hold real files ----------------------------
for volume in attachments reports backups redis_data; do
  say "Archiving volume: ${volume}"
  docker run --rm \
    -v "textile-erp_${volume}:/from:ro" \
    -v "$(pwd)/${STAGE}/volumes:/to" \
    alpine:3.20 tar czf "/to/${volume}.tar.gz" -C /from . 2>/dev/null
done

# Host-side data/ predates the volumes and still holds the manual
# pg_dumps taken before each destructive change. Losing those would
# lose the only copies of the books as they stood before a purge.
say "Archiving host data/ (excluding migration packages)"
tar czf "$STAGE/volumes/host-data.tar.gz" \
  --exclude='./migration' --exclude='./migration/*' \
  -C data . 2>/dev/null || true

# --- 4. secrets and host-specific config ----------------------------
say "Collecting secrets"
cp .env "$STAGE/secrets/.env"
if [ -d docker/cloudflared ]; then
  # the tunnel's identity: the public hostname follows these files, so
  # no DNS change is needed -- and two hosts must never run them at once
  cp -R docker/cloudflared "$STAGE/secrets/cloudflared"
fi
if [ -d docker/certs ] && [ -n "$(ls -A docker/certs 2>/dev/null)" ]; then
  cp -R docker/certs "$STAGE/secrets/certs"
fi

# --- 5. the manifest ------------------------------------------------
say "Writing the manifest"
GIT_SHA="$(git rev-parse HEAD)"
GIT_DIRTY="$(git status --porcelain | wc -l | tr -d ' ')"
PG_VERSION="$(compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "show server_version"' | tr -d '\r\n ')"

cat > "$STAGE/MANIFEST.txt" <<MANIFEST
textile-erp migration package
taken:        $(date -u +"%Y-%m-%dT%H:%M:%SZ") UTC
from:         $(hostname) ($(uname -m))
git commit:   ${GIT_SHA}
uncommitted:  ${GIT_DIRTY} file(s)
postgres:     ${PG_VERSION}

INCLUDED
  db/textile_erp.dump    pg_dump --format=custom, restores on any arch
  db/schema.sql          readable schema, for diffing after restore
  db/row-counts.csv      what the import must reproduce
  volumes/attachments    original sheet photos; purchase_headers rows
                         reference them, so losing these breaks the
                         link from a bill back to what was photographed
  volumes/reports        generated exports; regenerable, but cheap
  volumes/backups        the nightly backup history
  volumes/redis_data     sessions, and the demo-mode flag that decides
                         which set of books a phone writes to. Also the
                         webhook dedup keys, so a redelivery after the
                         move is not processed a second time.
  volumes/host-data      data/ on the old host: the manual pg_dumps
                         taken before each destructive change, which
                         are the only copies of the books as they stood
                         beforehand
  secrets/.env           every credential the stack reads
  secrets/cloudflared    the named tunnel's identity -- the public
                         hostname follows this, so DNS needs no change
  secrets/certs          TLS material, if any was kept locally

DELIBERATELY EXCLUDED
  celery_state           scheduler bookkeeping; rebuilt on first tick,
                         and carrying it over would only replay a
                         schedule the new host recomputes anyway.
  data/migration         previous packages -- excluded to stop this
                         archive containing a copy of itself.
  pg_data                the raw volume. Architecture- and build-
                         specific; the dump above is the portable form.
  whatsapp-bridge/session
                         a 228MB Chromium profile tied to this machine
                         and this CPU. Re-scan the QR on the VPS if the
                         bridge is ever needed -- and it is not needed
                         while WHATSAPP_TRANSPORT=meta and group
                         broadcasting is off.
  the code               it is in git. The VPS clones it.
MANIFEST

# --- 6. seal it -----------------------------------------------------
say "Sealing the archive"
tar czf "$ARCHIVE" -C "$OUT_DIR" "textile-erp-${STAMP}"
rm -rf "$STAGE"
chmod 600 "$ARCHIVE"

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
SHA="$(shasum -a 256 "$ARCHIVE" | cut -d' ' -f1)"

cat <<DONE

$(say "Done")
  archive : ${ARCHIVE}  (${SIZE})
  sha256  : ${SHA}

This file contains every credential the business has. Move it over scp
to a host you control, never through chat or cloud storage, and delete
it from both ends once the migration is verified.

  scp ${ARCHIVE} user@vps:/tmp/
  # then on the VPS, from the cloned repo:
  ./scripts/migrate-import.sh /tmp/$(basename "$ARCHIVE")

DONE
