"""Shared period grammar for `summary` and `profit`
(docs/08_WhatsApp.md #summary, #profit) -- both accept
`today|week|month|year|<DD-MM-YYYY> to <DD-MM-YYYY>`, so one parser
keeps their period semantics from drifting apart.
"""

from __future__ import annotations

import dataclasses
import datetime
import re

from backend.api.interactive import Choice, ListMenu, Section
from backend.core.exceptions import ValidationError

_RANGE = re.compile(r"^(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})$", re.IGNORECASE)

USAGE = "Say 'today', 'week', 'month', 'year', or a range like '01-07-2026 to 25-07-2026'."


@dataclasses.dataclass(frozen=True)
class Period:
    start: datetime.date
    end: datetime.date
    label: str


def _parse_date(raw: str, usage: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(raw, "%d-%m-%Y").date()
    except ValueError:
        raise ValidationError(f"'{raw}' is not a valid DD-MM-YYYY date. {usage}") from None


def parse_period(args: str, today: datetime.date, *, usage: str = USAGE) -> Period:
    """`today` must be the org's business-local date (business_today()),
    never server UTC-today -- docs/02_Database.md §8."""
    token = args.strip()

    if not token or token.lower() == "today":
        return Period(today, today, "today")
    if token.lower() == "week":
        # week-to-date (Monday through today), not a trailing 7 days --
        # matches "month"/"year" both being to-date rather than trailing.
        start = today - datetime.timedelta(days=today.weekday())
        return Period(start, today, "this week")
    if token.lower() == "month":
        start = today.replace(day=1)
        return Period(start, today, f"{today.strftime('%b %Y')}, MTD")
    if token.lower() == "year":
        start = today.replace(month=1, day=1)
        return Period(start, today, f"{today.year}, YTD")

    match = _RANGE.match(token)
    if match:
        start = _parse_date(match.group(1), usage)
        end = _parse_date(match.group(2), usage)
        if start > end:
            raise ValidationError("The range's start date is after its end date.")
        return Period(start, end, f"{match.group(1)} to {match.group(2)}")

    raise ValidationError(usage)


def period_menu(command: str) -> ListMenu:
    """Offered when `summary`/`profit`/`export` arrive without a period.
    `Custom` can't be a row that answers itself -- a date range is free
    text -- so it prompts for one."""
    return ListMenu(
        body=f"Which period for {command}?",
        menu_label="Pick period",
        sections=(
            Section(
                title="Period",
                rows=(
                    Choice(id=f"{command} today", title="Today"),
                    Choice(id=f"{command} week", title="This week", description="Monday to today"),
                    Choice(id=f"{command} month", title="This month", description="1st to today"),
                    Choice(id=f"{command} year", title="This year", description="1 Jan to today"),
                    Choice(
                        id=f"{command} custom",
                        title="Custom range",
                        description="You'll type the dates",
                    ),
                ),
            ),
        ),
    )
