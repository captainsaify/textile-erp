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
if [ "$code" = "200" ]; then
  ok "https://${DOMAIN}/healthz"
else
  # Cloudflare's edge judges the caller, and once this stack lives on a
  # VPS the caller is a datacenter IP -- which bot/ASN rules can 403
  # while serving every real browser normally. That is a fact about
  # where this script is running, not about the site, so ask the origin
  # directly before calling it a failure. Reported either way: a silent
  # pass here would hide a genuinely dead entrance.
  origin="$(docker compose exec -T api sh -lc \
    'curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:8000/healthz' \
    2>/dev/null | tr -d '\r')"
  if [ "$origin" = "200" ]; then
    printf "    https://%s/healthz returned '%s' from this host, but the origin\n" \
      "$DOMAIN" "${code:-no answer}"
    printf "    answers 200. Cloudflare is filtering this machine's own IP --\n"
    printf "    confirm from a browser or a phone, not from the server.\n"
  else
    bad "https://${DOMAIN}/healthz returned '${code:-no answer}' and the origin
    itself answered '${origin:-nothing}' -- the entrance is genuinely down."
  fi
fi

head "TLS"
# This stack terminates TLS itself. While the Cloudflare tunnel fronted
# the site, Cloudflare presented the public certificate and the origin's
# could be -- and was -- a self-signed placeholder. Now an expired or
# wrong certificate is the whole site being down, so it is checked here.
served="$(echo | openssl s_client -connect localhost:443 -servername "$DOMAIN" 2>/dev/null \
  | openssl x509 -noout -subject 2>/dev/null)"
if printf '%s' "$served" | grep -q "CN *= *${DOMAIN}"; then
  ok "443 serves a certificate for ${DOMAIN}"
else
  bad "443 is not serving a certificate for ${DOMAIN} (got: ${served:-no answer})"
fi

CERT="docker/certs/fullchain.pem"
if [ -f "$CERT" ]; then
  enddate="$(openssl x509 -in "$CERT" -noout -enddate 2>/dev/null | cut -d= -f2)"
  # 30 days is the window Let's Encrypt will actually renew in, so a
  # certificate inside it that has not been replaced means renewal is
  # already failing -- not that it is merely getting close.
  if openssl x509 -in "$CERT" -noout -checkend 2592000 >/dev/null 2>&1; then
    ok "certificate valid well past 30 days (expires ${enddate})"
  else
    bad "certificate expires ${enddate}, inside the renewal window and still
    not replaced -- renew-cert.sh is failing. Check ~/renew-cert.log."
  fi
fi

# The check that exists because of this migration. secrets/certs carries
# the certificate, so TLS serves and everything above passes -- but
# certbot's account key and renewal config live in a *volume*, and a
# host that did not receive it cannot renew anything. The failure is
# invisible for ~90 days and then total.
PROJECT="$(basename "$PWD")"
if docker volume inspect "${PROJECT}_letsencrypt" >/dev/null 2>&1; then
  state="$(docker run --rm -v "${PROJECT}_letsencrypt:/le:ro" alpine:3.20 sh -c \
    'printf "%s %s" "$(ls /le/renewal/*.conf 2>/dev/null | wc -l)" \
                    "$(find /le/accounts -name private_key.json 2>/dev/null | wc -l)"' \
    2>/dev/null | tr -d '\r')"
  confs="${state%% *}"; accts="${state##* }"
  if [ "${confs:-0}" -gt 0 ] && [ "${accts:-0}" -gt 0 ]; then
    ok "certbot can renew (renewal config and ACME account both present)"
  else
    bad "the letsencrypt volume has ${confs:-0} renewal config(s) and ${accts:-0} account
    key(s) -- certbot has nothing to renew from. TLS works today and
    stops working when the current certificate expires. Re-issue with:
      ./scripts/renew-cert.sh   (or certbot certonly --webroot, once)"
  fi
else
  bad "no ${PROJECT}_letsencrypt volume -- the certificate cannot be renewed.
    It was not carried over by the migration; restore it or re-issue."
fi

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
# Herestrings, not pipes: `grep -q` exits on its first match, and under
# `pipefail` the writer's SIGPIPE (141) becomes the pipeline's status.
# Piped, this reported a perfectly healthy scheduler as down -- and did
# so *because* it found what it was looking for.
if grep -q partner_notice_sweep <<<"$sched_logs"; then
  ok "partner_notice_sweep is firing"
else
  bad "partner_notice_sweep has not run in 2h -- beat or the scheduled worker is down"
fi
if grep -q daily_checkin <<<"$sched_logs"; then
  ok "daily_checkin has run"
else
  printf '    daily_checkin not seen yet (it fires once, on the hour)\n'
fi

printf '\n\033[1m%s passed, %s failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
