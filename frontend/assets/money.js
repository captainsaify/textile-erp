/* Money handling for the dashboard -- docs/21_WebDashboard.md §5.
 *
 * The one non-obvious constraint in this whole frontend: **money must
 * never become a JavaScript number.** The API sends it as a string
 * because the database stores NUMERIC and every Python path uses
 * Decimal; parseFloat here would put a 53-bit binary float back in the
 * middle of that and quietly betray the discipline the rest of the
 * system keeps.
 *
 * So amounts are handled as integer paise (a BigInt), which is exact,
 * and formatted with Indian digit grouping -- the same convention every
 * WhatsApp reply uses, so the two surfaces read as one system.
 */

const Money = (() => {
  /** "4092000.00" -> 409200000n paise. Exact, no float anywhere. */
  function toPaise(text) {
    const raw = String(text ?? "0").trim();
    const negative = raw.startsWith("-");
    const [whole, fraction = ""] = raw.replace(/^[-+]/, "").split(".");
    const paise = `${whole || "0"}${(fraction + "00").slice(0, 2)}`;
    const value = BigInt(paise.replace(/\D/g, "") || "0");
    return negative ? -value : value;
  }

  /** Indian grouping: 1,23,456.78 -- lakh/crore, not thousands. */
  function group(digits) {
    if (digits.length <= 3) return digits;
    const head = digits.slice(0, -3);
    const tail = digits.slice(-3);
    return `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}`;
  }

  function format(text, { sign = false } = {}) {
    const paise = toPaise(text);
    const negative = paise < 0n;
    const absolute = negative ? -paise : paise;
    const rupees = (absolute / 100n).toString();
    const remainder = (absolute % 100n).toString().padStart(2, "0");
    const prefix = negative ? "-₹" : sign ? "+₹" : "₹";
    return `${prefix}${group(rupees)}.${remainder}`;
  }

  /** Compact form for chart axes: ₹40.9L, ₹1.2Cr. Presentation only. */
  function compact(text) {
    const paise = toPaise(text);
    const negative = paise < 0n;
    const rupees = Number((negative ? -paise : paise) / 100n);
    const sign = negative ? "-" : "";
    if (rupees >= 10000000) return `${sign}₹${(rupees / 10000000).toFixed(1)}Cr`;
    if (rupees >= 100000) return `${sign}₹${(rupees / 100000).toFixed(1)}L`;
    if (rupees >= 1000) return `${sign}₹${(rupees / 1000).toFixed(1)}k`;
    return `${sign}₹${rupees}`;
  }

  /* Charts need a magnitude to scale bars by. This is the one place a
   * float is acceptable -- it decides pixel heights, never a displayed
   * figure, and every label still comes from the exact string. */
  function toPlotValue(text) {
    return Number(toPaise(text)) / 100;
  }

  function isNegative(text) {
    return toPaise(text) < 0n;
  }

  function isZero(text) {
    return toPaise(text) === 0n;
  }

  /** paise -> the same string shape the API sends, so a total can be
   * passed anywhere an amount from the server can. */
  function fromPaise(paise) {
    const negative = paise < 0n;
    const absolute = negative ? -paise : paise;
    const remainder = (absolute % 100n).toString().padStart(2, "0");
    return `${negative ? "-" : ""}${absolute / 100n}.${remainder}`;
  }

  /** Exact total of money strings. Summing with + on Numbers is how a
   * capital figure ends up a paisa out from the rows above it. */
  function sum(values) {
    return fromPaise(values.reduce((total, value) => total + toPaise(value), 0n));
  }

  return { toPaise, fromPaise, format, compact, toPlotValue, isNegative, isZero, sum };
})();
