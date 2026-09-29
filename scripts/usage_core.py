"""Read-only, deduplicated Claude telemetry shared by all reports."""
from __future__ import annotations
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens')


def settings():
    result = {'workspace': str(ROOT.parent), 'timezone': '-03:00'}
    p = ROOT / 'config/local.json'
    if p.exists():
        result.update(json.loads(p.read_text(encoding='utf-8-sig')))
    return result


def transcript_dir(workspace=None):
    cfg = settings()
    override = os.environ.get('PIPELINE_TRANSCRIPTS') or cfg.get('claude_transcripts')
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    key = re.sub(r'[^a-zA-Z0-9]', '-', str(Path(workspace or cfg['workspace']).resolve()))
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))) / 'projects' / key


def timezone(value):
    if value in ('UTC', 'Z'):
        return dt.timezone.utc
    if re.fullmatch(r'[+-]\d\d:\d\d', value):
        h, m = map(int, value[1:].split(':'))
        if h > 23 or m > 59:
            raise ValueError('Fuso invalido')
        return dt.timezone(dt.timedelta(minutes=(h * 60 + m) * (-1 if value[0] == '-' else 1)))
    from zoneinfo import ZoneInfo
    return ZoneInfo(value)


def instant(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def context(usage):
    return sum(usage.get(k, 0) for k in FIELDS[:3])


def rates():
    return json.loads((ROOT / 'config/pricing.json').read_text(encoding='utf-8'))


def estimate(model, usage, pricing, metadata=None):
    rate = pricing['models'].get(model)
    if rate is None:
        rate = next((v for k, v in pricing['models'].items()
                     if re.fullmatch(re.escape(k) + r'-\d{8}', model)), None)
    if rate is None:
        return None, {}, ['modelo sem tarifa']
    reasons = []
    detail = usage.get('cache_creation') or {}
    if not isinstance(detail, dict):
        return None, {}, ['detalhe de cache invalido']
    five, hour = detail.get('ephemeral_5m_input_tokens', 0), detail.get('ephemeral_1h_input_tokens', 0)
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in (five, hour)):
        return None, {}, ['detalhe de cache invalido']
    if five + hour != usage.get('cache_creation_input_tokens', 0):
        reasons.append('TTL de escrita ausente/inconsistente')
    if (metadata or {}).get('speed') in ('fast', 'fast_mode'):
        reasons.append('tarifa fast nao configurada')
    parts = {'input': usage.get('input_tokens', 0) * rate['input'],
             'cache_5m': five * rate['cache_5m'], 'cache_1h': hour * rate['cache_1h'],
             'cache_read': usage.get('cache_read_input_tokens', 0) * rate['cache_read'],
             'output': usage.get('output_tokens', 0) * rate['output']}
    factor = 1.1 if (metadata or {}).get('inference_geo') == 'us' else 1
    parts = {k: v * factor / 1e6 for k, v in parts.items()}
    return (None if reasons else sum(parts.values())), parts, reasons


def collect(directory=None, pricing=None):
    directory = Path(directory) if directory else transcript_dir()
    pricing = pricing or rates()
    calls, sessions = {}, {}
    diagnostics = collections.Counter()
    files = sorted(directory.rglob('*.jsonl')) if directory.is_dir() else []
    for path in files:
        rel = path.relative_to(directory)
        if len(rel.parts) != 1 and 'subagents' not in rel.parts:
            continue
        sid = rel.as_posix().removesuffix('.jsonl')
        parent = rel.parts[0] if 'subagents' in rel.parts else None
        session = sessions.setdefault(sid, {'id': sid, 'parent_id': parent, 'kind': 'subagent' if parent else 'main',
                                            'latest_event': None, 'latest_valid': None, 'message_ids': set()})
        with path.open(encoding='utf-8-sig', errors='replace') as stream:
            for line_no, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    diagnostics['malformed_lines'] += 1
                    continue
                if not isinstance(row, dict):
                    continue
                message = row.get('message') or {}
                if not isinstance(message, dict):
                    continue
                usage = message.get('usage') or row.get('usage')
                if not isinstance(usage, dict):
                    continue
                model = message.get('model') or row.get('model') or '?'
                stamp, when = row.get('timestamp'), instant(row.get('timestamp'))
                if when is None:
                    diagnostics['invalid_timestamps'] += 1
                synthetic = model == '<synthetic>' or bool(row.get('isApiErrorMessage'))
                previous = session['latest_event']
                if when and (not previous or when >= instant(previous['timestamp'])):
                    session['latest_event'] = {'timestamp': stamp, 'error': synthetic}
                if synthetic:
                    diagnostics['synthetic_records'] += 1
                    continue
                if any(not isinstance(usage.get(k, 0), int) or isinstance(usage.get(k, 0), bool)
                       or usage.get(k, 0) < 0 for k in FIELDS):
                    diagnostics['invalid_usage'] += 1
                    continue
                mid = message.get('id') or row.get('requestId') or row.get('uuid')
                if not mid:
                    mid = f'no-id:{sid}:{line_no}'
                    diagnostics['records_without_id'] += 1
                mid = str(mid)
                session['message_ids'].add(mid)
                candidate = {'id': mid, 'session_id': sid, 'parent_id': parent, 'kind': session['kind'],
                             'timestamp': stamp, 'model': model, 'usage': usage,
                             'metadata': {k: message.get(k, usage.get(k)) for k in ('speed', 'inference_geo')},
                             'seen_in': [sid]}
                prior = calls.get(mid)
                if prior:
                    diagnostics['duplicate_records'] += 1
                    if prior['usage'] != usage:
                        diagnostics['usage_variants'] += 1
                    score = lambda c: (c['usage'].get('output_tokens', 0), context(c['usage']),
                                       len(c['usage'].get('cache_creation') or {}))
                    locations = sorted(set(prior['seen_in'] + [sid]))
                    if score(candidate) > score(prior):
                        for key in ('session_id', 'parent_id', 'kind'):
                            candidate[key] = prior[key]
                        calls[mid] = candidate
                    calls[mid]['seen_in'] = locations
                else:
                    calls[mid] = candidate
                valid = session['latest_valid']
                if when and (not valid or when >= instant(valid['timestamp'])):
                    session['latest_valid'] = {'timestamp': stamp, 'message_id': mid}
    for call in calls.values():
        value, parts, reasons = estimate(call['model'], call['usage'], pricing, call['metadata'])
        call.update(estimated_usd=value, cost_parts=parts, unpriced_reasons=reasons)
    for session in sessions.values():
        latest = session['latest_valid']
        session['context_tokens'] = context(calls[latest['message_id']]['usage']) if latest else None
        session['observed_calls'] = len(session.pop('message_ids'))
        session['live_state'] = 'unknown'
    return {'schema_version': 1, 'transcript_directory': str(directory),
            'pricing_as_of': pricing['as_of'], 'pricing_source': pricing['source'],
            'calls': list(calls.values()), 'sessions': list(sessions.values()),
            'diagnostics': dict(diagnostics), 'source_exists': directory.is_dir()}


def summarize(report, tz='UTC', start=None, end=None, session_ids=None):
    zone = timezone(tz)
    totals, by_day, parts = collections.Counter(), {}, collections.Counter()
    known, unpriced = 0.0, 0
    for call in report['calls']:
        when = instant(call['timestamp'])
        if session_ids is not None and not set(call['seen_in']).intersection(session_ids):
            continue
        if (start or end) and (when is None or (start and when < start) or (end and when >= end)):
            continue
        day = when.astimezone(zone).date().isoformat() if when else 'unknown'
        bucket = by_day.setdefault(day, {'calls': 0, 'tokens': 0, 'known_estimated_usd': 0.0, 'unpriced_calls': 0})
        totals['calls'] += 1
        totals[call['kind'] + '_calls'] += 1
        bucket['calls'] += 1
        for key in FIELDS:
            count = call['usage'].get(key, 0)
            totals[key] += count
            bucket['tokens'] += count
        if call['estimated_usd'] is None:
            unpriced += 1
            bucket['unpriced_calls'] += 1
        else:
            known += call['estimated_usd']
            bucket['known_estimated_usd'] += call['estimated_usd']
            parts.update(call['cost_parts'])
    return {'totals': dict(totals), 'days': by_day, 'cost_parts': dict(parts),
            'known_estimated_usd': known, 'unpriced_calls': unpriced,
            'estimated_usd': known if not unpriced else None, 'subscription_quota': None, 'timezone': tz,
            'notice': 'Equivalente API estimado; nao e fatura nem percentual da assinatura. Extras nao incluidos.'}


def window(anchor=None, now=None, tz='UTC'):
    now = now or dt.datetime.now(dt.timezone.utc)
    zone = timezone(tz)
    if anchor:
        start = dt.datetime.fromisoformat(anchor.replace('Z', '+00:00'))
        if start.tzinfo is None:
            start = start.replace(tzinfo=zone)
        if start > now:
            raise ValueError('Ancora deve estar no passado')
        start += ((now - start) // dt.timedelta(days=7)) * dt.timedelta(days=7)
        return start, start + dt.timedelta(days=7), 'janela ancorada pelo usuario; reset nao verificado'
    end = now.astimezone(zone)
    return end - dt.timedelta(days=7), end, 'ultimos 7 dias; reset da assinatura desconhecido'
