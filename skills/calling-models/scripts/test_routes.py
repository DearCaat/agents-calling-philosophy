"""Tests for routes.py (route registry source, credentials location), agents-query.py read_table and _run,
and the llm-call.py refusal of a bad --base-url.

No network: the route tests replace agents-query's read_table with a fake, and the read_table tests
replace agents_query._run and time.monotonic. Only the _run tests start real processes (POSIX only).
AGENTS_* variables, HOME and XDG_CACHE_HOME point into a temporary directory. Credential values in
test data are placeholders (x, old, new, PLACEHOLDERSECRET), never real tokens.
"""
import contextlib
import importlib.util
import inspect
import json
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


routes = _load('routes_under_test', 'routes.py')
agents_query = _load('agents_query_under_test', 'agents-query.py')

COMMIT = 'abc1234' + 'f' * 33
OLD_COMMIT = '0987654' + 'e' * 33
HEADER = 'api_id\tbase\twire\tcredential_ref\tcheck\tauth\tstate\tnote\n'
REMOTE_ROW = 'tool\thttp://127.0.0.1:8317/tool\tcpa-tool\tCPA_KEY\t-\tbearer\tavailable\tfrom remote\n'
LOCAL_ROW = 'tool\thttp://127.0.0.1:9999/tool\tcpa-tool\tCPA_KEY\t-\tbearer\tavailable\tfrom local\n'
CACHED_ROW = 'tool\thttp://127.0.0.1:7777/tool\tcpa-tool\tCPA_KEY\t-\tbearer\tavailable\tfrom cache\n'
REMOTE_TABLE = '# routes\n' + REMOTE_ROW
LOCAL_TABLE = '# routes\n' + LOCAL_ROW
CACHED_TABLE = '# routes\n' + CACHED_ROW
TRUNCATED_ROW = 'tool\thttp://127.0.0.1:8317/tool\n'  # two of the seven required columns
COMMENT_ONLY = '# no route rows here\n'
NOTE_COLUMN = 7  # the notes column of an api-routes.tsv row
NO_DATA_ROW = 'no data row or a row without the 7 required columns'
INCOMPLETE_CACHE = 'incomplete or without a usable table'


def seven_columns(first):
    """A data row with exactly seven columns and the given api_id (first column)."""
    return '\t'.join([first, 'http://127.0.0.1:9999/tool', 'cpa-tool', 'CPA_KEY', '-', 'bearer', 'available']) + '\n'


class FakeQuery:
    """Stands in for the agents-query module that routes.py loads; read_table is the mock."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.deadlines = []

    def read_table(self, path, deadline=None):
        self.calls.append(path)
        self.deadlines.append(deadline)
        if self.error is not None:
            raise self.error
        return self.result


def remote_returns(fake):
    return mock.patch.object(routes, '_query_module', return_value=fake)


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.script_dir = str(self.root / 'skill' / 'scripts')
        os.makedirs(self.script_dir)
        self.cache_dir = self.root / 'cache'
        self.cache_file = self.cache_dir / 'api-routes.json'
        patch = mock.patch.dict(os.environ, {
            'HOME': str(self.home),
            'XDG_CACHE_HOME': str(self.root / 'xdg'),
            'AGENTS_CACHE_DIR': str(self.cache_dir),
        }, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_remote(self, **kwargs):
        fake = FakeQuery(**kwargs)
        patch = remote_returns(fake)
        patch.start()
        self.addCleanup(patch.stop)
        return fake

    def put(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def put_cache(self, text, commit, age_seconds=0):
        """Write the cache record; fetched_at is age_seconds in the past (negative: in the future)."""
        rec = {'commit': commit, 'text': text, 'fetched_at': time.time() - age_seconds}
        self.put(self.cache_file, json.dumps(rec))
        return rec

    def cache_record(self):
        return json.loads(self.cache_file.read_text(encoding='utf-8'))

    def local_table_path(self):
        os.environ['AGENTS_DATA_ROOT'] = str(self.root / 'data')
        return self.root / 'data' / 'local' / 'api-routes.tsv'


class RouteRegistryTests(RouteTestCase):
    def test_local_table_gives_no_note(self):
        self.put(self.local_table_path(), LOCAL_TABLE)
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertIsNone(note)
        self.assertEqual(fake.calls, [])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from local')

    def test_local_table_with_header_and_valid_row_is_usable(self):
        self.put(self.local_table_path(), HEADER + LOCAL_ROW)
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertIsNone(note)
        self.assertEqual(fake.calls, [])
        self.assertEqual(rows[0][0], 'api_id')  # the header is kept as a row but is not counted as data
        self.assertEqual(rows[1][NOTE_COLUMN], 'from local')

    def test_local_table_without_usable_row_raises_and_does_not_fall_back(self):
        self.put(self.local_table_path(), COMMENT_ONLY)
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn(NO_DATA_ROW, str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_local_header_only_table_raises(self):
        self.put(self.local_table_path(), HEADER)
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn(NO_DATA_ROW, str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_local_table_with_truncated_row_raises_without_remote(self):
        self.put(self.local_table_path(), LOCAL_TABLE + TRUNCATED_ROW)
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn(NO_DATA_ROW, str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_local_table_with_a_blank_api_id_row_raises_without_remote(self):
        # A good row, then one seven-column row whose api_id is empty or only spaces: the whole table is refused.
        path = self.local_table_path()
        for first in ['', '   ']:
            with self.subTest(api_id=first):
                self.put(path, LOCAL_TABLE + seven_columns(first))
                fake = FakeQuery(result=(COMMIT, REMOTE_TABLE))
                with remote_returns(fake):
                    with self.assertRaises(RuntimeError) as caught:
                        routes.route_rows(self.script_dir)
                self.assertIn(NO_DATA_ROW, str(caught.exception))
                self.assertEqual(fake.calls, [])

    def test_local_table_that_cannot_be_read_raises_and_does_not_fall_back(self):
        # A directory where the file should be: open() raises IsADirectoryError, an OSError.
        self.local_table_path().mkdir(parents=True)
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('not readable', str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_local_table_with_invalid_utf8_raises_without_remote(self):
        path = self.local_table_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b'\xff\xfe\x00')
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('not readable', str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_dangling_symlink_raises_and_does_not_read_the_cache(self):
        path = self.local_table_path()
        path.parent.mkdir(parents=True)
        os.symlink(self.root / 'missing-target.tsv', path)
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('link target missing', str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_fresh_cache_is_read_without_remote(self):
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertTrue(note.startswith('route registry from cache'))
        self.assertIn('agents-local@abc1234', note)
        self.assertNotIn('STALE', note)

    def test_age_comes_from_fetched_at_not_file_mtime(self):
        old = time.time() - 3 * routes.CACHE_TTL
        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=60)
        os.utime(self.cache_file, (old, old))  # old mtime, fresh fetched_at: still fresh
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        rows, _ = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')

        self.put_cache(CACHED_TABLE, COMMIT, age_seconds=2 * routes.CACHE_TTL)  # fresh mtime, old fetched_at: stale
        rows, _ = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')

    def test_remote_read_gets_monotonic_discovery_deadline(self):
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        before = time.monotonic()
        routes.route_rows(self.script_dir)
        after = time.monotonic()
        self.assertEqual(len(fake.deadlines), 1)
        self.assertGreaterEqual(fake.deadlines[0], before + routes.DISCOVERY_TIMEOUT)
        self.assertLessEqual(fake.deadlines[0], after + routes.DISCOVERY_TIMEOUT)

    def test_no_cache_fetches_remote_and_writes_one_json_record(self):
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertTrue(note.startswith('route registry from agents-local@abc1234'))
        self.assertNotIn('STALE', note)
        self.assertIn('cached to', note)
        self.assertNotIn('replaced the bad cache', note)
        self.assertEqual(sorted(os.listdir(self.cache_dir)), ['api-routes.json'])
        rec = self.cache_record()
        self.assertEqual(rec['commit'], COMMIT)
        self.assertEqual(rec['text'], REMOTE_TABLE)
        self.assertIsInstance(rec['fetched_at'], float)
        self.assertLess(abs(rec['fetched_at'] - time.time()), 60)

    def test_stale_cache_is_used_when_remote_fails_and_is_not_rewritten(self):
        before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
        fake = self.fake_remote(error=subprocess.CalledProcessError(128, ['git', 'fetch']))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertIn('STALE', note)
        self.assertIn('unreadable', note)
        self.assertEqual(self.cache_record(), before)

    def test_remote_without_usable_row_uses_stale_cache_and_does_not_overwrite_it(self):
        before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
        fake = self.fake_remote(result=(COMMIT, COMMENT_ONLY))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertIn('STALE', note)
        self.assertIn(NO_DATA_ROW, note)
        self.assertEqual(self.cache_record(), before)

    def test_remote_header_only_table_uses_stale_cache_and_does_not_overwrite_it(self):
        before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
        fake = self.fake_remote(result=(COMMIT, HEADER))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertIn('STALE', note)
        self.assertIn(NO_DATA_ROW, note)
        self.assertEqual(self.cache_record(), before)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])

    def test_remote_table_with_truncated_row_uses_stale_cache_and_does_not_overwrite_it(self):
        before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE + TRUNCATED_ROW))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertIn('STALE', note)
        self.assertIn(NO_DATA_ROW, note)
        self.assertEqual(self.cache_record(), before)

    def test_remote_blank_api_id_row_uses_stale_cache_and_does_not_overwrite_it(self):
        for first in ['', '   ']:
            with self.subTest(api_id=first):
                before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
                fake = FakeQuery(result=(COMMIT, REMOTE_TABLE + seven_columns(first)))
                with remote_returns(fake):
                    rows, note = routes.route_rows(self.script_dir)
                self.assertEqual(fake.calls, [routes.ROUTES_PATH])
                self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
                self.assertIn('STALE', note)
                self.assertIn(NO_DATA_ROW, note)
                self.assertEqual(self.cache_record(), before)

    def test_remote_without_usable_row_and_no_cache_raises_and_writes_nothing(self):
        fake = self.fake_remote(result=(COMMIT, COMMENT_ONLY))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn(NO_DATA_ROW, str(caught.exception))
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertFalse(self.cache_file.exists())

    def test_corrupt_cache_counts_as_missing_and_is_reported_if_remote_fails(self):
        cases = {
            'not JSON': ('not json', 'unreadable (JSONDecodeError)'),
            'JSON but not an object': ('[]', INCOMPLETE_CACHE),
            'missing fetched_at': (json.dumps({'commit': COMMIT, 'text': CACHED_TABLE}), INCOMPLETE_CACHE),
            'missing commit': (json.dumps({'text': CACHED_TABLE, 'fetched_at': time.time()}), INCOMPLETE_CACHE),
            'text without a usable row': (
                json.dumps({'commit': COMMIT, 'text': COMMENT_ONLY, 'fetched_at': time.time()}), INCOMPLETE_CACHE),
            'text not a string': (json.dumps({'commit': COMMIT, 'text': 5, 'fetched_at': time.time()}),
                                  INCOMPLETE_CACHE),
        }
        for name, (payload, problem) in cases.items():
            with self.subTest(name):
                self.put(self.cache_file, payload)
                fake = FakeQuery(error=subprocess.CalledProcessError(128, ['git', 'fetch']))
                with remote_returns(fake):
                    with self.assertRaises(RuntimeError) as caught:
                        routes.route_rows(self.script_dir)
                self.assertEqual(fake.calls, [routes.ROUTES_PATH])
                self.assertIn(problem, str(caught.exception))
                self.assertIn(str(self.cache_file), str(caught.exception))

    def test_corrupt_cache_is_replaced_by_remote_copy_and_the_note_says_so(self):
        self.put(self.cache_file, 'not json')
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertIn('cached to', note)
        self.assertIn('replaced the bad cache', note)
        self.assertEqual(self.cache_record()['commit'], COMMIT)

    def test_bad_commit_counts_as_corrupt(self):
        for commit in ['       ', 'xyz1234', 'abc12', 'a' * 65]:
            with self.subTest(commit=commit):
                self.put(self.cache_file, json.dumps({'commit': commit, 'text': CACHED_TABLE,
                                                      'fetched_at': time.time()}))
                fake = FakeQuery(result=(OLD_COMMIT, REMOTE_TABLE))
                with remote_returns(fake):
                    rows, note = routes.route_rows(self.script_dir)
                self.assertEqual(fake.calls, [routes.ROUTES_PATH])
                self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
                self.assertIn('replaced the bad cache', note)

    def test_bad_fetched_at_counts_as_corrupt(self):
        # Values json reads but a fetched_at cannot be: NaN and Infinity, booleans, an integer too large
        # for a float (must not raise OverflowError), and a numeric string.
        literals = {
            'NaN': 'NaN', 'Infinity': 'Infinity', 'true': 'true', 'false': 'false',
            '10**400': str(10 ** 400), 'string': '"1700000000"',
        }
        for name, literal in literals.items():
            payload = '{"commit": "%s", "text": %s, "fetched_at": %s}' % (COMMIT, json.dumps(CACHED_TABLE), literal)
            with self.subTest(name + ', remote succeeds'):
                self.put(self.cache_file, payload)
                fake = FakeQuery(result=(OLD_COMMIT, REMOTE_TABLE))
                with remote_returns(fake):
                    rows, note = routes.route_rows(self.script_dir)
                self.assertEqual(fake.calls, [routes.ROUTES_PATH])
                self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
                self.assertIn('replaced the bad cache', note)
            with self.subTest(name + ', remote fails'):
                self.put(self.cache_file, payload)
                fake = FakeQuery(error=subprocess.CalledProcessError(128, ['git', 'fetch']))
                with remote_returns(fake):
                    with self.assertRaises(RuntimeError) as caught:
                        routes.route_rows(self.script_dir)
                self.assertIn(INCOMPLETE_CACHE, str(caught.exception))
                self.assertIn(str(self.cache_file), str(caught.exception))

    def test_future_fetched_at_is_not_fresh_and_remote_is_read(self):
        before = self.put_cache(CACHED_TABLE, COMMIT, age_seconds=-600)
        fake = self.fake_remote(result=(OLD_COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertNotIn('STALE', note)
        self.assertEqual(self.cache_record()['commit'], OLD_COMMIT)
        self.assertNotEqual(self.cache_record(), before)

    def test_future_fetched_at_is_used_as_stale_copy_when_remote_fails(self):
        before = self.put_cache(CACHED_TABLE, COMMIT, age_seconds=-600)
        fake = self.fake_remote(error=subprocess.CalledProcessError(128, ['git', 'fetch']))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from cache')
        self.assertIn('STALE', note)
        self.assertIn('later than the current clock', note)
        self.assertEqual(self.cache_record(), before)

    def test_unwritable_cache_still_returns_remote_result_and_says_not_cached(self):
        # A regular file in place of the cache directory makes every cache write fail, whoever runs the test.
        blocker = self.root / 'blocker'
        blocker.write_text('not a directory', encoding='utf-8')
        os.environ['AGENTS_CACHE_DIR'] = str(blocker / 'cache')
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertTrue(note.startswith('route registry from agents-local@abc1234'))
        self.assertIn('not cached', note)
        self.assertNotIn('cached to', note)

    def test_failed_write_keeps_the_old_cache_and_leaves_no_temporary_file(self):
        before = self.put_cache(CACHED_TABLE, OLD_COMMIT, age_seconds=2 * routes.CACHE_TTL)
        fake = self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        with mock.patch.object(os, 'replace', side_effect=OSError('disk full')):
            rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(fake.calls, [routes.ROUTES_PATH])
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertIn('not cached', note)
        self.assertNotIn('cached to', note)
        self.assertEqual(self.cache_record(), before)
        self.assertEqual(sorted(os.listdir(self.cache_dir)), ['api-routes.json'])

    def test_failed_write_over_a_bad_cache_does_not_claim_it_was_replaced(self):
        self.put(self.cache_file, 'not json')
        self.fake_remote(result=(COMMIT, REMOTE_TABLE))
        with mock.patch.object(os, 'replace', side_effect=OSError('disk full')):
            rows, note = routes.route_rows(self.script_dir)
        self.assertEqual(rows[0][NOTE_COLUMN], 'from remote')
        self.assertIn('not cached', note)
        self.assertNotIn('replaced the bad cache', note)  # the bad file is still there
        self.assertEqual(self.cache_file.read_text(encoding='utf-8'), 'not json')

    def test_permission_failure_message_names_transport_and_credential_split(self):
        self.fake_remote(error=subprocess.CalledProcessError(128, ['git', 'fetch'], stderr='Permission denied'))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        message = str(caught.exception)
        self.assertIn('SSH or https', message)
        self.assertIn('separate from the gateway bearer credential', message)

    def test_timeout_failure_message_says_timed_out(self):
        self.fake_remote(error=subprocess.TimeoutExpired(['git', 'fetch'], 15))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('timed out', str(caught.exception))

    def test_missing_file_in_the_fetched_commit_is_reported_as_missing(self):
        self.fake_remote(error=agents_query.MissingFile('该提交里没有这个文件。'))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('route registry is not readable', str(caught.exception))
        self.assertIn('is missing from the repository', str(caught.exception))
        self.assertNotIn('from the fetched commit', str(caught.exception))

    def test_other_runtime_error_from_the_fetched_commit_is_reported_as_unreadable(self):
        self.fake_remote(error=RuntimeError('无法读取该提交中的文件。'))
        with self.assertRaises(RuntimeError) as caught:
            routes.route_rows(self.script_dir)
        self.assertIn('cannot read local/api-routes.tsv from the fetched commit', str(caught.exception))
        self.assertNotIn('is missing from the repository', str(caught.exception))


class DiscoveryTimeoutEnvTests(unittest.TestCase):
    """AGENTS_ROUTE_DISCOVERY_TIMEOUT is read when routes.py is imported, so each probe loads it again."""

    def load_with(self, value):
        with mock.patch.dict(os.environ, {'AGENTS_ROUTE_DISCOVERY_TIMEOUT': value}):
            return _load('routes_timeout_probe', 'routes.py')

    def test_positive_value_is_used(self):
        self.assertEqual(self.load_with('7').DISCOVERY_TIMEOUT, 7.0)

    def test_non_finite_zero_negative_or_unparsable_values_fall_back_to_fifteen(self):
        for value in ['nan', 'inf', '-inf', '1e400', '-1', '0', 'abc', '']:
            with self.subTest(value=value):
                self.assertEqual(self.load_with(value).DISCOVERY_TIMEOUT, 15.0)


class CredentialsFileTests(RouteTestCase):
    def test_explicit_credentials_variable_wins(self):
        os.environ['AGENTS_CREDENTIALS_FILE'] = '/explicit/creds.env'
        os.environ['AGENTS_DATA_ROOT'] = str(self.root / 'data')
        self.assertEqual(routes.credentials_file(str(self.root / 'plugin')), '/explicit/creds.env')

    def test_data_root_private_file(self):
        os.environ['AGENTS_DATA_ROOT'] = str(self.root / 'data')
        self.assertEqual(routes.credentials_file(str(self.root / 'plugin')),
                         str(self.root / 'data' / 'private' / 'credentials.env'))

    def test_only_legacy_plugin_file_exists(self):
        legacy = self.root / 'plugin' / 'private' / 'credentials.env'
        self.put(legacy, 'OPENAI_LOCAL_8317_TOKEN=x\n')
        self.assertEqual(routes.credentials_file(str(self.root / 'plugin')), str(legacy))

    def test_no_file_anywhere_uses_home_config(self):
        self.assertEqual(routes.credentials_file(str(self.root / 'plugin')),
                         str(self.home / '.config' / 'agents' / 'credentials.env'))

    def test_home_config_file_beats_legacy_plugin_file(self):
        self.put(self.root / 'plugin' / 'private' / 'credentials.env', 'OPENAI_LOCAL_8317_TOKEN=old\n')
        stable = self.home / '.config' / 'agents' / 'credentials.env'
        self.put(stable, 'OPENAI_LOCAL_8317_TOKEN=new\n')
        self.assertEqual(routes.credentials_file(str(self.root / 'plugin')), str(stable))


class FakeClock:
    """Stands in for time.monotonic. A git call spends `cost` seconds; a hung SSH fetch spends its whole timeout."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class ReadTableTests(unittest.TestCase):
    """agents-query.read_table with agents_query._run mocked: SSH first, https when SSH fails."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {'HOME': tmp.name}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.kwargs_seen = []  # keyword arguments of every fake git call: timeout=, env=, check=

    def fake_git(self, ssh_error=None, https_error=None, clock=None, cost=0.0, ssh_hangs=False,
                 ssh_config='', ls_tree=None, show_error=None):
        calls = []
        listing = routes.ROUTES_PATH + '\n' if ls_tree is None else ls_tree

        def spend(seconds):
            if clock is not None:
                clock.now += seconds

        def ok(args, stdout=''):
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr='')

        def run(args, **kwargs):
            calls.append(list(args))
            self.kwargs_seen.append(kwargs)
            if 'config' in args:
                spend(cost)
                return ok(args, ssh_config)
            if 'init' in args:
                spend(cost)
                return ok(args)
            if 'fetch' in args:
                remote = args[-2]
                if remote == agents_query.REMOTE:
                    if ssh_hangs:
                        spend(kwargs['timeout'])
                        raise subprocess.TimeoutExpired(args, kwargs['timeout'])
                    spend(cost)
                    if ssh_error is not None:
                        raise ssh_error
                    return ok(args)
                if remote == agents_query.REMOTE_HTTPS:
                    spend(cost)
                    if https_error is not None:
                        raise https_error
                    return ok(args)
                raise AssertionError('unexpected remote: ' + remote)
            if 'rev-parse' in args:
                spend(cost)
                return ok(args, COMMIT + '\n')
            if 'ls-tree' in args:
                spend(cost)
                return ok(args, listing)
            if 'show' in args:
                spend(cost)
                if show_error is not None:
                    raise show_error
                return ok(args, REMOTE_TABLE)
            raise AssertionError('unexpected git call: ' + repr(args))

        return run, calls

    def read(self, deadline=None, clock=None, **options):
        run, calls = self.fake_git(clock=clock, **options)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(agents_query, '_run', side_effect=run))
            if clock is not None:
                stack.enter_context(mock.patch.object(time, 'monotonic', clock.monotonic))
            commit, table = agents_query.read_table(routes.ROUTES_PATH, deadline=deadline)
        fetched = [args[-2] for args in calls if 'fetch' in args]
        return commit, table, fetched, calls

    def timeouts(self):
        return [kwargs['timeout'] for kwargs in self.kwargs_seen]

    def use_data_root_with_git_config(self):
        os.environ['AGENTS_DATA_ROOT'] = str(self.root / 'data')
        config = self.root / 'data' / '.git' / 'config'
        config.parent.mkdir(parents=True)
        config.write_text('[core]\n', encoding='utf-8')

    def test_ssh_failure_retries_over_https(self):
        error = subprocess.CalledProcessError(128, ['git', 'fetch'], stderr='Permission denied (publickey).')
        commit, table, fetched, calls = self.read(ssh_error=error)
        self.assertEqual(fetched, [agents_query.REMOTE, agents_query.REMOTE_HTTPS])
        self.assertEqual((commit, table), (COMMIT, REMOTE_TABLE))
        self.assertEqual(calls[-1][-1], COMMIT + ':' + routes.ROUTES_PATH)

    def test_ssh_timeout_retries_over_https(self):
        commit, table, fetched, _ = self.read(ssh_error=subprocess.TimeoutExpired(['git', 'fetch'], 20))
        self.assertEqual(fetched, [agents_query.REMOTE, agents_query.REMOTE_HTTPS])
        self.assertEqual((commit, table), (COMMIT, REMOTE_TABLE))

    def test_ssh_success_does_not_try_https(self):
        commit, table, fetched, _ = self.read()
        self.assertEqual(fetched, [agents_query.REMOTE])
        self.assertEqual((commit, table), (COMMIT, REMOTE_TABLE))

    def test_both_transports_failing_raises(self):
        ssh = subprocess.CalledProcessError(128, ['git', 'fetch'], stderr='Permission denied (publickey).')
        https = subprocess.CalledProcessError(128, ['git', 'fetch'], stderr='could not read Username')
        with self.assertRaises(subprocess.CalledProcessError):
            self.read(ssh_error=ssh, https_error=https)

    def test_listing_lists_the_path_then_show_reads_it(self):
        _, _, _, calls = self.read()
        names = [c for c in calls if 'ls-tree' in c]
        self.assertEqual(names[0][-1], routes.ROUTES_PATH)
        self.assertTrue(any('show' in c for c in calls))

    def test_empty_listing_is_a_missing_file_and_git_show_is_not_run(self):
        run, calls = self.fake_git(ls_tree='')
        with mock.patch.object(agents_query, '_run', side_effect=run):
            with self.assertRaises(agents_query.MissingFile):
                agents_query.read_table(routes.ROUTES_PATH)
        self.assertTrue(any('ls-tree' in c for c in calls))
        self.assertFalse(any('show' in c for c in calls))

    def test_failed_show_after_a_listing_is_a_plain_runtime_error(self):
        error = subprocess.CalledProcessError(128, ['git', 'show'], stderr='fatal: bad object')
        run, _ = self.fake_git(show_error=error)
        with mock.patch.object(agents_query, '_run', side_effect=run):
            with self.assertRaises(RuntimeError) as caught:
                agents_query.read_table(routes.ROUTES_PATH)
        self.assertNotIsInstance(caught.exception, agents_query.MissingFile)
        self.assertIn('无法读取该提交中的文件', str(caught.exception))

    def test_default_ssh_command_has_batch_mode_and_connect_timeout(self):
        self.read()
        ssh_command = self.kwargs_seen[0]['env']['GIT_SSH_COMMAND']
        self.assertIn('BatchMode=yes', ssh_command)
        self.assertIn('ConnectTimeout=5', ssh_command)

    def test_git_config_is_read_in_the_local_encoding_and_other_steps_in_utf8(self):
        self.use_data_root_with_git_config()
        self.read(ssh_config='ssh -o BatchMode=yes\n')
        self.assertEqual(self.kwargs_seen[0]['encoding'], locale.getpreferredencoding(False))  # git config
        for kwargs in self.kwargs_seen[1:]:
            self.assertEqual(kwargs.get('encoding', 'utf-8'), 'utf-8')
        self.assertEqual(inspect.signature(agents_query._run).parameters['encoding'].default, 'utf-8')

    def test_no_deadline_gives_each_git_step_twenty_seconds(self):
        self.read()
        self.assertEqual(self.timeouts(), [20, 20, 20, 20, 20])  # init, fetch, rev-parse, ls-tree, show

    def test_no_deadline_caps_the_ssh_config_step_at_five_seconds(self):
        self.use_data_root_with_git_config()
        self.read(ssh_config='ssh -o BatchMode=yes\n')
        self.assertEqual(self.timeouts()[0], 5)

    def test_deadline_caps_every_git_step_to_the_remaining_time(self):
        self.read(deadline=time.monotonic() + 3)
        timeouts = self.timeouts()
        self.assertEqual(len(timeouts), 5)
        for timeout in timeouts:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 3)

    def test_passed_deadline_raises_before_any_git_call(self):
        with mock.patch.object(agents_query, '_run') as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                agents_query.read_table(routes.ROUTES_PATH, deadline=time.monotonic() - 1)
        run.assert_not_called()

    def test_ssh_gets_at_most_half_of_what_is_left_and_https_still_runs_after_it_hangs(self):
        clock = FakeClock()
        commit, table, fetched, _ = self.read(deadline=clock.now + 10, clock=clock, cost=0.5, ssh_hangs=True)
        self.assertEqual(fetched, [agents_query.REMOTE, agents_query.REMOTE_HTTPS])
        self.assertEqual((commit, table), (COMMIT, REMOTE_TABLE))
        # init 10 s (of 10 left); SSH half of 9.5 left = 4.75 s, it hangs and spends it; https gets the other 4.75 s
        expected = [10, 4.75, 4.75, 4.25, 3.75, 3.25]  # init, SSH fetch, https fetch, rev-parse, ls-tree, show
        timeouts = self.timeouts()
        self.assertEqual(len(timeouts), len(expected))
        for got, want in zip(timeouts, expected):
            self.assertAlmostEqual(got, want)
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))  # the budget only shrinks

    def test_timeouts_shrink_as_the_budget_is_spent_and_then_the_read_times_out(self):
        clock = FakeClock()
        with self.assertRaises(subprocess.TimeoutExpired):
            # every git call takes 4 s: init 10 s, SSH min(20, 6 * 0.5) = 3 s, rev-parse 2 s, then nothing is left
            self.read(deadline=clock.now + 10, clock=clock, cost=4)
        timeouts = self.timeouts()
        self.assertEqual(len(timeouts), 3)
        for got, want in zip(timeouts, [10, 3, 2]):
            self.assertAlmostEqual(got, want)

    def test_ssh_config_step_is_capped_by_the_remaining_budget(self):
        self.use_data_root_with_git_config()
        clock = FakeClock()
        self.read(deadline=clock.now + 3, clock=clock, cost=0.5,
                  ssh_config='ssh -o BatchMode=yes -i /placeholder/key\n')
        self.assertLessEqual(self.timeouts()[0], 3)
        self.assertAlmostEqual(self.timeouts()[0], 3)  # min(5, 3 s left)
        # the configured command is what the later git calls run with
        self.assertEqual(self.kwargs_seen[1]['env']['GIT_SSH_COMMAND'],
                         'ssh -o BatchMode=yes -i /placeholder/key')


def process_state(pid):
    """'gone' when the process has exited (or is a zombie, which counts as exited), else 'running'."""
    if os.path.isdir('/proc/self'):  # Linux: field 3 of /proc/<pid>/stat is the state
        try:
            with open('/proc/%d/stat' % pid, encoding='utf-8') as f:
                data = f.read()
        except FileNotFoundError:
            return 'gone'
        return 'gone' if data.rsplit(')', 1)[1].split()[0] == 'Z' else 'running'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'gone'
    except PermissionError:
        return 'running'
    return 'running'


def wait_until_gone(pid, seconds):
    deadline = time.monotonic() + seconds
    while process_state(pid) != 'gone':
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@unittest.skipUnless(os.name == 'posix', 'process groups are POSIX-only')
class RunProcessTests(unittest.TestCase):
    """agents_query._run with real processes (no mock)."""

    def test_timeout_kills_the_grandchild_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, 'grandchild.pid')
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                agents_query._run(['sh', '-c', 'sleep 30 & echo $! > "$0"; wait', pidfile],
                                  env=dict(os.environ), timeout=1)
            self.assertLess(time.monotonic() - started, 5)
            grandchild = int(Path(pidfile).read_text(encoding='utf-8').strip())
        self.assertTrue(wait_until_gone(grandchild, 3), 'grandchild %d still running after the timeout' % grandchild)

    def test_check_raises_called_process_error_with_stderr(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            agents_query._run(['sh', '-c', 'echo boom >&2; exit 3'], env=dict(os.environ), timeout=10)
        self.assertEqual(caught.exception.returncode, 3)
        self.assertEqual(caught.exception.stderr, 'boom\n')

    def test_check_false_returns_the_return_code(self):
        result = agents_query._run(['sh', '-c', 'echo boom >&2; exit 3'], env=dict(os.environ), timeout=10,
                                   check=False)
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stderr, 'boom\n')

    def test_success_returns_text_stdout(self):
        result = agents_query._run(['sh', '-c', 'echo hi'], env=dict(os.environ), timeout=10)
        self.assertIsInstance(result.stdout, str)
        self.assertEqual(result.stdout, 'hi\n')

    def test_timeout_returns_without_waiting_for_a_descendant_that_keeps_the_output_open(self):
        if shutil.which('setsid') is None:
            self.skipTest('needs setsid to start a descendant outside the process group')
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            agents_query._run(['sh', '-c', "setsid sh -c 'sleep 1.5; echo late' & sleep 5"],
                              env=dict(os.environ), timeout=0.3)
        # A pipe-based version waited for the descendant's output to close, i.e. until 'late' at 1.5 s.
        self.assertLess(time.monotonic() - started, 1.0)

    def test_timeout_with_non_utf8_stderr_is_still_a_timeout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):  # not UnicodeDecodeError
            agents_query._run(['sh', '-c', "printf '\\344' >&2; sleep 5"], env=dict(os.environ), timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_non_utf8_stderr_of_a_finished_command_is_replaced_not_raised(self):
        result = agents_query._run(['sh', '-c', "printf '\\344' >&2"], env=dict(os.environ), timeout=10,
                                   check=False)
        self.assertIsInstance(result.stderr, str)
        self.assertIn('\ufffd', result.stderr)  # the replacement character

    def test_keyboard_interrupt_kills_the_grandchild_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, 'grandchild.pid')
            real_wait = subprocess.Popen.wait
            state = {'first': True}

            def wait_then_interrupt(process, timeout=None):
                if state['first']:
                    state['first'] = False
                    # Ctrl-C arrives while the command runs: wait until the grandchild has written its pid.
                    deadline = time.monotonic() + 5
                    while not (os.path.exists(pidfile) and os.path.getsize(pidfile) > 0):
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                    raise KeyboardInterrupt
                return real_wait(process, timeout=timeout)

            with mock.patch.object(subprocess.Popen, 'wait', wait_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    agents_query._run(['sh', '-c', 'sleep 30 & echo $! > "$0"; wait', pidfile],
                                      env=dict(os.environ), timeout=60)
            grandchild = int(Path(pidfile).read_text(encoding='utf-8').strip())
        self.assertTrue(wait_until_gone(grandchild, 3), 'grandchild %d still running after Ctrl-C' % grandchild)


class LlmCallUserinfoTests(unittest.TestCase):
    """llm-call.py refuses a bad --base-url before any request or output file, and its messages never echo the URL."""

    def refused(self, base_url):
        with tempfile.TemporaryDirectory() as tmp:
            infile = os.path.join(tmp, 'in.jsonl')
            outfile = os.path.join(tmp, 'out.jsonl')
            Path(infile).write_text('{"id": "1", "prompt": "hi"}\n', encoding='utf-8')
            env = {'PATH': os.environ.get('PATH', ''), 'X': 'x'}  # X is the bearer key placeholder
            try:
                result = subprocess.run(
                    [sys.executable, '-B', str(HERE / 'llm-call.py'), '--base-url', base_url, '--key-env', 'X',
                     '--model', 'm', '--in', infile, '--out', outfile],
                    env=env, cwd=tmp, capture_output=True, text=True, timeout=30)  # a refusal takes milliseconds
            except subprocess.TimeoutExpired:
                self.fail('llm-call did not refuse %r; it kept running for 30 s' % base_url)
            out_created = os.path.exists(outfile)
        return result, out_created

    def assert_refused(self, base_url, message):
        result, out_created = self.refused(base_url)
        self.assertEqual(result.returncode, 2)
        self.assertIn(message, result.stderr)
        self.assertNotIn('PLACEHOLDERSECRET', result.stdout + result.stderr)
        self.assertNotIn(base_url, result.stderr)  # the message does not echo the URL
        self.assertFalse(out_created)  # refused before any output was opened

    def test_userinfo_is_refused_with_fixed_text(self):
        self.assert_refused('http://user:PLACEHOLDERSECRET@127.0.0.1/v1', 'must not contain')

    def test_fullwidth_colon_before_the_port_with_userinfo(self):
        self.assert_refused('http://user:PLACEHOLDERSECRET@127.0.0.1：8317/v1', 'must not contain')

    def test_missing_colon_after_the_scheme_with_userinfo(self):
        self.assert_refused('http//u:PLACEHOLDERSECRET@h/v1', 'must not contain')

    def test_fullwidth_colon_before_the_port_without_userinfo_is_not_a_valid_url(self):
        self.assert_refused('http://127.0.0.1：8317/v1', 'not a valid URL')

    def test_non_http_scheme_is_not_a_valid_url(self):
        self.assert_refused('ftp://127.0.0.1/v1', 'not a valid URL')

    def test_non_numeric_port_is_not_a_valid_url(self):
        self.assert_refused('http://127.0.0.1:abc/v1', 'not a valid URL')

    def test_port_out_of_range_is_not_a_valid_url(self):
        self.assert_refused('http://127.0.0.1:99999/v1', 'not a valid URL')

    def test_empty_host_is_not_a_valid_url(self):
        self.assert_refused('http:///v1', 'not a valid URL')

    def test_query_string_is_refused_without_echoing_the_key(self):
        self.assert_refused('http://127.0.0.1/v1?api_key=PLACEHOLDERSECRET', 'must not contain a query or fragment')

    def test_fragment_is_refused_without_echoing_it(self):
        self.assert_refused('http://127.0.0.1/v1#PLACEHOLDERSECRET', 'must not contain a query or fragment')


if __name__ == '__main__':
    unittest.main()
