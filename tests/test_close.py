"""pipeline.py close: encerra a tarefa no runner sem apagar nada."""
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pipeline as runner
os.environ["PIPELINE_SEM_AMBIENTE"] = "1"  # nunca abrir MR/servidor de verdade nos testes


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'pipeline'
        (self.root / 'tarefas' / 'demo').mkdir(parents=True)
        (self.root / 'config').mkdir()
        (self.root / 'config' / 'pipeline.json').write_text(json.dumps({'max_slices_per_run': 6}), encoding='utf-8')
        (self.root / 'tarefas' / 'demo' / 'task.json').write_text(json.dumps({'repo': 'x', 'branch': 'b'}), encoding='utf-8')
        self.local = self.root / '.local' / 'tasks' / 'demo'
        self.local.mkdir(parents=True)
        self.state = {'phase': 'ready_for_draft', 'created_at': '2026-10-26T00:00:00+00:00', 'handoff': {'summary': 's'}}
        (self.local / 'state.json').write_text(json.dumps(self.state), encoding='utf-8')
        p = patch.object(runner, 'ROOT', self.root)
        p.start()
        self.addCleanup(p.stop)

    def test_encerra_arquiva_e_nao_roda_mais(self):
        with self.assertRaises(ValueError):
            runner.close('demo', '  ')
        closed = runner.close('demo', 'MR !602 mergeado em 26/10')
        self.assertEqual(closed['phase'], 'closed')
        self.assertEqual(closed['closed_phase'], 'ready_for_draft')
        self.assertEqual(closed['reason'], 'MR !602 mergeado em 26/10')
        archived = list((self.local / 'archive').glob('*-state.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding='utf-8')), self.state)
        self.assertFalse((self.local / 'runner.lock').exists())
        # De novo: nada muda (sem segundo arquivo).
        runner.close('demo', 'outro motivo')
        self.assertEqual(len(list((self.local / 'archive').glob('*-state.json'))), 1)
        # run numa tarefa fechada so devolve o estado.
        with patch.object(runner, 'repository', lambda m: self.root):
            self.assertEqual(runner.execute('demo', executor=None)['phase'], 'closed')

    def test_recusa_com_execucao_sem_desfecho(self):
        run = self.root / '.local' / 'runs' / 'demo' / '20261103-000000-abc'
        run.mkdir(parents=True)
        (run / 'receipt.json').write_text(json.dumps({'status': 'running'}), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            runner.close('demo', 'entregue')
        self.assertEqual(json.loads((self.local / 'state.json').read_text(encoding='utf-8'))['phase'], 'ready_for_draft')


if __name__ == '__main__':
    unittest.main()
