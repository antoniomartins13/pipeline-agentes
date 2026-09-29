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


@contextlib.contextmanager
def lock(local):
    path = local / 'runner.lock'
    with mutex(local):
        try:
            with path.open('x', encoding='utf-8') as f:
                json.dump({'pid': os.getpid(), 'host': socket.gethostname(), 'started_at': now()}, f)
        except FileExistsError:
            raise RuntimeError('Lock existente. Usar recover --stale-lock apos inspecao humana.')
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


def gate_hash(folder, manifest, gate):
    # Include manifest (base, providers, checks, scope) and exact task inputs.
    material = json.dumps(manifest, sort_keys=True).encode()
    names = ['task-spec.md']
    if gate != 'CP0':
        names += ['plan.md']
    if gate == 'PROTOTIPO':
        names += ['prototipo/index.html']
    for name in names:
        material += name.encode() + (folder / name).read_bytes()
    return digest(material)


def approved(folder, local, manifest, gate):
    p = local / 'approvals' / (gate + '.json')
    return p.exists() and read(p).get('hash') == gate_hash(folder, manifest, gate)


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
            args += ['--add-dir', str(run.resolve()), str(task_folder.resolve())]
        # Do not grant a dev additional writable directories. Its short task
        # context is carried by the prompt; read denials remain explicit failures.
    elif provider == 'codex':
        args = [exe, 'exec', '--json', '--color', 'never', '--sandbox',
                'workspace-write' if phase == 'dev' else 'read-only',
                '--output-schema', str(schema_path), '-o', str(result_path), '-']
    else:
        raise ValueError('Adapter automatico implementado somente para claude/codex')
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
        if data.get('is_error') or data.get('permission_denials'):
            raise RuntimeError('Claude retornou erro ou permissao negada; entrega nao aceita')
        return data.get('structured_output'), {'session_id': data.get('session_id'), 'usage': data.get('usage')}
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


def run_checks(repo, manifest, run, config):
    if not manifest['checks']:
        raise RuntimeError('Sem checks aprovados; validacao nao pode ser declarada verde')
    results = []
    before = fingerprint(repo)
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
    if fingerprint(repo) != before:
        raise RuntimeError('Check alterou a arvore; inspecionar antes de validar novamente')
    save(run / 'validation.json', {'fingerprint': before, 'head': git(repo, 'rev-parse', 'HEAD'), 'tests': results, 'at': now()})
    if any(r['exit_code'] for r in results):
        raise RuntimeError('Validacao falhou; logs preservados. Corrigir causa/classificar falha antes de continuar.')
    return results


def build_prompt(folder, manifest, state, config):
    phase = state['phase']
    task = (folder / 'task-spec.md').read_text(encoding='utf-8')
    if len(task) > 16000:
        task = f'Leia somente criterios relevantes em {folder / "task-spec.md"}; entrada extensa nao injetada.'
    return '\n\n'.join([
        'Execute SOMENTE a fase ' + phase + ' desta tarefa. Nao dispare outros agentes, nao publique, nao faca push/merge, nao execute migrations.',
        'Politica vigente:\n' + (ROOT / 'POLITICA.md').read_text(encoding='utf-8'),
        'Manifesto aprovado:\n' + json.dumps(manifest, ensure_ascii=False),
        'Criterios:\n' + task,
        'Handoff curto anterior:\n' + json.dumps(state.get('handoff', {}), ensure_ascii=False),
        'Evidencia de validacao do runner:\n' + json.dumps(state.get('validation', {}), ensure_ascii=False),
        'Documento parcial da fase anterior (se existir): ' + str(folder / (phase + '-partial.md')),
        'Plano: ' + str(folder / 'plan.md') + '. Ler quando existir e for pertinente. Findings: ' + str(folder / 'review-report.md'),
        'Fases plan/docs/review/prototype sao somente leitura do codigo; entregue documento completo no campo document. '
        'Plan deve enumerar fatias pequenas e criterios de aceite. Prototype entrega HTML estatico. '
        'Dev implementa UMA fatia coerente e retorna continue com proximo passo quando faltar trabalho; nao altere task.json, plan.md, approvals ou estado do runner. '
        'Review: verdict deve ser approved ou changes_required; forneca findings verificaveis. Nao corrija. '
        'Informe artifacts com paths existentes relativos ao repo, nao com alegacoes de testes. '
        'O runner executara os checks aprovados. Qualquer permissao negada ou necessidade de decisao retorna blocked. '
        f'Contexto alvo: aviso {config["context"]["warn_tokens"]}, transicao {config["context"]["transition_tokens"]}. '
        'Retorne handoff antes de crescer desnecessariamente; este alvo nao e quota do plano. '
        'Somente JSON conforme schema; nao assuma aprovacao humana.'])


def execute(task, executor=invoke):
    folder, local = task_paths(task)
    config = read(ROOT / 'config/pipeline.json')
    with lock(local):
        manifest = read(folder / 'task.json')
        state = read(local / 'state.json')
        repo = repository(manifest)
        pending = unsettled_runs(task, state)
        if pending:
            state.update(phase='blocked', blocked_phase=pending[-1][1].get('phase', state.get('blocked_phase', state['phase'])),
                         reason='Execucao anterior sem desfecho; inspecionar processos e usar recover',
                         last_run=str(pending[-1][0].relative_to(ROOT)))
            save(local / 'state.json', state)
            write_handoff(folder, state)
            return state
        if state['phase'] in ('blocked', 'ready_for_draft') or state['phase'].startswith('awaiting_'):
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
            if phase != 'validate' and slices >= config['max_slices_per_run']:
                state.update(blocked_phase=phase, phase='blocked',
                             reason='Limite de fatias de agente por execucao; reavaliar antes de recuperar')
                break
            if manifest['size'] != 'P' and not approved(folder, local, manifest, 'CP0'):
                state['phase'] = 'awaiting_CP0'
                break
            if phase not in ('plan',) and not approved(folder, local, manifest, 'CP1'):
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
                before = fingerprint(repo)
                if phase == 'validate':
                    check_scope(repo, manifest)
                    receipt['tests'] = run_checks(repo, manifest, run, config)
                    state['validated_tree'] = fingerprint(repo)
                    state['validation'] = {'receipt': str((run / 'validation.json').relative_to(ROOT)),
                                           'tests': receipt['tests'], 'fingerprint': state['validated_tree']}
                    state['phase'] = 'review'
                else:
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
                        if before == after:
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
                            state['phase'] = 'awaiting_CP1'
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
        return state


def recover(task, target=None, stale_lock=False):
    if not sys.stdin.isatty():
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
        if state['phase'] != 'blocked' and not pending and owner is None:
            raise RuntimeError('Tarefa nao bloqueada nem interrompida')
        for run, receipt, proc in pending:
            if not proc and receipt.get('status') == 'running' and receipt.get('process_protocol') != 1:
                raise RuntimeError('Liveness desconhecida: execucao legada sem registro de processo em ' + str(run))
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled'):
                if process_alive(proc) is not False:
                    raise RuntimeError('Worker/check vivo ou desconhecido em ' + str(run))
        original = state.get('blocked_phase', pending[-1][1].get('phase') if pending else state['phase'])
        destination = target or ('validate' if original in ('validate', 'review', 'docs') else original)
        allowed = {'dev', 'validate', original} - {'review', 'docs'}
        if destination not in allowed or destination not in ('plan', 'prototype', 'dev', 'validate'):
            raise RuntimeError('Destino de recuperacao invalido')
        if original in ('plan', 'prototype') and destination != original:
            raise RuntimeError('Concluir planejamento/prototipo antes de recuperar para desenvolvimento')
        rounds = state.get('rounds', 0) + (1 if destination == 'dev' else 0)
        if rounds >= read(ROOT / 'config/pipeline.json')['max_rounds']:
            raise RuntimeError('Limite de rodadas atingido; reavaliar escopo/plano')
        print(json.dumps({'state': state, 'lock': owner, 'destination': destination,
                          'pending': [{'run': str(r), 'process': p} for r, _, p in pending]}, ensure_ascii=False, indent=2))
        if input('Inspecionou worktree, filhos dos processos e causa? RECUPERAR: ') != 'RECUPERAR':
            raise RuntimeError('Recuperacao cancelada')
        # Recheck immediately before mutation; a reused PID must never be stolen.
        if owner is not None and process_alive(owner) is not False:
            raise RuntimeError('Dono do lock vivo ou desconhecido; nao recuperar')
        for run, _, proc in pending:
            if proc.get('status') in ('launching', 'running', 'timeout_unsettled') and process_alive(proc) is not False:
                raise RuntimeError('Worker/check vivo ou desconhecido em ' + str(run))
        event = {'at': now(), 'source': 'interactive-human', 'from_phase': original,
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
        for key in ('validated_tree', 'validation', 'active_run', 'reason', 'blocked_phase'):
            state.pop(key, None)
        state.update(phase=destination, rounds=rounds, last_recovery=recovery_id)
        save(local / 'state.json', state)
        write_handoff(folder, state)
        return state


def approve(task):
    folder, local = task_paths(task)
    if not sys.stdin.isatty():
        raise RuntimeError('Aprovacao exige terminal humano interativo; agente nao pode aprovar por pipe')
    with lock(local):
        manifest, state = read(folder / 'task.json'), read(local / 'state.json')
        gate = state['phase'].removeprefix('awaiting_')
        if gate not in ('CP0', 'CP1', 'PROTOTIPO'):
            raise RuntimeError('Nao ha checkpoint aguardando')
        value = gate_hash(folder, manifest, gate)
        print('Revise criterios/plano, base, escopo e checks de', folder, '\nGate:', gate, '\nHash:', value)
        if input('Digite APROVAR ' + gate + ': ') != 'APROVAR ' + gate:
            raise RuntimeError('Aprovacao nao concedida')
        save(local / 'approvals' / (gate + '.json'), {'hash': value, 'at': now(), 'source': 'interactive-human'})
        state['phase'] = 'plan' if gate == 'CP0' else ('prototype' if gate == 'CP1' and manifest.get('prototype') else state.pop('resume_phase', 'dev'))
        save(local / 'state.json', state)
    return execute(task)


def initialize(task, repo_name, source, base_ref, scope, checks, size='P', prototype=False):
    folder, local = task_paths(task)
    config = read(ROOT / 'config/pipeline.json')
    with lock(local):
        if (folder / 'task.json').exists() or (local / 'state.json').exists():
            raise RuntimeError('Tarefa ja cadastrada')
        manifest = {'schema_version': 1, 'repo': repo_name, 'base_ref': base_ref, 'scope': scope,
                    'checks': checks, 'size': size, 'prototype': prototype, 'providers': config['providers']}
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
        state = {'phase': 'plan' if size == 'P' else 'awaiting_CP0', 'created_at': now(), 'rounds': 0}
        save(local / 'state.json', state)
        write_handoff(folder, state)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('task'); init.add_argument('--repo', required=True); init.add_argument('--spec', required=True)
    init.add_argument('--base', required=True); init.add_argument('--scope', action='append', required=True)
    init.add_argument('--check', action='append', required=True, help='argv JSON; aprovado junto ao plano')
    init.add_argument('--size', choices=['P', 'M', 'G'], default='P'); init.add_argument('--prototype', action='store_true')
    for name in ('run', 'approve', 'status', 'recover'):
        q = sub.add_parser(name); q.add_argument('task')
        if name == 'recover':
            q.add_argument('--to', choices=['dev', 'validate'], help='Fase apos inspecao humana; nao ignora gates')
            q.add_argument('--stale-lock', action='store_true', help='Arquivar lock somente com dono comprovadamente encerrado')
    attach = sub.add_parser('attach', help='Reconstruir controle local de tarefa versionada em outra maquina')
    attach.add_argument('task')
    attach.add_argument('--phase', choices=['plan', 'dev', 'validate'], default='dev')
    a = p.parse_args()
    try:
        if a.command == 'init':
            result = initialize(a.task, a.repo, a.spec, a.base, a.scope, [json.loads(c) for c in a.check], a.size, a.prototype)
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
                result = {'phase': 'awaiting_CP0' if manifest['size'] != 'P' else ('plan' if a.phase == 'plan' else 'awaiting_CP1'),
                          'resume_phase': a.phase, 'rounds': 0, 'created_at': now(),
                          'handoff': {'summary': handoff, 'next_step': 'Revalidar aprovacao nesta maquina e continuar fase ' + a.phase, 'decisions': 'Sem aprovacao transferida automaticamente'}}
                save(local / 'state.json', result)
        elif a.command == 'recover':
            result = recover(a.task, a.to, a.stale_lock)
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
