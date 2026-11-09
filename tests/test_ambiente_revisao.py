"""Ambiente de revisao (scripts/ambiente_revisao.py): partes que nao tocam git, GitLab nem servidores."""
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
os.environ["PIPELINE_SEM_AMBIENTE"] = "1"  # nunca abrir MR/servidor de verdade nos testes
import ambiente_revisao as amb

DOC = """# Documentacao tecnica

## Resumo
Publicacao em lote de pagamentos e cargo nas diarias.

## Detalhes
Achado interno que nao vai para o MR.

## Roteiro de teste manual
1. Abrir Pagamentos.
2. Publicar dois em lote.
"""


class AmbienteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name, data in {'a-back': {'entrega': 'ent'}, 'a-front': {'entrega': 'ent'}, 'solo': {}}.items():
            (self.root / 'tarefas' / name).mkdir(parents=True)
            (self.root / 'tarefas' / name / 'task.json').write_text(json.dumps(data), encoding='utf-8')
            (self.root / 'tarefas' / name / 'task-spec.md').write_text(
                '# [TRELLO-1] Titulo da ' + name + '\nCard https://trello.com/c/AbC123/1-x\n', encoding='utf-8')
        (self.root / 'tarefas' / 'a-back' / 'documentacao-tecnica.md').write_text(DOC, encoding='utf-8')
        for target in (amb, amb.runner):
            p = patch.object(target, 'ROOT', self.root)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(amb, 'STATE_DIR', self.root / '.local' / 'ambientes')
        p.start()
        self.addCleanup(p.stop)

    def test_membros_da_entrega_ou_a_propria_tarefa(self):
        self.assertEqual(amb.members('ent'), ['a-back', 'a-front'])
        self.assertEqual(amb.members('solo'), ['solo'])
        with self.assertRaises(ValueError):
            amb.members('nao-existe')

    def test_descricao_do_mr_so_tem_resumo_e_card(self):
        desc = amb.mr_description('a-back')
        self.assertIn('Publicacao em lote', desc)
        self.assertIn('https://trello.com/c/AbC123/1-x', desc)
        self.assertNotIn('Achado interno', desc)
        self.assertNotIn('Abrir Pagamentos', desc)
        self.assertEqual(amb.spec_title('a-back'), '[TRELLO-1] Titulo da a-back')

    def test_secao_do_roteiro(self):
        self.assertEqual(amb.section(DOC, 'Roteiro de teste manual'), '1. Abrir Pagamentos.\n2. Publicar dois em lote.')
        self.assertEqual(amb.section(DOC, 'Inexistente'), '')

    def test_portas_fixas_por_alvo_e_devolvidas_ao_encerrar(self):
        a = amb.ports_for('ent')
        self.assertEqual(amb.ports_for('ent'), a)
        b = amb.ports_for('solo')
        self.assertNotEqual(a['back'], b['back'])
        self.assertEqual(a['back'], (amb.PORT_BASE + 10, amb.PORT_BASE + 15))
        amb.free_ports('ent')
        self.assertEqual(amb.ports_for('outro'), a)  # bloco liberado volta

    def test_so_prepara_com_todas_as_tarefas_prontas(self):
        local = self.root / '.local' / 'tasks'
        for t, phase in (('a-back', 'ready_for_draft'), ('a-front', 'review')):
            (local / t).mkdir(parents=True)
            (local / t / 'state.json').write_text(json.dumps({'phase': phase}), encoding='utf-8')
        self.assertEqual(amb.preparar('ent'), {'alvo': 'ent', 'aguardando': ['a-front']})
        with self.assertRaises(ValueError):
            amb.preparar('../etc')

    def test_tipo_do_projeto(self):
        repo = self.root / 'repo'
        repo.mkdir()
        self.assertIsNone(amb.kind_of(repo))
        (repo / 'package.json').write_text(json.dumps({'dependencies': {'next': '16'}}), encoding='utf-8')
        self.assertEqual(amb.kind_of(repo), 'portal')
        (repo / 'package.json').write_text(json.dumps({'devDependencies': {'vite': '7'}}), encoding='utf-8')
        self.assertEqual(amb.kind_of(repo), 'front')
        (repo / 'artisan').write_text('', encoding='utf-8')
        self.assertEqual(amb.kind_of(repo), 'back')

    def test_seed_so_no_banco_local_sem_operacao_proibida_e_uma_vez_por_versao(self):
        repo = self.root / 'back'
        (repo / '.revisao').mkdir(parents=True)
        self.assertEqual(amb.apply_seed('ent', repo)['status'], 'sem_seed')
        seed = repo / '.revisao' / 'seed.php'
        seed.write_text('Processo::firstOrCreate(["n" => 1]); echo "Processo 1";', encoding='utf-8')
        (repo / '.env').write_text('DB_HOST=10.0.0.5\nAPP_ENV=local\n', encoding='utf-8')
        self.assertIn('nao e local', amb.apply_seed('ent', repo)['motivo'])
        (repo / '.env').write_text('DB_HOST=127.0.0.1\nAPP_ENV=local\n', encoding='utf-8')
        seed.write_text('Processo::query()->delete();', encoding='utf-8')
        self.assertEqual(amb.apply_seed('ent', repo)['status'], 'barrado')
        seed.write_text('Processo::firstOrCreate(["n" => 1]); echo "Processo 1";', encoding='utf-8')
        ran = []

        def fake_run(args, **kwargs):
            ran.append(args)
            return type('R', (), {'returncode': 0, 'stdout': 'Processo 1\n', 'stderr': ''})()
        with patch.object(amb.subprocess, 'run', side_effect=fake_run):
            first = amb.apply_seed('ent', repo)
            self.assertEqual((first['status'], first['registros']), ('aplicado', ['Processo 1']))
            amb.runner.save(amb.STATE_DIR / 'ent.json', {'seed': first})
            self.assertEqual(amb.apply_seed('ent', repo), first)  # mesma versao: nao roda de novo
        self.assertEqual(len(ran), 1)
        self.assertIn('tinker', ran[0])

    def test_pasta_revisao_fica_fora_do_git(self):
        import subprocess
        repo = self.root / 'git'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        amb.runner.exclude_revisao(repo)
        amb.runner.exclude_revisao(repo)  # idempotente
        (repo / '.revisao').mkdir()
        (repo / '.revisao' / 'seed.php').write_text('echo 1;', encoding='utf-8')
        out = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'], capture_output=True, text=True).stdout
        self.assertEqual(out, '')
        self.assertEqual((repo / '.git/info/exclude').read_text().count('/.revisao/'), 1)


    def test_seed_sem_tag_php_executa_e_nao_imprime(self):
        repo = self.root / 'back2'
        (repo / '.revisao').mkdir(parents=True)
        (repo / '.env').write_text('DB_HOST=127.0.0.1\nAPP_ENV=local\n', encoding='utf-8')
        (repo / '.revisao' / 'seed.php').write_text('use App\\X;\nX::firstOrCreate(["n" => 1]);\necho "ok";', encoding='utf-8')

        def fake_run(args, **kwargs):
            return type('R', (), {'returncode': 0, 'stdout': 'ok\n', 'stderr': ''})()
        with patch.object(amb.subprocess, 'run', side_effect=fake_run):
            res = amb.apply_seed('ent', repo)
        self.assertEqual(res['registros'], ['ok'])
        self.assertTrue((repo / '.revisao' / '.seed-exec.php').read_text().startswith('<?php\nuse App'))

        def echo_run(args, **kwargs):  # PHP imprimiu o arquivo em vez de rodar
            code = (repo / '.revisao' / '.seed-exec.php').read_text()[6:]
            return type('R', (), {'returncode': 0, 'stdout': code, 'stderr': ''})()
        (repo / '.revisao' / 'seed.php').write_text('X::firstOrCreate(["n" => 2]);', encoding='utf-8')
        with patch.object(amb.subprocess, 'run', side_effect=echo_run):
            self.assertEqual(amb.apply_seed('ent', repo)['status'], 'erro')

    def test_coordenadora_no_tmux_reabre_a_conversa_e_recebe_o_roteiro_novo(self):
        (self.root / 'tarefas' / 'ent').mkdir(exist_ok=True)
        rot = self.root / 'tarefas' / 'ent' / 'roteiro-teste.md'
        rot.write_text('v1', encoding='utf-8')
        info = {'roteiro': 'tarefas/ent/roteiro-teste.md', 'tarefas': []}
        projects = self.root / 'projects'
        conv = projects / re.sub(r'[^A-Za-z0-9]', '-', str(self.root / 'tarefas' / 'ent'))
        conv.mkdir(parents=True)
        (conv / 'sess-1.jsonl').write_text('{"content": "Você é a sessão coordenadora da funcionalidade ent"}', encoding='utf-8')
        calls, alive = [], {'v': False}

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[:2] == ['tmux', 'has-session']:
                return type('R', (), {'returncode': 0 if alive['v'] else 1, 'stdout': '', 'stderr': ''})()
            if args[:2] == ['tmux', 'new-session']:
                alive['v'] = True
            return type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        with patch.object(amb, 'CLAUDE_PROJECTS', projects), patch.object(amb.subprocess, 'run', side_effect=fake_run):
            # Coordenadora antiga (segundo plano, sem tmux): reabre a MESMA conversa, sem prompt novo (roteiro igual).
            info['coordenador'] = {'id': 'abc', 'versao': amb.roteiro_versao(info)}
            res = amb.spawn_coordinator('ent', info)
            new = [c for c in calls if c[:2] == ['tmux', 'new-session']][0]
            self.assertEqual((res['sessao'], res['tmux']), ('sess-1', 'coord-ent'))
            self.assertEqual(new[new.index('--resume') + 1], 'sess-1')
            self.assertFalse(any('Atualização' in a for a in new))
            # Viva e roteiro igual: nada.
            info['coordenador'] = res
            calls.clear()
            self.assertEqual(amb.spawn_coordinator('ent', info)['sessao'], 'sess-1')
            self.assertEqual([c[:2] for c in calls], [['tmux', 'has-session']])
            # Viva e roteiro novo: cola a atualizacao na mesma sessao.
            rot.write_text('v2', encoding='utf-8')
            calls.clear()
            res = amb.spawn_coordinator('ent', info)
            self.assertEqual(res['versao'], amb.roteiro_versao(info))
            self.assertIn(['tmux', 'send-keys', '-t', '=coord-ent:', 'Enter'], calls)


if __name__ == '__main__':
    unittest.main()
