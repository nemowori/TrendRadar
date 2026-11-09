import json
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('market_movers_under_test', Path(__file__).resolve().parents[1] / 'trendradar/market_movers.py')
market = importlib.util.module_from_spec(spec)
spec.loader.exec_module(market)

NOW = datetime(2026, 11, 5, 0, 20, tzinfo=market.CHINA)
QUOTED = datetime(2026, 11, 4, 15, 35, tzinfo=market.CHINA)
ART = 'AN202611041831415468'


def row(code='688613', change=-19.72, **overrides):
    return {'f12': code, 'f14': '示例医疗', 'f2': 8.75, 'f3': change,
            'f6': 309151259.0, 'f8': 25.86, 'f13': 1, 'f124': QUOTED.timestamp(), **overrides}


class MarketMoversTests(unittest.TestCase):
    def test_percentage_units_sorting_and_duplicates(self):
        payload = {'data': {'diff': [row(change=-5), row('600001', -10), row('600002', 8), row('600001', -10)]}}
        items = market.parse_ranking(payload, 'loss', NOW)
        self.assertEqual([i['code'] for i in items], ['600001', '688613'])
        self.assertEqual(items[0]['change_pct'], -10)
        self.assertEqual(items[0]['amount_yuan'], 309151259.0)
        self.assertEqual(items[0]['quoted_at'], QUOTED.isoformat())

    def test_missing_nonfinite_and_future_values_are_rejected(self):
        rows = [row(f2='-'), row(f3=float('nan')), row(f124=(NOW + timedelta(days=1)).timestamp()), row(f14='坏\ufffd名称'), row('not-a-code')]
        self.assertEqual(market.parse_ranking({'data': {'diff': rows}}, 'loss', NOW), [])

    def test_actual_publication_time_precedes_document_date(self):
        actual, exact = market._notice_time({'display_time': '2026-11-04 18:26:17:237', 'notice_date': '2026-11-05 00:00:00'})
        self.assertTrue(exact)
        self.assertEqual(actual.hour, 18)
        self.assertEqual(actual.day, 4)
        self.assertGreater(actual, QUOTED)

    def test_evidence_checks_company_and_chronology(self):
        quote = market.parse_ranking({'data': {'diff': [row()]}}, 'loss', NOW)[0]
        listing = {'data': {'list': [
            {'art_code': ART, 'codes': [{'stock_code': '688613'}], 'title': '示例医疗:风险提示', 'display_time': '2026-11-04 18:26:17:237'},
            {'art_code': 'AN202611031831380208', 'codes': [{'stock_code': '600001'}], 'title': '另一家公司公告', 'display_time': '2026-11-03 18:00:00'},
        ]}}
        detail = {'data': {'art_code': ART, 'security': [{'stock': '688613'}], 'notice_content': '公司披露风险；这不是股价因果证明。'}}
        with patch.object(market, '_json_get', side_effect=[listing, detail]):
            evidence = market._evidence(quote, NOW)
        self.assertEqual(len(evidence['announcements']), 1)
        self.assertIn('不能解释此前下跌', evidence['announcements'][0]['timing'])
        self.assertIn('不是股价因果证明', evidence['announcements'][0]['excerpt'])

    def test_date_only_is_not_treated_as_verified_pre_quote_evidence(self):
        quote = market.parse_ranking({'data': {'diff': [row()]}}, 'loss', NOW)[0]
        listing = {'data': {'list': [{'art_code': ART, 'codes': [{'stock_code': '688613'}], 'title': '风险提示', 'notice_date': '2026-11-04 00:00:00'}]}}
        with patch.object(market, '_json_get', side_effect=[listing, {'data': {}}]):
            evidence = market._evidence(quote, NOW)
        self.assertIn('发布时间不确定', evidence['announcements'][0]['timing'])

    def test_failure_replaces_previous_success_instead_of_reusing_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'latest.json'
            path.write_text(json.dumps({'status': 'success', 'quotes': ['old price']}), encoding='utf-8')
            with patch.object(market, 'SNAPSHOT_PATH', path), patch.object(market, '_ranking', side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):
                    market.fetch_market_movers(NOW)
                data = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(data['status'], 'unavailable')
                self.assertEqual(data['quotes'], [])
                self.assertIn('未取得可用', market.read_market_context(path, NOW))

    def test_stale_or_different_run_context_is_not_supplied_to_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'latest.json'
            path.write_text(json.dumps({'status': 'success', 'fetched_at': (NOW - timedelta(hours=1)).isoformat(), 'run_id': '1'}), encoding='utf-8')
            self.assertIn('未取得可用', market.read_market_context(path, NOW))
            path.write_text(json.dumps({'status': 'success', 'fetched_at': NOW.isoformat(), 'run_id': '1'}), encoding='utf-8')
            with patch.dict('os.environ', {'GITHUB_RUN_ID': '2'}):
                self.assertIn('不使用历史缓存', market.read_market_context(path, NOW))

    def test_new_source_uses_existing_crawler_and_domain_validation(self):
        fetch_spec = importlib.util.spec_from_file_location('market_fetcher_under_test', Path(__file__).resolve().parents[1] / 'trendradar/crawler/fetcher.py')
        fetcher_module = importlib.util.module_from_spec(fetch_spec)
        fetch_spec.loader.exec_module(fetcher_module)
        package = types.ModuleType('trendradar')
        package.__path__ = []
        payload = {'status': 'success', 'items': [{'title': 'A股行情快照｜跌幅榜1 示例(688613) -19.72%', 'url': 'https://quote.eastmoney.com/sh688613.html'}]}
        with patch.dict('sys.modules', {'trendradar': package, 'trendradar.market_movers': market}), patch.object(market, 'fetch_market_movers', return_value=payload):
            results, names, failed = fetcher_module.DataFetcher().crawl_websites([('a-share-movers', 'A股榜单')], domain_rules={'a-share-movers': 'eastmoney.com'})
        self.assertEqual(failed, [])
        self.assertEqual(names['a-share-movers'], 'A股榜单')
        self.assertEqual(len(results['a-share-movers']), 1)


class MarketIndicesTests(unittest.TestCase):
    def test_turnover_uses_a_share_quotes_and_correct_units(self):
        rows = [
            row('000001', 0.55, f13=1, f2=3669.42, f5=900_000_000, f6=900_000_000_000),
            row('000002', 0.56, f13=1, f5=200_000_000, f6=400_000_000_000),
            row('399107', 0.59, f13=0, f5=300_000_000, f6=500_000_000_000),
        ]
        with patch.object(market, '_json_get', return_value={'rc': 0, 'data': {'diff': rows}}):
            items = market.fetch_market_indices(NOW)['items']
        self.assertEqual(len(items), 2)
        self.assertIn('成交量 5.00亿手，成交额 9000.00亿元', items[0]['title'])
        self.assertIn('上证指数 3669.42点 +0.55%', items[1]['title'])
        self.assertTrue(all('2026-11-04 15:35' in item['title'] for item in items))

    def test_mixed_session_does_not_add_old_quotes_or_turnover(self):
        previous_session = (QUOTED - timedelta(days=1)).timestamp()
        rows = {
            '0': row('000001', 0.55, f13=1),
            '1': row('000002', 0.56, f13=1, f5=200_000_000, f6=400_000_000_000),
            '2': row('399107', 0.59, f13=0, f5=300_000_000, f6=500_000_000_000, f124=previous_session),
            '3': row('399001', 0.49, f13=0, f124=previous_session),
            '4': row('399006', float('nan'), f13=0),
        }
        with patch.object(market, '_json_get', return_value={'rc': 0, 'data': {'diff': rows}}):
            items = market.fetch_market_indices(NOW)['items']
        self.assertEqual(len(items), 1)
        self.assertIn('上证指数', items[0]['title'])


if __name__ == '__main__':
    unittest.main()
