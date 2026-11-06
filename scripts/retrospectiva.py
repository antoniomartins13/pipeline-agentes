#!/usr/bin/env python3
"""Retrospectiva factual; sem custo por tarefa inventado a partir de datas de arquivos."""
import argparse
import json
import re
from usage_core import ROOT, collect, settings, summarize, transcript_dir


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('task', nargs='?')
    p.add_argument('--backlog', action='store_true')
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    if a.backlog:
        txt = (ROOT / 'docs/BACKLOG.md').read_text(encoding='utf-8')
        print('\n'.join(re.findall(r'^- \[ \].*$', txt, re.M)))
        return 0
    if not a.task or not re.fullmatch(r'[a-zA-Z0-9_-]+', a.task):
        p.error('informe id simples da tarefa')
    folder = ROOT / 'tarefas' / a.task
    if not folder.is_dir():
        p.error('tarefa inexistente')
    runs = ROOT / '.local/runs' / a.task
    ids, evidence = set(), []
    for f in sorted(runs.glob('*/receipt.json')):
        data = json.loads(f.read_text(encoding='utf-8'))
        if data.get('provider') == 'claude' and data.get('session_id'):
            ids.add(data['session_id'])
        evidence.append({k: data.get(k) for k in ('phase', 'status', 'provider', 'started_at', 'ended_at', 'tests')})
    source = None
    if (folder / 'task.json').exists():
        from pipeline import repository
        source = transcript_dir(repository(json.loads((folder / 'task.json').read_text(encoding='utf-8'))))
    report = collect(source)
    # Include children of explicitly linked sessions.
    ids.update(s['id'] for s in report['sessions'] if s.get('parent_id') in ids)
    result = {'task': a.task, 'artifacts': sorted(x.name for x in folder.iterdir() if x.is_file()),
              'runs': evidence, 'claude_usage': summarize(report, settings()['timezone'], session_ids=ids) if ids else None,
              'notice': 'Sem vinculo de sessao, custo da tarefa e desconhecido. Tempo de arquivo nao mede espera humana. Rework nao prova falha de plano.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
