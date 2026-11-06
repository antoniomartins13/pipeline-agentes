import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import socket
import os
import contextlib
import io
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline as runner
os.environ["PIPELINE_SEM_AMBIENTE"] = "1"  # nunca abrir MR/servidor de verdade nos testes


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'pipeline'
        self.root.mkdir()
        (self.root / 'config').mkdir()
        config = json.loads((runner.ROOT / 'config/pipeline.json').read_text(encoding='utf-8'))
        (self.root / 'config/pipeline.json').write_text(json.dumps(config), encoding='utf-8')
        (self.root / 'POLITICA.md').write_text('Politica de teste.', encoding='utf-8')
        self.repo = Path(self.tmp.name) / 'demo'
        self.repo.mkdir()
        self.git('init')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.repo / 'app.txt').write_text('old', encoding='utf-8')
        self.git('add', '.'); self.git('commit', '-m', 'base')
        self.git('branch', 'base'); self.git('checkout', '-b', 'task/demo')
        self.spec = Path(self.tmp.name) / 'spec.md'
        self.spec.write_text('Trocar old por new.', encoding='utf-8')
        self.root_patch = patch.object(runner, 'ROOT', self.root)
        self.settings_patch = patch.object(runner, 'settings', return_value={'repositories': {'demo': str(self.repo)}})
        self.root_patch.start(); self.settings_patch.start()
        runner.initialize('demo', 'demo', self.spec, 'base', ['app.txt'], [[sys.executable, '-c', "from pathlib import Path; assert Path('app.txt').read_text() == 'new'"]])
        self.folder, self.local = runner.task_paths('demo')
        self.phases = []

    def tearDown(self):
        self.settings_patch.stop(); self.root_patch.stop()
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True, capture_output=True)

    def response(self, phase, **updates):
        result = {'status': 'completed', 'summary': 'Feito ' + phase, 'next_step': 'Proxima fase',
                  'decisions': 'Sem mudanca de contrato', 'document': 'Documento ' + phase,
                  'verdict': 'approved', 'artifacts': []}
        result.update(updates)
        return result, {'session_id': 'session-' + str(len(self.phases))}

    def worker(self, provider, prompt, repo, run, phase, config):
        self.phases.append(phase)
        if phase == 'dev':
            (repo / 'app.txt').write_text('new', encoding='utf-8')
        return self.response(phase)

    def plan_and_gate(self):
        state = runner.execute('demo', self.worker)
        self.assertEqual(state['phase'], 'awaiting_CP1')
        manifest = runner.read(self.folder / 'task.json')
        # Test fixture only; production approve refuses non-TTY input.
        runner.save(self.local / 'approvals/CP1.json', {'hash': runner.gate_hash(self.folder, manifest, 'CP1')})
        state['phase'] = 'dev'
        runner.save(self.local / 'state.json', state)

    def test_full_flow_with_independent_review_and_real_check(self):
        state = runner.execute('demo', self.worker)
        self.assertEqual(state['phase'], 'awaiting_CP1')
        h = runner.gate_status('demo')['combined_hash'][:10]
        runner.register_approvals('demo', 'CP1', 'chat-usuario', expected_hash=h)
        result = runner.execute('demo', self.worker)
        self.assertEqual(result['phase'], 'ready_for_draft')
        self.assertEqual(self.phases, ['plan', 'dev', 'review', 'docs'])
        validation = list((self.root / '.local/runs/demo').glob('*/validation.json'))
        self.assertEqual(len(validation), 1)
        self.assertEqual(runner.read(validation[0])['tests'][0]['exit_code'], 0)

    def test_approval_invalidated_by_spec_change_not_plan(self):
        self.plan_and_gate()
        (self.folder / 'plan.md').write_text('Plano gerado pelo runner nao invalida CP1', encoding='utf-8')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')
        (self.folder / 'task-spec.md').write_text('Mudou escopo da funcionalidade', encoding='utf-8')
        state = runner.read(self.local / 'state.json'); state['phase'] = 'dev'
        runner.save(self.local / 'state.json', state)
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')

    def test_no_progress_blocks(self):
        self.plan_and_gate()
        # "continue" sem mudar nada e travamento; "completed" sem nada em relacao a base tambem bloqueia.
        result = runner.execute('demo', lambda *args: self.response(args[4], status='continue'))
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('progresso', result['reason'])

    def test_completed_without_new_change_goes_to_validation(self):
        self.plan_and_gate()
        (self.repo / 'app.txt').write_text('new', encoding='utf-8')  # trabalho de um run anterior
        result = runner.execute('demo', lambda *args: self.response(args[4]))
        self.assertEqual(result['phase'], 'ready_for_draft')

    def test_validation_failure_goes_back_to_dev_with_the_log(self):
        self.plan_and_gate()
        prompts = []
        def worker(provider, prompt, repo, run, phase, config):
            if phase == 'dev':
                prompts.append(prompt)
                (repo / 'app.txt').write_text('wrong' if len(prompts) == 1 else 'new', encoding='utf-8')
            return self.response(phase)
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'ready_for_draft')
        self.assertEqual(result['rounds'], 1)
        self.assertEqual(len(prompts), 2)
        self.assertIn('A validacao anterior falhou', prompts[1])
        self.assertIn('check-0.log', prompts[1])
        self.assertNotIn('auto_failure', result)
        self.assertFalse((self.local / 'recoveries').exists())  # nada registrado em nome do dono

    def test_check_writing_a_clean_tracked_file_is_restored_and_back_to_dev(self):
        (self.repo / 'fixture.bin').write_text('original', encoding='utf-8')
        self.git('add', '.'); self.git('commit', '-m', 'fixture')
        manifest = runner.read(self.folder / 'task.json')
        manifest['base_sha'] = runner.git(self.repo, 'rev-parse', 'HEAD')
        manifest['checks'] = [[sys.executable, '-c', "from pathlib import Path; Path('fixture.bin').write_text('x')"]]
        runner.save(self.folder / 'task.json', manifest)
        self.git('branch', '-f', 'base')
        self.plan_and_gate()
        result = runner.execute('demo', self.worker)
        self.assertEqual((self.repo / 'fixture.bin').read_text(encoding='utf-8'), 'original')
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('Limite de rodadas', result['reason'])
        self.assertIn('fixture.bin', result['reason'])

    def test_continue_dispatches_next_slice_automatically(self):
        self.plan_and_gate()
        count = 0
        def worker(*args):
            nonlocal count
            if args[4] == 'dev':
                count += 1
                (self.repo / 'app.txt').write_text('partial' if count == 1 else 'new', encoding='utf-8')
                return self.response('dev', status='continue' if count == 1 else 'completed')
            return self.worker(*args)
        self.assertEqual(runner.execute('demo', worker)['phase'], 'ready_for_draft')
        self.assertEqual(count, 2)

    def test_review_cannot_change_code(self):
        self.plan_and_gate()
        def worker(*args):
            result = self.worker(*args)
            if args[4] == 'review':
                (self.repo / 'app.txt').write_text('review edited', encoding='utf-8')
            return result
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('somente leitura', result['reason'])

    def test_outside_scope_blocks(self):
        self.plan_and_gate()
        def worker(*args):
            (self.repo / 'extra.txt').write_text('outside', encoding='utf-8')
            return self.worker(*args)
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('escopo', result['reason'])

    def test_test_failure_blocks_before_review(self):
        self.plan_and_gate()
        def worker(*args):
            (self.repo / 'app.txt').write_text('wrong', encoding='utf-8')
            return self.response('dev')
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertEqual(result['blocked_phase'], 'validate')

    def test_timeout_blocks_without_another_worker(self):
        self.plan_and_gate()
        def worker(*args):
            raise RuntimeError('Timeout: processo pode estar vivo')
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('Timeout', result['reason'])

    def test_approval_cannot_be_piped(self):
        with patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(RuntimeError):
                runner.approve('demo')

    def test_lock_prevents_double_dispatch(self):
        with runner.lock(self.local):
            with self.assertRaises(RuntimeError):
                runner.execute('demo', self.worker)

    def test_protected_manifest_change_blocks(self):
        self.plan_and_gate()
        def worker(*args):
            m = runner.read(self.folder / 'task.json')
            m['checks'] = []
            runner.save(self.folder / 'task.json', m)
            return self.worker(*args)
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('protegida', result['reason'])

    def recover(self, target=None, stale=False):
        # Only a temporary test task; no real approval/recovery is generated.
        with patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='RECUPERAR'), contextlib.redirect_stdout(io.StringIO()):
            return runner.recover('demo', target, stale)

    def test_rename_checks_both_paths_staged_and_committed(self):
        (self.repo / 'outside.txt').write_text('outside', encoding='utf-8')
        self.git('add', '.'); self.git('commit', '-m', 'extra base')
        manifest = runner.read(self.folder / 'task.json')
        manifest['base_sha'] = runner.git(self.repo, 'rev-parse', 'HEAD')
        manifest['scope'] = ['app.txt', 'inside']
        (self.repo / 'inside').mkdir()
        self.git('mv', 'outside.txt', 'inside/moved.txt')
        with self.assertRaisesRegex(RuntimeError, 'outside.txt'):
            runner.check_scope(self.repo, manifest)
        self.git('commit', '-m', 'rename')
        with self.assertRaisesRegex(RuntimeError, 'outside.txt'):
            runner.check_scope(self.repo, manifest)
        manifest['scope'].append('outside.txt')
        runner.check_scope(self.repo, manifest)

    def test_scope_unicode_paths(self):
        name = 'ação com espaço.txt'
        (self.repo / name).write_text('new', encoding='utf-8')
        self.git('add', name)
        manifest = runner.read(self.folder / 'task.json')
        manifest['scope'] = [name]
        runner.check_scope(self.repo, manifest)

    def test_test_failure_can_recover_to_dev_then_revalidate(self):
        self.plan_and_gate()
        # Falha de validacao volta ao dev sozinha; a recuperacao humana fica para bloqueio de outro tipo.
        def broken(*args):
            raise RuntimeError('agy retornou erro ou permissao negada; entrega nao aceita')
        result = runner.execute('demo', broken)
        self.assertEqual(result['blocked_phase'], 'dev')
        state = self.recover('dev')
        self.assertEqual(state['rounds'], 0)
        self.assertNotIn('validation', state)
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')
        self.assertEqual(len(list((self.local / 'recoveries').glob('*.json'))), 1)

    def test_one_review_correction_fits_budget(self):
        self.plan_and_gate()
        reviews = 0
        devs = 0
        def worker(*args):
            nonlocal reviews, devs
            phase = args[4]
            if phase == 'dev':
                devs += 1
                (self.repo / 'app.txt').write_text('new', encoding='utf-8')
                if devs == 2:
                    self.git('add', 'app.txt'); self.git('commit', '-m', 'review correction')
                return self.response('dev')
            if phase == 'review':
                reviews += 1
                return self.response('review', verdict='changes_required' if reviews == 1 else 'approved')
            return self.worker(*args)
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'ready_for_draft')
        self.assertEqual(result['rounds'], 1)

    def test_slice_exhaustion_still_blocks(self):
        self.plan_and_gate()
        count = 0
        def worker(*args):
            nonlocal count
            if args[4] == 'dev':
                count += 1
                (self.repo / 'app.txt').write_text(str(count), encoding='utf-8')
                return self.response('dev', status='continue')
            return self.worker(*args)
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertEqual(count, 6)
        self.assertIn('Limite de fatias', result['reason'])

    def test_round_limit_cannot_be_reset_by_recover(self):
        self.plan_and_gate()
        max_rounds = runner.read(self.root / 'config/pipeline.json')['max_rounds']
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='validate', rounds=max_rounds - 1)
        runner.save(self.local / 'state.json', state)
        with self.assertRaisesRegex(RuntimeError, 'Limite de rodadas'):
            self.recover('dev')

    def test_review_round_limit_is_not_counted_twice_on_recover(self):
        self.plan_and_gate()
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='review', rounds=3,
                     reason='RuntimeError: Limite de rodadas atingido; reavaliar escopo/plano')
        runner.save(self.local / 'state.json', state)
        config = runner.read(self.root / 'config/pipeline.json')
        runner.save(self.root / 'config/pipeline.json', dict(config, max_rounds=4))
        self.assertEqual(self.recover('dev')['rounds'], 3)

    def test_resuming_interrupted_dev_does_not_spend_round(self):
        self.plan_and_gate()
        max_rounds = runner.read(self.root / 'config/pipeline.json')['max_rounds']
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='dev', rounds=max_rounds - 1)
        runner.save(self.local / 'state.json', state)
        self.assertEqual(self.recover('dev')['rounds'], max_rounds - 1)
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='dev', rounds=max_rounds)
        runner.save(self.local / 'state.json', state)
        with self.assertRaisesRegex(RuntimeError, 'Limite de rodadas'):
            self.recover('dev')

    def test_reopened_cp1_resumes_previous_phase_after_approval(self):
        self.plan_and_gate()
        state = runner.read(self.local / 'state.json')
        state['phase'] = 'validate'
        runner.save(self.local / 'state.json', state)
        manifest = runner.read(self.folder / 'task.json')
        manifest['providers']['review'] = 'codex'
        runner.save(self.folder / 'task.json', manifest)
        state = runner.execute('demo', self.worker)
        self.assertEqual(state['phase'], 'awaiting_CP1')
        self.assertEqual(state.get('resume_phase'), 'validate')
        h = runner.gate_status('demo')['combined_hash'][:10]
        runner.register_approvals('demo', 'CP1', 'test', expected_hash=h)
        state = runner.read(self.local / 'state.json')
        self.assertEqual(state['phase'], 'validate')
        self.assertNotIn('resume_phase', state)

    def test_unexpected_exception_blocks_and_does_not_retry(self):
        self.plan_and_gate()
        def broken(*args):
            raise AttributeError('malformed event')
        state = runner.execute('demo', broken)
        self.assertEqual(state['phase'], 'blocked')
        self.assertIn('AttributeError', state['reason'])
        calls = len(self.phases)
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'blocked')
        self.assertEqual(len(self.phases), calls)

    def orphan(self, process=None):
        run = self.root / '.local/runs/demo/orphan'
        runner.save(run / 'receipt.json', {'phase': 'dev', 'status': 'running', 'process_protocol': 1})
        if process is not None:
            runner.save(run / 'process.json', process)
        return run

    def test_orphan_receipt_blocks_dispatch(self):
        self.plan_and_gate()
        run = self.orphan()
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'blocked')
        self.assertEqual(self.phases, [])
        self.assertEqual(self.recover('dev')['phase'], 'dev')
        self.assertEqual(runner.read(run / 'receipt.json')['status'], 'recovered')

    def test_state_save_failure_cannot_redispatch_completed_worker(self):
        self.plan_and_gate()
        original_save = runner.save
        def failing(path, data):
            if path == self.local / 'state.json' and data['phase'] == 'validate':
                raise PermissionError('locked state file')
            original_save(path, data)
        with patch.object(runner, 'save', side_effect=failing):
            with self.assertRaises(PermissionError):
                runner.execute('demo', self.worker)
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'blocked')
        self.assertEqual(self.phases, ['dev'])

    def test_recovery_rejects_live_foreign_and_unknown_processes(self):
        self.plan_and_gate()
        for proc in ({'pid': os.getpid(), 'host': socket.gethostname(), 'status': 'running'},
                     {'pid': os.getpid(), 'host': 'other-host', 'status': 'running'},
                     {'pid': None, 'host': socket.gethostname(), 'status': 'launching'}):
            with self.subTest(proc=proc):
                self.orphan(proc)
                runner.execute('demo', self.worker)
                with self.assertRaisesRegex(RuntimeError, 'vivo ou desconhecido'):
                    self.recover('dev')

    def test_stale_lock_recovery_requires_dead_owner(self):
        self.plan_and_gate()
        owner = {'pid': os.getpid(), 'host': socket.gethostname()}
        runner.save(self.local / 'runner.lock', owner)
        with self.assertRaisesRegex(RuntimeError, 'vivo ou desconhecido'):
            self.recover('dev', True)
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=10)
        owner['pid'] = child.pid
        self.assertIs(runner.process_alive(owner), False)
        runner.save(self.local / 'runner.lock', owner)
        self.assertEqual(self.recover('dev', True)['phase'], 'dev')
        self.assertFalse((self.local / 'runner.lock').exists())
        self.assertEqual(len(list((self.local / 'recoveries').glob('*.lock.json'))), 1)

    def test_recover_cannot_be_piped_or_skip_gate(self):
        self.plan_and_gate()
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='dev')
        runner.save(self.local / 'state.json', state)
        with patch.object(sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(RuntimeError):
                runner.recover('demo', 'dev')
        self.recover('dev')
        (self.folder / 'task-spec.md').write_text('changed', encoding='utf-8')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')

    def test_mutually_exclusive_runner_and_recovery(self):
        self.plan_and_gate()
        with runner.lock(self.local):
            with self.assertRaisesRegex(RuntimeError, 'ativo'):
                self.recover('dev', True)

    # --- Aprovacao pelo chat, entrega agrupada, CP0 fundido e lock morto ---

    def dead_owner(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=10)
        return {'pid': child.pid, 'host': socket.gethostname()}

    def two_task_entrega(self):
        m = runner.read(self.folder / 'task.json'); m['entrega'] = 'ent'
        runner.save(self.folder / 'task.json', m)
        runner.initialize('demo2', 'demo', self.spec, 'base', ['app.txt'], [[sys.executable, '-c', 'pass']], 'M', entrega='ent')
        for task in ('demo', 'demo2'):
            self.assertEqual(runner.execute(task, self.worker)['phase'], 'awaiting_CP1')

    def test_m_size_has_no_cp0_and_legacy_cp0_resumes_plan(self):
        runner.initialize('big', 'demo', self.spec, 'base', ['app.txt'], [[sys.executable, '-c', 'pass']], 'G')
        _, local = runner.task_paths('big')
        self.assertEqual(runner.read(local / 'state.json')['phase'], 'awaiting_CP1')
        state = runner.read(local / 'state.json'); state['phase'] = 'awaiting_CP0'
        runner.save(local / 'state.json', state)
        self.assertEqual(runner.execute('big', self.worker)['phase'], 'awaiting_CP1')

    def test_dead_lock_is_archived_automatically_live_one_blocks(self):
        runner.save(self.local / 'runner.lock', {'pid': os.getpid(), 'host': socket.gethostname()})
        with self.assertRaisesRegex(RuntimeError, 'vivo ou desconhecido'):
            runner.execute('demo', self.worker)
        runner.save(self.local / 'runner.lock', {'pid': os.getpid(), 'host': 'other-host'})
        with self.assertRaisesRegex(RuntimeError, 'vivo ou desconhecido'):
            runner.execute('demo', self.worker)
        runner.save(self.local / 'runner.lock', self.dead_owner())
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        self.assertFalse((self.local / 'runner.lock').exists())
        self.assertEqual(len(list((self.local / 'recoveries').glob('*.lock.json'))), 1)

    def test_entrega_chat_approval_is_all_or_nothing(self):
        self.two_task_entrega()
        state = runner.read(self.local / 'state.json'); state['phase'] = 'plan'
        runner.save(self.local / 'state.json', state)
        with self.assertRaisesRegex(RuntimeError, 'Nem todas'):
            runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash='0' * 12)
        _, local2 = runner.task_paths('demo2')
        self.assertFalse((local2 / 'approvals/CP1.json').exists())
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        combined = runner.gate_status('ent')['combined_hash']
        with self.assertRaisesRegex(RuntimeError, 'Informe o hash'):
            runner.register_approvals('ent', 'CP1', 'chat-usuario')
        with self.assertRaisesRegex(RuntimeError, 'Hash informado'):
            runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash='00000000')
        runner.register_approvals('ent', 'CP1', 'chat-usuario', {'linha': 'APROVAR CP1 ent'}, combined[:12])
        for local in (self.local, local2):
            record = runner.read(local / 'approvals/CP1.json')
            self.assertEqual(record['source'], 'chat-usuario')
            self.assertEqual(record['tasks'], ['demo', 'demo2'])
            self.assertEqual(runner.read(local / 'state.json')['phase'], 'plan')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')

    def test_task_of_entrega_cannot_be_approved_alone(self):
        self.two_task_entrega()
        with self.assertRaisesRegex(RuntimeError, 'pertence a entrega ent'):
            runner.register_approvals('demo', 'CP1', 'chat-usuario', expected_hash='0' * 12)

    def test_spec_change_in_other_task_invalidates_whole_entrega(self):
        self.two_task_entrega()
        combined = runner.gate_status('ent')['combined_hash']
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=combined)
        other, _ = runner.task_paths('demo2')
        (other / 'task-spec.md').write_text('Mudou a spec da outra tarefa', encoding='utf-8')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        self.assertEqual(self.phases, [])

    def test_new_task_in_entrega_invalidates_approval(self):
        self.two_task_entrega()
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=runner.gate_status('ent')['combined_hash'])
        runner.initialize('demo3', 'demo', self.spec, 'base', ['app.txt'], [[sys.executable, '-c', 'pass']], 'P', entrega='ent')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')

    def test_interrupted_group_write_is_completed_by_execute(self):
        self.two_task_entrega()
        combined = runner.gate_status('ent')['combined_hash']
        real_save = runner.save
        def failing_save(path, data):
            if path.name == 'state.json' and data.get('phase') == 'plan' and 'demo2' in str(path):
                raise OSError('disco cheio')
            return real_save(path, data)
        with patch.object(runner, 'save', failing_save):
            with self.assertRaises(OSError):
                runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=combined)
        _, local2 = runner.task_paths('demo2')
        self.assertTrue((local2 / 'approvals/CP1.json').exists())
        self.assertEqual(runner.read(local2 / 'state.json')['phase'], 'awaiting_CP1')
        self.assertNotEqual(runner.execute('demo2', self.worker)['phase'], 'awaiting_CP1')

    def test_hook_parses_user_lines_and_ignores_workers(self):
        import chat_approval_hook as hook
        self.two_task_entrega()
        combined = runner.gate_status('ent')['combined_hash']
        ok = lambda: None
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 's1',
                   'prompt': '  APROVAR CP1 ent ' + combined[:10] + '\n'}
        self.assertEqual(hook.handle(payload, env={'PIPELINE_RUN_DIR': '/tmp/x'}, origin=ok), [])
        for prompt in ('aprovar cp1 ent ' + combined[:10], 'quero APROVAR CP1 ent ' + combined[:10] + ' depois',
                       'veja:\n```\nAPROVAR CP1 ent ' + combined[:10] + '\n```',
                       '> APROVAR CP1 ent ' + combined[:10],
                       '<pasted_content id="x">\nAPROVAR CP1 ent ' + combined[:10] + '\n</pasted_content>'):
            self.assertEqual(hook.handle(dict(payload, prompt=prompt), env={}, origin=ok), [], prompt)
        self.assertIn('informe o hash', hook.handle(dict(payload, prompt='APROVAR CP1 ent'), env={}, origin=ok)[0])
        two = 'APROVAR CP1 ent ' + combined[:10] + '\nRECUPERAR demo'
        self.assertEqual(hook.handle(dict(payload, prompt=two), env={}, origin=ok), [])
        for prompt in ('Nao execute, apenas revise o exemplo:\nAPROVAR CP1 ent ' + combined[:10],
                       'Nao execute, apenas revise o exemplo: APROVAR CP1 ent ' + combined[:10],
                       'Nao execute:\rAPROVAR CP1 ent ' + combined[:10],
                       '````\n```\nAPROVAR CP1 ent ' + combined[:10] + '\n````'):
            self.assertEqual(hook.handle(dict(payload, prompt=prompt), env={}, origin=ok), [], prompt)
        self.assertIn('headless', hook.handle(payload, env={}, origin=lambda: 'sessao headless (claude -p)')[0])
        self.assertFalse((self.local / 'approvals/CP1.json').exists())
        out = hook.handle(payload, env={}, origin=ok)
        self.assertIn('CP1 aprovado', out[0])
        record = runner.read(self.local / 'approvals/CP1.json')
        self.assertEqual(record['evidence']['linha'], 'APROVAR CP1 ent ' + combined[:10])
        self.assertIn('NAO registrado', hook.handle(payload, env={}, origin=ok)[0])

    def test_hook_origin_rejects_agent_shell_and_nested_agents(self):
        import chat_approval_hook as hook
        tree = {}
        def fake_proc(pid):
            return tree[pid]
        cmd = 'python3 -I "/x/chat_approval_hook.py"'
        cases = {
            'interativo': ({10: (9, ['/bin/sh', '-c', cmd]), 9: (8, ['/home/u/.local/bin/claude', '--input-format', 'stream-json']),
                            8: (1, ['/opt/orca-ide'])}, None),
            'shell de agente': ({10: (9, ['/usr/bin/zsh', '-c', 'source snap && python3 x']), 9: (8, ['claude']), 8: (1, ['orca'])},
                                'nao foi chamado'),
            'headless': ({10: (9, ['claude', '-p', 'x']), 9: (1, ['bash'])}, 'headless'),
            'aninhado': ({10: (9, ['claude']), 9: (8, ['zsh', '-c', 'claude']), 8: (7, ['node', '/x/bin/codex']), 7: (1, ['init'])},
                         'outro agente'),
            'runner': ({10: (9, ['claude']), 9: (8, ['python3', 'pipeline.py', 'run']), 8: (1, ['init'])}, 'runner'),
        }
        with patch.object(hook, '_proc', fake_proc), patch.object(hook, 'configured_command', return_value=cmd), \
                patch.object(hook.sys, 'platform', 'linux'):
            for name, (procs, expected) in cases.items():
                tree.clear(); tree.update(procs)
                problem = hook.origin_problem(10)
                if expected is None:
                    self.assertIsNone(problem, name)
                else:
                    self.assertIn(expected, problem or '', name)

    def test_recover_from_chat_keeps_liveness_checks(self):
        import chat_approval_hook as hook
        self.plan_and_gate()
        self.orphan({'pid': os.getpid(), 'host': socket.gethostname(), 'status': 'running'})
        runner.execute('demo', self.worker)
        payload = {'hook_event_name': 'UserPromptSubmit', 'prompt': 'RECUPERAR demo dev'}
        ok = lambda: None
        self.assertIn('NAO registrado', hook.handle(payload, env={}, origin=ok)[0])
        self.orphan({'pid': self.dead_owner()['pid'], 'host': socket.gethostname(), 'status': 'running'})
        with patch.object(sys.stdin, 'isatty', return_value=False):
            out = hook.handle(payload, env={}, origin=ok)
        self.assertIn('Recuperacao de demo registrada', out[-1])
        recovery = next(p for p in (self.local / 'recoveries').glob('*.json') if not p.name.endswith('.lock.json'))
        self.assertEqual(runner.read(recovery)['source'], 'chat-usuario')

    def test_failure_while_publishing_approvals_releases_nobody(self):
        self.two_task_entrega()
        combined = runner.gate_status('ent')['combined_hash']
        real_save = runner.save
        def failing_save(path, data):
            if path.name == 'CP1.json' and 'demo2' in str(path):
                raise OSError('disco cheio')
            return real_save(path, data)
        with patch.object(runner, 'save', failing_save):
            with self.assertRaises(OSError):
                runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=combined)
        self.assertTrue((self.local / 'approvals/CP1.json').exists())
        manifest = runner.read(self.folder / 'task.json')
        self.assertFalse(runner.approved(self.folder, self.local, manifest, 'CP1'))
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=combined)
        self.assertTrue(runner.approved(self.folder, self.local, manifest, 'CP1'))

    def test_entrega_requires_same_prototype_flag(self):
        self.two_task_entrega()
        with self.assertRaisesRegex(RuntimeError, 'mesmo --prototype'):
            runner.initialize('demo3', 'demo', self.spec, 'base', ['app.txt'], [[sys.executable, '-c', 'pass']], 'P', True, entrega='ent')

    def test_recover_entrega_recovers_only_blocked_members(self):
        import chat_approval_hook as hook
        self.two_task_entrega()
        for task in ('demo', 'demo2'):
            _, local = runner.task_paths(task)
            state = runner.read(local / 'state.json')
            if task == 'demo':
                state.update(phase='blocked', blocked_phase='plan')
            runner.save(local / 'state.json', state)
        with patch.object(sys.stdin, 'isatty', return_value=False):
            out = hook.handle({'hook_event_name': 'UserPromptSubmit', 'prompt': 'RECUPERAR ent'}, env={}, origin=lambda: None)
        self.assertEqual(len(out), 1)
        self.assertIn('Recuperacao de demo registrada', out[0])
        self.assertEqual(runner.read(self.local / 'state.json')['phase'], 'plan')

    def test_ready_for_draft_reopens_only_to_plan_and_needs_new_cp1(self):
        self.plan_and_gate()
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')
        with self.assertRaisesRegex(RuntimeError, 'so reabre para o plano'):
            self.recover('dev')
        state = self.recover('plan')
        self.assertEqual((state['phase'], state['rounds']), ('plan', 0))
        self.assertNotIn('validated_tree', state)
        self.assertFalse((self.local / 'approvals/CP1.json').exists())
        self.assertEqual(len(list((self.local / 'recoveries').glob('*.CP1.json'))), 1)
        # Plano refeito identico nao herda o CP1 arquivado.
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')

    def test_reopened_entrega_gate_covers_concluded_member(self):
        import chat_approval_hook as hook
        self.two_task_entrega()
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=runner.gate_status('ent')['combined_hash'])
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')
        _, local2 = runner.task_paths('demo2')
        state2 = runner.read(local2 / 'state.json'); state2['phase'] = 'ready_for_draft'
        runner.save(local2 / 'state.json', state2)
        with self.assertRaisesRegex(RuntimeError, 'Nem todas'):
            runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash='0' * 12)
        with patch.object(sys.stdin, 'isatty', return_value=False):
            out = hook.handle({'hook_event_name': 'UserPromptSubmit', 'prompt': 'RECUPERAR ent plan'}, env={}, origin=lambda: None)
            self.assertIn('reabertura e por tarefa', out[0])
            out = hook.handle({'hook_event_name': 'UserPromptSubmit', 'prompt': 'RECUPERAR demo plan'}, env={}, origin=lambda: None)
        self.assertIn('Recuperacao de demo registrada', out[0])
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        status = runner.gate_status('ent')
        self.assertTrue(status['combined_hash'])
        self.assertEqual([i.get('concluida', False) for i in status['tasks']], [False, True])
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=status['combined_hash'])
        self.assertEqual(runner.read(self.local / 'state.json')['phase'], 'dev')
        self.assertEqual(runner.read(local2 / 'state.json')['phase'], 'ready_for_draft')
        self.assertTrue(runner.approved(self.folder, self.local, runner.read(self.folder / 'task.json'), 'CP1'))

    def test_read_dirs_added_only_for_read_only_phases(self):
        m = runner.read(self.folder / 'task.json'); m['read_dirs'] = ['irmao']
        runner.save(self.folder / 'task.json', m)
        captured = []
        def fake_start(run, args, **kwargs):
            captured.append((run.name, args))
            raise OSError('parar aqui')
        with patch.object(runner.shutil, 'which', return_value='/bin/claude'), \
                patch.object(runner, 'start_process', fake_start):
            for phase in ('plan', 'dev'):
                run = self.root / '.local/runs/demo' / phase
                run.mkdir(parents=True)
                with self.assertRaises(OSError):
                    runner.invoke('claude', 'p', self.repo, run, phase, {'timeout_seconds': 5})
        plan_args, dev_args = captured[0][1], captured[1][1]
        self.assertIn(str((self.root / '../irmao').resolve()), plan_args)
        self.assertNotIn('--add-dir', dev_args)


    def test_dev_after_failed_validation_receives_reason_and_check_log(self):
        run = self.root / '.local/runs/demo/r1'
        run.mkdir(parents=True)
        (run / 'check-0.log').write_text('inicio\n' + 'x' * 7000 + '\nTests: 1 failed', encoding='utf-8')
        event = {'to_phase': 'dev', 'runs': [],
                 'previous_state': {'reason': 'RuntimeError: Validacao falhou', 'last_run': '.local/runs/demo/r1'}}
        runner.save(self.local / 'recoveries' / 'a.json', event)
        text = runner.recovered_failure('demo')
        self.assertIn('Validacao falhou', text)
        self.assertIn('Tests: 1 failed', text)
        self.assertNotIn('inicio', text)
        runner.save(self.local / 'recoveries' / 'a.json', dict(event, to_phase='validate'))
        self.assertEqual(runner.recovered_failure('demo'), '')


    def test_human_decisions_reach_prompt_and_are_protected(self):
        self.plan_and_gate()
        (self.folder / runner.DECISOES).write_text('- Opcao A: remover operacoes autenticadas.', encoding='utf-8')
        prompts = []
        def worker(*args):
            prompts.append(args[1])
            (self.folder / runner.DECISOES).write_text('alterado pelo agente', encoding='utf-8')
            (self.repo / 'app.txt').write_text('new', encoding='utf-8')
            return self.response('dev')
        result = runner.execute('demo', worker)
        self.assertIn('Opcao A: remover operacoes autenticadas', prompts[0])
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('protegida', result['reason'])

    def test_corrigir_and_concluir_from_chat_hook(self):
        import chat_approval_hook as hook
        self.two_task_entrega()
        combined = runner.gate_status('ent')['combined_hash']
        runner.register_approvals('ent', 'CP1', 'chat-usuario', expected_hash=combined)
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'ready_for_draft')
        _, local2 = runner.task_paths('demo2')
        state2 = runner.read(local2 / 'state.json'); state2['phase'] = 'ready_for_draft'
        runner.save(local2 / 'state.json', state2)

        ok = lambda: None
        out = hook.handle({'hook_event_name': 'UserPromptSubmit',
                           'prompt': 'CORRIGIR ent: - Ignorar item 2 do review\\n- Ajustar filtro de ano'}, env={}, origin=ok)
        self.assertEqual(len(out), 2)
        self.assertIn('DECISOES-HUMANAS.md', out[0])
        self.assertEqual(runner.read(self.local / 'state.json')['phase'], 'dev')
        dec = (self.folder / runner.DECISOES).read_text(encoding='utf-8')
        self.assertIn('- Ignorar item 2 do review\n- Ajustar filtro de ano', dec)
        self.assertTrue(runner.approved(self.folder, self.local, runner.read(self.folder / 'task.json'), 'CP1'))

        out_close = hook.handle({'hook_event_name': 'UserPromptSubmit', 'prompt': 'CONCLUIR ent'}, env={}, origin=ok)
        self.assertEqual(len(out_close), 2)
        self.assertEqual(runner.read(self.local / 'state.json')['phase'], 'closed')
        self.assertEqual(runner.read(local2 / 'state.json')['phase'], 'closed')


if __name__ == '__main__':
    unittest.main()
