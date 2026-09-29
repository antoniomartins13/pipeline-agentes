import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from usage_core import collect, summarize, window


def record(mid='m1', output=3, model='claude-opus-5', stamp='2026-09-22T02:00:00Z'):
    return {'timestamp': stamp, 'message': {'id': mid, 'model': model, 'usage': {
        'input_tokens': 2, 'cache_creation_input_tokens': 100, 'cache_read_input_tokens': 600000,
        'output_tokens': output, 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 100}}}}


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, rows):
        p = self.base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')

    def test_stream_mirror_children_and_error(self):
        error = record('err', 0, '<synthetic>', '2026-09-22T03:00:00Z')
        error['isApiErrorMessage'] = True
        error['message']['usage'] = dict.fromkeys(('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'), 0)
        self.write('original.jsonl', [record(), record(output=6883), error])
        self.write('mirror.jsonl', [record(output=6883)])
        self.write('original/subagents/worker.jsonl', [record('child', 10)])
        r = collect(self.base)
        s = summarize(r, '-03:00')
        self.assertEqual(s['totals']['calls'], 2)
        self.assertEqual(s['totals']['output_tokens'], 6893)
        self.assertEqual(s['totals']['subagent_calls'], 1)
        original = next(x for x in r['sessions'] if x['id'] == 'original')
        self.assertEqual(original['context_tokens'], 600102)
        self.assertTrue(original['latest_event']['error'])
        self.assertEqual(list(s['days']), ['2026-09-21'])
        self.assertAlmostEqual(s['cost_parts']['cache_1h'], .002)

    def test_unknown_price_or_ttl_is_not_zero(self):
        a, b = record('a', model='unknown'), record('b')
        del b['message']['usage']['cache_creation']
        self.write('s.jsonl', [a, b])
        s = summarize(collect(self.base))
        self.assertIsNone(s['estimated_usd'])
        self.assertEqual(s['unpriced_calls'], 2)
        self.assertIsNone(s['subscription_quota'])

    def test_dashboard_uses_shared_collector_without_recount(self):
        from dashboard import render
        self.write('s.jsonl', [record(), record(output=50)])
        report = collect(self.base)
        html = render(report)
        self.assertIn('1 chamadas principais', html)
        self.assertIn('Quota da assinatura: desconhecida', html)

    def test_bad_cache_detail_is_reported_not_crash(self):
        row = record()
        row['message']['usage']['cache_creation'] = {'ephemeral_1h_input_tokens': 'unknown'}
        self.write('s.jsonl', [row])
        self.assertEqual(summarize(collect(self.base))['unpriced_calls'], 1)

    def test_equal_usage_without_id_is_not_dropped(self):
        a = record()
        del a['message']['id']
        self.write('s.jsonl', [a, a])
        self.assertEqual(len(collect(self.base)['calls']), 2)

    def test_missing_empty_and_partial_files(self):
        self.assertFalse(collect(self.base / 'missing')['source_exists'])
        self.write('s.jsonl', [record()])
        with (self.base / 's.jsonl').open('a') as f:
            f.write('{partial')
        r = collect(self.base)
        self.assertEqual(len(r['calls']), 1)
        self.assertEqual(r['diagnostics']['malformed_lines'], 1)

    def test_window_has_no_invented_reset(self):
        now = dt.datetime(2026, 9, 22, 13, tzinfo=dt.timezone.utc)
        start, end, label = window(now=now)
        self.assertEqual(end - start, dt.timedelta(days=7))
        self.assertIn('desconhecido', label)
        start, end, _ = window('2026-09-15T10:00:00-03:00', now, '-03:00')
        self.assertEqual(start, now)
        with self.assertRaises(ValueError):
            window('2027-01-01', now)

    def test_window_half_open_and_session_filter(self):
        self.write('a.jsonl', [record('a')])
        self.write('b.jsonl', [record('b', stamp='2026-09-23T02:00:00Z')])
        r = collect(self.base)
        s = summarize(r, end=dt.datetime(2026, 9, 23, 2, tzinfo=dt.timezone.utc))
        self.assertEqual(s['totals']['calls'], 1)
        self.assertEqual(summarize(r, session_ids=['b'])['totals']['calls'], 1)


if __name__ == '__main__':
    unittest.main()
