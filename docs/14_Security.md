# 14 — Security

## 1. RBAC {#rbac}

Three roles (`users.role`, §3.2 in
[02_Database.md](02_Database.md#32-users)): `owner`, `staff`,
`viewer`. Full matrix:

| Capability | owner | staff | viewer |
|---|---|---|---|
| `purchase`, `sale`, `return`, `expense`, `income`, `received`, `paid` | ✅ | ✅ | ❌ (no WhatsApp access) |
| Override duplicate-invoice warning | ✅ | ❌ (escalate to owner) | — |
| Override below-cost / credit-limit warning | ✅ | ❌ (escalate) | — |
| Manual inventory adjustment / damage | ✅ | ❌ | — |
| `capital`, `withdraw` | ✅ | ❌ | — |
| Approve a pending withdrawal | ✅ (not the requester) | ❌ | — |
| `profit` command / P&L visibility | ✅ | ❌ | ✅ (dashboard, read-only) |
| Partner capital balances (dashboard field) | ✅ | ❌ (omitted from their view) | ✅ |
| `settings`, `restore`, `delete` (confirmed records) | ✅ | ❌ | ❌ |
| `edit`/`undo` own draft entries | ✅ | ✅ | — |
| `edit`/`undo` confirmed entries | ✅ | ❌ | — |
| `export` | ✅ | ✅ (logged distinctly) | ✅ (dashboard) |
| `stock`, `search`, `dashboard`, `summary`, `ledger`, `cash`, `bank`, `supplier`, `customer` | ✅ | ✅ | ✅ (dashboard) |
| Admin API: users, ocr-templates, audit-logs | ✅ | ❌ | ❌ |

Enforced in exactly one place per interface: a `require_role(min_role)`
decorator/dependency used by both the WhatsApp command dispatcher and
FastAPI route dependencies, both reading from the same `users.role` —
not two parallel permission implementations that could drift (same
principle as the AI-query permission reuse in
[09_AI.md §6](09_AI.md#6-permissions)).

**Why only three roles, and why not per-action granular permissions**:
this is a two-partner business with at most a handful of staff. A
fine-grained permission-matrix UI would be complexity built for an
organization size that doesn't exist yet — see
[18_FutureRoadmap.md](18_FutureRoadmap.md) for where a richer
permission model becomes worth it (multi-tenant SaaS with varied org
structures).

## 2. Audit log {#audit-log-detail}

Schema: [02_Database.md §3.18](02_Database.md#318-audit_logs-audit-logs).
Guarantees:
- Every mutating service method writes exactly one `audit_logs` row in
  the same DB transaction as its business-table writes — enforced by a
  CI test that statically checks every method in `backend/services/`
  matching a mutation naming pattern (`create_*`, `confirm_*`,
  `undo_*`, `update_*`, `delete_*`) calls `AuditService.record(...)`
  at least once (see
  [17_CodingStandards.md](17_CodingStandards.md), and
  [15_Testing.md #audit-coverage-test](15_Testing.md#audit-coverage-test)).
- `audit_logs` has **no `UPDATE`/`DELETE` grants**, including to the
  application's own database role — enforced at the Postgres role
  level:
  ```sql
  REVOKE UPDATE, DELETE ON audit_logs FROM app_user;
  GRANT INSERT, SELECT ON audit_logs TO app_user;
  ```
  A compromised application-layer bug (or a compromised app-tier
  credential) literally cannot alter or erase the audit trail — this
  is a defense-in-depth guarantee that doesn't depend on application
  code being bug-free.
- `before_state`/`after_state` capture full row snapshots (JSONB),
  enabling exact reconstruction of "what did this look like before/
  after" without needing to replay other tables.

## 3. Daily backups & version history

- Nightly automated backup: [11_BackgroundWorkers.md §5](11_BackgroundWorkers.md#nightly-backup).
- "Version history" (per `CLAUDE.md`) is provided by the combination
  of (a) `audit_logs` before/after snapshots for field-level history,
  and (b) nightly backups for point-in-time full-database recovery —
  not a separate row-versioning system (e.g., `SELECT ... FOR SYSTEM
  TIME AS OF`-style temporal tables), which would be redundant given
  audit logs already capture every change with full context (who,
  when, via which channel) that a generic temporal table would not.

## 4. Input validation

- Every WhatsApp command and API request is validated through typed
  Pydantic schemas before reaching service logic — untrusted input
  (WhatsApp text, OCR output, API bodies) never reaches a repository/
  ORM call unvalidated.
- SQL injection: eliminated structurally — all queries go through
  SQLAlchemy's parameterized query construction (§ pattern in
  [01_Architecture.md §12](01_Architecture.md#12-illustrative-pattern-not-a-stub-this-is-the-actual-shape-every-servicerepository-follows));
  raw SQL strings are never built via f-string/`.format()`
  interpolation of user input anywhere in the codebase — enforced via
  a `ruff`/custom lint rule banning `text()` calls with non-literal
  arguments (see [17_CodingStandards.md](17_CodingStandards.md)).
- AI/NLU layer: explicitly does **not** generate SQL — see
  [09_AI.md §1](09_AI.md#1-why-this-is-not-send-the-question-to-an-llm-with-db-access)
  for why this is a security property, not just a design preference.
- File uploads (purchase sheet photos/PDFs): MIME-type validated
  server-side (not trusted from the client-declared type), size-capped
  (`settings.max_attachment_size_mb`, default 15MB), and PDFs are
  rendered to images via a sandboxed renderer (`pdf2image`/`poppler`)
  rather than any code path that could execute embedded PDF
  JavaScript/actions.

## 5. WhatsApp sender verification {#whatsapp-sender-verification}

- Every inbound webhook POST is verified via HMAC-SHA256 signature
  (`X-Hub-Signature-256` header, computed against the Meta app secret)
  before any parsing occurs — an unsigned or mis-signed payload is
  rejected with `401` and never touches business logic.
- Sender authentication is the phone number itself, resolved against
  `users.whatsapp_number` (§ [08_WhatsApp.md §2](08_WhatsApp.md#2-sender-resolution-sender-resolution)) —
  WhatsApp's own phone-number verification (SIM-based) is the identity
  root of trust here; there is no additional password/PIN layer for
  WhatsApp commands, a deliberate tradeoff favoring frictionless daily
  use, offset by the dual-approval requirement on the one
  highest-risk action class (large capital withdrawals, §
  [06_Accounting.md §8](06_Accounting.md#8-partner-capital-accounting)).
- If a partner's phone is lost/compromised, `owner`-only `settings`/
  admin-API `PATCH /users/{id}` can immediately deactivate that
  `whatsapp_number` (`is_active = false`), cutting off command access
  without needing WhatsApp-side account changes — response time for
  this matters, so it's a single dashboard action, not a multi-step
  process.

## 6. Rate limiting {#rate-limiting}

- WhatsApp command rate limiting: [08_WhatsApp.md §8](08_WhatsApp.md#8-rate-limiting)
  (abuse/mistake protection for known users).
- API rate limiting: [10_API.md §7](10_API.md#7-rate-limiting--abuse-protection)
  (login brute-force protection + general throttling).
- Perimeter: Nginx `limit_req` zones as a coarse first line of defence
  (protects worker/DB capacity from any burst regardless of source),
  independent of and in addition to the application-level limiters
  above — two layers because Nginx rate limiting is cheap and stateless
  (protects against volume) while the Redis-backed limiters are precise
  and identity-aware (protects against a specific user/IP's behaviour).

  | Zone | Applies to | Rate | Burst |
  |---|---|---|---|
  | `webhook` | `/webhooks/` | 30 r/s | 60 |
  | `login` | `/api/v1/auth/` | **12 r/min** | 5 |
  | `api` | everything else under `/api/` | 20 r/s | 40 |

  Throttled requests get **429**, not 503: a client being slowed down
  should be told to slow down, not told the server is broken.

> **This paragraph used to describe zones "on the webhook and API
> endpoints". Only the webhook one existed.** The 2026-08-01 audit
> (§13) confirmed it against production: twelve consecutive wrong
> passwords, twelve 401s, no throttling at any layer — the
> application-level login protection this document points at in
> `10_API.md §7` was never built either. A control that exists only in
> the documentation is worse than a missing one, because it stops
> anyone looking.

## 7. Secrets management {#secrets}

- No secrets committed to the repository, ever — `.env` files are
  git-ignored; `docker-compose.yml` references environment variables,
  never hardcoded values (see
  [16_Deployment.md](16_Deployment.md#secrets)).
- Production secrets (DB credentials, WhatsApp app secret/access
  token, JWT signing key) are injected via the deployment platform's
  secret store (Docker Compose `.env` file with restricted filesystem
  permissions `600`, owned by the deploy user only, on the single
  production host — the appropriate level of ceremony for a
  single-server deployment; a dedicated secrets manager (Vault, AWS
  Secrets Manager) is a documented upgrade path in
  [18_FutureRoadmap.md](18_FutureRoadmap.md) if/when the deployment
  topology grows beyond one host).
- JWT signing key: rotated procedure documented in
  [16_Deployment.md](16_Deployment.md), minimum 256-bit random value,
  never derived from a guessable seed.

## 8. Suspicious transaction detection {#suspicious-transaction-detection}

Runs both synchronously (at transaction time, blocking-or-warning per
each domain doc) and via the hourly `suspicious_activity_scan`
aggregate job ([11_BackgroundWorkers.md §9](11_BackgroundWorkers.md#9-suspicious_activity_scan))
for patterns only visible across multiple transactions:

| Pattern | Detection | Response |
|---|---|---|
| Repeated below-cost sales by one user in a short window | ≥3 below-cost-confirmed sales by the same `created_by` within 1 hour | Alert to all `owner`s: "Farida confirmed 3 below-cost sales in the last hour — worth a check-in?" |
| Large manual inventory adjustment | `adjustment_increase`/`decrease` magnitude > `settings.large_adjustment_value_threshold` (default ₹10,000 equivalent value) | Requires `owner` (already enforced, per §1) + logged with elevated visibility on dashboard |
| Unusual transaction time | Purchase/sale confirmed between 12 AM–5 AM org-local time (outside normal business hours, configurable) | Soft flag only, surfaced in weekly summary — not blocked, since legitimate late entry (catching up on paperwork) is plausible, but worth surfacing as a pattern |
| Rapid sequential undo/re-entry cycling on the same entity | ≥3 undo+recreate cycles on the same product/customer within 1 hour | Flagged for review — possible sign of confusion, a bug being worked around manually, or (rarely) an attempt to obscure a transaction's true history |
| Duplicate-invoice or duplicate-sale warning overridden repeatedly | ≥3 overrides by the same user within 24h | Flagged — overrides are meant to be occasional exceptions to a good default, not a routine bypass |
| Capital withdrawal just under the dual-approval threshold, repeatedly | ≥2 withdrawals within 7 days each individually below `capital_withdrawal_dual_approval_threshold` but summing above it | Flagged — a plausible pattern for deliberately avoiding the dual-approval control, surfaced (not blocked) so the other partner is aware |

All of these are **detection and visibility, not automatic blocking**
(beyond the underlying action's own existing permission/warning
rules) — consistent with the system's overall philosophy
([00_ProjectVision.md §3](00_ProjectVision.md#3-why-not-a-crud-application-not-a-crud-app)):
surface judgment-requiring situations to the humans who own the
business, don't try to arbitrate them algorithmically.

## 9. Data retention {#data-retention}

- Financial/transactional data: retained indefinitely (no automatic
  purge) — this is the business's permanent accounting record.
- Soft-deleted rows: retained indefinitely as well (never
  hard-deleted by any automated process); a manual, `owner`-approved,
  audited hard-purge tool is a documented future admin capability, not
  built in v1, since there's no current requirement driving it.
- `audit_logs`, `inventory_movements`, `cash_ledger`, `bank_ledger`:
  partitioned by month (§9 in
  [02_Database.md](02_Database.md#9-performance-considerations-specific-to-this-schema)),
  old partitions archived (moved to cheaper storage) but never dropped.
- Backups: `settings.backup_retention_days` (default 90) rolling
  window for *automated* backups; the partners can additionally
  request an on-demand `backup` be retained longer by flagging it
  explicitly (a simple `retain_until` override on that backup's
  metadata).

## 10. Signed download links {#signed-download-links}

Report exports and attachment downloads
([13_Reports.md §9](13_Reports.md#9-failure-scenarios)) use
time-limited, signed URLs (HMAC-signed query param, verified
server-side, no session/cookie required so they work when forwarded)
rather than permanently-accessible object storage URLs — a forwarded
WhatsApp document link or dashboard export link expiring after
`settings.report_link_expiry_days` bounds the exposure window for
financial documents that may end up outside the intended recipient's
control (forwarded, screenshotted-and-shared, etc. — expiry limits
the *link's* usable lifetime even though it can't prevent
already-downloaded copies from persisting).

## 11. Transport security

- All external traffic terminates TLS at Nginx (Let's Encrypt-issued
  certs, auto-renewed — see [16_Deployment.md](16_Deployment.md)); no
  plaintext HTTP is served beyond the ACME challenge path and an
  HTTP→HTTPS redirect.
- Internal service-to-service traffic (FastAPI ↔ Postgres/Redis) stays
  within the Docker Compose private network, not exposed on host
  ports beyond what's needed for debugging in non-production
  environments.

## 12. Dependency & container security

- Base images pinned to specific digests (not floating `latest` tags)
  in every `Dockerfile` — see
  [16_Deployment.md](16_Deployment.md#dockerfiles).
- Dependency versions pinned (`requirements.txt`/`poetry.lock` fully
  locked); `pip-audit`/`safety` (or equivalent) run in CI to catch
  known-vulnerable dependency versions before merge — see
  [15_Testing.md](15_Testing.md#ci-pipeline).
- Containers run as non-root users (explicit `USER` directive in every
  Dockerfile), and the Postgres data volume and backup volume are the
  only paths requiring write access beyond application code
  directories.

## 13. Audit, 2026-08-01 {#audit-2026-08}

Run when the repository was made public. Everything below was confirmed
against the live deployment, not read off the configuration — the two
had already diverged once (§6), and a control nobody has exercised is a
control nobody should trust.

### Fixed

| # | Finding | Severity |
|---|---|---|
| 1 | The dashboard owner's password had been pasted into a chat transcript and **still authenticated**. Rotated to a generated 24-character secret; the old one now fails. | **critical** |
| 2 | **No rate limiting on sign-in**, at any layer. Twelve wrong passwords, twelve 401s. With #1 that is a complete account-takeover chain against an internet-reachable dashboard. Now 12/min per address. | **critical** |
| 3 | **Every security header was missing from the dashboard page itself.** `add_header` is inherited only while the child block declares none of its own; `location = /index.html` set `Cache-Control` and silently dropped HSTS, `X-Frame-Options`, `nosniff` and `Referrer-Policy`. They were present on `/api/` throughout, which is why nobody noticed. Moved to `docker/security-headers.conf` and re-included wherever a block adds a header. | high |
| 4 | **No Content-Security-Policy.** Added a strict one — the dashboard has no build step, no CDN and no third-party anything, so it can afford `script-src 'self'` with no escape hatch. | medium |

### Verified sound

- **Webhook signatures**: HMAC-SHA256 over the raw body, constant-time
  compare, fails closed on a missing or malformed header.
- **Authorisation**: every API route 401s unauthenticated. `org_id` is
  read from the token and never from the request.
- **Passwords**: Argon2. **JWT**: 15-minute access tokens, 64-character
  signing key.
- **Network exposure**: only nginx publishes a port. Postgres, Redis and
  the API are unmapped inside the compose network; the local override
  that publishes them is not in use in production. The WhatsApp bridge
  binds `127.0.0.1` only.
- **No CORS middleware**, so no cross-origin API use is possible — the
  right answer for a same-origin dashboard.
- **Refresh tokens** are returned by the API and the frontend simply
  never stores one, so there is nothing for an XSS bug to steal. Note
  this differs from [`21_WebDashboard.md §5`](21_WebDashboard.md), which
  describes an `HttpOnly` cookie that was never implemented; the
  behaviour is safer than the design, and the session just expires after
  15 minutes.

### Open

- **Backups are plaintext.** `BACKUP_ENCRYPTION_KEY` exists in
  `.env` and in `Settings`, and **nothing reads it** — the `pg_dump`
  output is written unencrypted. A setting that implies a protection it
  does not provide is the dangerous kind. Either wire it up or delete
  it; leaving it is the one option that must not stand.
- **Backups are on the same disk as the database they protect**, so
  neither survives losing the machine.
- No `ssl_prefer_server_ciphers`, and `ssl_ciphers HIGH:!aNULL:!MD5`
  is broader than needed. Low impact behind Cloudflare, which
  terminates TLS for the public request anyway.
