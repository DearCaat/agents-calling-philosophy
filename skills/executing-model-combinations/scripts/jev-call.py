#!/usr/bin/env python3
"""Compatibility entry for jev batch calls: runs tool-call.py jev (served at /tool/jev/systemone).

Same options as before; --api is still required and must be jev. Input and output
formats are those of tool-call.py jev: one JSON object per line with "id" and "state",
"questions", optional "model" and "images"; results are appended to --out, and reruns
skip ids that already succeeded. Use tool-call.py for the other tools.
"""
import argparse
import importlib.util
import os
import sys


def load_tool_call():
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'tool-call.py')
    spec = importlib.util.spec_from_file_location('tool_call', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--api', required=True, help='must be jev')
    api, rest = parser.parse_known_args(argv)
    if api.api != 'jev':
        print('jev-call: --api must be jev; use tool-call.py for other tools', file=sys.stderr)
        return 2
    return load_tool_call().main(['--api', 'tool', 'jev'] + rest)


# ── library compatibility layer ────────────────────────────────────────────────
# The CLI entry above delegates to tool-call.py. Callers that import this file as a
# module (web-mcp's jev.py) expect the library surface: resolve_route / make_opener /
# run_one. Provide it here so both the CLI and importers keep working.
_TC = None


def _tool_call():
    global _TC
    if _TC is None:
        _TC = load_tool_call()
    return _TC


def resolve_route(api):
    """Return (base, key, state) for an api_id; base ends without a trailing slash."""
    import urllib.parse
    tc = _tool_call()
    local, credentials_file = tc.data_paths()
    path = os.path.join(local, 'api-routes.tsv')
    with open(path, encoding='utf-8') as f:
        rows = [line.rstrip('\r\n').split('\t') for line in f
                if line.strip() and not line.lstrip().startswith('#')]
    matches = [r for r in rows if r and r[0] == api]
    if len(matches) != 1:
        raise RuntimeError('unregistered or duplicate API route: ' + api)
    row = matches[0]
    if len(row) < 7:
        raise RuntimeError('route registry row is incomplete for API: ' + api)
    _, base, wire, credential_ref, _, auth, state = row[:7]
    if base == '-' or auth != 'bearer' or state == 'unavailable':
        raise RuntimeError('route %s has no usable bearer endpoint (state=%s)' % (api, state))
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise RuntimeError('route %s has an invalid base URL' % api)
    key = tc.read_credential(credential_ref, credentials_file)
    return base.rstrip('/'), key, state


def make_opener():
    return _tool_call().make_opener()


def run_one(item, args, ctx):
    """POST one judgment body to ctx['base'] + '/systemone'; returns (record, raw)."""
    import json as _json
    import time as _time
    started = _time.monotonic()
    record = {'id': item.get('id'), 'ok': False, 'answers': None, 'usage': None,
              'model': item.get('model'), 'status': None, 'attempts': 0,
              'latency_s': None, 'error': None}
    url = ctx['base'] + '/systemone'
    attempts = getattr(args, 'attempts', 4) or 1
    timeout = getattr(args, 'timeout', 160.0) or 160.0
    for attempt in range(1, attempts + 1):
        record['attempts'] = attempt
        payload = _json.dumps(item['body'], ensure_ascii=False).encode('utf-8')
        status, headers, raw, transport_error = _tool_call().request_once(
            ctx['opener'], url, ctx['headers'], payload, 'POST', timeout)
        record['status'] = status
        if transport_error is not None:
            record['error'] = ('timeout: ' if status == 'timeout' else 'network error: ') + transport_error
            break
        if isinstance(status, int) and 200 <= status < 300:
            try:
                response = _json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                record['error'] = 'HTTP %d: response is not valid JSON' % status
            else:
                if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
                    record['error'] = 'HTTP %d: response is missing an answers object' % status
                else:
                    record.update(ok=True, answers=response['answers'], usage=response.get('usage'),
                                  model=response.get('model') or item.get('model'), error=None)
                    record['_observed_model'] = response.get('model')
                    break
        else:
            record['error'] = 'HTTP %s: %s' % (status, (raw or b'')[:200])
        if attempt < attempts:
            _time.sleep(1.5 * attempt)
    record['latency_s'] = round(_time.monotonic() - started, 2)
    return record, None


if __name__ == '__main__':
    sys.exit(main())
