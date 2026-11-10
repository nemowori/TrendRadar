import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}):
    from trendradar.__main__ import NewsAnalyzer
    from trendradar.economic_calendar import _fed_range, _fed_release_time, nbs_calendar
    from trendradar.finance_brief import BriefHistory
    from trendradar.finance_sources import oil_prices, quote_item


TODAY = date(2026, 11, 10)


class BriefHistoryTests(unittest.TestCase):
    def test_delivery_history_preserves_new_numbers_and_confirmed_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.json"
            first = BriefHistory(TODAY, path)
            title = "中国10年期国债收益率升至1.59%"
            article = {"title": title, "url": "https://example.com/news?utm_source=first"}
            self.assertTrue(first.keep_news(**article))
            self.assertFalse(first.keep_news(title + " - Reuters", "https://other.example.com/article"))
            self.assertTrue(BriefHistory(TODAY, path).keep_news(**article))
            first.record([{"titles": [article]}], [], None)

            second = BriefHistory(TODAY, path)
            self.assertFalse(second.keep_news(title, "https://example.com/news?utm_medium=repeat"))
            self.assertTrue(second.keep_news("中国10年期国债收益率升至1.61%", article["url"]))
            self.assertTrue(second.keep_news("官方确认" + title, article["url"]))
            self.assertTrue(BriefHistory(TODAY + timedelta(days=4), path).keep_news(**article))

    def test_announcement_is_deduplicated_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.json"
            announcement = {"title": "中国海油业绩公告", "url": "https://example.com/document.pdf"}
            section = {"id": "watchlist-announcements", "items": [announcement]}
            history = BriefHistory(TODAY, path)
            self.assertEqual(history.keep_official(section["id"], [announcement]), [announcement])
            history.record([], [], {"platforms": [section]})
            self.assertEqual(BriefHistory(TODAY, path).keep_official(section["id"], [announcement]), [])

    def test_headlineless_brief_records_events_only_when_dingtalk_succeeds(self):
        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer.ctx = Mock()
        analyzer.ctx.config = {
            "FINANCE_BRIEF": {"enabled": True},
            "ENABLE_NOTIFICATION": True, "AI_ANALYSIS": {"ENABLED": False},
            "SHOW_VERSION_UPDATE": False,
        }
        analyzer.ctx.prepare_report.return_value = {}
        analyzer.ctx.platform_ids = []
        analyzer.ctx.get_time.return_value = datetime(2026, 11, 10, 11)
        analyzer.ctx.format_date.return_value = TODAY.isoformat()
        analyzer.ctx.create_scheduler.return_value.already_executed.return_value = False
        analyzer._has_notification_configured = Mock(return_value=True)
        analyzer._has_valid_content = Mock(return_value=False)
        analyzer.proxy_url = None
        analyzer.frequency_file = None
        analyzer._hotlist_total_count = analyzer._rss_matched_count = analyzer._rss_total_count = 0
        analyzer._rss_source_total = analyzer._rss_source_failed = 0
        schedule = Mock(push=True, period_key="midday", period_name="盘中简报", once_push=True)
        standalone = {"platforms": [{"id": "macro-quotes", "items": [{"title": "指数行情"}]}]}
        dispatch = analyzer.ctx.create_notification_dispatcher.return_value.dispatch_all
        dispatch.side_effect = [{"dingtalk": False}, {"dingtalk": True}]
        with patch("trendradar.__main__.BriefHistory") as history:
            analyzer._send_notification_if_needed([], "盘中简报", "daily", schedule=schedule, standalone_data=standalone)
            history.return_value.record.assert_not_called()
            analyzer._send_notification_if_needed([], "盘中简报", "daily", schedule=schedule, standalone_data=standalone)
            history.return_value.record.assert_called_once_with([], None, standalone)


class FinanceSourceTests(unittest.TestCase):
    def test_eia_blank_holiday_is_not_a_zero_or_shifted_day(self):
        body = """<table><tr><td>2026 Nov- 2 to Nov- 6</td>
                  <td>100.06</td><td></td><td>99.64</td><td></td><td></td></tr></table>"""
        with patch("trendradar.finance_sources.get_text", return_value=body):
            rows = oil_prices("https://example.com", TODAY)
        self.assertEqual(rows, [(date(2026, 11, 2), Decimal("100.06")), (date(2026, 11, 4), Decimal("99.64"))])
        item = quote_item("WTI", "https://example.com", TODAY, rows)
        self.assertIn("2026-11-04", item["title"])
        self.assertIn("-0.42 美元/桶", item["title"])

    def test_yield_changes_use_basis_points(self):
        item = quote_item("中债", "https://example.com", TODAY, [
            (TODAY, Decimal("1.5965")), (TODAY - timedelta(days=1), Decimal("1.5874"))
        ], yield_rate=True)
        self.assertIn("1.5965%", item["title"])
        self.assertIn("+0.9 基点", item["title"])

    def test_cpi_result_prefers_complete_title_and_uses_prior_release(self):
        slots = "<td>……</td>" * 10 + "<td>9/一</td><td>9/三</td>"
        schedule = f"2026年国家统计局主要统计信息发布日程表<table><tr><td>5</td><td>居民消费价格指数月度报告</td>{slots}</tr></table>"
        latest = """<a href="./202611/t20261109_10.html">2026年10月份居民消费价格同比上涨0.7%</a>
                    <a href="./202611/t20261109_10.html">2026年10月份居民消费价格同比上...</a>"""
        previous = '<a href="./202610/t20261014_9.html">2026年9月份居民消费价格同比上涨0.9%</a>'
        with patch("trendradar.economic_calendar.get_text", side_effect=[schedule, latest, previous]):
            upcoming, results = nbs_calendar(TODAY, "morning")
        self.assertEqual(upcoming, [])
        self.assertIn("实际 0.7%，前值 0.9%", results[0]["title"])

    def test_fomc_fractions_and_day_rollover(self):
        with patch("trendradar.economic_calendar.get_text", return_value=(
            "The Committee decided to raise the target range for the federal funds rate "
            "by 1/4 percentage point to 3-3/4 to 4 percent."
        )):
            self.assertEqual(_fed_range("https://example.com"), "3.75–4%")
        self.assertEqual(_fed_release_time(date(2026, 12, 9)).isoformat(), "2026-12-10T03:00:00+08:00")
