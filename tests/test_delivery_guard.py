import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from scripts.delivery_guard import (
    database_deliveries, decide, read_ledger, resolve, snapshot, verify_delivery,
)

ROOT = Path(__file__).resolve().parents[1]


class DeliveryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.ledger = self.root / "state.json"

    def record(self, period, action="push", day="2026-11-05"):
        folder = self.output / "news"
        folder.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(folder / f"{day}.db")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS period_executions (execution_date, period_key, action)")
            db.execute("INSERT INTO period_executions VALUES (?, ?, ?)", (day, period, action))
            db.commit()

    def test_all_seven_days_have_three_delivery_windows(self):
        for offset in range(7):
            day = date(2026, 11, 2) + timedelta(days=offset)
            for clock, period in [("07:59", None), ("08:00", "morning"),
                                  ("09:59", "morning"), ("10:00", None),
                                  ("11:00", "midday"), ("12:59", "midday"),
                                  ("13:00", None), ("17:00", "after_market"),
                                  ("18:59", "after_market"), ("19:00", None)]:
                with self.subTest(day=day, clock=clock):
                    current = resolve(ROOT, datetime.fromisoformat(f"{day}T{clock}:00"))
                    self.assertEqual(current.period_key, period)
                    should_run, _ = decide(current, str(day), set(), "workflow_dispatch")
                    self.assertEqual(should_run, period is not None)

    def test_duplicate_is_skipped_even_if_news_cache_is_missing(self):
        self.record("after_market")
        snapshot(self.output, self.ledger, date(2026, 11, 5))
        empty_output = self.root / "missing_cache"
        sent = read_ledger(self.ledger) | database_deliveries(empty_output)
        current = resolve(ROOT, datetime(2026, 11, 5, 18, 30))
        self.assertFalse(decide(current, "2026-11-05", sent, "workflow_dispatch")[0])

    def test_analysis_alone_does_not_count_as_delivery(self):
        self.record("midday", "analyze")
        with self.assertRaises(RuntimeError):
            verify_delivery(self.output, {"date": "2026-11-05", "period": "midday"})

    def test_failed_delivery_can_retry_and_success_is_verified(self):
        current = resolve(ROOT, datetime(2026, 11, 5, 8, 10))
        self.assertTrue(decide(current, "2026-11-05", set(), "workflow_dispatch")[0])
        self.record("morning")
        verify_delivery(self.output, {"date": "2026-11-05", "period": "morning"})
        self.assertFalse(decide(current, "2026-11-05", database_deliveries(self.output), "schedule")[0])

    def test_yesterday_does_not_suppress_todays_report(self):
        current = resolve(ROOT, datetime(2026, 11, 6, 11, 0))
        self.assertTrue(decide(current, "2026-11-06", {("2026-11-05", "midday")}, "workflow_dispatch")[0])

    def test_check_only_never_runs_even_with_preview_set(self):
        current = resolve(ROOT, datetime(2026, 11, 5, 17, 0))
        self.assertFalse(decide(current, "2026-11-05", set(), "workflow_dispatch", True, True)[0])

    def test_hourly_collection_still_runs_outside_delivery_window(self):
        current = resolve(ROOT, datetime(2026, 11, 5, 14, 17))
        self.assertTrue(decide(current, "2026-11-05", set(), "schedule")[0])
        self.assertFalse(decide(current, "2026-11-05", set(), "workflow_dispatch")[0])

    def test_invalid_ledger_is_not_treated_as_empty(self):
        self.ledger.write_text('{"version": 999, "sent": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            read_ledger(self.ledger)

    def test_snapshot_retains_existing_markers_when_cache_is_empty(self):
        self.ledger.write_text(json.dumps({"version": 1, "sent": [
            {"date": "2026-11-05", "period": "morning"},
            {"date": "2026-09-01", "period": "midday"},
        ]}), encoding="utf-8")
        snapshot(self.output, self.ledger, date(2026, 11, 5))
        self.assertEqual(read_ledger(self.ledger), {("2026-11-05", "morning")})


if __name__ == "__main__":
    unittest.main()
