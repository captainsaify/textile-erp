"""`settings` -- docs/08_WhatsApp.md #settings.

`settings` lists every tunable with its current value; `settings <key>
<value>` changes one. Validation is per-key and typed, and a bad value
is rejected naming the expected type/range rather than coerced.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.core.settings_registry import SettingError, closest_key, spec_for
from backend.repositories.settings_repository import SettingsRepository
from backend.services.audit_service import AuditService

USAGE = "Usage: settings   (list)   or   settings <key> <value>"


async def handle_settings(args: str, ctx: RequestContext) -> CommandResult:
    text = args.strip()
    if not text:
        return await _list(ctx)

    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        # `settings <key>` -- show just that one, rather than erroring on
        # a half-typed command
        return await _show_one(parts[0], ctx)

    key_raw, value_raw = parts
    try:
        spec = spec_for(key_raw)
        parsed = spec.parse(value_raw)
    except SettingError as exc:
        suggestion = closest_key(key_raw)
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        return CommandResult(reply=f"{exc}{hint}")

    async with ctx.session_factory() as session:
        repo = SettingsRepository(session)
        async with session.begin():
            before = await repo.get(ctx.user.org_id, spec.key)
            setting_id = await repo.set(ctx.user.org_id, spec.key, parsed, ctx.user.id)
            await AuditService(session).record(
                ctx.user.org_id,
                ctx.user.id,
                action="settings.updated",
                entity_type="settings",
                entity_id=setting_id,
                before_state={"key": spec.key, "value": str(before)},
                after_state={"key": spec.key, "value": str(parsed)},
                whatsapp_message_id=ctx.message_id,
            )
        after = await repo.get(ctx.user.org_id, spec.key)

    return CommandResult(
        reply=(f"✅ {spec.key}: {spec.display(before)} → {spec.display(after)}\n{spec.description}")
    )


async def _list(ctx: RequestContext) -> CommandResult:
    async with ctx.session_factory() as session:
        values = await SettingsRepository(session).all_values(ctx.user.org_id)

    lines = ["⚙️ Settings:"]
    for spec, value, customised in values:
        marker = "" if customised else " (default)"
        lines.append(f"• {spec.key}: {spec.display(value)}{marker}")
    lines.append("Change one with: settings <key> <value>")
    return CommandResult(reply="\n".join(lines))


async def _show_one(key_raw: str, ctx: RequestContext) -> CommandResult:
    try:
        spec = spec_for(key_raw)
    except SettingError as exc:
        suggestion = closest_key(key_raw)
        hint = f" Did you mean '{suggestion}'?" if suggestion else ""
        return CommandResult(reply=f"{exc}{hint}")

    async with ctx.session_factory() as session:
        value = await SettingsRepository(session).get(ctx.user.org_id, spec.key)

    bounds = []
    if spec.minimum is not None:
        bounds.append(f"min {spec.minimum}")
    if spec.maximum is not None:
        bounds.append(f"max {spec.maximum}")
    limits = f" ({', '.join(bounds)})" if bounds else ""
    return CommandResult(
        reply=(
            f"⚙️ {spec.key}: {spec.display(value)}\n{spec.description}\n"
            f"Default {spec.display(spec.default)}{limits}. "
            f"Change with: settings {spec.key} <value>"
        )
    )
