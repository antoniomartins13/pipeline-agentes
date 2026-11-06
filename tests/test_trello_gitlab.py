"""Espelho Trello -> GitLab com Trello e GitLab falsos: nada de rede."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import trello_gitlab as tg  # noqa: E402

LISTS = [{'id': 'l1', 'name': 'Em andamento'}, {'id': 'l2', 'name': 'Concluído'}, {'id': 'l3', 'name': 'Arquivo'}]


def card(short, lista, members=('me',), name='Card'):
    return {'name': name + ' ' + short, 'desc': 'Descricao', 'idList': lista, 'idMembers': list(members),
            'shortUrl': 'https://trello.com/c/' + short, 'shortLink': short, 'closed': False, 'checklists': []}


class EspelhoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        task = root / 'tarefas' / 'minha-back'
        task.mkdir(parents=True)
        (task / 'task-spec.md').write_text('# Spec\nCard https://trello.com/c/MEU1/x\n', encoding='utf-8')
        (task / 'task.json').write_text(json.dumps({'repo': 'erp-back-end'}), encoding='utf-8')
        (task / 'review-report.md').write_text('parecer privado', encoding='utf-8')
        for target, value in ((tg, root), (tg.runner, root)):
            p = patch.object(target, 'ROOT', value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('STATE', root / '.local/gestao.json'),):
            p = patch.object(tg, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(tg.runner, 'settings', return_value={'trello_board': 'B', 'gestao_project': 'g/gestao'})
        p.start()
        self.addCleanup(p.stop)
        self.cards = [card('MEU1', 'l1'), card('OUTRO', 'l1', members=('alguem',)), card('VELHO', 'l2'),
                      card('ARQ', 'l3')]
        self.calls = []

        def trello(path, params=None):
            if path == 'members/me':
                return {'id': 'me'}
            if path.endswith('/lists'):
                return LISTS
            return self.cards

        def gitlab(method, path, body=None):
            self.calls.append((method, path, body))
            if method == 'POST' and path.endswith('/issues'):
                return {'iid': 7, 'web_url': 'https://gl/issues/7'}
            if method == 'POST' and path.endswith('/notes'):
                return {'id': len(self.calls)}
            return {}
        for name, fake in (('trello_get', trello), ('gitlab', gitlab)):
            p = patch.object(tg, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def test_so_meus_cards_uma_issue_docs_sem_parecer_e_fecha_no_concluido(self):
        tg.sync()
        created = [c for c in self.calls if c[0] == 'POST' and c[1].endswith('/issues')]
        self.assertEqual(len(created), 1)  # OUTRO nao e meu; VELHO concluido sem issue; ARQ arquivado
        self.assertIn('<!-- trello:MEU1 -->', created[0][2]['description'])
        self.assertEqual(created[0][2]['labels'], 'fase::andamento,pipeline,repo::back-end')
        notes = [c[2]['body'] for c in self.calls if c[1].endswith('/notes')]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith('### Spec'))
        self.assertFalse(any('parecer privado' in json.dumps(c[2] or {}) for c in self.calls))
        # Segunda rodada sem mudanca: nada novo.
        self.calls.clear()
        tg.sync()
        self.assertEqual(self.calls, [])
        # Card vai para Concluido: atualiza labels e fecha.
        self.cards[0]['idList'] = 'l2'
        tg.sync()
        self.assertIn(('PUT', 'projects/g%2Fgestao/issues/7', {'state_event': 'close'}), self.calls)
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/issues') for c in self.calls))


if __name__ == '__main__':
    unittest.main()
