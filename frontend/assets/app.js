/* Dashboard shell -- docs/21_WebDashboard.md.
 *
 * Read-only by design (CLAUDE.md philosophy #5): the only writes this
 * page can make are signing in, signing out, and acknowledging a
 * reconciliation mismatch. Everything that changes business data stays
 * on WhatsApp.
 *
 * The access token lives in a module variable, never localStorage --
 * docs/21 §5. An XSS bug that can read localStorage hands over a
 * long-lived credential; a variable at least dies with the tab.
 */

(() => {
  const API = "/api/v1";
  let token = null;
  let me = null;

  const $ = (id) => document.getElementById(id);

  function banner(message) {
    const node = $("banner");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(banner.timer);
    banner.timer = setTimeout(() => {
      node.hidden = true;
    }, 4000);
  }

  async function api(path, options = {}) {
    const response = await fetch(API + path, {
      ...options,
      headers: {
        ...(options.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
    });
    if (response.status === 401) {
      signOut();
      throw new Error("Session expired — please sign in again.");
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || detail.message || `Request failed (${response.status})`);
    }
    return response.json();
  }

  /* Owner-only endpoints 403 for staff. That is not an error to shout
   * about -- the section simply isn't theirs to see (docs/12 §6). */
  async function optional(path) {
    try {
      return await api(path);
    } catch {
      return null;
    }
  }

  const text = (value) => String(value ?? "");

  /* The API uses two list envelopes: paged reads return
   * {items, next_cursor}, the endpoints added for this dashboard return
   * {data}. Reading both here beats changing tested response shapes to
   * suit the client. */
  const rowsOf = (payload) => (payload && (payload.items || payload.data)) || [];

  function table(columns, rows, { onRowClick } = {}) {
    const el = document.createElement("table");
    const head = el.createTHead().insertRow();
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column.label;
      if (column.numeric) th.style.textAlign = "right";
      head.append(th);
    });
    const body = el.createTBody();
    if (!rows.length) {
      const cell = body.insertRow().insertCell();
      cell.colSpan = columns.length;
      cell.className = "muted";
      cell.textContent = "Nothing here yet.";
      return el;
    }
    rows.forEach((row) => {
      const tr = body.insertRow();
      if (onRowClick) {
        tr.className = "clickable";
        tr.addEventListener("click", () => onRowClick(row));
      }
      columns.forEach((column) => {
        const td = tr.insertCell();
        const value = column.render ? column.render(row) : text(row[column.key]);
        if (value instanceof Node) td.append(value);
        else td.innerHTML = value;
        if (column.numeric) td.className = "num";
      });
    });
    return el;
  }

  function money(value) {
    const formatted = Money.format(value);
    return Money.isNegative(value)
      ? `<span style="color:var(--bad)">${formatted}</span>`
      : formatted;
  }

  // ------------------------------------------------------------ auth

  async function signIn(event) {
    event.preventDefault();
    const error = $("login-error");
    error.hidden = true;
    try {
      const body = JSON.stringify({ email: $("email").value, password: $("password").value });
      const response = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!response.ok) throw new Error("Those details weren't accepted.");
      const payload = await response.json();
      token = payload.access_token;
      me = { full_name: payload.full_name, role: payload.role };
      $("login").hidden = true;
      $("app").hidden = false;
      $("whoami").textContent = `${me.full_name} · ${me.role}`;
      await showPage("overview");
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    }
  }

  function signOut() {
    token = null;
    me = null;
    $("app").hidden = true;
    $("login").hidden = false;
  }

  // -------------------------------------------------------- overview

  function kpi(label, value, sub, { negative = false } = {}) {
    return `<div class="kpi">
      <div class="label">${label}</div>
      <div class="value${negative ? " negative" : ""}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>`;
  }

  async function loadOverview() {
    const data = await api("/dashboard");
    $("kpis").innerHTML = [
      kpi("Cash", Money.format(data.cash_balance), "In hand", {
        negative: Money.isNegative(data.cash_balance),
      }),
      kpi("Bank", Money.format(data.bank_balance), "Balance", {
        negative: Money.isNegative(data.bank_balance),
      }),
      kpi(
        "Stock value",
        Money.format(data.inventory.value),
        `${data.inventory.active_products} products`,
      ),
      kpi(
        "Receivables",
        Money.format(data.receivables.total),
        `${data.receivables.parties} party(ies)`,
      ),
      kpi("Payables", Money.format(data.payables.total), `${data.payables.parties} party(ies)`),
    ].join("");

    const alerts = [];
    if (Number(data.inventory.negative_stock_count) > 0) {
      alerts.push(
        `<p><span class="pill bad">Negative stock</span> ${data.inventory.negative_stock_count} product(s) below zero — a sale was recorded against stock that wasn't there.</p>`,
      );
    }
    if (Number(data.inventory.low_stock_count) > 0) {
      alerts.push(
        `<p><span class="pill warn">Low stock</span> ${data.inventory.low_stock_count} product(s) at or under their reorder level.</p>`,
      );
    }
    const runs = await optional("/inventory/reconciliations?unacknowledged=true");
    const openRuns = rowsOf(runs);
    if (openRuns.length) {
      alerts.push(
        `<p><span class="pill bad">Reconciliation</span> ${openRuns.length} unacknowledged mismatch(es) — see Admin.</p>`,
      );
    }
    $("alerts").innerHTML =
      alerts.join("") || `<p class="muted">Nothing needs attention right now.</p>`;

    // Profit is owner-only; a staff account simply gets no trend, which
    // is why this degrades rather than erroring.
    const metrics = await optional("/metrics/monthly?months=6");
    if (metrics) {
      Charts.profitColumns($("chart-profit"), rowsOf(metrics));
      Charts.revenueColumns($("chart-revenue"), rowsOf(metrics));
    } else {
      const note = `<p class="muted">Profit figures are owner-only.</p>`;
      $("chart-profit").innerHTML = note;
      $("chart-revenue").innerHTML = note;
    }
  }

  // ----------------------------------------------------------- stock

  let stockRows = [];

  function renderStock(filter = "") {
    const needle = filter.trim().toLowerCase();
    const rows = needle
      ? stockRows.filter(
          (row) =>
            row.code.toLowerCase().includes(needle) ||
            (row.brand || "").toLowerCase().includes(needle) ||
            (row.description || "").toLowerCase().includes(needle),
        )
      : stockRows;

    $("stock-table").replaceChildren(
      table(
        [
          { label: "Code", key: "code" },
          {
            // a code is unique only within a brand, so a stock list
            // without this column can't tell two of them apart
            label: "Brand",
            render: (row) =>
              row.brand ? text(row.brand) : `<span class="muted">not set</span>`,
          },
          { label: "Description", key: "description" },
          { label: "On hand", numeric: true, render: (row) => text(row.qty_on_hand) },
          { label: "Avg cost", numeric: true, render: (row) => money(row.avg_cost) },
          { label: "Value", numeric: true, render: (row) => money(row.value) },
          {
            label: "",
            render: (row) =>
              Money.isNegative(row.qty_on_hand) ? `<span class="pill bad">negative</span>` : "",
          },
        ],
        rows,
      ),
    );
  }

  async function loadStock() {
    const payload = await api("/inventory?limit=200");
    stockRows = rowsOf(payload);
    renderStock($("stock-search").value);
  }

  // ------------------------------------------------------- purchases

  async function loadPurchases() {
    const payload = await api("/purchases?limit=50");
    $("purchase-detail").hidden = true;
    $("purchases-table").replaceChildren(
      table(
        [
          { label: "Date", key: "date" },
          { label: "Invoice", key: "invoice_no" },
          { label: "Supplier", key: "supplier" },
          { label: "Total", numeric: true, render: (row) => money(row.grand_total) },
          { label: "Paid", numeric: true, render: (row) => money(row.amount_paid) },
          {
            label: "Status",
            render: (row) =>
              `<span class="pill ${row.payment_status === "paid" ? "good" : "warn"}">${row.payment_status}</span>`,
          },
        ],
        rowsOf(payload),
        { onRowClick: (row) => loadPurchaseDetail(row.id) },
      ),
    );
  }

  /* The one genuinely new capability over WhatsApp (docs/21 §3): the
   * scanned sheet beside the lines that were read out of it. */
  async function loadPurchaseDetail(id) {
    const detail = await api(`/purchases/${id}`);
    const panel = $("purchase-detail");

    const lines = table(
      [
        { label: "#", key: "line_no" },
        { label: "Code", key: "code" },
        { label: "Description", key: "description" },
        { label: "Qty", numeric: true, key: "qty" },
        { label: "Rate", numeric: true, render: (row) => money(row.rate) },
        { label: "Total", numeric: true, render: (row) => money(row.line_total) },
      ],
      detail.lines || [],
    );

    panel.replaceChildren();
    const head = document.createElement("div");
    head.className = "detail-head";
    head.innerHTML = `
      <div>
        <h2>${text(detail.invoice_no)} · ${text(detail.supplier)}</h2>
        <p class="muted">${text(detail.date)} · ${money(detail.grand_total)}</p>
      </div>
      <button class="link" id="close-detail">Close</button>`;
    panel.append(head);

    const beside = document.createElement("div");
    beside.className = "scan-beside";
    const left = document.createElement("div");
    left.className = "table-scroll";
    left.append(lines);
    beside.append(left);

    const right = document.createElement("div");
    right.className = "scan-pane";
    if (detail.scan_url) {
      const img = document.createElement("img");
      img.alt = `Scanned sheet for ${text(detail.invoice_no)}`;
      img.loading = "lazy";
      img.title = "Click to view full size";
      // a photographed sheet is often unreadable at column width, so
      // clicking drops the fit-to-column constraint
      img.addEventListener("click", () => img.classList.toggle("zoomed"));
      // the image endpoint needs the bearer token, so it is fetched
      // rather than set as a plain src
      fetch(API + detail.scan_url, { headers: { Authorization: `Bearer ${token}` } })
        .then((response) => (response.ok ? response.blob() : Promise.reject(response)))
        .then((blob) => {
          img.src = URL.createObjectURL(blob);
        })
        .catch(() => {
          right.innerHTML = `<p class="muted">The original scan couldn't be loaded.</p>`;
        });
      right.append(img);
    } else {
      right.innerHTML = `<p class="muted">This purchase was typed in, not photographed.</p>`;
    }
    beside.append(right);
    panel.append(beside);
    panel.hidden = false;
    $("close-detail").addEventListener("click", () => {
      panel.hidden = true;
    });
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ----------------------------------------------------------- money

  async function loadMoney() {
    const [receivables, payables, cash, bank] = await Promise.all([
      optional("/receivables"),
      optional("/payables"),
      optional("/ledgers/cash?limit=25"),
      optional("/ledgers/bank?limit=25"),
    ]);

    const partyColumns = [
      { label: "Name", key: "name" },
      { label: "Outstanding", numeric: true, render: (row) => money(row.outstanding) },
      { label: "Oldest", render: (row) => text(row.oldest_date || "") },
    ];
    $("receivables").replaceChildren(table(partyColumns, rowsOf(receivables)));
    $("payables").replaceChildren(table(partyColumns, rowsOf(payables)));

    const entries = [
      ...((cash && cash.entries) || []).map((row) => ({ ...row, account: "Cash" })),
      ...((bank && bank.entries) || []).map((row) => ({ ...row, account: "Bank" })),
    ].sort((a, b) => String(b.date).localeCompare(String(a.date)));

    $("ledgers").replaceChildren(
      table(
        [
          { label: "Date", key: "date" },
          { label: "Account", key: "account" },
          { label: "Type", key: "type" },
          { label: "Amount", numeric: true, render: (row) => money(row.amount) },
          { label: "Balance", numeric: true, render: (row) => money(row.resulting_balance) },
          { label: "Notes", render: (row) => text(row.notes || "") },
        ],
        entries,
      ),
    );
  }

  // ----------------------------------------------------------- admin

  async function loadAdmin() {
    const runs = await optional("/inventory/reconciliations");
    const container = $("reconciliations");
    container.replaceChildren();

    if (!rowsOf(runs).length) {
      container.innerHTML = `<p class="muted">No reconciliation runs recorded yet.</p>`;
    } else {
      container.append(
        table(
          [
            { label: "Started", render: (row) => text(row.started_at || "").slice(0, 16) },
            { label: "Kind", key: "kind" },
            {
              label: "Result",
              render: (row) =>
                row.status === "ok"
                  ? `<span class="pill good">ok</span>`
                  : `<span class="pill bad">${row.status}</span>`,
            },
            { label: "Checked", numeric: true, key: "checked_count" },
            { label: "Mismatches", numeric: true, key: "mismatch_count" },
            {
              label: "",
              render: (row) => {
                if (row.mismatch_count === 0) return "";
                if (row.acknowledged_at) return `<span class="muted">acknowledged</span>`;
                const button = document.createElement("button");
                button.className = "link";
                button.textContent = "Acknowledge";
                button.addEventListener("click", async (event) => {
                  event.stopPropagation();
                  try {
                    await api(`/inventory/reconcile/${row.id}/acknowledge`, { method: "POST" });
                    banner("Recorded that you've seen it. The figures are unchanged.");
                    await loadAdmin();
                  } catch (exc) {
                    banner(exc.message);
                  }
                });
                return button;
              },
            },
          ],
          rowsOf(runs),
        ),
      );
    }

    await loadAudit();
  }

  /* The audit trail with the payload every mutation already writes.
   * Twenty rows reading "product.created" answer nothing; the same rows
   * naming the product, and expanding to the exact before/after, are a
   * record of what happened. */
  let auditRows = [];

  const IDENTIFYING = ["code", "name", "invoice_no", "reference", "amount", "via", "description"];

  function summarise(entry) {
    const state = entry.after_state || entry.before_state || {};
    // Data-driven rather than a branch per action: payload fields are
    // named consistently across services, so a new action gets a
    // readable summary without anyone editing this.
    const keys = [
      ...IDENTIFYING.filter((key) => state[key] !== undefined && state[key] !== null),
      ...Object.keys(state).filter(
        (key) => !IDENTIFYING.includes(key) && typeof state[key] !== "object",
      ),
    ].slice(0, 3);
    if (!keys.length) return `<span class="muted">no detail recorded</span>`;
    return keys
      .map((key) => {
        const raw = state[key];
        const shown = key === "amount" || key === "grand_total" ? Money.format(raw) : text(raw);
        return `<b>${shown}</b>`;
      })
      .join(" · ");
  }

  function changeGrid(entry) {
    const before = entry.before_state || {};
    const after = entry.after_state || {};
    const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])];
    if (!fields.length) {
      return `<p class="muted">Nothing was recorded beyond the action itself.</p>`;
    }

    const show = (value) =>
      value === undefined || value === null
        ? "—"
        : typeof value === "object"
          ? JSON.stringify(value)
          : text(value);

    const rows = fields
      .map((field) => {
        const was = before[field];
        const now = after[field];
        const changed = JSON.stringify(was) !== JSON.stringify(now);
        return `<div class="field">${field}</div>
          <div class="${was !== undefined && changed ? "was" : "muted"}">${show(was)}</div>
          <div class="${changed ? "changed" : ""}">${show(now)}</div>`;
      })
      .join("");

    return `<div class="change-grid">
        <div class="head">Field</div><div class="head">Before</div><div class="head">After</div>
        ${rows}
      </div>`;
  }

  function renderAudit() {
    const action = $("audit-action").value;
    const needle = $("audit-search").value.trim().toLowerCase();
    const rows = auditRows.filter((entry) => {
      if (action && entry.action !== action) return false;
      if (!needle) return true;
      return JSON.stringify(entry).toLowerCase().includes(needle);
    });

    const built = table(
      [
        { label: "When", render: (row) => text(row.created_at).slice(0, 16).replace("T", " ") },
        { label: "Action", key: "action" },
        { label: "What", render: summarise },
        { label: "By", render: (row) => text(row.actor || "") },
        { label: "Channel", key: "channel" },
      ],
      rows,
    );

    built.querySelectorAll("tbody tr").forEach((tr, index) => {
      const cell = tr.cells[2];
      if (!cell) return;
      cell.classList.add("summary");
      tr.classList.add("clickable");
      tr.addEventListener("click", () => {
        const next = tr.nextElementSibling;
        if (next && next.classList.contains("detail-row")) {
          next.remove();
          return;
        }
        const detail = tr.parentElement.insertRow(tr.rowIndex);
        detail.className = "detail-row";
        const td = detail.insertCell();
        td.colSpan = 5;
        td.innerHTML = changeGrid(rows[index]);
      });
    });

    $("audit").replaceChildren(built);
  }

  async function loadAudit() {
    const [entries, actions] = await Promise.all([
      optional("/audit?limit=200"),
      optional("/audit/actions"),
    ]);
    auditRows = rowsOf(entries);

    const select = $("audit-action");
    const chosen = select.value;
    select.replaceChildren(new Option("All actions", ""));
    rowsOf(actions).forEach((row) => {
      select.append(new Option(`${row.action} (${row.count})`, row.action));
    });
    select.value = chosen;

    renderAudit();
  }

  // ------------------------------------------------------ navigation

  const LOADERS = {
    overview: loadOverview,
    stock: loadStock,
    purchases: loadPurchases,
    money: loadMoney,
    admin: loadAdmin,
  };

  async function showPage(name) {
    document.querySelectorAll("#nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.page === name);
    });
    document.querySelectorAll(".page").forEach((page) => {
      page.hidden = page.id !== `page-${name}`;
    });
    try {
      await LOADERS[name]();
    } catch (exc) {
      banner(exc.message);
    }
  }

  $("login-form").addEventListener("submit", signIn);
  $("signout").addEventListener("click", signOut);
  $("stock-search").addEventListener("input", (event) => renderStock(event.target.value));
  $("audit-action").addEventListener("change", renderAudit);
  $("audit-search").addEventListener("input", renderAudit);
  document.querySelectorAll("#nav button").forEach((button) => {
    button.addEventListener("click", () => showPage(button.dataset.page));
  });
})();
