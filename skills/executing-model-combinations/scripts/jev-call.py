#!/usr/bin/env python3
"""Call the native JEV decision endpoint over a JSONL batch.

Each input line is one request, for example:
  {"id":"sample-1","state":"...","questions":{"eligible":{"type":"noul","instructions":"...","criteria":{"true":"yes","false":"no"}}}}

Optional fields are "model" (otherwise --model; there is no default because the served
models change, list them with --list-models) and "images" (file paths or base64 data
URIs). Results are appended to --out as each item finishes. On rerun, ids with a
successful result already in --out are skipped.

The route is resolved from --api in local/api-routes.tsv. This is JEV's native
/v1/systemone protocol, not a chat-completions or Messages API. Requests bypass
environment proxies.
"""
import argparse
import base64
import binascii
import concurrent.futures as cf
import json
import mimetypes
import os
import random
import re
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
from io import BytesIO
from pathlib import Path


MAX_ERROR_CHARS = 600


def die(msg):
    print('jev-call: ' + msg, file=sys.stderr)
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
    if wire != 'jev-systemone':
        die('route %s uses unsupported wire protocol: %s' % (api, wire))
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        die('route %s has an invalid base URL' % api)
    key = read_credential(credential_ref, credentials_file)
    return base.rstrip('/'), key, state


def image_bytes_to_data_uri(data, mime, max_side=None):
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ValueError('images must be PNG, JPEG, or WebP')
    if max_side is not None:
        try:
            from PIL import Image, ImageOps
        except ImportError:
            raise ValueError('Pillow (PIL) is required for --max-side; install Pillow and retry')
        try:
            image = Image.open(BytesIO(data))
            image = ImageOps.exif_transpose(image)
            image.load()
        except Exception as e:
            raise ValueError('cannot decode image for --max-side: %s' % e)
        width, height = image.size
        longest = max(width, height)
        if longest > max_side:
            scale = max_side / float(longest)
            size = (max(1, round(width * scale)), max(1, round(height * scale)))
            resampling = getattr(Image, 'Resampling', Image).LANCZOS
            image = image.resize(size, resampling)
            fmt = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}[mime]
            if fmt == 'JPEG' and image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            output = BytesIO()
            image.save(output, format=fmt)
            data = output.getvalue()
    return 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))


def image_to_data_uri(value, max_side=None):
    if not isinstance(value, str) or not value:
        raise ValueError('each images entry must be a nonempty file path or data URI')
    if value.startswith('data:'):
        match = re.fullmatch(r'data:(image/(?:png|jpeg|webp));base64,(.*)', value, re.DOTALL)
        if not match:
            raise ValueError('image data URI must be base64 PNG, JPEG, or WebP')
        if max_side is None:
            return value
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('image data URI has invalid base64')
        return image_bytes_to_data_uri(data, match.group(1), max_side)

    path = Path(os.path.expanduser(value))
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ValueError('cannot read image file %s: %s' % (path, e))
    mime = mimetypes.guess_type(str(path))[0]
    if mime == 'image/jpg':
        mime = 'image/jpeg'
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ValueError('unsupported image file type: %s' % path.suffix)
    return image_bytes_to_data_uri(data, mime, max_side)


def prepare_item(item, args, line_no):
    if not isinstance(item, dict):
        die('input line %d must be a JSON object' % line_no)
    if 'id' not in item or item['id'] is None:
        die('input line %d needs "id"' % line_no)
    item_id = str(item['id'])
    if not item_id:
        die('input line %d has an empty id' % line_no)
    state = item.get('state')
    questions = item.get('questions')
    if not isinstance(state, str) or not state.strip():
        die('input line %d needs a nonempty string "state"' % line_no)
    if not isinstance(questions, dict) or not questions:
        die('input line %d needs a nonempty object "questions"' % line_no)
    model = item['model'] if 'model' in item else args.model
    if model is None:
        die('input line %d has no "model" and --model is not set; take an id from --list-models' % line_no)
    if not isinstance(model, str) or not model.strip():
        die('input line %d has an invalid model' % line_no)
    body = {'model': model, 'state': state, 'questions': questions}
    if 'images' in item:
        images = item['images']
        if not isinstance(images, list):
            die('input line %d: "images" must be a list' % line_no)
        try:
            body['images'] = [image_to_data_uri(image, args.max_side) for image in images]
        except ValueError as e:
            die('input line %d: %s' % (line_no, e))
    return {'id': item_id, 'model': model, 'body': body}


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
    # JEV is on the private network; do not let ambient HTTP(S)_PROXY settings intercept it.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request_once(opener, url, headers, body, timeout):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(url, data=data, headers=headers, method='GET' if body is None else 'POST')
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, response.headers, raw, None
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


def run_one(item, args, ctx):
    started = time.monotonic()
    record = {
        'id': item['id'], 'ok': False, 'answers': None, 'usage': None,
        'model': item['model'], 'status': None, 'attempts': 0,
        'latency_s': None, 'error': None,
    }
    rate_limits = 0
    url = ctx['base'] + '/systemone'
    for attempt in range(1, args.attempts + 1):
        record['attempts'] = attempt
        status, headers, raw, transport_error = request_once(
            ctx['opener'], url, ctx['headers'], item['body'], args.timeout)
        record['status'] = status

        if transport_error is not None:
            record['error'] = ('timeout: ' if status == 'timeout' else 'network error: ') + transport_error
            break

        if isinstance(status, int) and 200 <= status < 300:
            try:
                response = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                record['error'] = 'HTTP %d: response is not valid JSON' % status
                break
            if not isinstance(response, dict) or not isinstance(response.get('answers'), dict):
                record['error'] = 'HTTP %d: response is missing an answers object' % status
                break
            observed = response.get('model')
            record.update(ok=True, answers=response['answers'], usage=response.get('usage'),
                          model=observed or item['model'], error=None)
            record['_observed_model'] = observed
            break

        message = response_error(raw, ctx['key'])
        record['error'] = 'HTTP %s: %s' % (status, message)
        if status == 429:
            rate_limits += 1
            if attempt < args.attempts:
                time.sleep(retry_delay(headers, attempt))
                continue
        elif status == 503 and is_model_warming(raw) and attempt < args.attempts:
            time.sleep(retry_delay(headers, attempt))
            continue
        # In particular, 504 and socket timeouts are never replayed. The gateway may
        # still be processing an already dispatched request.
        break

    record['latency_s'] = round(time.monotonic() - started, 3)
    return record, rate_limits


def read_jsonl(path, args):
    source = sys.stdin if path == '-' else open(path, encoding='utf-8')
    items = []
    seen = set()
    try:
        for line_no, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                die('input line %d is not valid JSON' % line_no)
            item = prepare_item(value, args, line_no)
            if item['id'] in seen:
                die('duplicate id on input line %d: %s' % (line_no, item['id']))
            seen.add(item['id'])
            items.append(item)
    finally:
        if source is not sys.stdin:
            source.close()
    return items


def run_batch(items, args, base, key):
    if args.inp != '-' and os.path.abspath(args.inp) == os.path.abspath(args.out):
        die('--in and --out must be different files')
    done = load_done(args.out)
    todo = [item for item in items if item['id'] not in done]
    already_done = len(items) - len(todo)
    print('jev-call: %d items, %d already done, %d to run; model default %s via %s' %
          (len(items), already_done, len(todo), args.model or 'none (set per line)', args.api), file=sys.stderr)
    if args.dry_run:
        if todo:
            # The request body contains only user-provided task input; no credentials.
            print(json.dumps(todo[0]['body'], ensure_ascii=False, indent=1))
        return 0

    ctx = {
        'base': base,
        'key': key,
        'opener': make_opener(),
        'headers': {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key},
    }
    lock = threading.Lock()
    state = {'ok': 0, 'failed': 0, 'streak': 0, 'stopped': False,
             'rate_limits': 0, 'latencies': [], 'models': Counter()}
    try:
        out = open(args.out, 'a', encoding='utf-8')
    except OSError as e:
        die('cannot open output file: %s' % e)

    def save_result(future):
        item, (record, rate_limits) = future_to_item[future], future.result()
        with lock:
            observed_model = record.pop('_observed_model', None)
            output_line = json.dumps(record, ensure_ascii=False)
            if key:
                output_line = output_line.replace(key, '[REDACTED]')
            out.write(output_line + '\n')
            out.flush()
            state['rate_limits'] += rate_limits
            state['latencies'].append(record['latency_s'])
            if observed_model:
                state['models'][observed_model] += 1
            if record['ok']:
                state['ok'] += 1
                state['streak'] = 0
            else:
                state['failed'] += 1
                state['streak'] += 1
                print('jev-call: %s failed after %d tries (HTTP %s): %s' %
                      (record['id'], record['attempts'], record['status'],
                       (record.get('error') or '')[:200]), file=sys.stderr)
                if state['streak'] >= args.stop_after_failures:
                    state['stopped'] = True

    next_index = 0
    future_to_item = {}
    t0 = time.monotonic()
    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            while next_index < len(todo) or future_to_item:
                while (not state['stopped'] and next_index < len(todo) and
                       len(future_to_item) < args.concurrency):
                    item = todo[next_index]
                    next_index += 1
                    future = pool.submit(run_one, item, args, ctx)
                    future_to_item[future] = item
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
    print('jev-call: ok %d, failed %d, not started %d; 429 responses %d; latency p50 %ss / max %ss' %
          (state['ok'], state['failed'], not_started, state['rate_limits'], p50, max_latency), file=sys.stderr)
    observed = json.dumps(state['models'], ensure_ascii=False)
    if key:
        observed = observed.replace(key, '[REDACTED]')
    print('jev-call: observed models %s' % observed, file=sys.stderr)
    if state['stopped']:
        print('jev-call: stopped after %d consecutive failed items; rerun the same command to resume' %
              args.stop_after_failures, file=sys.stderr)
    return 0 if state['failed'] == 0 and not_started == 0 else 1


def read_only(args, base, key):
    suffix = '/models' if args.list_models else '/health'
    status, _, raw, transport_error = request_once(
        make_opener(), base + suffix,
        {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
        None, args.timeout)
    if transport_error:
        print('jev-call: %s: %s' % (status, transport_error), file=sys.stderr)
        return 1
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        print('jev-call: HTTP %s: response is not valid JSON' % status, file=sys.stderr)
        return 1
    if not isinstance(status, int) or not 200 <= status < 300:
        print('jev-call: HTTP %s: %s' % (status, response_error(raw, key)), file=sys.stderr)
        return 1
    rendered = json.dumps(response, ensure_ascii=False, indent=2)
    print(rendered.replace(key, '[REDACTED]') if key else rendered)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--api', required=True, help='api_id in the local api-routes.tsv (e.g. jev)')
    parser.add_argument('--in', dest='inp', help='input JSONL, or - for stdin')
    parser.add_argument('--out', help='output JSONL (appended; also the resume record)')
    parser.add_argument('--model', help='model id for lines without "model"; no default, the served models change '
                        '(take an id from --list-models)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-models', action='store_true', help='print GET /v1/models and exit')
    mode.add_argument('--health', action='store_true', help='print GET /v1/health and exit')
    parser.add_argument('-j', '--concurrency', type=int, default=32,
                        help='parallel requests (default 32; the gateway queues, so the client does not throttle)')
    parser.add_argument('--attempts', type=int, default=8, help='tries per item for 429/model_warming (default 8; a busy gateway queues a request up to about 30 s before answering 429)')
    parser.add_argument('--timeout', type=float, default=135,
                        help='seconds to wait for one request (default 135; gateway budget is 120 seconds)')
    parser.add_argument('--stop-after-failures', type=int, default=10,
                        help='stop starting requests after this many consecutive failed items (default 10)')
    parser.add_argument('--max-side', type=int, metavar='PIXELS',
                        help='resize input images so the longest side is at most PIXELS (requires Pillow)')
    parser.add_argument('--dry-run', action='store_true', help='print the first request body; make no calls')
    args = parser.parse_args(argv)

    if args.concurrency < 1 or args.attempts < 1 or args.stop_after_failures < 1 or args.timeout <= 0:
        die('-j, --attempts, --stop-after-failures, and --timeout must be positive')
    if args.max_side is not None and args.max_side < 1:
        die('--max-side must be positive')
    if not (args.list_models or args.health) and (not args.inp or not args.out):
        die('batch mode needs --in and --out')
    if args.inp == '-' and args.out == '-':
        die('--out must be a file path')

    base, key, state = resolve_route(args.api)
    if args.list_models or args.health:
        return read_only(args, base, key)
    items = read_jsonl(args.inp, args)
    return run_batch(items, args, base, key)


if __name__ == '__main__':
    sys.exit(main())
