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
        self.plan_and_gate()
        result = runner.execute('demo', self.worker)
        self.assertEqual(result['phase'], 'ready_for_draft')
        self.assertEqual(self.phases, ['plan', 'dev', 'review', 'docs'])
        validation = list((self.root / '.local/runs/demo').glob('*/validation.json'))
        self.assertEqual(len(validation), 1)
        self.assertEqual(runner.read(validation[0])['tests'][0]['exit_code'], 0)

    def test_approval_invalidated_by_plan_change(self):
        self.plan_and_gate()
        (self.folder / 'plan.md').write_text('Mudou escopo', encoding='utf-8')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')
        self.assertEqual(self.phases, ['plan'])

    def test_no_progress_blocks(self):
        self.plan_and_gate()
        result = runner.execute('demo', lambda *args: self.response('dev'))
        self.assertEqual(result['phase'], 'blocked')
        self.assertIn('progresso', result['reason'])

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
        def broken(*args):
            (self.repo / 'app.txt').write_text('wrong', encoding='utf-8')
            return self.response('dev')
        result = runner.execute('demo', broken)
        self.assertEqual(result['blocked_phase'], 'validate')
        state = self.recover('dev')
        self.assertEqual(state['rounds'], 1)
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
            count += 1
            (self.repo / 'app.txt').write_text(str(count), encoding='utf-8')
            return self.response('dev', status='continue')
        result = runner.execute('demo', worker)
        self.assertEqual(result['phase'], 'blocked')
        self.assertEqual(count, 6)
        self.assertIn('Limite de fatias', result['reason'])

    def test_round_limit_cannot_be_reset_by_recover(self):
        self.plan_and_gate()
        state = runner.read(self.local / 'state.json')
        state.update(phase='blocked', blocked_phase='dev', rounds=2)
        runner.save(self.local / 'state.json', state)
        with self.assertRaisesRegex(RuntimeError, 'Limite de rodadas'):
            self.recover('dev')

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
        self.assertEqual(self.phases, ['plan'])
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
        self.assertEqual(self.phases, ['plan', 'dev'])

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
        (self.folder / 'plan.md').write_text('changed', encoding='utf-8')
        self.assertEqual(runner.execute('demo', self.worker)['phase'], 'awaiting_CP1')

    def test_mutually_exclusive_runner_and_recovery(self):
        self.plan_and_gate()
        with runner.lock(self.local):
            with self.assertRaisesRegex(RuntimeError, 'ativo'):
                self.recover('dev', True)


if __name__ == '__main__':
    unittest.main()
