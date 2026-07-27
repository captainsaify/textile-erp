"""The typed registry behind the `settings` command --
docs/08_WhatsApp.md #settings ("each key has a typed validator ...
rejected with the expected type/range on mismatch, never silently
coerced").

This is the single source of every tunable's default. Services read
through SettingsRepository rather than holding their own constant, so a
default can't drift between the registry and the code that uses it.

**Only keys something actually reads live here.** A key that can be set
but that no code consults is a placeholder pretending to be a feature
(CLAUDE.md rule 4), and worse than absent -- a partner would change it
and reasonably expect behaviour to change. The remaining keys named in
docs/ (`backup_retention_days`, `report_link_expiry_days`,
`undo_window_hours`, `week_start_day`, `large_adjustment_value_threshold`,
`low_stock_check_hour`, the OCR thresholds) arrive with the features
that read them.

`base_currency` and `timezone` are deliberately *not* here: they are
columns on `organizations` and that stays the one source of truth --
`business_today()` reads the column, so a divergent settings row would
silently date entries wrong.
"""

from __future__ import annotations

import dataclasses
import decimal
from collections.abc import Callable
from typing import Literal

SettingValue = int | str | float

Kind = Literal["int", "money", "percent"]


class SettingError(ValueError):
    """Raised with user-facing copy naming the expected type/range."""


@dataclasses.dataclass(frozen=True)
class SettingSpec:
    key: str
    kind: Kind
    default: decimal.Decimal | int
    description: str
    #: inclusive bounds; None means unbounded on that side
    minimum: decimal.Decimal | int | None = None
    maximum: decimal.Decimal | int | None = None

    def parse(self, raw: str) -> SettingValue:
        """Text from WhatsApp -> a JSON-storable value, or SettingError."""
        text = raw.strip()
        if self.kind == "int":
            try:
                value: decimal.Decimal | int = int(text)
            except ValueError:
                raise SettingError(f"'{self.key}' expects a whole number.") from None
        else:
            try:
                value = decimal.Decimal(text)
            except decimal.InvalidOperation:
                raise SettingError(f"'{self.key}' expects a number.") from None
        self._check_range(value)
        # Decimal isn't JSON-serialisable; store money/percent as a string
        # so no precision is lost round-tripping through JSONB.
        return int(value) if self.kind == "int" else str(value)

    def _check_range(self, value: decimal.Decimal | int) -> None:
        if self.minimum is not None and value < self.minimum:
            raise SettingError(f"'{self.key}' must be at least {self.minimum}.")
        if self.maximum is not None and value > self.maximum:
            raise SettingError(f"'{self.key}' must be at most {self.maximum}.")

    def coerce(self, stored: object) -> decimal.Decimal | int:
        """Stored JSONB -> typed value. A row hand-edited to something
        unusable falls back to the default rather than raising: a bad
        settings row must not take down an unrelated command."""
        if isinstance(stored, bool) or not isinstance(stored, int | float | str):
            return self.default
        try:
            if self.kind == "int":
                return int(stored)
            return decimal.Decimal(str(stored))
        except (ValueError, decimal.InvalidOperation):
            return self.default

    def display(self, value: decimal.Decimal | int) -> str:
        if self.kind == "money":
            return f"₹{value}"
        if self.kind == "percent":
            return f"{value}%"
        return str(value)


_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="capital_withdrawal_dual_approval_threshold",
        kind="money",
        default=decimal.Decimal("25000"),
        minimum=decimal.Decimal("0"),
        description="Withdrawals at or above this need a second partner's approval.",
    ),
    SettingSpec(
        key="withdrawal_approval_timeout_hours",
        kind="int",
        default=48,
        minimum=1,
        description="How long a pending withdrawal waits before it expires.",
    ),
    SettingSpec(
        key="slow_moving_days",
        kind="int",
        default=60,
        minimum=1,
        description="Days without a sale before stock counts as slow moving.",
    ),
    SettingSpec(
        key="purchase_total_mismatch_tolerance",
        kind="money",
        default=decimal.Decimal("1.00"),
        minimum=decimal.Decimal("0"),
        description="Rounding slack before an invoice total mismatch is queried.",
    ),
    SettingSpec(
        key="duplicate_invoice_window_days",
        kind="int",
        default=3,
        minimum=0,
        description="How many days either side of an invoice date to scan for duplicates.",
    ),
    SettingSpec(
        key="below_cost_sale_tolerance_percent",
        kind="percent",
        default=decimal.Decimal("0"),
        minimum=decimal.Decimal("0"),
        maximum=decimal.Decimal("100"),
        description="Headroom below average cost before a sale warns.",
    ),
    SettingSpec(
        key="undo_window_hours",
        kind="int",
        default=24,
        minimum=1,
        description="How long after an entry it can still be undone.",
    ),
    SettingSpec(
        key="sale_dedup_window_minutes",
        kind="int",
        default=10,
        minimum=1,
        description="Window in which an identical repeated sale is treated as a duplicate.",
    ),
)

REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}


def spec_for(key: str) -> SettingSpec:
    spec = REGISTRY.get(key.strip().lower())
    if spec is None:
        raise SettingError(f"'{key.strip()}' is not a setting. Send 'settings' to see the list.")
    return spec


def closest_key(word: str) -> str | None:
    import difflib

    matches = difflib.get_close_matches(word.strip().lower(), list(REGISTRY), n=1, cutoff=0.6)
    return matches[0] if matches else None


#: convenience for callers that want the default without a DB round trip
def default_for(key: str) -> decimal.Decimal | int:
    return spec_for(key).default


ValidatorFn = Callable[[str], SettingValue]
