#!/usr/bin/env bash
# Where is the permanent webhook setup up to? -- docs/16_Deployment.md §11
#
# Every step of moving off quick tunnels either succeeded or didn't, and
# the failure mode that matters is the silent one: a tunnel that runs but
# is connected to nothing, or a hostname that resolves but reaches the
# wrong place. This checks each link end to end and says which one is
# broken, so nobody has to infer it from a WhatsApp message not arriving.
#
# Safe to run at any point, including before any of it is set up.
#   ./docker/tunnel-check.sh

set -uo pipefail
cd "$(dirname "$0")/.."

HOSTNAME_FQDN="${TUNNEL_HOSTNAME:-erp.example.com}"
DOMAIN="${HOSTNAME_FQDN#*.}"
CREDENTIALS="docker/cloudflared/credentials.json"

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAILED=$((FAILED + 1)); }
info() { printf '    %s\n' "$1"; }
FAILED=0

echo
echo "Permanent webhook — $HOSTNAME_FQDN"
echo

# 1. the zone has to be on Cloudflare before a tunnel hostname can exist
echo "1. DNS delegation"
NS="$(dig +short NS "$DOMAIN" | sort | tr '\n' ' ')"
if [ -z "$NS" ]; then
    fail "$DOMAIN has no nameservers — is the domain registered and active?"
elif printf '%s' "$NS" | grep -q "ns.cloudflare.com"; then
    pass "$DOMAIN is on Cloudflare ($NS)"
else
    fail "$DOMAIN still uses $NS"
    info "Add the site at dash.cloudflare.com, then set those nameservers at the registrar."
fi

# 2. the tunnel's own identity
echo "2. Tunnel credentials"
if [ -f "$CREDENTIALS" ]; then
    pass "$CREDENTIALS present"
else
    fail "$CREDENTIALS missing — run: cloudflared tunnel create textile-erp"
    info "then copy ~/.cloudflared/<UUID>.json to $CREDENTIALS"
fi

TUNNEL_ID="$(grep -E '^CLOUDFLARE_TUNNEL_ID=' .env 2>/dev/null | cut -d= -f2-)"
if [ -n "$TUNNEL_ID" ]; then
    pass "CLOUDFLARE_TUNNEL_ID set in .env"
else
    fail "CLOUDFLARE_TUNNEL_ID is empty in .env"
fi

if grep -qE '^COMPOSE_PROFILES=.*tunnel' .env 2>/dev/null; then
    pass "COMPOSE_PROFILES includes 'tunnel' — the service starts with the stack"
else
    fail "COMPOSE_PROFILES=tunnel not in .env — 'docker compose up -d' will skip cloudflared"
fi

# 3. the hostname must resolve, and resolve to Cloudflare's edge
echo "3. Hostname"
ADDRS="$(dig +short "$HOSTNAME_FQDN" A | tr '\n' ' ')"
if [ -z "$ADDRS" ]; then
    fail "$HOSTNAME_FQDN does not resolve"
    info "run: cloudflared tunnel route dns textile-erp $HOSTNAME_FQDN"
else
    pass "$HOSTNAME_FQDN resolves ($ADDRS)"
fi

# 4. running, and actually connected -- these are different things
echo "4. Tunnel process"
STATUS="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    textile-erp-cloudflared-1 2>/dev/null)"
case "$STATUS" in
    "running healthy") pass "cloudflared container running, edge connections established" ;;
    "running "*)       fail "cloudflared running but not ready — no edge connections yet" ;;
    running*)          fail "cloudflared running; health unknown: $STATUS" ;;
    "")                fail "cloudflared container not created — docker compose up -d cloudflared" ;;
    *)                 fail "cloudflared container is $STATUS" ;;
esac

# 5. the whole chain, exercised the way Meta exercises it
echo "5. End to end"
VERIFY="$(grep -E '^WHATSAPP_VERIFY_TOKEN=' .env 2>/dev/null | cut -d= -f2-)"
if [ -z "$VERIFY" ]; then
    fail "WHATSAPP_VERIFY_TOKEN not set in .env — can't test the handshake"
else
    URL="https://$HOSTNAME_FQDN/webhooks/whatsapp"
    BODY="$(curl -fsS --max-time 15 \
        --get "$URL" \
        --data-urlencode "hub.mode=subscribe" \
        --data-urlencode "hub.verify_token=$VERIFY" \
        --data-urlencode "hub.challenge=tunnel-check" 2>&1)"
    if [ "$BODY" = "tunnel-check" ]; then
        pass "$URL completed Meta's verification handshake"
    else
        fail "$URL did not answer the handshake"
        info "got: ${BODY:-<empty>}"
    fi

    # a wrong token must be refused; a webhook that accepts anything is
    # worse than one that is down
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
        --get "$URL" \
        --data-urlencode "hub.mode=subscribe" \
        --data-urlencode "hub.verify_token=wrong-on-purpose" \
        --data-urlencode "hub.challenge=x" 2>/dev/null)"
    if [ "$CODE" = "403" ]; then
        pass "a wrong verify token is refused (403)"
    else
        fail "a wrong verify token returned $CODE, expected 403"
    fi
fi

echo
if [ "$FAILED" -eq 0 ]; then
    echo "All good. Point Meta's callback URL at:"
    echo "  https://$HOSTNAME_FQDN/webhooks/whatsapp"
    echo "It never needs changing again."
else
    echo "$FAILED check(s) failed — see docs/16_Deployment.md §11 for the step each one belongs to."
fi
echo
exit "$FAILED"
