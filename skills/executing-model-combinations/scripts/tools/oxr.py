"""Tool oxr: document parsing through the OXR parser behind CPA (/tool/oxr).

Input line: {"id", "file": "<local PDF or image path>", "output_formats"?: ["markdown", "json"],
"element_formats"?: {"table": "markdown", ...}}. The id may use letters, digits, "_", "." and "-":
it names the result files. On the command line, --file PATH (repeatable; the id is the file name)
replaces --in, and --out-dir is required.
Per item: one multipart POST to /oxr/parse/sync with the file (read into memory, at most 200 MiB),
one output_formats field per format, an api_key placeholder (the gateway key is never sent in the
form), and element_formats only when given; otherwise the server default applies. A code other than
0 in the answer is a failure. Results: <out-dir>/<id>.json (the full response body) and, when
markdown was requested, <out-dir>/<id>.md.
"""
import json
import os
import re
import uuid

from tools import ToolError

NAME = 'oxr'
DESCRIPTION = 'document parsing (OXR parser): one synchronous multipart call per file'
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT = 3600.0
MAX_FILE_BYTES = 200 << 20
MAX_ERROR_CHARS = 600
ENDPOINT = '/oxr/parse/sync'
FORMATS = ('markdown', 'json')
API_KEY_FIELD = 'unused'  # the form field the API requires; the real key stays in the Authorization header
ID_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*')


def add_arguments(parser):
    parser.add_argument('--file', action='append', default=[], metavar='PATH',
                        help='file to parse; repeat for several, instead of --in')
    parser.add_argument('--out-dir', help='directory for results: <id>.json, and <id>.md when markdown '
                                          'is requested (required)')
    parser.add_argument('--element-formats', metavar='JSON', default=None,
                        help='element_formats object sent with every item (default: the server default); '
                             'a row may set its own')


def _element_formats(value, label):
    """Return the element_formats JSON text to send, or None for the server default."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ToolError('%s must be valid JSON' % label)
    if not isinstance(value, dict) or not value or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ToolError('%s must be a nonempty JSON object of strings' % label)
    return json.dumps(value, ensure_ascii=False)


def check_args(args):
    if not args.out_dir:
        raise ToolError('--out-dir is required (results are written there)')
    args.element_formats = _element_formats(args.element_formats, '--element-formats')


def _safe_name(name):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', name).lstrip('.')
    if not ID_RE.fullmatch(safe):
        safe = '_' + safe
    return safe


def _form_name(path):
    return re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(path))


def items_from_args(args):
    if not args.file:
        return None
    return [{'id': _safe_name(os.path.basename(path)), 'file': path} for path in args.file]


def prepare(item, args):
    item_id = str(item['id'])
    if not ID_RE.fullmatch(item_id):
        raise ToolError('id %r may use only letters, digits, "_", "." and "-" (it names the result files)'
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
    formats = item.get('output_formats')
    if formats is None:
        formats = list(FORMATS)
    if not isinstance(formats, list) or not formats or not all(
            isinstance(f, str) and f in FORMATS for f in formats):
        raise ToolError('"output_formats" must be a nonempty list of: %s' % ', '.join(FORMATS))
    formats = list(dict.fromkeys(formats))
    if 'element_formats' in item:
        element = _element_formats(item['element_formats'], '"element_formats"')
    else:
        element = args.element_formats
    preview = {'file': path, 'bytes': size, 'output_formats': formats,
               'element_formats': json.loads(element) if element else None, 'submit': ENDPOINT}
    return {'file': path, 'output_formats': formats, 'element_formats': element, 'preview': preview}


def _json_object(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _multipart(fields, filename, data, boundary):
    """Form body: the file part first, then one text part per (name, value) pair."""
    body = bytearray(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n' % (boundary, filename)).encode('utf-8'))
    body += data
    body += b'\r\n'
    for name, value in fields:
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (boundary, name, value)).encode('utf-8')
    body += ('--%s--\r\n' % boundary).encode('ascii')
    return bytes(body)


def _write_result(dest, data):
    part = dest + '.part'
    with open(part, 'wb') as f:
        f.write(data)
    os.replace(part, dest)


def run_one(client, item, args):
    record = {'ok': False, 'status': None, 'code': None, 'message': None, 'pages': None,
              'processing_time_ms': None, 'markdown_file': None, 'json_file': None,
              'attempts': 0, 'error': None, '_rate_limits': 0}

    def send(**kwargs):
        reply = client.call('POST', ENDPOINT, timeout=args.timeout, attempts=args.attempts, **kwargs)
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

    boundary = uuid.uuid4().hex
    fields = [('output_formats', f) for f in item['output_formats']]
    fields.append(('api_key', API_KEY_FIELD))
    if item['element_formats'] is not None:
        fields.append(('element_formats', item['element_formats']))
    body = _multipart(fields, _form_name(item['file']), data, boundary)
    del data
    reply = send(data=body, content_type='multipart/form-data; boundary=' + boundary)
    del body
    if reply.error is not None:
        record['error'] = reply.error
        return record

    answer = _json_object(reply.raw)
    if answer is None:
        record['error'] = 'answer is not a JSON object'
        return record
    code = answer.get('code')
    message = answer.get('message')
    record['code'] = code
    record['message'] = str(message)[:MAX_ERROR_CHARS] if message is not None else None
    if code != 0:
        record['error'] = ('code %s: %s' % (code, record['message'] or 'no message'))[:MAX_ERROR_CHARS]
        return record

    data_obj = _dict(answer.get('data'))
    outputs = _dict(_dict(data_obj.get('result')).get('outputs'))
    metadata = _dict(data_obj.get('metadata'))
    record['pages'] = metadata.get('page_num')
    record['processing_time_ms'] = metadata.get('processing_time_ms')
    try:
        os.makedirs(args.out_dir, exist_ok=True)
        record['json_file'] = os.path.join(args.out_dir, '%s.json' % item['id'])
        _write_result(record['json_file'], reply.raw)
        markdown = outputs.get('markdown')
        if 'markdown' in item['output_formats']:
            if not isinstance(markdown, str):
                record['error'] = 'answer has no markdown output'
                return record
            record['markdown_file'] = os.path.join(args.out_dir, '%s.md' % item['id'])
            _write_result(record['markdown_file'], markdown.encode('utf-8'))
    except OSError as e:
        record['error'] = 'cannot write results: %s' % (e.strerror or e)
        return record
    record.update(ok=True, error=None)
    return record
