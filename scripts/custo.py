#!/usr/bin/env python3
"""Tokens observados e equivalente API; nunca quota presumida."""
import argparse
import json
import sys
from usage_core import ROOT, collect, settings, summarize, transcript_dir, window


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', nargs='?')
    p.add_argument('--workspace')
    p.add_argument('--sessoes', action='store_true')
    p.add_argument('--semana', action='store_true')
    p.add_argument('--ancora', help='Reset informado, data/hora ISO 8601')
    p.add_argument('--timezone', default=settings()['timezone'])
    p.add_argument('--json', action='store_true')
    a = p.parse_args()
    r = collect(a.directory or transcript_dir(a.workspace))
    start = end = None
    label = 'periodo completo dos arquivos locais'
    if a.semana:
        start, end, label = window(a.ancora, tz=a.timezone)
    s = summarize(r, a.timezone, start, end)
    result = {k: v for k, v in r.items() if k != 'calls'}
    result.update(summary=s, period=label)
    if start:
        result.update(start=start.isoformat(), end=end.isoformat())
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('CLAUDE - tokens observados e equivalente API estimado')
        print('Fonte:', r['transcript_directory'])
        print('Periodo:', label, '| fuso:', a.timezone)
        if not r['source_exists']:
            print('Diretorio nao encontrado. Informe caminho ou configure config/local.json.')
        if a.sessoes:
            print('Ultima chamada VALIDA; transcript nao comprova sessao ativa:')
            for x in sorted(r['sessions'], key=lambda x: x['context_tokens'] or 0, reverse=True):
                ctx = x['context_tokens']
                limits = json.loads((ROOT / 'config/pipeline.json').read_text(encoding='utf-8'))['context']
                verdict = 'sem medicao' if ctx is None else ('transicao recomendada' if ctx >= limits['transition_tokens'] else ('atencao' if ctx >= limits['warn_tokens'] else 'abaixo do alerta'))
                print(x['id'], x['kind'], 'contexto=', ctx, verdict, 'ultimo evento=', x['latest_event'])
        else:
            for day, row in sorted(s['days'].items()):
                print(day, row)
        print('Totais:', s['totals'])
        print('Equivalente conhecido: US$ %.2f; chamadas sem estimativa: %d' % (s['known_estimated_usd'], s['unpriced_calls']))
        print(s['notice'])
        print('Diagnostico:', r['diagnostics'])
    return 0 if r['source_exists'] else 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
