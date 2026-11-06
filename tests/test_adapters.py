"""Exercise real local subprocess IO with fake CLIs, never provider calls."""
import contextlib
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline as runner
os.environ["PIPELINE_SEM_AMBIENTE"] = "1"  # nunca abrir MR/servidor de verdade nos testes


FAKE_CLI = r'''
import json, os, pathlib, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
pathlib.Path('argv.json').write_text(json.dumps(args), encoding='utf-8')
answer = dict(status='completed', summary='done', next_step='next', decisions='',
              document='document', verdict='approved', artifacts=[])
mode = os.environ.get('FAKE_CLI_MODE', '')
if mode == 'bad_json':
    print('{broken')
elif mode == 'scalar':
    print('null')
elif any(a.startswith('-p=') for a in args):
    pathlib.Path('prompt-arg.txt').write_text(args[-1][3:], encoding='utf-8')
    print(json.dumps(dict(conversation_id='fake-conv', status='ERROR' if mode == 'agy_error' else 'SUCCESS',
                         structured_output=answer, usage={'total_tokens': 1},
                         **({'denied_actions': [{'action': 'command'}]} if mode == 'denied' else {}))))
elif '-p' in args:
    print(json.dumps(dict(structured_output=answer, session_id='fake-session',
                         permission_denials=[{'tool_name': 'Edit' if mode == 'denied_write' else 'Read',
                                             'tool_input': {'file_path': '/x/scripts/pipeline.py'}}]
                         if mode in ('denied', 'denied_write') else [])))
else:
    pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps(answer), encoding='utf-8')
    print(json.dumps(dict(type='thread.started', thread_id='fake-session')))
    print(json.dumps(dict(type='turn.completed', usage={})))
'''


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.run = self.root / '.local/runs/demo/one'
        self.run.mkdir(parents=True)
        self.fake = self.root / 'fake.py'
        self.fake.write_text(FAKE_CLI, encoding='utf-8')
        self.real_popen = subprocess.Popen
        self.config = {'timeout_seconds': 10}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, provider, phase='review', mode=''):
        def popen(args, **kwargs):
            if args[0] == 'git':  # git rev-parse do adaptador do agy: git real, nao o CLI falso
                return self.real_popen(args, **kwargs)
            return self.real_popen([sys.executable, str(self.fake), *args[1:]], **kwargs)
        with patch.object(runner, 'ROOT', self.root), patch.object(runner.shutil, 'which', return_value=sys.executable), \
                patch.object(runner.subprocess, 'Popen', side_effect=popen), patch.dict(os.environ, {'FAKE_CLI_MODE': mode}):
            return runner.invoke(provider, 'test prompt', self.repo, self.run, phase, self.config)

    def test_claude_review_has_only_required_extra_dirs_and_read_tools(self):
        result, meta = self.call('claude')
        runner.validate_response(result)
        args = json.loads((self.repo / 'argv.json').read_text())
        self.assertEqual(args[args.index('--tools') + 1], 'Read,Grep,Glob')
        self.assertEqual(args[args.index('--add-dir') + 1:], [str(self.run.parent.resolve()), str((self.root / 'tarefas/demo').resolve())])
        self.assertEqual(args[args.index('--permission-mode') + 1], 'default')
        self.assertEqual(meta['session_id'], 'fake-session')
        self.assertEqual(runner.read(self.run / 'process.json')['status'], 'exited')

    def test_claude_review_reads_symlinked_node_modules(self):
        deps = self.root / 'clone-principal/node_modules'
        deps.mkdir(parents=True)
        (self.repo / 'node_modules').symlink_to(deps)
        self.call('claude')
        args = json.loads((self.repo / 'argv.json').read_text())
        self.assertIn(str(deps.resolve()), args[args.index('--add-dir') + 1:])
        self.call('claude', 'dev')
        args = json.loads((self.repo / 'argv.json').read_text())
        self.assertNotIn('--add-dir', args)

    def test_codex_sandbox_and_result_contract(self):
        for phase, sandbox in [('review', 'read-only'), ('dev', 'workspace-write')]:
            with self.subTest(phase=phase):
                result, _ = self.call('codex', phase)
                runner.validate_response(result)
                args = json.loads((self.repo / 'argv.json').read_text())
                self.assertEqual(args[args.index('--sandbox') + 1], sandbox)
                self.assertNotIn('--add-dir', args)

    def test_agy_modes_dirs_and_prompt_as_last_argument(self):
        for phase, mode in [('review', 'plan'), ('docs', 'plan'), ('dev', 'accept-edits')]:
            with self.subTest(phase=phase):
                result, meta = self.call('agy', phase)
                runner.validate_response(result)
                args = json.loads((self.repo / 'argv.json').read_text())
                self.assertEqual(args[args.index('--mode') + 1], mode)
                self.assertEqual(args[args.index('--model') + 1], 'gemini-4-argon-high')
                self.assertNotIn('--dangerously-skip-permissions', args)
                self.assertTrue(args[-1].startswith('-p=test prompt\n\n'))
                # Negativa encerra o agy headless: o prompt limita o shell a leitura e proibe o runner.
                self.assertIn('somente comandos de leitura', args[-1])
                self.assertIn('Nao leia scripts/', args[-1])
                # Sandbox (Landlock) sempre: e ele que barra `ls > arquivo` fora do workspace.
                self.assertIn('--sandbox', args)
                self.assertEqual(json.loads(args[args.index('--json-schema') + 1]), runner.schema())
                self.assertEqual(meta['session_id'], 'fake-conv')
                dirs = [args[i + 1] for i, a in enumerate(args) if a == '--add-dir']
                settings = runner.read(Path(args[args.index('--settings') + 1]))
                commands = ['command(' + c + ')' for c in runner.AGY_READ_COMMANDS]
                self.assertNotIn('command(*)', settings['permissions']['allow'])
                if phase == 'dev':
                    self.assertEqual(dirs, [])
                    self.assertEqual(settings, {'permissions': {'allow': commands + [
                        'read_file(' + str(self.run.parent.resolve()) + ')',
                        'read_file(' + str((self.root / 'tarefas').resolve()) + ')']}})
                else:
                    self.assertEqual(settings, {'permissions': {'allow': commands}})
                    self.assertEqual(dirs, [str(self.run.parent.resolve()), str((self.root / 'tarefas').resolve())])

    def test_agy_model_from_config_and_oversized_prompt(self):
        self.config['agy_model'] = 'gemini-4-argon-medium'
        self.call('agy')
        args = json.loads((self.repo / 'argv.json').read_text())
        self.assertEqual(args[args.index('--model') + 1], 'gemini-4-argon-medium')
        with patch.object(runner.shutil, 'which', return_value=sys.executable):
            with self.assertRaisesRegex(RuntimeError, 'Prompt grande demais'):
                runner.invoke('agy', 'x' * 120001, self.repo, self.run, 'review', self.config)

    def test_agy_error_and_denied_fail_explicitly(self):
        for mode in ('agy_error', 'denied'):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(RuntimeError, 'agy retornou erro ou permissao negada'):
                    self.call('agy', 'dev', mode=mode)
        for mode in ('scalar', 'bad_json'):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.call('agy', mode=mode)

    def test_invalid_events_and_denials_fail_explicitly(self):
        for provider in ('codex', 'claude'):
            for mode in ('scalar', 'bad_json'):
                with self.subTest(provider=provider, mode=mode):
                    with self.assertRaises(ValueError):
                        self.call(provider, mode=mode)
                    self.assertEqual(runner.read(self.run / 'process.json')['status'], 'exited')
        # Negativa de leitura no dev, ou de escrita em qualquer fase, bloqueia.
        with self.assertRaisesRegex(RuntimeError, 'permissao negada'):
            self.call('claude', phase='dev', mode='denied')
        with self.assertRaisesRegex(RuntimeError, 'permissao negada'):
            self.call('claude', phase='plan', mode='denied_write')

    def test_claude_read_denial_in_read_only_phase_is_incidental(self):
        # O plano tentou ler o runner e foi negado, mas entregou o resultado completo: aceito, com registro.
        result, meta = self.call('claude', phase='plan', mode='denied')
        runner.validate_response(result)
        self.assertEqual(meta['negativas_incidentais'][0]['tool'], 'Read')

    def test_launch_failure_is_distinct_from_unknown_spawn(self):
        with self.assertRaises(OSError):
            runner.start_process(self.run, [str(self.root / 'absent.exe')])
        self.assertEqual(runner.read(self.run / 'process.json')['status'], 'launch_failed')

    def test_timeout_records_live_process_without_termination(self):
        # Fake the wait, not the process: observe the PID, then let it exit.
        process = self.real_popen([sys.executable, '-c', 'import time; time.sleep(1)'])
        try:
            with patch.object(runner.shutil, 'which', return_value=sys.executable), \
                    patch.object(runner.subprocess, 'Popen', return_value=process), \
                    patch.object(process, 'communicate', side_effect=subprocess.TimeoutExpired('fake', 0)):
                with self.assertRaisesRegex(RuntimeError, 'Timeout'):
                    runner.invoke('codex', 'test', self.repo, self.run, 'dev', self.config)
            self.assertEqual(runner.read(self.run / 'process.json')['pid'], process.pid)
            self.assertEqual(runner.read(self.run / 'process.json')['status'], 'timeout_unsettled')
        finally:
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
