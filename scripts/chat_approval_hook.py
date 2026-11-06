#!/usr/bin/env python3
"""Hook UserPromptSubmit / PreInvocation: registra gates do runner a partir de mensagem do dono (config/local.json "dono").

Somente o Claude Code (UserPromptSubmit) ou o Antigravity (PreInvocation) executa
este hook, com o texto que o usuario enviou. A mensagem deve conter exatamente
UMA linha de comando, fora de bloco de codigo, citacao ou conteudo colado:

    APROVAR CP1 <tarefa-ou-entrega> <hash>        (hash: >= 8 caracteres de "pipeline.py gate")
    APROVAR PROTOTIPO <tarefa-ou-entrega> <hash>
    RECUPERAR <tarefa> [dev|validate|plan]   (plan: reabre tarefa concluida; exige novo CP1)

Origem: a sessao precisa ser interativa (claude/agy sem -p/--print) e nenhum ancestral
dela pode ser outro agente (claude, codex, agy) ou o runner. Assim um `claude -p` aberto
por um agente, ou um worker do runner, nao registra nada. Linux: verificacao por
/proc; em outro SO o hook recusa e o caminho e o approve interativo.

Nao e fronteira criptografica contra processo malicioso do mesmo usuario (POLITICA 12).
O hook nunca bloqueia a mensagem; o resultado vai como contexto para a sessao. Excecao: no console sem
janela (HABBLAUD_CONSOLE_SILENT=1) a mensagem com linha de gate e encerrada no hook, sem ir ao modelo.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

APROVAR = re.compile(r'^\s*APROVAR\s+(CP1|PROTOTIPO)\s+([A-Za-z0-9_-]+)\s+([0-9a-fA-F]{8,64})\s*$')
APROVAR_SEM_HASH = re.compile(r'^\s*APROVAR\s+(CP1|PROTOTIPO)\s+([A-Za-z0-9_-]+)\s*$')
RECUPERAR = re.compile(r'^\s*RECUPERAR\s+([A-Za-z0-9_-]+)(?:\s+(dev|validate|plan))?\s*$')
CONCLUIR = re.compile(r'^\s*CONCLUIR\s+([A-Za-z0-9_-]+)(?:\s+(.+?))?\s*$')
CORRIGIR = re.compile(r'^\s*CORRIGIR\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$')
AGENTES = ('claude', 'codex', 'agy')


def dono():
    """Nome de quem decide (config/local.json "dono"), so para as mensagens."""
    import pipeline as runner
    return runner.DONO


def live_lines(prompt):
    """Linhas fora de ``` / ~~~, de citacao (>) e de <pasted_content>...</pasted_content>."""
    fence = pasted = False
    for line in prompt.splitlines():
        s = line.strip()
        if re.match(r'^<pasted_content\b', s):
            pasted = True
        if pasted:
            if '</pasted_content>' in s:
                pasted = False
            continue
        if s.startswith('```') or s.startswith('~~~'):
            fence = not fence
            continue
        if fence or s.startswith('>'):
            continue
        yield line


def commands(prompt):
    # A mensagem inteira tem de ser o comando: nada antes nem depois. Texto para
    # analise, exemplos, citacoes e colagens nunca contam como decisao.
    if len(prompt.strip().splitlines()) != 1:  # \n, \r, U+2028, U+0085 etc.
        return []
    found = []
    for line in live_lines(prompt.strip()):
        m = APROVAR.match(line)
        if m:
            found.append(('aprovar', m.group(1), m.group(2), m.group(3), line.strip()))
            continue
        m = APROVAR_SEM_HASH.match(line)
        if m:
            found.append(('sem_hash', m.group(1), m.group(2), None, line.strip()))
            continue
        m = RECUPERAR.match(line)
        if m:
            found.append(('recuperar', m.group(2), m.group(1), None, line.strip()))
            continue
        m = CONCLUIR.match(line)
        if m:
            found.append(('concluir', (m.group(2) or f'Concluída por {dono()} via chat/Habblaud').strip(), m.group(1), None, line.strip()))
            continue
        m = CORRIGIR.match(line)
        if m:
            found.append(('corrigir', m.group(2).replace('\\n', '\n').strip(), m.group(1), None, line.strip()))
    return found


def _proc(pid):
    with open(f'/proc/{pid}/stat', 'rb') as f:
        stat = f.read().decode('utf-8', 'replace')
    ppid = int(stat[stat.rindex(')') + 2:].split()[1])
    with open(f'/proc/{pid}/cmdline', 'rb') as f:
        argv = [a.decode('utf-8', 'replace') for a in f.read().split(b'\0') if a]
    return ppid, argv


def _is_agent(argv):
    # Executavel, ou o script quando o executavel e um interpretador (node .../codex).
    names = [os.path.basename(argv[0])] if argv else []
    if names and re.match(r'^(node|nodejs|bun|deno|python[0-9.]*)$', names[0]) and len(argv) > 1:
        names.append(os.path.basename(argv[1]))
    return any(n in AGENTES or n.startswith(('claude', 'codex')) for n in names)


SETTINGS = Path.home() / '.claude' / 'settings.json'
AGY_HOOKS = Path.home() / '.gemini' / 'config' / 'hooks.json'
SHELLS = ('sh', 'dash', 'bash', 'zsh')


def configured_commands():
    """Comandos deste hook registrados em UserPromptSubmit (Claude) ou PreInvocation (Antigravity)."""
    me = str(Path(__file__).resolve())
    cmds = []
    if SETTINGS.exists():
        data = json.loads(SETTINGS.read_text(encoding='utf-8'))
        for entry in data.get('hooks', {}).get('UserPromptSubmit', []):
            for hook in entry.get('hooks', []):
                cmd = hook.get('command', '').strip()
                if me in cmd and cmd not in cmds:
                    cmds.append(cmd)
    if AGY_HOOKS.exists():
        data = json.loads(AGY_HOOKS.read_text(encoding='utf-8'))
        for group in data.values():
            if isinstance(group, dict) and group.get('enabled', True) is not False:
                for hook in group.get('PreInvocation', []):
                    cmd = hook.get('command', '').strip()
                    if me in cmd and cmd not in cmds:
                        cmds.append(cmd)
    return cmds


def configured_command():
    """Comando deste hook registrado em UserPromptSubmit, apontando para ESTE arquivo."""
    cmds = configured_commands()
    return cmds[0] if cmds else None


def origin_problem(start_pid=None):
    """None se o hook foi chamado pelo Claude Code / Antigravity numa sessao interativa sem agente acima."""
    if not sys.platform.startswith('linux'):
        return 'verificacao de origem disponivel so no Linux; use o approve interativo'
    try:
        cmd = configured_command()
        allowed = set(configured_commands())
        if cmd:
            allowed.add(cmd)
        if not allowed:
            return 'hook nao registrado no settings para este arquivo'
        pid = start_pid or os.getppid()
        # O pai deve ser o claude/agy da sessao, direto ou via `sh -c <comando configurado>`.
        # Shell de ferramenta de agente (zsh com snapshot etc.) nao passa aqui.
        ppid, argv = _proc(pid)
        if argv and os.path.basename(argv[0]) in SHELLS:
            if len(argv) < 3 or argv[1] != '-c' or argv[2].strip() not in allowed:
                return 'hook nao foi chamado pelo Claude Code'
            pid = ppid
            ppid, argv = _proc(pid)
        if not _is_agent(argv):
            return 'hook nao foi chamado pelo Claude Code'
        session = None
        for _ in range(64):
            if pid <= 1:
                break
            ppid, argv = _proc(pid)
            if any('pipeline.py' in a for a in argv):
                return 'sessao iniciada pelo runner'
            if _is_agent(argv):
                if session is None:
                    session = argv
                    if '-p' in argv or '--print' in argv:
                        return 'sessao headless (claude -p)'
                else:
                    return 'sessao aberta por outro agente (' + os.path.basename(argv[0]) + ')'
            pid = ppid
        if session is None:
            return 'sessao do Claude Code nao identificada'
        return None
    except (OSError, ValueError, IndexError) as error:
        return 'origem indeterminada: ' + type(error).__name__


def handle(payload, env=os.environ, origin=origin_problem):
    if env.get('PIPELINE_RUN_DIR') or env.get('PIPELINE_PHASE'):
        return []
    is_agy = 'lastUserInput' in payload and 'hook_event_name' not in payload
    if not is_agy and payload.get('hook_event_name', 'UserPromptSubmit') != 'UserPromptSubmit':
        return []
    if is_agy and payload.get('initialNumSteps') is not None:
        # Em PreInvocation do Antigravity, executa apenas na primeira invocacao do turno do usuario.
        try:
            tpath = payload.get('transcriptPath')
            if tpath and Path(tpath).exists():
                last_line = ''
                with open(tpath, 'r', encoding='utf-8') as tf:
                    for line in tf:
                        if line.strip():
                            last_line = line
                if last_line and json.loads(last_line).get('type') != 'USER_INPUT':
                    return []
        except Exception:  # noqa: BLE001
            return []
    prompt = (payload.get('lastUserInput') if is_agy else payload.get('prompt')) or ''
    if not any(k in prompt for k in ('APROVAR', 'RECUPERAR', 'CONCLUIR', 'CORRIGIR')):
        return []
    found = commands(prompt)
    if not found:
        return []
    if len(found) > 1:
        return ['[pipeline] Mais de um comando de gate na mensagem; nada registrado. Envie um por mensagem.']
    kind, arg, target, expected, line = found[0]
    if kind == 'sem_hash':
        return [f'[pipeline] "{line}" NAO registrado: informe o hash mostrado pelo agente '
                f'(ex.: APROVAR {arg} {target} <hash>).']
    problem = origin()
    if problem:
        return [f'[pipeline] "{line}" NAO registrado: {problem}.']
    import pipeline as runner
    evidence = {
        'linha': line,
        'session_id': payload.get('conversationId') if is_agy else payload.get('session_id'),
        'transcript_path': payload.get('transcriptPath') if is_agy else payload.get('transcript_path'),
        'cwd': (payload.get('workspacePaths') or [None])[0] if is_agy else payload.get('cwd'),
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
    }
    try:
        if kind == 'aprovar':
            status = runner.register_approvals(target, arg, 'chat-usuario', evidence, expected)
            return [f'[pipeline] {arg} aprovado por {runner.DONO} para {target}: ' +
                    ', '.join(i['task'] for i in status['tasks']) +
                    f" (hash {status['combined_hash'][:12]}). Disparar: pipeline.py run <tarefa>."]
        members = sorted(p.parent.name for p in (runner.ROOT / 'tarefas').glob('*/task.json')
                         if runner.read(p).get('entrega') == target)
        if kind == 'concluir':
            out = []
            for task in members or [target]:
                try:
                    runner.close(task, arg)
                    out.append(f'[pipeline] Tarefa {task} concluida por {runner.DONO} ({arg}).')
                except Exception as error:  # noqa: BLE001
                    out.append(f'[pipeline] "{line}" ({task}) NAO registrado: {type(error).__name__}: {error}')
            return out
        if kind == 'corrigir':
            if members:
                eligible = [t for t in members
                            if runner.read(runner.task_paths(t)[1] / 'state.json').get('phase') in ('ready_for_draft', 'blocked')]
                if not eligible:
                    return [f'[pipeline] Nenhuma tarefa em ready_for_draft ou blocked na entrega {target}.']
                members = eligible
            out = []
            for task in members or [target]:
                try:
                    state = runner.request_correction(task, arg, source='chat-usuario', evidence=evidence)
                    out.append(f"[pipeline] Correcao de {task} registrada por {runner.DONO} em DECISOES-HUMANAS.md; fase {state['phase']}.")
                except Exception as error:  # noqa: BLE001
                    out.append(f'[pipeline] "{line}" ({task}) NAO registrado: {type(error).__name__}: {error}')
            return out
        # RECUPERAR <entrega> recupera as tarefas bloqueadas da entrega; <tarefa>, so ela.
        if members and arg == 'plan':
            return [f'[pipeline] "{line}" NAO registrado: reabertura e por tarefa (RECUPERAR <tarefa> plan).']
        if members:
            members = [t for t in members
                       if runner.read(runner.task_paths(t)[1] / 'state.json').get('phase') == 'blocked']
            if not members:
                return [f'[pipeline] Nenhuma tarefa bloqueada na entrega {target}.']
    except Exception as error:  # noqa: BLE001
        return [f'[pipeline] "{line}" NAO registrado: {type(error).__name__}: {error}']
    out = []
    for task in members or [target]:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                state = runner.recover(task, arg, stale_lock=True, confirm=lambda: True,
                                       source='chat-usuario', evidence=evidence)
            out.append(f"[pipeline] Recuperacao de {task} registrada por {runner.DONO}; fase {state['phase']}.")
        except Exception as error:  # noqa: BLE001 - reportar e seguir; nunca bloquear a mensagem
            out.append(f'[pipeline] "{line}" ({task}) NAO registrado: {type(error).__name__}: {error}')
    return out


def main():
    payload = {}
    try:
        payload = json.loads(sys.stdin.read() or '{}')
        lines = handle(payload)
    except Exception as error:  # noqa: BLE001
        lines = [f'[pipeline] hook de aprovacao falhou: {type(error).__name__}: {error}']
    is_agy = isinstance(payload, dict) and 'lastUserInput' in payload and 'hook_event_name' not in payload
    if is_agy:
        if lines:
            print(json.dumps({'injectSteps': [{'ephemeralMessage': '\n'.join(lines)}]}, ensure_ascii=False))
        else:
            print('{}')
    elif lines and os.environ.get('HABBLAUD_CONSOLE_SILENT') == '1':
        # Console sem janela (habblaud-console.service, decisao do dono 05/11/2026): a linha de gate ja foi
        # registrada (ou recusada) acima; encerrar a mensagem aqui evita gastar cota com o modelo so para
        # responder "registrado". O resultado aparece na sessao como motivo do bloqueio.
        print(json.dumps({'decision': 'block', 'reason': '\n'.join(lines)}, ensure_ascii=False))
    elif lines:
        print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
