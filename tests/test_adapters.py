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
elif '-p' in args:
    print(json.dumps(dict(structured_output=answer, session_id='fake-session',
                         permission_denials=['Read'] if mode == 'denied' else [])))
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
            return self.real_popen([sys.executable, str(self.fake), *args[1:]], **kwargs)
        with patch.object(runner, 'ROOT', self.root), patch.object(runner.shutil, 'which', return_value=sys.executable), \
                patch.object(runner.subprocess, 'Popen', side_effect=popen), patch.dict(os.environ, {'FAKE_CLI_MODE': mode}):
            return runner.invoke(provider, 'test prompt', self.repo, self.run, phase, self.config)

    def test_claude_review_has_only_required_extra_dirs_and_read_tools(self):
        result, meta = self.call('claude')
        runner.validate_response(result)
        args = json.loads((self.repo / 'argv.json').read_text())
        self.assertEqual(args[args.index('--tools') + 1], 'Read,Grep,Glob')
        self.assertEqual(args[args.index('--add-dir') + 1:], [str(self.run.resolve()), str((self.root / 'tarefas/demo').resolve())])
        self.assertEqual(args[args.index('--permission-mode') + 1], 'default')
        self.assertEqual(meta['session_id'], 'fake-session')
        self.assertEqual(runner.read(self.run / 'process.json')['status'], 'exited')

    def test_codex_sandbox_and_result_contract(self):
        for phase, sandbox in [('review', 'read-only'), ('dev', 'workspace-write')]:
            with self.subTest(phase=phase):
                result, _ = self.call('codex', phase)
                runner.validate_response(result)
                args = json.loads((self.repo / 'argv.json').read_text())
                self.assertEqual(args[args.index('--sandbox') + 1], sandbox)
                self.assertNotIn('--add-dir', args)

    def test_invalid_events_and_denials_fail_explicitly(self):
        for provider in ('codex', 'claude'):
            for mode in ('scalar', 'bad_json'):
                with self.subTest(provider=provider, mode=mode):
                    with self.assertRaises(ValueError):
                        self.call(provider, mode=mode)
                    self.assertEqual(runner.read(self.run / 'process.json')['status'], 'exited')
        with self.assertRaisesRegex(RuntimeError, 'permissao negada'):
            self.call('claude', mode='denied')

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
