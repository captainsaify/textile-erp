# 16 — Deployment

## 1. Target topology

> **How this is actually deployed today:** on a Mac behind a home
> connection, reached through a Cloudflare Tunnel rather than an open
> port 443 — see [§11](#tunnel). The topology below is otherwise
> accurate; only the edge differs.

Single production host (a modest VPS is sufficient at current scale —
see [01_Architecture.md §11](01_Architecture.md#11-performance--scalability-considerations)),
everything orchestrated via Docker Compose:

```mermaid
flowchart TB
    subgraph Host[Single production host]
        Nginx[nginx: TLS termination,\nreverse proxy]
        API1[api: FastAPI\n(gunicorn+uvicorn workers)]
        WorkerWA[worker-whatsapp: Celery]
        WorkerOCR[worker-ocr: Celery]
        WorkerSched[worker-scheduled: Celery]
        Beat[beat: Celery Beat]
        PG[(postgres:16)]
        Redis[(redis:7)]
        Vol1[(pg_data volume)]
        Vol2[(attachments volume)]
        Vol3[(backups volume, separate disk/mount)]
    end
    Internet -- 443 --> Nginx
    Nginx --> API1
    API1 --> PG
    API1 --> Redis
    WorkerWA --> Redis
    WorkerOCR --> Redis
    WorkerSched --> Redis
    WorkerWA --> PG
    WorkerOCR --> PG
    WorkerSched --> PG
    Beat --> Redis
    PG --> Vol1
    API1 --> Vol2
    WorkerOCR --> Vol2
    WorkerSched -- nightly_backup --> Vol3
```

## 2. `docker-compose.yml`

```yaml
version: "3.9"

services:
  nginx:
    image: nginx:1.27-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./docker/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./docker/certs:/etc/nginx/certs:ro
      - certbot_www:/var/www/certbot
    depends_on:
      - api

  api:
    build:
      context: .
      dockerfile: docker/Dockerfile.api
    restart: unless-stopped
    env_file: .env
    volumes:
      - attachments:/data/attachments
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/healthz"]
      interval: 15s
      timeout: 5s
      retries: 3

  worker-whatsapp:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    restart: unless-stopped
    env_file: .env
    command: celery -A backend.workers.app worker -Q whatsapp -c 8 --loglevel=info
    volumes:
      - attachments:/data/attachments
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker-ocr:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    restart: unless-stopped
    env_file: .env
    command: celery -A backend.workers.app worker -Q ocr -c 2 --loglevel=info
    volumes:
      - attachments:/data/attachments
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 2g

  worker-scheduled:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    restart: unless-stopped
    env_file: .env
    command: celery -A backend.workers.app worker -Q scheduled,backup,reports -c 4 --loglevel=info
    volumes:
      - attachments:/data/attachments
      - backups:/data/backups
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  beat:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    restart: unless-stopped
    env_file: .env
    command: celery -A backend.workers.app beat --loglevel=info
    depends_on:
      redis:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    env_file: .env
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
    # Not exposed on a host port in production -- reachable only within
    # the compose network. Exposed via docker-compose.override.local.yml
    # in local dev for direct psql access.

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

  frontend:
    build:
      context: ./frontend
      dockerfile: ../docker/Dockerfile.frontend
    restart: unless-stopped
    # Served as static assets through nginx in production; this
    # service exists for the build step / local dev server.

volumes:
  pg_data:
  redis_data:
  attachments:
  backups:
  certbot_www:
```

## 3. Dockerfiles (illustrative shape — actual files live in `docker/`)

```dockerfile
# docker/Dockerfile.api
FROM python:3.12-slim@sha256:REPLACE_WITH_PINNED_DIGEST AS base
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
COPY alembic ./alembic
COPY alembic.ini .
USER app
EXPOSE 8000
HEALTHCHECK CMD curl -f http://localhost:8000/healthz || exit 1
CMD ["gunicorn", "backend.api.main:app", "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "30"]
```

```dockerfile
# docker/Dockerfile.worker
FROM python:3.12-slim@sha256:REPLACE_WITH_PINNED_DIGEST AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr libgl1 libglib2.0-0 poppler-utils \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd -r app && useradd -r -g app app
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
USER app
```

Both images pin the base image by digest (not `python:3.12-slim`
floating), run as non-root (`USER app`), and install only what each
role needs — the API image never carries PaddleOCR/Tesseract's system
dependencies, keeping it smaller and reducing its attack surface,
since OCR only ever runs in worker containers.

## 4. Nginx configuration (essentials)

```nginx
# docker/nginx.conf (excerpt)
limit_req_zone $binary_remote_addr zone=webhook:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=api:10m rate=20r/s;

server {
    listen 443 ssl http2;
    server_name erp.example-domain.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 20m;  # matches settings.max_attachment_size_mb headroom

    location /webhooks/whatsapp {
        limit_req zone=webhook burst=20 nodelay;
        proxy_pass http://api:8000;
        proxy_read_timeout 10s;
    }

    location /api/ {
        limit_req zone=api burst=40 nodelay;
        proxy_pass http://api:8000;
    }

    location / {
        root /usr/share/nginx/html;  # built frontend static assets
        try_files $uri /index.html;
    }
}

server {
    listen 80;
    server_name erp.example-domain.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}
```

## 5. Secrets {#secrets}

- `.env` (git-ignored, `chmod 600`, owned by the deploy user) holds:
  `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
  `REDIS_PASSWORD`, `JWT_SIGNING_KEY`, `WHATSAPP_APP_SECRET`,
  `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_VERIFY_TOKEN`,
  `ANTHROPIC_API_KEY` (for the AI intent classifier,
  [09_AI.md](09_AI.md)), `BACKUP_ENCRYPTION_KEY`.
- `.env.example` (committed) documents every required key with a
  placeholder and a one-line comment — the only file new environment
  setup should need to consult.
- Rotation procedure (documented, run manually — not automated, since
  rotation is rare and each secret has different blast-radius
  considerations): generate new value → update `.env` → restart
  affected services (`docker compose up -d <service>`, not a full
  stack restart) → verify health checks pass → revoke old value at
  the provider (Meta app secret rotation, etc.) only after confirming
  the new value works.

## 6. Deployment procedure

1. `git pull` the target release tag/commit on the production host.
2. `docker compose build` (or pull pre-built images from a registry,
   if CI is extended to push images — documented as the next
   maturity step once release cadence increases).
3. `docker compose run --rm api alembic upgrade head` — migrations run
   as an explicit, separate step **before** swapping traffic to new
   containers, never as an application-startup side effect (a
   crash-looping new container must never leave a migration
   half-applied because it happened to be mid-migration when it
   crashed).
4. `docker compose up -d` — Compose recreates only changed services;
   health checks (§2) gate `depends_on` ordering.
5. Smoke test: `curl https://.../healthz`, send a test WhatsApp
   command from a test number, confirm a reply.
6. On failure at any step: `docker compose up -d` the previous image
   tag (images are tagged by release, never overwritten) — rollback is
   "redeploy the last known-good tag," not a separate rollback
   mechanism, keeping the deploy path itself simple and well-exercised.

**Zero-downtime consideration**: with a single API container replica,
a deploy causes a brief (few-second) interruption during container
swap — acceptable for a two-user internal tool; the migration-before-
traffic-swap ordering (step 3 before step 4) is what actually matters
for correctness (never serving requests against a schema the running
code doesn't expect), not multi-replica rolling updates, which would
be over-engineering for this deployment's actual availability
requirements. Scaling `api` to 2+ replicas behind Nginx (documented
lever, not built now) is the natural next step if that changes.

## 6b. Moving host {#moving-host}

Migrating the whole stack to another machine is
[30_VpsMigration.md](30_VpsMigration.md), with three scripts under
`scripts/`. The short version: the database travels as a `pg_dump`
rather than as its volume (the on-disk format is architecture-specific),
the tunnel credentials carry the public hostname so DNS never changes,
and Redis is deliberately left behind.

## 7. Backup & restore {#backup-restore}

- **Backup**: automated nightly (§ [11_BackgroundWorkers.md §5](11_BackgroundWorkers.md#nightly-backup)),
  `pg_dump --format=custom` (enables selective/parallel restore),
  encrypted at rest (`BACKUP_ENCRYPTION_KEY`, via `age` or GPG
  symmetric encryption applied to the dump file) before being written
  to the `backups` volume, which is mounted from **physically separate
  storage** from `pg_data` (a distinct disk/mount point, or synced to
  off-host object storage — the concrete choice depends on host
  provider, documented in the ops runbook, not this spec, but the
  requirement — never co-located with the primary data disk — is
  fixed).
- **Restore procedure** (manual, deliberately not one-command from
  WhatsApp — see [08_WhatsApp.md #restore](08_WhatsApp.md#restore)):
  1. `owner` initiates via WhatsApp `restore <backup-id>`.
  2. System generates a one-time confirmation code, shown **only** on
     the admin dashboard (requires JWT-authenticated dashboard access
     — the second channel).
  3. `owner` replies to WhatsApp with that code.
  4. System runs restore into a **new, separate database** first
     (`textile_erp_restore_verify`), runs the reconciliation checks
     from [11_BackgroundWorkers.md §6](11_BackgroundWorkers.md#reconciliation)
     against it, and reports the result before touching production
     data.
  5. Only on explicit second confirmation does the system swap the
     verified-restored database in for the live one (via a
     connection-string/schema-rename swap, minimizing downtime, with
     the previous live database renamed-not-dropped so it's
     recoverable if the restore itself was a mistake).
  - This multi-step, two-channel, verify-before-swap procedure exists
    because restore is the single action in this entire system capable
    of discarding real, committed business data — it is deliberately
    the most ceremonious operation exposed, in direct proportion to
    its blast radius, per the "hard-to-reverse operations" guidance
    this spec follows throughout.

## 8. Monitoring & logging

- Structured JSON logging (`structlog`) from every service, shipped to
  the host's Docker logging driver (`json-file` with rotation
  configured, `max-size: 10m, max-file: 5`, in
  `docker-compose.yml`'s `logging:` block per service) — sufficient at
  current scale; a log aggregator (Loki/ELK) is a documented upgrade,
  not built now.
- Health checks (`/healthz` on `api`, Postgres/Redis native health
  checks) drive both Docker's own restart behavior and an external
  uptime check (a simple cron-based curl-and-alert script, or a free-
  tier third-party uptime monitor hitting `/healthz` — the specific
  tool is an ops choice, not an architectural one).
- Application-level alerting (backup failure, reconciliation mismatch,
  suspicious activity, repeated infra errors — per each owning doc's
  §alerting behavior) goes directly to `owner`s via WhatsApp, which is
  already the channel they're guaranteed to see promptly — no separate
  alerting stack (PagerDuty, etc.) is warranted for a two-person
  business.

## 9. Environment parity

- `local`, `staging`, `production` all run the identical Compose
  topology (§1) — the only differences are `.env` values (weaker
  secrets, smaller resource limits, debug logging locally) and a
  `docker-compose.override.local.yml` for local-only conveniences
  (exposed Postgres port for `psql` access, hot-reload volume mounts).
  This parity is what makes staging a trustworthy pre-production gate
  for OCR template tuning
  ([01_Architecture.md §9](01_Architecture.md#9-configuration--environments))
  and release smoke-testing — a topology that diverges between
  staging and production would undermine exactly the confidence
  staging exists to provide.

## 10. Scalability considerations (deployment-level)

Per [01_Architecture.md §11](01_Architecture.md#11-performance--scalability-considerations):
current single-host Compose topology is appropriately sized for the
actual target scale. Documented, not built, scaling levers for later:
horizontal `api` replicas behind Nginx `upstream`, dedicated worker
hosts per queue, Postgres read replica for reporting queries, and
(only if multi-tenancy per
[18_FutureRoadmap.md](18_FutureRoadmap.md) is actually pursued)
migration to a managed orchestrator — each of these is a config/
infrastructure change layered on top of the existing service
boundaries, not a rewrite, because those boundaries (stateless API,
stateless workers, `org_id`-scoped data model) were chosen with this
path in mind from day one.

## 11. Public hostname: Cloudflare Tunnel {#tunnel}

§1's topology assumes a VPS with a public IP, port 443 open, and certbot
renewing a certificate. **That is not how this is actually deployed.**
The stack runs on the partners' own Mac behind a home connection, which
has no inbound reachability at all — so the public entrance is a
**Cloudflare Tunnel**: an outbound connection from `cloudflared` to
Cloudflare's edge, which then forwards requests back down it. No port
forwarding, no public IP, and no certbot — the edge presents the
certificate.

### Why this replaced quick tunnels

The bot was previously exposed with `cloudflared tunnel --url
http://localhost:8000`. That is a **quick tunnel**: it invents a random
`*.trycloudflare.com` hostname, and that hostname is gone the moment the
process restarts. Meta's callback URL then points at nothing.

The failure is silent in the worst way. Cloudflare returns error 1016 to
the caller, Meta's dashboard keeps showing the webhook as subscribed,
nothing is logged on this side because nothing arrives, and the first
sign of trouble is a partner saying the bot has stopped replying. This
happened twice in July 2026. A dead quick tunnel was also found running
while writing this section — the process was alive, with zero edge
connections.

A **named tunnel** fixes the cause: the hostname is a DNS record in the
zone, permanent across restarts, reboots and `cloudflared` upgrades. The
callback URL is configured once and never touched again.

### What is in the repo

| Path | Role |
|---|---|
| `docker/cloudflared/config.yml` | ingress rules — tracked, holds no secret |
| `docker/cloudflared/credentials.json` | the tunnel's identity — **gitignored**, written by `tunnel create` |
| `cloudflared` service in `docker-compose.yml` | runs it, restarts it, health-checks it |
| `docker/tunnel-check.sh` | says which step of the setup is incomplete |

Traffic goes to `https://nginx:443`, not straight to `api:8000`, so a
tunnelled request takes exactly the same path as any other public one —
the webhook rate limit, the security headers and the access log all
still apply. Verification of nginx's certificate is off (`noTLSVerify`)
because that hop is inside the compose network and the certificate is
self-signed; the encryption that matters is the tunnel's own.

### One-time setup

Steps 1–2 need a browser and the domain registrar; the rest is local.

1. **Move the zone to Cloudflare.** Add `example.com` at
   dash.cloudflare.com (Free plan is sufficient), then replace the
   nameservers at the registrar with the two Cloudflare gives you.
   Cloudflare shows the zone as Active when it has taken effect.
   *A tunnel hostname cannot exist on a zone Cloudflare doesn't host —
   the DNS record points at `<UUID>.cfargotunnel.com`, which only
   resolves inside Cloudflare.*

2. **Authorise this machine.** `cloudflared tunnel login`, then pick the
   zone in the browser. Writes `~/.cloudflared/cert.pem`.

3. **Create the tunnel and install its credentials.**

   ```bash
   cloudflared tunnel create textile-erp        # prints a UUID
   cp ~/.cloudflared/<UUID>.json docker/cloudflared/credentials.json
   ```

4. **Point the hostname at it.**

   ```bash
   cloudflared tunnel route dns textile-erp erp.example.com
   ```

5. **Wire it into the stack.** In `.env`:

   ```
   CLOUDFLARE_TUNNEL_ID=<the UUID from step 3>
   COMPOSE_PROFILES=tunnel
   ```

   The service sits behind a compose profile so that a machine without
   credentials starts the stack cleanly instead of crash-looping a
   tunnel it cannot run. Setting `COMPOSE_PROFILES` in `.env` means
   plain `docker compose up -d` includes it from then on.

6. **Start it, and check every link:**

   ```bash
   docker compose up -d cloudflared
   ./docker/tunnel-check.sh
   ```

7. **Repoint Meta, once.** Callback URL
   `https://erp.example.com/webhooks/whatsapp`, verify token
   from `WHATSAPP_VERIFY_TOKEN`. Then kill any leftover quick tunnel:
   `pkill -f 'cloudflared tunnel --url'`.

### Monitoring

The compose healthcheck calls cloudflared's own `/ready`, which reports
the number of **established edge connections** — not merely whether the
process is alive. That distinction is the whole point: the dead tunnel
found in July was a live process with zero connections. `docker compose
ps` shows it as unhealthy, and `restart: unless-stopped` plus
cloudflared's own reconnect loop covers the ordinary cases.

