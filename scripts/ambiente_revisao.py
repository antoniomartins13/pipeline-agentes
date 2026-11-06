#!/usr/bin/env python3
"""Ambiente de revisao de uma funcionalidade pronta para concluir (decisao do dono, 04/11/2026).

Quando todas as tarefas de uma entrega (ou a tarefa sem entrega) chegam em ready_for_draft, o
runner chama `preparar`, que deixa a funcionalidade esperando o dono com:

- MR em draft por repositorio (CP3 da POLITICA: draft depois da validacao, sem novo gate). Commit
  so na branch da tarefa, e so se a arvore e a mesma que o runner validou; push sem force. A
  descricao leva so o resumo da documentacao tecnica e o card (o parecer da revisao e privado).
- Servidor dev por repositorio (back: artisan serve; front: vite; portal: next dev), como unidades
  do systemd do usuario, publicados SO na tailnet (tailscale serve, HTTPS). O front e o portal
  apontam para o back da mesma funcionalidade.
- Roteiro de teste em tarefas/<alvo>/roteiro-teste.md: links, migrations a aplicar e a secao
  "Roteiro de teste manual" da documentacao de cada tarefa.

Nunca toca o banco: migration nova so e listada, com o comando, para o dono aprovar (POLITICA
item 8). `encerrar` (chamado no CONCLUIR) para os servidores e tira da tailnet; o MR fica.

Uso: ambiente_revisao.py preparar|encerrar|status <entrega-ou-tarefa>
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pipeline as runner  # noqa: E402

ROOT = runner.ROOT
STATE_DIR = ROOT / '.local' / 'ambientes'
# GitLab dos MRs e arquivo com GITLAB_TOKEN=... (config/local.json; sem arquivo vale o login do glab).
GITLAB_HOST = runner.settings().get('gitlab_host') or 'gitlab.com'
GITLAB_TOKEN_FILE = runner.settings().get('gitlab_token_file')
SHARED_BRANCHES = {'homologacao', 'main', 'master', 'develop', 'production', 'producao'}
# Portas: cada alvo ganha um bloco de 10 a partir daqui; local = bloco+k, tailnet = bloco+5+k.
PORT_BASE = 18000
KINDS = ('back', 'front', 'portal')
TASK_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def members(alvo):
    """Tarefas da entrega `alvo`; sem entrega com esse nome, a propria tarefa."""
    found = sorted(p.parent.name for p in (ROOT / 'tarefas').glob('*/task.json')
                   if runner.read(p).get('entrega') == alvo)
    if found:
        return found
    if (ROOT / 'tarefas' / alvo / 'task.json').exists():
        return [alvo]
    raise ValueError('Entrega/tarefa desconhecida: ' + alvo)


def phase_of(task):
    path = ROOT / '.local' / 'tasks' / task / 'state.json'
    return runner.read(path).get('phase') if path.exists() else None


def kind_of(repo):
    repo = Path(repo)
    if (repo / 'artisan').exists():
        return 'back'
    pkg = repo / 'package.json'
    if pkg.exists():
        deps = json.loads(pkg.read_text(encoding='utf-8'))
        deps = {**deps.get('dependencies', {}), **deps.get('devDependencies', {})}
        if 'next' in deps:
            return 'portal'
        if 'vite' in deps:
            return 'front'
    return None


def tailnet_host():
    try:
        out = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=15)
        name = json.loads(out.stdout)['Self']['DNSName'].rstrip('.')
        return name or None
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return None


def ports_for(alvo):
    """Bloco de portas fixo por alvo (registrado em portas.json; blocos de alvos encerrados voltam)."""
    path = STATE_DIR / 'portas.json'
    table = runner.read(path) if path.exists() else {}
    if alvo not in table:
        used = set(table.values())
        block = next(PORT_BASE + 10 * i for i in range(1, 500) if PORT_BASE + 10 * i not in used)
        table[alvo] = block
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        runner.save(path, table)
    block = table[alvo]
    return {k: (block + i, block + 5 + i) for i, k in enumerate(KINDS)}


def free_ports(alvo):
    path = STATE_DIR / 'portas.json'
    if path.exists():
        table = runner.read(path)
        if table.pop(alvo, None) is not None:
            runner.save(path, table)


def gitlab_env():
    env = os.environ.copy()
    env['GITLAB_HOST'] = GITLAB_HOST
    token_file = Path(GITLAB_TOKEN_FILE).expanduser() if GITLAB_TOKEN_FILE else None
    if token_file and token_file.exists() and not env.get('GITLAB_TOKEN'):
        for line in token_file.read_text(encoding='utf-8').splitlines():
            if line.startswith('GITLAB_TOKEN='):
                env['GITLAB_TOKEN'] = line.split('=', 1)[1].strip().strip('"\'')
    return env


def gitlab_project(repo):
    url = runner.git(repo, 'remote', 'get-url', 'origin')
    m = re.search(re.escape(GITLAB_HOST) + r'[:/](.+?)(?:\.git)?$', url)
    if not m:
        raise RuntimeError('origin nao e do GitLab configurado (' + GITLAB_HOST + ')')
    return m.group(1)


def spec_title(task):
    for line in (ROOT / 'tarefas' / task / 'task-spec.md').read_text(encoding='utf-8').splitlines():
        if line.startswith('# '):
            return line[2:].strip()[:200]
    return task


# Titulos aceitos, na ordem de preferencia (o docs novo usa os primeiros; os antigos variavam).
RESUMO = ('Resumo', 'O que mudou', 'Descrição do MR', 'Visão Geral', 'Comportamento Entregue')
ROTEIRO = ('Roteiro de teste manual', 'Verificação manual', 'Guia de Operação')


def section(text, *titles):
    """Conteudo da primeira secao '## [N.] <titulo>...' encontrada (ate a proxima '## '), ou ''."""
    for title in titles:
        m = re.search(r'^##\s+(?:\d+\.\s*)?' + re.escape(title) + r'[^\n]*\n(.*?)(?=^##\s|\Z)', text, re.M | re.S | re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ''


def mr_description(task):
    """So o resumo (decisao do dono): o que mudou + card. Nunca o parecer da revisao."""
    docs = ROOT / 'tarefas' / task / 'documentacao-tecnica.md'
    text = docs.read_text(encoding='utf-8') if docs.exists() else ''
    resumo = section(text, *RESUMO)
    if len(resumo) > 3000:
        resumo = resumo[:3000].rsplit('\n', 1)[0] + '\n\n(continua na documentação técnica da tarefa)'
    spec = (ROOT / 'tarefas' / task / 'task-spec.md').read_text(encoding='utf-8')
    card = re.search(r'https://trello\.com/c/[A-Za-z0-9]+[^\s)>\]]*', spec)
    parts = [resumo or spec_title(task)]
    if card:
        parts.append('Card: ' + card.group(0))
    return '\n\n'.join(parts)


def ensure_mr(task, manifest, state, repo):
    """Commit (se preciso) + push + MR draft. Idempotente: reaproveita MR aberto da branch."""
    branch = manifest['branch']
    target = manifest['base_ref'].split('/', 1)[1] if manifest['base_ref'].startswith('origin/') else manifest['base_ref']
    if branch in SHARED_BRANCHES or branch == target:
        raise RuntimeError(f'Branch {branch} e compartilhada; MR nao aberto')
    if runner.git(repo, 'branch', '--show-current') != branch:
        raise RuntimeError(f'Worktree fora da branch da tarefa ({branch})')
    if runner.git(repo, 'status', '--porcelain'):
        if runner.fingerprint(repo) != state.get('validated_tree'):
            raise RuntimeError('Arvore mudou depois da validacao do runner; nada commitado')
        runner.git(repo, 'add', '-A')
        subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', spec_title(task),
                        '-m', f'Tarefa {task} do pipeline (validada pelo runner).'], check=True,
                       capture_output=True, text=True)
    push = subprocess.run(['git', '-C', str(repo), 'push', '-q', '-u', 'origin', branch],
                          capture_output=True, text=True, timeout=180)
    if push.returncode:
        raise RuntimeError('push falhou: ' + (push.stderr or push.stdout).strip()[-300:])
    project, env = gitlab_project(repo), gitlab_env()
    listed = subprocess.run(['glab', 'mr', 'list', '-R', project, '--source-branch', branch, '-F', 'json'],
                            capture_output=True, text=True, env=env, timeout=60, cwd=repo)
    try:
        existing = json.loads(listed.stdout or '[]')
    except ValueError:
        existing = []
    if existing:
        mr = existing[0]
        return {'url': mr.get('web_url'), 'iid': mr.get('iid'), 'novo': False}
    created = subprocess.run(['glab', 'mr', 'create', '-R', project, '--source-branch', branch,
                              '--target-branch', target, '--draft', '--title', spec_title(task),
                              '--description', mr_description(task), '--yes'],
                             capture_output=True, text=True, env=env, timeout=120, cwd=repo)
    url = re.search(r'https://\S+/merge_requests/(\d+)', created.stdout + created.stderr)
    if created.returncode or not url:
        raise RuntimeError('glab mr create falhou: ' + (created.stderr or created.stdout).strip()[-300:])
    return {'url': url.group(0), 'iid': int(url.group(1)), 'novo': True}


# (MemoryHigh, MemoryMax, heap do Node em MB) por tipo de servidor.
SERVER_MEMORY = {'back': ('512M', '768M', 0), 'front': ('1536M', '2G', 1536), 'portal': ('1200M', '1536M', 1024)}


def unit_name(alvo, kind):
    return f'revisao-{alvo}-{kind}'


def unit_active(unit):
    return subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit]).returncode == 0


def start_server(alvo, kind, repo, port, env_extra):
    unit = unit_name(alvo, kind)
    if unit_active(unit):
        return unit
    exe = {'back': [shutil.which('php') or 'php', 'artisan', 'serve', '--host=127.0.0.1', f'--port={port}'],
           'front': [shutil.which('npm') or 'npm', 'run', 'dev', '--', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
           'portal': [shutil.which('npx') or 'npx', 'next', 'dev', '-H', '127.0.0.1', '-p', str(port)]}[kind]
    setenv = [f'--setenv=PATH={os.environ.get("PATH", "")}', f'--setenv=HOME={Path.home()}']
    setenv += [f'--setenv={k}={v}' for k, v in env_extra.items()]
    # Teto por servidor (04/11/2026: next dev + vite + build do pipeline travaram a maquina, com swap
    # de 512 MB). MemoryHigh freia, MemoryMax mata so este servidor; o heap do Node fica abaixo.
    high, top, heap = SERVER_MEMORY[kind]
    if kind != 'back':
        setenv.append(f'--setenv=NODE_OPTIONS=--max-old-space-size={heap}')
    subprocess.run(['systemd-run', '--user', '--quiet', '--collect', f'--unit={unit}',
                    '-p', f'MemoryHigh={high}', '-p', f'MemoryMax={top}', '-p', 'MemorySwapMax=0',
                    f'--working-directory={repo}', *setenv, *exe], check=True, capture_output=True, text=True)
    return unit


def publish(local_port, public_port):
    """So na tailnet (tailscale serve, nunca funnel)."""
    subprocess.run(['tailscale', 'serve', '--bg', f'--https={public_port}', f'http://127.0.0.1:{local_port}'],
                   check=True, capture_output=True, text=True, timeout=30)


def new_migrations(repo, manifest):
    out = runner.git(repo, 'diff', '--name-only', '--diff-filter=A', manifest['base_sha'], 'HEAD', '--', 'database/migrations')
    status = runner.git(repo, 'status', '--porcelain', '--', 'database/migrations')
    files = [l for l in out.splitlines() if l.endswith('.php')]
    files += [l[3:] for l in status.splitlines() if l.startswith('??') and l.endswith('.php')]
    return sorted(set(files))


# SQL que nunca e aplicado sem o dono olhar: apaga ou reescreve dado/estrutura.
DESTRUTIVO = re.compile(r'\b(drop|truncate|delete\s+from|rename)\b|\bupdate\s+\S+\s+set\b(?![\s\S]*\bwhere\b)', re.I)


def env_value(repo, key):
    env = Path(repo) / '.env'
    if not env.exists():
        return None
    for line in env.read_text(encoding='utf-8', errors='replace').splitlines():
        if line.startswith(key + '='):
            return line.split('=', 1)[1].strip().strip('"\'')
    return None


def apply_migrations(repo):
    """Aplica as migrations pendentes no banco LOCAL de revisao (decisao do dono, 04/11/2026).

    So com .env em banco local e APP_ENV=local; gera o SQL com --pretend e recusa se houver algo
    destrutivo (DESTRUTIVO). Sem --force: em producao o Laravel nao rodaria sem confirmacao.
    """
    host, app_env = env_value(repo, 'DB_HOST'), env_value(repo, 'APP_ENV')
    banco = f"{host}:{env_value(repo, 'DB_PORT')}/{env_value(repo, 'DB_DATABASE')}"
    if host not in ('127.0.0.1', 'localhost') or app_env != 'local':
        return {'status': 'barrada', 'banco': banco, 'motivo': f'banco nao e local (DB_HOST={host}, APP_ENV={app_env})'}
    php = shutil.which('php') or 'php'
    pretend = subprocess.run([php, 'artisan', 'migrate', '--pretend', '--no-ansi'], cwd=repo, capture_output=True,
                             text=True, timeout=300, stdin=subprocess.DEVNULL)
    if pretend.returncode:
        return {'status': 'barrada', 'banco': banco, 'motivo': 'migrate --pretend falhou: ' + (pretend.stderr or pretend.stdout).strip()[-300:]}
    sql = [line.strip().lstrip('⇂').strip() for line in pretend.stdout.splitlines() if '⇂' in line]
    if not sql:
        return {'status': 'nada', 'banco': banco, 'sql': []}
    perigoso = [s for s in sql if DESTRUTIVO.search(s)]
    if perigoso:
        return {'status': 'barrada', 'banco': banco, 'sql': sql, 'motivo': 'SQL destrutivo: ' + '; '.join(perigoso)[:300]}
    run = subprocess.run([php, 'artisan', 'migrate', '--no-ansi'], cwd=repo, capture_output=True, text=True,
                         timeout=600, stdin=subprocess.DEVNULL)
    if run.returncode:
        return {'status': 'erro', 'banco': banco, 'sql': sql, 'motivo': (run.stderr or run.stdout).strip()[-400:]}
    return {'status': 'aplicadas', 'banco': banco, 'sql': sql}


def write_roteiro(alvo, info):
    lines = [f'# Roteiro de teste — {alvo}', '']
    lines.append('## Onde abrir')
    for t in info['tarefas']:
        srv = t.get('servidor') or {}
        mr = t.get('mr') or {}
        lines.append(f"- **{t['task']}** ({t.get('kind') or '?'}): "
                     + (f"[servidor]({srv['url']})" if srv.get('url') else 'servidor: ' + str(srv.get('erro', '—')))
                     + ' · ' + (f"[MR !{mr['iid']}]({mr['url']})" if mr.get('url') else 'MR: ' + str(mr.get('erro', '—'))))
    mig = info.get('migracao') or {}
    if info.get('migrations') or mig.get('sql'):
        if mig.get('status') in ('aplicadas', 'nada'):
            lines += ['', '## Migrations', (f"Aplicadas automaticamente no banco local `{mig.get('banco')}`."
                                            if mig['status'] == 'aplicadas' else
                                            f"Já estavam aplicadas no banco local `{mig.get('banco')}`."), '']
        else:
            lines += ['', '## Antes de testar: migrations (precisam de você)',
                      f"Não apliquei: {mig.get('motivo', 'sem verificação')}. Comando, depois de olhar o SQL:", '',
                      '```', info.get('comando_migrate', ''), '```', '']
        lines += [f'- `{m}`' for m in info.get('migrations', [])]
        if mig.get('sql'):
            lines += ['', 'SQL:', '', '```sql', *mig['sql'], '```']
    for t in info['tarefas']:
        docs = ROOT / 'tarefas' / t['task'] / 'documentacao-tecnica.md'
        roteiro = section(docs.read_text(encoding='utf-8'), *ROTEIRO) if docs.exists() else ''
        lines += ['', f"## {t['task']}", roteiro or '_A documentação desta tarefa não trouxe roteiro de teste manual._']
    out = ROOT / 'tarefas' / alvo / 'roteiro-teste.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
    return str(out.relative_to(ROOT))


HABBLAUD = os.environ.get('HABBLAUD_URL', 'http://127.0.0.1:4747')


def coordinator_prompt(alvo, info):
    tasks = ', '.join(t['task'] for t in info['tarefas'])
    return (
        f'Você é a sessão coordenadora da funcionalidade {alvo} ({tasks}), que ficou pronta para {runner.DONO} concluir. '
        f'O runner já preparou o ambiente de revisão: leia {ROOT / info["roteiro"]} e {STATE_DIR / (alvo + ".json")}. '
        f'Na primeira resposta, entregue a {runner.DONO}, em português e curto: os links dos servidores (tailnet), os links dos MRs '
        '(draft), se as migrations foram aplicadas, e o roteiro de teste em passos numerados. '
        'Depois fique à disposição: tire dúvidas sobre o que mudou (documentacao-tecnica.md de cada tarefa em '
        f'{ROOT / "tarefas"} e o diff nos worktrees), confira os servidores com `systemctl --user status "revisao-{alvo}-*"` '
        f'e, se algum cair, suba de novo com `python3 {ROOT / "scripts" / "ambiente_revisao.py"} preparar {alvo}`. '
        'Regras: não edite código, não faça commit/push e não mexa no banco. Não registre CONCLUIR, CORRIGIR, RECUPERAR nem '
        f'APROVAR: quem decide é {runner.DONO}, pelo botão do Habblaud ou pelo console. Se ele pedir uma correção, escreva a linha '
        f'pronta `CORRIGIR <tarefa>: <o que mudar>` para ele mandar. Nunca leve o parecer da revisão para o MR.'
    )


def spawn_coordinator(alvo, info):
    """Abre a sessao coordenadora na pasta da funcionalidade (sala dela no Habblaud), uma vez por ambiente."""
    import urllib.request
    old = (info.get('coordenador') or {})
    if old.get('id') and not old.get('erro'):
        return old
    folder = ROOT / 'tarefas' / alvo
    body = json.dumps({'folder': str(folder), 'prompt': coordinator_prompt(alvo, info)}).encode('utf-8')
    req = urllib.request.Request(HABBLAUD + '/api/spawn', data=body, method='POST',
                                 headers={'Content-Type': 'application/json', 'Origin': 'http://localhost:4747'})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8') or '{}')
        return {'id': data.get('id'), 'at': runner.now(), 'pasta': str(folder)}
    except (OSError, ValueError) as error:
        return {'erro': 'Habblaud nao abriu a sessao coordenadora: ' + str(error)[:200], 'pasta': str(folder)}


def preparar(alvo):
    if not TASK_ID.match(alvo):
        raise ValueError('Alvo invalido')
    tasks = members(alvo)
    pending = [t for t in tasks if phase_of(t) != 'ready_for_draft']
    if pending:
        return {'alvo': alvo, 'aguardando': pending}
    host = tailnet_host()
    ports = ports_for(alvo)
    info = {'alvo': alvo, 'at': runner.now(), 'host': host, 'tarefas': [], 'migrations': [], 'erros': []}
    repos = {}
    for task in tasks:
        manifest = runner.read(ROOT / 'tarefas' / task / 'task.json')
        state = runner.read(ROOT / '.local' / 'tasks' / task / 'state.json')
        repo = runner.repository(manifest)
        kind = kind_of(repo)
        entry = {'task': task, 'kind': kind, 'repo': str(repo), 'branch': manifest.get('branch')}
        try:
            entry['mr'] = ensure_mr(task, manifest, state, repo)
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
            entry['mr'] = {'erro': str(error)[:300]}
        if kind == 'back':
            info['migrations'] += [f'{task}: {m}' for m in new_migrations(repo, manifest)]
        if kind:
            repos[kind] = (task, repo)
        info['tarefas'].append(entry)
    if 'back' in repos:
        info['comando_migrate'] = f"cd {repos['back'][1]} && php artisan migrate"
        try:
            info['migracao'] = apply_migrations(repos['back'][1])
        except (OSError, subprocess.SubprocessError) as error:
            info['migracao'] = {'status': 'erro', 'motivo': str(error)[:300]}
    back_url = f'https://{host}:{ports["back"][1]}' if host and 'back' in repos else 'http://127.0.0.1:8000'
    for entry in info['tarefas']:
        kind = entry['kind']
        if kind not in KINDS:
            entry['servidor'] = {'erro': 'tipo de projeto nao reconhecido'}
            continue
        local, public = ports[kind]
        env = {}
        if kind == 'front':
            env = {'VITE_API_URL': back_url + '/api', '__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS': host or ''}
        elif kind == 'portal':
            env = {'NEXT_PUBLIC_API_URL': back_url + '/api'}
        elif kind == 'back' and host:
            env = {'APP_URL': back_url}
        try:
            unit = start_server(alvo, kind, entry['repo'], local, env)
            if not host:
                raise RuntimeError('Tailscale indisponivel: servidor so em 127.0.0.1:' + str(local))
            publish(local, public)
            entry['servidor'] = {'url': f'https://{host}:{public}', 'local': f'http://127.0.0.1:{local}', 'unidade': unit}
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            msg = getattr(error, 'stderr', None) or str(error)
            entry['servidor'] = {'erro': str(msg).strip()[:300], 'local': f'http://127.0.0.1:{local}'}
    info['roteiro'] = write_roteiro(alvo, info)
    previous = STATE_DIR / (alvo + '.json')
    if previous.exists() and not runner.read(previous).get('encerrado_em'):
        info['coordenador'] = runner.read(previous).get('coordenador')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    runner.save(STATE_DIR / (alvo + '.json'), info)
    if not os.environ.get('PIPELINE_SEM_AMBIENTE'):
        info['coordenador'] = spawn_coordinator(alvo, info)
        runner.save(STATE_DIR / (alvo + '.json'), info)
    return info


def encerrar(alvo):
    if not TASK_ID.match(alvo):
        raise ValueError('Alvo invalido')
    path = STATE_DIR / (alvo + '.json')
    info = runner.read(path) if path.exists() else {'tarefas': []}
    ports = ports_for(alvo)
    for kind in KINDS:
        subprocess.run(['systemctl', '--user', 'stop', unit_name(alvo, kind)], capture_output=True)
        subprocess.run(['tailscale', 'serve', f'--https={ports[kind][1]}', 'off'], capture_output=True, timeout=30)
    free_ports(alvo)
    if path.exists():
        info['encerrado_em'] = runner.now()
        runner.save(path, info)
    return {'alvo': alvo, 'encerrado': True}


def alvo_de(task):
    manifest = runner.read(ROOT / 'tarefas' / task / 'task.json')
    return manifest.get('entrega') or task


def main(argv):
    if len(argv) != 3 or argv[1] not in ('preparar', 'encerrar', 'status'):
        print(__doc__)
        return 2
    cmd, alvo = argv[1], argv[2]
    if cmd == 'status':
        path = STATE_DIR / (alvo + '.json')
        print(path.read_text(encoding='utf-8') if path.exists() else '{}')
        return 0
    result = preparar(alvo) if cmd == 'preparar' else encerrar(alvo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
