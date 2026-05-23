"""Mushi: a single-user Telegram interface to Claude Code.

Named after the Den Den Mushi, the transponder snails of One Piece.
Small creatures people carry to talk to others across the Grand Line.
Same idea here: phone in your hand, laptop running Claude, work happening
in the world.

Run mushi.py on your laptop. Message your bot from anywhere. Claude
responds with full file access on the host machine. Long-running tasks
(`/cc`) stream their output back to chat. Voice messages get transcribed
via Whisper and routed. Markdown memories accumulate across sessions in
a local folder Claude reads at the start of every conversation.

Env vars (see .env.example for full descriptions):

    Required
        TELEGRAM_BOT_TOKEN: bot token from @BotFather
        TELEGRAM_USER_ID:   your Telegram numeric ID. Only this user is served.

    LLM access (need at least one)
        CLAUDE_CLI:         path to `claude` CLI binary. Default: 'claude' on PATH.
                            Recommended path. Routes /think and /cc through your
                            Claude Code subscription instead of API credits.
        ANTHROPIC_API_KEY:  API fallback if CLAUDE_CLI is absent.
        OPENAI_API_KEY:     enables voice message transcription (Whisper).
        DEEPSEEK_API_KEY:   cheap fallback for /ask and /think.

    Storage
        MEMORY_DIR: markdown memory folder. Default: ~/.claude-bridge/memory.
        INBOX_DIR:  drop folder for files/images from chat.
                    Default: ~/.claude-bridge/inbox.

    Optional integrations (silent no-op if unset)
        BRIDGE_EXTRA_COST_LOGS: comma-separated additional .log files
                                /cost should scan for token-usage lines.
        BRIDGE_IDEAS_DIR:       JSON reports folder for the /ideas command.

Security:
    Only the Telegram user ID set above can send commands. A password
    unlocks sensitive commands for UNLOCK_MINUTES. /lock ends the window.
    Low-stakes reads (/help, /list, /ask) work without unlock.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import openai as openai_sdk
except ImportError:
    openai_sdk = None

# Optional user-supplied plugin: if a sibling `instagram_handler.py`
# module is present on the import path, the bridge offloads Instagram-
# URL handling to it. Public installs typically don't have this file;
# the import quietly fails and the bridge runs without that feature.
try:
    import instagram_handler
except Exception:
    instagram_handler = None

# ---------- config ----------

# Default storage roots live under the user's home so a fresh clone Just
# Works on any OS without surgery. Override via env in production.
_HOME = pathlib.Path(os.path.expanduser('~'))
_DEFAULT_BRIDGE_HOME = _HOME / '.claude-bridge'

MEMORY_DIR = pathlib.Path(
    os.environ.get('MEMORY_DIR') or (_DEFAULT_BRIDGE_HOME / 'memory')
)
INBOX_DIR = pathlib.Path(
    os.environ.get('INBOX_DIR') or (_DEFAULT_BRIDGE_HOME / 'inbox')
)
INDEX_FILE     = MEMORY_DIR / 'MEMORY.md'
PASSHASH_FILE  = MEMORY_DIR / '.bot_passhash'
LOG_FILE       = pathlib.Path(__file__).with_suffix('.log')

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
AUTHORIZED_USER_ID = int(os.environ.get('TELEGRAM_USER_ID', '0') or 0)

API = f'https://api.telegram.org/bot{TOKEN}'
VALID_TYPES = {'user', 'feedback', 'project', 'reference'}

HAIKU   = 'claude-haiku-4-5-20251001'
SONNET  = 'claude-sonnet-4-6'
OPUS    = 'claude-opus-4-7'

UNLOCK_MINUTES   = 60
MAX_FAILED       = 5
LOCKOUT_MINUTES  = 10

CLAUDE_CLI = os.environ.get('CLAUDE_CLI', 'claude')

COST_LOG = pathlib.Path(__file__).resolve().parent / 'cost_log.jsonl'

# Optional integration: list of additional .log files /cost scans for
# token-usage patterns. Format: comma-separated absolute paths. Lines
# matching the regex `tokens in=N out=M` near a `triage:` or `opus
# returned` keyword will be bucketed as triage/proposer usage. Useful
# if you run a sibling agent (e.g. a trading bot) on the same machine
# and want unified cost reporting. Unset = no external log scanning.
_EXTRA_COST_LOGS_RAW = os.environ.get('BRIDGE_EXTRA_COST_LOGS', '').strip()
EXTRA_COST_LOGS = [
    pathlib.Path(p.strip())
    for p in _EXTRA_COST_LOGS_RAW.split(',')
    if p.strip()
]

_COST_RATES = {
    'claude-haiku-4-5-20251001': (0.80,  4.00),
    'claude-haiku-4-5':          (0.80,  4.00),
    'claude-sonnet-4-6':         (3.00, 15.00),
    'claude-opus-4-7':          (15.00, 75.00),
    'deepseek-chat':             (0.27,  1.10),
    'deepseek-reasoner':         (0.55,  2.19),
}


def _log_cost(service: str, model: str, input_tok: int, output_tok: int):
    try:
        rates = _COST_RATES.get(model, (1.00, 5.00))
        usd = (input_tok * rates[0] + output_tok * rates[1]) / 1_000_000
        entry = json.dumps({
            'ts': now().isoformat(),
            'service': service,
            'model': model,
            'in': input_tok,
            'out': output_tok,
            'usd': round(usd, 6),
        })
        with open(COST_LOG, 'a', encoding='utf-8') as f:
            f.write(entry + '\n')
    except Exception as e:
        log.warning('cost log error: %s', e)


def _cost_report(days: int = 7) -> str:
    """Build N-day cost breakdown from cost_log.jsonl + any extra log files
    configured via the BRIDGE_EXTRA_COST_LOGS env var (optional)."""
    import re as _re
    from collections import defaultdict as _dd
    cutoff = now() - timedelta(days=days)
    rows = []

    if COST_LOG.exists():
        for line in COST_LOG.read_text(encoding='utf-8', errors='ignore').splitlines():
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e['ts'])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    rows.append(e)
            except Exception:
                pass

    # Scan any user-configured external log files for token-usage lines.
    # Pattern matches lines like "...triage:...tokens in=15963 out=1329"
    # or "...opus returned...tokens in=8200 out=1500". Common shape for
    # an agent that runs sibling LLM calls on the same machine and writes
    # its own log. Skipped silently if no extra logs configured.
    for extra_log in EXTRA_COST_LOGS:
        if not extra_log.exists():
            continue
        haiku_pat = _re.compile(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?triage:.*?tokens in=(\d+) out=(\d+)'
        )
        opus_pat = _re.compile(
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?opus returned.*?tokens in=(\d+) out=(\d+)'
        )
        try:
            for line in extra_log.read_text(encoding='utf-8', errors='ignore').splitlines():
                for pat, label in [(haiku_pat, 'triage'), (opus_pat, 'proposer')]:
                    m = pat.search(line)
                    if m:
                        ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(
                            tzinfo=timezone.utc)
                        if ts >= cutoff:
                            inp, out = int(m.group(2)), int(m.group(3))
                            rates = _COST_RATES['deepseek-chat']
                            usd = (inp * rates[0] + out * rates[1]) / 1_000_000
                            rows.append({
                                'ts': ts.isoformat(), 'service': f'deepseek/{label}',
                                'model': 'deepseek-chat', 'in': inp, 'out': out,
                                'usd': round(usd, 6),
                            })
        except Exception as e:
            log.warning('cost report FX log error: %s', e)

    if not rows:
        return f'`No cost data ({days}d)`'

    by_svc = _dd(lambda: {'in': 0, 'out': 0, 'usd': 0.0, 'calls': 0})
    total_usd = 0.0
    for r in rows:
        k = r['service']
        by_svc[k]['in'] += r.get('in', 0)
        by_svc[k]['out'] += r.get('out', 0)
        by_svc[k]['usd'] += r.get('usd', 0.0)
        by_svc[k]['calls'] += 1
        total_usd += r.get('usd', 0.0)

    lines = [f'{days}d API cost breakdown:']
    for svc, d in sorted(by_svc.items()):
        lines.append(
            f'  {svc:<20s}  {d["calls"]:3d}x  in={d["in"]:,}  out={d["out"]:,}  ${d["usd"]:.4f}'
        )
    total_calls = sum(d['calls'] for d in by_svc.values())
    lines.append(f'  {"TOTAL":<20s}  {total_calls:3d}x  ${total_usd:.4f}')
    return '```\n' + '\n'.join(lines) + '\n```'


# Optional integration: an upstream tool produces JSON "ideas reports"
# in this folder (one per source item). Set BRIDGE_IDEAS_DIR to point
# at it and the /ideas command will browse them. Unset = /ideas no-ops.
# Example shape: {"reel_id": "ABC", "ideas": [
#   {"idea": "...", "novelty_score": 7, "viability_score": 6,
#    "weekend_buildable": True, "stack": ["nextjs","supabase"]}, ...]}
_IDEAS_DIR_RAW = os.environ.get('BRIDGE_IDEAS_DIR', '').strip()
IDEAS_DIR = pathlib.Path(_IDEAS_DIR_RAW) if _IDEAS_DIR_RAW else None


def _ideas_report(arg: str) -> str:
    """Browse extracted ideas reports. Usage: /ideas [weekend|top N|<keyword>]"""
    arg = (arg or '').strip().lower()
    if IDEAS_DIR is None:
        return ('`/ideas not configured. Set BRIDGE_IDEAS_DIR to a folder of '
                'JSON reports to enable.`')
    if not IDEAS_DIR.exists():
        return f'`No reports found at {IDEAS_DIR}.`'

    weekend_only = False
    keyword = ''
    limit = 12
    for tok in arg.split():
        if tok == 'weekend':
            weekend_only = True
        elif tok.isdigit():
            limit = max(1, min(40, int(tok)))
        elif tok == 'top':
            continue
        else:
            keyword = tok if not keyword else keyword + ' ' + tok

    ideas = []
    for fp in IDEAS_DIR.glob('*.json'):
        try:
            d = json.loads(fp.read_text(encoding='utf-8'))
        except Exception:
            continue
        rid = d.get('reel_id', fp.stem)
        for i in d.get('ideas', []):
            if weekend_only and not i.get('weekend_buildable'):
                continue
            text = i.get('idea', '')
            if keyword and keyword not in text.lower() and keyword not in ' '.join(i.get('stack', [])).lower():
                continue
            ideas.append({
                'reel': rid,
                'idea': text,
                'score': (i.get('novelty_score', 0) or 0) + (i.get('viability_score', 0) or 0),
                'novelty': i.get('novelty_score', 0) or 0,
                'viability': i.get('viability_score', 0) or 0,
                'weekend': i.get('weekend_buildable', False),
            })

    if not ideas:
        return f'`No matching ideas (weekend={weekend_only} keyword={keyword!r}).`'

    ideas.sort(key=lambda x: -x['score'])
    total = len(ideas)
    ideas = ideas[:limit]

    lines = [f'IG ideas: showing {len(ideas)}/{total}'
             + (f' weekend' if weekend_only else '')
             + (f' "{keyword}"' if keyword else '')]
    for i in ideas:
        flag = 'W' if i['weekend'] else ' '
        text = i['idea']
        if len(text) > 110:
            text = text[:107] + '...'
        lines.append(f"[{i['novelty']}+{i['viability']}={i['score']:>2}] {flag} {i['reel']} {text}")
    body = '\n'.join(lines)
    if len(body) > 3800:
        body = body[:3800] + '\n…(truncated)'
    return '```\n' + body + '\n```'


# When 1 (default), /think, /ask, classify, and summarise all go through the
# `claude` CLI, which uses the user's Claude Code SUBSCRIPTION instead of
# burning Anthropic API credits. Flip to 0 to force the old API path.
USE_CLI_FOR_AI = os.environ.get('USE_CLI_FOR_AI', '1') not in ('0', 'false', 'no', '')

# Optional fallback: if API keys aren't in the process environment, try
# loading them from a sibling .env file next to this script (KEY=value
# format, one per line). Process env always wins. Useful if you keep
# secrets out of your shell profile but in a local .env file.
_BRIDGE_ENV_FILE = pathlib.Path(__file__).resolve().parent / '.env'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('mem-bot')


# ---------- keys ----------

def _load_env_key(name):
    """Read a named API key. Process env first, then .env sibling file
    (KEY=value, one per line; comments with `#` ignored). Empty string
    if not found anywhere."""
    key = os.environ.get(name, '').strip()
    if key:
        return key
    if _BRIDGE_ENV_FILE.exists():
        try:
            for line in _BRIDGE_ENV_FILE.read_text(encoding='utf-8', errors='ignore').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith(f'{name}='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ''


ANTHROPIC_KEY = _load_env_key('ANTHROPIC_API_KEY')
OPENAI_KEY    = _load_env_key('OPENAI_API_KEY')
DEEPSEEK_KEY  = _load_env_key('DEEPSEEK_API_KEY')


def deepseek_chat_call(prompt, *, system=None, model='deepseek-chat',
                       max_tokens=2048, temperature=0.5, timeout=120):
    """One-shot DeepSeek chat completion. Returns assistant text.

    Used as fallback when `claude_cli_call` fails (cap hit, exit 1, timeout).
    No conversation history. Caller is responsible for stitching history into
    the prompt if continuity is needed across fallback turns.
    """
    if not DEEPSEEK_KEY:
        raise RuntimeError('DEEPSEEK_API_KEY not configured')
    msgs = []
    if system:
        msgs.append({'role': 'system', 'content': system})
    msgs.append({'role': 'user', 'content': prompt})
    payload = {
        'model': model,
        'messages': msgs,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'stream': False,
    }
    r = requests.post(
        'https://api.deepseek.com/chat/completions',
        headers={'Authorization': f'Bearer {DEEPSEEK_KEY}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f'deepseek {r.status_code}: {r.text[:300]}')
    data = r.json()
    content = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
    usage = data.get('usage') or {}
    try:
        _log_cost('deepseek/think', model,
                  usage.get('prompt_tokens', 0),
                  usage.get('completion_tokens', 0))
    except Exception:
        pass
    return (content or '').strip()

_anthropic_client = None
_openai_client = None


def claude_client():
    global _anthropic_client
    if _anthropic_client is None and anthropic and ANTHROPIC_KEY:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client


def openai_client():
    global _openai_client
    if _openai_client is None and openai_sdk and OPENAI_KEY:
        _openai_client = openai_sdk.OpenAI(api_key=OPENAI_KEY)
    return _openai_client


# ---------- password / session ----------

unlock_expiry = {}      # chat_id -> datetime (UTC) when unlock expires
failed_attempts = {}    # chat_id -> (count, last_fail datetime)


def _hash_password(password, salt):
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest()


def set_password(password):
    salt = secrets.token_hex(16)
    PASSHASH_FILE.write_text(json.dumps({
        'salt': salt,
        'hash': _hash_password(password, salt),
    }), encoding='utf-8')


def check_password(password):
    if not PASSHASH_FILE.exists():
        return False
    data = json.loads(PASSHASH_FILE.read_text(encoding='utf-8'))
    return hmac.compare_digest(_hash_password(password, data['salt']), data['hash'])


def now():
    return datetime.now(timezone.utc)


def is_locked_out(chat_id):
    rec = failed_attempts.get(chat_id)
    if not rec:
        return False
    count, last = rec
    if count < MAX_FAILED:
        return False
    if now() - last > timedelta(minutes=LOCKOUT_MINUTES):
        failed_attempts.pop(chat_id, None)
        return False
    return True


def record_fail(chat_id):
    count, _ = failed_attempts.get(chat_id, (0, now()))
    failed_attempts[chat_id] = (count + 1, now())


def record_success(chat_id):
    failed_attempts.pop(chat_id, None)
    unlock_expiry[chat_id] = now() + timedelta(minutes=UNLOCK_MINUTES)


def is_unlocked(chat_id):
    exp = unlock_expiry.get(chat_id)
    return bool(exp and exp > now())


def lock(chat_id):
    unlock_expiry.pop(chat_id, None)


SENSITIVE_CMDS = {
    '/remember', '/feedback', '/project', '/user', '/reference',
    '/forget', '/think', '/reset',
    '/cc', '/cc-new', '/cc-stop', '/cd', '/cc-model', '/cc-effort',
}


# ---------- telegram ----------

def tg(method, **params):
    try:
        r = requests.post(f'{API}/{method}', data=params, timeout=35)
        return r.json()
    except Exception as e:
        log.error('telegram %s failed: %s', method, e)
        return {'ok': False, 'error': str(e)}


def reply(chat_id, text):
    # Telegram caps messages at 4096 chars; chunk if larger.
    text = text or '(empty)'
    while text:
        chunk, text = text[:3900], text[3900:]
        tg('sendMessage', chat_id=chat_id, text=chunk, parse_mode='Markdown',
           disable_web_page_preview='true')


def reply_plain(chat_id, text):
    text = text or '(empty)'
    while text:
        chunk, text = text[:3900], text[3900:]
        tg('sendMessage', chat_id=chat_id, text=chunk, disable_web_page_preview='true')


def edit_message(chat_id, message_id, text):
    text = (text or '')[:3900]
    tg('editMessageText', chat_id=chat_id, message_id=message_id,
       text=text, disable_web_page_preview='true')


def _is_overloaded(e):
    return getattr(e, 'status_code', None) == 529


def _create_with_fallback(client, models, **kwargs):
    """Call messages.create trying models in order; retry once on 529 per model."""
    last_err = None
    for i, model in enumerate(models):
        for attempt in range(2):
            try:
                return client.messages.create(model=model, **kwargs)
            except Exception as e:
                last_err = e
                if not _is_overloaded(e):
                    raise
                if attempt == 0:
                    log.warning('overloaded on %s, retrying…', model)
                    time.sleep(2)
                else:
                    log.warning('overloaded on %s after retry; next model', model)
    raise last_err


def _format_api_error(e):
    """Extract a user-friendly one-liner from an anthropic/openai API error."""
    msg = None
    body = getattr(e, 'body', None)
    if isinstance(body, dict):
        err = body.get('error')
        if isinstance(err, dict):
            msg = err.get('message')
    if not msg:
        resp = getattr(e, 'response', None)
        if resp is not None:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    err = data.get('error')
                    if isinstance(err, dict):
                        msg = err.get('message')
            except Exception:
                pass
    if not msg:
        msg = str(e)
    status = getattr(e, 'status_code', None)
    label = f'API error {status}' if status else 'API error'
    return f'⚠️ {label}: {msg}'


# ---------- help ----------

HELP_TEXT = (
    '*Mushi commands*\n\n'
    '_Low-stakes (always):_\n'
    '`/ask <q>`   search memories and answer (Haiku)\n'
    '`/list`      show the MEMORY.md index\n'
    '`/help`      this message\n'
    '`/status`    unlock state, /think history, /cc job + config\n'
    '`/cost`      7-day API cost breakdown (bot + any BRIDGE_EXTRA_COST_LOGS)\n'
    '`/ideas`     browse idea reports (requires BRIDGE_IDEAS_DIR). args: `weekend`, `top N`, `<keyword>`\n\n'
    '_Unlock first_ (`/unlock <password>`, 60 min window):\n'
    'Plain text   → auto-classified and filed\n'
    'Voice msg    → transcribed, then routed\n'
    '`/think <q>` Opus 4.7 conversation, memory-aware, multi-turn\n'
    '`/reset`     clear /think conversation\n\n'
    '_Claude Code bridge:_\n'
    '`/cc <task>` run Claude Code. Live-streams tool use + messages.\n'
    '`/cc-new`    fresh session (don\'t resume previous)\n'
    '`/cc-stop`   kill the active /cc job\n'
    '`/cd <path>` set working directory for /cc\n'
    '`/cc-model <m>`  sonnet / opus / haiku / full-name\n'
    '`/cc-effort <e>` low / medium / high / xhigh / max\n\n'
    '_Memory write:_\n'
    '`/remember`  explicit form: `<type> <title>: <body>`\n'
    '`/feedback`, `/project`, `/user`, `/reference`: typed shortcuts\n'
    '`/forget <slug>` delete a memory\n\n'
    '`/lock`      end the unlock window now'
)


# ---------- memory I/O ----------

def slugify(s, maxlen=50):
    s = re.sub(r'[^a-zA-Z0-9]+', '_', s.lower()).strip('_')
    return (s[:maxlen] or 'untitled').rstrip('_')


def ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text('', encoding='utf-8')


def append_index(title, filename, hook):
    line = f'- [{title}]({filename}): {hook}'
    content = INDEX_FILE.read_text(encoding='utf-8') if INDEX_FILE.exists() else ''
    if line in content:
        return
    if content and not content.endswith('\n'):
        content += '\n'
    INDEX_FILE.write_text(content + line + '\n', encoding='utf-8')


def remove_from_index(filename):
    if not INDEX_FILE.exists():
        return False
    lines = INDEX_FILE.read_text(encoding='utf-8').splitlines()
    kept = [ln for ln in lines if f']({filename})' not in ln]
    changed = len(kept) != len(lines)
    if changed:
        INDEX_FILE.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    return changed


def write_memory(mem_type, title, body, hook=None):
    slug = slugify(title)
    filename = f'{mem_type}_{slug}.md'
    path = MEMORY_DIR / filename
    n = 1
    while path.exists():
        filename = f'{mem_type}_{slug}_{n}.md'
        path = MEMORY_DIR / filename
        n += 1
    desc = (hook or body.splitlines()[0] if body else title)[:140]
    front = (
        '---\n'
        f'name: {title}\n'
        f'description: {desc}\n'
        f'type: {mem_type}\n'
        '---\n\n'
        f'{body}\n'
    )
    path.write_text(front, encoding='utf-8')
    append_index(title, filename, (hook or desc)[:120])
    return filename


def parse_remember(raw, default_type=None):
    raw = raw.strip()
    mem_type = default_type
    if not mem_type:
        first, _, rest = raw.partition(' ')
        if first.lower() in VALID_TYPES:
            mem_type, raw = first.lower(), rest.strip()
        else:
            mem_type = 'project'
    if ':' in raw:
        title, _, body = raw.partition(':')
        title, body = title.strip(), body.strip()
    else:
        words = raw.split()
        title = ' '.join(words[:6]) if words else 'Note'
        body = ' '.join(words[6:]) if len(words) > 6 else raw
    return mem_type, title or 'Note', body


# ---------- memory context for LLMs ----------

def load_memory_context(include_recent=True, recent_char_cap=2000):
    """Return MEMORY.md index + optional body of the most recently-written memory file.

    Previously dumped full bodies of up to ~22k chars worth of memory files into
    every system prompt. That burned ~5k tokens per /think turn on boilerplate.
    Now: index only (so Claude knows what exists) plus one freshness hint. Claude
    pulls specific bodies on demand via the `read_memory` tool.
    """
    parts = []
    if INDEX_FILE.exists():
        parts.append('## MEMORY.md\n' + INDEX_FILE.read_text(encoding='utf-8'))
    if include_recent:
        candidates = [f for f in MEMORY_DIR.glob('*.md') if f.name != 'MEMORY.md']
        if candidates:
            newest = max(candidates, key=lambda f: f.stat().st_mtime)
            body = newest.read_text(encoding='utf-8', errors='ignore')[:recent_char_cap]
            parts.append(f'\n\n## Most recent memory: {newest.name}\n{body}')
    return ''.join(parts)


# ---------- claude CLI backend (uses Code SUBSCRIPTION, not API credits) ----------
#
# Every AI call in this file has an equivalent `*_cli` variant that shells out
# to the `claude` binary. The CLI bills against the Claude Code subscription,
# not the Anthropic API. When USE_CLI_FOR_AI is on (default), all dispatchers
# prefer the CLI path and only fall back to the API if the CLI errors.

def _claude_cli_exe():
    # `claude.cmd` on Windows, `claude` elsewhere. Resolve via shutil so the
    # subprocess call works without caller worrying about .cmd vs no-extension.
    exe = shutil.which(CLAUDE_CLI) or shutil.which(CLAUDE_CLI + '.cmd')
    return exe or CLAUDE_CLI


def claude_cli_call(prompt, *, system=None, session_id=None, resume=False,
                    model='sonnet', effort=None, cwd=None, timeout=300):
    """Run `claude -p` headless. Returns the final assistant text.

    Uses the user's Claude Code SUBSCRIPTION. No API credits consumed.
    `model` accepts short aliases (sonnet/opus/haiku) or full IDs.

    Session handling (CLI >=2.1.118):
    - `--session-id <uuid>` CREATES a new session; fails if the UUID exists.
    - `--resume <uuid>` RESUMES an existing session.
    Pass resume=True once the session has been created at least once.
    On "already in use" from a --session-id call (e.g. the session file
    survived a prior crash), we retry with --resume automatically.
    """
    exe = _claude_cli_exe()

    def _build_args(use_resume):
        a = [exe, '-p', prompt,
             '--output-format', 'json',
             '--model', model,
             '--dangerously-skip-permissions']
        if effort and effort != 'default':
            a += ['--effort', effort]
        if session_id:
            a += (['--resume', session_id] if use_resume else ['--session-id', session_id])
        if system:
            a += ['--append-system-prompt', system]
        return a

    # Critical: strip API auth env vars so the CLI falls back to the OAuth
    # subscription (~/.claude/.credentials.json). Otherwise the depleted API
    # key loaded by the instagram_analyser .env would be used and every call
    # fails with "Credit balance is too low".
    child_env = dict(os.environ)
    for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
        child_env.pop(k, None)

    def _run(use_resume):
        try:
            return subprocess.run(
                _build_args(use_resume), cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                env=child_env,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise RuntimeError(f'claude CLI not found: {exe}')

    proc = _run(use_resume=resume)
    # Recover from stale session files: if --session-id fails with "already
    # in use" (a prior call created the session but our flag didn't persist),
    # retry once as --resume.
    if (proc.returncode != 0 and session_id and not resume
            and 'already in use' in (proc.stderr or proc.stdout or '').lower()):
        proc = _run(use_resume=True)
    if proc.returncode != 0:
        raw = (proc.stdout or proc.stderr or '').strip()
        msg = raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get('is_error') and data.get('result'):
                msg = str(data['result'])
        except Exception:
            pass
        raise RuntimeError(
            f'claude CLI exit {proc.returncode}: {msg.strip()[:2000]}'
        )
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return (proc.stdout or '').strip()
    return (data.get('result') or data.get('content') or '').strip()


# ---------- classify ----------

CLASSIFY_SYSTEM = (
    "You file thoughts into a Claude Code memory system. Types:\n"
    "- user: who the user is, their role/skills/preferences\n"
    "- feedback: rules for how Claude should work (with a Why)\n"
    "- project: facts about ongoing work, deadlines, decisions (with a Why)\n"
    "- reference: pointers to external systems\n\n"
    "Return ONLY a JSON object, no prose, no code fences. Keys:\n"
    '  "type": user|feedback|project|reference\n'
    '  "title": 2-8 words, no trailing punctuation\n'
    '  "body": the thought rewritten cleanly. For feedback/project include **Why:** and **How to apply:** lines.\n'
    '  "hook": one-line summary <=100 chars\n'
)


def _extract_json(text):
    text = (text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.S)
    m = re.search(r'\{.*\}', text, re.S)
    return json.loads(m.group(0) if m else text)


def _classify_parse(raw, text):
    try:
        data = _extract_json(raw)
    except Exception as e:
        log.warning('classify parse failed: %s (%r)', e, (raw or '')[:200])
        return None
    t = (data.get('type') or '').lower()
    if t not in VALID_TYPES:
        t = 'project'
    return (t,
            (data.get('title') or 'Note').strip(),
            (data.get('body') or text).strip(),
            (data.get('hook') or '').strip())


def classify_thought_cli(text):
    try:
        raw = claude_cli_call(
            f'Thought: {text}\n\nReturn ONLY the JSON object, no code fences, no prose.',
            system=CLASSIFY_SYSTEM,
            model='haiku',
            timeout=60,
        )
    except Exception as e:
        log.warning('classify_cli error: %s', e)
        return None
    return _classify_parse(raw, text)


def classify_thought_api(text):
    c = claude_client()
    if not c:
        return None
    try:
        resp = c.messages.create(
            model=HAIKU,
            max_tokens=700,
            system=CLASSIFY_SYSTEM,
            messages=[{'role': 'user', 'content': f'Thought: {text}'}],
        )
        if resp.usage:
            _log_cost('anthropic/classify', resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception as e:
        log.error('classify API error: %s', e)
        return None
    return _classify_parse(resp.content[0].text if resp.content else '', text)


def classify_thought(text):
    if USE_CLI_FOR_AI:
        out = classify_thought_cli(text)
        if out is not None:
            return out
        log.info('classify_cli failed, trying API fallback')
    return classify_thought_api(text)


# ---------- /ask (Haiku + prompt cache) ----------

ASK_SYSTEM = (
    "You answer questions from the user's personal memory store, which lives at "
    f"{MEMORY_DIR}. It's a folder of markdown files plus an index at MEMORY.md.\n\n"
    "Workflow: Read MEMORY.md first to see what exists, Grep or Read specific files "
    "for details, then answer. Cite filenames inline like `feedback_autonomy.md`. "
    "If memory doesn't cover the question, say so. Don't guess."
)


def ask_memory_cli(question):
    index = INDEX_FILE.read_text(encoding='utf-8', errors='ignore') if INDEX_FILE.exists() else ''
    system = ASK_SYSTEM + ('\n\nIndex snapshot:\n' + index if index else '')
    try:
        return claude_cli_call(
            question,
            system=system,
            model='haiku',
            cwd=str(MEMORY_DIR),
            timeout=180,
        )
    except Exception as e:
        log.error('ask_cli error: %s', e)
        return None


def ask_memory_api(question):
    c = claude_client()
    if not c:
        return None
    ctx = load_memory_context()
    try:
        resp = c.messages.create(
            model=HAIKU,
            max_tokens=2000,
            system=[
                {'type': 'text',
                 'text': ("Answer using ONLY the memory notes provided. Cite filenames inline "
                          "like `feedback_autonomy.md`. If the notes don't cover it, say so. "
                          "Be thorough when the question calls for it.")},
                {'type': 'text', 'text': f'Memory notes:\n{ctx}',
                 'cache_control': {'type': 'ephemeral'}},
            ],
            messages=[{'role': 'user', 'content': question}],
        )
        if resp.usage:
            _log_cost('anthropic/ask', resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception as e:
        log.error('ask API error: %s', e)
        return _format_api_error(e)
    return resp.content[0].text if resp.content else ''


def ask_memory(question):
    if USE_CLI_FOR_AI:
        out = ask_memory_cli(question)
        if out:
            return out
        log.info('ask_cli returned empty, trying API fallback')
    return ask_memory_api(question)


# ---------- /think (Opus, multi-turn, rolling summary) ----------

think_sessions = {}   # chat_id -> {'history': [...], 'summary': str, 'last': datetime}
THINK_HISTORY_CHARS = 18000
THINK_KEEP_RECENT = 6   # most recent turns (user+assistant pairs) kept verbatim


THINK_TOOLS = [
    {
        'name': 'save_memory',
        'description': ('Save a new memory note to the user\'s persistent memory system. '
                        'Use when the user says "remember this", "save that", or when you '
                        'identify a durable fact, preference, or decision worth persisting. '
                        'feedback/project types should include Why and How-to-apply in the body.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'type':  {'type': 'string', 'enum': ['user', 'feedback', 'project', 'reference']},
                'title': {'type': 'string', 'description': '2-8 words, no trailing punctuation'},
                'body':  {'type': 'string', 'description': 'Full memory content in markdown'},
                'hook':  {'type': 'string', 'description': 'One-line summary <=100 chars'},
            },
            'required': ['type', 'title', 'body'],
        },
    },
    {
        'name': 'read_memory',
        'description': 'Read the full content of a specific memory file by filename (with or without .md).',
        'input_schema': {
            'type': 'object',
            'properties': {'filename': {'type': 'string'}},
            'required': ['filename'],
        },
    },
    {
        'name': 'forget_memory',
        'description': 'Delete a memory file. Requires explicit user confirmation in the conversation.',
        'input_schema': {
            'type': 'object',
            'properties': {'filename': {'type': 'string'}},
            'required': ['filename'],
        },
    },
    {
        'name': 'launch_cc',
        'description': ('Launch a Claude Code task on the user\'s PC. This runs the real `claude` CLI '
                        'non-interactively, streams tool use and messages back to the Telegram chat '
                        'as they happen, and finishes with the assistant\'s final answer. '
                        'Multiple jobs can run in parallel in the same chat. Each gets a short job_id. '
                        'Use for any request that requires reading/writing files, running commands, '
                        'or inspecting project state. `cwd` should be the absolute path of the project '
                        'to work in (defaults to the current chat\'s cwd). `new_session=true` starts fresh; '
                        'otherwise it may resume the last idle /cc session in that chat. '
                        'Returns the job_id in the result. Pass it to cc_status / cc_stop. '
                        'IMAGE/FILE DELIVERY: the launched cc can push a file to this Telegram chat by '
                        'emitting the sentinel `[[SEND_IMAGE: <abs_path>]]` or `[[SEND_FILE: <abs_path>]]` '
                        'in any of its output (assistant text, final answer, or even a bash echo). '
                        'The bridge strips the sentinel and calls Telegram sendPhoto (<=10MB .png/.jpg/'
                        '.jpeg/.gif/.webp) or sendDocument (everything else, <=50MB). Tell the launched '
                        'cc to use this whenever it generates an image the user should see: QR codes, '
                        'charts, screenshots, reel thumbnails.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string', 'description': 'The task for Claude Code'},
                'cwd':    {'type': 'string', 'description': 'Absolute working directory'},
                'new_session': {'type': 'boolean'},
            },
            'required': ['prompt'],
        },
    },
    {
        'name': 'cc_status',
        'description': ('Inspect /cc jobs in this chat. With no arg, lists every job (running or '
                        'recently finished) one line each. With job_id, returns full status and '
                        'the last few events for that job.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'description': 'Optional 6-char job id'},
            },
        },
    },
    {
        'name': 'cc_stop',
        'description': ('Kill a running /cc job. With job_id, kills that one. With no arg, kills '
                        'every running job in this chat (panic button). Returns which ids were '
                        'killed, or `no such job: <id>` if the id is unknown.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'string', 'description': 'Optional 6-char job id'},
            },
        },
    },
]


def _think_tool(name, inp, chat_id):
    try:
        if name == 'save_memory':
            mt = (inp.get('type') or 'project').lower()
            if mt not in VALID_TYPES:
                mt = 'project'
            fname = write_memory(mt, inp.get('title') or 'Note',
                                 inp.get('body') or '', inp.get('hook'))
            reply(chat_id, f'_(Opus filed {mt}: `{fname}`)_')
            return f'Saved {fname}'
        if name == 'read_memory':
            slug = (inp.get('filename') or '').strip()
            if not slug.endswith('.md'):
                slug += '.md'
            p = MEMORY_DIR / slug
            if not p.exists():
                matches = list(MEMORY_DIR.glob(f'*{slug[:-3]}*.md'))
                if len(matches) == 1:
                    p = matches[0]
                else:
                    return f'not found: {slug}'
            return p.read_text(encoding='utf-8', errors='ignore')[:8000]
        if name == 'forget_memory':
            slug = (inp.get('filename') or '').strip()
            if not slug.endswith('.md'):
                slug += '.md'
            p = MEMORY_DIR / slug
            if not p.exists():
                return f'not found: {slug}'
            p.unlink()
            remove_from_index(p.name)
            reply(chat_id, f'_(Opus deleted `{p.name}`)_')
            return f'deleted {p.name}'
        if name == 'launch_cc':
            prompt = (inp.get('prompt') or '').strip()
            if not prompt:
                return 'error: prompt required'
            cwd = inp.get('cwd')
            if cwd:
                pp = pathlib.Path(cwd)
                if pp.exists() and pp.is_dir():
                    cc_config[chat_id]['cwd'] = str(pp)
                else:
                    return f'error: cwd does not exist: {cwd}'
            job, err = cc_start(chat_id, prompt, new_session=bool(inp.get('new_session')))
            if job:
                return (f'cc launched. Output will stream separately '
                        f'(job_id: {job.job_id})')
            return f'failed: {err}'
        if name == 'cc_status':
            return cc_status_text(chat_id, (inp.get('job_id') or '').strip() or None)
        if name == 'cc_stop':
            return cc_stop_tool(chat_id, (inp.get('job_id') or '').strip() or None)
        return f'unknown tool: {name}'
    except Exception as e:
        log.error('think tool %s failed: %s', name, traceback.format_exc())
        return f'error: {e}'


def _content_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                t = b.get('type')
                if t == 'text':
                    parts.append(b.get('text', ''))
                elif t == 'tool_use':
                    parts.append(f"[tool:{b.get('name')} {json.dumps(b.get('input', {}))[:200]}]")
                elif t == 'tool_result':
                    parts.append(f"[result: {str(b.get('content',''))[:200]}]")
        return '\n'.join(parts)
    return str(c)


def _assistant_to_dicts(content):
    out = []
    for blk in content:
        t = getattr(blk, 'type', None)
        if t == 'text':
            out.append({'type': 'text', 'text': blk.text})
        elif t == 'tool_use':
            out.append({'type': 'tool_use', 'id': blk.id, 'name': blk.name, 'input': blk.input})
    return out


def _summarise_old(history):
    """Compress old turns into a short summary. Prefers CLI (sub), falls back to API."""
    if not history:
        return ''
    transcript = '\n\n'.join(
        f"{m['role'].upper()}: {_content_text(m['content'])}"
        for m in history
    )[:30000]
    sys_prompt = ("Summarise this conversation into 6-10 bullet points capturing "
                  "the user's ideas, decisions, and open threads. Be dense.")
    if USE_CLI_FOR_AI:
        try:
            return claude_cli_call(transcript, system=sys_prompt, model='haiku', timeout=90)
        except Exception as e:
            log.warning('summarise_cli error: %s; falling back to API', e)
    c = claude_client()
    if not c:
        return ''
    try:
        resp = c.messages.create(
            model=HAIKU,
            max_tokens=500,
            system=sys_prompt,
            messages=[{'role': 'user', 'content': transcript}],
        )
        if resp.usage:
            _log_cost('anthropic/summarise', resp.model, resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception as e:
        log.warning('summarise API error: %s', e)
        return ''
    return resp.content[0].text if resp.content else ''


def _maybe_compact(chat_id):
    s = think_sessions.get(chat_id)
    if not s:
        return
    total = sum(len(_content_text(m['content'])) for m in s['history'])
    if total < THINK_HISTORY_CHARS:
        return
    split = max(0, len(s['history']) - THINK_KEEP_RECENT * 2)
    old, recent = s['history'][:split], s['history'][split:]
    if not old:
        return
    # never split a tool_use from its tool_result: if first recent turn is a tool_result,
    # walk back one more.
    while recent and isinstance(recent[0].get('content'), list) and any(
        isinstance(b, dict) and b.get('type') == 'tool_result' for b in recent[0]['content']
    ):
        old.append(recent.pop(0))
    log.info('compacting /think history: %d old turns -> summary', len(old))
    summary = _summarise_old(old)
    existing = s.get('summary', '')
    s['summary'] = (existing + '\n\n' + summary).strip() if existing else summary
    s['history'] = recent


THINK_SYSTEM = (
    "You are the user's intellectual sparring partner, running inside a Telegram bot "
    "on their personal PC. You have REAL TOOLS. Use them, don't hedge:\n"
    "  - save_memory / read_memory / forget_memory: the user's long-term memory system. "
    "Save durable facts, preferences, decisions. Read when context would help.\n"
    "  - launch_cc: run Claude Code headless on the PC. This is REAL execution. "
    "it can read/write files, run commands, inspect projects. Use it whenever the user "
    "wants something done, not just discussed. The output streams back to this chat live. "
    "The launched cc can also push images/files to this chat by printing "
    "`[[SEND_IMAGE: <abs_path>]]` or `[[SEND_FILE: <abs_path>]]`. Use this for QR codes, "
    "charts, screenshots, anything visual.\n"
    "  - cc_status: check what an already-running /cc job is doing.\n\n"
    "NEVER say you can't run code, access files, or act on the PC; you can, via launch_cc. "
    "When the user asks you to remember something, just call save_memory; don't ask for permission. "
    "Engage critically with ideas: steelman, challenge, connect to prior thoughts. "
    "Reference memory files by filename. Be direct, no filler."
)


# ---------- /think via CLI (subscription) ----------

# Per-chat claude CLI session IDs for /think. The CLI persists history under
# this session, so re-using the id across turns = multi-turn continuity, and
# no conversation transcript needs to be resent each time.
#
# Persisted to disk (2026-04-30): bot restarts no longer break /think continuity.
# Without this, killing the bot to pick up a code change forced the user to
# start a fresh conversation, which led to the Phase-4-mid-flight DeepSeek
# fallback episode. The session_id mapping is the only piece of bot state
# that needs survive a restart; chat_id ↔ session_id ↔ on-disk claude history.
think_cli_sessions = {}   # chat_id -> {'session_id': str, 'model': str, 'effort': str, 'last': datetime, 'created': bool}
THINK_SESSIONS_FILE = MEMORY_DIR / '.think_sessions.json'


def _load_think_cli_sessions() -> None:
    """Restore think_cli_sessions from disk on startup. Best-effort; any
    parse error or missing file means we start fresh, which is functionally
    fine (the user gets a new session, not a broken one)."""
    try:
        if not THINK_SESSIONS_FILE.exists():
            return
        raw = json.loads(THINK_SESSIONS_FILE.read_text(encoding='utf-8'))
        for chat_id_str, s in raw.items():
            chat_id = int(chat_id_str)
            last_str = s.get('last')
            try:
                last = datetime.fromisoformat(last_str) if last_str else now()
            except (TypeError, ValueError):
                last = now()
            think_cli_sessions[chat_id] = {
                'session_id': s['session_id'],
                'model': s.get('model', 'claude-opus-4-7'),
                'effort': s.get('effort', 'default'),
                'last': last,
                'created': bool(s.get('created', True)),
            }
        log.info('think_cli: restored %d session(s) from %s',
                 len(think_cli_sessions), THINK_SESSIONS_FILE)
    except Exception as e:
        log.warning('think_cli: failed to load sessions from %s: %s. '
                    'starting fresh', THINK_SESSIONS_FILE, e)


def _save_think_cli_sessions() -> None:
    """Persist think_cli_sessions after every mutation. Atomic write via
    temp + replace so a crash mid-write doesn't corrupt the file."""
    try:
        out = {}
        for chat_id, s in think_cli_sessions.items():
            out[str(chat_id)] = {
                'session_id': s['session_id'],
                'model': s.get('model', 'claude-opus-4-7'),
                'effort': s.get('effort', 'default'),
                'last': s['last'].isoformat() if isinstance(s.get('last'), datetime)
                        else str(s.get('last') or ''),
                'created': bool(s.get('created', True)),
            }
        tmp = THINK_SESSIONS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(out, indent=2), encoding='utf-8')
        tmp.replace(THINK_SESSIONS_FILE)
    except Exception as e:
        log.warning('think_cli: failed to save sessions: %s', e)

THINK_CLI_SYSTEM = (
    "You are the user's intellectual sparring partner running inside a Telegram "
    "bot on their PC. Engage critically. Steelman, challenge, connect ideas to "
    "prior memories. Be direct, no filler.\n\n"
    "You have FULL file access via your native tools (Read, Write, Edit, Bash, "
    "Grep, Glob). Use them freely; the user has already granted permission. "
    "When the user wants work done (run commands, edit code, build features), "
    "just do it. Don't say you can't.\n\n"
    "MEMORY SYSTEM:\n"
    f"The user's long-term memory lives at {MEMORY_DIR}\n"
    "  - MEMORY.md is the index (one line per memory file)\n"
    "  - Individual memory files have YAML frontmatter (name, description, type) "
    "then a markdown body.\n"
    "  - Types: user | feedback | project | reference.\n"
    "  - feedback/project bodies should include **Why:** and **How to apply:** lines.\n\n"
    "When the user says 'remember', 'save that', or shares a durable fact/decision "
    "worth keeping, SAVE IT without asking permission:\n"
    "  1. Write a new file at <MEMORY_DIR>/<type>_<slug>.md with proper frontmatter.\n"
    "  2. Edit MEMORY.md to append one line:\n"
    "       - [Title](filename.md): one-line hook\n\n"
    "When a question may be covered by memory, Read MEMORY.md first, then Read "
    "relevant files. Cite filenames inline like `feedback_autonomy.md`.\n\n"
    "TELEGRAM DELIVERY: to push an image/file to this chat, print the sentinel "
    "[[SEND_IMAGE: <abs_path>]] or [[SEND_FILE: <abs_path>]] on its own line in "
    "your final reply. The bridge strips it and sends the file."
)


def think_turn_cli(chat_id, user_msg, image_path=None):
    """One /think turn via `claude -p`. Uses subscription, not API credits."""
    s = think_cli_sessions.setdefault(chat_id, {
        'session_id': _cc_new_session_id(),
        'model': 'claude-opus-4-7',
        'effort': 'default',
        'last': now(),
        'created': False,
    })
    s['last'] = now()

    system = THINK_CLI_SYSTEM
    if INDEX_FILE.exists():
        idx = INDEX_FILE.read_text(encoding='utf-8', errors='ignore')
        system += '\n\nCURRENT MEMORY.md INDEX:\n' + idx

    prompt = user_msg if isinstance(user_msg, str) else ''
    if image_path:
        prompt = (f'Image attached at: {image_path}\n'
                  f'Use your Read tool to view it.\n\n'
                  f'User message: {prompt or "Describe what you see."}')

    try:
        out = claude_cli_call(
            prompt,
            system=system,
            session_id=s['session_id'],
            resume=s['created'],
            model=s['model'],
            effort=s.get('effort', 'default'),
            cwd=str(MEMORY_DIR),
            timeout=1800,
        )
        s['created'] = True
        _save_think_cli_sessions()
        return out or '_(no reply)_'
    except subprocess.TimeoutExpired:
        # A DeepSeek fallback used to fire here, but it has no tools and
        # no session continuity, and tended to invent code references to
        # files / modules that don't exist in the actual repo. Cost-of-
        # fallback outweighed cost-of-retry. Session_id continuity means
        # the next /think turn picks up exactly where this one left off.
        log.warning('think_cli: claude timeout after 30 min. Session preserved')
        # Save anyway so the persisted state reflects the most recent
        # session_id even if this turn timed out (continuity of the next turn).
        _save_think_cli_sessions()
        return '_(Claude timed out after 30 min. Session preserved, just retry)_'
    except (RuntimeError, Exception) as e:
        reason = str(e)[:1500]
        log.warning('think_cli: claude failed (%s). Session preserved', reason)
        _save_think_cli_sessions()
        return f'_(Claude error: {reason}. Session preserved, just retry)_'


def reset_think_cli(chat_id):
    think_cli_sessions.pop(chat_id, None)
    _save_think_cli_sessions()


# ---------- /think via API (legacy) ----------


def think_turn_api(chat_id, user_msg):
    c = claude_client()
    if not c:
        return 'Claude API not configured.'
    s = think_sessions.setdefault(chat_id, {'history': [], 'summary': '', 'last': now()})
    s['last'] = now()

    system_blocks = [
        {'type': 'text', 'text': THINK_SYSTEM},
        {'type': 'text',
         'text': 'User memory snapshot:\n' + load_memory_context(),
         'cache_control': {'type': 'ephemeral'}},
    ]
    if s['summary']:
        system_blocks.append({'type': 'text',
                              'text': 'Earlier conversation summary:\n' + s['summary']})

    s['history'].append({'role': 'user', 'content': user_msg})

    final_text_parts = []
    for hop in range(6):  # tool-use loop, capped
        try:
            resp = _create_with_fallback(
                c,
                models=[OPUS, SONNET],
                max_tokens=4000,
                system=system_blocks,
                tools=THINK_TOOLS,
                messages=s['history'],
            )
        except Exception as e:
            log.error('think API error: %s', e)
            # Roll back the partial assistant turn / user msg so next try isn't stuck.
            if s['history'] and s['history'][-1].get('role') == 'user' and hop == 0:
                s['history'].pop()
            return _format_api_error(e)
        assistant_dicts = _assistant_to_dicts(resp.content)
        s['history'].append({'role': 'assistant', 'content': assistant_dicts})

        for blk in assistant_dicts:
            if blk['type'] == 'text' and blk.get('text'):
                final_text_parts.append(blk['text'])

        if resp.stop_reason != 'tool_use':
            break

        tool_results = []
        for blk in assistant_dicts:
            if blk['type'] != 'tool_use':
                continue
            result = _think_tool(blk['name'], blk.get('input') or {}, chat_id)
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': blk['id'],
                'content': str(result)[:8000],
            })
        s['history'].append({'role': 'user', 'content': tool_results})
    else:
        final_text_parts.append('\n\n_(stopped after 6 tool hops)_')

    _maybe_compact(chat_id)
    return '\n\n'.join(p for p in final_text_parts if p).strip() or '_(no text reply)_'


def reset_think(chat_id):
    think_sessions.pop(chat_id, None)
    reset_think_cli(chat_id)


# ---------- /think dispatcher: CLI-first (subscription), API fallback ----------

# Per-chat lock so /think turns serialize within a chat (preserving CLI session
# continuity) but never block the polling loop or other chats' turns.
_think_locks = {}
_think_locks_guard = threading.Lock()


def _get_think_lock(chat_id):
    with _think_locks_guard:
        lock = _think_locks.get(chat_id)
        if lock is None:
            lock = threading.Lock()
            _think_locks[chat_id] = lock
        return lock


def think_async(chat_id, work):
    """Run a /think call (text or image) in a worker thread so handle_update
    returns immediately. `work` is a zero-arg callable that returns the reply
    text. Replies are serialized per chat via _think_locks.
    """
    def run():
        lock = _get_think_lock(chat_id)
        with lock:
            try:
                out = work()
            except Exception as e:
                if anthropic is not None and isinstance(e, anthropic.APIError):
                    reply_plain(chat_id, _format_api_error(e))
                    log.warning('anthropic API error surfaced to %s: %s', chat_id, e)
                    return
                log.error('think async error:\n%s', traceback.format_exc())
                reply(chat_id, '_(internal error in /think)_')
                return
            try:
                reply_plain(chat_id, out)
            except Exception:
                log.error('think async reply failed:\n%s', traceback.format_exc())
    threading.Thread(target=run, daemon=True).start()


def think_turn(chat_id, user_msg):
    """Route a /think turn. Prefer CLI (subscription) unless explicitly off.

    `user_msg` is a plain string for text-only turns. For image turns the caller
    should use `think_turn_image(chat_id, image_path, caption)` instead.
    """
    if USE_CLI_FOR_AI:
        if isinstance(user_msg, list):
            # Legacy content-block form. Only happens from the photo path when
            # CLI mode is off. In CLI mode, callers use think_turn_image() which
            # resolves this to a filesystem path up-front.
            return think_turn_api(chat_id, user_msg)
        try:
            return think_turn_cli(chat_id, user_msg)
        except Exception as e:
            log.error('think_cli dispatch failed: %s. Falling back to API', e)
    return think_turn_api(chat_id, user_msg)


def think_turn_image(chat_id, image_path, caption):
    """/think with a local image file. CLI path Reads the file; API path sends
    base64 bytes in the legacy content-block form."""
    if USE_CLI_FOR_AI:
        try:
            return think_turn_cli(chat_id, caption, image_path=str(image_path))
        except Exception as e:
            log.error('think_cli image failed: %s. Falling back to API', e)
    # Legacy API path: rebuild the base64 content block from the file on disk.
    try:
        data = pathlib.Path(image_path).read_bytes()
        block = {
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': 'image/jpeg',
                'data': base64.b64encode(data).decode('ascii'),
            },
        }
    except Exception as e:
        return f'_(could not load image: {e})_'
    return think_turn_api(chat_id, [block, {'type': 'text', 'text': caption}])


# ---------- /cc (Claude Code headless, live streaming, parallel jobs) ----------

cc_sessions = {}   # chat_id -> {'session_id': str}  (most recent session used in chat
cc_config = defaultdict(lambda: {
    # Default working directory for /cc jobs. Override per-chat with
    # `/cd <path>`. Falls back to the user's home folder so a fresh
    # install doesn't fail on the example /cc command.
    'cwd':    os.environ.get('BRIDGE_DEFAULT_CWD') or str(_HOME),
    'model':  'sonnet',
    'effort': 'medium',
})

# Parallel job registry. chat_id -> {job_id: CCJob}. Job entries stay in the
# dict until cleared by /cc-stop or overwritten. They linger briefly after
# completion so cc_status can report on recently-finished jobs.
cc_jobs = defaultdict(dict)
cc_jobs_lock = threading.RLock()

MAX_CC_JOBS_PER_CHAT = 5
MAX_CC_JOBS_GLOBAL   = 15

CC_LOGS_DIR = pathlib.Path(__file__).resolve().parent / 'cc_logs'


def _cc_new_session_id():
    return str(uuid.uuid4())


def _cc_new_job_id():
    return secrets.token_hex(3)  # 6 hex chars


def _count_running_jobs():
    """Return (running_in_all_chats, dict chat_id -> running_count)."""
    total = 0
    per_chat = {}
    with cc_jobs_lock:
        for cid, jobs in cc_jobs.items():
            n = sum(1 for j in jobs.values() if j.status == 'running')
            per_chat[cid] = n
            total += n
    return total, per_chat


def _short_tool_desc(name, inp):
    if not isinstance(inp, dict):
        return ''
    if name == 'Bash':
        return (inp.get('command') or '')[:120]
    if name in ('Read', 'Write'):
        return inp.get('file_path') or inp.get('path') or ''
    if name == 'Edit':
        return (inp.get('file_path') or '') + ': ' + (inp.get('old_string') or '')[:60]
    if name == 'Glob':
        return inp.get('pattern') or ''
    if name == 'Grep':
        p = inp.get('pattern', '')
        g = inp.get('glob') or inp.get('path') or ''
        return f'{p} {g}'.strip()[:120]
    if name == 'Task':
        return (inp.get('description') or '')[:120]
    if name == 'WebFetch':
        return inp.get('url', '')[:120]
    # generic
    for k, v in inp.items():
        return f'{k}={str(v)[:100]}'
    return ''


# ---------- image/file send sentinel ----------
#
# Claude running inside a /cc job can request Telegram delivery of a file by
# printing [[SEND_IMAGE: <abs_path>]] or [[SEND_FILE: <abs_path>]] on its own
# line or inline. The bridge strips the sentinel from forwarded text, resolves
# the path, and calls sendPhoto / sendDocument against the job's chat_id.

SEND_SENTINEL_RE = re.compile(
    r'\[\[SEND_(IMAGE|FILE)\s*:\s*([^\]\n\r]+?)\s*\]\]',
    re.IGNORECASE,
)

IMG_PHOTO_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
TELEGRAM_PHOTO_MAX = 10 * 1024 * 1024
TELEGRAM_DOC_MAX   = 50 * 1024 * 1024


def _send_file_to_chat(chat_id, path, job_id, kind):
    """Deliver `path` to `chat_id`. Returns None on success, error string otherwise."""
    try:
        if not path.exists() or not path.is_file():
            return f'not found: {path}'
        size = path.stat().st_size
    except Exception as e:
        return f'stat failed: {e}'
    if size > TELEGRAM_DOC_MAX:
        return f'too large: {size / (1024*1024):.1f}MB > 50MB'
    ext = path.suffix.lower()
    # SEND_IMAGE + real image ext + under 10MB → sendPhoto. Everything else
    # (PDFs, oversized images, SEND_FILE) goes as sendDocument.
    use_photo = (
        kind == 'IMAGE' and ext in IMG_PHOTO_EXTS and size <= TELEGRAM_PHOTO_MAX
    )
    method, field = ('sendPhoto', 'photo') if use_photo else ('sendDocument', 'document')
    caption = f'[{job_id}] {path.name}'[:1024]
    try:
        with open(path, 'rb') as f:
            r = requests.post(
                f'{API}/{method}',
                data={'chat_id': chat_id, 'caption': caption},
                files={field: (path.name, f)},
                timeout=120,
            )
        resp = r.json()
        if not resp.get('ok'):
            return f'telegram {method} rejected: {resp.get("description", "?")}'
        return None
    except Exception as e:
        return f'send exception: {e}'


def _process_send_sentinels(text, chat_id, job_id, fired):
    """Scan text for SEND_IMAGE / SEND_FILE sentinels, deliver each, strip from text.

    `fired` is a per-job set of (kind, normalised_abs_path) tuples, the same
    sentinel often shows up in streamed assistant text, tool_result, AND the
    final result message, so we dedupe across all three hook points.
    """
    if not text or '[[SEND_' not in text.upper():
        return text

    for m in SEND_SENTINEL_RE.finditer(text):
        kind = m.group(1).upper()
        raw_path = m.group(2).strip().strip('"').strip("'")
        try:
            p = pathlib.Path(raw_path).resolve()
        except Exception:
            p = pathlib.Path(raw_path)
        key = (kind, str(p).lower())
        if key in fired:
            continue
        fired.add(key)
        err = _send_file_to_chat(chat_id, p, job_id, kind)
        if err:
            verb = 'image' if kind == 'IMAGE' else 'file'
            reply_plain(chat_id, f'[{job_id}] {verb} send failed: {err}')
            log.warning('cc %s sentinel %s failed for %s: %s', job_id, kind, p, err)
        else:
            log.info('cc %s sentinel %s delivered %s', job_id, kind, p)

    out_lines = []
    for line in text.splitlines(keepends=False):
        if SEND_SENTINEL_RE.fullmatch(line.strip()):
            continue
        out_lines.append(SEND_SENTINEL_RE.sub('', line))
    result = '\n'.join(out_lines)
    if text.endswith('\n') and not result.endswith('\n'):
        result += '\n'
    return result


class CCJob:
    def __init__(self, chat_id, prompt, new_session=False):
        self.chat_id = chat_id
        self.job_id = _cc_new_job_id()
        self.prompt = prompt
        self.cfg = dict(cc_config[chat_id])  # snapshot
        # Session id: fresh for this job unless we can safely reuse the chat's
        # last session (i.e. no other job is currently running in this chat).
        # Running two jobs against the same session_id corrupts state.
        with cc_jobs_lock:
            other_running = any(
                j.status == 'running'
                for j in (cc_jobs.get(chat_id) or {}).values()
            )
        last = cc_sessions.get(chat_id, {}).get('session_id')
        if new_session or other_running or not last:
            self.session_id = _cc_new_session_id()
            self.resume_existing = False
        else:
            self.session_id = last
            self.resume_existing = True
        cc_sessions[chat_id] = {'session_id': self.session_id}
        self.proc = None
        self.status_msg_id = None
        self.events = []
        self.final_text = ''
        self.start_time = now()
        self.end_time = None
        self.status = 'running'   # running | done | killed | error
        self.rc = None
        self._last_edit = 0.0
        self._lock = threading.Lock()
        self.thread = None
        self.stopped = False
        self._fired_sentinels = set()   # (kind, abs_path_lower) → already sent

    def _cmd(self):
        cmd = [CLAUDE_CLI, '-p', self.prompt,
               '--output-format', 'stream-json',
               '--verbose',
               '--model', self.cfg['model'],
               '--dangerously-skip-permissions']
        # CLI >=2.1.118 rejects --session-id for an existing UUID. Use
        # --resume when continuing a session, --session-id only for first run.
        if self.resume_existing:
            cmd += ['--resume', self.session_id]
        else:
            cmd += ['--session-id', self.session_id]
        effort = self.cfg.get('effort')
        if effort and effort != 'default':
            cmd += ['--effort', effort]
        return cmd

    def start(self):
        r = tg('sendMessage', chat_id=self.chat_id,
               text=(f'[{self.job_id}] cc starting... '
                     f'model={self.cfg["model"]} effort={self.cfg["effort"]}\n'
                     f'cwd={self.cfg["cwd"]}'),
               disable_web_page_preview='true')
        if r.get('ok'):
            self.status_msg_id = r['result']['message_id']
        # Strip API auth env vars so the CLI uses the OAuth subscription, not
        # the depleted API key loaded by instagram_analyser's .env.
        child_env = dict(os.environ)
        for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
            child_env.pop(k, None)
        try:
            self.proc = subprocess.Popen(
                self._cmd(),
                cwd=self.cfg['cwd'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                env=child_env,
                bufsize=1,
            )
        except FileNotFoundError:
            self.status = 'error'
            self._edit_status('claude CLI not found - set CLAUDE_CLI env var')
            return False
        log.info('cc job start job_id=%s session=%s pid=%s cwd=%s model=%s effort=%s',
                 self.job_id, self.session_id, self.proc.pid, self.cfg['cwd'],
                 self.cfg['model'], self.cfg['effort'])
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        return True

    def _edit_status(self, body):
        if not self.status_msg_id:
            return
        elapsed = int((now() - self.start_time).total_seconds())
        header = (f'[{self.job_id}] cc [{elapsed}s] '
                  f'{self.cfg["model"]}/{self.cfg["effort"]} '
                  f'{self.status} sess={self.session_id[:8]}\n')
        full = header + (body or '')
        if len(full) > 3800:
            full = header + '...\n' + full[-(3800 - len(header) - 5):]
        edit_message(self.chat_id, self.status_msg_id, full)

    def _maybe_edit(self, force=False):
        t = time.time()
        if not force and t - self._last_edit < 2.0:
            return
        self._last_edit = t
        with self._lock:
            recent = self.events[-25:]
        body = '\n'.join(f'[{self.job_id}] {ev}' for ev in recent)
        self._edit_status(body)

    def _add(self, s):
        with self._lock:
            self.events.append(s)
        self._maybe_edit()

    @property
    def last_output(self):
        with self._lock:
            return self.events[-1] if self.events else ''

    def _handle_event(self, ev):
        typ = ev.get('type')
        if typ == 'system' and ev.get('subtype') == 'init':
            self._add(f'> init model={ev.get("model","?")} tools={len(ev.get("tools",[]))}')
        elif typ == 'assistant':
            msg = ev.get('message', {}) or {}
            for blk in msg.get('content', []) or []:
                btyp = blk.get('type')
                if btyp == 'text':
                    txt = blk.get('text') or ''
                    txt = _process_send_sentinels(
                        txt, self.chat_id, self.job_id, self._fired_sentinels
                    ).strip()
                    if txt:
                        self._add('[msg] ' + txt[:300])
                elif btyp == 'tool_use':
                    name = blk.get('name', 'tool')
                    desc = _short_tool_desc(name, blk.get('input', {}))
                    self._add(f'[tool] {name}: {desc}')
                elif btyp == 'thinking':
                    pass
        elif typ == 'user':
            msg = ev.get('message', {}) or {}
            for blk in msg.get('content', []) or []:
                if blk.get('type') == 'tool_result':
                    content = blk.get('content')
                    if isinstance(content, list):
                        content = ''.join(
                            c.get('text', '') for c in content if isinstance(c, dict)
                        )
                    full = str(content or '')
                    full = _process_send_sentinels(
                        full, self.chat_id, self.job_id, self._fired_sentinels
                    )
                    display = full.replace('\n', ' ').strip()[:140]
                    mark = 'ERR' if blk.get('is_error') else 'ok'
                    self._add(f'   -> {mark}: {display}')
        elif typ == 'result':
            self.final_text = ev.get('result', '') or ''
            if ev.get('is_error'):
                self._add(f'! error: {self.final_text[:300]}')

    def _reader(self):
        try:
            for line in self.proc.stdout:
                if self.stopped:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    self._add('raw: ' + line[:200])
                    continue
                try:
                    self._handle_event(ev)
                except Exception:
                    log.error('cc handle_event:\n%s', traceback.format_exc())
        except Exception:
            log.error('cc reader:\n%s', traceback.format_exc())
        self.rc = self.proc.wait() if self.proc else -1
        self.end_time = now()
        elapsed = int((self.end_time - self.start_time).total_seconds())
        # Pick final status. Killed wins over done/error.
        if self.stopped:
            self.status = 'killed'
        elif self.status == 'running':
            self.status = 'error' if self.rc not in (0, None) else 'done'
        log.info('cc job end job_id=%s session=%s rc=%s status=%s elapsed=%ss',
                 self.job_id, self.session_id, self.rc, self.status, elapsed)
        self._maybe_edit(force=True)
        self._write_log()
        if self.status == 'killed':
            reply(self.chat_id, f'_[{self.job_id}] cc stopped after {elapsed}s_')
        else:
            final = _process_send_sentinels(
                self.final_text or '', self.chat_id, self.job_id, self._fired_sentinels
            ).strip()
            if final:
                reply_plain(self.chat_id, f'[{self.job_id}] {final}')
            else:
                reply(self.chat_id,
                      f'_[{self.job_id}] cc done in {elapsed}s '
                      f'(rc={self.rc}, no final text)_')

    def _write_log(self):
        try:
            CC_LOGS_DIR.mkdir(parents=True, exist_ok=True)
            ts = self.start_time.strftime('%Y%m%d_%H%M%S')
            path = CC_LOGS_DIR / f'{self.job_id}_{ts}.md'
            end_time = self.end_time or now()
            with self._lock:
                tail = list(self.events[-50:])
            body = [
                f'# cc job {self.job_id}',
                '',
                f'- **chat_id:** {self.chat_id}',
                f'- **cwd:** `{self.cfg.get("cwd")}`',
                f'- **model:** {self.cfg.get("model")}',
                f'- **effort:** {self.cfg.get("effort")}',
                f'- **session_id:** {self.session_id}',
                f'- **start_time:** {self.start_time.isoformat()}',
                f'- **end_time:** {end_time.isoformat()}',
                f'- **status:** {self.status}',
                f'- **rc:** {self.rc}',
                '',
                '## Prompt (first 500 chars)',
                '',
                '```',
                (self.prompt or '')[:500],
                '```',
                '',
                '## Final assistant message',
                '',
                self.final_text or '_(none)_',
                '',
                '## Last 50 stream events',
                '',
                '```',
            ]
            body.extend(tail)
            body.append('```')
            path.write_text('\n'.join(body), encoding='utf-8')
            log.info('cc job log written: %s', path)
        except Exception:
            log.error('cc log write failed:\n%s', traceback.format_exc())

    def stop(self):
        """Kill the job and its whole process tree."""
        if self.status != 'running' and not (self.proc and self.proc.poll() is None):
            return
        self.stopped = True
        self.status = 'killed'
        if not self.proc:
            return
        pid = self.proc.pid
        try:
            if sys.platform == 'win32':
                # Windows: kill the whole tree (claude CLI spawns node children).
                subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    capture_output=True, timeout=10,
                )
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception as e:
            log.warning('cc stop job_id=%s: %s', self.job_id, e)
            try:
                self.proc.kill()
            except Exception:
                pass


def cc_start(chat_id, prompt, new_session=False):
    """Launch a new /cc job. Returns (job, err). Exactly one is None."""
    total_running, per_chat = _count_running_jobs()
    chat_running = per_chat.get(chat_id, 0)
    if chat_running >= MAX_CC_JOBS_PER_CHAT:
        return None, (f'cap reached: {chat_running}/{MAX_CC_JOBS_PER_CHAT} '
                      f'in this chat. Use cc_stop to free a slot.')
    if total_running >= MAX_CC_JOBS_GLOBAL:
        return None, (f'cap reached: {total_running}/{MAX_CC_JOBS_GLOBAL} '
                      f'global. Use cc_stop to free a slot.')
    job = CCJob(chat_id, prompt, new_session=new_session)
    with cc_jobs_lock:
        cc_jobs[chat_id][job.job_id] = job
    if not job.start():
        # start() failure already set status=error and posted a Telegram note.
        return None, 'Failed to start claude CLI.'
    return job, None


def cc_find_job(chat_id, job_id):
    with cc_jobs_lock:
        return (cc_jobs.get(chat_id) or {}).get(job_id)


def cc_running_jobs(chat_id):
    with cc_jobs_lock:
        return [j for j in (cc_jobs.get(chat_id) or {}).values()
                if j.status == 'running']


def cc_all_jobs(chat_id):
    with cc_jobs_lock:
        return list((cc_jobs.get(chat_id) or {}).values())


def cc_stop_job(chat_id, job_id):
    """Kill one job by id. Returns (killed_bool, error_or_None)."""
    j = cc_find_job(chat_id, job_id)
    if not j:
        return False, f'no such job: {job_id}'
    if j.status != 'running':
        return False, f'job {job_id} already {j.status}'
    j.stop()
    return True, None


def cc_stop_all(chat_id):
    """Kill every running job in chat. Returns list of killed job_ids."""
    killed = []
    for j in cc_running_jobs(chat_id):
        j.stop()
        killed.append(j.job_id)
    return killed


def cc_status_text(chat_id, job_id=None):
    """String rendering used by the cc_status tool and by /status."""
    if job_id:
        j = cc_find_job(chat_id, job_id)
        if not j:
            return f'no such job: {job_id}'
        elapsed = int((now() - j.start_time).total_seconds())
        with j._lock:
            recent = list(j.events[-10:])
        tail = '\n'.join(recent) if recent else '(no events yet)'
        return (f'[{j.job_id}] {j.status} {elapsed}s '
                f'cwd={j.cfg.get("cwd")} model={j.cfg.get("model")} '
                f'sess={j.session_id[:8]}\n'
                f'prompt: {(j.prompt or "")[:200]}\n'
                f'last events:\n{tail}')
    jobs = cc_running_jobs(chat_id)
    if not jobs:
        return 'no running cc jobs in this chat'
    lines = []
    for j in jobs:
        elapsed = int(((j.end_time or now()) - j.start_time).total_seconds())
        last = (j.last_output or '(no events)').replace('\n', ' ')[:100]
        lines.append(
            f'[{j.job_id}] {elapsed}s cwd={j.cfg.get("cwd")} last={last}'
        )
    return '\n'.join(lines)


def cc_stop_tool(chat_id, job_id=None):
    """String rendering used by the cc_stop tool."""
    if job_id:
        ok, err = cc_stop_job(chat_id, job_id)
        if not ok:
            return err
        return f'killed: [{job_id}]'
    killed = cc_stop_all(chat_id)
    if not killed:
        return 'no running jobs to kill'
    return 'killed: [' + ', '.join(killed) + ']'


def cc_reset(chat_id):
    cc_sessions.pop(chat_id, None)


# ---------- voice transcription ----------

def download_telegram_file(file_id):
    r = tg('getFile', file_id=file_id)
    if not r.get('ok'):
        return None
    file_path = r['result']['file_path']
    url = f'https://api.telegram.org/file/bot{TOKEN}/{file_path}'
    data = requests.get(url, timeout=60).content
    tmp = pathlib.Path(tempfile.gettempdir()) / f'tgvoice_{secrets.token_hex(4)}.ogg'
    tmp.write_bytes(data)
    return tmp


def transcribe(audio_path):
    oc = openai_client()
    if not oc:
        return None
    with open(audio_path, 'rb') as f:
        r = oc.audio.transcriptions.create(model='whisper-1', file=f)
    return r.text


def download_image_to_tmp(file_id, media_type='image/jpeg'):
    """Fetch a Telegram photo, save to tmp, return pathlib.Path.

    Used by the CLI /think path. Claude's Read tool opens the file directly.
    """
    r = tg('getFile', file_id=file_id)
    if not r.get('ok'):
        return None
    file_path = r['result']['file_path']
    url = f'https://api.telegram.org/file/bot{TOKEN}/{file_path}'
    data = requests.get(url, timeout=60).content
    if not data:
        return None
    ext = '.jpg'
    if 'png' in media_type:
        ext = '.png'
    elif 'webp' in media_type:
        ext = '.webp'
    elif 'gif' in media_type:
        ext = '.gif'
    tmp = pathlib.Path(tempfile.gettempdir()) / f'tgphoto_{secrets.token_hex(4)}{ext}'
    tmp.write_bytes(data)
    return tmp


def download_image_block(file_id, media_type='image/jpeg'):
    """Fetch a Telegram photo and return an Anthropic image content block."""
    r = tg('getFile', file_id=file_id)
    if not r.get('ok'):
        return None
    file_path = r['result']['file_path']
    url = f'https://api.telegram.org/file/bot{TOKEN}/{file_path}'
    data = requests.get(url, timeout=60).content
    if not data:
        return None
    return {
        'type': 'image',
        'source': {
            'type': 'base64',
            'media_type': media_type,
            'data': base64.b64encode(data).decode('ascii'),
        },
    }


# ---------- command dispatcher ----------

def handle_command(cmd, arg, chat_id, raw_text):
    cmd = cmd.lower()

    if cmd in ('/help', '/start'):
        reply(chat_id, HELP_TEXT)
        return
    if cmd == '/status':
        cfg = cc_config[chat_id]
        running = cc_running_jobs(chat_id)
        all_jobs = cc_all_jobs(chat_id)
        if running:
            job_lines = [f'CC jobs: {len(running)} running / {len(all_jobs)} total']
            for j in all_jobs:
                elapsed = int(((j.end_time or now()) - j.start_time).total_seconds())
                pid = j.proc.pid if j.proc else '?'
                job_lines.append(
                    f'  [{j.job_id}] {j.status} pid={pid} elapsed={elapsed}s '
                    f'sess={j.session_id[:8]}'
                )
            job_block = '\n'.join(job_lines)
        elif all_jobs:
            job_lines = [f'CC jobs: idle ({len(all_jobs)} completed in registry)']
            for j in all_jobs[-5:]:
                elapsed = int(((j.end_time or now()) - j.start_time).total_seconds())
                job_lines.append(
                    f'  [{j.job_id}] {j.status} elapsed={elapsed}s '
                    f'sess={j.session_id[:8]}'
                )
            job_block = '\n'.join(job_lines)
        else:
            job_block = (f'CC jobs: idle  '
                         f'last_sess={cc_sessions.get(chat_id,{}).get("session_id","none")[:8]}')
        total_running, _ = _count_running_jobs()
        lines = [
            f'Unlock: {"YES ("+ (unlock_expiry[chat_id]-now()).__str__().split(".")[0] +" left)" if is_unlocked(chat_id) else "no"}',
            f'Think history: {len(think_sessions.get(chat_id,{}).get("history",[]))} msgs'
            + (f", summary={len(think_sessions[chat_id]['summary'])} chars" if think_sessions.get(chat_id,{}).get('summary') else ''),
            ('Think CLI session: ' + (
                f'{think_cli_sessions[chat_id]["session_id"][:8]} '
                f'model={think_cli_sessions[chat_id].get("model","sonnet")}'
                if think_cli_sessions.get(chat_id) else 'none'
            )),
            job_block,
            f'CC caps: {len(running)}/{MAX_CC_JOBS_PER_CHAT} chat, {total_running}/{MAX_CC_JOBS_GLOBAL} global',
            f'CC cfg: cwd={cfg["cwd"]}  model={cfg["model"]}  effort={cfg["effort"]}',
            f'AI backend: {"CLI (subscription)" if USE_CLI_FOR_AI else "API"}'
            + f'  | Anthropic={"on" if claude_client() else "OFF"}'
            + f'  OpenAI={"on" if openai_client() else "OFF"}',
        ]
        reply(chat_id, '```\n' + '\n'.join(lines) + '\n```')
        return
    if cmd == '/cost':
        days = 7
        try:
            if arg.strip():
                days = int(arg.strip())
        except ValueError:
            pass
        reply(chat_id, _cost_report(days))
        return
    if cmd == '/list':
        ensure_dirs()
        reply(chat_id, '```\n' + (INDEX_FILE.read_text(encoding='utf-8').strip() or '(empty)')[:3800] + '\n```')
        return
    if cmd == '/ideas':
        reply(chat_id, _ideas_report(arg))
        return
    if cmd == '/unlock':
        if is_locked_out(chat_id):
            reply(chat_id, 'Too many failed attempts. Try again later.')
            return
        if check_password(arg.strip()):
            record_success(chat_id)
            reply(chat_id, f'Unlocked for {UNLOCK_MINUTES} minutes.')
        else:
            record_fail(chat_id)
            left = MAX_FAILED - failed_attempts[chat_id][0]
            reply(chat_id, f'Wrong password. {max(left,0)} attempts left.')
        return
    if cmd == '/lock':
        lock(chat_id)
        reply(chat_id, 'Locked.')
        return
    if cmd == '/ask':
        if not arg.strip():
            reply(chat_id, 'Usage: `/ask what have I said about trading?`')
            return
        reply(chat_id, ask_memory(arg) or 'AI not configured.')
        return

    # Sensitive commands below require unlock.
    if cmd in SENSITIVE_CMDS and not is_unlocked(chat_id):
        reply(chat_id, 'Locked. Send `/unlock <password>` first.')
        return

    if cmd == '/think':
        if not arg.strip():
            reply(chat_id, 'Usage: `/think What do you make of X?`')
            return
        think_async(chat_id, lambda: think_turn(chat_id, arg))
        return
    if cmd == '/reset':
        reset_think(chat_id)
        reply(chat_id, 'Think conversation cleared.')
        return
    if cmd == '/cc':
        if not arg.strip():
            reply(chat_id, 'Usage: `/cc <prompt>`  e.g. `/cc summarise the changes in this repo since yesterday`')
            return
        job, err = cc_start(chat_id, arg, new_session=False)
        if not job:
            reply(chat_id, err or 'Could not start /cc.')
        return
    if cmd == '/cc-new':
        cc_reset(chat_id)
        reply(chat_id, 'Next /cc starts a fresh session. (Running jobs keep their own sessions.)')
        return
    if cmd == '/cc-stop':
        target = arg.strip()
        if target:
            ok, err = cc_stop_job(chat_id, target)
            reply(chat_id, f'Stopping `{target}`...' if ok else (err or 'Could not stop.'))
        else:
            killed = cc_stop_all(chat_id)
            if killed:
                reply(chat_id, 'Stopping: ' + ', '.join(f'`{k}`' for k in killed))
            else:
                reply(chat_id, 'No /cc job is running.')
        return
    if cmd == '/cd':
        path = arg.strip().strip('"').strip("'")
        if not path:
            reply(chat_id, f'cwd = `{cc_config[chat_id]["cwd"]}`\nUsage: `/cd <absolute-path>`  (e.g. `/cd ~/my-project`)')
            return
        p = pathlib.Path(path)
        if not p.exists() or not p.is_dir():
            reply(chat_id, f'Not a directory: `{path}`')
            return
        cc_config[chat_id]['cwd'] = str(p)
        reply(chat_id, f'cwd = `{p}`')
        return
    if cmd == '/cc-model':
        m = arg.strip()
        if not m:
            reply(chat_id, f'model = `{cc_config[chat_id]["model"]}`\nUsage: `/cc-model sonnet|opus|haiku|<full-name>`')
            return
        cc_config[chat_id]['model'] = m
        reply(chat_id, f'model = `{m}`')
        return
    if cmd == '/cc-effort':
        e = arg.strip().lower()
        valid = {'low', 'medium', 'high', 'xhigh', 'max', 'default'}
        if e not in valid:
            reply(chat_id, f'effort = `{cc_config[chat_id]["effort"]}`\nOptions: {", ".join(sorted(valid))}')
            return
        cc_config[chat_id]['effort'] = e
        reply(chat_id, f'effort = `{e}`')
        return
    if cmd == '/forget':
        slug = arg.strip()
        if not slug:
            reply(chat_id, 'Usage: `/forget <slug>`')
            return
        if not slug.endswith('.md'):
            slug += '.md'
        path = MEMORY_DIR / slug
        if not path.exists():
            matches = list(MEMORY_DIR.glob(f'*{slug[:-3]}*.md'))
            if len(matches) == 1:
                path = matches[0]
            elif len(matches) > 1:
                reply(chat_id, 'Multiple matches:\n' + '\n'.join(m.name for m in matches))
                return
            else:
                reply(chat_id, f'No memory matches `{slug}`')
                return
        path.unlink()
        remove_from_index(path.name)
        reply(chat_id, f'Forgot `{path.name}`.')
        return
    if cmd == '/remember':
        if not arg.strip():
            reply(chat_id, 'Usage: `/remember feedback short title: body`')
            return
        mt, title, body = parse_remember(arg)
        fname = write_memory(mt, title, body)
        reply(chat_id, f'Saved *{mt}* → `{fname}`')
        return
    if cmd[1:] in VALID_TYPES:
        if not arg.strip():
            reply(chat_id, f'Usage: `{cmd} title: body`')
            return
        mt, title, body = parse_remember(arg, default_type=cmd[1:])
        fname = write_memory(mt, title, body)
        reply(chat_id, f'Saved *{mt}* → `{fname}`')
        return

    reply(chat_id, 'Unknown command. /help for list.')


def handle_text(text, chat_id):
    """Plain (non-command) text → auto-classify if unlocked, else /ask shortcut."""
    if not is_unlocked(chat_id):
        reply(chat_id, 'Locked. Plain text would be filed as a memory. Send `/unlock <password>` first, '
                       'or use `/ask <question>` for a read-only query.')
        return
    classified = None
    try:
        classified = classify_thought(text)
    except Exception as e:
        log.error('classify error:\n%s', traceback.format_exc())
        if anthropic is not None and isinstance(e, anthropic.APIError):
            reply_plain(chat_id, _format_api_error(e))
    if classified:
        mt, title, body, hook = classified
        fname = write_memory(mt, title, body, hook)
        reply(chat_id, f'Filed as *{mt}*: *{title}*\n`{fname}`')
    else:
        mt, title, body = parse_remember(text, default_type='project')
        fname = write_memory(mt, title, body)
        reply(chat_id, f'Saved (default: *project*) → `{fname}`')


# ---------- update handler ----------

def handle_update(u):
    msg = u.get('message') or u.get('edited_message')
    if not msg:
        return
    user = msg.get('from', {})
    chat_id = msg.get('chat', {}).get('id')
    if AUTHORIZED_USER_ID and user.get('id') != AUTHORIZED_USER_ID:
        log.warning('unauthorized user %s (%s)', user.get('id'), user.get('username'))
        reply(chat_id, 'This bot is private.')
        return

    # Photo → route to /think with the image attached (caption becomes prompt)
    photos = msg.get('photo') or []
    document = msg.get('document') or {}
    doc_is_image = isinstance(document, dict) and (document.get('mime_type') or '').startswith('image/')
    if photos or doc_is_image:
        if not is_unlocked(chat_id):
            reply(chat_id, 'Locked. Images route to `/think`. Send `/unlock <password>` first.')
            return
        if photos:
            # photos is an ordered list of sizes; last is largest.
            file_id = photos[-1]['file_id']
            media_type = 'image/jpeg'
        else:
            file_id = document['file_id']
            media_type = document.get('mime_type') or 'image/jpeg'
        img_path = download_image_to_tmp(file_id, media_type=media_type)
        if not img_path:
            reply(chat_id, '(could not download image)')
            return
        caption = (msg.get('caption') or '').strip() or 'What do you see? Describe it briefly.'
        log.info('<- photo %s: %r', chat_id, caption[:120])
        think_async(chat_id, lambda: think_turn_image(chat_id, img_path, caption))
        return

    # Voice message → transcribe → route as text
    voice = msg.get('voice') or msg.get('audio')
    if voice:
        file_id = voice.get('file_id')
        tmp = download_telegram_file(file_id)
        text = transcribe(tmp) if tmp else None
        if tmp and tmp.exists():
            try: tmp.unlink()
            except Exception: pass
        if not text:
            reply(chat_id, '(could not transcribe (OpenAI key missing?)')
            return
        log.info('<- voice %s: %r', chat_id, text[:120])
        reply(chat_id, f'_heard:_ {text}')
        # fall through as if the user typed it
        try:
            if text.startswith('/'):
                cmd, _, arg = text.partition(' ')
                cmd = cmd.split('@')[0]
                handle_command(cmd, arg, chat_id, text)
            else:
                handle_text(text, chat_id)
        except Exception as e:
            if anthropic is not None and isinstance(e, anthropic.APIError):
                reply_plain(chat_id, _format_api_error(e))
                log.warning('anthropic API error surfaced to %s: %s', chat_id, e)
                return
            raise
        return

    text = (msg.get('text') or '').strip()
    if not text:
        return
    log.info('<- %s %s: %r', user.get('username'), chat_id, text[:120])

    if instagram_handler is not None:
        try:
            if instagram_handler.try_cancel(text, chat_id, reply):
                return
            if instagram_handler.try_handle(text, chat_id, reply, reply_plain):
                return
        except Exception:
            log.error('instagram_handler error:\n%s', traceback.format_exc())

    try:
        if text.startswith('/'):
            cmd, _, arg = text.partition(' ')
            cmd = cmd.split('@')[0]
            handle_command(cmd, arg, chat_id, text)
        else:
            handle_text(text, chat_id)
    except Exception as e:
        if anthropic is not None and isinstance(e, anthropic.APIError):
            reply_plain(chat_id, _format_api_error(e))
            log.warning('anthropic API error surfaced to %s: %s', chat_id, e)
            return
        raise


# ---------- main loop ----------

def main():
    if not TOKEN:
        log.error('TELEGRAM_BOT_TOKEN not set'); sys.exit(1)
    if not AUTHORIZED_USER_ID:
        log.warning('TELEGRAM_USER_ID not set; bot will accept from anyone')
    ensure_dirs()
    log.info('starting; memory=%s inbox=%s', MEMORY_DIR, INBOX_DIR)
    _load_think_cli_sessions()
    me = tg('getMe')
    if not me.get('ok'):
        log.error('getMe failed: %s', me); sys.exit(1)
    log.info('hello from @%s', me['result'].get('username'))
    log.info('AI: anthropic=%s openai=%s  claude_cli=%s  password=%s',
             'on' if claude_client() else 'OFF',
             'on' if openai_client() else 'OFF',
             CLAUDE_CLI,
             'set' if PASSHASH_FILE.exists() else 'NOT SET')

    offset = 0
    backoff = 1
    while True:
        try:
            r = requests.get(f'{API}/getUpdates',
                             params={'offset': offset, 'timeout': 25,
                                     'allowed_updates': json.dumps(['message','edited_message'])},
                             timeout=35)
            data = r.json()
            if not data.get('ok'):
                log.error('getUpdates: %s', data)
                time.sleep(backoff); backoff = min(backoff*2, 60); continue
            backoff = 1
            for u in data.get('result', []):
                offset = max(offset, u['update_id'] + 1)
                try:
                    handle_update(u)
                except Exception:
                    log.error('handler error:\n%s', traceback.format_exc())
        except requests.exceptions.RequestException as e:
            log.warning('network: %s', e); time.sleep(backoff); backoff = min(backoff*2, 60)
        except KeyboardInterrupt:
            log.info('bye'); return


if __name__ == '__main__':
    main()
