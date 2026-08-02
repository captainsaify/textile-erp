"""Celery Beat schedule -- docs/11_BackgroundWorkers.md §3.

Times are the org-local ones the doc specifies. Beat itself runs in UTC
(one process, many orgs in the multi-tenant future), so each task
resolves the org's own calendar via `business_today()` when it needs a
date -- the schedule decides *when to fire*, never what "today" means.
"""

from __future__ import annotations

from typing import Any

from celery.schedules import crontab

CELERYBEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    # Every minute: near-real-time for a group chat without hooking the
    # relay into any command's critical path (docs/22 §3).
    "group-broadcast-sweep": {
        "task": "group_broadcast_sweep",
        "schedule": crontab(minute="*"),
    },
    # Every minute, for the same reason: a partner hears about a sale
    # while it can still be undone, and no confirmation ever waits on
    # the fan-out (docs/22 §7).
    "partner-notice-sweep": {
        "task": "partner_notice_sweep",
        "schedule": crontab(minute="*"),
    },
    "low-stock-scan": {
        "task": "low_stock_scan",
        "schedule": crontab(hour="6", minute="0"),
        "options": {"queue": "scheduled"},
    },
    "nightly-backup": {
        "task": "nightly_backup",
        "schedule": crontab(hour="2", minute="0"),
        "options": {"queue": "backup"},
    },
    "inventory-reconciliation": {
        "task": "inventory_reconciliation",
        "schedule": crontab(hour="2", minute="30"),
        "options": {"queue": "scheduled"},
    },
    "ledger-reconciliation": {
        "task": "ledger_reconciliation",
        "schedule": crontab(hour="2", minute="45"),
        "options": {"queue": "scheduled"},
    },
    "session-expiry-sweep": {
        "task": "session_expiry_sweep",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "scheduled"},
    },
    "withdrawal-approval-timeout-sweep": {
        "task": "withdrawal_approval_timeout_sweep",
        "schedule": crontab(minute="0"),
        "options": {"queue": "scheduled"},
    },
}
