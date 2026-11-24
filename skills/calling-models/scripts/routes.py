"""Route registry and credentials file location, shared by tool-call.py, llm-call.py and jev-call.py.

Order for the route registry:
1. local/api-routes.tsv under AGENTS_LOCAL_ROOT, or under the data root (AGENTS_DATA_ROOT,
   CLAUDE_PLUGIN_DATA, PLUGIN_DATA, GROK_PLUGIN_DATA), or the plugin's references/local/.
   Only a missing file moves on; an existing file that cannot be read or has no usable row is an
   error, so a machine that pins its own endpoint never silently gets another one.
2. otherwise local/api-routes.tsv in the private agents-local repository, read with agents-query.py
   (SSH, then https; SSH gets at most half of the budget) within DISCOVERY_TIMEOUT seconds in total, and cached for CACHE_TTL seconds
   under AGENTS_CACHE_DIR (default ~/.cache/agents) as one JSON record (commit, text, fetched_at).
3. if the remote cannot be read or returns an unusable table (see usable()), the last valid cached copy.
Whenever the registry does not come from a local file, route_rows() returns a note saying where it
came from, and callers print it to stderr: a substituted source is never silent. route_rows()
raises RuntimeError on failure; CLI callers turn it into their exit code, library callers keep it.

Credentials file: AGENTS_CREDENTIALS_FILE; else <data root>/private/credentials.env; else
~/.config/agents/credentials.env, except that an install which only has <plugin dir>/private/
credentials.env (the old default) keeps using it. The plugin directory changes with every version,
so new installs should use ~/.config/agents/credentials.env.
"""
import importlib.util
import json
import math
import re
import os
import subprocess
import tempfile
import time

CACHE_TTL = 3600
ROUTES_PATH = 'local/api-routes.tsv'
REPO = 'DearCaat/agents-local'


def _float_env(name, default):
    """A positive finite number from the environment, else default."""
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


DISCOVERY_TIMEOUT = _float_env('AGENTS_ROUTE_DISCOVERY_TIMEOUT', 15.0)


def data_root():
    return next((os.environ[k] for k in ('AGENTS_DATA_ROOT', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA', 'GROK_PLUGIN_DATA')
                 if os.environ.get(k)), '')


def local_dir(skill_dir):
    root = data_root()
    return os.environ.get('AGENTS_LOCAL_ROOT') or (
        os.path.join(root, 'local') if root else os.path.join(skill_dir, 'references', 'local'))


def credentials_file(plugin_dir):
    if os.environ.get('AGENTS_CREDENTIALS_FILE'):
        return os.environ['AGENTS_CREDENTIALS_FILE']
    root = data_root()
    if root:
        return os.path.join(root, 'private', 'credentials.env')
    stable = os.path.join(os.path.expanduser('~'), '.config', 'agents', 'credentials.env')
    legacy = os.path.join(plugin_dir, 'private', 'credentials.env')
    return legacy if os.path.isfile(legacy) and not os.path.isfile(stable) else stable


def cache_path():
    base = os.environ.get('AGENTS_CACHE_DIR') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'agents')
    return os.path.join(base, 'api-routes.json')


def parse(text):
    return [line.rstrip('\r\n').split('\t') for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


HEADER = 'api_id'
COMMIT_RE = re.compile(r'[0-9a-f]{7,64}')


def usable(rows):
    """A registry is usable when it has at least one data row and every data row has the seven
    required columns with a non-empty api_id. An uncommented header row is not a data row. One
    truncated row rejects the whole table: a partly written table must not replace a complete one."""
    data = [r for r in rows if r[0] != HEADER]
    return bool(data) and all(len(r) >= 7 and r[0].strip() for r in data)


def _query_module(script_dir):
    spec = importlib.util.spec_from_file_location('agents_query', os.path.join(script_dir, 'agents-query.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_cache(path):
    """(record, problem). record is None when there is no valid cache; problem says why, if it exists."""
    try:
        with open(path, 'rb') as f:
            rec = json.loads(f.read().decode('utf-8'))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as e:
        return None, 'cache %s unreadable (%s)' % (path, type(e).__name__)
    ok = isinstance(rec, dict) and isinstance(rec.get('commit'), str) and COMMIT_RE.fullmatch(rec['commit']) \
        and isinstance(rec.get('text'), str) and isinstance(rec.get('fetched_at'), (int, float)) \
        and not isinstance(rec['fetched_at'], bool)
    if ok:
        try:
            ok = math.isfinite(float(rec['fetched_at']))
        except OverflowError:
            ok = False
    if not ok or not usable(parse(rec['text'])):
        return None, 'cache %s incomplete or without a usable table' % path
    return rec, None


def _write_cache(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.routes_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _age_text(rec, now):
    age = now - rec['fetched_at']
    if age < 0:
        return 'fetched at a time later than the current clock'
    return 'fetched %.1f h ago' % (age / 3600) if age >= 3600 else 'fetched %ds ago' % age


def _remote_reason(e, timeout):
    if isinstance(e, subprocess.TimeoutExpired):
        return 'timed out (route discovery budget %gs)' % timeout
    if isinstance(e, subprocess.CalledProcessError):
        return ('cannot read the private repository %s over SSH or https: check this machine\'s Git access '
                'to it (separate from the gateway bearer credential)' % REPO)
    if isinstance(e, OSError):
        return 'git not runnable (%s)' % type(e).__name__
    if type(e).__name__ == 'MissingFile':  # agents-query checked with git ls-tree
        return '%s is missing from the repository' % ROUTES_PATH
    if isinstance(e, RuntimeError):
        return 'cannot read %s from the fetched commit' % ROUTES_PATH
    return type(e).__name__


def route_rows(script_dir):
    """Return (rows, note). note is None when the registry came from the local file."""
    skill_dir = os.path.dirname(script_dir)
    path = os.path.join(local_dir(skill_dir), 'api-routes.tsv')
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8')
    except FileNotFoundError:
        if os.path.lexists(path):  # a dangling symlink is a configured override whose target is missing
            raise RuntimeError('route registry is not readable: %s (link target missing)' % path)
        text = None
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError('route registry is not readable: %s (%s)' % (path, type(e).__name__))
    if text is not None:
        rows = parse(text)
        if not usable(rows):
            raise RuntimeError('route registry has no data row or a row without the 7 required columns: %s '
                               '(fix it, or remove it to use the agents-local registry)' % path)
        return rows, None

    cache = cache_path()
    now = time.time()
    rec, cache_problem = _read_cache(cache)
    if rec is not None and 0 <= now - rec['fetched_at'] < CACHE_TTL:
        return parse(rec['text']), 'route registry from cache %s (agents-local@%s, %s; no %s)' % (
            cache, rec['commit'][:7], _age_text(rec, now), path)

    problem = None
    try:
        commit, text = _query_module(script_dir).read_table(
            ROUTES_PATH, deadline=time.monotonic() + DISCOVERY_TIMEOUT)
        rows = parse(text)
        if not usable(rows):
            problem = 'remote agents-local@%s has no data row or a row without the 7 required columns' % commit[:7]
    except Exception as e:
        problem = 'remote agents-local unreadable: ' + _remote_reason(e, DISCOVERY_TIMEOUT)
    if problem is None:
        try:
            _write_cache(cache, {'commit': commit, 'text': text, 'fetched_at': now})
            where = 'cached to %s' % cache
            replaced = '; replaced the bad cache (%s)' % cache_problem if cache_problem else ''
        except OSError as e:  # the fetched copy is still used; say that it was not kept
            where = 'not cached (%s: %s)' % (cache, type(e).__name__)
            replaced = '; bad cache left in place (%s)' % cache_problem if cache_problem else ''
        return rows, 'route registry from agents-local@%s (no %s; %s%s)' % (commit[:7], path, where, replaced)
    if rec is not None:
        return parse(rec['text']), 'route registry from STALE cache %s (agents-local@%s, %s); %s; no %s' % (
            cache, rec['commit'][:7], _age_text(rec, now), problem, path)
    raise RuntimeError('route registry is not readable: no %s; %s%s' % (
        path, problem, '; ' + cache_problem if cache_problem else ''))
