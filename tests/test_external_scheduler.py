import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from scripts import setup_external_scheduler as scheduler
from scripts.delivery_guard import resolve


class ExternalSchedulerTests(unittest.TestCase):
    def test_external_schedule_covers_three_targets_and_retries_only_inside_windows(self):
        news, check_in = scheduler.job_specs("nemowori/TrendRadar", "fake-token")
        schedule = news["schedule"]
        self.assertEqual(schedule["timezone"], "Asia/Shanghai")
        self.assertTrue({8, 11, 17}.issubset(schedule["hours"]))
        self.assertIn(0, schedule["minutes"])
        for hour in schedule["hours"]:
            for minute in schedule["minutes"]:
                result = resolve(Path(__file__).resolve().parents[1], datetime(2026, 11, 5, hour, minute))
                self.assertTrue(result.push)
        self.assertEqual(check_in["schedule"]["hours"], [7])
        body = json.loads(news["extendedData"]["body"])
        self.assertIs(body["inputs"]["send_now"], False)
        self.assertIs(body["inputs"]["check_only"], False)

    def test_repeated_setup_updates_two_jobs_without_duplicates_or_secret_logs(self):
        jobs = {}
        operations = []

        def api(url, token, method="GET", body=None):
            if url.startswith("https://api.github.com/"):
                self.assertEqual(body["inputs"], {"send_now": False, "check_only": True})
                return {}
            if url.endswith("/jobs") and method == "GET":
                return {"jobs": [{"jobId": key, **value} for key, value in jobs.items()], "someFailed": False}
            if method == "PUT":
                key = len(jobs) + 1
                jobs[key] = copy.deepcopy(body["job"])
                operations.append("create")
                return {"jobId": key}
            key = int(url.rsplit("/", 1)[1])
            if method == "PATCH":
                jobs[key] = copy.deepcopy(body["job"])
                operations.append("update")
                return {}
            return {"jobDetails": jobs[key]}

        captured = io.StringIO()
        with patch.dict("os.environ", {"CRON_JOB_API_KEY": "test-cron-secret",
                                      "SCHEDULER_GITHUB_TOKEN": "test-github-secret",
                                      "GITHUB_REPOSITORY": "nemowori/TrendRadar"}), \
             patch("sys.argv", ["setup_external_scheduler.py"]), \
             patch.object(scheduler, "request_json", side_effect=api), \
             patch.object(scheduler.time, "sleep"), redirect_stdout(captured):
            scheduler.main()
            scheduler.main()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(operations, ["create", "create", "update", "update"])
        self.assertNotIn("test-cron-secret", captured.getvalue())
        self.assertNotIn("test-github-secret", captured.getvalue())

    def test_incomplete_remote_job_list_does_not_create_jobs(self):
        with patch.dict("os.environ", {"CRON_JOB_API_KEY": "test-cron-secret",
                                      "SCHEDULER_GITHUB_TOKEN": "test-github-secret",
                                      "GITHUB_REPOSITORY": "nemowori/TrendRadar"}), \
             patch("sys.argv", ["setup_external_scheduler.py"]), \
             patch.object(scheduler, "request_json", side_effect=[{}, {"someFailed": True}]) as api:
            with self.assertRaises(RuntimeError):
                scheduler.main()
            self.assertEqual(api.call_count, 2)


if __name__ == "__main__":
    unittest.main()
