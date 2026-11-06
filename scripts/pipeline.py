#!/usr/bin/env python3
"""Portable phase runner. New process/session per slice; approvals are human gates."""
from __future__ import annotations
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import uuid

from usage_core import ROOT, settings


# Quem aprova e decide (gates, curadoria, conclusao): config/local.json "dono"; PIPELINE_DONO sobrepoe.
DONO = os.environ.get('PIPELINE_DONO') or settings().get('dono') or 'Dono'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def task_paths(task):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', task):
        raise ValueError('ID deve conter somente letras, numeros, _ ou -')
    return ROOT / 'tarefas' / task, ROOT / '.local/tasks' / task


@contextlib.contextmanager
def mutex(local):
    """Kernel lock serializes acquisition/recovery, including after a runner dies."""
    local.mkdir(parents=True, exist_ok=True)
    with (local / 'runner.mutex').open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError('Runner/recuperacao ativo; nao iniciar outra execucao') from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def process_alive(record):
    """True/False/None; foreign hosts, denied probes and invalid PIDs are unknown."""
    pid = record.get('pid')
    if record.get('host') != socket.gethostname() or type(pid) is not int or pid <= 0:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            return False if result == 0 else True if result == 258 else None
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None
    return True


def archive_dead_lock(local, path, source='auto-dono-encerrado'):
    """Archive (never delete) a lock whose owner is provably dead on this host.

    Called under the task mutex. Live, foreign-host or unreadable owners stay
    blocking: PID reuse or doubt never releases a lock.
    """
    try:
        owner = read(path)
    except (OSError, ValueError):
        owner = None
    if not isinstance(owner, dict) or process_alive(owner) is not False:
        raise RuntimeError('Lock existente com dono vivo ou desconhecido; inspecionar antes de recover --stale-lock.')
    recovery_id = uuid.uuid4().hex
    save(local / 'recoveries' / (recovery_id + '.json'),
         {'at': now(), 'source': source, 'stale_lock': owner, 'phase': read(local / 'state.json').get('phase')
          if (local / 'state.json').exists() else None})
    path.rename(local / 'recoveries' / (recovery_id + '.lock.json'))
    return recovery_id


@contextlib.contextmanager
def lock(local):
    path = local / 'runner.lock'
    with mutex(local):
        record = {'pid': os.getpid(), 'host': socket.gethostname(), 'started_at': now()}
        try:
            with path.open('x', encoding='utf-8') as f:
                json.dump(record, f)
        except FileExistsError:
            archive_dead_lock(local, path)
            with path.open('x', encoding='utf-8') as f:
                json.dump(record, f)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)


def process_record(run, status, pid=None, **extra):
    save(run / 'process.json', {'pid': pid, 'host': socket.gethostname(),
                               'status': status, 'at': now(), **extra})


def start_process(run, args, **kwargs):
    # Persist intent before spawning; a crash in the spawn/PID gap is unknown,
    # never evidence that no process exists.
    process_record(run, 'launching')
    try:
        process = subprocess.Popen(args, **kwargs)
    except OSError:
        process_record(run, 'launch_failed')
        raise
    process_record(run, 'running', process.pid)
    return process


def unsettled_runs(task, state):
    pending = []
    for run in sorted((ROOT / '.local/runs' / task).glob('*')):
        receipt = read(run / 'receipt.json') if (run / 'receipt.json').exists() else {}
        proc = read(run / 'process.json') if (run / 'process.json').exists() else {}
        if (receipt.get('status') == 'running' or
                proc.get('status') in ('launching', 'running', 'timeout_unsettled') or
                state.get('active_run') == str(run.relative_to(ROOT))):
            pending.append((run, receipt, proc))
    return pending


def git(repo, *args, binary=False):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
    return p.stdout if binary else p.stdout.decode('utf-8', errors='replace').strip()


def repository(manifest):
    aliases = settings().get('repositories', {})
    path = Path(aliases.get(manifest['repo'], '../' + manifest['repo']))
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def fingerprint(repo):
    """HEAD + tracked modifications + untracked file content; no .env is printed."""
    h = hashlib.sha256(git(repo, 'rev-parse', 'HEAD').encode())
    h.update(git(repo, 'diff', 'HEAD', '--binary', binary=True))
    for raw in git(repo, 'ls-files', '--others', '--exclude-standard', '-z', binary=True).split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8', errors='strict')
        p = repo / name
        h.update(raw)
        if p.is_file():
            with p.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
    return h.hexdigest()


def gate_hash(folder, manifest, gate, include_plan=False):
    # Include manifest (base, providers, checks, scope) and exact task inputs.
    # CP1 aprova a especificacao da funcionalidade (task-spec.md + manifesto);
    # plan.md e gerado pelo proprio runner na fase plan, sem novo gate humano.
    material = json.dumps(manifest, sort_keys=True).encode()
    names = ['task-spec.md']
    if include_plan and gate != 'CP0' and (folder / 'plan.md').exists():
        names += ['plan.md']
    if gate == 'PROTOTIPO':
        names += ['prototipo/index.html']
    for name in names:
        material += name.encode() + (folder / name).read_bytes()
    return digest(material)


def approved(folder, local, manifest, gate):
    p = local / 'approvals' / (gate + '.json')
    if not p.exists():
        return False
    record = read(p)
    valid_hashes = {gate_hash(folder, manifest, gate), gate_hash(folder, manifest, gate, include_plan=True)}
    if record.get('hash') not in valid_hashes:
        return False
    if manifest.get('entrega'):
        # A aprovacao vale para a entrega inteira: mudar a spec/manifesto de outra
        # tarefa, ou a composicao da entrega, invalida todas.
        # Exige tambem que TODAS as tarefas tenham o registro publicado com o mesmo
        # hash combinado: falha no meio da gravacao nao libera parte da entrega.
        try:
            valid_combined = {
                entrega_hash(manifest['entrega'], gate),
                entrega_hash(manifest['entrega'], gate, include_plan=True),
            }
            combined = record.get('combined_hash')
            if combined not in valid_combined:
                return False
            for task in entrega_tasks(manifest['entrega']):
                other = task_paths(task)[1] / 'approvals' / (gate + '.json')
                if not other.exists() or read(other).get('combined_hash') != combined:
                    return False
            return True
        except (OSError, ValueError, RuntimeError):
            return False
    return True


def write_handoff(folder, state):
    body = state.get('handoff', {})
    text = '# HANDOFF — ' + folder.name + '\n\n'
    text += 'Fase: ' + state['phase'] + '\n\n'
    for key, title in [('summary', 'Situação'), ('decisions', 'Decisões'), ('next_step', 'Próximo passo')]:
        text += '## ' + title + '\n' + str(body.get(key, 'Consultar estado local e manifesto.')) + '\n\n'
    text += 'Estado local: .local/tasks/' + folder.name + '/state.json (não sincronizar entre máquinas).\n'
    text += 'Manifesto portátil: task.json. Aprovações precisam ser revalidadas na máquina de execução.\n'
    (folder / 'HANDOFF.md').write_text(text, encoding='utf-8')


def schema():
    properties = {key: {'type': 'string'} for key in ('summary', 'next_step', 'decisions', 'document', 'verdict')}
    properties['status'] = {'type': 'string', 'enum': ['completed', 'continue', 'blocked']}
    properties['artifacts'] = {'type': 'array', 'items': {'type': 'string'}}
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def validate_response(response):
    if not isinstance(response, dict) or set(response) != set(schema()['required']):
        raise ValueError('Resposta nao corresponde ao contrato estruturado')
    if response['status'] not in ('completed', 'continue', 'blocked'):
        raise ValueError('Estado de resposta invalido')
    if any(not isinstance(response[k], str) for k in ('summary', 'next_step', 'decisions', 'document', 'verdict')):
        raise ValueError('Campos de texto invalidos')
    if not isinstance(response['artifacts'], list) or any(not isinstance(x, str) for x in response['artifacts']):
        raise ValueError('Lista de artefatos invalida')
    if not response['summary'].strip() or not response['next_step'].strip():
        raise ValueError('Handoff incompleto')
    if sum(len(response[k]) for k in ('summary', 'next_step', 'decisions')) > 10000:
        raise ValueError('Handoff excedeu 10 mil caracteres; nao transportar historico inteiro')


# Fases que nao editam codigo e ferramentas do Claude que so leem (negativa incidental aceita; ver invoke).
READ_ONLY_PHASES = ('plan', 'prototype', 'review', 'docs')
READ_TOOLS = ('Read', 'Grep', 'Glob', 'LS', 'NotebookRead')

# Comandos de shell que o agy pode rodar sem pedir (so leitura; ver invoke, ramo agy).
AGY_READ_COMMANDS = ('ls', 'cat', 'head', 'tail', 'wc', 'grep', 'sed -n', 'cut', 'tr', 'nl',
                     'git diff', 'git log', 'git show', 'git status', 'git grep')


def invoke(provider, prompt, repo, run, phase, config):
    exe = shutil.which(provider)
    if not exe:
        raise RuntimeError('CLI nao encontrado: ' + provider)
    result_path = run / 'result.json'
    schema_path = run / 'schema.json'
    save(schema_path, schema())
    if provider == 'claude':
        hook_command = subprocess.list2cmdline([sys.executable, str(ROOT / 'scripts/budget_hook.py')]) if os.name == 'nt' else __import__('shlex').join([sys.executable, str(ROOT / 'scripts/budget_hook.py')])
        hook_settings = {'hooks': {'PostToolUse': [{'hooks': [{'type': 'command', 'command': hook_command}]}]}}
        save(run / 'hooks.json', hook_settings)
        args = [exe, '-p', '--output-format', 'json', '--json-schema', json.dumps(schema()),
                '--permission-mode', 'default', '--session-id', str(uuid.uuid4()), '--settings', str(run / 'hooks.json')]
        # Do not install bypasses or change existing hooks. Reads can be automatic;
        # unapproved writes/commands fail visibly in headless mode.
        if phase != 'dev':
            args += ['--tools', 'Read,Grep,Glob']
        task_folder = ROOT / 'tarefas' / run.parent.name
        if phase != 'dev':
            # Pasta de runs da tarefa (inclui o atual): recibo de validacao e logs de checks sao evidencia.
            args += ['--add-dir', str(run.parent.resolve()), str(task_folder.resolve())]
            # Worktree com node_modules ligado ao clone principal: ler dependencias e legitimo.
            if (Path(repo) / 'node_modules').is_symlink():
                args.append(str((Path(repo) / 'node_modules').resolve()))
            # Leitura extra declarada no manifesto (aprovada no CP1): repos irmaos
            # da entrega, guias de convencao. Somente fases sem escrita.
            manifest_path = task_folder / 'task.json'
            for extra in (read(manifest_path).get('read_dirs', []) if manifest_path.exists() else []):
                args.append(str(repository({'repo': extra}) if re.fullmatch(r'[a-zA-Z0-9_-]+', extra)
                                else (ROOT / extra).resolve()))
        # Do not grant a dev additional writable directories. Its short task
        # context is carried by the prompt; read denials remain explicit failures.
    elif provider == 'codex':
        args = [exe, 'exec', '--json', '--color', 'never', '--sandbox',
                'workspace-write' if phase == 'dev' else 'read-only',
                '--output-schema', str(schema_path), '-o', str(result_path), '-']
    elif provider == 'agy':
        # Antigravity CLI (Gemini). Headless nao pede permissao: comando de shell sem
        # allow-rule e negado e aparece em denied_actions (falha visivel, como no Claude).
        # Sem --dangerously-skip-permissions. dev: accept-edits no workspace (cwd = worktree);
        # demais fases: plan, que le mas nao edita (verificado no agy 1.4.12 em 03/11/2026).
        # O agy nao le o prompt do stdin; vai como argumento (-p por ultimo, colado ao valor).
        # No headless, qualquer negativa encerra o run sem resposta (a regra de "negativa
        # incidental" do prompt geral nao vale aqui). O agy 1.4.12 nao tem ferramenta de listar
        # ou buscar: isso passa pelo shell. Por isso --sandbox (Landlock) e allow-rules so de
        # comandos de leitura. Testado em 03/11/2026: sem --sandbox, command(ls) deixa passar
        # `ls > arquivo` fora do workspace; com ele, a escrita fora (exceto /tmp) e negada, e no
        # modo plan ate a escrita dentro do workspace e negada.
        prompt += ('\n\nRestricoes deste executor (agy headless): qualquer acao sem permissao ENCERRA a execucao '
                   'sem resposta e a fase e perdida. No shell, use somente comandos de leitura: '
                   + ', '.join(AGY_READ_COMMANDS) + ' (direto, no diretorio atual, que ja e o repositorio: sem cd, sem git -C, sem caminho de outro repositorio no git). Nada de redirecionamento (>), outros comandos, '
                   'instalacao, testes ou servidores (o runner roda os checks); no dev o git nao funciona no sandbox, use o diff.patch. Leia apenas o repositorio, '
                   'a pasta desta tarefa e as das tarefas irmas em tarefas/. Nao leia scripts/, config/ nem .local/ '
                   'do runner e nao investigue falhas anteriores do pipeline: elas ja foram tratadas.')
        if len(prompt.encode('utf-8')) > 120000:
            raise RuntimeError('Prompt grande demais para argumento do agy; reduzir contexto')
        args = [exe, '--output-format', 'json', '--json-schema', json.dumps(schema()),
                '--model', config.get('agy_model', 'gemini-4-argon-high'),
                '--mode', 'accept-edits' if phase == 'dev' else 'plan']
        task_folder = ROOT / 'tarefas' / run.parent.name
        # Leitura de tarefas/ inteira (planos de tarefas irmas da entrega); continua so leitura.
        extra = [run.parent.resolve(), task_folder.parent.resolve()]
        if (Path(repo) / 'node_modules').is_symlink():
            extra.append((Path(repo) / 'node_modules').resolve())
        manifest_path = task_folder / 'task.json'
        for item in (read(manifest_path).get('read_dirs', []) if manifest_path.exists() else []):
            extra.append(repository({'repo': item}) if re.fullmatch(r'[a-zA-Z0-9_-]+', item)
                         else (ROOT / item).resolve())
        allow = ['command(' + c + ')' for c in AGY_READ_COMMANDS]
        if phase == 'dev':
            # O prompt aponta para diff.patch (runs) e plano/spec (tarefas), fora do worktree.
            # accept-edits so le o workspace; liberar apenas LEITURA desses diretorios, via
            # allow-rule de sessao (nao salva). Escrita fora do worktree segue negada.
            allow += ['read_file(' + str(d) + ')' for d in extra]
        else:
            # Worktree: o .git real fica no repo principal, que o sandbox esconde e o git falha
            # (128). Nas fases so de leitura (plan) entra no workspace; no dev nao, porque
            # accept-edits daria escrita nas refs do repo inteiro (la o git falha e o diff.patch basta).
            common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                    cwd=repo, capture_output=True, text=True)
            if common.returncode == 0 and common.stdout.strip():
                extra.append(Path(common.stdout.strip()).resolve())
            for directory in extra:
                args += ['--add-dir', str(directory)]
        settings_path = run / 'agy-settings.json'
        save(settings_path, {'permissions': {'allow': allow}})
        args += ['--sandbox', '--settings', str(settings_path)]
        args.append('-p=' + prompt)
    else:
        raise ValueError('Adapter automatico implementado somente para claude/codex/agy')
    env = os.environ.copy()
    env['PIPELINE_PHASE'] = phase
    env['PIPELINE_RUN_DIR'] = str(run)
    (run / 'prompt.txt').write_text(prompt, encoding='utf-8')
    with (run / 'stdout.jsonl').open('w', encoding='utf-8') as out, (run / 'stderr.log').open('w', encoding='utf-8') as err:
        process = start_process(run, args, cwd=repo, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                text=True, encoding='utf-8')
        try:
            process.communicate(prompt, timeout=config['timeout_seconds'])
        except subprocess.TimeoutExpired:
            # Do not terminate an agent mid-write or launch another writer. Keep
            # the task blocked until the exact PID is checked by recover.
            process_record(run, 'timeout_unsettled', process.pid)
            raise RuntimeError(f'Timeout: processo {process.pid} pode estar vivo. Nenhum redisparo; verificar antes de recover.')
        process_record(run, 'exited', process.pid, exit_code=process.returncode)
    if process.returncode:
        raise RuntimeError(f'{provider} saiu com codigo {process.returncode}; ver stderr.log')
    if provider == 'claude':
        data = read(run / 'stdout.jsonl')
        if not isinstance(data, dict):
            raise ValueError('Resposta Claude deve ser objeto JSON')
        denials = data.get('permission_denials') or []
        # Fase que nao edita codigo com resultado completo: negativa so de leitura (ex.: o plano tentou ler o
        # runner) e incidental, fica no recibo. Escrita/comando negado, ou qualquer negativa no dev, bloqueia.
        incidental = (phase in READ_ONLY_PHASES and data.get('structured_output') is not None
                      and all(isinstance(d, dict) and d.get('tool_name') in READ_TOOLS for d in denials))
        if data.get('is_error') or (denials and not incidental):
            raise RuntimeError('Claude retornou erro ou permissao negada; entrega nao aceita')
        meta = {'session_id': data.get('session_id'), 'usage': data.get('usage')}
        if denials:
            meta['negativas_incidentais'] = [{'tool': d.get('tool_name'), 'input': json.dumps(d.get('tool_input'), ensure_ascii=False)[:200]}
                                             for d in denials if isinstance(d, dict)][:10]
        return data.get('structured_output'), meta
    if provider == 'agy':
        data = read(run / 'stdout.jsonl')
        if not isinstance(data, dict):
            raise ValueError('Resposta agy deve ser objeto JSON')
        if data.get('status') != 'SUCCESS' or data.get('denied_actions'):
            raise RuntimeError('agy retornou erro ou permissao negada; entrega nao aceita')
        return data.get('structured_output'), {'session_id': data.get('conversation_id'), 'usage': data.get('usage')}
    events = []
    for line in (run / 'stdout.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise ValueError('Evento Codex nao e JSON valido') from error
        if not isinstance(event, dict):
            raise ValueError('Evento Codex deve ser objeto JSON')
        events.append(event)
    if any(e.get('type') in ('error', 'turn.failed') for e in events):
        raise RuntimeError('Codex retornou erro em evento; entrega nao aceita')
    return read(result_path), {'session_id': next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'), None),
                               'usage': [e.get('usage') for e in events if e.get('type') == 'turn.completed']}


def check_scope(repo, manifest):
    raw = git(repo, 'diff', '--no-renames', '--name-only', '-z', manifest['base_sha'], '--', binary=True)
    raw += git(repo, 'ls-files', '--others', '--exclude-standard', '-z', binary=True)
    names = [name.decode('utf-8', errors='strict') for name in raw.split(b'\0') if name]
    scope = manifest['scope']
    outside = [n for n in names if not any(n == p.rstrip('/') or n.startswith(p.rstrip('/') + '/') for p in scope)]
    if outside:
        raise RuntimeError('Mudanca fora do escopo: ' + ', '.join(outside[:12]))


class BackToDev(RuntimeError):
    """Falha de validacao que o proprio dev resolve: a tarefa volta ao dev sozinha, com o log, e conta
    uma rodada (decisao do dono, 04/11/2026: aprovou o CP1, o runner entrega pronto). So bloqueia
    quando estoura max_rounds."""


def tracked_changes(repo):
    return set(git(repo, 'diff', '--name-only', 'HEAD').splitlines())


def run_checks(repo, manifest, run, config):
    if not manifest['checks']:
        raise RuntimeError('Sem checks aprovados; validacao nao pode ser declarada verde')
    results = []
    before = fingerprint(repo)
    dirty_before = tracked_changes(repo)
    for index, command in enumerate(manifest['checks']):
        if not isinstance(command, list) or not command or any(not isinstance(x, str) for x in command):
            raise ValueError('Check deve ser argv JSON, sem shell')
        with (run / f'check-{index}.log').open('w', encoding='utf-8') as log:
            process = start_process(run, command, cwd=repo, stdout=log, stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=config['timeout_seconds'])
            except subprocess.TimeoutExpired:
                process_record(run, 'timeout_unsettled', process.pid, check_index=index)
                raise RuntimeError(f'Check timeout, PID {process.pid}; verificar antes de recuperar')
            process_record(run, 'exited', process.pid, exit_code=code, check_index=index)
        results.append({'argv': command, 'exit_code': code, 'log': f'check-{index}.log'})
    restored = []
    if fingerprint(repo) != before:
        # Arquivo versionado que estava limpo e os checks gravaram: volta ao HEAD (e o estado de antes dos
        # checks, nada do dev se perde) e o dev corrige a causa. Mexeu em arquivo do dev ou criou arquivo
        # novo: nao da para separar com seguranca, bloqueia.
        restored = sorted(tracked_changes(repo) - dirty_before)
        if restored:
            git(repo, 'checkout', 'HEAD', '--', *restored)
        if fingerprint(repo) != before:
            raise RuntimeError('Check alterou a arvore (arquivos do dev ou novos); inspecionar antes de validar novamente')
    save(run / 'validation.json', {'fingerprint': before, 'head': git(repo, 'rev-parse', 'HEAD'), 'tests': results,
                                   'restaurados_apos_checks': restored, 'at': now()})
    failed = [r for r in results if r['exit_code']]
    if restored or failed:
        parts = []
        if failed:
            parts.append('checks falharam: ' + '; '.join(' '.join(r['argv']) + f" (exit {r['exit_code']}, {r['log']})" for r in failed))
        if restored:
            parts.append('os checks gravaram em arquivos versionados (restaurados pelo runner; corrija para nao gravar): '
                         + ', '.join(restored[:12]))
        raise BackToDev('Validacao: ' + ' | '.join(parts))
    return results


def auto_failure(state, tail=6000, max_logs=3):
    """A falha de validacao que trouxe a tarefa de volta ao dev sozinha (BackToDev), com o fim dos logs."""
    info = state.get('auto_failure') or {}
    if not info:
        return ''
    parts = ['A validacao anterior falhou e a tarefa voltou ao dev para voce corrigir: ' + info.get('reason', '')]
    run = ROOT / info['run'] if info.get('run') else None
    for log in (sorted(run.glob('check-*.log'))[:max_logs] if run and run.exists() else []):
        text = log.read_text(encoding='utf-8', errors='replace')
        falhas = [l for l in text.splitlines() if re.search(r'\bFAIL|⨯|Error\b|error', l)][:20]
        parts.append(f'Linhas de falha de {log.relative_to(ROOT)}:\n' + '\n'.join(falhas))
        parts.append(f'Final de {log.relative_to(ROOT)}:\n' + text[-tail:])
    return '\n\n'.join(parts)


DECISOES = 'decisoes-humanas.md'


def decisoes_humanas(folder):
    """Decisoes do dono tomadas no chat dentro do escopo aprovado. Prevalecem sobre o handoff do agente;
    nao ampliam escopo nem substituem gate (escopo novo exige manifesto novo e CP1)."""
    path = folder / DECISOES
    text = path.read_text(encoding='utf-8').strip() if path.exists() else ''
    if not text:
        return ''
    return (f'Decisoes humanas ({DONO}; prevalecem sobre o handoff anterior, dentro do escopo aprovado):\n'
            + text[-8000:])


def recovered_failure(task, tail=6000, max_logs=3):
    """Motivo e final dos logs de checks do bloqueio que a ultima recuperacao levou ao dev."""
    folder = ROOT / '.local/tasks' / task / 'recoveries'
    events = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    event = read(events[-1]) if events else {}
    if event.get('to_phase') != 'dev':
        return ''
    previous = event.get('previous_state', {})
    parts = ['Bloqueio anterior que motivou a volta ao dev: ' + str(previous.get('reason', ''))]
    runs = [r['path'] for r in event.get('runs', [])] + ([previous['last_run']] if previous.get('last_run') else [])
    logs = [log for r in dict.fromkeys(runs) for log in sorted((ROOT / r).glob('check-*.log'))]
    for log in logs[:max_logs]:
        text = log.read_text(encoding='utf-8', errors='replace')
        falhas = [l for l in text.splitlines() if re.search(r'\bFAIL|⨯|Error\b', l)][:20]
        parts.append(f'Linhas de falha de {log.relative_to(ROOT)}:\n' + '\n'.join(falhas))
        parts.append(f'Final de {log.relative_to(ROOT)}:\n' + text[-tail:])
    return '\n\n'.join(parts)


def build_prompt(folder, manifest, state, config):
    phase = state['phase']
    task = (folder / 'task-spec.md').read_text(encoding='utf-8')
    if len(task) > 16000:
        task = f'Leia somente criterios relevantes em {folder / "task-spec.md"}; entrada extensa nao injetada.'
    return '\n\n'.join([
        'Execute SOMENTE a fase ' + phase + ' desta tarefa. Nao dispare outros agentes, nao publique, nao faca push/merge, nao execute migrations. '
        'Nao leia scripts/, config/ nem .local/ do runner (_pipeline): a leitura e negada e nada ali e necessario para a tarefa.',
        'Politica vigente:\n' + (ROOT / 'POLITICA.md').read_text(encoding='utf-8'),
        'Manifesto aprovado:\n' + json.dumps(manifest, ensure_ascii=False),
        'Criterios:\n' + task,
        'Handoff curto anterior:\n' + json.dumps(state.get('handoff', {}), ensure_ascii=False),
        'Evidencia de validacao do runner:\n' + json.dumps(state.get('validation', {}), ensure_ascii=False),
        (auto_failure(state) or recovered_failure(folder.name)) if phase == 'dev' else '',
        decisoes_humanas(folder),
        'Documento parcial da fase anterior (se existir): ' + str(folder / (phase + '-partial.md')),
        'Plano: ' + str(folder / 'plan.md') + '. Ler quando existir e for pertinente. Findings: ' + str(folder / 'review-report.md'),
        'Fases plan/docs/review/prototype sao somente leitura do codigo; entregue documento completo no campo document. '
        'Plan deve enumerar fatias pequenas e criterios de aceite. Prototype entrega HTML estatico. '
        'Docs: o documento tem as secoes "## Resumo" (o que mudou, em linguagem de produto; vai para a descricao do MR, '
        'entao sem achados da revisao) e "## Roteiro de teste manual" (passos numerados para ' + DONO + ' testar no '
        'servidor de revisao do ponto de vista de quem usa o sistema: menu/tela onde clicar, o que preencher, o que deve '
        'aparecer na tela; dados de exemplo e casos de erro como o usuario os ve; nada de rotas, codigos HTTP, classes ou '
        'testes automatizados, que ficam em documentacao-tecnica.md). '
        'Dev implementa UMA fatia coerente e retorna continue com proximo passo quando faltar trabalho; nao altere task.json, plan.md, approvals ou estado do runner. '
        'Review: verdict deve ser approved ou changes_required; forneca findings verificaveis. Nao corrija. '
        'Informe artifacts com paths existentes relativos ao repo, nao com alegacoes de testes. '
        'O runner executara os checks aprovados. Permissao negada que impeca concluir a fase, ou necessidade de decisao, retorna blocked; '
        'negativa incidental contornada sem prejuizo da entrega nao bloqueia: conclua e registre em decisions. '
        f'Contexto alvo: aviso {config["context"]["warn_tokens"]}, transicao {config["context"]["transition_tokens"]}. '
        'Retorne handoff antes de crescer desnecessariamente; este alvo nao e quota do plano. '
        'Somente JSON conforme schema; nao assuma aprovacao humana.'])


# --- Troca automatica de modelo por cota (decisao do dono, 03/11/2026) ---------------
# A ordem vem de config/pipeline.json ("fallback.chain"): cada fase comeca no provedor que o
# manifesto aprovou e, se ele estiver esgotado, segue a ordem. Claude: esgotado ANTES de estourar,
# pelo arquivo de uso que o Habblaud grava (five_hour >= limiar). Qualquer candidato: se a
# execucao falhar por cota (429, limite de sessao, RESOURCE_EXHAUSTED), fica esgotado ate a janela
# renovar e a mesma fase roda com o proximo. A escolha e feita na hora: task.json nao muda (CP1
# continua valendo) e o recibo da fase registra quem de fato rodou. Revisao pode cair para
# qualquer candidato (decisao do dono). Timeout nunca troca de candidato: segue a regra de
# nao redisparar e exigir inspecao.
QUOTA_ERROR = re.compile(r'usage_limit|session limit|usage limit|rate.?limit|RESOURCE_EXHAUSTED|quota|\b429\b', re.I)


def quota_state_path():
    return ROOT / '.local' / 'quota.json'


def candidates_for(provider, config):
    """Candidatos (provedor, modelo do agy ou None) na ordem, a partir do provedor do manifesto."""
    chain = [tuple((item.split(':', 1) + [None])[:2]) for item in config.get('fallback', {}).get('chain', [])]
    first = (provider, config.get('agy_model') if provider == 'agy' else None)
    if first not in chain:
        return [first] + chain
    return chain[chain.index(first):]


def claude_usage(config):
    """(utilizacao da janela de 5h, renova em epoch s) do arquivo de uso do Habblaud, ou (None, None)."""
    path = Path(os.path.expanduser(config.get('fallback', {}).get('claude_usage_file', '~/.habblaud/usage/.claude.json')))
    try:
        five = (read(path) or {}).get('five_hour') or {}
        return five.get('utilization'), five.get('resets_at')
    except (OSError, ValueError):
        return None, None


def exhausted_until(key, config, clock):
    """Ate quando o candidato esta esgotado (epoch s), ou 0."""
    state = read(quota_state_path()) if quota_state_path().exists() else {}
    until = float(state.get(key, 0) or 0)
    if key == 'claude':
        used, resets = claude_usage(config)
        limit = config.get('fallback', {}).get('claude_threshold', 85)
        if used is not None and used >= limit and resets:
            until = max(until, float(resets))
    return until if until > clock() else 0


def mark_exhausted(key, config, clock):
    hours = config.get('fallback', {}).get('exhausted_hours', 5)
    until = clock() + hours * 3600
    if key == 'claude':
        resets = claude_usage(config)[1]
        if resets and float(resets) > clock():
            until = float(resets)
    path = quota_state_path()
    state = read(path) if path.exists() else {}
    state[key] = until
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, state)


def quota_failure(run):
    """A execucao que falhou foi por cota? Le so o erro (stderr e o fim do stdout)."""
    text = ''
    for name in ('stderr.log', 'stdout.jsonl'):
        p = run / name
        if p.exists():
            text += p.read_text(encoding='utf-8', errors='replace')[-20000:]
    return bool(QUOTA_ERROR.search(text))


# Fases em que uma negativa do agy e repetida uma vez sem passar pelo dono. No dev tambem:
# quando a negativa chega o processo ja terminou, e a repeticao continua do worktree como um
# RECUPERAR para o dev faria; validate e review conferem depois. A segunda negativa bloqueia.
AGY_RETRY_PHASES = ('plan', 'prototype', 'dev', 'review', 'docs')


def agy_denial(run):
    """Primeira linha do erro de permissao do agy nesta execucao (do transcript da conversa), ou ''."""
    try:
        conv = read(run / 'stdout.jsonl').get('conversation_id')
        log = Path.home() / '.gemini/antigravity-cli/brain' / conv / '.system_generated/logs/transcript_full.jsonl'
        for line in log.read_text(encoding='utf-8').splitlines():
            step = json.loads(line)
            if step.get('status') == 'ERROR' and 'permission' in str(step.get('error', '')):
                return str(step['error']).splitlines()[0][:400]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return ''


def stash_attempt(run, suffix):
    """Guarda os logs de uma tentativa e libera os nomes para a proxima."""
    for name in ('stdout.jsonl', 'stderr.log', 'process.json', 'result.json'):
        p = run / name
        if p.exists():
            p.rename(run / (name + '.' + suffix))


def invoke_fallback(provider, prompt, repo, run, phase, config, invoke_one=None, clock=None, denial=None):
    invoke_one = invoke_one or invoke
    denial = denial or agy_denial
    clock = clock or (lambda: dt.datetime.now(dt.timezone.utc).timestamp())
    tried = []
    for cand, model in candidates_for(provider, config):
        key = cand + (':' + model if model else '')
        if exhausted_until(key, config, clock):
            tried.append({'candidato': key, 'pulado': 'esgotado'})
            continue
        attempt_config = dict(config, agy_model=model) if model else config
        attempt_prompt, retried, quota = prompt, None, False
        while True:
            try:
                response, meta = invoke_one(cand, attempt_prompt, repo, run, phase, attempt_config)
                break
            except RuntimeError as error:
                if str(error).startswith('Timeout'):
                    raise
                if quota_failure(run):
                    quota = True
                    break
                # agy barrado: repete UMA vez dizendo o que foi negado (ver AGY_RETRY_PHASES).
                if retried is None and cand == 'agy' and phase in AGY_RETRY_PHASES and str(error).startswith('agy retornou'):
                    retried = denial(run) or 'acao sem permissao'
                    stash_attempt(run, 'negada')
                    attempt_prompt = (prompt + '\n\nA tentativa anterior desta fase foi encerrada por esta negativa: '
                                      + retried + '\nNao repita esse comando nem variacoes dele; rode o git sem -C, '
                                      'no diretorio atual, sem $\'...\' nem expansoes do shell, ou leia os arquivos com read_file.')
                    continue
                raise
        if quota:
            mark_exhausted(key, config, clock)
            tried.append({'candidato': key, 'falhou': 'cota'})
            stash_attempt(run, key.replace(':', '_'))
            continue
        meta = dict(meta or {})
        meta['executado_por'] = key
        if retried:
            meta['repetida_apos_negativa'] = retried
        if tried:
            meta['fallback'] = tried
        return response, meta
    raise RuntimeError('Todos os candidatos estao sem cota: ' + ', '.join(t['candidato'] for t in tried))


def execute(task, executor=invoke_fallback):
    folder, local = task_paths(task)
    config = read(ROOT / 'config/pipeline.json')
    with lock(local):
        manifest = read(folder / 'task.json')
        state = read(local / 'state.json')
        repo = repository(manifest)
        if state['phase'] == 'awaiting_CP0':
            # CP0 foi fundido no CP1 (especificacao da funcionalidade + manifesto).
            state['phase'] = 'awaiting_CP1'
            save(local / 'state.json', state)
        gate = state['phase'].removeprefix('awaiting_')
        if state['phase'].startswith('awaiting_') and gate in GATES and approved(folder, local, manifest, gate):
            # Aprovacao gravada e valida, mas o estado nao avancou (interrupcao
            # entre as gravacoes da entrega): conclui a mesma transicao.
            state['phase'] = next_phase_after(gate, manifest, state, folder)
            save(local / 'state.json', state)
        pending = unsettled_runs(task, state)
        if pending:
            state.update(phase='blocked', blocked_phase=pending[-1][1].get('phase', state.get('blocked_phase', state['phase'])),
                         reason='Execucao anterior sem desfecho; inspecionar processos e usar recover',
                         last_run=str(pending[-1][0].relative_to(ROOT)))
            save(local / 'state.json', state)
            write_handoff(folder, state)
            return state
        # Bloqueio de validacao da regra antiga (antes de BackToDev, 04/11/2026): segue a regra nova.
        legacy = ('RuntimeError: Validacao falhou', 'RuntimeError: Check alterou a arvore; inspecionar')
        if (state['phase'] == 'blocked' and state.get('blocked_phase') == 'validate' and state.get('last_run')
                and str(state.get('reason', '')).startswith(legacy)
                and state.get('rounds', 0) + 1 < config['max_rounds']):
            state.update(phase='dev', rounds=state.get('rounds', 0) + 1,
                         auto_failure={'reason': state['reason'], 'run': state['last_run'], 'at': now()})
            state.pop('blocked_phase', None)
            state.pop('reason', None)
        if state['phase'] in ('blocked', 'ready_for_draft', 'closed') or state['phase'].startswith('awaiting_'):
            return state
        if git(repo, 'branch', '--show-current') != manifest['branch']:
            raise RuntimeError('Branch atual difere da aprovada')
        if git(repo, 'rev-parse', manifest['base_ref']) != manifest['base_sha']:
            raise RuntimeError('Base mudou. Replanejar e aprovar novamente.')
        if manifest['providers']['dev'] == manifest['providers']['review']:
            raise RuntimeError('Piloto exige provedores diferentes para dev e review')
        slices = 0
        while True:
            phase = state['phase']
            if phase.startswith('awaiting_') or phase in ('blocked', 'ready_for_draft'):
                break
            if phase not in ('plan', 'validate') and slices >= config['max_slices_per_run']:
                state.update(blocked_phase=phase, phase='blocked',
                             reason='Limite de fatias de agente por execucao; reavaliar antes de recuperar')
                break
            if not approved(folder, local, manifest, 'CP1'):
                # Sem CP1 valido, aguarda aprovacao da funcionalidade (task-spec.md + manifesto).
                # Se estava em fase posterior ao plan, retoma de la apos aprovar.
                if phase not in ('plan', 'awaiting_CP1', 'awaiting_CP0'):
                    state.setdefault('resume_phase', phase)
                state['phase'] = 'awaiting_CP1'
                break
            if manifest.get('prototype') and phase in ('dev', 'validate', 'review', 'docs') and not approved(folder, local, manifest, 'PROTOTIPO'):
                state['phase'] = 'awaiting_PROTOTIPO'
                break
            run = ROOT / '.local/runs' / task / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
            run.mkdir(parents=True)
            provider = 'script' if phase == 'validate' else manifest['providers'].get(phase, manifest['providers']['plan'])
            receipt = {'phase': phase, 'provider': provider, 'started_at': now(), 'status': 'running',
                       'process_protocol': 1,
                       'runner': {'pid': os.getpid(), 'host': socket.gethostname()}}
            save(run / 'receipt.json', receipt)
            state['active_run'] = str(run.relative_to(ROOT))
            save(local / 'state.json', state)
            try:
                input_hash = digest((folder / 'task.json').read_bytes())
                protected = {p: digest(p.read_bytes()) for p in [folder / 'task-spec.md', folder / 'task.json'] + list((local / 'approvals').glob('*.json'))}
                if phase != 'plan' and (folder / 'plan.md').exists():
                    protected[folder / 'plan.md'] = digest((folder / 'plan.md').read_bytes())
                if (folder / DECISOES).exists():
                    protected[folder / DECISOES] = digest((folder / DECISOES).read_bytes())
                before = fingerprint(repo)
                if phase == 'validate':
                    check_scope(repo, manifest)
                    receipt['tests'] = run_checks(repo, manifest, run, config)
                    state.pop('auto_failure', None)
                    state['validated_tree'] = fingerprint(repo)
                    state['validation'] = {'receipt': str((run / 'validation.json').relative_to(ROOT)),
                                           'tests': receipt['tests'], 'fingerprint': state['validated_tree']}
                    state['phase'] = 'review'
                else:
                    if phase != 'plan':
                        slices += 1
                    diff = run / 'diff.patch'
                    diff.write_bytes(git(repo, 'diff', manifest['base_sha'], '--', binary=True))
                    prompt = build_prompt(folder, manifest, state, config) + '\nDiff contra base: ' + str(diff)
                    response, meta = executor(provider, prompt, repo, run, phase, config)
                    receipt.update(meta)
                    validate_response(response)
                    if any(not p.exists() or digest(p.read_bytes()) != value for p, value in protected.items()):
                        raise RuntimeError('Worker alterou entrada/aprovacao protegida; precisa revisao humana')
                    if input_hash != digest((folder / 'task.json').read_bytes()):
                        raise RuntimeError('Manifesto alterado durante execucao')
                    after = fingerprint(repo)
                    if git(repo, 'branch', '--show-current') != manifest['branch']:
                        raise RuntimeError('Worker trocou de branch; inspecao humana obrigatoria')
                    if phase != 'dev' and after != before:
                        raise RuntimeError('Fase somente leitura alterou codigo')
                    state['handoff'] = {k: response[k] for k in ('summary', 'next_step', 'decisions')}
                    if response['status'] == 'blocked':
                        raise RuntimeError(response['summary'])
                    for artifact in response['artifacts']:
                        p = (repo / artifact).resolve()
                        if not p.is_relative_to(repo) or not p.exists():
                            raise RuntimeError('Artefato ausente/fora do repo: ' + artifact)
                    if phase == 'dev':
                        check_scope(repo, manifest)
                        # Sem mudanca nova mas dizendo que terminou: o trabalho ja esta no worktree (ex.: run
                        # anterior interrompido), segue para a validacao. So o "continue" sem mudanca e travamento.
                        if before == after and response['status'] == 'continue':
                            raise RuntimeError('Dev sem progresso verificavel; nao redisparar automaticamente')
                        if not git(repo, 'diff', '--name-only', manifest['base_sha'], '--') and not git(repo, 'ls-files', '--others', '--exclude-standard'):
                            raise RuntimeError('Dev nao deixou alteracao em relacao a base')
                        state['phase'] = 'dev' if response['status'] == 'continue' else 'validate'
                    elif response['status'] == 'continue':
                        previous = state.get('document_progress')
                        progress = digest(response['document'].encode())
                        if not response['document'].strip() or progress == previous:
                            raise RuntimeError('Fatia sem progresso documental')
                        state['document_progress'] = progress
                        (folder / (phase + '-partial.md')).write_text(response['document'], encoding='utf-8')
                    else:
                        if not response['document'].strip():
                            raise RuntimeError('Documento obrigatorio ausente')
                        name = {'plan': 'plan.md', 'prototype': 'prototipo/index.html', 'review': 'review-report.md', 'docs': 'documentacao-tecnica.md'}[phase]
                        (folder / name).parent.mkdir(parents=True, exist_ok=True)
                        (folder / name).write_text(response['document'], encoding='utf-8')
                        if phase == 'plan':
                            state['phase'] = 'prototype' if manifest.get('prototype') else 'dev'
                        elif phase == 'prototype':
                            state['phase'] = 'awaiting_PROTOTIPO'
                        elif phase == 'review':
                            if state.get('validated_tree') != after:
                                raise RuntimeError('Arvore mudou depois dos testes; revisao nao aceita')
                            if response['verdict'] == 'approved':
                                state['phase'] = 'docs'
                            elif response['verdict'] == 'changes_required':
                                state['rounds'] = state.get('rounds', 0) + 1
                                if state['rounds'] >= config['max_rounds']:
                                    raise RuntimeError('Limite de rodadas atingido; reavaliar escopo/plano')
                                state['phase'] = 'dev'
                            else:
                                raise RuntimeError('Veredito de review invalido')
                        else:
                            if state.get('validated_tree') != after:
                                raise RuntimeError('Arvore mudou apos validacao')
                            state['phase'] = 'ready_for_draft'
                receipt['status'] = 'completed'
            except BackToDev as error:
                rounds = state.get('rounds', 0) + 1
                if rounds >= config['max_rounds']:
                    reason = f'Limite de rodadas atingido ({rounds}); {error}'
                    state.update(phase='blocked', blocked_phase=phase, reason=reason, last_run=str(run.relative_to(ROOT)))
                    receipt.update(status='blocked', reason=reason)
                else:
                    state.update(phase='dev', rounds=rounds, last_run=str(run.relative_to(ROOT)),
                                 auto_failure={'reason': str(error), 'run': str(run.relative_to(ROOT)), 'at': now()})
                    receipt.update(status='failed', reason=str(error), next='dev (automatico)')
            except (Exception, KeyboardInterrupt) as error:
                reason = type(error).__name__ + ': ' + str(error)
                state.update(phase='blocked', blocked_phase=phase, reason=reason, last_run=str(run.relative_to(ROOT)))
                receipt.update(status='blocked', reason=reason)
            receipt['ended_at'] = now()
            receipt['next_phase'] = state['phase']
            save(run / 'receipt.json', receipt)
            state.pop('active_run', None)
            save(local / 'state.json', state)
            write_handoff(folder, state)
        save(local / 'state.json', state)
        write_handoff(folder, state)
    if state['phase'] == 'ready_for_draft':
        review_environment('preparar', manifest.get('entrega') or task)
    return state


def review_environment(action, alvo):
    """Ambiente de revisao (MR draft, servidores, roteiro) em segundo plano; falha nao afeta a tarefa.

    preparar so age quando todas as tarefas da entrega estao em ready_for_draft (ver ambiente_revisao.py).
    """
    if os.environ.get('PIPELINE_SEM_AMBIENTE'):
        return
    logs = ROOT / '.local' / 'ambientes'
    try:
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / (alvo + '.log')).open('a', encoding='utf-8') as out:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / 'ambiente_revisao.py'), action, alvo],
                             cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError:
        pass


def recover(task, target=None, stale_lock=False, confirm=None, source='interactive-human', evidence=None):
    # confirm=None: terminal humano. O hook de chat passa confirm e a evidencia
    # da mensagem do dono; as checagens de processo abaixo valem igual.
    if confirm is None and not sys.stdin.isatty():
        raise RuntimeError('Recuperacao exige inspecao humana de processos e worktree')
    folder, local = task_paths(task)
    # Same kernel mutex as run/approve: no other invocation can take the task
    # between inspection, confirmation and replacement of stale metadata.
    with mutex(local):
        lock_path = local / 'runner.lock'
        owner = read(lock_path) if lock_path.exists() else None
        if owner is not None:
            if not stale_lock:
                raise RuntimeError('Lock existente; usar recover --stale-lock apos inspecao')
            if process_alive(owner) is not False:
                raise RuntimeError('Dono do lock vivo ou desconhecido; nao recuperar')
        state = read(local / 'state.json')
        pending = unsettled_runs(task, state)
        # Reabertura pos-draft (bug/pedido do teste manual): so volta ao plano, que exige
        # novo CP1 da entrega. Nao gasta rodada; o limite segue valendo nos ciclos seguintes.
        reabrir = state['phase'] == 'ready_for_draft' and not pending and owner is None
        if reabrir and target != 'plan':
            raise RuntimeError('Tarefa concluida so reabre para o plano (novo CP1): RECUPERAR <tarefa> plan')
        if state['phase'] != 'blocked' and not pending and owner is None and not reabrir:
            raise RuntimeError('Tarefa nao bloqueada nem interrompida')
        for run, receipt, proc in pending:
            if not proc and receipt.get('status') == 'running' and receipt.get('process_protocol') != 1:
                raise RuntimeError('Liveness desconhecida: execucao legada sem registro de processo em ' + str(run))
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled'):
                if process_alive(proc) is not False:
                    raise RuntimeError('Worker/check vivo ou desconhecido em ' + str(run))
        original = state.get('blocked_phase', pending[-1][1].get('phase') if pending else state['phase'])
        destination = target or ('validate' if original in ('validate', 'review', 'docs') else original)
        allowed = {'plan'} if reabrir else {'dev', 'validate', original} - {'review', 'docs'}
        if destination not in allowed or destination not in ('plan', 'prototype', 'dev', 'validate'):
            raise RuntimeError('Destino de recuperacao invalido')
        if original in ('plan', 'prototype') and destination != original:
            raise RuntimeError('Concluir planejamento/prototipo antes de recuperar para desenvolvimento')
        # Rodada = ciclo de correcao (validate/review -> dev). Continuar um dev interrompido
        # (limite de fatias, crash) nao gasta rodada; o limite continua valendo sobre o total.
        # Review com changes_required ja somou a rodada no execute antes de bloquear pelo limite.
        ja_contada = original == 'review' and 'Limite de rodadas' in str(state.get('reason', ''))
        rounds = state.get('rounds', 0) + (1 if destination == 'dev' and original != 'dev' and not ja_contada else 0)
        if rounds >= read(ROOT / 'config/pipeline.json')['max_rounds']:
            raise RuntimeError('Limite de rodadas atingido; reavaliar escopo/plano')
        print(json.dumps({'state': state, 'lock': owner, 'destination': destination,
                          'pending': [{'run': str(r), 'process': p} for r, _, p in pending]}, ensure_ascii=False, indent=2))
        confirmed = confirm() if confirm else input('Inspecionou worktree, filhos dos processos e causa? RECUPERAR: ') == 'RECUPERAR'
        if not confirmed:
            raise RuntimeError('Recuperacao cancelada')
        # Recheck immediately before mutation; a reused PID must never be stolen.
        if owner is not None and process_alive(owner) is not False:
            raise RuntimeError('Dono do lock vivo ou desconhecido; nao recuperar')
        for run, _, proc in pending:
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled') and process_alive(proc) is not False:
                raise RuntimeError('Worker/check vivo ou desconhecido em ' + str(run))
        event = {'at': now(), 'source': source, 'evidence': evidence or {}, 'from_phase': original,
                 'to_phase': destination, 'previous_state': state.copy(), 'stale_lock': owner,
                 'runs': [{'path': str(r.relative_to(ROOT)), 'receipt': rec, 'process': p} for r, rec, p in pending]}
        recovery_id = uuid.uuid4().hex
        save(local / 'recoveries' / (recovery_id + '.json'), event)
        # Keep state blocked until every prior record is settled. A crash during
        # recovery cannot silently open the task for dispatch.
        state.update(phase='blocked', blocked_phase=destination, reason='Recuperacao em andamento', rounds=rounds)
        save(local / 'state.json', state)
        for run, receipt, proc in pending:
            if receipt.get('status') == 'running':
                receipt.update(status='recovered', recovery_id=recovery_id)
                save(run / 'receipt.json', receipt)
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled'):
                proc.update(status='recovered', recovery_id=recovery_id)
                save(run / 'process.json', proc)
        if owner is not None:
            lock_path.rename(local / 'recoveries' / (recovery_id + '.lock.json'))
        if reabrir:
            # Plano refeito igual ao anterior nao pode herdar o CP1 antigo; sem este registro
            # a aprovacao da entrega inteira deixa de valer (approved exige todos os membros).
            for gate in GATES:
                approval = local / 'approvals' / (gate + '.json')
                if approval.exists():
                    approval.rename(local / 'recoveries' / (recovery_id + '.' + gate + '.json'))
        for key in ('validated_tree', 'validation', 'active_run', 'reason', 'blocked_phase', 'document_progress'):
            state.pop(key, None)
        state.update(phase=destination, rounds=rounds, last_recovery=recovery_id)
        save(local / 'state.json', state)
        write_handoff(folder, state)
        return state


def request_correction(task, note, source='chat-usuario', evidence=None):
    """Registra curadoria/correcao do dono em DECISOES-HUMANAS.md e volta a tarefa para dev sem perder o CP1."""
    clean = (note or '').strip()
    if not clean:
        raise ValueError('Informe o que corrigir')
    folder, local = task_paths(task)
    with mutex(local):
        lock_path = local / 'runner.lock'
        owner = read(lock_path) if lock_path.exists() else None
        if owner is not None and process_alive(owner) is not False:
            raise RuntimeError('Dono do lock vivo ou desconhecido; nao reabrir para correcao')
        state = read(local / 'state.json')
        pending = unsettled_runs(task, state)
        for run, receipt, proc in pending:
            if not proc and receipt.get('status') == 'running' and receipt.get('process_protocol') != 1:
                raise RuntimeError('Liveness desconhecida em ' + str(run))
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled') and process_alive(proc) is not False:
                raise RuntimeError('Worker/check vivo ou desconhecido em ' + str(run))
        if state.get('phase') not in ('ready_for_draft', 'blocked'):
            raise RuntimeError('Correcao humana exige tarefa em ready_for_draft ou blocked (atual: ' + str(state.get('phase')) + ')')
        dec_path = folder / DECISOES
        existing = dec_path.read_text(encoding='utf-8').rstrip() + '\n\n' if dec_path.exists() else ''
        header = (f'## Curadoria de {DONO} ({now()})\n'
                  f'Prevalece sobre review-report.md: pontos removidos abaixo foram descartados por {DONO} '
                  'e nao devem ser cobrados; pontos mantidos/acrescentados sao obrigatorios.\n\n')
        dec_path.write_text(existing + header + clean + '\n', encoding='utf-8')
        recovery_id = uuid.uuid4().hex
        event = {'at': now(), 'source': source, 'evidence': evidence or {}, 'from_phase': state.get('phase'),
                 'to_phase': 'dev', 'note': clean, 'previous_state': state.copy(), 'stale_lock': owner}
        save(local / 'recoveries' / (recovery_id + '.json'), event)
        for run, receipt, proc in pending:
            if receipt.get('status') == 'running':
                receipt.update(status='recovered', recovery_id=recovery_id)
                save(run / 'receipt.json', receipt)
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled'):
                proc.update(status='recovered', recovery_id=recovery_id)
                save(run / 'process.json', proc)
        if owner is not None:
            lock_path.rename(local / 'recoveries' / (recovery_id + '.lock.json'))
        for key in ('validated_tree', 'validation', 'active_run', 'reason', 'blocked_phase', 'document_progress'):
            state.pop(key, None)
        state.update(phase='dev', rounds=0, last_recovery=recovery_id)
        save(local / 'state.json', state)
        write_handoff(folder, state)
        return state


GATES = ('CP1', 'PROTOTIPO')


def entrega_tasks(target):
    """Tarefas de uma entrega (manifest['entrega']) ou a tarefa avulsa.

    Tarefa que pertence a uma entrega nao e aprovada sozinha: o gate e da entrega.
    """
    task_paths(target)
    found = sorted(p.parent.name for p in (ROOT / 'tarefas').glob('*/task.json')
                   if read(p).get('entrega') == target)
    if found:
        return found
    if (ROOT / 'tarefas' / target / 'task.json').exists():
        entrega = read(ROOT / 'tarefas' / target / 'task.json').get('entrega')
        if entrega:
            raise RuntimeError(target + ' pertence a entrega ' + entrega + '; aprovar a entrega inteira')
        return [target]
    raise RuntimeError('Tarefa ou entrega desconhecida: ' + target)


def entrega_hash(entrega, gate, tasks=None, include_plan=False):
    """Hash combinado do gate para TODAS as tarefas da entrega, independente da fase."""
    items = []
    for task in tasks or entrega_tasks(entrega):
        folder, _ = task_paths(task)
        items.append([task, gate_hash(folder, read(folder / 'task.json'), gate, include_plan=include_plan)])
    return digest(json.dumps([gate, items]).encode())


def gate_status(target):
    """Somente leitura: hash por tarefa e hash combinado do gate pendente."""
    tasks = entrega_tasks(target)
    items = []
    for task in tasks:
        folder, local = task_paths(task)
        manifest, state = read(folder / 'task.json'), read(local / 'state.json')
        phase = state['phase']
        gate = phase.removeprefix('awaiting_') if phase.startswith('awaiting_') else None
        items.append({'task': task, 'phase': phase, 'gate': gate,
                      'hash': gate_hash(folder, manifest, gate) if gate in GATES else None})
    # Membro ja concluido (ready_for_draft) de entrega reaberta entra no gate pendente
    # dos demais: o hash combinado cobre o plano dele, mas a fase nao muda.
    pending_gates = {i['gate'] for i in items if i['gate']}
    if len(pending_gates) == 1:
        gate = next(iter(pending_gates))
        for i in items:
            if i['phase'] == 'ready_for_draft':
                folder, _ = task_paths(i['task'])
                i.update(gate=gate, hash=gate_hash(folder, read(folder / 'task.json'), gate), concluida=True)
    gates = {i['gate'] for i in items}
    combined = None
    if len(gates) == 1 and None not in gates and all(i['hash'] for i in items):
        combined = entrega_hash(target, gates.pop(), tasks)
    return {'target': target, 'tasks': items, 'combined_hash': combined}


def next_phase_after(gate, manifest, state, folder=None):
    if 'resume_phase' in state:
        return state.pop('resume_phase')
    if gate == 'CP1':
        if folder is not None and not (folder / 'plan.md').exists():
            return 'plan'
        if manifest.get('prototype'):
            return 'prototype'
    return 'dev'


def register_approvals(target, gate, source, evidence=None, expected_hash=None):
    """Grava a aprovacao de todas as tarefas da entrega, ou de nenhuma.

    expected_hash e obrigatorio: vincula a decisao ao conteudo que o dono viu.
    Primeiro gravam-se todas as aprovacoes, depois os estados; uma interrupcao no
    meio e retomada pelo execute (awaiting_ + aprovacao valida avanca sozinho).
    """
    if gate not in GATES:
        raise RuntimeError('Gate invalido: ' + gate)
    if not expected_hash or len(expected_hash) < 8:
        raise RuntimeError('Informe o hash do gate (minimo 8 caracteres) mostrado por "pipeline.py gate"')
    tasks = entrega_tasks(target)
    with contextlib.ExitStack() as stack:
        for task in tasks:
            stack.enter_context(lock(task_paths(task)[1]))
        if entrega_tasks(target) != tasks:
            raise RuntimeError('Composicao da entrega mudou durante a aprovacao; revisar novamente')
        status = gate_status(target)
        waiting = [i for i in status['tasks'] if i['phase'] not in ('awaiting_' + gate, 'ready_for_draft')]
        if waiting or all(i['phase'] == 'ready_for_draft' for i in status['tasks']):
            raise RuntimeError('Nem todas as tarefas aguardam ' + gate + ': ' +
                               ', '.join(i['task'] + '=' + i['phase'] for i in waiting))
        if not (status['combined_hash'] or '').startswith(expected_hash.lower()):
            raise RuntimeError('Hash informado nao corresponde ao gate atual; revisar novamente')
        for item in status['tasks']:
            _, local = task_paths(item['task'])
            save(local / 'approvals' / (gate + '.json'),
                 {'hash': item['hash'], 'at': now(), 'source': source, 'target': target, 'tasks': tasks,
                  'combined_hash': status['combined_hash'], 'evidence': evidence or {}})
        for item in status['tasks']:
            if item.get('concluida'):
                continue
            folder, local = task_paths(item['task'])
            manifest, state = read(folder / 'task.json'), read(local / 'state.json')
            state['phase'] = next_phase_after(gate, manifest, state, folder)
            save(local / 'state.json', state)
            write_handoff(folder, state)
    return status


def approve(task):
    if not sys.stdin.isatty():
        raise RuntimeError('Aprovacao exige terminal humano interativo; agente nao pode aprovar por pipe')
    status = gate_status(task)
    gates = {i['gate'] for i in status['tasks']}
    if len(gates) != 1 or not gates <= set(GATES):
        raise RuntimeError('Nao ha checkpoint aguardando em todas as tarefas: ' + json.dumps(status['tasks'], ensure_ascii=False))
    gate = gates.pop()
    print('Revise criterios, plano, base, escopo e checks de', ', '.join(i['task'] for i in status['tasks']),
          '\nGate:', gate, '\nHash:', status['combined_hash'])
    if input('Digite APROVAR ' + gate + ': ') != 'APROVAR ' + gate:
        raise RuntimeError('Aprovacao nao concedida')
    register_approvals(task, gate, 'interactive-human', expected_hash=status['combined_hash'])
    results = [execute(t) for t in entrega_tasks(task)]
    return results[0] if len(results) == 1 else {'phase': 'entrega', 'tasks': results}


def initialize(task, repo_name, source, base_ref, scope, checks, size='P', prototype=False, entrega=None):
    folder, local = task_paths(task)
    config = read(ROOT / 'config/pipeline.json')
    with lock(local):
        if (folder / 'task.json').exists() or (local / 'state.json').exists():
            raise RuntimeError('Tarefa ja cadastrada')
        manifest = {'schema_version': 1, 'repo': repo_name, 'base_ref': base_ref, 'scope': scope,
                    'checks': checks, 'size': size, 'prototype': prototype, 'providers': config['providers']}
        if entrega:
            task_paths(entrega)
            for p in (ROOT / 'tarefas').glob('*/task.json'):
                other = read(p)
                if other.get('entrega') == entrega and bool(other.get('prototype')) != bool(prototype):
                    raise RuntimeError('Tarefas da mesma entrega devem ter o mesmo --prototype (gate unico)')
            manifest['entrega'] = entrega
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', repo_name):
            raise ValueError('Use alias simples do repo em config/local.json')
        if any(Path(x).is_absolute() or '..' in Path(x).parts or not x.strip('./\\') for x in scope):
            raise ValueError('Escopo deve listar arquivos/diretorios relativos explicitos')
        repo = repository(manifest)
        if git(repo, 'status', '--porcelain'):
            raise RuntimeError('Cadastro automatico requer checkout limpo; preserve e registre alteracoes existentes antes')
        manifest['base_sha'] = git(repo, 'rev-parse', base_ref)
        manifest['branch'] = git(repo, 'branch', '--show-current')
        if manifest['branch'] in ('', 'main', 'master', 'homologacao'):
            raise RuntimeError('Selecione uma branch da tarefa antes de cadastrar')
        folder.mkdir(parents=True, exist_ok=True)
        spec = folder / 'task-spec.md'
        if spec.exists() and spec.resolve() != Path(source).resolve():
            raise RuntimeError('task-spec existente; nao sobrescrever')
        if spec.resolve() != Path(source).resolve():
            spec.write_text(Path(source).read_text(encoding='utf-8'), encoding='utf-8')
        save(folder / 'task.json', manifest)
        state = {'phase': 'awaiting_CP1', 'created_at': now(), 'rounds': 0}
        save(local / 'state.json', state)
        write_handoff(folder, state)
    return state


def close(task, reason):
    """Encerra a tarefa no runner (entregue ou abandonada): so registra, sem apagar nada.

    O estado anterior fica em .local/tasks/<id>/archive/; a fase vira 'closed' com o motivo e a data.
    Recusa com run em andamento (inspecionar antes). Tarefa fechada nao roda mais: reabrir e um
    `init`/`attach` novo, decisao do dono.
    """
    if not reason or not reason.strip():
        raise ValueError('Informe o motivo do encerramento (--motivo)')
    folder, local = task_paths(task)
    with lock(local):
        state = read(local / 'state.json')
        if state.get('phase') == 'closed':
            return state
        if unsettled_runs(task, state):
            raise RuntimeError('Ha execucao sem desfecho; inspecionar processos antes de encerrar')
        archive = local / 'archive'
        archive.mkdir(exist_ok=True)
        save(archive / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-state.json'), state)
        closed = {'phase': 'closed', 'closed_phase': state.get('phase'), 'closed_at': now(),
                  'reason': reason.strip(), 'created_at': state.get('created_at'),
                  'handoff': state.get('handoff', {})}
        save(local / 'state.json', closed)
    review_environment('encerrar', read(folder / 'task.json').get('entrega') or task)
    return closed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('task'); init.add_argument('--repo', required=True); init.add_argument('--spec', required=True)
    init.add_argument('--base', required=True); init.add_argument('--scope', action='append', required=True)
    init.add_argument('--check', action='append', required=True, help='argv JSON; aprovado junto ao plano')
    init.add_argument('--size', choices=['P', 'M', 'G'], default='P'); init.add_argument('--prototype', action='store_true')
    init.add_argument('--entrega', help='Agrupa tarefas aprovadas num unico gate (ex.: 3964)')
    gate = sub.add_parser('gate', help='Somente leitura: hashes do gate pendente da tarefa/entrega'); gate.add_argument('target')
    for name in ('run', 'approve', 'status', 'recover'):
        q = sub.add_parser(name); q.add_argument('task')
        if name == 'recover':
            q.add_argument('--to', choices=['dev', 'validate', 'plan'], help='Fase apos inspecao humana; nao ignora gates. plan reabre tarefa concluida (novo CP1)')
            q.add_argument('--stale-lock', action='store_true', help='Arquivar lock somente com dono comprovadamente encerrado')
    close_cmd = sub.add_parser('close', help='Encerrar tarefa entregue/abandonada: arquiva o estado, sem apagar nada')
    close_cmd.add_argument('task'); close_cmd.add_argument('--motivo', required=True)
    attach = sub.add_parser('attach', help='Reconstruir controle local de tarefa versionada em outra maquina')
    attach.add_argument('task')
    attach.add_argument('--phase', choices=['plan', 'dev', 'validate'], default='dev')
    a = p.parse_args()
    try:
        if a.command == 'init':
            result = initialize(a.task, a.repo, a.spec, a.base, a.scope, [json.loads(c) for c in a.check], a.size, a.prototype, a.entrega)
        elif a.command == 'gate':
            print(json.dumps(gate_status(a.target), ensure_ascii=False, indent=2))
            return 0
        elif a.command == 'run':
            result = execute(a.task)
        elif a.command == 'approve':
            result = approve(a.task)
        elif a.command == 'attach':
            folder, local = task_paths(a.task)
            with lock(local):
                if (local / 'state.json').exists():
                    raise RuntimeError('Estado local ja existe; nao sobrescrever')
                manifest = read(folder / 'task.json')
                repo = repository(manifest)
                if git(repo, 'branch', '--show-current') != manifest['branch'] or git(repo, 'rev-parse', manifest['base_ref']) != manifest['base_sha']:
                    raise RuntimeError('Checkout/base nao correspondem ao manifesto portatil')
                handoff = (folder / 'HANDOFF.md').read_text(encoding='utf-8') if (folder / 'HANDOFF.md').exists() else ''
                if len(handoff) > 10000:
                    raise RuntimeError('Resuma o handoff antes de transferir a tarefa')
                result = {'phase': 'plan' if a.phase == 'plan' else 'awaiting_CP1',
                          'resume_phase': a.phase, 'rounds': 0, 'created_at': now(),
                          'handoff': {'summary': handoff, 'next_step': 'Revalidar aprovacao nesta maquina e continuar fase ' + a.phase, 'decisions': 'Sem aprovacao transferida automaticamente'}}
                save(local / 'state.json', result)
        elif a.command == 'recover':
            result = recover(a.task, a.to, a.stale_lock)
        elif a.command == 'close':
            result = close(a.task, a.motivo)
        else:
            _, local = task_paths(a.task)
            result = read(local / 'state.json')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('phase') == 'blocked' else 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
