"""Verify scheduled delivery and retain a small ledger independently of cache.

This ledger contains dates/period names only, never news text or credentials.
GitHub concurrency serializes runs; the artifact is restored before this guard.
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# Load the existing pure scheduler without executing trendradar.__init__, which
# eagerly imports AI/network clients even when this is a no-message check.
_spec = importlib.util.spec_from_file_location(
    "_delivery_scheduler", Path(__file__).resolve().parents[1] / "trendradar/core/scheduler.py"
)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
Scheduler = _module.Scheduler


def read_ledger(path):
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("sent"), list):
        raise ValueError("Invalid delivery ledger; refusing to risk duplicate delivery")
    result = set()
    for row in data["sent"]:
        day, period = row["date"], row["period"]
        if date.fromisoformat(day).isoformat() != day or period not in {
            "morning", "midday", "after_market"
        }:
            raise ValueError("Invalid delivery ledger entry")
        result.add((day, period))
    return result


def database_deliveries(output):
    result = set()
    for path in (output / "news").glob("????-??-??.db"):
        # Read-only: a missing/corrupt database must never be silently created.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_executions'"
            ).fetchone()
            if exists:
                result.update(db.execute(
                    "SELECT execution_date, period_key FROM period_executions WHERE action='push'"
                ).fetchall())
    return result


def resolve(root, now):
    config = yaml.safe_load((root / "config/config.yaml").read_text(encoding="utf-8"))
    timeline = yaml.safe_load((root / "config/timeline.yaml").read_text(encoding="utf-8"))
    return Scheduler(config["schedule"], timeline, None, lambda: now).resolve()


def decide(schedule, day, sent, event, check_only=False, preview=False):
    if check_only:
        return False, "只检查调度和发送记录，不采集、不分析、不发消息"
    if preview:
        return True, "用户明确选择强制预览"
    if schedule.period_key and (day, schedule.period_key) in sent:
        return False, f"{day}/{schedule.period_key} 已发送，跳过重复触发"
    if event == "workflow_dispatch" and not (schedule.period_key and schedule.push):
        return False, "独立触发器当前不在推送窗口，等待下个时段"
    return True, "本时段尚未发送，开始执行" if schedule.push else "仅采集新闻"


def verify_delivery(output, expected):
    if expected.get("period") and (expected["date"], expected["period"]) not in database_deliveries(output):
        raise RuntimeError("本时段未记录钉钉发送成功；任务标记失败，等待自动重试")


def snapshot(output, ledger, today):
    sent = read_ledger(ledger) | database_deliveries(output)
    cutoff = today - timedelta(days=14)
    sent = {(day, period) for day, period in sent if cutoff <= date.fromisoformat(day) <= today}
    data = {"version": 1, "sent": [{"date": d, "period": p} for d, p in sorted(sent)]}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    temporary = ledger.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(ledger)
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "verify", "snapshot"])
    args = parser.parse_args()
    root = Path.cwd()
    output = root / "output"
    state = root / ".delivery-state"
    ledger = state / "state.json"
    expected_path = state / "expected.json"
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    if args.command == "prepare":
        sent = read_ledger(ledger) | database_deliveries(output)
        schedule = resolve(root, now)
        preview = os.environ.get("FORCE_PREVIEW", "false").lower() == "true"
        check_only = os.environ.get("CHECK_ONLY", "false").lower() == "true"
        run, reason = decide(schedule, now.date().isoformat(), sent,
                             os.environ.get("RUN_KIND", "schedule"), check_only, preview)
        expected = {"date": now.date().isoformat(), "period": (
            schedule.period_key if run and schedule.push and not preview else None
        )}
        state.mkdir(parents=True, exist_ok=True)
        expected_path.write_text(json.dumps(expected), encoding="utf-8")
        print(f"[发送保护] {reason}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"run={'true' if run else 'false'}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"北京时间 {now:%Y-%m-%d %H:%M:%S}：{reason}。\n")
    elif args.command == "verify":
        verify_delivery(output, json.loads(expected_path.read_text(encoding="utf-8")))
        print("[发送保护] 本次执行结果已核实")
    else:
        result = snapshot(output, ledger, now.date())
        print(f"[发送保护] 已保存 {len(result['sent'])} 条时段发送记录（不含新闻或密钥）")


if __name__ == "__main__":
    main()
