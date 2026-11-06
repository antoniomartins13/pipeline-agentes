#!/usr/bin/env python3
"""Espelho Trello -> GitLab (decisao do dono, 06/11/2026). O Trello continua sendo a fonte; o GitLab e o espelho.

- Cards do dono (membro do card) no board configurado viram uma issue cada no projeto de gestao, com link do card,
  descricao e checklists. A label fase:: segue a lista do Trello; card em lista de concluido fecha a issue.
- Tarefas do pipeline cuja spec cita o card: a issue ganha a label pipeline e repo::, e uma nota por documento
  (task-spec.md, plan.md, documentacao-tecnica.md), atualizada quando o arquivo muda. O parecer da revisao
  (review-report.md) e privado e NUNCA vai.
- Nunca apaga issue nem nota; nunca escreve no Trello.

Config (config/local.json): "trello_board" (shortLink do board), "gestao_project" (caminho no GitLab),
"gitlab_host". Chave: ~/.config/pipeline/trello.env com TRELLO_KEY=... e TRELLO_TOKEN=... (criada pelo dono;
vai no cabecalho Authorization, nunca na URL). Estado: .local/gestao.json.

Uso: trello_gitlab.py sync [--se-passou SEGUNDOS] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as runner  # noqa: E402

ROOT = runner.ROOT
STATE = ROOT / '.local' / 'gestao.json'
KEY_FILE = Path.home() / '.config' / 'pipeline' / 'trello.env'
DOCS = (('task-spec.md', 'Spec'), ('plan.md', 'Plano'), ('documentacao-tecnica.md', 'Documentação'))
# Lista do Trello -> label de fase (por prefixo do nome, sem acento/caixa).
FASES = (('parada', 'parada'), ('em refinamento', 'backlog'), ('backlog', 'backlog'), ('bugs', 'backlog'),
         ('em andamento', 'andamento'), ('aguardando correc', 'correcoes'), ('aguardando revis', 'revisao'),
         ('em fase de teste', 'teste'), ('homologac', 'homologacao'), ('concluid', 'concluido'))
SKIP_LISTS = ('arquivo',)
MARK = re.compile(r'<!-- trello:([A-Za-z0-9]+) -->')


def plain(text):
    table = str.maketrans('áàãâéêíóôõúçÁÀÃÂÉÊÍÓÔÕÚÇ', 'aaaaeeiooouc' + 'aaaaeeiooouc'.upper())
    return (text or '').translate(table).lower().strip()


def fase_of(list_name):
    name = plain(list_name)
    for prefix, fase in FASES:
        if name.startswith(prefix):
            return fase
    return 'backlog'


def issue_body(card, list_name):
    lines = [f"**Card:** {card['shortUrl']}  ·  **Lista no Trello:** {list_name}", '', (card.get('desc') or '').strip()]
    for checklist in card.get('checklists') or []:
        lines += ['', f"**{checklist.get('name') or 'Checklist'}**"]
        for item in sorted(checklist.get('checkItems') or [], key=lambda i: i.get('pos', 0)):
            lines.append(f"- [{'x' if item.get('state') == 'complete' else ' '}] {item.get('name', '').strip()}")
    lines += ['', '_Espelho do Trello (scripts/trello_gitlab.py). Edite no Trello; aqui é sobrescrito._',
              f"<!-- trello:{card['shortLink']} -->"]
    return '\n'.join(lines).strip() + '\n'


def tasks_for_card(short_link):
    """Tarefas do pipeline cuja spec cita o card."""
    found = []
    for spec in sorted((ROOT / 'tarefas').glob('*/task-spec.md')):
        if f'trello.com/c/{short_link}' in spec.read_text(encoding='utf-8', errors='replace'):
            manifest = spec.parent / 'task.json'
            found.append((spec.parent.name, runner.read(manifest) if manifest.exists() else {}))
    return found


def repo_label(manifest):
    repo = manifest.get('repo', '')
    if repo.startswith('erp-back'):
        return 'repo::back-end'
    if repo.startswith('erp-front'):
        return 'repo::front-end'
    if repo.startswith('transparencia'):
        return 'repo::portal'
    return None


def labels_for(card, list_name, tasks):
    labels = {'fase::' + fase_of(list_name)}
    if plain(list_name).startswith('bugs'):
        labels.add('tipo::bug')
    if tasks:
        labels.add('pipeline')
    labels |= {lab for _, m in tasks if (lab := repo_label(m))}
    return sorted(labels)


# --- Trello (somente leitura) -----------------------------------------------------------------------------------
def trello_get(path, params=None):
    env = {}
    if KEY_FILE.exists():
        for line in KEY_FILE.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"\'')
    key, token = env.get('TRELLO_KEY'), env.get('TRELLO_TOKEN')
    if not key or not token:
        raise RuntimeError(f'sem TRELLO_KEY/TRELLO_TOKEN em {KEY_FILE}')
    url = 'https://api.trello.com/1/' + path + ('?' + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, headers={'Authorization': f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"',
                                               'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


# --- GitLab (via glab, com o login/token do ambiente de revisao) ----------------------------------------------
def gitlab(method, path, body=None):
    import ambiente_revisao as amb
    args = ['glab', 'api', '-X', method, path]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(body, tmp, ensure_ascii=False)
        tmp.close()
        args += ['-H', 'Content-Type: application/json', '--input', tmp.name]
    try:
        out = subprocess.run(args, capture_output=True, text=True, env=amb.gitlab_env(), timeout=120)
    finally:
        if tmp:
            os.unlink(tmp.name)
    if out.returncode:
        raise RuntimeError(f'GitLab {method} {path}: ' + (out.stderr or out.stdout).strip()[-300:])
    return json.loads(out.stdout or 'null')


def sync(dry_run=False):
    cfg = runner.settings()
    board, project = cfg.get('trello_board'), cfg.get('gestao_project')
    if not board or not project:
        raise RuntimeError('configure "trello_board" e "gestao_project" em config/local.json')
    pid = urllib.parse.quote(project, safe='')
    state = runner.read(STATE) if STATE.exists() else {'cards': {}}
    me = trello_get('members/me', {'fields': 'id'})['id']
    lists = {l['id']: l['name'] for l in trello_get(f'boards/{board}/lists', {'fields': 'name', 'filter': 'all'})}
    cards = trello_get(f'boards/{board}/cards/all', {'fields': 'name,desc,idList,idMembers,shortUrl,shortLink,closed',
                                                      'checklists': 'all'})
    report = []
    for card in cards:
        if me not in (card.get('idMembers') or []):
            continue
        list_name = lists.get(card['idList'], '?')
        known = state['cards'].get(card['shortLink'])
        fase = fase_of(list_name)
        if plain(list_name).startswith(SKIP_LISTS) or card.get('closed'):
            continue
        if not known and fase == 'concluido':
            continue  # so espelha concluido de quem ja estava aqui (fecha a issue)
        tasks = tasks_for_card(card['shortLink'])
        body = {'title': card['name'][:250], 'description': issue_body(card, list_name),
                'labels': ','.join(labels_for(card, list_name, tasks))}
        digest = runner.digest(json.dumps(body, sort_keys=True).encode('utf-8'))
        entry = known or {'notes': {}}
        if dry_run:
            report.append(('criar' if not known else 'atualizar', card['shortLink'], card['name'][:60], body['labels']))
            continue
        if not known:
            issue = gitlab('POST', f'projects/{pid}/issues', body)
            entry.update(iid=issue['iid'], url=issue['web_url'])
            report.append(('criada', issue['web_url'], card['name'][:60]))
        elif entry.get('hash') != digest:
            gitlab('PUT', f"projects/{pid}/issues/{entry['iid']}", body)
            report.append(('atualizada', entry['url'], card['name'][:60]))
        want = 'close' if fase == 'concluido' else 'reopen'
        if entry.get('state_event') != want:
            gitlab('PUT', f"projects/{pid}/issues/{entry['iid']}", {'state_event': want})
            entry['state_event'] = want
        entry['hash'] = digest
        for task, _ in tasks:
            for name, title in DOCS:
                path = ROOT / 'tarefas' / task / name
                if not path.exists():
                    continue
                text = path.read_text(encoding='utf-8', errors='replace')
                note_key = f'{task}/{name}'
                h = runner.digest(text.encode('utf-8'))
                old = entry['notes'].get(note_key) or {}
                if old.get('hash') == h:
                    continue
                note = {'body': f'### {title} — `{task}`\n\n' + text[:60000]}
                if old.get('id'):
                    gitlab('PUT', f"projects/{pid}/issues/{entry['iid']}/notes/{old['id']}", note)
                else:
                    old['id'] = gitlab('POST', f"projects/{pid}/issues/{entry['iid']}/notes", note)['id']
                old['hash'] = h
                entry['notes'][note_key] = old
                report.append(('documento', entry['url'], note_key))
        state['cards'][card['shortLink']] = entry
        runner.save(STATE, state)  # a cada card: falha no meio nao duplica issue
    state['last_sync'] = time.time()
    if not dry_run:
        runner.save(STATE, state)
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('cmd', choices=['sync'])
    p.add_argument('--se-passou', type=int, default=0, help='so sincroniza se a ultima foi ha mais de N segundos')
    p.add_argument('--dry-run', action='store_true')
    a = p.parse_args(argv)
    if a.se_passou:
        if not KEY_FILE.exists():
            return 0  # sem chave: o timer nao faz nada
        last = (runner.read(STATE).get('last_sync') or 0) if STATE.exists() else 0
        if time.time() - last < a.se_passou:
            return 0
    for line in sync(a.dry_run):
        print(' | '.join(str(x) for x in line))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
