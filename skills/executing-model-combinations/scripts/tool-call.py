#!/usr/bin/env python3
"""Client for the CPA tool gateway (/tool/<name>): batch JSONL calls and read-only queries.

  tool-call.py [--api tool] <tool> --in FILE|- --out FILE [options]   batch
  tool-call.py [--api tool] <tool> --schema | --health | --list-models
  tool-call.py [--api tool] --list                                    enabled tools (GET /tool/)

Tools (jev, vela, mineru, oxr) are modules in tools/; this file holds no tool-specific fields
or rules. An input line is one JSON object with an "id" and the tool's own fields. Results
are appended to --out as each item finishes; on rerun, ids that already have ok=true there
are skipped. The route comes from --api in local/api-routes.tsv (wire cpa-tool). Requests
bypass environment proxies. Credentials are never printed.
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import socket
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import tools  # noqa: E402
from tools import ToolError  # noqa: E402

MAX_ERROR_CHARS = 600
DEFAULT_ATTEMPTS = 8
DEFAULT_STOP_AFTER = 10
READ_ONLY_TIMEOUT = 60.0


def die(msg):
    print('tool-call: ' + msg, file=sys.stderr)
    raise SystemExit(2)


def data_paths():
    script_dir = os.path.dirname(os.path.realpath(__file__))
    skill_dir = os.path.dirname(script_dir)
    plugin_dir = os.path.dirname(os.path.dirname(skill_dir))
    root = next((os.environ[k] for k in
                 ('AGENTS_DATA_ROOT', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA', 'GROK_PLUGIN_DATA')
                 if os.environ.get(k)), '')
    local = os.environ.get('AGENTS_LOCAL_ROOT') or (
        os.path.join(root, 'local') if root else os.path.join(skill_dir, 'references', 'local'))
    creds = os.environ.get('AGENTS_CREDENTIALS_FILE') or os.path.join(
        root or plugin_dir, 'private', 'credentials.env')
    return local, creds


def read_credential(ref, credentials_file):
    """Resolve a route credential without ever including its value in diagnostics."""
    if ref.startswith('file:'):
        path = os.path.expanduser(os.path.expandvars(ref[len('file:'):]))
        try:
            with open(path, encoding='utf-8') as f:
                value = f.read().strip()
        except OSError:
            die('credential file is not readable: ' + path)
        if not value:
            die('credential file is empty: ' + path)
        return value

    name = ref[len('env:'):] if ref.startswith('env:') else ref
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(credentials_file, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
                if m and m.group(1) == name:
                    value = m.group(2).strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in '\'"':
                        value = value[1:-1]
                    if value:
                        return value
    except OSError:
        die('credentials file is not readable: ' + credentials_file)
    die('credential is not defined for the selected route')


def resolve_route(api):
    """Return (base, key) for an api_id of wire cpa-tool; base ends with /tool."""
    local, credentials_file = data_paths()
    path = os.path.join(local, 'api-routes.tsv')
    try:
        with open(path, encoding='utf-8') as f:
            rows = [line.rstrip('\r\n').split('\t') for line in f
                    if line.strip() and not line.lstrip().startswith('#')]
    except OSError:
        die('route registry is not readable: ' + path)
    matches = [r for r in rows if r and r[0] == api]
    if len(matches) != 1:
        die('unregistered or duplicate API route: ' + api)
    row = matches[0]
    if len(row) < 7:
        die('route registry row is incomplete for API: ' + api)
    _, base, wire, credential_ref, _, auth, state = row[:7]
    if base == '-' or auth != 'bearer' or state == 'unavailable':
        die('route %s has no usable bearer endpoint (state=%s)' % (api, state))
    if wire != 'cpa-tool':
        die('route %s uses unsupported wire protocol: %s' % (api, wire))
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        die('route %s has an invalid base URL' % api)
    key = read_credential(credential_ref, credentials_file)
    return base.rstrip('/'), key


def parse_retry_after(headers):
    raw = headers.get('Retry-After') if headers else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except (ValueError, AttributeError):
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(headers, attempt):
    delay = parse_retry_after(headers)
    if delay is None:
        delay = min(2 ** (attempt - 1), 60)
    # Never retry before Retry-After; add a small positive jitter to spread clients.
    jitter = random.uniform(0.0, min(2.0, max(0.1, delay * 0.25)))
    return delay + jitter


def response_error(raw, key):
    text = raw[:MAX_ERROR_CHARS].decode('utf-8', 'replace').strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            value = obj.get('error') or obj.get('message') or obj.get('detail') or obj
            text = json.dumps(value, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        pass
    if key:
        text = text.replace(key, '[REDACTED]')
    return text[:MAX_ERROR_CHARS] or 'empty response body'


def is_model_warming(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return b'model_warming' in raw
    if isinstance(obj, dict):
        # Servers have used both a top-level code and an error object.
        return 'model_warming' in json.dumps(obj, ensure_ascii=False)
    return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_opener():
    # The gateway may sit on a private network; do not let ambient HTTP(S)_PROXY settings intercept it.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request_once(opener, url, headers, data, method, timeout, save_to=None):
    """One HTTP exchange: (status, headers, raw, transport_error).

    With save_to, a 2xx body is streamed into that file (through save_to + ".part") and raw is empty.
    """
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            if save_to is not None and 200 <= response.status < 300:
                part = save_to + '.part'
                with open(part, 'wb') as f:
                    shutil.copyfileobj(response, f)
                os.replace(part, save_to)
                return response.status, response.headers, b'', None
            return response.status, response.headers, response.read(), None
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except OSError:
            raw = b''
        return e.code, e.headers, raw, None
    except (socket.timeout, TimeoutError) as e:
        return 'timeout', None, b'', str(e) or 'request timed out'
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)) or 'timed out' in str(e.reason).lower():
            return 'timeout', None, b'', 'request timed out'
        return 'network_error', None, b'', str(e.reason)
    except OSError as e:
        if isinstance(e, (socket.timeout, TimeoutError)) or 'timed out' in str(e).lower():
            return 'timeout', None, b'', 'request timed out'
        return 'network_error', None, b'', str(e)


class Reply:
    """Outcome of one logical request after its retries."""

    def __init__(self):
        self.status = None  # HTTP status, or 'timeout' / 'network_error'
        self.headers = None
        self.raw = b''
        self.error = None  # set for transport failures and for non-2xx answers
        self.attempts = 0
        self.rate_limits = 0

    @property
    def ok(self):
        return self.error is None and isinstance(self.status, int) and 200 <= self.status < 300


class Client:
    def __init__(self, base, key, timeout, attempts):
        parsed = urllib.parse.urlparse(base)
        self.base = base  # .../tool, no trailing slash
        self.origin = '%s://%s' % (parsed.scheme, parsed.netloc)
        self.key = key
        self.timeout = timeout
        self.attempts = attempts
        self.opener = make_opener()

    def scrub(self, text):
        return text.replace(self.key, '[REDACTED]') if self.key else text

    def call(self, method, path, json_body=None, data=None, content_type=None, save_to=None,
             absolute=False, timeout=None, attempts=None):
        """Send one request. path starts with "/" and is relative to the tool base, or to the origin when absolute.

        429 and 503 model_warming are retried after Retry-After (or backoff). Transport errors and
        other statuses are never replayed: the gateway may still be working on the request.
        """
        url = (self.origin if absolute else self.base) + path
        headers = {'Authorization': 'Bearer ' + self.key}
        body = data
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        elif content_type:
            headers['Content-Type'] = content_type
        limit = attempts or self.attempts
        timeout = timeout or self.timeout
        reply = Reply()
        for attempt in range(1, limit + 1):
            reply.attempts = attempt
            status, resp_headers, raw, transport = request_once(
                self.opener, url, headers, body, method, timeout, save_to)
            reply.status, reply.headers, reply.raw = status, resp_headers, raw
            if transport is not None:
                reply.error = ('timeout: ' if status == 'timeout' else 'network error: ') + transport
                break
            if isinstance(status, int) and 200 <= status < 300:
                return reply
            if status == 429:
                reply.rate_limits += 1
                if attempt < limit:
                    time.sleep(retry_delay(resp_headers, attempt))
                    continue
            elif status == 503 and is_model_warming(raw) and attempt < limit:
                time.sleep(retry_delay(resp_headers, attempt))
                continue
            break
        if reply.error is None:
            reply.error = 'HTTP %s: %s' % (reply.status, response_error(reply.raw, self.key))
        return reply


def read_jsonl(path):
    try:
        source = sys.stdin if path == '-' else open(path, encoding='utf-8')
    except OSError as e:
        die('cannot read input: %s' % e)
    rows = []
    try:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                die('input line %d is not valid JSON' % line_no)
            rows.append(('input line %d' % line_no, value))
    finally:
        if source is not sys.stdin:
            source.close()
    return rows


def prepare_rows(module, rows, args):
    """Validate every row before any request is sent; a refusal stops the whole batch."""
    prepared, seen = [], set()
    for where, item in rows:
        if not isinstance(item, dict):
            die('%s must be a JSON object' % where)
        if item.get('id') is None or not str(item['id']):
            die('%s needs a nonempty "id"' % where)
        item_id = str(item['id'])
        if item_id in seen:
            die('duplicate id at %s: %s' % (where, item_id))
        seen.add(item_id)
        try:
            entry = module.prepare(item, args)
        except ToolError as e:
            die('%s: %s' % (where, e))
        entry['id'] = item_id
        prepared.append(entry)
    return prepared


def load_done(path):
    done = set()
    if not os.path.exists(path):
        return done
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    result = json.loads(line)
                    if result.get('ok') and 'id' in result:
                        done.add(str(result['id']))
                except (ValueError, AttributeError):
                    continue
    except OSError:
        die('output file is not readable: ' + path)
    return done


def run_item(module, item, args, client):
    started = time.monotonic()
    try:
        record = module.run_one(client, item, args)
    except ToolError as e:
        record = {'error': str(e)}
    full = {'id': item['id'], 'ok': False, 'status': None, 'attempts': 0, 'error': None}
    full.update(record)
    full['id'] = item['id']
    full['latency_s'] = round(time.monotonic() - started, 3)
    return full


def run_batch(module, prepared, args, client):
    if args.inp not in (None, '-') and os.path.abspath(args.inp) == os.path.abspath(args.out):
        die('--in and --out must be different files')
    done = load_done(args.out)
    todo = [item for item in prepared if item['id'] not in done]
    print('tool-call: %d items, %d already done, %d to run; tool %s via %s' %
          (len(prepared), len(prepared) - len(todo), len(todo), module.NAME, args.api), file=sys.stderr)
    if args.dry_run:
        if todo:
            # Previews hold the user's task input only; no credentials.
            print(json.dumps(todo[0]['preview'], ensure_ascii=False, indent=1))
        return 0

    try:
        out = open(args.out, 'a', encoding='utf-8')
    except OSError as e:
        die('cannot open output file: %s' % e)
    lock = threading.Lock()
    state = {'ok': 0, 'failed': 0, 'streak': 0, 'stopped': False,
             'rate_limits': 0, 'latencies': [], 'models': Counter()}

    def save_result(future):
        record = future.result()
        with lock:
            observed = record.pop('_observed', None)
            state['rate_limits'] += record.pop('_rate_limits', 0)
            out.write(client.scrub(json.dumps(record, ensure_ascii=False)) + '\n')
            out.flush()
            state['latencies'].append(record['latency_s'])
            if observed:
                state['models'][observed] += 1
            if record['ok']:
                state['ok'] += 1
                state['streak'] = 0
            else:
                state['failed'] += 1
                state['streak'] += 1
                print('tool-call: %s failed after %s tries (HTTP %s): %s' %
                      (record['id'], record.get('attempts'), record.get('status'),
                       client.scrub((record.get('error') or '')[:200])), file=sys.stderr)
                if state['streak'] >= args.stop_after_failures:
                    state['stopped'] = True

    next_index = 0
    future_to_item = {}
    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            while next_index < len(todo) or future_to_item:
                while (not state['stopped'] and next_index < len(todo) and
                       len(future_to_item) < args.concurrency):
                    item = todo[next_index]
                    next_index += 1
                    future_to_item[pool.submit(run_item, module, item, args, client)] = item
                if not future_to_item:
                    break
                done_futures, _ = cf.wait(future_to_item, return_when=cf.FIRST_COMPLETED)
                for future in done_futures:
                    save_result(future)
                    del future_to_item[future]
    finally:
        out.close()

    not_started = len(todo) - state['ok'] - state['failed']
    latencies = state['latencies']
    p50 = round(statistics.median(latencies), 3) if latencies else None
    max_latency = round(max(latencies), 3) if latencies else None
    print('tool-call: ok %d, failed %d, not started %d; 429 responses %d; latency p50 %ss / max %ss' %
          (state['ok'], state['failed'], not_started, state['rate_limits'], p50, max_latency), file=sys.stderr)
    if state['models']:
        print('tool-call: observed models %s' %
              client.scrub(json.dumps(state['models'], ensure_ascii=False)), file=sys.stderr)
    if state['stopped']:
        print('tool-call: stopped after %d consecutive failed items; rerun the same command to resume' %
              args.stop_after_failures, file=sys.stderr)
    return 0 if state['failed'] == 0 and not_started == 0 else 1


def read_only(client, path):
    reply = client.call('GET', path)
    if reply.error:
        print('tool-call: ' + client.scrub(reply.error), file=sys.stderr)
        return 1
    text = reply.raw.decode('utf-8', 'replace')
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    print(client.scrub(text))
    return 0


def batch(module, args, client):
    if not args.out:
        die('batch mode needs --out (appended results; also the resume record)')
    if args.out == '-':
        die('--out must be a file path')
    items_from = getattr(module, 'items_from_args', None)
    inputs = items_from(args) if items_from else None
    if inputs is None:
        if not args.inp:
            die('batch mode needs --in (JSONL, or - for stdin)')
        rows = read_jsonl(args.inp)
    else:
        if args.inp:
            die("give either --in or the tool's command-line inputs, not both")
        rows = [('input %d' % n, item) for n, item in enumerate(inputs, 1)]
    prepared = prepare_rows(module, rows, args)
    return run_batch(module, prepared, args, client)


def add_common_arguments(p):
    # Subparser defaults are suppressed so that the main parser's defaults stand.
    s = argparse.SUPPRESS
    p.add_argument('--in', dest='inp', default=s, help='input JSONL, or - for stdin')
    p.add_argument('--out', default=s, help='output JSONL (appended; also the resume record)')
    p.add_argument('-j', '--concurrency', type=int, default=s,
                   help="parallel items (default: the tool's)")
    p.add_argument('--attempts', type=int, default=s,
                   help='tries per request for 429 and model_warming 503 (default 8)')
    p.add_argument('--timeout', type=float, default=s,
                   help="seconds per request (default: the tool's)")
    p.add_argument('--stop-after-failures', type=int, default=s,
                   help='stop starting items after this many consecutive failures (default 10)')
    p.add_argument('--dry-run', action='store_true', default=s,
                   help='print the first request preview; make no calls')
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--schema', action='store_true', default=s,
                      help='print GET /tool/<tool>/schema and exit')
    mode.add_argument('--health', action='store_true', default=s,
                      help='print GET /tool/<tool>/health and exit')
    mode.add_argument('--list-models', action='store_true', default=s,
                      help='print GET /tool/<tool>/models and exit')


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--api', default='tool', help='api_id in local/api-routes.tsv (default tool)')
    parser.add_argument('--list', action='store_true', help='print GET /tool/ (the enabled tools) and exit')
    parser.set_defaults(inp=None, out=None, concurrency=None, attempts=None, timeout=None,
                        stop_after_failures=DEFAULT_STOP_AFTER, dry_run=False,
                        schema=False, health=False, list_models=False)
    subs = parser.add_subparsers(dest='tool', metavar='<tool>',
                                 help='tool to call: ' + ', '.join(tools.NAMES))
    for name in tools.NAMES:
        module = tools.load(name)
        sub = subs.add_parser(name, description=module.DESCRIPTION, help=module.DESCRIPTION,
                              formatter_class=argparse.RawDescriptionHelpFormatter)
        add_common_arguments(sub)
        module.add_arguments(sub)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.list:
            if args.tool:
                parser.error('--list takes no tool name')
            base, key = resolve_route(args.api)
            client = Client(base, key, args.timeout or READ_ONLY_TIMEOUT, args.attempts or DEFAULT_ATTEMPTS)
            return read_only(client, '/')
        if not args.tool:
            parser.error('name a tool (%s) or use --list' % ', '.join(tools.NAMES))

        module = tools.load(args.tool)
        if args.concurrency is None:
            args.concurrency = module.DEFAULT_CONCURRENCY
        if args.timeout is None:
            args.timeout = module.DEFAULT_TIMEOUT
        if args.attempts is None:
            args.attempts = DEFAULT_ATTEMPTS
        if args.concurrency < 1 or args.attempts < 1 or args.stop_after_failures < 1 or args.timeout <= 0:
            die('-j, --attempts, --stop-after-failures, and --timeout must be positive')
        # Tool checks cover batch options only; --schema, --health and --list-models need none.
        check = getattr(module, 'check_args', None)
        if check and not (args.schema or args.health or args.list_models):
            check(args)

        base, key = resolve_route(args.api)
        client = Client(base, key, args.timeout, args.attempts)
        if args.schema:
            return read_only(client, '/%s/schema' % args.tool)
        if args.health:
            return read_only(client, '/%s/health' % args.tool)
        if args.list_models:
            return read_only(client, '/%s/models' % args.tool)
        return batch(module, args, client)
    except ToolError as e:
        die(str(e))


if __name__ == '__main__':
    sys.exit(main())
