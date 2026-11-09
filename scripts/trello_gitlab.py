#!/usr/bin/env python3
"""Espelho Trello -> GitLab (decisao do dono, 06/11/2026). O Trello continua sendo a fonte; o GitLab e o espelho.

- Cards do dono (membro do card) no board configurado viram uma issue cada no projeto de gestao, com link do card,
  descricao e checklists. A label fase:: segue a lista do Trello; card em lista de concluido fecha a issue.
- Tarefas do pipeline ligadas ao card (link ou numero do card): revisao de colega (pasta mr-*) ganha papel::revisao
  e NADA anexado; desenvolvimento do dono ganha papel::dev e a issue ganha a label pipeline e repo-*, e uma nota por documento
  (task-spec.md, plan.md, documentacao-tecnica.md), atualizada quando o arquivo muda. O parecer da revisao
  (review-report.md) e privado e NUNCA vai.
- Revisao em "Em Fase de Teste": sobe servidor e roteiro (ambiente_revisao.py revisao); saiu da coluna, derruba.
- Nunca apaga issue nem nota; nunca escreve no Trello.

Config (config/local.json): "trello_board" (shortLink do board), "gestao_project" (caminho no GitLab),
"gitlab_host"; "gestao_sync": true liga a sincronizacao pelo timer (sem ele, so manual). Chave: ~/.config/pipeline/trello.env com TRELLO_KEY=... e TRELLO_TOKEN=... (criada pelo dono;
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
import urllib.error
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


def issue_body(card, list_name, revisao=False, mrs=()):
    lines = [f"**Card:** {card['shortUrl']}  ·  **Lista no Trello:** {list_name}"]
    if revisao:
        lines.append('**Papel:** revisão de MR de colega. O parecer fica no pipeline e é privado: não vem para cá.')
    if mrs:
        lines.append('**MRs:** ' + ' · '.join(f"[{m['repo']} !{m['iid']}]({m['url']}) ({m['estado']})" for m in mrs))
    lines += ['', (card.get('desc') or '').strip()]
    for checklist in card.get('checklists') or []:
        lines += ['', f"**{checklist.get('name') or 'Checklist'}**"]
        for item in sorted(checklist.get('checkItems') or [], key=lambda i: i.get('pos', 0)):
            lines.append(f"- [{'x' if item.get('state') == 'complete' else ' '}] {item.get('name', '').strip()}")
    lines += ['', '_Espelho do Trello (scripts/trello_gitlab.py). Edite no Trello; aqui é sobrescrito._',
              f"<!-- trello:{card['shortLink']} -->"]
    return '\n'.join(lines).strip() + '\n'


def manifest_of(task):
    path = ROOT / 'tarefas' / task / 'task.json'
    return runner.read(path) if path.exists() else {}


def card_short(url):
    m = re.search(r'trello\.com/c/([A-Za-z0-9]+)', url or '')
    return m.group(1) if m else None


def declared_owners():
    """card (shortLink) -> {'tarefas': [...], 'origem': ...}: a tabela gestao_cards de config/local.json (excecoes das
    tarefas antigas, vence tudo) mais o campo "card"/"origem" do task.json das tarefas novas (/iniciar --card)."""
    owners = {}
    for folder in sorted(p for p in (ROOT / 'tarefas').iterdir() if p.is_dir()):
        manifest = manifest_of(folder.name)
        short = card_short(manifest.get('card'))
        if short:
            entry = owners.setdefault(short, {'tarefas': [], 'origem': None})
            entry['tarefas'].append(folder.name)
            entry['origem'] = entry['origem'] or manifest.get('origem')
    for short, override in (runner.settings().get('gestao_cards') or {}).items():
        owners[short] = {'tarefas': list(override.get('tarefas') or []), 'origem': override.get('origem'),
                         'papel': override.get('papel', 'dev')}
    return owners


def tasks_for_card(card, owners=None):
    """(tarefas ligadas ao card [(tarefa, manifesto, papel)], origem). Declarada (task.json "card" ou gestao_cards)
    vence: o card e desenvolvimento do dono. Sem declaracao, deduz: link ou numero do card no nome/.md de uma pasta;
    pasta mr-* e revisao de colega. Tarefa declarada para outro card nao conta por mencao."""
    owners = declared_owners() if owners is None else owners
    short, number = card.get('shortLink', ''), str(card.get('idShort') or '')
    if short in owners:
        own = owners[short]
        papel = own.get('papel', 'dev')
        return [(t, manifest_of(t), papel) for t in own['tarefas']], own.get('origem')
    claimed = {t for own in owners.values() for t in own['tarefas']}
    num = re.compile(r'(?<!\d)' + re.escape(number) + r'(?!\d)') if number else None
    found = []
    for folder in sorted(p for p in (ROOT / 'tarefas').iterdir() if p.is_dir()):
        if folder.name in claimed:
            continue
        texts = [folder.name] + [f.read_text(encoding='utf-8', errors='replace') for f in sorted(folder.glob('*.md'))]
        blob = '\n'.join(texts)
        if (short and f'trello.com/c/{short}' in blob) or (num and num.search(blob)):
            papel = 'revisao' if folder.name.startswith('mr-') else 'dev'
            found.append((folder.name, manifest_of(folder.name), papel))
    return found, None


MR_STATE = {'opened': 'aberto', 'merged': 'mergeado', 'closed': 'fechado', 'locked': 'travado'}


def review_mrs(task, cache):
    """MRs de uma revisao (pasta mr-*): numeros do nome da pasta e referencias !NNN dos .md dela, conferidos no GitLab
    (projetos de gestao_mr_projects). Fica o par back/front que compartilha branch; numero do nome da pasta sem par
    so entra se existir em um projeto so. Da pasta sai so o numero: nenhum conteudo da revisao. Cache de 1 hora."""
    projects = runner.settings().get('gestao_mr_projects') or []
    folder = ROOT / 'tarefas' / task
    if not projects or not folder.is_dir():
        return []
    from_name = set(re.findall(r'(?<!\d)\d{2,3}(?!\d)', task))
    text = '\n'.join(f.read_text(encoding='utf-8', errors='replace') for f in sorted(folder.glob('*.md')))
    numbers = sorted(from_name | set(re.findall(r'!(\d{2,4})\b', text)), key=int)
    key = ','.join(numbers)
    hit = cache.get(task) or {}
    # MR aberto pode ser mergeado a qualquer momento (o merge encerra a revisao): cache curto enquanto houver aberto.
    ttl = 600 if any(m.get('estado') == 'aberto' for m in hit.get('mrs') or []) else 3600
    if hit.get('key') == key and time.time() - hit.get('at', 0) < ttl and all('branch' in m for m in hit['mrs']):
        return hit['mrs']
    found = []
    for number in numbers:
        for project in projects:
            try:
                mr = gitlab('GET', f"projects/{urllib.parse.quote(project, safe='')}/merge_requests/{number}")
            except RuntimeError:
                continue
            if isinstance(mr, dict) and mr.get('iid'):
                found.append({'n': number, 'repo': project.rsplit('/', 1)[-1], 'iid': mr['iid'], 'url': mr['web_url'],
                              'estado': MR_STATE.get(mr.get('state'), mr.get('state')), 'branch': mr.get('source_branch')})
    branches = {}
    for mr in found:
        branches.setdefault(mr['branch'], set()).add(mr['n'])
    keep = [mr for mr in found if len(branches[mr['branch']]) >= 2]
    for number in from_name:
        alone = [mr for mr in found if mr['n'] == number]
        if len(alone) == 1 and alone[0] not in keep:
            keep.append(alone[0])
    order = [project.rsplit('/', 1)[-1] for project in projects]
    keep.sort(key=lambda mr: (mr['estado'] != 'aberto', order.index(mr['repo']), -mr['iid']))
    # MR fechado (substituido por um consolidado, desistido) nao e o que revisar: so abertos e mergeados.
    mrs = [{k: mr[k] for k in ('repo', 'iid', 'url', 'estado', 'branch')} for mr in keep if mr['estado'] != 'fechado']
    cache[task] = {'key': key, 'at': time.time(), 'mrs': mrs}
    return mrs


def mr_links():
    """tarefa -> URL do MR, dos ambientes de revisao (.local/ambientes/*.json)."""
    links = {}
    for path in sorted((ROOT / '.local' / 'ambientes').glob('*.json')):
        try:
            data = runner.read(path)
        except (OSError, ValueError):
            continue
        for item in data.get('tarefas') or [] if isinstance(data, dict) else []:
            url = (item.get('mr') or {}).get('url')
            if url:
                links[item['task']] = url
    return links


def card_report(card, tasks):
    """Relatorio do que o dono mudou no card: resumo de cada tarefa (documentacao-tecnica.md) e o MR."""
    import ambiente_revisao as amb
    links = mr_links()
    lines = [f"# Relatório de mudanças — {card['name']}", '', f"Card: {card['shortUrl']}", '']
    for task, _, papel in tasks:
        if papel != 'dev':
            continue
        folder = ROOT / 'tarefas' / task
        docs = folder / 'documentacao-tecnica.md'
        resumo = amb.section(docs.read_text(encoding='utf-8'), *amb.RESUMO) if docs.exists() else ''
        spec = folder / 'task-spec.md'
        heads = [l[2:].strip() for l in spec.read_text(encoding='utf-8').splitlines() if l.startswith('# ')] if spec.exists() else []
        title = heads[0][:200] if heads else task
        lines += [f'## {title}', f"`{task}`" + (f" · MR: {links[task]}" if task in links else ' · MR: ainda não aberto'), '',
                  resumo or '_Ainda sem resumo (a documentação sai no fim do pipeline)._', '']
    return '\n'.join(lines).rstrip() + '\n'


def repo_label(manifest):
    repo = manifest.get('repo', '')
    if repo.startswith('erp-back'):
        return 'repo-back-end'
    if repo.startswith('erp-front'):
        return 'repo-front-end'
    if repo.startswith('transparencia'):
        return 'repo-portal'
    return None


# Fase do runner nas issues de desenvolvimento (independe da lista do Trello): CORRIGIR volta a tarefa para dev e a
# issue mostra pipeline::dev mesmo com o card parado em "Em Fase de Teste". Varias tarefas: vale a mais atrasada,
# e qualquer bloqueio aparece.
PIPELINE_FASE = {'awaiting_CP0': 'aguardando-cp1', 'awaiting_CP1': 'aguardando-cp1', 'awaiting_PROTOTIPO': 'aguardando-cp1',
                 'plan': 'dev', 'prototype': 'dev', 'dev': 'dev', 'validate': 'dev', 'review': 'revisao', 'docs': 'docs',
                 'ready_for_draft': 'pronto', 'closed': 'concluido', 'blocked': 'bloqueado'}
PIPELINE_ORDEM = ('aguardando-cp1', 'dev', 'revisao', 'docs', 'pronto', 'concluido')


def pipeline_fase(tasks):
    fases = []
    for task, _, papel in tasks:
        path = ROOT / '.local' / 'tasks' / task / 'state.json'
        if papel == 'dev' and path.exists():
            fases.append(PIPELINE_FASE.get(runner.read(path).get('phase'), 'dev'))
    if not fases:
        return None
    if 'bloqueado' in fases:
        return 'bloqueado'
    return min(fases, key=PIPELINE_ORDEM.index)


def labels_for(card, list_name, tasks, mrs=()):
    labels = {'fase::' + fase_of(list_name)}
    if plain(list_name).startswith('bugs'):
        labels.add('tipo::bug')
    revisao = any(t[2] == 'revisao' for t in tasks)
    dev = [] if revisao else [t for t in tasks if t[2] == 'dev']  # revisao ligada: tarefa dev so cita o card
    labels.add('papel::revisao' if revisao else 'papel::dev')
    if dev:
        labels.add('pipeline')
        fase_pipeline = pipeline_fase(dev)
        if fase_pipeline:
            labels.add('pipeline::' + fase_pipeline)
    labels |= {lab for _, m, _ in dev if (lab := repo_label(m))}
    labels |= {'repo-' + ('portal' if m['repo'] == 'transparencia' else m['repo']) for m in mrs}
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


def mem_available_mb():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


AMBIENTE_MIN_MB = 3072


def ambiente_teste(task, em_teste):
    """Revisao em "Em Fase de Teste" tem servidor e roteiro (decisao do dono, 06/11/2026); saiu da coluna, derruba.
    Roda o ambiente_revisao.py em segundo plano (fetch, migrations e servidores demoram)."""
    state_path = ROOT / '.local' / 'ambientes' / (task + '.json')
    ambiente = runner.read(state_path) if state_path.exists() else {}
    no_ar = bool(ambiente) and not ambiente.get('encerrado_em')
    if em_teste == no_ar:
        return None
    if em_teste and mem_available_mb() < AMBIENTE_MIN_MB:
        return ('ambiente adiado (memoria)', task)
    log = open(ROOT / '.local' / 'ambientes' / (task + '.log'), 'a', encoding='utf-8')
    subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'ambiente_revisao.py'), 'revisao' if em_teste else 'encerrar', task],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ROOT))
    return ('ambiente ' + ('subindo' if em_teste else 'encerrado'), task)


def ensure_titulo(task, card_name):
    """Nome da sala no Habblaud (tarefas/<pasta>/titulo.txt): o do card, so se ainda nao existir (o dono pode editar).
    Tarefa de entrega grava na pasta da entrega."""
    manifest = manifest_of(task)
    folder = ROOT / 'tarefas' / (manifest.get('entrega') or task)
    path = folder / 'titulo.txt'
    if folder.is_dir() and not path.exists():
        path.write_text(card_name.strip()[:120] + '\n', encoding='utf-8')
        return True
    return False


def sync(dry_run=False):
    cfg = runner.settings()
    board, project = cfg.get('trello_board'), cfg.get('gestao_project')
    if not board or not project:
        raise RuntimeError('configure "trello_board" e "gestao_project" em config/local.json')
    pid = urllib.parse.quote(project, safe='')
    state = runner.read(STATE) if STATE.exists() else {'cards': {}}
    me = trello_get('members/me', {'fields': 'id'})['id']
    lists = {l['id']: l['name'] for l in trello_get(f'boards/{board}/lists', {'fields': 'name', 'filter': 'all'})}
    cards = trello_get(f'boards/{board}/cards/all', {'fields': 'name,desc,idList,idMembers,shortUrl,shortLink,idShort,closed',
                                                      'checklists': 'all'})
    report = []
    owners = declared_owners()
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
        tasks, origem = tasks_for_card(card, owners)
        revisao = any(t[2] == 'revisao' for t in tasks)
        mrs = []
        if revisao and not dry_run:
            cache = state.setdefault('mr_cache', {})
            for task, _, papel in tasks:
                if papel == 'revisao':
                    mrs += [m for m in review_mrs(task, cache) if m not in mrs]
        body = {'title': card['name'][:250], 'description': issue_body(card, list_name, revisao, mrs),
                'labels': ','.join(labels_for(card, list_name, tasks, mrs))}
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
        for task, _, papel in tasks:
            if revisao or papel != 'dev':
                continue  # revisao de colega: parecer, HANDOFF e relatorio para o dev sao privados
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
                state['cards'][card['shortLink']] = entry
                runner.save(STATE, state)  # nota a nota: falha no meio nao duplica na proxima rodada
                report.append(('documento', entry['url'], note_key))
        if not dry_run:
            for task, _, papel in tasks:
                if (papel == 'revisao') == revisao and ensure_titulo(task, card['name']):
                    report.append(('titulo', task, card['name'][:60]))
        if revisao and not dry_run:
            (ROOT / '.local' / 'ambientes').mkdir(parents=True, exist_ok=True)
            # Revisao de colega nao tem CONCLUIR: o merge e a aprovacao. Tudo mergeado -> derruba o ambiente,
            # mesmo com o card ainda em "Em Fase de Teste".
            mergeado = bool(mrs) and all(m['estado'] == 'mergeado' for m in mrs)
            for task, _, papel in tasks:
                if papel == 'revisao' and task.startswith('mr-'):
                    acao = ambiente_teste(task, fase == 'teste' and not mergeado)
                    if acao:
                        report.append(acao)
                    break
        if origem and not revisao and not entry.get('origem_note'):
            note = {'body': f'Assumido a partir da revisão `{origem}` (continuação da branch de outro dev). '
                            'O parecer daquela revisão é privado e fica no pipeline.'}
            entry['origem_note'] = gitlab('POST', f"projects/{pid}/issues/{entry['iid']}/notes", note)['id']
            state['cards'][card['shortLink']] = entry
            runner.save(STATE, state)
            report.append(('origem', entry['url'], origem))
        if not revisao and any(t[2] == 'dev' for t in tasks):
            text = card_report(card, tasks)
            out = ROOT / 'relatorios' / 'cards' / f"{card.get('idShort') or card['shortLink']}-{card['shortLink']}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
            h = runner.digest(text.encode('utf-8'))
            old = entry.get('relatorio') or {}
            if old.get('hash') != h:
                note = {'body': text}
                if old.get('id'):
                    gitlab('PUT', f"projects/{pid}/issues/{entry['iid']}/notes/{old['id']}", note)
                else:
                    old['id'] = gitlab('POST', f"projects/{pid}/issues/{entry['iid']}/notes", note)['id']
                old['hash'] = h
                entry['relatorio'] = old
                report.append(('relatorio', entry['url'], str(out.relative_to(ROOT))))
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
        # Chamada do timer: so com a chave E a ativacao explicita do dono ("gestao_sync": true em config/local.json).
        if not KEY_FILE.exists() or runner.settings().get('gestao_sync') is not True:
            return 0
        last = (runner.read(STATE).get('last_sync') or 0) if STATE.exists() else 0
        if time.time() - last < a.se_passou:
            return 0
    try:
        for line in sync(a.dry_run):
            print(' | '.join(str(x) for x in line))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
        # Rede fora (ex.: proxy do cabo com certificado proprio): uma linha no log e tenta no proximo ciclo.
        # Nunca desligar a verificacao de certificado para contornar.
        print(f'{runner.now()} | rede | Trello/GitLab inacessivel, tento no proximo ciclo: {str(error)[:160]}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
