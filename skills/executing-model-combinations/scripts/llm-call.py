#!/usr/bin/env python3
"""Call an LLM API directly over a JSONL batch: concurrent, retried, resumable.

Each input line is one request:
  {"id": "...", "prompt": "..."}                      single user turn
  {"id": "...", "messages": [{"role": ..., "content": ...}, ...]}
optional per line: "system" (overrides --system-file), "params" (merged into the body).

Each output line is one result, appended as soon as it finishes:
  id, ok, text, reasoning, parsed (--json), model (observed), tokens, usage (raw),
  finish, latency_s, attempts, error, fp.
tokens.reasoning is null when the route does not report a thinking-token count (Messages
usually does not); whether the model thought is then seen in "reasoning".
Rerunning with the same --out skips ids whose last record is ok and whose request
fingerprint (protocol + full body) is unchanged; failed or changed ids run again.

Thinking is on by default: a request that sets neither reasoning_effort nor thinking (in --param
or in its line's "params") gets reasoning_effort=high on chat and thinking={"type":"enabled"} on
Messages. --no-think drops this default; switches set explicitly are still sent.

Stable content (instructions, rubric, shared context) belongs in the system prompt
or at the start of the messages so that provider prefix caching can reuse it across
requests; the per-item part goes last.

Prefix caches live in each upstream (and account), so requests that share a prefix
should reach the same one. Requests carry an X-Session-ID that a gateway can stick to:
by default a hash of the request's system prompt, so one batch shares one id; set
--session-id for multi-turn work whose shared prefix is in the messages. Requests
without a system prompt and without --session-id carry none.

Requests stream by default, so a stalled upstream is caught by --idle-timeout instead of
waiting out the whole response, and --think-limit can stop a thinking loop as it happens.
A timeout that arrives after minutes of waiting (HTTP 408/504/524, or no reply within
--timeout) is not resent: the same request would wait out the same limit again. A
thinking runaway (output limit hit with an empty answer, or --think-limit passed) is
resent once on the same route (--runaway-retries); it is usually a one-off.

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

# No output limit is sent unless --max-tokens is given, on either protocol: thinking can
# run to tens of thousands of tokens and any default would cut some answers off. Routes
# that insist on one (Anthropic's own API) reject the request with a 400, which is loud.
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
# 10-22: pj-lab returned 504 after ~560s to the same request five times in a row (46 min).
TIMEOUT_STATUS = {408, 504, 524}
LATE_S = 120
# Streamed thinking is counted in characters; ~3 per token (DeepSeek: 462772 chars for 128000 tokens).
CHARS_PER_TOKEN = 3
# Sent when the caller sets no thinking switch (user rule 2026-10-26: thinking on by default). A route
# drops a switch it does not know without error, and deepseek-flash does not think on either
# protocol unless asked (10-26), so a missing switch silently lowers quality.
DEFAULT_THINKING = {'chat': ('reasoning_effort', 'high'), 'messages': ('thinking', {'type': 'enabled'})}


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
    if not a.no_think and not ({'reasoning_effort', 'thinking'} & set(params)):
        k, v = DEFAULT_THINKING[a.protocol]
        params[k] = v
    msgs = item.get('messages') or [{'role': 'user', 'content': item['prompt']}]
    if 'system' in item:
        system = item['system']
    if a.protocol == 'chat':
        body = {'model': a.model, 'messages': ([{'role': 'system', 'content': system}] if system else []) + msgs}
        if a.max_tokens:
            body['max_tokens'] = a.max_tokens
    else:
        body = {'model': a.model, 'messages': msgs}
        if a.max_tokens:
            body['max_tokens'] = a.max_tokens
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
            'reasoning': (u.get('output_tokens_details') or {}).get('thinking_tokens')}


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


class Runaway(Exception):
    pass


def late(t0, what):
    return RuntimeError('%s after %ds; not resent, the same request would wait out the same limit again '
                        '(try a faster route, or a smaller task)' % (what, time.time() - t0))


def read_stream(resp, protocol, a, t0):
    """Rebuild a non-streaming response body from server-sent events."""
    text, think, finish, model, usage, nthink, started = [], [], None, None, {}, 0, False
    for raw in resp:
        if time.time() - t0 > a.timeout:
            raise late(t0, 'no result within --timeout')
        line = raw.decode('utf-8', 'replace').strip()
        if not line.startswith('data:'):
            continue
        data = line[5:].strip()
        if data == '[DONE]':
            break
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        if ev.get('error') or ev.get('type') == 'error':
            raise Retry('error in stream: %s' % json.dumps(ev.get('error') or ev, ensure_ascii=False)[:300])
        if protocol == 'chat':
            model = ev.get('model') or model
            usage = ev.get('usage') or usage
            for c in ev.get('choices') or []:
                d = c.get('delta') or {}
                if d.get('content'):
                    text.append(d['content'])
                r = d.get('reasoning_content') or d.get('reasoning')
                if r:
                    think.append(r)
                    nthink += len(r)
                finish = c.get('finish_reason') or finish
        else:
            t = ev.get('type')
            if t == 'message_start':
                m = ev.get('message') or {}
                model = m.get('model')
                usage.update(m.get('usage') or {})
            elif t == 'content_block_start':
                started = True
                b = ev.get('content_block') or {}
                if b.get('type') == 'text':
                    text.append(b.get('text') or '')
            elif t == 'content_block_delta':
                d = ev.get('delta') or {}
                if d.get('type') == 'text_delta':
                    text.append(d.get('text') or '')
                elif d.get('type') == 'thinking_delta':
                    think.append(d.get('thinking') or '')
                    nthink += len(d.get('thinking') or '')
            elif t == 'message_delta':
                finish = (ev.get('delta') or {}).get('stop_reason') or finish
                usage.update(ev.get('usage') or {})
            elif t == 'message_stop':
                break
        if a.think_limit and nthink > a.think_limit * CHARS_PER_TOKEN:
            raise Runaway('thinking passed --think-limit %d (~%d tokens streamed, no answer yet: %s)'
                          % (a.think_limit, nthink // CHARS_PER_TOKEN, 'yes' if text else 'no'))
    if finish is None:
        raise Retry('stream ended without a finish reason')
    if protocol == 'chat':
        msg = {'content': ''.join(text), 'reasoning_content': ''.join(think) or None}
        return {'model': model, 'choices': [{'message': msg, 'finish_reason': finish}], 'usage': usage}
    blocks = ([{'type': 'thinking', 'thinking': ''.join(think)}] if think else []) + \
             ([{'type': 'text', 'text': ''.join(text)}] if text or started else [])
    return {'model': model, 'content': blocks, 'stop_reason': finish, 'usage': usage}


def post(opener, url, headers, body, a):
    stream = not a.no_stream
    if stream:
        body = dict(body, stream=True)
        if a.protocol == 'chat':
            body['stream_options'] = {'include_usage': True}
    q = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    t0 = time.time()
    try:
        # The socket timeout applies to each read: with streaming it is the idle limit.
        with opener.open(q, timeout=a.idle_timeout if stream else a.timeout) as resp:
            if stream:
                return read_stream(resp, a.protocol, a, t0)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode('utf-8', 'replace')
        if e.code in TIMEOUT_STATUS and time.time() - t0 > LATE_S:
            raise late(t0, 'HTTP %d' % e.code)
        if e.code in RETRY_STATUS:
            ra = e.headers.get('retry-after')
            wait = float(ra) if ra and ra.replace('.', '', 1).isdigit() and float(ra) <= 120 else None
            raise Retry('HTTP %d %s' % (e.code, detail), wait)
        hint = '; this route requires an output limit, pass --max-tokens' if 'max_tokens' in detail else ''
        raise RuntimeError('HTTP %d %s%s' % (e.code, detail, hint))
    except (urllib.error.URLError, OSError) as e:
        if not stream and 'timed out' in str(e) and time.time() - t0 > LATE_S:
            raise late(t0, 'no reply within --timeout')
        raise Retry('network: %s%s' % (e, ' (nothing for --idle-timeout %ds)' % a.idle_timeout
                                       if stream and 'timed out' in str(e) else ''))
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
    t0, attempt, json_left, runaway_left = time.time(), 0, a.json_retries, a.runaway_retries
    while True:
        attempt += 1
        try:
            headers = dict(ctx['headers'], **({'X-Session-ID': item['_sid']} if item['_sid'] else {}))
            r = post(ctx['opener'], ctx['url'], headers, body, a)
            text, think, finish = extract(a.protocol, r)
            if finish in ('length', 'max_tokens'):
                # A cut-off answer is a failure, never a result; thinking often eats the whole budget.
                # With a partial answer the limit is too small and a resend is cut off the same way;
                # with an empty one it is usually a thinking loop (10-22: 1 of 8 identical calls).
                tok = norm_tokens(a.protocol, r.get('usage'))
                empty = not (text or '').strip()
                rec.update(text=text, reasoning=think, finish=finish, model=r.get('model'), usage=r.get('usage'),
                           tokens=tok, error='truncated: output hit the token limit (finish=%s, output %d tokens, '
                           'answer %s); raise --max-tokens, or drop it if set' % (finish, tok['output'], 'empty' if empty else 'partial'))
                if empty and runaway_left > 0 and attempt < a.attempts:
                    runaway_left -= 1
                    continue
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
        except Runaway as e:
            rec['error'] = 'runaway: %s' % e
            if runaway_left > 0 and attempt < a.attempts:
                runaway_left -= 1
                continue
            break
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
    p.add_argument('--session-id', help="X-Session-ID for every request (default: hash of each request's system prompt)")
    p.add_argument('--cache-system', action='store_true', help='messages protocol: mark the system prompt cache_control ephemeral')
    p.add_argument('--max-tokens', type=int,
                   help='output limit including thinking; none is sent unless set. '
                        'Hitting it is reported as a failure, not accepted')
    p.add_argument('--param', action='append', default=[], metavar='K=V',
                   help='extra body field, V parsed as JSON when possible (e.g. temperature=0). Thinking is on by '
                        'default (see --no-think); set it yourself per protocol: chat reasoning_effort=low|high|max; '
                        'messages thinking=\'{"type":"enabled"}\'. A field the route does not know is dropped without error')
    p.add_argument('--no-think', action='store_true',
                   help='do not add the default thinking switch (chat reasoning_effort=high, messages thinking '
                        'enabled); switches set with --param or a line\'s "params" are still sent')
    p.add_argument('--json', action='store_true', help='parse the reply as JSON into "parsed"; unparseable counts as failed')
    p.add_argument('--json-retries', type=int, default=1, help='re-asks when the reply is not JSON (default 1)')
    p.add_argument('-j', '--concurrency', type=int, default=32,
                   help='parallel requests (default 32; the 8317 gateway queues, backs off and switches upstreams)')
    p.add_argument('--warmup', type=int, default=2,
                   help='with -j > 1, run this many requests one by one first so the shared prefix gets cached (default 2)')
    p.add_argument('--attempts', type=int, default=6, help='tries per request on retryable errors (default 6)')
    p.add_argument('--timeout', type=float, default=3600,
                   help='seconds one attempt may take in total (default 3600); long thinking needs a long wait')
    p.add_argument('--idle-timeout', type=float, default=180,
                   help='streaming: seconds with no data before the attempt is dropped and resent (default 180)')
    p.add_argument('--no-stream', action='store_true',
                   help='wait for the whole response instead (for routes that cannot stream); '
                        '--idle-timeout and --think-limit then do not apply')
    p.add_argument('--think-limit', type=int, default=0, metavar='TOKENS',
                   help='streaming: stop an attempt once its thinking passes about this many tokens '
                        '(counted as %d characters each; default 0 = no limit)' % CHARS_PER_TOKEN)
    p.add_argument('--runaway-retries', type=int, default=1,
                   help='resends on the same route after a thinking runaway (default 1)')
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
    k, v = DEFAULT_THINKING[a.protocol]
    think = ', '.join('%s=%s' % (f, json.dumps(a.param[f])) for f in ('reasoning_effort', 'thinking') if f in a.param) \
        or ('off (--no-think)' if a.no_think else '%s=%s (default, unless a line sets its own)' % (k, json.dumps(v)))
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
        sys_used = it.get('system', system)
        it['_sid'] = a.session_id or ('llm-call-' + hashlib.sha256(sys_used.encode()).hexdigest()[:16] if sys_used else None)
        items.append(it)

    done = load_done(a.out)
    todo = [it for it in items if done.get(it['id']) != it['_fp']]
    print('llm-call: %d items, %d already done, %d to run; %s %s via %s; %d session ids; thinking %s'
          % (len(items), len(items) - len(todo), len(todo), a.protocol, a.model, a.api or base,
             len({it['_sid'] for it in items if it['_sid']}), think), file=sys.stderr)
    if a.dry_run:
        if todo:
            print(json.dumps(todo[0]['_body'], ensure_ascii=False, indent=1))
        return 0

    ctx = {'opener': opener, 'url': url, 'headers': headers}
    lock, stop = threading.Lock(), threading.Event()
    state = {'ok': 0, 'fail': 0, 'streak': 0, 'tok': {}, 'models': {}, 'finish': {}, 'thought': 0}
    out = open(a.out, 'a', encoding='utf-8')

    def work(it):
        if stop.is_set():
            return
        rec = run_one(it, a, ctx)
        with lock:
            out.write(json.dumps(rec, ensure_ascii=False) + '\n')
            out.flush()
            for key, v in (('models', rec.get('model')), ('finish', rec.get('finish'))):
                if v is not None:
                    state[key][v] = state[key].get(v, 0) + 1
            if rec['ok']:
                state['ok'] += 1
                state['streak'] = 0
                for k, v in rec['tokens'].items():
                    if v is not None:
                        state['tok'][k] = state['tok'].get(k, 0) + v
                if rec.get('reasoning') or rec['tokens'].get('reasoning'):
                    state['thought'] += 1
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
    # Settings that a route ignores do not error; show what came back so drift is visible.
    print('llm-call: observed models %s; finish reasons %s'
          % (json.dumps(state['models']), json.dumps(state['finish'])), file=sys.stderr)
    print('llm-call: %d of %d ok results show thinking' % (state['thought'], state['ok']), file=sys.stderr)
    if a.protocol == 'messages' and 'reasoning_effort' in a.param and 'thinking' not in a.param:
        print('llm-call: warning: reasoning_effort is a chat field; Messages routes drop it silently. '
              'Use --param thinking=\'{"type":"enabled"}\'', file=sys.stderr)
    elif (not a.no_think or {'reasoning_effort', 'thinking'} & set(a.param)) and state['ok'] and not state['thought']:
        print('llm-call: warning: thinking was requested but no result shows any; check the route honours it',
              file=sys.stderr)
    if stop.is_set():
        print('llm-call: stopped after %d consecutive failures; rerun the same command to resume' % a.stop_after_failures,
              file=sys.stderr)
    return 0 if state['fail'] == 0 and skipped == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
