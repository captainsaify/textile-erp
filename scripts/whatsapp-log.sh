#!/usr/bin/env bash
#
# What has a given phone number done with the bot, and what did we manage
# to send back.
#
#   ./scripts/whatsapp-log.sh 9977250571
#   ./scripts/whatsapp-log.sh +919354082168 --since 3h
#   ./scripts/whatsapp-log.sh 7000087329 --follow
#   ./scripts/whatsapp-log.sh                      # everyone, recent
#
# **There is no message transcript, and this script cannot invent one.**
# Nothing stores what a person typed or what the bot replied. Three
# separate things survive, with three different lifetimes:
#
#   whatsapp_command log lines   the command name only -- until logs rotate
#   whatsapp_sessions.context    wizard answers in progress -- swept ~30
#                                minutes after the last reply
#   audit_logs                   completed mutations only -- forever
#
# So an abandoned conversation leaves a command, then nothing. Firoz
# started a purchase from his second number on 2026-08-06, spent six
# minutes in the wizard, walked away, and half an hour later the only
# remaining evidence was one log line.
set -euo pipefail

cd "$(dirname "$0")/.."

SINCE="1440m"
FOLLOW=""
RAW=""
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    --follow|-f) FOLLOW=1; shift ;;
    -h|--help) sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) RAW="$1"; shift ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if [ -n "$FOLLOW" ]; then
  say "Tailing WhatsApp traffic. Ctrl-C to stop."
  # --line-buffered or grep sits on a 4KB block and the output arrives in
  # bursts minutes after the thing you are watching for happened.
  exec docker compose logs -f api | grep --line-buffered -i whatsapp
fi

# ------------------------------------------------------------------
# Normalise: accept 9977250571, +919977250571, 91 9977 250571.
# ------------------------------------------------------------------
if [ -n "$RAW" ]; then
  MSISDN="$(printf '%s' "$RAW" | tr -cd '0-9')"
  [ "${#MSISDN}" -eq 10 ] && MSISDN="91${MSISDN}"

  # Inbound messages carry the sender base64'd *inside* the wamid, so the
  # plain digits never appear on those lines. Outbound failures use the
  # plain number in "to". Match either.
  #
  # Two greps, not one: in the JSON the "event" field comes after
  # "message_id", so `grep 'whatsapp_command.*<b64>'` silently matches
  # nothing while looking like a working query.
  B64="$(printf '%s' "$MSISDN" | base64 | tr -d '\n')"
  say "Number $MSISDN  (wamid tag: $B64)  window: last $SINCE"
else
  MSISDN=""; B64=""
  say "Everyone, last $SINCE"
fi

filter() {
  if [ -z "$MSISDN" ]; then cat
  else grep -E "${B64}|${MSISDN}" || true
  fi
}

echo
say "Commands received"
COMMANDS="$(docker compose logs api --since "$SINCE" 2>&1 |
  grep whatsapp_command 2>/dev/null | filter |
  sed -E 's/.*"command": "([^"]*)".*"timestamp": "([^"]*)".*/  \2  \1/' |
  sort | tail -20 || true)"
if [ -n "$COMMANDS" ]; then printf '%s\n' "$COMMANDS"
else echo "  none in this window"; fi

echo
say "Deliveries that failed"
FAILURES="$(docker compose logs api --since "$SINCE" 2>&1 |
  grep whatsapp_delivery_failed 2>/dev/null | filter |
  sed -E 's/.*"to": "([^"]*)".*"error_code": ([0-9]*).*"timestamp": "([^"]*)".*/  \3  \1  \2/' |
  sort | tail -20 || true)"
if [ -n "$FAILURES" ]; then
  printf '%s\n' "$FAILURES"
  cat <<'EOF'
  131047 = the 24h window is shut; that person must message us first,
           or it has to go as an approved template
  131030 = not on the test number's recipient allowlist (cap: 5)
EOF
else
  echo "  none -- everything we sent arrived"
fi

echo
say "Conversations open right now"
docker compose exec -T postgres sh -lc 'psql -U $POSTGRES_USER -d $POSTGRES_DB -c "
  SELECT u.whatsapp_number, u.full_name, s.state, s.expires_at
    FROM whatsapp_sessions s JOIN users u ON u.id = s.user_id
   ORDER BY s.updated_at DESC;"' 2>&1 | sed 's/^/  /'

echo
say "What was actually completed (audited, so it is real)"
WHERE=""
[ -n "$MSISDN" ] && WHERE="WHERE u.whatsapp_number = '+${MSISDN}'"
docker compose exec -T postgres sh -lc "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c \"
  SELECT a.created_at, a.action, u.full_name
    FROM audit_logs a JOIN users u ON u.id = a.actor_user_id
    ${WHERE}
   ORDER BY a.created_at DESC LIMIT 15;\"" 2>&1 | sed 's/^/  /'
