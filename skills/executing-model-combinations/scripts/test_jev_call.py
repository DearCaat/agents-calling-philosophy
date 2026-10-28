#!/usr/bin/env python3
"""Offline tests for jev-call.py using a local HTTP mock only."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT = Path(__file__).with_name('jev-call.py')
PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j'
    'KloAAAAASUVORK5CYII=')


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        self.records = []
        self.paths = []
        self.counts = {}
        self.active = 0
        self.max_active = 0


def start_mock():
    state = MockState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_json(self, status, obj, extra_headers=None):
            payload = json.dumps(obj).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = json.loads(self.rfile.read(length))
            sample = body['state']
            with state.lock:
                state.records.append((body, self.headers.get('Authorization')))
                state.paths.append(self.path)
                state.counts[sample] = state.counts.get(sample, 0) + 1
                count = state.counts[sample]
                state.active += 1
                state.max_active = max(state.max_active, state.active)
            try:
                if sample == 'retry' and count == 1:
                    payload = b'<html>busy</html>'
                    self.send_response(429)
                    self.send_header('Retry-After', '0')
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                elif sample == 'warming' and count == 1:
                    self.send_json(503, {'code': 'model_warming'}, {'Retry-After': '0'})
                elif sample == 'fail504':
                    self.send_json(504, {'error': 'processing deadline'})
                elif sample == 'fail422':
                    self.send_json(422, {'error': 'nonempty state and questions are required'})
                else:
                    if sample.startswith('parallel-'):
                        time.sleep(0.12)
                    self.send_json(200, {
                        'model': 'mock-model',
                        'answers': {'q': {'type': 'noul', 'noul': 0.75}},
                        'usage': {'input_tokens': 1, 'output_tokens': 0},
                    })
            finally:
                with state.lock:
                    state.active -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state


class JevCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jev-call-test-')
        self.root = Path(self.tmp.name)
        self.server, self.server_thread, self.state = start_mock()
        self.local = self.root / 'local'
        self.local.mkdir()
        self.key_file = self.root / 'mock-key'
        self.key_file.write_text('unit-test-key\n', encoding='utf-8')
        base = 'http://127.0.0.1:%d/v1' % self.server.server_address[1]
        tool_base = 'http://127.0.0.1:%d/tool' % self.server.server_address[1]
        (self.local / 'api-routes.tsv').write_text(
            'api_id\tbase_url\twire_protocol\tcredential_ref\tmodels_path\tauth_scheme\tstate\tnotes\n'
            'jev\t%s\tjev-systemone\tfile:%s\t-\tbearer\tavailable\tlocal mock\n'
            'tool\t%s\tcpa-tool\tfile:%s\t/\tbearer\tavailable\tlocal mock\n' %
            (base, self.key_file, tool_base, self.key_file), encoding='utf-8')
        self.env = dict(os.environ)
        self.env['AGENTS_LOCAL_ROOT'] = str(self.local)
        self.env.pop('HTTP_PROXY', None)
        self.env.pop('HTTPS_PROXY', None)
        self.env.pop('http_proxy', None)
        self.env.pop('https_proxy', None)
        self.input = self.root / 'input.jsonl'
        self.output = self.root / 'output.jsonl'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.tmp.cleanup()

    def write_rows(self, rows):
        self.input.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def invoke(self, *extra):
        if '--model' not in extra:
            extra += ('--model', 'test-model')
        return subprocess.run(
            [sys.executable, str(SCRIPT), '--api', 'jev', '--in', str(self.input),
             '--out', str(self.output), *extra],
            env=self.env, text=True, capture_output=True, timeout=15)

    def output_rows(self):
        if not self.output.exists():
            return []
        return [json.loads(line) for line in self.output.read_text(encoding='utf-8').splitlines()]

    @staticmethod
    def row(item_id, state):
        return {
            'id': item_id,
            'state': state,
            'questions': {'q': {'type': 'noul', 'instructions': 'Is it true?',
                                'criteria': {'true': 'yes', 'false': 'no'}}},
        }

    def test_429_retry_honors_retry_after_and_records_count(self):
        self.write_rows([self.row('r1', 'retry')])
        result = self.invoke('--attempts', '2', '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(row['status'], 200)
        self.assertIn('429 responses 1', result.stderr)
        self.assertEqual(self.state.counts['retry'], 2)
        self.assertTrue(all(auth == 'Bearer unit-test-key' for _, auth in self.state.records))
        self.assertNotIn('unit-test-key', result.stdout + result.stderr)

    def test_model_warming_503_is_retried(self):
        self.write_rows([self.row('warm', 'warming')])
        result = self.invoke('--attempts', '2', '-j', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        row, = self.output_rows()
        self.assertTrue(row['ok'])
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(self.state.counts['warming'], 2)

    def test_504_is_not_replayed_and_422_fails_immediately(self):
        self.write_rows([self.row('late', 'fail504'), self.row('bad', 'fail422')])
        result = self.invoke('--attempts', '4', '-j', '1')
        self.assertEqual(result.returncode, 1)
        rows = {row['id']: row for row in self.output_rows()}
        self.assertEqual(rows['late']['status'], 504)
        self.assertEqual(rows['late']['attempts'], 1)
        self.assertEqual(rows['bad']['status'], 422)
        self.assertEqual(rows['bad']['attempts'], 1)
        self.assertEqual(self.state.counts, {'fail504': 1, 'fail422': 1})

    def test_successful_id_is_skipped_on_resume(self):
        self.write_rows([self.row('once', 'normal')])
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('1 already done, 0 to run', second.stderr)
        self.assertEqual(self.state.counts['normal'], 1)
        self.assertEqual(set(self.state.paths), {'/tool/jev/systemone'})
        self.assertEqual(len(self.output_rows()), 1)

    def test_parallel_requests_overlap(self):
        self.write_rows([self.row('p%d' % i, 'parallel-%d' % i) for i in range(4)])
        result = self.invoke('-j', '4')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.output_rows()), 4)
        self.assertGreaterEqual(self.state.max_active, 2)

    def test_upstream_prefixed_model_is_refused_before_any_call(self):
        row = self.row('prefixed', 'normal')
        row['model'] = 'jev-vllm-dr/vllm-sr/d3-ultra'
        self.write_rows([row])
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('upstream prefix', result.stderr)
        self.assertEqual(self.state.records, [])

    def test_image_file_becomes_base64_data_uri(self):
        image = self.root / 'one.png'
        image.write_bytes(PNG_1X1)
        row = self.row('with-image', 'normal')
        row['images'] = [str(image)]
        self.write_rows([row])
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        body, _ = self.state.records[0]
        uri, = body['images']
        self.assertTrue(uri.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(uri.split(',', 1)[1]), PNG_1X1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
