"""Route registry and credentials file location, shared by tool-call.py, llm-call.py and jev-call.py.

Order for the route registry:
1. local/api-routes.tsv under AGENTS_LOCAL_ROOT, or under the data root (AGENTS_DATA_ROOT,
   CLAUDE_PLUGIN_DATA, PLUGIN_DATA, GROK_PLUGIN_DATA), or the plugin's references/local/;
2. otherwise the copy of local/api-routes.tsv in the private agents-local repository, read with
   agents-query.py and cached for CACHE_TTL seconds under AGENTS_CACHE_DIR (default ~/.cache/agents);
3. if the remote cannot be read, an older cached copy.
Whenever the registry does not come from a local file, route_rows() returns a note saying where it
came from, and callers print it to stderr: a substituted source is never silent.

Credentials file: AGENTS_CREDENTIALS_FILE, else <data root>/private/credentials.env, else
~/.config/agents/credentials.env (the plugin directory changes with every version, so it is only
used when an older install already keeps its file there).
"""
import importlib.util
import os
import tempfile
import time

CACHE_TTL = 3600
ROUTES_PATH = 'local/api-routes.tsv'


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


def cache_dir():
    return os.environ.get('AGENTS_CACHE_DIR') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'agents')


def parse(text):
    return [line.rstrip('\r\n').split('\t') for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def _query_module(script_dir):
    spec = importlib.util.spec_from_file_location('agents_query', os.path.join(script_dir, 'agents-query.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.routes_')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def route_rows(script_dir):
    """Return (rows, note). note is None when the registry came from a local file."""
    skill_dir = os.path.dirname(script_dir)
    path = os.path.join(local_dir(skill_dir), 'api-routes.tsv')
    try:
        with open(path, encoding='utf-8') as f:
            return parse(f.read()), None
    except OSError:
        pass
    cache = os.path.join(cache_dir(), 'api-routes.tsv')
    commit_file = cache + '.commit'
    try:
        age = time.time() - os.path.getmtime(cache)
        with open(cache, encoding='utf-8') as f:
            cached = f.read()
        with open(commit_file, encoding='utf-8') as f:
            cached_commit = f.read().strip()[:7]
    except OSError:
        age, cached, cached_commit = None, None, '?'
    if cached is not None and 0 <= age < CACHE_TTL:
        return parse(cached), 'route registry from cache %s (agents-local@%s, fetched %ds ago; no %s)' % (
            cache, cached_commit, age, path)
    try:
        commit, text = _query_module(script_dir).read_table(ROUTES_PATH)
    except Exception as e:  # remote unreadable: fall back to the stale cache, and say so
        reason = '%s: %s' % (type(e).__name__, str(e).strip().splitlines()[-1] if str(e).strip() else '')
        if cached is not None:
            return parse(cached), ('route registry from STALE cache %s (agents-local@%s, %.1f h old); '
                                   'remote agents-local unreadable (%s); no %s') % (
                cache, cached_commit, age / 3600, reason, path)
        raise RuntimeError('route registry is not readable: %s; remote agents-local unreadable (%s)' % (path, reason))
    try:
        _write(cache, text)
        _write(commit_file, commit + '\n')
    except OSError:
        pass  # cache is an optimisation; the fetched copy is still used
    return parse(text), 'route registry from agents-local@%s (no %s; cached to %s)' % (commit[:7], path, cache)
