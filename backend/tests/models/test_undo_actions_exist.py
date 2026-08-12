"""Every undoable action must be one a service actually writes.

`UNDOABLE_ACTIONS` listed `sale.confirmed` while `sales_service` has
always written `sale.created`. Nothing failed, nothing logged: `undo`
simply looked for an audit row that could never exist and answered
"undoable action <ref> not found". No sale was undoable for the entire
life of the feature.

A string on one side and a string on the other, with no compiler between
them, is exactly what a test is for.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.repositories.audit_repository import UNDOABLE_ACTIONS

SERVICES = Path(__file__).resolve().parents[2] / "services"
_ACTION = re.compile(r'action=f?"([a-z_]+\.[a-z_{}.]+)"')


def _written_actions() -> set[str]:
    written: set[str] = set()
    for path in SERVICES.glob("*.py"):
        for match in _ACTION.findall(path.read_text()):
            if "{" in match:
                # f-string like capital.{entry_type.value} -- record the
                # prefix so its members still count as written
                written.add(match.split(".{")[0])
            else:
                written.add(match)
    return written


def test_every_undoable_action_is_one_a_service_writes() -> None:
    written = _written_actions()
    missing = {
        action
        for action in UNDOABLE_ACTIONS
        if action not in written and action.rsplit(".", 1)[0] not in written
    }

    assert not missing, (
        f"undo looks for actions nothing writes: {sorted(missing)}. "
        "Either the service renamed its audit action or this list drifted; "
        "either way undo silently stopped working for those."
    )


def test_the_entity_types_match_too() -> None:
    """A right action name against the wrong table finds nothing just as
    quietly."""
    expected = {
        "purchase.confirmed": "purchase_headers",
        "sale.created": "sales_headers",
        "expense.created": "expenses",
        "income.created": "income",
    }
    for action, table in expected.items():
        assert UNDOABLE_ACTIONS[action] == table


def test_the_dispatch_table_matches_the_registry() -> None:
    """Three places have to agree on each name: what the service writes,
    what `find_action` looks for, and which handler runs. Two of them
    agreeing is enough to pass a test and still be broken -- which is
    exactly what happened."""
    import inspect

    from backend.services import undo_service

    # `undo` is now a thin transaction wrapper around
    # `undo_in_transaction` (the admin CLI needs to own the transaction
    # so it can roll back on a failed reconciliation). The dispatch table
    # moved with the body, so read it where it actually lives -- reading
    # the wrapper would pass vacuously the day a handler goes missing.
    source = inspect.getsource(undo_service.UndoService.undo_in_transaction)
    for action in ("purchase.confirmed", "sale.created", "expense.created", "income.created"):
        assert f'"{action}"' in source, (
            f"{action} is undoable but has no handler; undo would raise KeyError "
            "after finding the entry"
        )
