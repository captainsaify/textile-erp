"""Admin operations, callable from the CLI and from HTTP alike.

plan.md §3. These used to live inside Typer command functions, mixed in
with console printing and prompts, which meant Master Control could not
reach them without either duplicating the logic or importing a terminal.

The rule the split enforces: an operation returns a *result*, and the
caller decides how to say it. Nothing in here prints.
"""
