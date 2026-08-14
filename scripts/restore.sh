#!/usr/bin/env bash
#
# Go back to a backup, and come back up on it.
#
# Restoring cannot be done from the web app, and the reason is not
# caution — it is arithmetic. `pg_restore --clean` replaces every table,
# so it needs an ACCESS EXCLUSIVE lock on each one. While the API and the
# workers are connected it cannot have them, and a *queued* exclusive
# lock in Postgres makes every later reader queue behind it. The restore
# does not fail; it hangs, with the whole site stuck behind it. That
# happened once, on 14 Aug 2026, from a button that has since been taken
# out of Master Control.
#
# So the application stops first. That is the entire trick, and this
# script exists so it is one command rather than six remembered ones.
#
#   scripts/restore.sh                      # list what you can go back to
#   scripts/restore.sh backup-2026....dump  # go back to that one
#
# Run it on the server, from ~/textile-erp.

set -euo pipefail

cd "$(dirname "$0")/.."

# Everything that talks to the database. Postgres and nginx stay up:
# nginx keeps serving the "be right back" it already serves when the API
# is down, which is a better answer than a dead connection.
APP_SERVICES=(api worker-whatsapp worker-ocr worker-scheduled beat)

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

compose() { docker compose "$@"; }

if [[ $# -eq 0 ]]; then
  say "Backups you can go back to"
  compose exec -T api python -m backend.admin backups
  echo
  note "scripts/restore.sh <name>   — to use one"
  exit 0
fi

TARGET="$1"

say "About to restore: $TARGET"
note "This replaces the WHOLE database — both businesses, everything"
note "entered since that backup was taken."
note ""
note "A backup of the current state is taken first, so this is a step"
note "sideways rather than a step off a cliff."
echo
read -r -p "  Type the backup name to go ahead: " TYPED
[[ "$TYPED" == "$TARGET" ]] || die "got '$TYPED', expected '$TARGET' — nothing was changed."

# Taken while the app is still up, deliberately: this is the last moment
# the current state exists, and a restore that cannot itself be undone is
# not a safe operation no matter how many times it asks.
say "1/5  Backing up the current state first"
compose exec -T api python -m backend.admin backup

say "2/5  Stopping the application"
compose stop "${APP_SERVICES[@]}"

# Stopping a container does not always close its connections instantly,
# and one leftover session is enough to make the restore hang -- which is
# the exact failure this script exists to prevent. So it is checked, not
# assumed.
say "3/5  Waiting for connections to drain"
for attempt in $(seq 1 30); do
  LEFT=$(compose exec -T postgres psql -qtAX -U "${POSTGRES_USER:-sarfaraz}" \
    -d "${POSTGRES_DB:-textile_erp_dev}" \
    -c "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();" \
    2>/dev/null || echo "?")
  if [[ "$LEFT" == "0" ]]; then
    note "clear"
    break
  fi
  note "$LEFT still connected (attempt $attempt/30)…"
  sleep 2
done
[[ "${LEFT:-?}" == "0" ]] || die "connections are still open after a minute. Nothing was changed.
Find them with:
  docker compose exec postgres psql -U ${POSTGRES_USER:-sarfaraz} -d ${POSTGRES_DB:-textile_erp_dev} \\
    -c \"select pid, application_name, state from pg_stat_activity where datname=current_database();\""

# A one-off container: the `api` service is stopped, and this needs to
# run somewhere that has pg_restore, the backup volume and the settings.
# It holds exactly one connection and never opens a transaction, so it
# does not block itself.
say "4/5  Restoring"
compose run --rm --no-deps api python -m backend.admin --yes restore "$TARGET" \
  || die "the restore failed. The application is still stopped — nothing has been started
on a half-restored database. Read the error above before doing anything else."

say "5/5  Starting up, and checking the books"
compose up -d "${APP_SERVICES[@]}"
sleep 8
compose exec -T api python -m backend.admin check

say "Done — running on $TARGET"
note "The state from just before this is in the backup taken at step 1,"
note "so this is reversible too: scripts/restore.sh <that name>."
