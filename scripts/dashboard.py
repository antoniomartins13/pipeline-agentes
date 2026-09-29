#!/usr/bin/env python3
"""Dashboard local: usa o mesmo coletor de custo.py; nao infere quota/rework por prosa."""
import html
import json
from pathlib import Path
from usage_core import ROOT, collect, settings, summarize


def render(report):
    s = summarize(report, settings()['timezone'])
    rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%.2f</td><td>%s</td></tr>' %
                   (html.escape(day), row['calls'], format(row['tokens'], ','), row['known_estimated_usd'], row['unpriced_calls'])
                   for day, row in sorted(s['days'].items()))
    tasks = []
    for p in sorted((ROOT / 'tarefas').glob('*/HANDOFF.md')):
        tasks.append('<li>%s: handoff disponivel</li>' % html.escape(p.parent.name))
    return ("<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Pipeline</title>"
            "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px;color:#18233b}"
            "td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}table{width:100%%}</style>"
            '<h1>Consumo observado do pipeline</h1><p>%s</p><p>Equivalente conhecido: US$ %.2f; %d chamadas sem tarifa completa. '
            'Quota da assinatura: desconhecida.</p><p>%d chamadas principais e %d de subagentes. Fuso: %s.</p>'
            '<table><tr><th>Dia</th><th>Chamadas</th><th>Tokens, incluindo cache</th><th>USD conhecidos</th><th>Sem estimativa</th></tr>%s</table>'
            '<h2>Tarefas</h2><ul>%s</ul><p>Ciclo, espera humana e rework exigem eventos explicitos; nao sao inferidos de mtime.</p></html>') % (
                html.escape(s['notice']), s['known_estimated_usd'], s['unpriced_calls'], s['totals'].get('main_calls', 0),
                s['totals'].get('subagent_calls', 0), html.escape(s['timezone']), rows, ''.join(tasks))


def main():
    r = collect()
    out = ROOT / '.local/dashboard.html'
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(r), encoding='utf-8')
    print(out)
    if not r['source_exists']:
        print('Fonte de transcripts ausente; dashboard sem dados.')
    return 0 if r['source_exists'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
