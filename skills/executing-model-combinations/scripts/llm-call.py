#!/usr/bin/env python3
"""Call an LLM API directly over a JSONL batch: concurrent, retried, resumable.

Each input line is one request:
  {"id": "...", "prompt": "..."}                      single user turn
  {"id": "...", "messages": [{"role": ..., "content": ...}, ...]}
optional per line: "system" (overrides --system-file), "params" (merged into the body).

Each output line is one result, appended as soon as it finishes:
  id, ok, text, reasoning, parsed (--json), model (observed), tokens, usage (raw),
  finish, latency_s, attempts, error, fp.
Rerunning with the same --out skips ids whose last record is ok and whose request
fingerprint (protocol + full body) is unchanged; failed or changed ids run again.

Stable content (instructions, rubric, shared context) belongs in the system prompt
or at the start of the messages so that provider prefix caching can reuse it across
requests; the per-item part goes last.

Standard library only. The route comes from --api (resolved in the local
api-routes.tsv, credential read from the private credentials file and never
printed) or from --base-url with --key-env.
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


def die(msg):
    print('llm-call: ' + msg, file=sys.stderr)
    sys.exit(2)


def data_paths():
    skill = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    plugin = os.path.dirname(os.path.dirname(skill))
    root = next((os.environ[k] for k in ('AGENTS_DATA_ROOT', 'CLAUDE_PLUGIN_DATA', 'PLUGIN_DATA', 'GROK_PLUGIN_DATA')
                 if os.environ.get(k)), '')
    local = os.environ.get('AGENTS_LOCAL_ROOT') or (os.path.join(root, 'local') if root else os.path.join(skill, 'references', 'local'))
    creds = os.environ.get('AGENTS_CREDENTIALS_FILE') or os.path.join(root or plugin, 'private', 'credentials.env')
    return local, creds


def resolve_route(api):
    local, creds = data_paths()
    path = os.path.join(local, 'api-routes.tsv')
    try:
        rows = [l.rstrip('\n').split('\t') for l in open(path, encoding='utf-8') if l.strip() and not l.startswith('#')]
    except OSError:
        die('route registry is not readable: ' + path)
    rows = [r for r in rows if r[0] == api]
    if len(rows) != 1:
        die('unregistered API route: ' + api)
    _, base, wire, cred, _, auth, state = rows[0][:7]
    if base == '-' or auth != 'bearer' or state == 'unavailable':
        die('route %s has no usable direct bearer endpoint (state=%s)' % (api, state))
    # Cost and availability live in the route notes; surface them on every run.
    print('llm-call: route %s (%s): %s' % (api, state, rows[0][7] if len(rows[0]) > 7 else ''), file=sys.stderr)
    return base, wire, read_credential(creds, cred)


def read_credential(path, name):
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in open(path, encoding='utf-8'):
            m = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line)
            if m and m.group(1) == name:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in '\'"':
                    v = v[1:-1]
                if v:
                    return v
    except OSError:
        die('credentials file is not readable: ' + path)
    die('credential %s is not defined' % name)


def build_body(item, a, system):
    params = dict(a.param)
    params.update(item.get('params') or {})
    msgs = item.get('messages') or [{'role': 'user', 'content': item['prompt']}]
    if 'system' in item:
        system = item['system']
    if a.protocol == 'chat':
        body = {'model': a.model, 'messages': ([{'role': 'system', 'content': system}] if system else []) + msgs}
        if a.max_tokens:
            body['max_tokens'] = a.max_tokens
    else:
        body = {'model': a.model, 'messages': msgs, 'max_tokens': a.max_tokens or 8192}
        if system:
            block = {'type': 'text', 'text': system}
            if a.cache_system:
                block['cache_control'] = {'type': 'ephemeral'}
            body['system'] = [block]
    body.update(params)
    return body


def fingerprint(protocol, body):
    return hashlib.sha256(json.dumps([protocol, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def norm_tokens(protocol, u):
    u = u or {}
    # chat prompt_tokens includes cached and written parts; messages input_tokens excludes both.
    if protocol == 'chat':
        d = u.get('prompt_tokens_details') or {}
        cached = d.get('cached_tokens') or u.get('prompt_cache_hit_tokens') or 0
        write = d.get('cache_write_tokens') or 0
        return {'input': (u.get('prompt_tokens') or 0) - cached - write, 'cache_read': cached, 'cache_write': write,
                'output': u.get('completion_tokens') or 0,
                'reasoning': (u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0}
    return {'input': u.get('input_tokens') or 0, 'cache_read': u.get('cache_read_input_tokens') or 0,
            'cache_write': u.get('cache_creation_input_tokens') or 0, 'output': u.get('output_tokens') or 0,
            'reasoning': (u.get('output_tokens_details') or {}).get('thinking_tokens') or 0}


def extract(protocol, r):
    if protocol == 'chat':
        c = (r.get('choices') or [{}])[0]
        m = c.get('message') or {}
        return m.get('content'), m.get('reasoning_content') or m.get('reasoning'), c.get('finish_reason')
    blocks = r.get('content') or []
    text = ''.join(b.get('text', '') for b in blocks if b.get('type') == 'text')
    think = ''.join(b.get('thinking', '') for b in blocks if b.get('type') == 'thinking')
    return (text if blocks else None), think or None, r.get('stop_reason')


def parse_json(text):
    t = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text or '')
    try:
        return json.loads(t)
    except ValueError:
        pass
    for o, c in (('{', '}'), ('[', ']')):
        i, j = t.find(o), t.rfind(c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                pass
    raise ValueError('output is not JSON')


class Retry(Exception):
    def __init__(self, msg, wait=None):
        super().__init__(msg)
        self.wait = wait


def post(opener, url, headers, body, timeout):
    q = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with opener.open(q, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode('utf-8', 'replace')
        if e.code in RETRY_STATUS:
            ra = e.headers.get('retry-after')
            wait = float(ra) if ra and ra.replace('.', '', 1).isdigit() and float(ra) <= 120 else None
            raise Retry('HTTP %d %s' % (e.code, detail), wait)
        raise RuntimeError('HTTP %d %s' % (e.code, detail))
    except (urllib.error.URLError, OSError) as e:
        raise Retry('network: %s' % e)
    try:
        r = json.loads(raw)
    except ValueError:
        raise Retry('non-JSON response: %r' % raw[:200])
    if r.get('error'):
        raise Retry('error in body: %s' % json.dumps(r['error'], ensure_ascii=False)[:300])
    return r


def run_one(item, a, ctx):
    body, fp = item['_body'], item['_fp']
    rec = {'id': item['id'], 'ok': False, 'api': a.api, 'protocol': a.protocol, 'requested_model': a.model, 'fp': fp}
    t0, attempt, json_left = time.time(), 0, a.json_retries
    while True:
        attempt += 1
        try:
            r = post(ctx['opener'], ctx['url'], ctx['headers'], body, a.timeout)
            text, think, finish = extract(a.protocol, r)
            if not (text or '').strip() and finish in ('length', 'max_tokens'):
                # Reasoning models can spend the whole budget thinking; a retry would do the same.
                rec.update(finish=finish, usage=r.get('usage'), tokens=norm_tokens(a.protocol, r.get('usage')),
                           error='empty answer: output budget used up (finish=%s); raise --max-tokens' % finish)
                break
            if text is None:
                raise Retry('no content in response: %s' % json.dumps(r, ensure_ascii=False)[:300])
            rec.update(text=text, reasoning=think, finish=finish, model=r.get('model'),
                       usage=r.get('usage'), tokens=norm_tokens(a.protocol, r.get('usage')))
            if a.json:
                try:
                    rec['parsed'] = parse_json(text)
                except ValueError as e:
                    if json_left > 0 and attempt < a.attempts:
                        json_left -= 1
                        continue
                    rec['error'] = str(e)
                    break
            rec['ok'] = True
            break
        except Retry as e:
            rec['error'] = str(e)
            if attempt >= a.attempts:
                break
            time.sleep(e.wait if e.wait is not None else min(2 * 2 ** (attempt - 1), 60) * (0.75 + 0.5 * random.random()))
        except RuntimeError as e:
            rec['error'] = str(e)
            break
    if rec['ok']:
        rec.pop('error', None)
    rec.update(attempts=attempt, latency_s=round(time.time() - t0, 2), t=time.strftime('%Y-%m-%dT%H:%M:%S'))
    return rec


def load_done(path):
    last = {}
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                r = json.loads(line)
                last[r['id']] = r
            except (ValueError, KeyError):
                continue
    return {k: v['fp'] for k, v in last.items() if v.get('ok')}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--in', dest='inp', required=True, help='input JSONL, or - for stdin')
    p.add_argument('--out', required=True, help='output JSONL (appended; also the resume record)')
    p.add_argument('--model', required=True, help='model name as the route expects it, e.g. a prefixed gateway name')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--api', help='api_id in the local api-routes.tsv')
    g.add_argument('--base-url', help='endpoint base ending in /v1 (needs --key-env)')
    p.add_argument('--key-env', help='environment variable holding the bearer key for --base-url')
    p.add_argument('--protocol', choices=['chat', 'messages'],
                   help='OpenAI Chat Completions or Anthropic Messages; default follows the route')
    p.add_argument('--system-file', help='shared system prompt for every request')
    p.add_argument('--cache-system', action='store_true', help='messages protocol: mark the system prompt cache_control ephemeral')
    p.add_argument('--max-tokens', type=int)
    p.add_argument('--param', action='append', default=[], metavar='K=V',
                   help='extra body field, V parsed as JSON when possible (e.g. reasoning_effort=high, temperature=0)')
    p.add_argument('--json', action='store_true', help='parse the reply as JSON into "parsed"; unparseable counts as failed')
    p.add_argument('--json-retries', type=int, default=1, help='re-asks when the reply is not JSON (default 1)')
    p.add_argument('-j', '--concurrency', type=int, default=4)
    p.add_argument('--warmup', type=int, default=2,
                   help='with -j > 1, run this many requests one by one first so the shared prefix gets cached (default 2)')
    p.add_argument('--attempts', type=int, default=6, help='tries per request on retryable errors (default 6)')
    p.add_argument('--timeout', type=float, default=600, help='seconds per HTTP request (default 600)')
    p.add_argument('--stop-after-failures', type=int, default=10,
                   help='stop starting new requests after this many consecutive failed items (default 10)')
    p.add_argument('--dry-run', action='store_true', help='print the first request body and counts; no calls')
    a = p.parse_args()

    params = {}
    for kv in a.param:
        k, sep, v = kv.partition('=')
        if not sep:
            die('--param needs K=V: ' + kv)
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    a.param = params

    if a.api:
        base, wire, key = resolve_route(a.api)
        a.protocol = a.protocol or ('messages' if wire == 'anthropic-messages' else 'chat')
    else:
        if not a.key_env or not os.environ.get(a.key_env):
            die('--base-url needs --key-env naming a set environment variable')
        base, key, a.protocol = a.base_url, os.environ[a.key_env], a.protocol or 'chat'
    base = base.rstrip('/')
    url = base + ('/chat/completions' if a.protocol == 'chat' else '/messages')
    headers = {'Content-Type': 'application/json'}
    if a.protocol == 'chat':
        headers['Authorization'] = 'Bearer ' + key
    else:
        headers.update({'x-api-key': key, 'Authorization': 'Bearer ' + key, 'anthropic-version': '2023-06-01'})
    host = urllib.parse.urlparse(base).hostname
    opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if host in ('127.0.0.1', 'localhost', '::1') else []))

    system = open(a.system_file, encoding='utf-8').read() if a.system_file else None
    src = sys.stdin if a.inp == '-' else open(a.inp, encoding='utf-8')
    items, seen = [], set()
    for n, line in enumerate(src, 1):
        if not line.strip():
            continue
        try:
            it = json.loads(line)
        except ValueError:
            die('input line %d is not JSON' % n)
        if 'id' not in it or not ('prompt' in it or 'messages' in it):
            die('input line %d needs "id" and "prompt" or "messages"' % n)
        it['id'] = str(it['id'])
        if it['id'] in seen:
            die('duplicate id on input line %d: %s' % (n, it['id']))
        seen.add(it['id'])
        it['_body'] = build_body(it, a, system)
        it['_fp'] = fingerprint(a.protocol, it['_body'])
        items.append(it)

    done = load_done(a.out)
    todo = [it for it in items if done.get(it['id']) != it['_fp']]
    print('llm-call: %d items, %d already done, %d to run; %s %s via %s'
          % (len(items), len(items) - len(todo), len(todo), a.protocol, a.model, a.api or base), file=sys.stderr)
    if a.dry_run:
        if todo:
            print(json.dumps(todo[0]['_body'], ensure_ascii=False, indent=1))
        return 0

    ctx = {'opener': opener, 'url': url, 'headers': headers}
    lock, stop = threading.Lock(), threading.Event()
    state = {'ok': 0, 'fail': 0, 'streak': 0, 'tok': {}}
    out = open(a.out, 'a', encoding='utf-8')

    def work(it):
        if stop.is_set():
            return
        rec = run_one(it, a, ctx)
        with lock:
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            out.flush()
            if rec['ok']:
                state['ok'] += 1
                state['streak'] = 0
                for k, v in rec['tokens'].items():
                    state['tok'][k] = state['tok'].get(k, 0) + v
            else:
                state['fail'] += 1
                state['streak'] += 1
                print('llm-call: %s failed after %d tries: %s' % (rec['id'], rec['attempts'], rec['error'][:200]), file=sys.stderr)
                if state['streak'] >= a.stop_after_failures:
                    stop.set()

    t0 = time.time()
    # Providers persist a shared prefix only after seeing it (DeepSeek: from the third
    # request; Anthropic: once the first response starts), so a burst of parallel first
    # requests all miss the cache. Running a few alone first lets the rest hit it.
    warm = todo[:a.warmup] if a.concurrency > 1 else []
    for it in warm:
        work(it)
    with cf.ThreadPoolExecutor(max_workers=max(1, a.concurrency)) as ex:
        list(ex.map(work, todo[len(warm):]))
    out.close()
    skipped = len(todo) - state['ok'] - state['fail']
    print('llm-call: ok %d, failed %d, not started %d, %.0fs; tokens %s'
          % (state['ok'], state['fail'], skipped, time.time() - t0, json.dumps(state['tok'])), file=sys.stderr)
    if stop.is_set():
        print('llm-call: stopped after %d consecutive failures; rerun the same command to resume' % a.stop_after_failures,
              file=sys.stderr)
    return 0 if state['fail'] == 0 and skipped == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
