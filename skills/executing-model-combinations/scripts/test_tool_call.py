#!/usr/bin/env python3
"""Offline tests for tool-call.py and its tools, using a local HTTP mock of /tool/... only."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name('tool-call.py')
KEY = 'unit-test-key'  # the mock echoes it in some error bodies, to test redaction


def parse_multipart(ctype, body):
    """Split a multipart/form-data body into dicts: name, filename, value (bytes)."""
    message = message_from_bytes(b'Content-Type: ' + ctype.encode('latin-1') + b'\r\n\r\n' + body)
    return [{'name': part.get_param('name', header='content-disposition'),
             'filename': part.get_filename(),
             'value': part.get_payload(decode=True)} for part in message.get_payload()]


class Mock:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []  # dicts: method, path, query, auth, ctype, body
        self.counts = {}
        self.tasks = {}
        self.task_seq = 0


def start_mock():
    mock = Mock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, status, payload, ctype='application/json', headers=None):
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def handle_any(self, method):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            auth = self.headers.get('Authorization')
            with mock.lock:
                mock.calls.append({'method': method, 'path': url.path, 'query': query, 'auth': auth,
                                   'ctype': self.headers.get('Content-Type'), 'body': body})
            if auth != 'Bearer ' + KEY:
                return self.reply(401, {'error': 'missing or invalid API key'})
            return self.answer(method, url.path, query, body)

        def do_GET(self):
            self.handle_any('GET')

        def do_POST(self):
            self.handle_any('POST')

        def answer(self, method, path, query, body):
            if method == 'GET' and path == '/tool/':
                return self.reply(200, {'object': 'list', 'data': [{'id': 'jev'}, {'id': 'vela'}, {'id': 'mineru'}, {'id': 'oxr'}]})
            if method == 'GET' and path == '/tool/jev/schema':
                return self.reply(200, {'tool': 'jev', 'endpoints': []})
            if method == 'GET' and path == '/tool/jev/health':
                return self.reply(200, {'status': 'ok'})
            if method == 'GET' and path == '/tool/jev/models':
                return self.reply(500, {'error': 'upstream refused token ' + KEY})
            if method == 'POST' and path in ('/tool/jev/systemone', '/tool/vela/systemone'):
                return self.judge(body)
            if path.startswith('/tool/oxr/'):
                return self.oxr(method, path, body)
            if path.startswith('/tool/mineru/'):
                return self.mineru(method, path, query, body)
            return self.reply(404, {'error': 'not found'})

        def judge(self, body):
            data = json.loads(body)
            state = data['state']
            sample = state['answer'] if isinstance(state, dict) else state
            with mock.lock:
                mock.counts[sample] = mock.counts.get(sample, 0) + 1
                count = mock.counts[sample]
            if sample == 'retry' and count == 1:
                return self.reply(429, b'<html>busy</html>', 'text/html', {'Retry-After': '0'})
            if sample == 'warming' and count == 1:
                return self.reply(503, {'code': 'model_warming'}, headers={'Retry-After': '0'})
            if sample == 'fail504':
                return self.reply(504, {'error': 'processing deadline'})
            if sample == 'fail422':
                return self.reply(422, {'error': 'nonempty state and questions are required'})
            if sample == 'leaky':
                return self.reply(500, {'error': 'gateway logged token ' + KEY})
            answers = {name: {'type': q.get('type', 'noul'), 'noul': 0.75}
                       for name, q in data['questions'].items()}
            return self.reply(200, {'model': 'mock-model', 'answers': answers,
                                    'usage': {'input_tokens': 1, 'output_tokens': 0}})

        def mineru(self, method, path, query, body):
            prefix = '/tool/mineru/tasks'
            if method == 'POST' and path == prefix:
                name = query.get('filename', [''])[0]
                with mock.lock:
                    mock.counts[('submit', name)] = mock.counts.get(('submit', name), 0) + 1
                    count = mock.counts[('submit', name)]
                    if name == 'retry-once.pdf' and count == 1:
                        retry = True
                    else:
                        retry = False
                        mock.task_seq += 1
                        tid = 't%d' % mock.task_seq
                        mock.tasks[tid] = {'file': name, 'polls': 0}
                if retry:
                    return self.reply(429, {'error': 'busy'}, headers={'Retry-After': '0'})
                return self.reply(200, {'task_id': tid, 'status': 'accepted',
                                        'status_url': '/tool/mineru/tasks/%s' % tid,
                                        'result_url': '/tool/mineru/tasks/%s/result' % tid})
            if method == 'GET' and path.startswith(prefix + '/'):
                tid, _, tail = path[len(prefix) + 1:].partition('/')
                with mock.lock:
                    task = mock.tasks.get(tid)
                    if task is not None and tail == '':
                        task['polls'] += 1
                        polls = task['polls']
                if task is None:
                    return self.reply(404, {'error': 'no such task'})
                if tail == '':
                    if task['file'].startswith('fail'):
                        return self.reply(200, {'task_id': tid, 'status': 'failed', 'error': 'parser crashed'})
                    if task['file'].startswith('never') or polls < 3:
                        return self.reply(200, {'task_id': tid, 'status': 'processing'})
                    return self.reply(200, {'task_id': tid, 'status': 'completed'})
                if tail == 'result':
                    if query.get('response_format') == ['zip']:
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, 'w') as z:
                            z.writestr('content.md', '# parsed %s' % task['file'])
                        return self.reply(200, buf.getvalue(), 'application/zip')
                    return self.reply(200, {'task_id': tid, 'markdown': '# parsed %s' % task['file']})
            return self.reply(404, {'error': 'not found'})

        def oxr(self, method, path, body):
            if method != 'POST' or path != '/tool/oxr/parse/sync':
                return self.reply(404, {'error': 'not found'})
            parts = parse_multipart(self.headers.get('Content-Type'), body)
            name = next((p['filename'] for p in parts if p['name'] == 'file'), '')
            with mock.lock:
                mock.counts[('oxr', name)] = mock.counts.get(('oxr', name), 0) + 1
                count = mock.counts[('oxr', name)]
            if name.startswith('warm') and count == 1:
                return self.reply(503, {'code': 'model_warming', 'message': 'warming up'},
                                  headers={'Retry-After': '0'})
            if name.startswith('bad'):
                return self.reply(200, {'code': 1133, 'message': 'element_formats: unsupported value',
                                        'warnings': [], 'data': None})
            return self.reply(200, {'code': 0, 'message': 'ok', 'warnings': [],
                                    'data': {'task_id': 'oxr-' + name, 'error': None,
                                             'result': {'file_name': name,
                                                        'outputs': {'markdown': '# parsed %s' % name,
                                                                    'json': {'pages': [{'index': 1}]}}},
                                             'metadata': {'file_type': 'pdf', 'page_num': 2,
                                                          'processing_time_ms': 1234}}})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, mock


class ToolCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tool-call-test-')
        self.root = Path(self.tmp.name)
        self.server, self.server_thread, self.mock = start_mock()
        self.local = self.root / 'local'
        self.local.mkdir()
        self.key_file = self.root / 'mock-key'
        self.key_file.write_text(KEY + '\n', encoding='utf-8')
        base = 'http://127.0.0.1:%d/tool' % self.server.server_address[1]
        (self.local / 'api-routes.tsv').write_text(
            'api_id\tbase_url\twire_protocol\tcredential_ref\tmodels_path\tauth_scheme\tstate\tnotes\n'
            'tool\t%s\tcpa-tool\tfile:%s\t/\tbearer\tavailable\tlocal mock\n' % (base, self.key_file),
            encoding='utf-8')
        self.env = dict(os.environ)
        self.env['AGENTS_LOCAL_ROOT'] = str(self.local)
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            self.env.pop(name, None)
        self.input = self.root / 'input.jsonl'
        self.output = self.root / 'output.jsonl'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.tmp.cleanup()

    def run_tool(self, *args, stdin=None):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), *args], env=self.env, text=True,
                              capture_output=True, input=stdin, timeout=60)

    def batch(self, tool, *extra):
        return self.run_tool(tool, '--in', str(self.input), '--out', str(self.output), *extra)

    def write_rows(self, rows):
        self.input.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def output_rows(self):
        if not self.output.exists():
            return []
        return [json.loads(line) for line in self.output.read_text(encoding='utf-8').splitlines()]

    def make_file(self, name, data=b'%PDF-1.4 unit test bytes\n'):
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    @staticmethod
    def question():
        return {'q': {'type': 'noul', 'instructions': 'Is it true?', 'criteria': {'true': 'yes', 'false': 'no'}}}

    def row(self, item_id, state, **extra):
        item = {'id': item_id, 'state': state, 'questions': self.question()}
        item.update(extra)
        return item

    def posts(self, path):
        return [c for c in self.mock.calls if c['method'] == 'POST' and c['path'] == path]

    # jev and vela

    def test_jev_judgment_goes_to_the_tool_path(self):
        self.write_rows([self.row('j1', 'normal')])
        result = self.batch('jev', '--model', 'm1', '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual(row['model'], 'mock-model')
        call, = self.posts('/tool/jev/systemone')
        self.assertEqual(call['auth'], 'Bearer ' + KEY)
        self.assertEqual(json.loads(call['body'])['model'], 'm1')

    def test_jev_refuses_object_state_before_any_call(self):
        self.write_rows([self.row('ok1', 'normal'), self.row('obj', {'context': 'a', 'answer': 'b'})])
        result = self.batch('jev', '--model', 'm1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('vela', result.stderr)
        self.assertEqual(self.mock.calls, [])
        self.assertFalse(self.output.exists())

    def test_jev_refuses_span_question_before_any_call(self):
        row = self.row('span', 'normal')
        row['questions'] = {'s': {'type': 'span', 'criteria': ['city']}}
        self.write_rows([row])
        result = self.batch('jev', '--model', 'm1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('span', result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_jev_refuses_upstream_prefixed_model_before_any_call(self):
        self.write_rows([self.row('pre', 'normal', model='jev-vllm-dr/some-model')])
        result = self.batch('jev')
        self.assertEqual(result.returncode, 2)
        self.assertIn('upstream prefix', result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_vela_accepts_object_state_and_span_question(self):
        row = self.row('v1', {'context': 'Paris is in France.', 'answer': 'Paris is in Germany.'})
        row['questions'] = {'s': {'type': 'span', 'criteria': ['unsupported']}}
        self.write_rows([row])
        result = self.batch('vela', '--model', 'vm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output_rows()[0]['ok'])
        call, = self.posts('/tool/vela/systemone')
        body = json.loads(call['body'])
        self.assertEqual(body['state'], {'context': 'Paris is in France.', 'answer': 'Paris is in Germany.'})

    def test_vela_needs_a_model(self):
        self.write_rows([self.row('v2', 'normal')])
        result = self.batch('vela')
        self.assertEqual(result.returncode, 2)
        self.assertIn('model', result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_429_is_retried_after_retry_after(self):
        self.write_rows([self.row('r1', 'retry')])
        result = self.batch('jev', '--model', 'm1', '--attempts', '2', '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertEqual(row['attempts'], 2)
        self.assertIn('429 responses 1', result.stderr)

    def test_model_warming_503_is_retried(self):
        self.write_rows([self.row('w1', 'warming')])
        result = self.batch('jev', '--model', 'm1', '--attempts', '2', '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows()[0]['attempts'], 2)

    def test_504_is_not_replayed(self):
        self.write_rows([self.row('late', 'fail504')])
        result = self.batch('jev', '--model', 'm1', '--attempts', '4', '-j', '1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.output_rows()[0]['attempts'], 1)
        self.assertEqual(len(self.posts('/tool/jev/systemone')), 1)

    def test_resume_skips_successful_ids(self):
        self.write_rows([self.row('once', 'normal')])
        first = self.batch('jev', '--model', 'm1')
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.batch('jev', '--model', 'm1')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('1 already done, 0 to run', second.stderr)
        self.assertEqual(len(self.posts('/tool/jev/systemone')), 1)
        self.assertEqual(len(self.output_rows()), 1)

    def test_stdin_input_and_dry_run_make_no_call(self):
        preview = self.run_tool('jev', '--in', '-', '--out', str(self.output), '--model', 'm1', '--dry-run',
                                stdin=json.dumps(self.row('d1', 'normal')) + '\n')
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn('"model": "m1"', preview.stdout)
        self.assertEqual(self.mock.calls, [])
        self.assertFalse(self.output.exists())

    def test_tool_option_on_wrong_tool_is_an_argument_error(self):
        result = self.run_tool('jev', '--file', 'x.pdf', '--out', str(self.output), '--in', str(self.input))
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments', result.stderr)

    # read-only queries and secrets

    def test_list_schema_and_health_print_json(self):
        listed = self.run_tool('--list')
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('"id": "jev"', listed.stdout)
        schema = self.run_tool('jev', '--schema')
        self.assertEqual(schema.returncode, 0, schema.stderr)
        self.assertIn('"tool": "jev"', schema.stdout)
        health = self.run_tool('jev', '--health')
        self.assertIn('"status": "ok"', health.stdout)
        self.assertNotIn(KEY, listed.stdout + schema.stdout + health.stdout)

    def test_list_models_error_body_is_redacted(self):
        result = self.run_tool('jev', '--list-models')
        self.assertEqual(result.returncode, 1)
        self.assertIn('[REDACTED]', result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)

    def test_error_text_written_to_output_is_redacted(self):
        self.write_rows([self.row('leak', 'leaky')])
        result = self.batch('jev', '--model', 'm1', '--attempts', '1')
        self.assertEqual(result.returncode, 1)
        text = self.output.read_text(encoding='utf-8')
        self.assertIn('[REDACTED]', text)
        self.assertNotIn(KEY, text + result.stdout + result.stderr)

    # mineru

    def test_mineru_submit_poll_and_download_zip(self):
        pdf = self.make_file('paper.pdf')
        self.write_rows([{'id': 'p1', 'file': pdf, 'output_formats': ['markdown', 'zip']}])
        outdir = self.root / 'results'
        result = self.batch('mineru', '--out-dir', str(outdir), '--poll-interval', '0.01')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual(row['task_status'], 'completed')
        self.assertTrue(row['task_id'])
        self.assertEqual(row['result_file'], str(outdir / 'p1.zip'))
        self.assertTrue((outdir / 'p1.zip').read_bytes().startswith(b'PK'))
        with zipfile.ZipFile(outdir / 'p1.zip') as z:
            self.assertEqual(z.read('content.md'), b'# parsed paper.pdf')
        submit, = self.posts('/tool/mineru/tasks')
        self.assertEqual(submit['ctype'], 'application/octet-stream')
        self.assertEqual(submit['body'], Path(pdf).read_bytes())
        self.assertEqual(submit['query']['filename'], ['paper.pdf'])
        self.assertEqual(submit['query']['tier'], ['advanced'])
        self.assertEqual(submit['query']['output_formats'], ['markdown', 'zip'])
        gets = [c for c in self.mock.calls if c['method'] == 'GET']
        self.assertTrue(any(c['query'].get('response_format') == ['zip'] for c in gets))
        self.assertTrue(all(c['auth'] == 'Bearer ' + KEY for c in self.mock.calls))

    def test_mineru_without_zip_downloads_json(self):
        pdf = self.make_file('doc.png', b'\x89PNG unit')
        self.write_rows([{'id': 'j2', 'file': pdf}])
        outdir = self.root / 'results'
        result = self.batch('mineru', '--out-dir', str(outdir), '--poll-interval', '0.01')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output_rows()[0]['result_file'], str(outdir / 'j2.json'))
        self.assertEqual(json.loads((outdir / 'j2.json').read_text(encoding='utf-8'))['task_id'],
                         self.output_rows()[0]['task_id'])
        gets = [c for c in self.mock.calls if c['method'] == 'GET' and c['path'].endswith('/result')]
        self.assertEqual(len(gets), 1)
        self.assertNotIn('response_format', gets[0]['query'])

    def test_mineru_failed_task_is_recorded(self):
        self.write_rows([{'id': 'f1', 'file': self.make_file('fail.pdf')}])
        result = self.batch('mineru', '--out-dir', str(self.root / 'r'), '--poll-interval', '0.01')
        self.assertEqual(result.returncode, 1)
        row, = self.output_rows()
        self.assertFalse(row['ok'])
        self.assertEqual(row['task_status'], 'failed')
        self.assertIn('task failed', row['error'])

    def test_mineru_poll_past_timeout_is_recorded(self):
        self.write_rows([{'id': 'n1', 'file': self.make_file('never.pdf')}])
        result = self.batch('mineru', '--out-dir', str(self.root / 'r'), '--poll-interval', '0.02',
                            '--timeout', '0.2')
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.output_rows()[0]['error'].startswith('timeout'))

    def test_mineru_submit_429_is_retried(self):
        self.write_rows([{'id': 'rt', 'file': self.make_file('retry-once.pdf')}])
        result = self.batch('mineru', '--out-dir', str(self.root / 'r'), '--poll-interval', '0.01')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.posts('/tool/mineru/tasks')), 2)
        self.assertTrue(self.output_rows()[0]['ok'])

    def test_mineru_file_over_200_mib_is_refused_before_any_call(self):
        big = self.root / 'big.pdf'
        with open(big, 'wb') as f:
            f.truncate(201 << 20)
        self.write_rows([{'id': 'big', 'file': str(big)}])
        result = self.batch('mineru', '--out-dir', str(self.root / 'r'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('200 MiB', result.stderr)
        self.assertEqual(self.mock.calls, [])

    def test_mineru_command_line_files_and_argument_checks(self):
        a = self.make_file('one.pdf')
        b = self.make_file('two.pdf')
        outdir = self.root / 'cli'
        missing_dir = self.run_tool('mineru', '--file', a, '--out', str(self.output))
        self.assertEqual(missing_dir.returncode, 2)
        self.assertIn('--out-dir', missing_dir.stderr)
        both = self.run_tool('mineru', '--in', str(self.input), '--file', a, '--out', str(self.output),
                            '--out-dir', str(outdir))
        self.assertEqual(both.returncode, 2)
        self.assertIn('not both', both.stderr)
        ok = self.run_tool('mineru', '--file', a, '--file', b, '--out', str(self.output),
                           '--out-dir', str(outdir), '--poll-interval', '0.01')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(sorted(r['id'] for r in self.output_rows()), ['one.pdf', 'two.pdf'])
        self.assertTrue((outdir / 'one.pdf.json').exists())

    def test_mineru_duplicate_command_line_names_are_refused(self):
        a = self.make_file('dup.pdf')
        sub = self.root / 'sub'
        sub.mkdir()
        b = str(sub / 'dup.pdf')
        Path(b).write_bytes(b'x')
        result = self.run_tool('mineru', '--file', a, '--file', b, '--out', str(self.output),
                               '--out-dir', str(self.root / 'o'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('duplicate id', result.stderr)
        self.assertEqual(self.mock.calls, [])


    # oxr

    def test_oxr_multipart_fields_and_server_default_elements(self):
        pdf = self.make_file('report.pdf', b'%PDF-1.7 oxr bytes\n')
        outdir = self.root / 'oxr'
        result = self.run_tool('oxr', '--file', pdf, '--out', str(self.output), '--out-dir', str(outdir))
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.posts('/tool/oxr/parse/sync')
        self.assertTrue(call['ctype'].startswith('multipart/form-data; boundary='))
        self.assertEqual(call['auth'], 'Bearer ' + KEY)
        self.assertNotIn(KEY.encode('utf-8'), call['body'])
        parts = parse_multipart(call['ctype'], call['body'])
        names = [p['name'] for p in parts]
        self.assertEqual(names, ['file', 'output_formats', 'output_formats', 'api_key'])
        self.assertEqual(parts[0]['filename'], 'report.pdf')
        self.assertEqual(parts[0]['value'], b'%PDF-1.7 oxr bytes\n')
        self.assertEqual([p['value'] for p in parts if p['name'] == 'output_formats'],
                         [b'markdown', b'json'])
        self.assertEqual(parts[-1]['value'], b'unused')
        self.assertNotIn('element_formats', names)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual((row['status'], row['code'], row['pages'], row['processing_time_ms']),
                         (200, 0, 2, 1234))

    def test_oxr_element_formats_pass_through_and_row_override(self):
        a = self.make_file('cli.pdf')
        b = self.make_file('row.pdf')
        self.write_rows([{'id': 'inherit', 'file': a},
                         {'id': 'own', 'file': b, 'element_formats': {'image': 'none'}}])
        result = self.batch('oxr', '--element-formats', '{"table": "markdown"}', '-j', '1',
                            '--out-dir', str(self.root / 'o'))
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = {}
        for call in self.posts('/tool/oxr/parse/sync'):
            parts = parse_multipart(call['ctype'], call['body'])
            name = next(p['filename'] for p in parts if p['name'] == 'file')
            fields = [p for p in parts if p['name'] == 'element_formats']
            sent[name] = json.loads(fields[0]['value']) if fields else None
        self.assertEqual(sent, {'cli.pdf': {'table': 'markdown'}, 'row.pdf': {'image': 'none'}})

    def test_oxr_code_1133_is_recorded_as_failure(self):
        self.write_rows([{'id': 'bad1', 'file': self.make_file('bad.pdf')}])
        outdir = self.root / 'o'
        result = self.batch('oxr', '--out-dir', str(outdir))
        self.assertEqual(result.returncode, 1)
        row, = self.output_rows()
        self.assertFalse(row['ok'])
        self.assertEqual((row['status'], row['code']), (200, 1133))
        self.assertIn('unsupported', row['message'])
        self.assertIn('code 1133', row['error'])
        self.assertFalse((outdir / 'bad1.md').exists())

    def test_oxr_503_model_warming_is_retried(self):
        self.write_rows([{'id': 'w1', 'file': self.make_file('warm.pdf')}])
        result = self.batch('oxr', '--out-dir', str(self.root / 'o'), '--attempts', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(len(self.posts('/tool/oxr/parse/sync')), 2)

    def test_oxr_writes_md_and_json_by_requested_formats(self):
        self.write_rows([{'id': 'doc', 'file': self.make_file('doc.pdf')},
                         {'id': 'jonly', 'file': self.make_file('jonly.pdf'), 'output_formats': ['json']}])
        outdir = self.root / 'o'
        result = self.batch('oxr', '--out-dir', str(outdir), '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = {r['id']: r for r in self.output_rows()}
        self.assertEqual(rows['doc']['markdown_file'], str(outdir / 'doc.md'))
        self.assertEqual((outdir / 'doc.md').read_text(encoding='utf-8'), '# parsed doc.pdf')
        saved = json.loads((outdir / 'doc.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['code'], 0)
        self.assertEqual(saved['data']['result']['outputs']['markdown'], '# parsed doc.pdf')
        self.assertEqual(rows['doc']['json_file'], str(outdir / 'doc.json'))
        self.assertTrue((outdir / 'jonly.json').exists())
        self.assertFalse((outdir / 'jonly.md').exists())
        self.assertIsNone(rows['jonly']['markdown_file'])

    def test_oxr_refusals_happen_before_any_call(self):
        pdf = self.make_file('r.pdf')
        no_dir = self.run_tool('oxr', '--file', pdf, '--out', str(self.output))
        self.assertEqual(no_dir.returncode, 2)
        self.assertIn('--out-dir', no_dir.stderr)
        bad_json = self.run_tool('oxr', '--file', pdf, '--out', str(self.output),
                                 '--out-dir', str(self.root / 'o'), '--element-formats', '{not json')
        self.assertEqual(bad_json.returncode, 2)
        self.assertIn('--element-formats', bad_json.stderr)
        self.write_rows([{'id': 'zip1', 'file': pdf, 'output_formats': ['zip']}])
        bad_format = self.batch('oxr', '--out-dir', str(self.root / 'o'))
        self.assertEqual(bad_format.returncode, 2)
        self.assertIn('output_formats', bad_format.stderr)
        big = self.root / 'big.pdf'
        with open(big, 'wb') as f:
            f.truncate(201 << 20)
        self.write_rows([{'id': 'big', 'file': str(big)}])
        too_big = self.batch('oxr', '--out-dir', str(self.root / 'o'))
        self.assertEqual(too_big.returncode, 2)
        self.assertIn('200 MiB', too_big.stderr)
        self.assertEqual(self.mock.calls, [])
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
