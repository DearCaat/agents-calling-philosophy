"""Tool modules for tool-call.py; each one serves /tool/<NAME> on the CPA gateway.

A tool module defines NAME, DESCRIPTION, DEFAULT_CONCURRENCY, DEFAULT_TIMEOUT,
add_arguments(parser), prepare(item, args) -> dict (with "preview") and
run_one(client, prepared, args) -> record dict (ok, error, status, attempts, ...).
Optional: check_args(args), items_from_args(args) (None means the tool reads --in only).
Tool-specific rules stay in that tool's module.
"""
import importlib

NAMES = ('jev', 'vela', 'mineru', 'oxr')


class ToolError(Exception):
    """An input or argument refused before any request; the message is shown as is."""


def load(name):
    if name not in NAMES:
        raise ToolError('unknown tool: %s (known: %s)' % (name, ', '.join(NAMES)))
    return importlib.import_module('tools.' + name)
