"""Tool mineru: document parsing with MinerU 4.1.2 (task API) behind CPA (/tool/mineru).

Input line: {"id", "file": "<local PDF or image path>", "tier"?: "<tier>", "output_formats"?: [...]}.
The id may use letters, digits, "_", "." and "-": it names the result file. On the command line,
--file PATH (repeatable; the id is the file name) replaces --in, and --out-dir is required.

Per item, as the upstream 4.1.2 task API takes it (checked against CPA on 2026-11-02):
  1. POST /mineru/tasks with a JSON body {"files": [{"file_name", "bytes"}], "tier", "output_formats"?}
     -> task_id and files[0].upload.url (task status "waiting-file").
  2. PUT the raw file bytes to that upload URL.
  3. Poll the task status until "completed" or "failed"; files[0].result_url names the result.
  4. GET the result, a zip that holds the markdown, into <out-dir>/<id>.zip.
URLs from the answers are used by their path only: the part after "/v1/" is requested under
/mineru/ through CPA, whatever host the answer names.
output_formats: "markdown" and "zip" name what the client returns and are not sent; the upstream
names (default, docx, html, latex) are sent in the body.
"""
import json
import os
import re
import time
from urllib.parse import quote, urlsplit

from tools import ToolError

NAME = 'mineru'
DESCRIPTION = 'document parsing (MinerU 4.1.2 task API): submit, upload, poll, download'
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT = 3600.0
DEFAULT_TIER = 'advanced'
MAX_FILE_BYTES = 200 << 20
POLL_MAX_INTERVAL = 60.0
MAX_ERROR_CHARS = 600
TOOL_PREFIX = '/tool/mineru/'
CLIENT_FORMATS = ('markdown', 'zip')
UPSTREAM_FORMATS = ('default', 'docx', 'html', 'latex')
ID_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*')
UPSTREAM_URL_RE = re.compile(r'(?:https?://[^/\s]+)?/v1/(\S+)')


def add_arguments(parser):
    parser.add_argument('--file', action='append', default=[], metavar='PATH',
                        help='file to parse; repeat for several, instead of --in')
    parser.add_argument('--tier', default=DEFAULT_TIER,
                        help='parsing tier, default advanced (ids from GET /tool/mineru/tiers)')
    parser.add_argument('--out-dir', help='directory for results: <id>.zip (required)')
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
    known = CLIENT_FORMATS + UPSTREAM_FORMATS
    unknown = [f for f in (formats or []) if f not in known]
    if unknown:
        raise ToolError('"output_formats" %s not known (use %s)' % (json.dumps(unknown), ', '.join(known)))
    upstream = [f for f in dict.fromkeys(formats or []) if f in UPSTREAM_FORMATS]
    body = {'files': [{'file_name': os.path.basename(path), 'bytes': size}], 'tier': tier}
    if upstream:
        body['output_formats'] = upstream
    preview = {'file': path, 'bytes': size, 'tier': tier, 'output_formats': formats or [],
               'submit': 'POST /mineru/tasks', 'body': body}
    return {'file': path, 'body': body, 'preview': preview}


def _json_object(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _first_file(answer):
    files = answer.get('files') if answer else None
    first = files[0] if isinstance(files, list) and files else None
    return first if isinstance(first, dict) else {}


def _route(url):
    """Return (path, absolute) under CPA for a URL from an answer, or None.

    CPA gives /tool/mineru/... URLs, bare or with its own host (http://<cpa>/tool/mineru/..., seen
    2026-11-02 evening); their path is used as it is. The upstream gives http://<host>/v1/... URLs, which
    CPA did not rewrite earlier that day: their path after /v1/ goes under /mineru/ and the host is dropped.
    """
    if not isinstance(url, str) or '..' in url:
        return None
    parts = urlsplit(url)
    path = parts.path + ('?' + parts.query if parts.query else '')
    if path.startswith(TOOL_PREFIX):
        return path, True
    m = UPSTREAM_URL_RE.fullmatch(url)
    return ('/mineru/' + m.group(1), False) if m else None


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

    reply = send('POST', '/mineru/tasks', json_body=item['body'])
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
    upload = _first_file(answer).get('upload')
    upload = upload if isinstance(upload, dict) else {}
    target = _route(upload.get('url'))
    if target is None:
        record['error'] = 'submit answer has no upload URL'
        return record
    headers = upload.get('headers')
    ctype = headers.get('Content-Type') if isinstance(headers, dict) else None

    reply = send('PUT', target[0], absolute=target[1], data=data,
                 content_type=ctype or 'application/octet-stream')
    del data
    if reply.error is not None:
        record['error'] = 'upload failed: ' + reply.error
        return record

    status_path = '/mineru/tasks/%s' % quote(task_id, safe='')
    interval = args.poll_interval
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            record['error'] = 'timeout: task %s not finished within %s s' % (task_id, args.timeout)
            return record
        time.sleep(min(interval, remaining))
        interval = min(interval * 2, POLL_MAX_INTERVAL)
        reply = send('GET', status_path)
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

    target = _route(_first_file(answer).get('result_url'))
    if target is None:
        record['error'] = 'completed answer has no result URL'
        return record
    try:
        os.makedirs(args.out_dir, exist_ok=True)
    except OSError as e:
        record['error'] = 'cannot create --out-dir: %s' % (e.strerror or e)
        return record
    dest = os.path.join(args.out_dir, '%s.zip' % item['id'])
    reply = send('GET', target[0], absolute=target[1], save_to=dest)
    if reply.error is not None:
        record['error'] = 'result download failed: ' + reply.error
        return record
    record.update(ok=True, result_file=dest, error=None)
    return record
