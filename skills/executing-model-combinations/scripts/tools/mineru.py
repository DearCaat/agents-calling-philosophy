"""Tool mineru: document parsing through the MinerU V1 API behind CPA (/tool/mineru).

Input line: {"id", "file": "<local PDF or image path>", "tier"?: "<tier>", "output_formats"?: ["markdown", "zip"]}.
The id may use letters, digits, "_", "." and "-": it names the result file. On the command line,
--file PATH (repeatable; the id is the file name) replaces --in, and --out-dir is required.
Per item: POST tasks?filename=&tier=[&output_formats=] with the raw file bytes -> task_id ->
poll the status until completed or failed -> GET the result (response_format=zip when
output_formats has "zip") into <out-dir>/<id>.zip or <id>.json. Status and result URLs are
used as CPA returns them (under /tool/mineru/).
"""
import json
import os
import re
import time
from urllib.parse import quote, urlencode

from tools import ToolError

NAME = 'mineru'
DESCRIPTION = 'document parsing (MinerU V1 API): submit, poll, download'
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT = 3600.0
DEFAULT_TIER = 'advanced'
MAX_FILE_BYTES = 200 << 20
POLL_MAX_INTERVAL = 60.0
MAX_ERROR_CHARS = 600
TOOL_PREFIX = '/tool/mineru/'
ID_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*')


def add_arguments(parser):
    parser.add_argument('--file', action='append', default=[], metavar='PATH',
                        help='file to parse; repeat for several, instead of --in')
    parser.add_argument('--tier', default=DEFAULT_TIER,
                        help='parsing tier, default advanced (ids from GET /tool/mineru/tiers)')
    parser.add_argument('--out-dir', help='directory for results: <id>.zip or <id>.json (required)')
    parser.add_argument('--poll-interval', type=float, default=5.0,
                        help='seconds before the first status poll, doubling up to 60 (default 5)')


def check_args(args):
    if not args.out_dir:
        raise ToolError('--out-dir is required (results are written there)')
    if args.poll_interval <= 0:
        raise ToolError('--poll-interval must be positive')


def _safe_name(name):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', name).lstrip('.')
    if not ID_RE.fullmatch(safe):
        safe = '_' + safe
    return safe


def items_from_args(args):
    if not args.file:
        return None
    return [{'id': _safe_name(os.path.basename(path)), 'file': path} for path in args.file]


def prepare(item, args):
    item_id = str(item['id'])
    if not ID_RE.fullmatch(item_id):
        raise ToolError('id %r may use only letters, digits, "_", "." and "-" (it names the result file)'
                        % item_id)
    path = item.get('file')
    if not isinstance(path, str) or not path.strip():
        raise ToolError('needs a nonempty string "file"')
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise ToolError('not a readable file: %s' % path)
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ToolError('file %s is larger than 200 MiB' % path)
    tier = item.get('tier', args.tier)
    if not isinstance(tier, str) or not tier.strip():
        raise ToolError('"tier" must be a nonempty string')
    formats = item.get('output_formats')
    if formats is not None and (not isinstance(formats, list) or
                                not all(isinstance(f, str) and f.strip() for f in formats)):
        raise ToolError('"output_formats" must be a list of strings')
    query = [('filename', os.path.basename(path)), ('tier', tier)]
    if formats:
        # MinerU V1 takes one output_formats parameter per format (gateway guide, 2026-10-26).
        query.extend(('output_formats', f) for f in formats)
    want_zip = bool(formats) and 'zip' in formats
    preview = {'file': path, 'bytes': size, 'tier': tier, 'output_formats': formats or [],
               'submit': '/mineru/tasks?' + urlencode(query)}
    return {'file': path, 'query': query, 'want_zip': want_zip, 'preview': preview}


def _json_object(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _locate(url, fallback):
    """Return (path, absolute): the URL CPA put in the answer, or the fallback under the tool base."""
    if isinstance(url, str) and url.startswith(TOOL_PREFIX) and '..' not in url:
        return url, True
    return fallback, False


def run_one(client, item, args):
    deadline = time.monotonic() + args.timeout
    record = {'ok': False, 'task_id': None, 'task_status': None, 'result_file': None,
              'status': None, 'attempts': 0, 'error': None, '_rate_limits': 0}

    def send(method, path, absolute=False, **kwargs):
        reply = client.call(method, path, absolute=absolute, timeout=args.timeout,
                            attempts=args.attempts, **kwargs)
        record['status'] = reply.status
        record['attempts'] += reply.attempts
        record['_rate_limits'] += reply.rate_limits
        return reply

    try:
        with open(item['file'], 'rb') as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        record['error'] = 'cannot read file: %s' % (e.strerror or e)
        return record
    if len(data) > MAX_FILE_BYTES:
        record['error'] = 'file is larger than 200 MiB'
        return record

    reply = send('POST', '/mineru/tasks?' + urlencode(item['query']), data=data,
                 content_type='application/octet-stream')
    del data
    if reply.error is not None:
        record['error'] = 'submit failed: ' + reply.error
        return record
    answer = _json_object(reply.raw)
    task_id = answer.get('task_id') if answer else None
    if task_id is None or str(task_id) == '':
        record['error'] = 'submit answer has no task_id'
        return record
    task_id = str(task_id)
    record['task_id'] = task_id
    task_quoted = quote(task_id, safe='')
    status_path, status_abs = _locate(answer.get('status_url'), '/mineru/tasks/%s' % task_quoted)
    result_path, result_abs = _locate(answer.get('result_url'), '/mineru/tasks/%s/result' % task_quoted)

    interval = args.poll_interval
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record['error'] = 'timeout: task %s not finished within %s s' % (task_id, args.timeout)
            return record
        time.sleep(min(interval, remaining))
        interval = min(interval * 2, POLL_MAX_INTERVAL)
        reply = send('GET', status_path, absolute=status_abs)
        if reply.error is not None:
            record['error'] = 'status poll failed: ' + reply.error
            return record
        answer = _json_object(reply.raw)
        if answer is None:
            record['error'] = 'status answer is not a JSON object'
            return record
        record['task_status'] = answer.get('status')
        if record['task_status'] == 'completed':
            break
        if record['task_status'] == 'failed':
            detail = answer.get('error', answer)
            record['error'] = 'task failed: ' + json.dumps(detail, ensure_ascii=False)[:MAX_ERROR_CHARS]
            return record

    try:
        os.makedirs(args.out_dir, exist_ok=True)
    except OSError as e:
        record['error'] = 'cannot create --out-dir: %s' % (e.strerror or e)
        return record
    ext = 'zip' if item['want_zip'] else 'json'
    dest = os.path.join(args.out_dir, '%s.%s' % (item['id'], ext))
    query = '?response_format=zip' if item['want_zip'] else ''
    reply = send('GET', result_path + query, absolute=result_abs, save_to=dest)
    if reply.error is not None:
        record['error'] = 'result download failed: ' + reply.error
        return record
    record.update(ok=True, result_file=dest, error=None)
    return record
