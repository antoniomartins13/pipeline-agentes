#!/usr/bin/env python3
"""Install thin workspace adapters; optional explicit Claude project-memory repair."""
import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
from usage_core import ROOT


def backup_write(path, text, backup_root):
    if path.exists() and path.read_text(encoding='utf-8-sig') == text:
        return
    if path.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        name = path.name + '-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S%f') + '.bak'
        shutil.copy2(path, backup_root / name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check', action='store_true')
    p.add_argument('--install', action='store_true')
    p.add_argument('--install-memory', action='store_true')
    a = p.parse_args()
    workspace = ROOT.parent
    backup = ROOT / '.local/bootstrap-backups'
    if a.install:
        for name in ('CLAUDE.md', 'AGENTS.md'):
            backup_write(workspace / name, (ROOT / 'integracoes/workspace' / name).read_text(encoding='utf-8'), backup)
        for name, entry in {'iniciar': 'INICIAR', 'revisar': 'REVISAR', 'retomar': 'RETOMAR', 'backlog': 'BACKLOG-SESSAO'}.items():
            text = f'---\nname: {name}\ndescription: Entrada {name} do pipeline da workspace; regras em POLITICA.md.\n---\n\nLeia `_pipeline/POLITICA.md` e `_pipeline/entradas/{entry}.md`. Use `$ARGUMENTS` como identificacao/insumo. Nao carregar historico completo.\n'
            backup_write(workspace / '.claude/skills' / name / 'SKILL.md', text, backup / name)
        local = ROOT / 'config/local.json'
        if not local.exists():
            shutil.copy2(ROOT / 'config/local.example.json', local)
    if a.install_memory:
        key = re.sub(r'[^a-zA-Z0-9]', '-', str(workspace))
        memory = Path.home() / '.claude/projects' / key / 'memory'
        pointer = ('---\nname: orca-pipeline-papeis\ndescription: Ponte para a politica vigente do pipeline portatil\ntype: project\n---\n\n'
                   'Politica vigente: `_pipeline/POLITICA.md`, na workspace atual. Entradas: `_pipeline/entradas/`. '
                   'Claude e Codex sao pares; coordenador em Python; sessao por fase/fatia. CP1 humano; CP3 draft sem gate previo; '
                   'Jules somente verificador; dev nao aplica migrate/sync:model no banco compartilhado. '
                   'Revisor nao corrige e parecer nao e publicado. Limiares em config/pipeline.json. '
                   'Esta ponte substitui a memoria antiga de quatro papeis e uma conversa para todo o ciclo.\n')
        backup_write(memory / 'orca-pipeline-papeis.md', pointer, backup / 'memory')
        index = memory / 'MEMORY.md'
        lines = index.read_text(encoding='utf-8-sig').splitlines() if index.exists() else []
        lines = [line for line in lines if not any(word in line for word in ('Pipeline de papéis', 'Backlog do pipeline', 'AO RETOMAR SESSÃO', '- Custo:', '[Pipeline vigente]'))]
        lines += ['- [Pipeline vigente](orca-pipeline-papeis.md) — ponte para _pipeline/POLITICA.md; nao duplicar regras aqui.',
                  '- Retomada: tarefa identificada, _pipeline/tarefas/<id>/HANDOFF.md; backlog em _pipeline/docs/BACKLOG.md.',
                  '- Consumo: custo.py mede tokens/equivalente API; quota oficial nao e inferida.']
        backup_write(index, '\n'.join(lines) + '\n', backup / 'memory')
    print(json.dumps({'workspace': str(workspace), 'pipeline': str(ROOT),
                      'cli': {name: bool(shutil.which(name)) for name in ('python', 'git', 'claude', 'agy', 'glab')},
                      'local_config': (ROOT / 'config/local.json').exists(),
                      'installed_workspace': a.install, 'installed_memory': a.install_memory,
                      'notice': 'Autenticacao, dependencias e guards sao configurados por maquina; nenhum segredo foi copiado.'}, indent=2))


if __name__ == '__main__':
    main()
