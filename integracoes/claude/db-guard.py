#!/usr/bin/env python3
# db-guard.py - PreToolUse hook (port Linux do db-guard.ps1)
# Inspeciona o comando completo e exige aprovacao manual para qualquer operacao
# que possa tocar/destruir banco de dados. Falha SEMPRE para o lado seguro (ask).
import json
import os
import re
import sys


def send_ask(motivo):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": motivo,
        }
    }))
    sys.exit(0)


def send_deny(motivo):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": motivo,
        }
    }))
    sys.exit(0)


# --- Gates do pipeline: aprovacao/recuperacao so pelo dono -------------------
# Aprovacoes do runner (_pipeline/.local/tasks/<id>/approvals|recoveries) sao
# gravadas pelo hook UserPromptSubmit a partir da mensagem do dono ou pelo
# approve interativo. Nenhum agente escreve nelas nem chama as funcoes de registro.
GATE_PATH = r'\.local[/\\]tasks[/\\][^\s\'"]*(approvals|recoveries)'
GATE_DIR = r'[/\\]_pipeline[/\\]\.local[/\\]tasks([/\\]|$)'
GATE_FUNCS = (r'register_approvals|chat_approval_hook|archive_dead_lock|next_phase_after'
              r'|\brecover\s*\(|confirm\s*=\s*lambda')
GATE_FRASES = r'\b(APROVAR\s+(CP1|PROTOTIPO)|RECUPERAR\s+[A-Za-z0-9_-]+)'
# Palavra em posicao de comando (inicio, apos ; & | ( ` $( ou quebra), com prefixos usuais opcionais.
# Prefixo: VAR=x, ou um wrapper seguido de flags (cada flag pode consumir um argumento: -u X, -n 5)
# e, opcionalmente, um argumento numerico (timeout 5). O backtracking cobre flag sem argumento.
WRAPPERS = r'(?:sudo|env|command|builtin|nohup|time|nice|stdbuf|timeout|ionice|chrt|setsid)'
PREFIXO_CMD = (r'(?:\S+=\S*\s+|' + WRAPPERS
               + r'(?:\s+-\S+(?:\s+[^\s;&|`()-]\S*)?)*(?:\s+\d\S*)?\s+)')
SEP_CMD = r'(?:^|[;&|`(\n]|\$\()\s*'
POSICAO_CMD = SEP_CMD + PREFIXO_CMD + r'*(?:\S*/)?'
# cd/pushd no proprio comando muda o diretorio em que caminhos relativos sao resolvidos.
MUDA_DIR = SEP_CMD + PREFIXO_CMD + r'*(?:cd|pushd)\s+(?:-\S+\s+)*([^\s;&|)]+)'
# Wrapper que executa em outro diretorio: env -C dir / env --chdir=dir / sudo -D dir.
WRAPPER_CHDIR = r'\b(?:env|sudo)\s(?:[^;&|\n]*?\s)?(?:-C|-D|--chdir)(?:=|\s*)([^\s;&|)]+)'
INTERPRETADOR = POSICAO_CMD + r'(python[0-9.]*|node|nodejs|php|perl|ruby|bun|deno|sh|bash|zsh|dash|source|exec|eval|xargs|\.)(?=\s|$)'
VERBOS_ESCRITA = (POSICAO_CMD + r'(mv|cp|rm|tee|touch|ln|dd|install|rsync|truncate|chmod|chown)(?=\s|$)'
                  r'|\bsed\s+(?:-\S+\s+)*-i')
REDIRECIONAMENTO = r'(?:\d|&)?>>?\|?\s*([^\s;&|<>]+)'
LOCAL_TAREFAS = r'[/\\]_pipeline[/\\]\.local[/\\]tasks[/\\]'
FERRAMENTAS_ESCRITA = ('Write', 'Edit', 'MultiEdit', 'NotebookEdit')


def destino_protegido(alvo, cwd):
    if alvo.startswith('&') or alvo == '/dev/null':
        return False
    caminho = alvo if os.path.isabs(alvo) or not cwd else os.path.join(cwd, alvo)
    for c in (alvo, caminho, os.path.realpath(caminho)):
        if re.search(GATE_PATH, c) or re.search(LOCAL_TAREFAS, c + os.sep):
            return True
    return False


def diretorios_efetivos(cmd, cwd):
    """cwd inicial + destinos de cd/pushd e env -C/sudo -D, em ordem. None = destino nao resolvivel.
    cd/pushd muda o diretorio do shell; o -C/-D do wrapper vale so para o proprio comando, entao
    entra como possibilidade adicional sem mudar a base dos cd seguintes."""
    dirs = [cwd]
    atual = cwd
    trocas = sorted((m.start(), rx is MUDA_DIR, m.group(1))
                    for rx in (MUDA_DIR, WRAPPER_CHDIR) for m in re.finditer(rx, cmd))
    for _, muda_shell, alvo in trocas:
        alvo = alvo.strip('\'"')
        if re.search(r'[$`*?]', alvo) or alvo == '-':
            dirs.append(None)
            continue
        alvo = os.path.expanduser(alvo)
        destino = os.path.normpath(alvo if os.path.isabs(alvo) or not atual else os.path.join(atual, alvo))
        dirs.append(destino)
        if muda_shell:
            atual = destino
    return dirs


def checar_gates(payload):
    tool = payload.get('tool_name') or ''
    tool_input = payload.get('tool_input') or {}
    cwd = str(payload.get('cwd') or '')
    if tool in FERRAMENTAS_ESCRITA:
        alvo = str(tool_input.get('file_path') or tool_input.get('notebook_path') or '')
        if alvo and cwd and not os.path.isabs(alvo):
            alvo = os.path.join(cwd, alvo)
        for caminho in (alvo, os.path.realpath(alvo) if alvo else ''):
            if re.search(GATE_PATH, caminho):
                send_deny('[gate-guard] Aprovacao/recuperacao do runner so pode vir do dono (chat ou terminal).')
    elif tool == 'Bash':
        cmd = str(tool_input.get('command') or '')
        interpretador = re.search(INTERPRETADOR, cmd)
        dirs = diretorios_efetivos(cmd, cwd)
        # cd para destino nao resolvivel ($VAR, glob, -) junto de approvals/recoveries conta como pasta local.
        na_pasta_local = (any(d and re.search(GATE_DIR, d) for d in dirs)
                          or (None in dirs and re.search(r'approvals|recoveries', cmd)))
        cita_gate = re.search(GATE_PATH, cmd) or (na_pasta_local and re.search(r'approvals|recoveries', cmd))
        # Ler/buscar o codigo do runner e livre; chamar as funcoes via interpretador, nao.
        if re.search(GATE_FUNCS, cmd) and interpretador:
            send_deny('[gate-guard] Agente nao chama o registro de aprovacao/recuperacao do runner.')
        # Redirecionamento so conta quando o destino e protegido (2>/tmp/x.log e livre).
        if any(destino_protegido(alvo, d or '') for alvo in re.findall(REDIRECIONAMENTO, cmd) for d in dirs):
            send_deny('[gate-guard] Escrita na pasta local de tarefas do runner e exclusiva do runner/o dono.')
        if (cita_gate or na_pasta_local) and re.search(VERBOS_ESCRITA, cmd):
            send_deny('[gate-guard] Escrita em approvals/recoveries do runner e exclusiva do dono.')
        if cita_gate and interpretador:
            send_deny('[gate-guard] Interpretador sobre approvals/recoveries do runner e exclusivo do dono.')
        if re.search(r'\borca\b', cmd) and re.search(GATE_FRASES, cmd):
            send_deny('[gate-guard] Agente nao envia frase de aprovacao/recuperacao para outra sessao.')


# --- Padroes de risco: regex -> descricao -------------------------------------
PADROES = [
    # SQL destrutivo
    (r'drop\s+(database|schema|table|index|view)', 'SQL destrutivo: DROP'),
    (r'truncate\s+table', 'SQL destrutivo: TRUNCATE'),
    # Sem TABLE: so em posicao de comando (coreutils truncate) ou abrindo string/linha de SQL;
    # "truncate " solto no meio de texto/codigo dava falso positivo.
    (r'(?:^|[;&|(\n\'"`]|\$\()\s*(?:sudo\s+)?truncate\s', 'SQL destrutivo: TRUNCATE'),
    (r'alter\s+table\s+.+\s+drop\s', 'SQL destrutivo: ALTER TABLE ... DROP'),

    # Laravel / artisan
    (r'artisan\s+migrate', 'Laravel: comando de migration'),
    (r'artisan\s+db:', 'Laravel: comando db: (possivel db:wipe)'),
    (r'migrate:(fresh|refresh|reset|rollback)', 'Laravel: migration destrutiva'),
    (r'db:wipe', 'Laravel: db:wipe apaga o banco inteiro'),

    # MySQL / MariaDB
    (r'\bmysql\b', 'Cliente MySQL'),
    (r'\bmysqladmin\b', 'mysqladmin (pode dropar banco)'),
    (r'\bmysqldump\b', 'mysqldump (acesso direto ao banco)'),
    (r'\bmariadb\b', 'Cliente MariaDB'),

    # PostgreSQL
    (r'\bpsql\b', 'Cliente PostgreSQL'),
    (r'\bdropdb\b', 'dropdb apaga o banco inteiro'),
    (r'\bpg_restore\b', 'pg_restore (sobrescreve dados)'),

    # Prisma / ORMs Node
    (r'prisma\s+migrate\s+reset', 'Prisma: migrate reset apaga todos os dados'),
    (r'prisma\s+migrate', 'Prisma: comando de migration'),
    (r'prisma\s+db\s+(push|execute)', 'Prisma: db push/execute altera o schema'),
    (r'--force-reset', 'Flag --force-reset destroi os dados'),
    (r'--accept-data-loss', 'Flag --accept-data-loss destroi os dados'),
    (r'synchronize\s*[:=]\s*true', 'TypeORM synchronize:true pode dropar colunas'),
    (r'force\s*[:=]\s*true', 'Sequelize/ORM force:true recria tabelas'),
    (r'sequelize\s+db:', 'Sequelize CLI: comando de banco'),
    (r'knex\s+migrate', 'Knex: comando de migration'),

    # Remocao de arquivos de dados / dumps
    # Palavra inteira e mesma linha de comando (continuacao com \ vale): "model ... json.dump("
    # casava "del ... .dump" atravessando linhas.
    (r'\b(rm|del|remove-item|unlink)\b(?:[^|;\n]|\\\n)*\.(sql|sqlite3?|db|dump|bak|mdf|frm|ibd)\b',
     'Remocao de arquivo de banco/dump'),
    (r'\b(rm|remove-item)\b(?:[^|;\n]|\\\n)*(data|database|db|mysql|postgres)[/\\]',
     'Remocao de diretorio de dados'),

    # Docker: apagar volume = apagar banco
    (r'docker\s+volume\s+rm', 'docker volume rm destroi dados persistidos'),
    (r'docker\s+volume\s+prune', 'docker volume prune destroi dados persistidos'),
    (r'docker[- ]compose\s+down\b[^|;]*(-v|--volumes)', 'compose down -v destroi os volumes do banco'),
    (r'docker\s+system\s+prune', 'docker system prune pode remover volumes'),
]

try:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.exit(0)

    payload = json.loads(raw)
    checar_gates(payload)
    if (payload.get('tool_name') or '') in FERRAMENTAS_ESCRITA:
        # Escrita de arquivo: so a checagem de gates. Os padroes de banco abaixo
        # sao para comandos, nao para conteudo de codigo.
        sys.exit(0)

    # Junta todos os valores string do tool_input - cobre Bash(command),
    # ferramentas MCP de banco (query/sql/statement) e variacoes futuras.
    tool_input = payload.get('tool_input') or {}
    alvo = ' '.join(v for v in tool_input.values() if isinstance(v, str))
    if not alvo.strip():
        sys.exit(0)

    t = alvo.lower()

    for padrao, descricao in PADROES:
        if re.search(padrao, t):
            send_ask(f'[db-guard] {descricao}. Confirme manualmente antes de executar.')

    # DELETE FROM sem WHERE
    if re.search(r'delete\s+from', t) and not re.search(r'delete\s+from[\s\S]{0,400}?\bwhere\b', t):
        send_ask('[db-guard] DELETE FROM sem clausula WHERE - apagaria a tabela inteira.')
    # UPDATE ... SET sem WHERE
    if re.search(r'update\s+\S+\s+set\b', t) and not re.search(r'update[\s\S]{0,400}?\bwhere\b', t):
        send_ask('[db-guard] UPDATE ... SET sem clausula WHERE - afetaria todas as linhas.')

    sys.exit(0)
except SystemExit:
    raise
except Exception as e:
    # Falha para o lado seguro: se nao consegui analisar, peco aprovacao.
    send_ask(f'[db-guard] Nao foi possivel analisar o comando ({e}). Confirme manualmente.')
