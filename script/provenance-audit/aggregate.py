#!/usr/bin/env python3
"""Offline reader for OpenCode audit/provenance JSONL and V2 SQLite tools.

Untrusted strings are data: no shell execution, eval, network or logged-command replay.
Only explicitly selected output files are written. Standard library only.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
from urllib.parse import quote, unquote, urlsplit

URL = re.compile(r"(?:https?|ssh|git)://[^\s<>\x22\x27`]+", re.I)
SENSITIVE = {
    'email': re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'),
    'private_path': re.compile(r'(?:/home/|/Users/|[A-Z]:\\Users\\)[^\s\x22\x27]+'),
    'credential_marker': re.compile(r'(?i)(?:authorization\s*[:=]|bearer\s+\S+|(?:api[_-]?key|password|passwd|secret|access[_-]?token)\s*[=:]\s*\S+|gh[pousr]_[\w]{12,}|sk-[\w-]{16,}|AKIA[A-Z0-9]{16})'),
}
INJECTION = re.compile(r'(?i)(?:ignore (?:all |the )?(?:previous|prior|above) (?:instructions|rules)|(?:send|upload|post|submit|transmit|exfiltrat\w*)[^\n]{0,140}(?:credentials|secrets|password|api.?key|personal|user.?info|email address)|(?:system|developer) (?:prompt|instructions?)\s*:)')
PUBLIC = {'raw.githubusercontent.com', 'github.com', 'svn.apache.org', 'apr.apache.org', 'perl.apache.org', 'httpd.apache.org', 'opencode.ai', 'models.dev', 'www.google.com', 'huggingface.co', 'gitbox.apache.org'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def load_jsonl(path, alias):
    """Bound each live input at its starting byte size; retain line references."""
    if not path.is_file():
        return [], {'source': alias, 'available': False}
    with path.open('rb') as f:
        raw = f.read(os.fstat(f.fileno()).st_size)
    records, bad = [], []
    for n, line in enumerate(raw.splitlines(), 1):
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError('not object')
            records.append((f'{alias}:L{n}', data))
        except (ValueError, UnicodeDecodeError):
            bad.append(n)
    return records, {'source': alias, 'available': True, 'bytes_read': len(raw), 'sha256': digest(raw), 'rows': len(records), 'malformed_lines': bad}


def unique_records(records, by_id=False):
    unique, seen, duplicates, conflicting_ids = [], {}, [], []
    for ref, row in records:
        identity = canonical(row)
        key = row.get('id', identity) if by_id else identity
        if key in seen and seen[key][1] == identity:
            duplicates.append({'copy': ref, 'original': seen[key][0]})
            continue
        if key in seen:
            conflicting_ids.append([seen[key][0], ref])
            key = identity
        seen[key] = (ref, identity)
        unique.append((ref, row))
    return unique, duplicates, conflicting_ids


def epoch(value):
    from datetime import datetime
    if isinstance(value, (float, int)):
        return value / 1000 if value > 10**11 else float(value)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def utc(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z') if value is not None else 'unknown'


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(x.get('text', '') for x in value if isinstance(x, dict) and isinstance(x.get('text'), str))
    return ''


def sensitive(text, identities=()):
    text = unquote(text)
    result = {kind for kind, pattern in SENSITIVE.items() if pattern.search(text)}
    if any(re.search(r'(?<!\w)' + re.escape(i) + r'(?!\w)', text, re.I) for i in identities if len(i) >= 3):
        result.add('local_identity')
    return sorted(result)


def destination(raw, identities=()):
    """Allow only known public source paths; never emit query/userinfo/fragment."""
    try:
        u = urlsplit(raw.rstrip(');,}'))
        host = (u.hostname or '').lower()
        local = host in ('localhost', '127.0.0.1', '::1')
        try:
            addr = ipaddress.ip_address(host)
            local = local or addr.is_loopback or addr.is_private or addr.is_link_local
        except ValueError:
            pass
        flags = set(sensitive(raw.split('#', 1)[0], identities))
        if u.username is not None or u.password is not None:
            flags.add('url_userinfo')
        if u.query:
            flags.add('query_present')
        if u.fragment:
            flags.add('fragment_present_not_sent_in_http')
        if '$' in host or not host:
            host = 'unresolved-host'
        elif host not in PUBLIC and not local:
            host = 'host-' + digest(host)[:12]  # Unknown DNS labels can themselves contain identifiers.
        path = u.path
        public_path = (host in ('raw.githubusercontent.com', 'github.com') and re.match(r'^/apache/(?:apr|apr-util|mod_perl)(?:/|$)', path)) or (host == 'svn.apache.org' and re.match(r'^/repos/asf/(?:poi/)?mod_perl(?:/|$)', path))
        try:
            port = (':' + str(u.port)) if u.port else ''
        except ValueError:
            port = ':[port-unresolved]'
            flags.add('dynamic_port')
        display = (u.scheme or 'https') + '://' + host + port
        display += path if public_path and not sensitive(path, identities) and not any(c in path for c in '$`{}') else ('/[path-redacted]' if path not in ('', '/') else path)
        display += '?[query-redacted]' if u.query else ''
        return {'host': host, 'url': display, 'local_or_private': local, 'url_signals': sorted(flags)}
    except (ValueError, UnicodeError):
        return {'host': 'unparseable-host', 'url': '[URL-redacted]', 'local_or_private': False, 'url_signals': ['unparseable_url']}


def lexical_network(command, depth=0):
    """Conservative literal command candidates; not a shell interpreter/request counter."""
    if depth > 3:
        return []
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=';&|()\n')
        lex.whitespace = ' \t\r'
        lex.whitespace_split = True
        lex.commenters = '#'
        tokens = list(lex)
    except ValueError:
        return []
    groups, group, result = [], [], []
    for token in tokens + [';']:
        if token and all(c in ';&|()\n' for c in token):
            if group:
                groups.append(group)
            group = []
        else:
            group.append(token)
    for words in groups:
        # Nested shell script strings are parsed only when explicitly passed with -c.
        for n, word in enumerate(words):
            if Path(word).name in ('bash', 'sh', 'zsh', 'dash') and any(re.fullmatch(r'-[a-z]*c[a-z]*', x) for x in words[n + 1:]):
                i = next(j for j in range(n + 1, len(words)) if re.fullmatch(r'-[a-z]*c[a-z]*', words[j]))
                if i + 1 < len(words):
                    result.extend(lexical_network(words[i + 1], depth + 1))
        if words[0] in ('echo', 'printf', 'cat', 'rg', 'grep', 'sed'):
            continue
        for n, word in enumerate(words):
            program = Path(word).name
            if program in ('curl', 'wget'):
                result.append({'program': program, 'tokens': words[n + 1:]})
                break
            if program == 'git' and any(x in words[n + 1:] for x in ('clone', 'fetch', 'pull', 'push', 'ls-remote', 'submodule')):
                result.append({'program': 'git', 'tokens': words[n + 1:]})
                break
    return result


def category(command, tool):
    if tool not in ('bash', 'shell', 'execute'):
        return {'webfetch': 'network.fetch', 'websearch': 'network.search', 'read': 'local.read', 'edit': 'local.edit', 'write': 'local.write'}.get(tool, 'tool.' + tool)
    if lexical_network(command):
        return 'command.network_literal'
    patterns = [('container', r'\bpodman\b'), ('session_monitoring', r'opencode\.db|session_message|session_v2'), ('git_inspect', r'\bgit\s+(?:status|diff|log|show|worktree|rev-parse)\b'), ('git_mutate', r'\bgit\s+(?:add|commit|merge|rebase|reset|checkout|restore)\b'), ('build_test', r'\b(?:make|gcc|gdb|prove|pytest|typecheck|bun\s+(?:run|test))\b'), ('read_search', r'\b(?:cat|rg|grep|find|sed|head|tail|ls|readelf|objdump)\b'), ('wait', r'\bsleep\b')]
    return next(('command.' + tag for tag, pat in patterns if re.search(pat, command)), 'command.other')


def outcome(state):
    status = state.get('status', 'unknown')
    metadata = state.get('metadata') or {}
    code = metadata.get('exit', metadata.get('exitCode'))
    text = content_text(state.get('content', state.get('output')))
    error = canonical(state.get('error', {}))
    combined = (text if status == 'error' or (isinstance(code, int) and code != 0) else '') + '\n' + error
    signals = []
    for tag, pat in [('guard_rejected', r'read.only.*(?:redirection|write)|cannot use shell redirection|blocked git subcommand'), ('missing_resource', r'No such file|File not found|Working directory does not exist|ENOENT'), ('stale_edit', r'Could not find oldString|oldString not found|not unique'), ('http_404', r'(?i)(?:status(?: code)?[: (]+404|404[: ]+Not Found|HTTP/\S+ 404)'), ('auth_denied', r'(?i)(?:HTTP/\S+ (?:401|403)|status(?: code)?[: (]+(?:401|403))')]:
        if re.search(pat, combined):
            signals.append(tag)
    return {'tool_status': status, 'process_exit': code if isinstance(code, int) else None, 'result': 'tool_error' if status == 'error' else 'process_nonzero' if isinstance(code, int) and code != 0 else 'completed_exit_zero' if status == 'completed' and code == 0 else 'tool_completed_exit_unknown' if status == 'completed' else status, 'signals': signals}


def read_database(path, audit_sessions, project):
    if not path.is_file():
        return [], {}, {'available': False}
    con = sqlite3.connect('file:' + quote(str(path.resolve())) + '?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    con.execute('BEGIN')
    try:
        sessions = {r[0]: {'project': r[1], 'directory': r[2]} for r in con.execute('SELECT id,project_id,directory FROM session_v2')}
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        legacy_counts = {name: con.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] for name in ('session', 'message', 'part', 'session_part') if name in tables}
        project_ids = {v['project'] for v in sessions.values() if Path(v['directory']).resolve() == project.resolve()}
        selected = set(audit_sessions) | {s for s, v in sessions.items() if v['project'] in project_ids}
        legacy_selected = 0
        if 'session' in tables:
            legacy_selected = sum(sid in selected or pid in project_ids for sid, pid in con.execute('SELECT id,project_id FROM session'))
        records = []
        if selected:
            placeholders = ','.join('?' for _ in selected)
            rows = con.execute(f"SELECT id,session_id,seq,time_created,data FROM session_message WHERE type='assistant' AND session_id IN ({placeholders}) ORDER BY session_id,seq,id", sorted(selected))
            for mid, sid, seq, created, data in rows:
                try:
                    parsed = json.loads(data)
                except (ValueError, TypeError):
                    continue
                for ordinal, item in enumerate(parsed.get('content') or []):
                    if not isinstance(item, dict) or item.get('type') != 'tool':
                        continue
                    state = item.get('state') or {}
                    timing = item.get('time') or {}
                    records.append({'ref': f'db:{mid}:{ordinal}', 'session': sid, 'seq': seq, 'call_id': item.get('id'), 'tool': item.get('name', item.get('tool', 'unknown')), 'input': state.get('input') or {}, 'state': state, 'epoch': epoch(timing.get('ran', timing.get('created', created))), 'executed': item.get('executed'), 'project': sessions[sid]['project'] in project_ids})
        coverage = {'available': True, 'legacy_table_rows': legacy_counts, 'legacy_sessions_in_selected_scope': legacy_selected, 'legacy_scope_limit': 'legacy part parser not used; any selected legacy sessions require additional outcome review', 'audit_sessions_present': len(set(audit_sessions) & sessions.keys()), 'project_sessions_present': sum(v['project'] in project_ids for v in sessions.values()), 'selected_tools': len(records), 'selected_tool_snapshot_sha256': digest(canonical(records)), 'snapshot_max_message_time': con.execute('SELECT MAX(time_updated) FROM session_message').fetchone()[0]}
        return records, sessions, coverage
    finally:
        con.rollback()
        con.close()


def requests_for(tool, identities):
    name, inp = tool['tool'], tool['input']
    if not isinstance(inp, dict):
        return []
    candidates = []
    if name == 'webfetch' and isinstance(inp.get('url'), str):
        candidates.append(('webfetch', inp['url'], canonical({k: v for k, v in inp.items() if k.lower() in ('body', 'headers', 'data', 'payload')}), []))
    elif name == 'websearch':
        query = inp.get('query', '')
        return [{'kind': 'websearch', 'host': 'search-provider-unrecorded', 'url': '[search-query-redacted]', 'local_or_private': False, 'url_signals': [], 'request_signals': sensitive(canonical(query), identities), 'payload_flags': ['search_query'], 'fingerprint': digest(canonical(inp)), '_raw_url': '', '_payload': canonical(query)}]
    elif name in ('shell', 'bash', 'execute'):
        command = inp.get('command', inp.get('code', ''))
        if not isinstance(command, str):
            return []
        for invocation in lexical_network(command):
            tokens = invocation['tokens']
            payload_indices = set()
            for i, token in enumerate(tokens[:-1]):
                if token in ('-H', '--header', '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '-F', '--form', '-T', '--upload-file', '-u', '--user', '--oauth2-bearer', '-b', '--cookie', '-o', '--output', '-K', '--config'):
                    payload_indices.add(i + 1)
            urls, payloads, flags = [], [], []
            for n, token in enumerate(tokens):
                if n not in payload_indices and URL.fullmatch(token):
                    urls.append(token)
                elif token.startswith('--url='):
                    urls.append(token.split('=', 1)[1])
                elif re.match(r'^[\w.-]+@[\w.-]+:[\w/]', token) and invocation['program'] == 'git':
                    urls.append('ssh://' + token.replace(':', '/', 1))
                if token in ('-H', '--header', '-d', '--data', '--data-raw', '--data-binary', '--data-urlencode', '-F', '--form', '-T', '--upload-file', '-u', '--user', '--oauth2-bearer', '-b', '--cookie'):
                    flags.append('header' if token in ('-H', '--header') else 'credential_option' if token in ('-u', '--user', '--oauth2-bearer', '-b', '--cookie') else 'body_or_upload')
                    if n + 1 < len(tokens):
                        payloads.append(tokens[n + 1])
                elif re.match(r'--(?:data[^=]*|header|form|upload-file|user|oauth2-bearer|cookie)=|^-(?:d|H|F|T|u|b).+', token):
                    flags.append('attached_request_option')
                    payloads.append(token)
            if any('--config' == t or t == '-K' for t in tokens):
                flags.append('external_config_uninspected')
            for url in urls:
                candidates.append((invocation['program'] + '_literal', url, '\n'.join(payloads), sorted(set(flags))))
            if not urls:
                candidates.append((invocation['program'] + '_dynamic', '', '\n'.join(payloads), sorted(set(flags + ['destination_unresolved']))))
    results = []
    for kind, raw, payload, flags in candidates:
        # Keep separate repeated invocations/URL arguments, even inside one tool.
        key = (kind, raw, payload)
        d = destination(raw, identities) if raw else {'host': 'destination-unresolved', 'url': '[dynamic-destination]', 'local_or_private': False, 'url_signals': []}
        d.update(kind=kind, request_signals=sorted(set(sensitive(payload, identities)) | set(d['url_signals'])), payload_flags=flags, fingerprint=digest(canonical(key)), _raw_url=raw, _payload=payload)
        results.append(d)
    return results


def aggregate(args):
    audit_rows, audit_meta = load_jsonl(args.audit, 'audit')
    audit, audit_dupes, _ = unique_records(audit_rows)
    prov_rows, input_meta = [], [audit_meta]
    for name in ('events.jsonl', 'incidents.jsonl'):
        rows, meta = load_jsonl(args.provenance_dir / name, 'provenance-' + name.removesuffix('.jsonl'))
        prov_rows.extend(rows)
        input_meta.append(meta)
    provenance, prov_dupes, conflicts = unique_records(prov_rows, by_id=True)
    audit_sessions = {d.get('context', {}).get('sessionId') for _, d in audit if d.get('context', {}).get('sessionId')}
    tools, sessions, db_meta = read_database(args.db, audit_sessions, args.project)
    project_ids = {v['project'] for v in sessions.values() if Path(v['directory']).resolve() == args.project.resolve()}
    all_sessions = audit_sessions | {sid for sid, v in sessions.items() if v['project'] in project_ids}
    aliases = {sid: 'S%02d' % (n + 1) for n, sid in enumerate(sorted(all_sessions))}
    identities = [Path.home().name]
    if args.identifiers:
        identities.extend(x.strip() for x in args.identifiers.read_text().splitlines() if x.strip())
    by_command = defaultdict(list)
    for t in tools:
        if t['tool'] in ('shell', 'bash') and isinstance(t['input'].get('command'), str):
            by_command[(t['session'], t['tool'], t['input']['command'])].append(t)
    audit_details, ambiguous, unmatched, linked = [], 0, 0, set()
    by_session, by_tool, by_category, by_day, by_hour = (Counter() for _ in range(5))
    raw_by_tool = Counter(d.get('context', {}).get('toolName', '<none>') for _, d in audit_rows)
    for ref, event in audit:
        ctx = event.get('context', {})
        sid, name, command = ctx.get('sessionId'), ctx.get('toolName', '<none>'), ctx.get('destination', '')
        when = epoch(event.get('timestamp'))
        cat = category(command, name) if name != '<none>' else 'audit.' + event.get('type', 'unknown')
        entry = {'ref': ref, 'session': aliases.get(sid, '<none>'), 'tool': name, 'time': utc(when), 'type': event.get('type', 'unknown'), 'allowed': event.get('metadata', {}).get('allowed'), 'category': cat, 'command_fingerprint': digest(command), 'input_signals_not_transmission': sensitive(command, identities)}
        if sid and name in ('shell', 'bash'):
            candidates = [t for t in by_command[(sid, name, command)] if when is not None and t['epoch'] is not None and abs(when - t['epoch']) <= 5 and t['ref'] not in linked]
            if len(candidates) == 1:
                t = candidates[0]
                entry['db_ref'], entry['outcome'] = t['ref'], outcome(t['state'])
                linked.add(t['ref'])
                t['audit_ref'], t['allowed'] = ref, entry['allowed']
            elif candidates:
                ambiguous += 1
                entry['correlation'] = 'ambiguous_same_command_time_window'
            else:
                unmatched += 1
                entry['correlation'] = 'no_unique_db_match'
        by_session[entry['session']] += 1
        by_tool[name] += 1
        by_category[cat] += 1
        by_day[entry['time'][:10]] += 1
        by_hour[entry['time'][:13]] += 1
        audit_details.append(entry)
    # Tools are independently counted by stable message/content/call identities, not audit rows.
    tool_counts, outcomes, error_signals = Counter(), Counter(), Counter()
    db_categories, db_days, db_hours = Counter(), Counter(), Counter()
    session_tools, tool_details, network, tool_groups = defaultdict(Counter), [], [], defaultdict(list)
    for t in tools:
        status = outcome(t['state'])
        tool_counts[t['tool']] += 1
        db_categories[category(t['input'].get('command', t['input'].get('code', '')), t['tool'])] += 1
        db_days[utc(t['epoch'])[:10]] += 1
        db_hours[utc(t['epoch'])[:13]] += 1
        outcomes[status['result']] += 1
        error_signals.update(status['signals'])
        session_tools[aliases[t['session']]][t['tool']] += 1
        fingerprint = digest(canonical(t['input']))
        tool_groups[(t['session'], t['tool'], fingerprint)].append(t)
        tool_details.append({'ref': t['ref'], 'call_id': t['call_id'], 'session': aliases[t['session']], 'time': utc(t['epoch']), 'tool': t['tool'], 'category': category(t['input'].get('command', t['input'].get('code', '')), t['tool']), 'outcome': status, 'input_fingerprint': fingerprint, 'project': t['project']})
        for req in requests_for(t, identities):
            req.update(ref=t['ref'], audit_ref=t.get('audit_ref'), session=aliases[t['session']], time=utc(t['epoch']), explicitly_audit_allowed=t.get('allowed'), tool_outcome=status, _tool=t)
            network.append(req)
    # Preserve audit-only network candidates, never count matched audit/DB copies twice.
    for entry, (_, event) in zip(audit_details, audit):
        if 'db_ref' in entry:
            continue
        ctx = event.get('context', {})
        fake = {'tool': ctx.get('toolName'), 'input': {'command': ctx.get('destination', '')}}
        for req in requests_for(fake, identities):
            req.update(ref=entry['ref'], session=entry['session'], time=entry['time'], explicitly_audit_allowed=entry['allowed'], tool_outcome={'result': 'not_recorded'}, audit_ref=entry['ref'], _tool=None)
            network.append(req)
    # Source retrieval repeats are candidates grouped by exact endpoint, query included in fingerprint.
    source_groups = defaultdict(list)
    host_counts = defaultdict(Counter)
    for req in network:
        host_counts[req['host']]['candidates'] += 1
        host_counts[req['host']]['local_or_private'] += int(req['local_or_private'])
        host_counts[req['host']]['explicit_audit_allowed'] += int(req['explicitly_audit_allowed'] is True)
        host_counts[req['host']][req['tool_outcome']['result']] += 1
        if re.search(r'apache/(?:apr|mod_perl)|perl-security/mod_perl|repos/asf/(?:poi/)?mod_perl', req['_raw_url']):
            source_groups[digest(req['_raw_url'])].append(req)
    # Scan external fetched/search text only. This finds candidates, never establishes causality alone.
    injection = []
    for req in network:
        t = req['_tool']
        if not t or req['local_or_private'] or req['kind'] not in ('webfetch', 'websearch', 'curl_literal', 'wget_literal'):
            continue
        text = content_text(t['state'].get('content', t['state'].get('output')))
        matches = list(INJECTION.finditer(text))
        if not matches:
            continue
        windows = [''.join(text[max(0, m.start() - 200):m.end() + 400]) for m in matches]
        directed = {u.rstrip(');,') for w in windows for u in URL.findall(w)}
        subsequent = [r for r in network if r['session'] == req['session'] and r['ref'] != req['ref'] and t['epoch'] is not None and epoch(r['time']) is not None and 0 < epoch(r['time']) - t['epoch'] <= 1800 and r['_raw_url'] in directed]
        injection.append({'source_ref': t['ref'], 'candidate_patterns': len(matches), 'directed_endpoint_count': len(directed), 'later_exact_endpoint_refs': sorted({r['ref'] for r in subsequent}), 'classification': 'instruction_candidate_with_later_endpoint_match_needs_review' if subsequent else 'instruction_text_candidate_no_observed_endpoint_match'})
    # Provenance correlations use explicit call IDs where present; do not pretend previews are full inputs.
    call_ids = {t['call_id'] for t in tools if t['call_id']}
    selected_prov = [(ref, d) for ref, d in provenance if d.get('session_id') in all_sessions]
    prov_tools = [(ref, d) for ref, d in selected_prov if d.get('kind') in ('tool.completed', 'tool.error')]
    def count(items):
        return dict(sorted(Counter(items).items()))
    repeats = []
    for (sid, name, fp), group in tool_groups.items():
        if len(group) > 1:
            repeats.append({'session': aliases[sid], 'tool': name, 'input_fingerprint': fp, 'calls': len(group), 'category': category(group[0]['input'].get('command', group[0]['input'].get('code', '')), name), 'distinct_call_ids': len({x['call_id'] for x in group}), 'refs': [x['ref'] for x in group], 'outcomes': count(outcome(x['state'])['result'] for x in group)})
    sources = [{'url': group[0]['url'], 'host': group[0]['host'], 'attempt_candidates': len(group), 'refs': [r['ref'] for r in group], 'outcomes': count(r['tool_outcome']['result'] for r in group), 'http_404_mentions': sum('http_404' in r['tool_outcome'].get('signals', []) for r in group), 'endpoint_fingerprint': fp} for fp, group in sorted(source_groups.items())]
    safe_network = [{k: v for k, v in r.items() if not k.startswith('_')} for r in network]
    resource_evidence = []
    for t in tools:
        command = t['input'].get('command', '')
        text = content_text(t['state'].get('content', t['state'].get('output')))
        if t['tool'] in ('shell', 'bash') and 'modperl_xs_sv_convert.h' in command and '#define mp_xs_sv2_APR__Pool' in text:
            resource_evidence.append({'resource': 'generated Pool conversion header', 'ref': t['ref'], 'session': aliases[t['session']], 'time': utc(t['epoch']), 'evidence': 'recorded output contains expected conversion macro', 'outcome': outcome(t['state'])})
    summary = {
        'schema_version': 1,
        'resource_availability_evidence': resource_evidence,
        'inputs': input_meta,
        'scope': {'audit_scope': 'entire supplied audit file', 'sqlite_scope': 'audit session IDs plus project_id of exact repository directory', 'audit_sessions': len(audit_sessions), 'supplemental_sessions': len(all_sessions), 'sqlite': db_meta, 'session_refs': {aliases[s]: s for s in sorted(all_sessions)}},
        'deduplication': {'method': 'exact complete canonical JSON including timestamp; provenance IDs with identical payload; no command-only or time-bucket deduplication', 'audit_raw': len(audit_rows), 'audit_unique': len(audit), 'audit_exact_copies_removed': len(audit_dupes), 'audit_duplicate_refs': audit_dupes, 'provenance_raw': len(prov_rows), 'provenance_unique': len(provenance), 'provenance_copies_removed': len(prov_dupes), 'provenance_conflicting_ids': conflicts},
        'audit': {'policy_url_checks': [{'ref': ref, **destination(d['context']['url'], identities), 'allowed': d.get('metadata', {}).get('allowed')} for ref, d in audit if d.get('context', {}).get('url')], 'input_signals_not_transmission': count(signal for entry in audit_details for signal in entry['input_signals_not_transmission']), 'types': count(d.get('type', 'unknown') for _, d in audit), 'allowed': count(str(d.get('metadata', {}).get('allowed')) for _, d in audit), 'tools_raw': dict(sorted(raw_by_tool.items())), 'tools_unique': dict(sorted(by_tool.items())), 'categories': dict(sorted(by_category.items())), 'sessions': dict(sorted(by_session.items())), 'days_utc': dict(sorted(by_day.items())), 'hours_utc': dict(sorted(by_hour.items())), 'correlation': {'unique_db_matches': len(linked), 'unmatched_shell_events': unmatched, 'ambiguous_shell_events': ambiguous, 'rule': 'same session/tool/exact command, exactly one unused DB tool within 5 seconds of recorded ran/created time'}},
        'sqlite': {'categories': dict(sorted(db_categories.items())), 'days_utc': dict(sorted(db_days.items())), 'hours_utc': dict(sorted(db_hours.items())), 'tools': dict(sorted(tool_counts.items())), 'outcomes': dict(sorted(outcomes.items())), 'outcome_signals': dict(sorted(error_signals.items())), 'sessions_tools': {k: dict(sorted(v.items())) for k, v in sorted(session_tools.items())}},
        'provenance': {'all_kinds': count(d.get('kind', 'unknown') for _, d in provenance), 'selected_kinds': count(d.get('kind', 'unknown') for _, d in selected_prov), 'selected_tool_outcomes': count(d['kind'] for _, d in prov_tools), 'selected_tool_counts': count(d.get('data', {}).get('tool', 'unknown') for _, d in prov_tools), 'selected_with_call_id': sum(bool(d.get('data', {}).get('call_id')) for _, d in prov_tools), 'selected_call_ids_matched_db': sum(d.get('data', {}).get('call_id') in call_ids for _, d in prov_tools)},
        'network': {'days_utc': count(r['time'][:10] for r in network), 'hours_utc': count(r['time'][:13] for r in network), 'sessions': count(r['session'] for r in network), 'kinds': count(r['kind'] for r in network), 'unit': 'literal URL argument candidates per recognized invocation (including repeats); unresolved invocations counted separately; loops/variables/redirects are not expanded', 'hosts': {k: dict(sorted(v.items())) for k, v in sorted(host_counts.items())}, 'candidates': len(network), 'local_or_private_candidates': sum(r['local_or_private'] for r in network), 'request_signal_counts_external_or_unresolved': count(flag for r in network if not r['local_or_private'] for flag in r['request_signals']), 'confirmed_network_transmissions': None, 'confirmation_limit': 'no packet/proxy/server receipt evidence; tool outcomes are reported operations, not exact transmitted bytes', 'source_retrieval_groups': sources},
        'repeated_tool_inputs': sorted(repeats, key=lambda x: (-x['calls'], x['session'], x['input_fingerprint'])),
        'prompt_injection': {'scan': 'instruction-pattern windows in available external webfetch/websearch/curl/wget result text; later exact endpoint matching within 30 minutes; no causal inference from match alone', 'candidates': injection, 'external_result_tools_scanned': len({r['ref'] for r in network if r['_tool'] and not r['local_or_private'] and r['kind'] in ('webfetch', 'websearch', 'curl_literal', 'wget_literal') and content_text(r['_tool']['state'].get('content', r['_tool']['state'].get('output')))}), 'observed_compliance_confirmed': None, 'compliance_classification': 'requires manual causal review; no automatic compliance finding', 'limit': 'negative heuristic result does not exclude injection, encoded payloads, truncated outputs, later actions or missing sessions'},
    }
    details = [{'record': 'audit', **r} for r in audit_details] + [{'record': 'tool', **r} for r in tool_details] + [{'record': 'network', **r} for r in safe_network]
    return summary, details


def markdown(summary):
    d, a, n = summary['deduplication'], summary['audit'], summary['network']
    lines = ['# OpenCode offline tool-use aggregation', '', 'Generated from bounded local log reads and a read-only SQLite transaction. Counts describe logged events and reported outcomes; they are not packet counts.', '', f"Audit rows: **{d['audit_raw']} raw / {d['audit_unique']} unique**, with **{d['audit_exact_copies_removed']} exact copies** removed. Genuine repeated commands remain separate. Audit sessions: {summary['scope']['audit_sessions']}; selected SQLite tools: {summary['scope']['sqlite'].get('selected_tools', 0)}.", '', '## Audit tools', '', '| Tool | Raw | Unique |', '| --- | ---: | ---: |']
    for tool, total in a['tools_unique'].items():
        lines.append(f"| {tool} | {a['tools_raw'][tool]} | {total} |")
    for title, data in [('Command/event categories', a['categories']), ('Audit periods (UTC days)', a['days_utc']), ('Audit sessions (opaque local aliases)', a['sessions']), ('Selected SQLite tool outcomes', summary['sqlite']['outcomes'])]:
        lines += ['', '## ' + title, '', '| Category | Count |', '| --- | ---: |']
        lines += [f'| {key} | {value} |' for key, value in data.items()]
    lines += ['', '## Network endpoint candidates', '', n['unit'] + '.', '', '| Host | Candidates | Explicit audit allowance | Local/private |', '| --- | ---: | ---: | ---: |']
    for host, v in n['hosts'].items():
        lines.append(f"| {host} | {v['candidates']} | {v.get('explicit_audit_allowed', 0)} | {v.get('local_or_private', 0)} |")
    lines += ['', '## APR/mod_perl source retrievals', '', '| Sanitized endpoint | Candidates | Recorded HTTP 404 mentions |', '| --- | ---: | ---: |']
    lines += [f"| `{r['url']}` | {r['attempt_candidates']} | {r['http_404_mentions']} |" for r in n['source_retrieval_groups']]
    lines += ['', '## Limits', '', '- Shell permission events establish logged allowance, not process success or transmission. Web tools are absent from this audit file; SQLite/provenance supplement them.', '- A completed shell tool may have a nonzero process exit, a masked earlier failure, or HTTP 404 without curl -f. Outcomes are reported tool observations.', '- URL/command parsing is static and incomplete: variables, redirects, nested programs, implicit network access and dynamically generated destinations remain gaps.', '- All query values, headers, bodies, command text, private paths and session titles are omitted. Unknown external hostnames are pseudonymized. Original event refs remain available in JSON/details.', '- No independent network capture, request receipt or exact wire bytes were available. No confirmed transmission count can be derived.', '- Pattern scanning does not prove absence of prompt injection. Subsequent endpoint matches require manual causal review.', '- JSONL cutoffs and the DB transaction are separate snapshots; live writers may append after the bounded reads.', '- Exact duplicate events lack logger IDs: identical legitimate events in the same millisecond cannot always be distinguished from logger copies.', '', 'See `summary.json` for hourly, session/tool, provenance, repetition, correlation, input hashes and original event references.', '']
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'opencode'
    state = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'opencode/provenance'
    p.add_argument('--audit', type=Path, default=data / 'audit.jsonl')
    p.add_argument('--db', type=Path, default=data / 'opencode.db')
    p.add_argument('--provenance-dir', type=Path, default=state)
    p.add_argument('--project', type=Path, default=Path.cwd())
    p.add_argument('--identifiers', type=Path, help='optional local file of additional names/identifiers to detect; never copied to output')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--details', action='store_true', help='also write sanitized event-level details.jsonl, never raw text')
    args = p.parse_args()
    summary, details = aggregate(args)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in [('summary.json', json.dumps(summary, indent=2, sort_keys=True) + '\n'), ('summary.md', markdown(summary)), *([('details.jsonl', ''.join(canonical(d) + '\n' for d in details))] if args.details else [])]:
        output = args.out / name
        if output.resolve() in {args.audit.resolve(), args.db.resolve(), (args.provenance_dir / 'events.jsonl').resolve(), (args.provenance_dir / 'incidents.jsonl').resolve()}:
            raise SystemExit('Output must not replace an input')
        with open(output, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write(text)
    print(canonical({'audit_unique': summary['deduplication']['audit_unique'], 'selected_tools': summary['scope']['sqlite'].get('selected_tools', 0), 'network_endpoint_candidates': summary['network']['candidates'], 'outputs': ['summary.md', 'summary.json'] + (['details.jsonl'] if args.details else [])}))


if __name__ == '__main__':
    main()
