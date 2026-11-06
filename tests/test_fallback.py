"""Troca automatica de modelo por cota (invoke_fallback em scripts/pipeline.py)."""
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

CHAIN = ['claude', 'agy:claude-opus-5-5-high', 'agy:gemini-4-argon-high', 'agy:gemini-3.8-flash-high']
NOW = 1_800_000_000.0


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.usage = base / 'claude.json'
        self.config = {'agy_model': 'gemini-4-argon-high',
                       'fallback': {'chain': CHAIN, 'claude_usage_file': str(self.usage), 'claude_threshold': 85, 'exhausted_hours': 5}}
        self.run = base / 'run'
        self.run.mkdir()
        self.quota = base / 'quota.json'
        patcher = patch.object(runner, 'quota_state_path', lambda: self.quota)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def usage_at(self, pct, resets=NOW + 3600):
        self.usage.write_text(json.dumps({'five_hour': {'utilization': pct, 'resets_at': resets}}), encoding='utf-8')

    def worker(self, outcomes):
        """Fake do invoke: cada candidato tem um desfecho ('ok', 'cota' ou 'erro')."""
        def invoke_one(provider, prompt, repo, run, phase, config):
            key = provider + (':' + config['agy_model'] if provider == 'agy' else '')
            self.calls.append(key)
            outcome = outcomes.get(key, 'ok')
            if outcome == 'cota':
                (run / 'stderr.log').write_text('API Error: 429 usage_limit_reached', encoding='utf-8')
                raise RuntimeError(provider + ' saiu com codigo 1; ver stderr.log')
            if outcome == 'erro':
                (run / 'stderr.log').write_text('TypeError: algo quebrou', encoding='utf-8')
                raise RuntimeError(provider + ' saiu com codigo 1; ver stderr.log')
            return {'status': 'completed'}, {'session_id': 's'}
        return invoke_one

    def go(self, provider, outcomes=None):
        return runner.invoke_fallback(provider, 'p', '/repo', self.run, 'review', self.config,
                                      invoke_one=self.worker(outcomes or {}), clock=lambda: NOW)

    def test_ordem_comeca_no_provedor_do_manifesto(self):
        self.assertEqual(runner.candidates_for('claude', self.config)[0], ('claude', None))
        self.assertEqual(runner.candidates_for('agy', self.config),
                         [('agy', 'gemini-4-argon-high'), ('agy', 'gemini-3.8-flash-high')])

    def test_claude_livre_roda_claude(self):
        self.usage_at(40)
        _, meta = self.go('claude')
        self.assertEqual(self.calls, ['claude'])
        self.assertEqual(meta['executado_por'], 'claude')
        self.assertNotIn('fallback', meta)

    def test_claude_perto_do_limite_pula_antes_de_estourar(self):
        self.usage_at(90)
        _, meta = self.go('claude')
        self.assertEqual(self.calls, ['agy:claude-opus-5-5-high'])
        self.assertEqual(meta['fallback'], [{'candidato': 'claude', 'pulado': 'esgotado'}])

    def test_erro_de_cota_marca_esgotado_e_segue_a_ordem(self):
        self.usage_at(10)
        _, meta = self.go('claude', {'claude': 'cota', 'agy:claude-opus-5-5-high': 'cota'})
        self.assertEqual(self.calls, ['claude', 'agy:claude-opus-5-5-high', 'agy:gemini-4-argon-high'])
        self.assertEqual(meta['executado_por'], 'agy:gemini-4-argon-high')
        state = json.loads(self.quota.read_text(encoding='utf-8'))
        self.assertEqual(set(state), {'claude', 'agy:claude-opus-5-5-high'})
        # Os logs das tentativas ficam guardados com o nome do candidato.
        self.assertTrue((self.run / 'stderr.log.claude').exists())
        # Na fase seguinte, os esgotados nem sao tentados.
        self.calls.clear()
        self.go('claude')
        self.assertEqual(self.calls, ['agy:gemini-4-argon-high'])

    def test_renovou_volta_ao_primeiro(self):
        self.usage_at(10, resets=NOW - 10)
        self.quota.write_text(json.dumps({'claude': NOW - 1}), encoding='utf-8')
        self.go('claude')
        self.assertEqual(self.calls, ['claude'])

    def test_erro_que_nao_e_cota_nao_troca(self):
        self.usage_at(10)
        with self.assertRaises(RuntimeError):
            self.go('claude', {'claude': 'erro'})
        self.assertEqual(self.calls, ['claude'])
        self.assertFalse(self.quota.exists())

    def test_timeout_nunca_troca_de_candidato(self):
        def timeout(*_args):
            (self.run / 'stderr.log').write_text('429', encoding='utf-8')
            raise RuntimeError('Timeout: processo 1 pode estar vivo. Nenhum redisparo; verificar antes de recover.')
        with self.assertRaises(RuntimeError):
            runner.invoke_fallback('claude', 'p', '/repo', self.run, 'review', self.config, invoke_one=timeout, clock=lambda: NOW)
        self.assertFalse(self.quota.exists())

    def test_todos_sem_cota_bloqueia_com_a_lista(self):
        self.usage_at(10)
        outcomes = {k: 'cota' for k in CHAIN}
        with self.assertRaises(RuntimeError) as ctx:
            self.go('claude', outcomes)
        self.assertIn('sem cota', str(ctx.exception))

    def test_agy_nao_cai_para_claude(self):
        self.usage_at(10)
        with self.assertRaises(RuntimeError):
            self.go('agy', {'agy:gemini-4-argon-high': 'cota', 'agy:gemini-3.8-flash-high': 'cota'})
        self.assertEqual(self.calls, ['agy:gemini-4-argon-high', 'agy:gemini-3.8-flash-high'])


    def negando(self, falhas):
        """Fake do agy: as primeiras `falhas` chamadas terminam em negativa de permissao."""
        prompts = []
        def invoke_one(provider, prompt, repo, run, phase, config):
            prompts.append(prompt)
            (run / 'stdout.jsonl').write_text('{}', encoding='utf-8')
            if len(prompts) <= falhas:
                (run / 'stderr.log').write_text('auto-denied', encoding='utf-8')
                raise RuntimeError('agy retornou erro ou permissao negada; entrega nao aceita')
            return {'status': 'completed'}, {'session_id': 's'}
        return invoke_one, prompts

    def test_agy_negado_em_fase_de_leitura_repete_uma_vez_com_a_negativa(self):
        invoke_one, prompts = self.negando(1)
        _, meta = runner.invoke_fallback('agy', 'p', '/repo', self.run, 'docs', self.config, invoke_one=invoke_one,
                                         clock=lambda: NOW, denial=lambda run: 'denied: git -C x status')
        self.assertEqual(len(prompts), 2)
        self.assertIn('denied: git -C x status', prompts[1])
        self.assertEqual(meta['repetida_apos_negativa'], 'denied: git -C x status')
        self.assertTrue((self.run / 'stderr.log.negada').exists())
        # Negativa nao e cota: o candidato nao fica esgotado.
        self.assertFalse(self.quota.exists())

    def test_agy_negado_duas_vezes_bloqueia(self):
        invoke_one, prompts = self.negando(2)
        with self.assertRaisesRegex(RuntimeError, 'permissao negada'):
            runner.invoke_fallback('agy', 'p', '/repo', self.run, 'review', self.config, invoke_one=invoke_one,
                                   clock=lambda: NOW, denial=lambda run: 'x')
        self.assertEqual(len(prompts), 2)

    def test_agy_negado_no_dev_tambem_repete_uma_vez(self):
        invoke_one, prompts = self.negando(1)
        runner.invoke_fallback('agy', 'p', '/repo', self.run, 'dev', self.config, invoke_one=invoke_one,
                               clock=lambda: NOW, denial=lambda run: 'x')
        self.assertEqual(len(prompts), 2)

    def test_erro_que_nao_e_negativa_nao_repete(self):
        calls = []
        def invoke_one(provider, prompt, repo, run, phase, config):
            calls.append(1)
            raise RuntimeError('agy saiu com codigo 1; ver stderr.log')
        with self.assertRaisesRegex(RuntimeError, 'codigo 1'):
            runner.invoke_fallback('agy', 'p', '/repo', self.run, 'docs', self.config, invoke_one=invoke_one,
                                   clock=lambda: NOW, denial=lambda run: 'x')
        self.assertEqual(len(calls), 1)

if __name__ == '__main__':
    unittest.main()
