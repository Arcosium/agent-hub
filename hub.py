#!/usr/bin/env python3
"""agent-hub: Claude Code, Codex, 로컬 모델(Hermes + arc-local)이 일을 나눠 맡게 하는 CLI.

  hub status                         세 실행자의 쿼터와 상태
  hub run [--kind K] [--to T] "작업"  잘하는 쪽에 맡기고, 한도가 막히면 다음 순서로 넘긴다
  hub sync [--apply]                 manifest.json 의 스킬·MCP·플러그인과 공통 지침을 세 도구에 맞춘다

표준 라이브러리만 쓴다. 실행 기록과 상태는 ~/vault/agent-hub 에 둔다.
"""
import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('AGENT_HUB_STATE', HOME / 'vault/agent-hub'))
MANIFEST = Path(os.environ.get('AGENT_HUB_MANIFEST', ROOT / 'manifest.json'))
LOCAL_URL = os.environ.get('AGENT_HUB_LOCAL_URL', 'http://127.0.0.1:11434/v1')
LOCAL_MODEL = 'arc-local'
TOOLS = ('claude', 'codex', 'hermes')
QUOTA_TTL = 60

# 잘하는 순서. 앞쪽이 한도에 막히면 다음으로 넘어간다. 클라우드는 각각 한 번씩만 있어서
# 한 작업의 클라우드 호출은 두 번을 넘지 않는다(작업공간 규칙).
ORDER = {
    'design': ('claude', 'codex', 'local'),
    'review': ('claude', 'codex', 'local'),
    'docs': ('claude', 'codex', 'local'),
    'debug': ('claude', 'codex', 'local'),
    'implement': ('codex', 'claude', 'local'),
    'test': ('codex', 'claude', 'local'),
    'fix': ('codex', 'claude', 'local'),
    'summarize': ('local', 'codex', 'claude'),
    'explore': ('local', 'codex', 'claude'),
}
KEYWORDS = (  # 위에서부터 처음 맞는 것
    ('review', r'리뷰|검토|review'),
    ('design', r'설계|아키텍처|리팩터|구조\s*(를|변경|개편)|architect|refactor|design'),
    ('docs', r'문서|윤문|보고서|README|번역|맞춤법|\bdocs?\b'),
    ('debug', r'원인|디버그|debug|왜 .*(안 ?되|실패|죽)'),
    ('test', r'테스트|\btests?\b'),
    ('fix', r'고쳐|버그|\bfix\b|\bbug\b'),
    ('summarize', r'요약|summar'),
    ('explore', r'찾아|어디(에|서)|목록|explore|\bfind\b|search'),
    ('implement', r'구현|만들|추가|작성|implement|\badd\b|create|write'),
)
LIMIT_RE = re.compile(r'usage limit|rate[ _-]?limit|limit reached|hit your .*limit|quota|\b429\b|한도', re.I)
SKILL_DIRS = {
    'claude': (HOME / '.claude/skills',),
    'codex': (HOME / '.codex/skills', HOME / '.agents/skills'),
    'hermes': (HOME / '.hermes/skills',),
}
LINK_DIR = {'claude': HOME / '.claude/skills', 'codex': HOME / '.agents/skills', 'hermes': HOME / '.hermes/skills'}


def expand(path):
    return Path(os.path.expanduser(path))


def manifest():
    return json.loads(MANIFEST.read_text())


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


# ---------------------------------------------------------------- 쿼터

def claude_quota():
    token = read_json(HOME / '.claude/.credentials.json', {})['claudeAiOauth']['accessToken']
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'claude-code/2.1'})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.load(response)
    limits = [x for x in data.get('limits', []) if x.get('kind') in ('session', 'weekly_all')]
    full = [x for x in limits if (x.get('percent') or 0) >= 100]
    return {'ok': not full, 'used': {x['kind']: x.get('percent') for x in limits},
            'resets_at': min(x['resets_at'] for x in full) if full else None}


def codex_quota(use_credits):
    process = subprocess.Popen(['codex', 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    try:
        for message in ({'method': 'initialize', 'id': 1, 'params': {'clientInfo': {'name': 'agent-hub', 'version': '1'}}},
                        {'method': 'initialized'}, {'method': 'account/rateLimits/read', 'id': 2}):
            process.stdin.write(json.dumps(message) + '\n')
        process.stdin.flush()
        reply, deadline = {}, time.time() + 20
        while reply.get('id') != 2:
            if time.time() > deadline:
                raise TimeoutError('codex app-server did not answer')
            if select.select([process.stdout], [], [], 1)[0]:
                line = process.stdout.readline()
                if not line:
                    raise ConnectionError('codex app-server closed')
                reply = json.loads(line)
    finally:
        process.kill()
    result = reply.get('result') or {}
    limits = result.get('rateLimits') or {}
    credits = limits.get('credits') or {}
    ordinary = result.get('ordinaryUsageAllowed', limits.get('rateLimitReachedType') is None)
    windows = {k: limits[k] for k in ('primary', 'secondary') if limits.get(k)}
    return {'ok': bool(ordinary or (use_credits and credits.get('hasCredits'))),
            'used': {k: v.get('usedPercent') for k, v in windows.items()},
            'resets_at': None if ordinary else iso(max((v.get('resetsAt') or 0) for v in windows.values()) if windows else 0),
            'credits': credits.get('balance') if credits.get('hasCredits') else None,
            'using_credits': bool(not ordinary and use_credits and credits.get('hasCredits'))}


def local_quota():
    with urllib.request.urlopen(LOCAL_URL + '/models', timeout=5) as response:
        names = [m.get('id') for m in json.load(response).get('data', [])]
    return {'ok': LOCAL_MODEL in names}


def status(fresh=False):
    """{'claude'|'codex'|'local': {'ok': True/False/None, ...}}. None 은 확인 실패(시도는 한다)."""
    cache = read_json(STATE / 'quota.json', {})
    blocked = read_json(STATE / 'blocked.json', {})
    skip = {x.strip() for x in os.environ.get('HUB_SKIP', '').split(',') if x.strip()}
    use_credits = os.environ.get('HUB_CODEX_CREDITS') == '1' or manifest().get('codex_use_credits', False)
    checks = {'claude': claude_quota, 'codex': lambda: codex_quota(use_credits), 'local': local_quota}
    result = {}
    for name, check in checks.items():
        entry = cache.get(name)
        if fresh or not entry or time.time() - entry.get('checked_at', 0) > QUOTA_TTL or entry.get('credits_mode') != use_credits:
            try:
                entry = check()
            except Exception as exc:  # 조회가 막혀도 실행은 막지 않는다. 한도 오류는 실행 결과로 다시 잡힌다.
                entry = {'ok': None, 'error': f'{type(exc).__name__}: {exc}'[:200]}
            entry.update(checked_at=time.time(), credits_mode=use_credits)
            cache[name] = entry
        entry = dict(entry)
        until = blocked.get(name, 0)
        if until > time.time():
            entry.update(ok=False, blocked_until=iso(until))
        if name in skip:
            entry.update(ok=False, skipped='HUB_SKIP')
        result[name] = entry
    write_json(STATE / 'quota.json', cache)
    return result


def block(name, resets_at):
    """실행 중 한도 오류를 받으면 초기화 시각(모르면 1시간)까지 그 실행자를 건너뛴다."""
    until = time.time() + 3600
    if resets_at:
        try:
            until = datetime.fromisoformat(resets_at).timestamp()
        except ValueError:
            pass
    blocked = read_json(STATE / 'blocked.json', {})
    blocked[name] = until
    write_json(STATE / 'blocked.json', blocked)


# ---------------------------------------------------------------- 실행

def ask_local(prompt, max_tokens=16, timeout=60):
    body = {'model': LOCAL_MODEL, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': max_tokens,
            'temperature': 0, 'chat_template_kwargs': {'enable_thinking': False}}
    request = urllib.request.Request(LOCAL_URL + '/chat/completions', data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return (json.load(response)['choices'][0]['message'].get('content') or '').strip()


def classify(task):
    for kind, pattern in KEYWORDS:
        if re.search(pattern, task, re.I):
            return kind
    try:
        word = ask_local('Classify this task into exactly one word from [' + ', '.join(ORDER) +
                         '].\nTask: ' + task[:2000] + '\nAnswer with the word only.').lower()
        return next((kind for kind in ORDER if kind in word), 'implement')
    except Exception:
        return 'implement'


def child_env():
    env = {k: v for k, v in os.environ.items() if k not in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY')}
    env['HUB_DEPTH'] = '1'  # 하위 실행자 안에서 hub run 을 다시 부르지 못하게 한다
    return env


def is_git(cwd):
    return subprocess.run(['git', '-C', str(cwd), 'rev-parse'], capture_output=True).returncode == 0


def call(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=child_env(), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or '', f'timeout after {timeout}s'


def run_claude(task, kind, cwd, timeout):
    cmd = ['claude', '-p', task, '--output-format', 'json', '--permission-mode', 'auto']
    if kind in ('implement', 'test', 'fix'):
        cmd += ['--model', 'sonnet']  # 구현 위임은 기존 route-implementer 와 같은 Sonnet
    code, out, err = call(cmd, cwd, timeout)
    data = read_json_text(out)
    text = data.get('result') or err.strip()[-2000:]
    failed = code != 0 or data.get('is_error') or not data
    return {'ok': not failed, 'limited': bool(failed and LIMIT_RE.search(text or '')),
            'answer': text, 'usage': data.get('usage'), 'raw': out[-20000:] or err[-5000:]}


def run_codex(task, kind, cwd, timeout):
    (STATE / 'tmp').mkdir(parents=True, exist_ok=True)
    last = STATE / 'tmp' / f'codex-{os.getpid()}-{time.time_ns()}.txt'
    cmd = ['codex', 'exec', '--json', '-o', str(last), '-C', str(cwd)]
    if not is_git(cwd):
        cmd.append('--skip-git-repo-check')
    code, out, err = call(cmd + [task], cwd, timeout)
    events = [read_json_text(line) for line in out.splitlines() if line.startswith('{')]
    errors = [json.dumps(e, ensure_ascii=False) for e in events if e.get('type') in ('error', 'turn.failed')]
    usage = next((e.get('usage') for e in reversed(events) if e.get('type') == 'turn.completed'), None)
    answer = last.read_text().strip() if last.exists() else ''
    last.unlink(missing_ok=True)
    failed = code != 0 or bool(errors) or usage is None
    detail = ' '.join(errors) + err[-2000:]
    return {'ok': not failed, 'limited': bool(failed and LIMIT_RE.search(detail)),
            'answer': answer or detail.strip()[-2000:], 'usage': usage, 'raw': out[-20000:] or err[-5000:]}


def run_local(task, kind, cwd, timeout):
    code, out, err = call(['hermes', '-z', task], cwd, timeout)
    answer = out.strip()
    failed = code != 0 or not answer or answer.startswith('Error:')
    return {'ok': not failed, 'limited': False, 'answer': answer or err.strip()[-2000:], 'usage': None,
            'raw': out[-20000:] or err[-5000:]}


EXECUTORS = {'claude': run_claude, 'codex': run_codex, 'local': run_local}


def read_json_text(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def git_status(cwd):
    """작업 폴더 안의 git 상태 항목 집합. git 밖이면 None."""
    if not is_git(cwd):
        return None
    out = subprocess.run(['git', '-C', str(cwd), 'status', '--porcelain', '-z', '--', '.'],
                         capture_output=True, text=True).stdout
    return {entry for entry in out.split('\0') if entry}


def run(task, kind=None, to=None, cwd=None, timeout=1800):
    if os.environ.get('HUB_DEPTH'):
        raise SystemExit('hub run 으로 실행된 작업 안에서는 다시 위임하지 않는다(HUB_DEPTH).')
    cwd = Path(cwd or os.getcwd()).resolve()
    kind = kind or classify(task)
    order = (to,) if to else ORDER[kind]
    quota = status()
    tried, result, who, started = [], None, None, time.time()
    before = git_status(cwd)
    for name in order:
        if quota[name].get('ok') is False:
            tried.append({'who': name, 'skipped': 'quota'})
            continue
        result = EXECUTORS[name](task, kind, cwd, timeout)
        tried.append({'who': name, 'ok': result['ok'], 'limited': result['limited']})
        who = name
        if result['limited']:
            block(name, quota[name].get('resets_at'))
            continue
        if not result['ok'] and name == 'local' and not to:
            continue  # 로컬이 못 하면 클라우드로 올린다
        break  # 성공했거나 한도가 아닌 실패는 되풀이하지 않고 보고한다
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + f'{os.getpid()}'
    ok = bool(result and result['ok'])
    report = {
        'id': run_id, 'kind': kind, 'who': who if result else None, 'ok': ok,
        'needs_review': ok and who == 'local' and order[0] != 'local',
        'tried': tried, 'changed': None if before is None else sorted(e[3:] for e in git_status(cwd) - before),
        'answer': (result or {}).get('answer', '실행할 수 있는 쪽이 없다(모두 한도 또는 건너뜀).')[:4000],
        'log': str(STATE / 'runs' / f'{run_id}.json'),
    }
    write_json(STATE / 'runs' / f'{run_id}.json', {**report, 'task': task, 'cwd': str(cwd),
                                                  'usage': (result or {}).get('usage'), 'raw': (result or {}).get('raw')})
    with (STATE / 'runs.jsonl').open('a') as index:  # 프롬프트 원문은 색인에 넣지 않는다
        index.write(json.dumps({k: report[k] for k in ('id', 'kind', 'who', 'ok', 'needs_review', 'tried')} |
                               {'seconds': round(time.time() - started), 'usage': (result or {}).get('usage')},
                               ensure_ascii=False) + '\n')
    return report


# ---------------------------------------------------------------- 동기화

def skill_plan(spec):
    """(links, problems). links = {hub 가 관리하는 링크: 대상}. 이미 그 도구에 보이는 원본은 건드리지 않는다."""
    links, problems = {}, []
    for name, entry in spec.get('skills', {}).items():
        for tool in TOOLS:
            path = entry.get(tool) or entry.get('all')
            if not path:
                continue
            source = expand(path)
            if not (source / 'SKILL.md').exists():
                problems.append(f'{tool}:{name} 원본 없음 {source}')
                continue
            link = LINK_DIR[tool] / source.name
            if link.is_symlink() and os.readlink(link) == str(source):
                links[str(link)] = str(source)
                continue
            same = [d / source.name for d in SKILL_DIRS[tool] if (d / source.name).exists() or (d / source.name).is_symlink()]
            if any(p.resolve() == source.resolve() for p in same):
                continue
            if same:  # 같은 이름의 다른 사본이 있으면 중복으로 보이므로 링크를 만들지 않는다
                problems.append(f'{tool}:{name} 같은 이름의 다른 사본 {same[0]}')
                continue
            links[str(link)] = str(source)
    return links, problems


def unmanaged_skills(spec):
    known = {expand(p).resolve() for e in spec.get('skills', {}).values() for k, p in e.items() if k != 'note'}
    found = []
    for tool, dirs in SKILL_DIRS.items():
        for d in dirs:
            for child in sorted(d.glob('*')) if d.exists() else ():
                if (child / 'SKILL.md').exists() and child.resolve() not in known:
                    found.append(f'{tool}:{child}')
    return found


def mcp_definition(entry):
    if entry.get('url'):
        return {'url': entry['url']}
    return {'command': os.path.expanduser(entry['command']), 'args': [os.path.expanduser(a) for a in entry.get('args', [])]}


def current_mcp():
    claude = read_json(HOME / '.claude.json', {}).get('mcpServers', {})
    try:
        codex = tomllib.loads((HOME / '.codex/config.toml').read_text()).get('mcp_servers', {})
    except OSError:
        codex = {}
    hermes = {}
    try:
        import yaml  # Hermes 설정 읽기에만 쓴다. 쓰기는 hermes CLI 가 한다.
        hermes = (yaml.safe_load((HOME / '.hermes/config.yaml').read_text()) or {}).get('mcp_servers') or {}
    except (ImportError, OSError):
        pass
    return {'claude': claude, 'codex': codex, 'hermes': hermes}


def comparable(entry):
    if entry.get('url'):
        return {'url': entry['url']}
    return {'command': entry.get('command'), 'args': list(entry.get('args') or [])}


def mcp_commands(tool, name, definition, replace):
    url = definition.get('url')
    if tool == 'claude':
        add = ['claude', 'mcp', 'add-json', '-s', 'user', name,
               json.dumps({'type': 'http', 'url': url} if url else {'type': 'stdio', **definition})]
        remove = ['claude', 'mcp', 'remove', '-s', 'user', name]
    elif tool == 'codex':
        add = ['codex', 'mcp', 'add', name] + (['--url', url] if url else ['--', definition['command'], *definition['args']])
        remove = ['codex', 'mcp', 'remove', name]
    else:
        add = ['hermes', 'mcp', 'add', name] + (['--url', url] if url else ['--command', definition['command'], '--args', *definition['args']])
        remove = ['hermes', 'mcp', 'remove', name]
    return ([remove] if replace else []) + [add]


def mcp_plan(spec, current, managed):
    commands, problems = [], []
    for name, entry in spec.get('mcp', {}).items():
        definition = mcp_definition(entry)
        for tool in entry.get('tools', TOOLS):
            have = current[tool].get(name)
            if have is not None and comparable(have) == definition:
                continue
            if have is not None and any(have.get(k) for k in ('env', 'headers', 'http_headers', 'env_http_headers')):
                problems.append(f'{tool}:{name} 기존 정의에 env·헤더가 있어 덮어쓰지 않음')
                continue
            commands += mcp_commands(tool, name, definition, replace=have is not None)
    wanted = {(t, n) for n, e in spec.get('mcp', {}).items() for t in e.get('tools', TOOLS)}
    for tool, name in sorted({tuple(x) for x in managed} - wanted):  # hub 가 넣었다가 매니페스트에서 빠진 것만 지운다
        if name in current[tool]:
            commands.append(mcp_commands(tool, name, {'url': 'x'}, replace=True)[0])
    return commands, problems, sorted(wanted)


def plugin_plan(spec):
    installed = read_json(HOME / '.claude/plugins/installed_plugins.json', {}).get('plugins', {})
    codex_list = subprocess.run(['codex', 'plugin', 'list'], capture_output=True, text=True).stdout
    codex_installed = {line.split()[0] for line in codex_list.splitlines()
                       if line.strip() and ' installed' in line and 'not installed' not in line}
    markets = subprocess.run(['codex', 'plugin', 'marketplace', 'list'], capture_output=True, text=True).stdout
    commands = []
    for entry in spec.get('plugins', {}).values():
        if entry.get('claude') and entry['claude'] not in installed:
            commands.append(['claude', 'plugin', 'install', entry['claude']])
        if entry.get('codex') and entry['codex'] not in codex_installed:
            market = entry['codex'].split('@')[-1]
            if entry.get('codex_marketplace') and not re.search(rf'(^|\W){re.escape(market)}(\W|$)', markets):
                commands.append(['codex', 'plugin', 'marketplace', 'add', os.path.expanduser(entry['codex_marketplace'])])
            commands.append(['codex', 'plugin', 'add', entry['codex']])
    return commands


def backup_configs():
    target = STATE / 'backups' / datetime.now().strftime('%Y%m%d-%H%M%S')
    target.mkdir(parents=True, exist_ok=True)
    for path in (HOME / '.claude.json', HOME / '.codex/config.toml', HOME / '.hermes/config.yaml'):
        if path.exists():
            shutil.copy2(path, target / path.name)
    return target


def sync(apply=False):
    spec = manifest()
    state = read_json(STATE / 'sync.json', {'links': {}, 'mcp': []})
    links, problems = skill_plan(spec)
    stale = [p for p in state['links'] if p not in links and Path(p).is_symlink()
             and os.readlink(p) == state['links'][p]]  # 매니페스트에서 빠진 hub 링크만 지운다
    new_links = {p: t for p, t in links.items() if not Path(p).is_symlink()}
    mcp_cmds, mcp_problems, wanted = mcp_plan(spec, current_mcp(), state.get('mcp', []))
    plugin_cmds = plugin_plan(spec)
    plan = {'skill_links': new_links, 'remove_links': stale, 'mcp': [' '.join(c) for c in mcp_cmds],
            'plugins': [' '.join(c) for c in plugin_cmds], 'problems': problems + mcp_problems,
            'unmanaged_skills': unmanaged_skills(spec)}
    if not apply:
        return plan
    plan['backup'] = str(backup_configs())
    for path in stale:
        Path(path).unlink()
    for path, target in new_links.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).symlink_to(target)
    failures = []
    for cmd in mcp_cmds + plugin_cmds:
        # hermes mcp add/remove 는 '도구를 모두 켤까요? [Y/n]' 처럼 확인을 묻는다
        p = subprocess.run(cmd, capture_output=True, text=True, input='Y\n' if cmd[0] == 'hermes' else '', timeout=300)
        if p.returncode != 0:
            failures.append(f'{" ".join(cmd[:4])}: {(p.stderr or p.stdout).strip()[-300:]}')
    rules = subprocess.run([sys.executable, str(HOME / 'projects/ArcAI.ve/tools/workspace_admin.py'), 'sync-rules'],
                           capture_output=True, text=True)
    plan['rules'] = (rules.stdout or rules.stderr).strip()[-300:]
    plan['failures'] = failures
    write_json(STATE / 'sync.json', {'links': links, 'mcp': wanted})
    return plan


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(prog='hub', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p_status = sub.add_parser('status', help='쿼터와 상태')
    p_status.add_argument('--fresh', action='store_true', help='캐시 무시')
    p_run = sub.add_parser('run', help='작업 위임')
    p_run.add_argument('task')
    p_run.add_argument('--kind', choices=sorted(ORDER))
    p_run.add_argument('--to', choices=sorted(EXECUTORS), help='실행자 고정(대체 없음)')
    p_run.add_argument('--cwd')
    p_run.add_argument('--timeout', type=int, default=1800)
    p_sync = sub.add_parser('sync', help='스킬·MCP·플러그인·공통 지침 동기화')
    p_sync.add_argument('--apply', action='store_true', help='계획만 보이지 말고 실제로 반영')
    args = parser.parse_args(argv)
    if args.command == 'status':
        result = status(args.fresh)
        pending = [json.loads(line)['id'] for line in (STATE / 'runs.jsonl').open()
                   if json.loads(line).get('needs_review')] if (STATE / 'runs.jsonl').exists() else []
        result['needs_review'] = pending[-10:]
    elif args.command == 'run':
        result = run(args.task, args.kind, args.to, args.cwd, args.timeout)
    else:
        result = sync(args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
