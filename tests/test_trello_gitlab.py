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


def card(short, lista, members=('me',), name='Card', number=0):
    return {'name': name + ' ' + short, 'idShort': number, 'desc': 'Descricao', 'idList': lista, 'idMembers': list(members),
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
        rev = root / 'tarefas' / 'mr-10-20-acl'
        rev.mkdir()
        (rev / 'HANDOFF.md').write_text('Revisao do card 3905 (MR de colega).', encoding='utf-8')
        (rev / 'review-report.md').write_text('parecer da revisao privado', encoding='utf-8')
        # Tarefa dev que so cita o numero do card de revisao (ex.: MR base): nao vira dev desse card.
        cita = root / 'tarefas' / 'outra-dev'
        cita.mkdir()
        (cita / 'task-spec.md').write_text('Base: MR do card 3905.', encoding='utf-8')
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
        self.cards = [card('MEU1', 'l1', number=1111), card('OUTRO', 'l1', members=('alguem',)), card('VELHO', 'l2'),
                      card('ARQ', 'l3'), card('REV1', 'l1', number=3905)]
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
        self.assertEqual(len(created), 2)  # OUTRO nao e meu; VELHO concluido sem issue; ARQ arquivado
        self.assertIn('<!-- trello:MEU1 -->', created[0][2]['description'])
        self.assertEqual(created[0][2]['labels'], 'fase::andamento,papel::dev,pipeline,repo-back-end')
        self.assertEqual(created[1][2]['labels'], 'fase::andamento,papel::revisao')
        self.assertIn('revisão de MR de colega', created[1][2]['description'])
        notes = [c[2]['body'] for c in self.calls if c[1].endswith('/notes')]
        self.assertEqual(len(notes), 2)  # a spec da tarefa e o relatorio de mudancas do card
        self.assertTrue(notes[0].startswith('### Spec'))
        self.assertTrue(notes[1].startswith('# Relatório de mudanças'))
        self.assertFalse(any(x in json.dumps(c[2] or {}, ensure_ascii=False)
                             for x in ('parecer privado', 'parecer da revisao privado', 'Base: MR', 'Revisao do card')
                             for c in self.calls))
        # Segunda rodada sem mudanca: nada novo.
        self.calls.clear()
        tg.sync()
        self.assertEqual(self.calls, [])
        # Card vai para Concluido: atualiza labels e fecha.
        self.cards[0]['idList'] = 'l2'
        tg.sync()
        self.assertIn(('PUT', 'projects/g%2Fgestao/issues/7', {'state_event': 'close'}), self.calls)
        self.assertFalse(any(c[0] == 'POST' and c[1].endswith('/issues') for c in self.calls))
        self.assertEqual(sum(1 for c in self.calls if c[1].endswith('/issues/7') and c[2] == {'state_event': 'close'}), 1)

    def test_card_declarado_vence_revisao_e_gera_relatorio_com_mr(self):
        # Card que comecou como revisao (mr-*) e virou desenvolvimento do dono, declarado em gestao_cards.
        root = tg.ROOT
        (root / 'tarefas' / 'minha-back' / 'documentacao-tecnica.md').write_text(
            '# Doc\n\n## Resumo\nNovo campo Data de Assinatura.\n\n## Roteiro de teste manual\n1. x\n', encoding='utf-8')
        amb = root / '.local' / 'ambientes'
        amb.mkdir(parents=True)
        (amb / 'ent.json').write_text(json.dumps({'tarefas': [{'task': 'minha-back', 'mr': {'url': 'https://gl/mr/620'}}]}))
        settings = {'trello_board': 'B', 'gestao_project': 'g/gestao',
                    'gestao_cards': {'REV1': {'papel': 'dev', 'tarefas': ['minha-back'], 'origem': 'mr-10-20-acl'}}}
        with patch.object(tg.runner, 'settings', return_value=settings):
            tg.sync()
        rev = [c for c in self.calls if c[0] == 'POST' and c[1].endswith('/issues') and 'REV1' in c[2]['title']][0]
        self.assertEqual(rev[2]['labels'], 'fase::andamento,papel::dev,pipeline,repo-back-end')
        bodies = [c[2]['body'] for c in self.calls if c[1].endswith('/notes')]
        self.assertTrue(any('Assumido a partir da revisão `mr-10-20-acl`' in b for b in bodies))
        relatorio = [b for b in bodies if b.startswith('# Relatório de mudanças')]
        self.assertTrue(relatorio and 'Novo campo Data de Assinatura.' in relatorio[0] and 'https://gl/mr/620' in relatorio[0])
        self.assertFalse(any('parecer da revisao privado' in json.dumps(c[2] or {}) for c in self.calls))
        self.assertTrue((root / 'relatorios' / 'cards' / '3905-REV1.md').exists())
        # MEU1 era da minha-back por link; agora a tarefa e declarada do REV1, entao MEU1 nao a leva junto.
        meu = [c for c in self.calls if c[0] == 'POST' and c[1].endswith('/issues') and 'MEU1' in c[2]['title']][0]
        self.assertEqual(meu[2]['labels'], 'fase::andamento,papel::dev')

    def test_card_no_task_json_declara_dono(self):
        root = tg.ROOT
        (root / 'tarefas' / 'outra-dev' / 'task.json').write_text(
            json.dumps({'repo': 'erp-front-end', 'card': 'https://trello.com/c/REV1/3905-x', 'origem': 'mr-10-20-acl'}))
        tasks, origem = tg.tasks_for_card({'shortLink': 'REV1', 'idShort': 3905})
        self.assertEqual([(t, p) for t, _, p in tasks], [('outra-dev', 'dev')])
        self.assertEqual(origem, 'mr-10-20-acl')

    def test_mrs_da_revisao_pareados_pela_branch_sem_conteudo_da_pasta(self):
        rev = tg.ROOT / 'tarefas' / 'mr-10-20-acl'
        (rev / 'HANDOFF.md').write_text('Revisao do card 3905. Rodada 2 consolidada em !30 (back) e !31 (front).',
                                        encoding='utf-8')
        existe = {('back', '10'): 'b1', ('front', '10'): 'antigo', ('front', '20'): 'b1', ('back', '30'): 'b2',
                  ('front', '31'): 'b2', ('back', '31'): 'outra-coisa'}

        def gitlab(method, path, body=None):
            proj, n = path.split('/')[1].split('%2F')[-1], path.rsplit('/', 1)[-1]
            if (proj, n) not in existe:
                raise RuntimeError('404')
            return {'iid': int(n), 'web_url': f'https://gl/{proj}/{n}', 'state': 'opened', 'source_branch': existe[(proj, n)]}
        with patch.object(tg, 'gitlab', side_effect=gitlab), \
                patch.object(tg.runner, 'settings', return_value={'gestao_mr_projects': ['g/back', 'g/front']}):
            mrs = tg.review_mrs('mr-10-20-acl', {})
        self.assertEqual([(m['repo'], m['iid']) for m in mrs],
                         [('back', 30), ('back', 10), ('front', 31), ('front', 20)])
        body = tg.issue_body(card('REV1', 'l1'), 'Em andamento', True, mrs)
        self.assertIn('[back !30](https://gl/back/30) (aberto)', body)
        self.assertNotIn('Rodada 2', body)


if __name__ == '__main__':
    unittest.main()
