# 30 — Moving to a VPS

## 1. Why this is small {#why-small}

The system was built to be a deployment unit: one `docker-compose.yml`,
one `.env`, named volumes. Nothing is installed on the laptop that the
VPS will not get from `docker compose up`.

Two facts make the cutover far less frightening than it sounds:

- **The public hostname follows the Elastic IP, not the host.**
  `erp.captainsresearch.co.in` is an A record pointing at an Elastic
  IP, and the stack serves 443 directly ([16 §11](16_Deployment.md#tunnel)).
  Re-associate that address with the new instance and the same hostname
  answers from it. **No DNS change, no propagation wait.**

  > This used to be true for a different reason. The site was fronted
  > by a *named* Cloudflare Tunnel, and the hostname followed
  > `credentials.json` rather than any IP. That was dropped because
  > Cloudflare's free plan routes Indian traffic via Singapore, which
  > cost ~350 ms on every request. The export still carries the tunnel
  > credentials, so the old arrangement remains available as a
  > fallback — but it is not what serves today.

- **Meta needs no reconfiguration.** The webhook URL is that hostname,
  so the app secret, verify token, phone number id and the recipient
  allowed list all stay exactly as they are.

The whole migration is therefore: copy the data, copy the secrets, stop
the old stack, move one IP address.

## 2. What moves, and what deliberately does not {#inventory}

| | Size | How it travels |
|---|---|---|
| Database — **both businesses** | 80 MB volume → **396 KB dump** | `pg_dump --format=custom` |
| Attachments | 1.1 MB | volume tar — **must move** |
| Redis | 808 KB | volume tar — **carries demo mode** |
| Host `data/` | 9.9 MB | manual pre-purge backups |
| Backup history | 2.9 MB | volume tar |
| Reports | 108 KB | volume tar |
| `.env` | 2 KB | in the package |
| Tunnel credentials | 200 B | in the package |
| TLS certs | — | in the package |
| Let's Encrypt state | ~200 KB | volume tar — **or TLS cannot renew** |
| Code | — | `git clone` |

**Total: ~15 MB.** The demo business is not a separate anything — it is
rows in the same database under `org_id 0000…0dbeef`, so the one dump
carries both sets of books, both sets of partners and both watermarks.

**The Let's Encrypt volume is copied**, and it is the one item here
whose absence is invisible. `secrets/certs` carries the certificate
nginx actually serves, so a host without this volume passes every check
on cutover day and serves TLS perfectly — while `certbot renew` has no
account key and no renewal config, fails silently twice a day into a
log nobody reads, and the site stops answering the day the certificate
expires. `migrate-verify.sh` checks for it by name for that reason.

**Redis is copied**, though it looks like a cache. `wa:demo:<number>`
is what decides which set of books a phone writes to, and both partners'
phones are on the demo right now. Dropping it would silently return them
to the real business mid-demonstration — precisely the accident demo
mode exists to prevent. It also carries the webhook dedup keys, so a
message Meta redelivers across the cutover is not processed twice.

**Not moved, on purpose:**

- **`pg_data` as a raw volume.** Its on-disk format is tied to the CPU
  architecture and server build, and Postgres will refuse to start on a
  data directory written by the other. This has now mattered in both
  directions: `arm64` laptop → `x86_64` EC2 on the first move, and
  `x86_64` → `arm64` on the move to Graviton. The dump restores
  anywhere, which is why the database is the one thing that does *not*
  travel as a volume.
- **`whatsapp-bridge/session/`.** A 228 MB Chromium profile tied to
  this machine and this CPU. Re-scan the QR on the VPS *if* the bridge
  is ever needed — and it is not needed today: `WHATSAPP_TRANSPORT=meta`
  carries every command, and group broadcasting is off. **The bridge can
  be left behind entirely.**

## 3. The three scripts {#scripts}

```
scripts/migrate-export.sh    on the old host, stack up
scripts/migrate-import.sh    on the new host, from the cloned repo
scripts/migrate-verify.sh    on the new host, after cutover
```

`migrate-export.sh` writes one `tar.gz` and prints its sha256. It
refuses to package an empty database, and records an **exact
`COUNT(*)` per table** — not `pg_stat_user_tables.n_live_tup`, which is
a planner estimate that reads zero until `ANALYZE` has run and would
let a restore "match" by both sides being unmeasured.

`migrate-import.sh` installs the secrets, brings up **only** postgres
and redis (an app container connecting mid-restore would read a
half-populated schema and could write against it), restores, then
compares every table against those counts and **exits non-zero** if any
is short or missing.

`migrate-verify.sh` answers "is this host actually serving the
business". Every check corresponds to a failure that has really
happened here, which is why it prints what a failure *means* rather
than a bare tick — including the stale-app-secret trap that silently
401s every inbound message.

## 4. The run {#runbook}

### Before you start

- A VPS with Docker and the compose plugin. 2 GB RAM is comfortable;
  the whole dataset is under 100 MB.
- **Ports 80/443 must be open to the world**, and 22 only to you. The
  stack serves TLS itself now, and 80 is not optional — Let's Encrypt's
  HTTP-01 challenge is what renews the certificate.
- **Match the AMI to the instance architecture.** A Graviton (`t4g`,
  `m7g`, …) instance needs an `arm64` image; the `x86_64` one will not
  boot, and this is also why the old root volume cannot simply be
  detached and re-attached.
- Push the code first: the VPS clones from `origin`.

```bash
git push origin main
```

### On the old host

```bash
docker compose up -d              # export reads a running postgres
./scripts/migrate-export.sh
```

Move the archive over `scp` to a host you control — never through chat
or cloud storage. **It contains every credential the business has.**

```bash
scp data/migration/textile-erp-<stamp>.tar.gz user@vps:/tmp/
```

### On the VPS

```bash
git clone https://github.com/captainsaify/textile-erp.git
cd textile-erp
./scripts/migrate-import.sh /tmp/textile-erp-<stamp>.tar.gz
```

It stops before starting the app, deliberately.

### The cutover

**Stop the whole old stack, not just its front door.** Moving the
Elastic IP stops anything *reaching* the old host, which makes it
tempting to leave it running as a warm rollback. Don't: **beat reaches
Meta outbound and needs no inbound route at all.** Left up, it keeps
firing daily check-ins and partner notices at three real phones, from
books that stopped being true at the cutover — and the partners have no
way to tell those from the live ones. `stop` preserves every volume, so
stopping costs nothing in rollback terms.

```bash
# old host
docker compose stop

# then, in the AWS console: re-associate the Elastic IP

# new host
docker compose up -d
./scripts/migrate-verify.sh
```

**Build the images before stopping the old host.** `docker compose
build` takes about four minutes on 2 vCPU — longer on a cold cache —
and there is no reason for it to happen inside the outage window.

The address moves in seconds, but a client that resolved the hostname
before the switch may hold the old IP for the rest of its TTL. Nothing
answers there once the old stack is stopped, so the failure mode is a
brief refusal rather than a wrong answer — which is the right way round.

Then, from a partner's phone: send `help`, then `activity`. The second
one proves the database came across, because it can only answer from
real rows.

### Keep the old host for a week

Stopped, not deleted. `docker compose stop` leaves every volume intact,
so rolling back is starting the old tunnel and stopping the new one. A
migration you cannot reverse is not finished, it is committed.

## 5. Afterwards {#afterwards}

- **Delete the archive from both ends.** It is a plaintext copy of
  every secret.
- **Rotate `JWT_SIGNING_KEY` and the dashboard passwords** if the
  archive touched anything you do not fully control. Nothing else needs
  rotating: the Meta credentials are tied to the app, not the host.
- **Check the first daily check-in fires** the next morning at
  `settings.daily_checkin_hour` ([22 §8](22_GroupBroadcast.md#daily-checkin)).
  It is the first scheduled job that reaches a person, so it is the one
  that proves beat survived the move.
- **The nightly backup writes to a volume on the same disk.** That was
  acceptable on a laptop that a person looks at. On a VPS it is the
  thing to fix next — [16 §7](16_Deployment.md#backup-restore) already
  requires separate storage, and a VPS makes off-host object storage a
  one-line addition rather than a project.

## 6. What a VPS is still worth doing about {#after-move}

Two limits are *not* solved by moving hosts, and it is worth being
clear that they are unrelated to it:

- The Meta sender is a **test number**: five registered recipients, and
  a 24-hour window on free-form messages ([22 §8](22_GroupBroadcast.md#delivery)).
  A verified business number removes both. That is a Meta process, not
  a hosting one.
- The web.js bridge, if group broadcasting is ever switched on, needs
  Chromium and a QR scan on the VPS. It has no session to inherit.

## 7. Sizing the box {#sizing}

Measured on the live stack, not estimated.

| | Default | Tuned for a small host |
|---|---|---|
| worker-whatsapp | 371 MB | **128 MB** |
| worker-scheduled | 294 MB | **127 MB** |
| worker-ocr | 125 MB | **89 MB** |
| api | 120 MB | 120 MB |
| beat · postgres · cloudflared · redis · nginx | 177 MB | 177 MB |
| **total** | **1086 MB** | **640 MB** |

The difference is entirely Celery prefork children — 14 of them at the
defaults, each holding its own copy of the app. Those defaults are
sized for a busy system. This business is three people and a handful of
messages an hour, so:

```bash
CELERY_WHATSAPP_CONCURRENCY=2
CELERY_OCR_CONCURRENCY=1
CELERY_SCHEDULED_CONCURRENCY=2
```

Two concurrent WhatsApp tasks is still more parallelism than three
partners can generate. Nothing is given up.

### So: 2 GB is enough

640 MB of stack plus ~250 MB of Ubuntu leaves roughly **1 GB free** on
a 2 GB box. Two things make it comfortable rather than merely possible:

- **The images build cheaply.** The dependency set is
  `opencv-python-headless` and `numpy` — no PaddleOCR, no torch — and
  both ship prebuilt wheels that `uv` downloads rather than compiles.
  The build is I/O, not memory.
- **Swap.** `vps-bootstrap.sh` gives a 2 GB host **4 GB** of swap,
  because the build is the one tight moment and swap is what stops it
  being fatal.

Disk is not a constraint at any tier: the whole dataset is 15 MB and
`pg_data` is 80 MB. Budget ~5 GB for images and 30 GB is generous.

**1 vCPU works.** Builds are slower; nothing else notices.

Go to 4 GB only if you plan to run local OCR heavily (PaddleOCR would
change the arithmetic) or add a second business for real.

## 8. Serving 443 ourselves {#direct-443}

The tunnel was retired on 2026-08-03. It is still configured and can be
brought back in one command — see the rollback below — but it is not
what answers the hostname today.

**Why.** Cloudflare's free plan does not serve Indian traffic from
Indian PoPs. Every request went India → Singapore → back to a Chennai
or Mumbai edge → the box, and the same in reverse: **~450 ms**, against
an origin that answers in 3 ms. Serving directly costs ~100 ms, and the
dashboard is the one part of the system a person sits and waits on.

**What it takes.** An A record for `erp` pointing at the box, with
proxying **off** — a grey cloud, not orange. Orange keeps the Singapore
detour and the entire exercise is wasted. Inbound 80 and 443 open on
the instance's security group; 80 is not optional, ACME renewals
validate over it.

**What is given up.** The origin IP is public and Cloudflare's WAF and
DDoS filtering are out of the path. The security group and nginx are
now the only things in front of the login form — which is why the
dashboard password stops being a formality (see
[14 §rbac](14_Security.md#rbac)).

**Issuing the first certificate without downtime.** The 443 server
block carries its own `/.well-known/acme-challenge/` location, not just
the :80 block. That looks redundant and is not: while the tunnel still
fronts the site, its ingress speaks to `https://nginx:443`, so a
challenge arriving that way never reaches the :80 block and falls
through to the SPA — which answers **200 with index.html**, so the ACME
client is cheerfully told the token exists and reads a web page
instead. With that location present the certificate can be issued
*through the tunnel*, before DNS is moved, so the hostname never serves
the self-signed placeholder to Meta or anyone else.

**Renewal** is `scripts/renew-cert.sh`, twice daily from cron. It
copies out of certbot's volume — a renewal nobody copies out is a
certificate that expires anyway — and **restarts** nginx rather than
reloading it. `nginx -s reload` was observed serving the previous chain
after the files had been replaced underneath it: the container could
see the new bytes and still presented the old certificate until the
process came back.

**Rollback**, if the exposure is ever regretted:

```bash
docker compose up -d cloudflared      # credentials were never removed
```

then re-add the tunnel's public hostname in Cloudflare, which recreates
the DNS record. Nothing else changes: the webhook URL is the hostname,
so Meta needs no reconfiguration in either direction.

## 9. Moving to Graviton (arm64) {#graviton}

AWS's own recommendation to move `t3.small` → `t4g.small` is worth
taking — same 2 vCPU and 2 GB, roughly 20% cheaper — but it is **not a
resize**. The architectures differ, so the x86_64 root volume will not
boot on Graviton and the old EBS volume cannot simply be re-attached.
It is a new instance plus a full restore, which is why it belongs in
this document rather than in a one-line runbook.

**The compatibility question is answerable, not a matter of nerve.**
Do not reason about it — compute it. Every wheel in `uv.lock` either
has an `aarch64` build or is pure Python:

```bash
python3 - <<'PY'
import re, pathlib
blocks = pathlib.Path("uv.lock").read_text().split("[[package]]")
for b in blocks[1:]:
    name = re.search(r'name = "([^"]+)"', b)
    wheels = re.findall(r'url = "[^"]*/([^/"]+\.whl)"', b)
    if not name or not wheels:        # sdist-only builds anywhere
        continue
    if not any(k in w for w in wheels
               for k in ("any.whl", "aarch64", "arm64", "universal2")):
        print("NO aarch64 wheel:", name.group(1))
PY
```

Silence means go. Run it again after any dependency change that lands
before the move.

**The one thing that would block it is already absent.** PaddlePaddle
publishes no `aarch64` wheel on PyPI, and `ocr_primary_engine` still
names `paddle` in config — but it has never been installed, the import
fails, and `backend/ocr/engines.py` degrades to the vision engine and
Tesseract without comment. Verify rather than assume:

```bash
docker compose exec -T worker-ocr python -c \
  "import importlib.util as u; print(u.find_spec('paddle') is not None)"
```

If that ever prints `True`, this migration stops being free.

**The rest is unremarkable.** All four base images
(`python:3.12-slim`, `postgres:16-alpine`, `redis:7-alpine`,
`nginx:1.27-alpine`) are multi-arch, and images are built on the box —
so on Graviton they build natively, with no `buildx`, no emulation and
no cross-compilation. A side benefit: the development laptop is arm64,
so for the first time dev and production agree on architecture.

**Sequencing matters if a Savings Plan is involved.** A Savings Plan
commits to *dollars per hour*, not to an instance. Bought at the
`t3.small` rate and then followed by a move to `t4g.small`, usage falls
below the commitment and the difference is paid for nothing — the
saving is bought and handed straight back. Migrate first, let the bill
settle, then size the plan to the lower number. Use a **Compute**
Savings Plan, not an **EC2 Instance** one: the latter locks to an
instance family and would have stranded this move.
