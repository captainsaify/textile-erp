/* Master Control — invoice entry. ui-plan.md §5.
 *
 * No framework and no build step, as everywhere else here. The one
 * genuinely fiddly component is the picker, and it is fiddly because of
 * what it has to show rather than how it is built: a code names a
 * product only together with its brand — three share `55X` on these
 * books — so the list carries the brand and the stock that tells them
 * apart. Choosing by looking is the single reason this beats the chat.
 *
 * Money never becomes a JavaScript number. Totals are computed in
 * integer paise through money.js, and the server recomputes them on
 * save; what it returns is what gets displayed afterwards.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // In memory only. A control session is 30 minutes and has no refresh
  // token; putting it in localStorage would give an XSS bug a key to
  // the half of the app that can delete things.
  let token = null;
  let kind = "purchase";
  let draftKey = null;

  const DRAFT_STORE = "control:draft";
  const DRAFT_TTL_DAYS = 7;

  async function api(path, options = {}) {
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
    });
    if (response.status === 401) {
      token = null;
      $("app").hidden = true;
      $("signin").hidden = false;
      throw new Error("Session expired — sign in again.");
    }
    const body = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      throw new Error(body?.detail || body?.message || `Request failed (${response.status})`);
    }
    return body;
  }

  function banner(message, ok = false) {
    const node = $("banner");
    node.textContent = message;
    node.className = ok ? "banner ok" : "banner";
    node.hidden = false;
    if (ok) setTimeout(() => (node.hidden = true), 6000);
  }

  // ------------------------------------------------------------ combo

  /** Text input + filtered list + keyboard. Selection is by looking,
   *  never by the code alone. */
  function combo(input, listNode, { fetchItems, render, onPick, onClear }) {
    let items = [];
    let active = -1;
    let timer = null;

    const close = () => {
      listNode.hidden = true;
      active = -1;
    };

    const paint = () => {
      listNode.replaceChildren();
      items.forEach((item, index) => {
        const row = render(item);
        row.className = `combo-item${item.__create ? " create" : ""}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(index === active));
        row.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(index);
        });
        listNode.append(row);
      });
      listNode.hidden = items.length === 0;
    };

    const choose = (index) => {
      const item = items[index];
      if (!item) return;
      onPick(item);
      close();
    };

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.addEventListener("input", () => {
      if (onClear) onClear();
      clearTimeout(timer);
      const value = input.value.trim();
      if (value.length < 1) return close();
      // Debounced: a picker that fires per keystroke turns a fourteen
      // line bill into a few hundred requests.
      timer = setTimeout(async () => {
        try {
          items = await fetchItems(value);
          active = items.length ? 0 : -1;
          paint();
          input.setAttribute("aria-expanded", String(!listNode.hidden));
        } catch (exc) {
          banner(exc.message);
        }
      }, 140);
    });

    input.addEventListener("keydown", (event) => {
      if (listNode.hidden) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        paint();
      } else if (event.key === "Enter") {
        // Only when something is highlighted, so Enter on a typo cannot
        // pick — or create — anything.
        if (active >= 0) {
          event.preventDefault();
          choose(active);
        }
      } else if (event.key === "Escape") {
        close();
      }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
  }

  // ------------------------------------------------------------- rows

  let rows = [];

  function blankRow() {
    return {
      product_id: null,
      code: "",
      brand: null,
      description: "",
      pieces: "",
      weight_kg: "",
      rate: "",
      note: "",
    };
  }

  /** Total KG = bales x kg per bale. Derived, never typed: a total and
   *  its parts that can disagree is a class of bug, not a feature. */
  function totalKg(row) {
    const pieces = Number(row.pieces || 0);
    const per = Number(row.weight_kg || 0);
    if (!pieces || !per) return "";
    return String(Math.round(pieces * per * 1000) / 1000);
  }

  /** Amount in paise, exactly. Quantity is not money, so it may be a
   *  Number; the rate and the product are handled as paise. */
  function amountPaise(row) {
    const kg = Number(totalKg(row) || 0);
    if (!kg || !row.rate) return 0n;
    const ratePaise = Money.toPaise(row.rate);
    return (BigInt(Math.round(kg * 1000)) * ratePaise) / 1000n;
  }

  function renderRows() {
    const body = $("rows");
    body.replaceChildren();

    rows.forEach((row, index) => {
      const tr = body.insertRow();
      tr.insertCell().outerHTML = `<td class="num derived">${index + 1}</td>`;

      const itemCell = tr.insertCell();
      const wrap = el("div", "field");
      const input = el("input");
      input.type = "text";
      input.placeholder = "Code or description…";
      input.value = row.code ? `${row.code} · ${row.brand || "—"}` : "";
      const list = el("div", "combo-list");
      list.hidden = true;
      wrap.append(input, list);
      itemCell.append(wrap);

      combo(input, list, {
        fetchItems: async (query) => {
          const found = await api(
            `/control/items?q=${encodeURIComponent(query)}&kind=${kind}`,
          );
          return found.items;
        },
        render: (item) => {
          const node = el("div");
          node.append(
            el("span", "code", item.code),
            el("span", "brand", item.brand || "—"),
            el("span", "desc", item.description || ""),
            el("span", "stock", `${item.on_hand} on hand`),
          );
          return node;
        },
        onClear: () => {
          row.product_id = null;
          row.note = "";
        },
        onPick: (item) => {
          row.product_id = item.product_id;
          row.code = item.code;
          row.brand = item.brand;
          row.description = item.description;
          // The rate is a suggestion, not an answer — filled only when
          // the row is empty so it can never overwrite a typed price.
          if (!row.rate && item.last_rate) row.rate = item.last_rate;
          row.note =
            kind === "sale" && Number(item.on_hand) <= 0
              ? `nothing on hand — this will go negative`
              : "";
          renderRows();
          focusCell(index, "pieces");
        },
      });

      const numeric = (key, placeholder) => {
        const cell = tr.insertCell();
        cell.className = "num";
        const field = el("input");
        field.type = "text";
        field.inputMode = "decimal";
        field.value = row[key];
        field.placeholder = placeholder;
        field.dataset.row = String(index);
        field.dataset.key = key;
        field.addEventListener("input", () => {
          row[key] = field.value;
          repaintDerived();
        });
        field.addEventListener("keydown", (event) => {
          if (event.key === "Enter" && index === rows.length - 1) {
            event.preventDefault();
            addRow();
          }
        });
        cell.append(field);
      };

      numeric("pieces", "bales");
      numeric("weight_kg", "kg");

      const kgCell = tr.insertCell();
      kgCell.className = "num derived";
      kgCell.dataset.derivedKg = String(index);
      kgCell.textContent = totalKg(row) || "—";

      numeric("rate", "0.00");

      const amountCell = tr.insertCell();
      amountCell.className = "num derived";
      amountCell.dataset.derivedAmount = String(index);
      amountCell.textContent = Money.format(Money.fromPaise(amountPaise(row)));

      const dropCell = tr.insertCell();
      const drop = el("button", "drop", "×");
      drop.type = "button";
      drop.title = "Remove this line";
      drop.addEventListener("click", () => {
        rows.splice(index, 1);
        if (!rows.length) rows.push(blankRow());
        renderRows();
      });
      dropCell.append(drop);

      if (row.note) {
        const noteRow = body.insertRow();
        const cell = noteRow.insertCell();
        cell.colSpan = 8;
        cell.className = "line-note";
        cell.textContent = `⚠ ${row.note}`;
      }
    });

    repaintDerived();
  }

  /** Recompute without rebuilding: rebuilding steals focus mid-typing. */
  function repaintDerived() {
    rows.forEach((row, index) => {
      const kgCell = document.querySelector(`[data-derived-kg="${index}"]`);
      const amountCell = document.querySelector(`[data-derived-amount="${index}"]`);
      if (kgCell) kgCell.textContent = totalKg(row) || "—";
      if (amountCell) {
        amountCell.textContent = Money.format(Money.fromPaise(amountPaise(row)));
      }
    });
    renderTotals();
    saveDraft();
  }

  function addRow() {
    rows.push(blankRow());
    renderRows();
    const inputs = $("rows").querySelectorAll("input[type=text]");
    const last = inputs[inputs.length - 5];
    if (last) last.focus();
  }

  function focusCell(index, key) {
    const field = document.querySelector(`input[data-row="${index}"][data-key="${key}"]`);
    if (field) field.focus();
  }

  // ---------------------------------------------------------- charges

  let charges = [];

  function renderCharges() {
    const host = $("charge-rows");
    host.replaceChildren();
    charges.forEach((charge, index) => {
      const row = el("div", "charge-row");
      const label = el("input");
      label.placeholder = "GST, packing…";
      label.value = charge.label;
      label.addEventListener("input", () => {
        charge.label = label.value;
        saveDraft();
      });
      const amount = el("input");
      amount.className = "num";
      amount.inputMode = "decimal";
      amount.placeholder = "0";
      amount.value = charge.amount;
      amount.addEventListener("input", () => {
        charge.amount = amount.value;
        renderTotals();
        saveDraft();
      });
      const drop = el("button", "drop", "×");
      drop.type = "button";
      drop.addEventListener("click", () => {
        charges.splice(index, 1);
        renderCharges();
        renderTotals();
      });
      row.append(label, amount, drop);
      host.append(row);
    });
  }

  // ----------------------------------------------------------- totals

  function renderTotals() {
    const subtotal = rows.reduce((total, row) => total + amountPaise(row), 0n);
    const chargeTotal = charges.reduce(
      (total, charge) => total + (charge.amount ? Money.toPaise(charge.amount) : 0n),
      0n,
    );
    const freight = $("freight").value ? Money.toPaise($("freight").value) : 0n;
    const discount = $("discount").value ? Money.toPaise($("discount").value) : 0n;
    const grand = subtotal + chargeTotal + freight - discount;
    const paid = kind === "sale" && $("paid-now").value ? Money.toPaise($("paid-now").value) : 0n;

    const line = (label, value, className) => {
      const node = el("div", className);
      node.append(el("span", null, label), el("b", null, Money.format(Money.fromPaise(value))));
      return node;
    };

    const host = $("totals");
    host.replaceChildren(line("Subtotal", subtotal));
    charges.forEach((charge) => {
      if (charge.amount) {
        host.append(line(charge.label || "Charge", Money.toPaise(charge.amount)));
      }
    });
    if (freight) host.append(line("Freight", freight));
    if (discount) host.append(line("Discount", -discount));
    host.append(line("Total", grand, "grand"));
    if (paid) {
      host.append(line("Paid now", paid));
      host.append(line("Balance", grand - paid, "balance"));
    }
  }

  // ----------------------------------------------------------- drafts

  /** A half-entered bill survives a closed laptop. Only what was typed
   *  — never a server figure, so a stale draft cannot show a stale
   *  balance — and never the paid amount, which is the one field where
   *  being quietly wrong costs money. */
  function saveDraft() {
    if (!draftKey) return;
    const payload = {
      at: Date.now(),
      kind,
      party: $("party").value,
      invoice: $("invoice").value,
      date: $("entry-date").value,
      rows,
      charges,
      freight: $("freight").value,
      discount: $("discount").value,
    };
    try {
      localStorage.setItem(DRAFT_STORE, JSON.stringify(payload));
    } catch {
      /* storage full or blocked; a draft is a convenience, not a record */
    }
  }

  function readDraft() {
    try {
      const raw = localStorage.getItem(DRAFT_STORE);
      if (!raw) return null;
      const draft = JSON.parse(raw);
      const age = (Date.now() - draft.at) / 86400000;
      if (age > DRAFT_TTL_DAYS) {
        localStorage.removeItem(DRAFT_STORE);
        return null;
      }
      return draft;
    } catch {
      return null;
    }
  }

  function clearDraft() {
    localStorage.removeItem(DRAFT_STORE);
    $("draft-bar").hidden = true;
  }

  function offerDraft() {
    const draft = readDraft();
    if (!draft || !draft.rows?.some((row) => row.product_id)) return;
    const when = new Date(draft.at).toLocaleString();
    $("draft-label").textContent =
      `Unsaved ${draft.kind} from ${when} — ${draft.rows.filter((r) => r.product_id).length} line(s).`;
    $("draft-bar").hidden = false;
    $("draft-restore").onclick = () => {
      kind = draft.kind;
      setKind(kind);
      $("party").value = draft.party || "";
      $("invoice").value = draft.invoice || "";
      $("entry-date").value = draft.date || "";
      rows = draft.rows.length ? draft.rows : [blankRow()];
      charges = draft.charges || [];
      $("freight").value = draft.freight || "";
      $("discount").value = draft.discount || "";
      renderRows();
      renderCharges();
      renderTotals();
      $("draft-bar").hidden = true;
    };
    $("draft-discard").onclick = clearDraft;
  }

  // ------------------------------------------------------------- mode

  function setKind(next) {
    kind = next;
    document.querySelectorAll("#nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.page === kind);
    });
    const sale = kind === "sale";
    $("party-label").textContent = sale ? "Customer" : "Supplier";
    $("invoice-field").hidden = sale;
    $("payment-field").hidden = !sale;
    $("paid-block").hidden = !sale;
    $("date-label").textContent = sale ? "Sale date" : "Invoice date";
    $("save").textContent = sale ? "Save sale" : "Save bill";
  }

  function resetForm() {
    rows = [blankRow()];
    charges = [];
    ["party", "invoice", "freight", "discount", "paid-now"].forEach((id) => ($(id).value = ""));
    $("party-note").textContent = "";
    $("invoice-note").textContent = "";
    $("entry-date").value = new Date().toISOString().slice(0, 10);
    draftKey = crypto.randomUUID();
    renderRows();
    renderCharges();
    renderTotals();
  }

  // ------------------------------------------------------------- save

  async function save(event) {
    event.preventDefault();
    const chosen = rows.filter((row) => row.product_id);
    if (!chosen.length) return banner("Add at least one line.");
    if (!$("party").value.trim()) return banner("Pick a party first.");

    const payload = {
      lines: chosen.map((row) => ({
        code: row.code,
        brand: row.brand,
        qty: totalKg(row),
        rate: row.rate || "0",
        pieces: row.pieces || null,
        weight_kg: row.weight_kg || null,
        description: row.description || null,
      })),
      charges: Object.fromEntries(
        charges.filter((c) => c.label && c.amount).map((c) => [c.label, c.amount]),
      ),
      freight: $("freight").value || "0",
      discount: $("discount").value || "0",
    };

    $("save").disabled = true;
    try {
      let result;
      if (kind === "purchase") {
        result = await api("/control/purchases", {
          method: "POST",
          body: JSON.stringify({
            ...payload,
            supplier: $("party").value.trim(),
            invoice_no: $("invoice").value.trim(),
            invoice_date: $("entry-date").value,
          }),
        });
        banner(
          result.already_existed
            ? `That bill was already recorded — showing the one that exists (${result.grand_total}).`
            : `Saved: ${result.invoice_no}, ${result.grand_total}.`,
          true,
        );
      } else {
        result = await api("/control/sales", {
          method: "POST",
          body: JSON.stringify({
            ...payload,
            customer: $("party").value.trim(),
            payment_type: $("payment-type").value,
            paid_now: $("paid-now").value || "0",
            paid_via: $("paid-via").value,
            sale_date: $("entry-date").value,
            // Minted with the form. A slow connection and an impatient
            // thumb is the case a terminal never has.
            idempotency_key: draftKey,
          }),
        });
        banner(
          result.already_existed
            ? "That sale was already recorded."
            : `Saved: ${result.grand_total}, ${result.customer} now owes ${result.outstanding_after}.`,
          true,
        );
      }
      clearDraft();
      resetForm();
    } catch (exc) {
      banner(exc.message);
    } finally {
      $("save").disabled = false;
    }
  }

  // ------------------------------------------------------------- boot

  async function afterSignIn(name) {
    $("signin").hidden = true;
    $("app").hidden = false;
    $("whoami").textContent = name;
    resetForm();
    offerDraft();
    try {
      const health = await api("/control/health");
      $("books-health").textContent = health.ok
        ? "books balance"
        : "⚠ books do not balance";
      $("books-health").style.color = health.ok ? "" : "var(--danger)";
    } catch {
      /* the header is a courtesy; a failed check must not block entry */
    }
  }

  $("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("signin-error").hidden = true;
    try {
      const result = await api("/auth/control/login", {
        method: "POST",
        body: JSON.stringify({
          email: $("email").value.trim(),
          password: $("password").value,
        }),
      });
      token = result.token;
      $("password").value = "";
      await afterSignIn(result.full_name);
    } catch (exc) {
      $("signin-error").textContent = exc.message;
      $("signin-error").hidden = false;
    }
  });

  $("signout").addEventListener("click", () => {
    token = null;
    $("app").hidden = true;
    $("signin").hidden = false;
  });

  document.querySelectorAll("#nav button").forEach((button) => {
    button.addEventListener("click", () => {
      setKind(button.dataset.page);
      resetForm();
    });
  });

  $("add-charge").addEventListener("click", () => {
    charges.push({ label: "", amount: "" });
    renderCharges();
  });
  ["freight", "discount", "paid-now"].forEach((id) => {
    $(id).addEventListener("input", () => {
      renderTotals();
      saveDraft();
    });
  });
  $("discard").addEventListener("click", () => {
    clearDraft();
    resetForm();
  });
  $("entry").addEventListener("submit", save);

  combo($("party"), $("party-list"), {
    fetchItems: async (query) => {
      const found = await api(
        `/control/parties?q=${encodeURIComponent(query)}&kind=${
          kind === "sale" ? "customer" : "supplier"
        }`,
      );
      return found.items;
    },
    render: (item) => {
      const node = el("div");
      node.append(
        el("span", "code", item.name),
        el("span", "stock", `${Money.format(item.outstanding)} open`),
      );
      return node;
    },
    onClear: () => ($("party-note").textContent = ""),
    onPick: (item) => {
      $("party").value = item.name;
      $("party-note").textContent = `${Money.format(item.outstanding)} outstanding`;
    },
  });

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!$("app").hidden) $("entry").requestSubmit();
    }
  });

  setKind("purchase");
})();
