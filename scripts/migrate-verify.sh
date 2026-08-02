#!/usr/bin/env bash
#
# Is this host actually serving the business? Run after a migration,
# and any time something feels wrong.
#
#   ./scripts/migrate-verify.sh
#
# Every check answers a failure that has actually happened to this
# system, which is why each one prints what it means rather than just
# a tick.

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$*"; }

DOMAIN="$(grep -E '^TUNNEL_HOSTNAME=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"
DOMAIN="${DOMAIN:-erp.captainsresearch.co.in}"

head "Containers"
for service in postgres redis api nginx worker-whatsapp worker-ocr worker-scheduled beat; do
  state="$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$service" '$1==s{print $2}')"
  [ "$state" = "running" ] && ok "$service" || bad "$service is '${state:-missing}'"
done

head "Database"
# Not "does it connect" but "does it hold the business": an empty
# schema connects perfectly well. One simple query per table -- a
# multi-line one had to survive three layers of shell quoting and
# silently returned nothing, reporting a healthy database as empty.
count_of() {
  docker compose exec -T postgres sh -lc \
    "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc 'SELECT count(*) FROM $1'" \
    2>/dev/null | tr -d '\r\n '
}
users="$(count_of users)"
purchases="$(count_of purchase_headers)"
products="$(count_of products)"
[ "${users:-0}" -gt 0 ] && ok "${users} user(s)" || bad "no users -- the import did not land"
[ "${products:-0}" -gt 0 ] && ok "${products} product(s)" || bad "no products"
printf '    %s purchase(s)\n' "${purchases:-0}"

# The app's own credentials, not psql's socket auth -- these differ,
# and only the app's matter for serving.
if docker compose exec -T api python -c "
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def m():
    e = create_async_engine(os.environ['DATABASE_URL'])
    async with e.connect() as c: await c.execute(text('select 1'))
    await e.dispose()
asyncio.run(m())" >/dev/null 2>&1; then
  ok "the api authenticates to postgres"
else
  bad "the api cannot authenticate to postgres -- POSTGRES_PASSWORD in .env
    does not match the role in the data directory. Fix with:
      printf \"ALTER USER \$USER WITH PASSWORD '<the .env value>';\" | docker compose exec -T postgres psql -U \$USER -d \$DB -f -"
fi

head "Attachments"
files="$(docker compose exec -T api sh -lc 'find /data/attachments -type f 2>/dev/null | wc -l' | tr -d '\r ')"
rows="$(docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM attachments"' 2>/dev/null | tr -d '\r ')"
if [ "${rows:-0}" -eq 0 ] || [ "${files:-0}" -gt 0 ]; then
  ok "${files:-0} file(s) on disk for ${rows:-0} attachment row(s)"
else
  bad "${rows} attachment rows but no files -- every bill has lost the
    photo it was read from. Restore the attachments volume."
fi

head "Public entrance"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://${DOMAIN}/healthz" 2>/dev/null)"
[ "$code" = "200" ] && ok "https://${DOMAIN}/healthz" || bad "https://${DOMAIN}/healthz returned '${code:-no answer}'"

# One tunnel, not two. Both hosts sharing credentials.json answer, and
# webhooks land wherever Cloudflare felt like sending them. Probed from
# the api container: the cloudflared image is distroless and has no
# shell to exec into.
edges="$(docker compose exec -T api sh -lc \
  'curl -s --max-time 5 http://cloudflared:2000/ready' 2>/dev/null | tr -d '\r')"
case "$edges" in
  *'"readyConnections":0'*)
    bad "the tunnel is up but connected to nothing -- the hostname will not answer" ;;
  *readyConnections*)
    ok "tunnel connected: $(printf '%s' "$edges" | sed 's/.*readyConnections":\([0-9]*\).*/\1/') edge(s)" ;;
  *)
    printf '    tunnel status unavailable (is cloudflared in this stack?)\n' ;;
esac

head "WhatsApp"
# The failure that cost hours: containers holding a stale app secret
# reject every inbound message with 401 and nothing says why.
host_secret="$(grep -E '^WHATSAPP_APP_SECRET=' .env | cut -d= -f2- | tr -d '\r\n')"
cont_secret="$(docker compose exec -T api sh -lc 'printf "%s" "$WHATSAPP_APP_SECRET"' 2>/dev/null)"
if [ -n "$host_secret" ] && [ "$host_secret" = "$cont_secret" ]; then
  ok "the api is running .env's app secret"
else
  bad "the api's WHATSAPP_APP_SECRET differs from .env -- every inbound
    message will 401. Fix with: docker compose up -d --force-recreate"
fi

recent_401="$(docker compose logs api --since 10m 2>/dev/null | grep -c webhook_signature_invalid)"
[ "${recent_401:-0}" -eq 0 ] && ok "no rejected webhooks in the last 10 min" \
  || bad "${recent_401} webhook(s) rejected in the last 10 min"

undelivered="$(docker compose logs api --since 30m 2>/dev/null | grep -c whatsapp_delivery_failed)"
[ "${undelivered:-0}" -eq 0 ] && ok "no failed deliveries in the last 30 min" \
  || printf '    %s message(s) failed to deliver in 30 min -- check the recipient allowed list\n' "$undelivered"

head "Scheduled work"
sched_logs="$(docker compose logs worker-scheduled beat --since 2h 2>/dev/null)"
# The minute-by-minute sweep proves beat survived the move. The daily
# check-in fires once, so an absence here means "not yet today", not
# "broken" -- reported without failing the run.
printf '%s' "$sched_logs" | grep -q partner_notice_sweep \
  && ok "partner_notice_sweep is firing" \
  || bad "partner_notice_sweep has not run in 2h -- beat or the scheduled worker is down"
printf '%s' "$sched_logs" | grep -q daily_checkin \
  && ok "daily_checkin has run" \
  || printf '    daily_checkin not seen yet (it fires once, on the hour)\n'

printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
