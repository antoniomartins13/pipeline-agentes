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

Uso: ambiente_revisao.py preparar|encerrar|status <entrega-ou-tarefa>  |  revisao <pasta mr-* de revisao de colega>
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
    # GitLab interno nao passa pelo proxy corporativo (o proxy dava timeout nas chamadas da API).
    for key in ('NO_PROXY', 'no_proxy'):
        env[key] = ','.join(x for x in (GITLAB_HOST, env.get(key, '')) if x)
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


# Seed de dados de exemplo: so cria/atualiza registros proprios. Qualquer coisa que apague, reescreva em massa,
# mexa em estrutura ou chame o shell barra o seed inteiro.
SEED_PERIGOSO = re.compile(
    r'->\s*(delete|forceDelete|truncate)\s*\(|::\s*(truncate|destroy|query\(\)\s*->\s*delete)\b|'
    r'DB::\s*(statement|unprepared|delete|update|raw)\b|Schema::|\b(drop|truncate)\s+table\b|'
    r'\b(shell_exec|exec|system|passthru|proc_open|popen|unlink|rmdir)\s*\(|`', re.I)


def apply_seed(alvo, repo):
    """Roda .revisao/seed.php (escrito pelo dev do back, fora do MR) no banco LOCAL de revisao, uma vez por versao."""
    seed = Path(repo) / runner.REVISAO_DIR / 'seed.php'
    if not seed.exists():
        return {'status': 'sem_seed'}
    host, app_env = env_value(repo, 'DB_HOST'), env_value(repo, 'APP_ENV')
    if host not in ('127.0.0.1', 'localhost') or app_env != 'local':
        return {'status': 'barrado', 'motivo': f'banco nao e local (DB_HOST={host}, APP_ENV={app_env})'}
    code = seed.read_text(encoding='utf-8', errors='replace')
    perigoso = sorted({m.group(0).strip() for m in SEED_PERIGOSO.finditer(code)})
    if perigoso:
        return {'status': 'barrado', 'motivo': 'seed com operacao proibida: ' + ', '.join(perigoso)[:200]}
    # 'v2': o seed sem `<?php` era so impresso pelo require, sem rodar; a versao muda o hash para reaplicar.
    digest = runner.digest(b'seed-v2\n' + code.encode('utf-8'))
    previous = STATE_DIR / (alvo + '.json')
    old = (runner.read(previous).get('seed') or {}) if previous.exists() else {}
    if old.get('status') == 'aplicado' and old.get('hash') == digest:
        return old
    body = code.lstrip()
    if body.startswith('<?php'):
        body = body[5:]
    # Arquivo de execucao sempre com a tag de abertura: sem ela o PHP imprime o texto em vez de executar.
    runfile = Path(repo) / runner.REVISAO_DIR / '.seed-exec.php'
    runfile.write_text('<?php\n' + body, encoding='utf-8')
    run = subprocess.run([shutil.which('php') or 'php', 'artisan', 'tinker', '--no-ansi', '--execute',
                          f"require base_path('{runner.REVISAO_DIR}/.seed-exec.php');"],
                         cwd=repo, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    out = (run.stdout or '').strip()
    if run.returncode:
        return {'status': 'erro', 'hash': digest, 'motivo': ((run.stderr or '') + out).strip()[-400:]}
    if body.strip() and out.endswith(body.strip()[-200:]):
        return {'status': 'erro', 'hash': digest, 'motivo': 'o seed foi impresso em vez de executado'}
    return {'status': 'aplicado', 'hash': digest, 'registros': out.splitlines()[-30:]}


def write_roteiro(alvo, info):
    lines = [f'# Roteiro de teste — {alvo}', '']
    lines.append('## Onde abrir')
    for t in info['tarefas']:
        srv = t.get('servidor') or {}
        mr = t.get('mr') or {}
        lines.append(f"- **{t['task']}** ({t.get('kind') or '?'}): "
                     + (f"[servidor]({srv['url']})" if srv.get('url') else 'servidor: ' + str(srv.get('erro', '—')))
                     + ' · ' + (f"[MR !{mr['iid']}]({mr['url']})" if mr.get('url') else 'MR: ' + str(mr.get('erro', '—'))))
    seed = info.get('seed') or {}
    if seed.get('status') == 'aplicado':
        lines += ['', '## Dados de exemplo (já no banco local)', *[f'- {r}' for r in seed.get('registros') or ['(seed sem resumo)']]]
    elif seed.get('status') in ('barrado', 'erro'):
        lines += ['', '## Dados de exemplo', f"Não carreguei os dados de exemplo: {seed.get('motivo')}"]
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


def review_coordinator_prompt(alvo, info):
    mrs = ', '.join(f"{t['task']} ({(t.get('mr') or {}).get('url', '')})" for t in info['tarefas'])
    return (
        f'Você é a sessão coordenadora da revisão {alvo}: MRs de colega {mrs}, com o card em "Em Fase de Teste". '
        f'O ambiente de teste já está no ar: leia {ROOT / info["roteiro"]} e {STATE_DIR / (alvo + ".json")}. '
        f'Na primeira resposta, entregue a {runner.DONO}, em português e curto: os links dos servidores (tailnet), os links '
        'dos MRs, se houve migration no banco local e o roteiro em passos numerados. '
        'Depois fique à disposição para dúvidas sobre o que o MR muda (diff nos worktrees de _review-wt e os arquivos da '
        f'pasta {ROOT / "tarefas" / alvo}). Confira os servidores com `systemctl --user status "revisao-{alvo}-*"` e, se '
        f'algum cair, suba de novo com `python3 {ROOT / "scripts" / "ambiente_revisao.py"} revisao {alvo}`. '
        'Regras: não edite código, não faça commit/push, não comente no MR, no Trello ou no GitLab e não mexa no banco. '
        'O parecer (review-report.md) é privado: use só para responder aqui, nunca copie para fora. Não registre '
        'APROVAR, RECUPERAR, CONCLUIR nem CORRIGIR.'
    )


CLAUDE_PROJECTS = Path.home() / '.claude' / 'projects'
COORD_MARK = 'sessão coordenadora'


def roteiro_versao(info):
    """Versao do que a coordenadora entregou: muda quando o roteiro muda (CORRIGIR, ambiente refeito)."""
    path = ROOT / info['roteiro'] if info.get('roteiro') else None
    return runner.digest(path.read_bytes())[:16] if path and path.exists() else ''


def coordinator_session(folder):
    """Id da conversa da coordenadora da sala: a mais recente da pasta que comecou como coordenadora."""
    pasta = CLAUDE_PROJECTS / re.sub(r'[^A-Za-z0-9]', '-', str(folder))
    for f in sorted(pasta.glob('*.jsonl'), key=lambda x: x.stat().st_mtime, reverse=True) if pasta.is_dir() else []:
        with f.open(encoding='utf-8', errors='replace') as fh:
            head = fh.read(20000)
        if COORD_MARK in head:
            return f.stem
    return None


def update_prompt(alvo, prompt):
    return (f'Atualização do runner: a funcionalidade {alvo} mudou (correção ou ambiente refeito) e o roteiro foi '
            'regravado. Esqueça o roteiro anterior, releia os arquivos e, na resposta, diga primeiro o que mudou desde a '
            'versão que você entregou antes. As instruções valem como no início:\n\n' + prompt)


def claude_bin():
    return shutil.which('claude') or str(Path.home() / '.local' / 'bin' / 'claude')


def tmux_name(alvo):
    return 'coord-' + re.sub(r'[^A-Za-z0-9_-]', '-', alvo)[:44]


def tmux_alive(name):
    return subprocess.run(['tmux', 'has-session', '-t', '=' + name], capture_output=True).returncode == 0


def tmux_start(alvo, folder, title, session=None, prompt=None):
    """Coordenadora interativa no tmux (como o console): fica viva entre as respostas, aparece no Habblaud e recebe
    as mensagens dele. Parada, nao gasta cota."""
    import uuid
    sid = session or str(uuid.uuid4())
    cmd = [claude_bin(), '--resume', sid] if session else [claude_bin(), '--session-id', sid]
    cmd += ['-n', title[:60]]
    if prompt:
        cmd.append(prompt)
    run = subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_name(alvo), '-c', str(folder), '--', *cmd],
                         capture_output=True, text=True, timeout=30)
    if run.returncode:
        raise RuntimeError(((run.stderr or '') + (run.stdout or '')).strip()[:200] or 'tmux nao abriu a sessao')
    return sid


def tmux_send(name, text):
    """Cola o texto inteiro de uma vez (bracketed paste) e manda Enter: o pedido vira uma mensagem so."""
    buf = 'coord-' + name
    subprocess.run(['tmux', 'load-buffer', '-b', buf, '-'], input=text, text=True, check=True, capture_output=True)
    subprocess.run(['tmux', 'paste-buffer', '-p', '-d', '-b', buf, '-t', '=' + name + ':'], check=True, capture_output=True)
    subprocess.run(['tmux', 'send-keys', '-t', '=' + name + ':', 'Enter'], check=True, capture_output=True)


def tmux_stop(alvo):
    subprocess.run(['tmux', 'kill-session', '-t', '=' + tmux_name(alvo)], capture_output=True)


def spawn_coordinator(alvo, info, prompt=None):
    """Sessao coordenadora da sala (pasta da funcionalidade/revisao), uma por sala, interativa no tmux.

    Ja viva e roteiro igual: nada. Viva e roteiro novo: recebe a atualizacao na mesma conversa. Fora do ar (reboot,
    ou de antes, quando rodava em segundo plano e sumia do Habblaud ao terminar o turno): reabre a mesma conversa."""
    old = (info.get('coordenador') or {})
    folder = ROOT / 'tarefas' / alvo
    versao = roteiro_versao(info)
    prompt = prompt or coordinator_prompt(alvo, info)
    name = tmux_name(alvo)
    title = 'Coordenação: ' + alvo
    mudou = old.get('versao') != versao
    try:
        if tmux_alive(name):
            if mudou:
                tmux_send(name, update_prompt(alvo, prompt))
            return {**old, 'tmux': name, 'versao': versao, **({'at': runner.now()} if mudou else {})}
        session = old.get('sessao') if old.get('tmux') else None
        session = session or (coordinator_session(folder) if old.get('id') else None)
        if session:
            sid = tmux_start(alvo, folder, title, session, update_prompt(alvo, prompt) if mudou else None)
        else:
            sid = tmux_start(alvo, folder, title, None, prompt)
        return {'id': sid[:8], 'sessao': sid, 'tmux': name, 'versao': versao, 'at': runner.now(), 'pasta': str(folder)}
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return {**old, 'erro': 'coordenadora nao abriu: ' + str(error)[:200], 'pasta': str(folder)}


def subir_servidores(alvo, info, host, ports, repos):
    """Servidor dev por tarefa (back/front/portal), com teto de memoria, publicado so na tailnet."""
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
        if (info.get('migracao') or {}).get('status') in ('aplicadas', 'nada'):
            try:
                info['seed'] = apply_seed(alvo, repos['back'][1])
            except (OSError, subprocess.SubprocessError) as error:
                info['seed'] = {'status': 'erro', 'motivo': str(error)[:300]}
    subir_servidores(alvo, info, host, ports, repos)
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


# --- Revisao de MR de colega (pasta tarefas/mr-*): mesmo ambiente, sem MR/commit (decisao do dono, 06/11/2026:
# tudo em "Em Fase de Teste" tem servidor e roteiro). Worktrees em _review-wt/<be|fe|pt>_mr<iid>.
REVIEW_WT = ROOT.parent / '_review-wt'
REVIEW_PREFIX = {'back-end': 'be', 'front-end': 'fe', 'transparencia': 'pt'}
REVIEW_CLONE = {'back-end': 'erp-back-end', 'front-end': 'erp-front-end', 'transparencia': 'transparencia'}


def review_worktree(mr):
    """Worktree do MR no commit atual da branch. Ja existe e tem alteracao local: usa como esta, sem atualizar."""
    prefix = REVIEW_PREFIX.get(mr['repo'])
    if not prefix:
        raise RuntimeError('repositorio sem prefixo de revisao: ' + mr['repo'])
    main = runner.repository({'repo': runner.settings().get('review_clones', REVIEW_CLONE).get(mr['repo'], mr['repo'])})
    subprocess.run(['git', '-C', str(main), 'fetch', '-q', 'origin', mr['branch']], check=True, capture_output=True,
                   text=True, timeout=180)
    target = runner.git(main, 'rev-parse', 'FETCH_HEAD')
    pattern = re.compile(r'(?<!\d)' + str(mr['iid']) + r'(?!\d)')
    found = sorted((d for d in REVIEW_WT.glob(prefix + '_*') if d.is_dir() and (d / '.git').exists() and pattern.search(d.name)),
                   key=lambda d: (d.name != f"{prefix}_mr{mr['iid']}", d.name))
    if found:
        wt = found[0]
        if runner.git(wt, 'status', '--porcelain', '--untracked-files=no'):
            return wt, 'worktree com alteracao local: nao atualizei'
        if runner.git(wt, 'rev-parse', 'HEAD') != target:
            subprocess.run(['git', '-C', str(wt), 'checkout', '-q', '--detach', target], check=True, capture_output=True, text=True)
        return wt, ''
    wt = REVIEW_WT / f"{prefix}_mr{mr['iid']}"
    REVIEW_WT.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', '-C', str(main), 'worktree', 'add', '-q', '--detach', str(wt), target], check=True,
                   capture_output=True, text=True)
    for name in ('.env', 'vendor', 'node_modules'):
        src = main / name
        if src.exists() and not (wt / name).exists():
            if name == '.env':
                shutil.copy2(src, wt / name)
            else:
                (wt / name).symlink_to(src)
    return wt, 'worktree novo (dependencias ligadas ao clone principal)'


def write_roteiro_revisao(alvo, info):
    lines = [f'# Roteiro de teste — revisão {alvo}', '', '## Onde abrir']
    for t in info['tarefas']:
        srv, mr = t.get('servidor') or {}, t.get('mr') or {}
        lines.append(f"- **{t['task']}** ({t.get('kind') or '?'}): "
                     + (f"[servidor]({srv['url']})" if srv.get('url') else 'servidor: ' + str(srv.get('erro', '—')))
                     + (f" · [MR]({mr['url']})" if mr.get('url') else '') + (f" — _{t['nota']}_" if t.get('nota') else ''))
    mig = info.get('migracao') or {}
    if mig:
        texto = {'aplicadas': f"Migrations do MR aplicadas no banco local `{mig.get('banco')}`.",
                 'nada': f"Sem migration pendente no banco local `{mig.get('banco')}`."}.get(mig.get('status'),
                 f"Migrations NÃO aplicadas: {mig.get('motivo', '?')}. Comando, depois de olhar o SQL: `{info.get('comando_migrate', '')}`")
        lines += ['', '## Banco', texto]
    manual = ROOT / 'tarefas' / alvo / 'roteiro.md'
    lines += ['', manual.read_text(encoding='utf-8').strip() if manual.exists() else
              '## O que testar\n_Esta revisão ainda não tem roteiro: peça à sessão de revisão o `roteiro.md` '
              '(passos do ponto de vista de quem usa a tela)._']
    out = ROOT / 'tarefas' / alvo / 'roteiro-teste.md'
    out.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
    return str(out.relative_to(ROOT))


def preparar_revisao(alvo):
    """Ambiente de teste de uma revisao de MR de colega: worktree por MR aberto, migrations nao destrutivas no banco
    local, servidores na tailnet e roteiro (tarefas/<alvo>/roteiro.md, escrito pela revisao). Nunca commit/push/MR."""
    import trello_gitlab as tg
    if not TASK_ID.match(alvo) or not alvo.startswith('mr-') or not (ROOT / 'tarefas' / alvo).is_dir():
        raise ValueError('Revisao desconhecida (pasta tarefas/mr-*): ' + alvo)
    mrs = [m for m in tg.review_mrs(alvo, {}) if m['estado'] == 'aberto']
    host, ports = tailnet_host(), ports_for(alvo)
    info = {'alvo': alvo, 'revisao': True, 'at': runner.now(), 'host': host, 'tarefas': [], 'migrations': [], 'erros': []}
    repos = {}
    for mr in mrs:
        entry = {'task': f"{mr['repo']} !{mr['iid']}", 'mr': {'url': mr['url'], 'iid': mr['iid']}}
        try:
            wt, nota = review_worktree(mr)
            entry.update(repo=str(wt), kind=kind_of(wt), nota=nota)
            if entry['kind']:
                repos[entry['kind']] = (entry['task'], wt)
        except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
            entry.update(kind=None, nota='worktree: ' + str(error)[:200])
        info['tarefas'].append(entry)
    if 'back' in repos:
        info['comando_migrate'] = f"cd {repos['back'][1]} && php artisan migrate"
        try:
            info['migracao'] = apply_migrations(repos['back'][1])
        except (OSError, subprocess.SubprocessError) as error:
            info['migracao'] = {'status': 'erro', 'motivo': str(error)[:300]}
    subir_servidores(alvo, info, host, ports, repos)
    info['roteiro'] = write_roteiro_revisao(alvo, info)
    previous = STATE_DIR / (alvo + '.json')
    if previous.exists() and not runner.read(previous).get('encerrado_em'):
        info['coordenador'] = runner.read(previous).get('coordenador')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    runner.save(STATE_DIR / (alvo + '.json'), info)
    if not os.environ.get('PIPELINE_SEM_AMBIENTE'):
        info['coordenador'] = spawn_coordinator(alvo, info, review_coordinator_prompt(alvo, info))
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
    tmux_stop(alvo)  # a sala acaba junto com o ambiente
    if path.exists():
        info['encerrado_em'] = runner.now()
        runner.save(path, info)
    return {'alvo': alvo, 'encerrado': True}


def alvo_de(task):
    manifest = runner.read(ROOT / 'tarefas' / task / 'task.json')
    return manifest.get('entrega') or task


def main(argv):
    if len(argv) != 3 or argv[1] not in ('preparar', 'encerrar', 'status', 'revisao'):
        print(__doc__)
        return 2
    cmd, alvo = argv[1], argv[2]
    if cmd == 'status':
        path = STATE_DIR / (alvo + '.json')
        print(path.read_text(encoding='utf-8') if path.exists() else '{}')
        return 0
    result = {'preparar': preparar, 'encerrar': encerrar, 'revisao': preparar_revisao}[cmd](alvo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
