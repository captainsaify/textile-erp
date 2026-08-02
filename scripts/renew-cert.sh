#!/usr/bin/env bash
#
# Renew the TLS certificate and hand it to nginx. Run from cron twice a
# day; Let's Encrypt only acts inside the last 30 days, so the other
# ~120 runs do nothing and cost nothing.
#
# Only needed because the stack serves 443 itself. While the Cloudflare
# tunnel fronted the site, Cloudflare terminated TLS and the origin's
# certificate could be — and was — a self-signed placeholder.
#
# Two things here are not obvious and are the reason this is a script
# rather than a cron one-liner:
#
#   * certbot writes into its own volume; nginx reads ./docker/certs.
#     A renewal nobody copies out is a certificate that expires anyway.
#   * nginx is *restarted*, not reloaded. A SIGHUP reload was observed
#     to keep serving the previous certificate after the files had been
#     replaced underneath it -- the container could see the new bytes
#     and still presented the old chain until the process came back.
#
# The restart only happens when the certificate actually changed, so a
# no-op run never interrupts anyone.
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN="${CERT_DOMAIN:-erp.captainsresearch.co.in}"
# Named after the compose project, which is the directory name.
PROJECT="$(basename "$PWD")"
CERT="docker/certs/fullchain.pem"

before="$(sha256sum "$CERT" 2>/dev/null | cut -d' ' -f1 || echo none)"

docker run --rm \
  -v "${PROJECT}_certbot_www:/var/www/certbot" \
  -v "${PROJECT}_letsencrypt:/etc/letsencrypt" \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet

docker run --rm \
  -v "${PROJECT}_letsencrypt:/le" \
  -v "$PWD/docker/certs:/out" \
  alpine:3.20 sh -c "cp -L /le/live/$DOMAIN/fullchain.pem /out/fullchain.pem &&
                     cp -L /le/live/$DOMAIN/privkey.pem  /out/privkey.pem"

# The copy above runs as root inside the container, so the files come
# back owned by root on the host.
sudo chown "$(id -u):$(id -g)" docker/certs/fullchain.pem docker/certs/privkey.pem
chmod 644 docker/certs/fullchain.pem
chmod 600 docker/certs/privkey.pem

after="$(sha256sum "$CERT" | cut -d' ' -f1)"
if [ "$before" != "$after" ]; then
  echo "certificate changed -- restarting nginx"
  docker compose restart nginx
  openssl x509 -in "$CERT" -noout -subject -enddate
else
  echo "certificate unchanged -- nginx left alone"
fi
