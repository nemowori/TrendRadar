"""Data panels and delivery-aware event filtering for the A-share brief."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
from unicodedata import normalize
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .economic_calendar import FOMC_SCHEDULE, NBS_SCHEDULE, fed_calendar, nbs_calendar
from .finance_sources import (
    BRENT, CN_BOND, US_BOND_PAGE, WTI,
    china_yields, oil_prices, quote_item, us_yields, watchlist_announcements,
)


BRIEF_PERIODS = {"morning", "midday", "after_market"}
_OFFICIAL_SECTIONS = {"economic-results", "watchlist-announcements"}
_SOURCE_SUFFIX = re.compile(r"\s+-\s+(?:Reuters|CNBC|BBC(?: News)?|Financial Times|央视网|中国新闻网|中新网)$", re.I)
_PROGRESS = (
    re.compile(r"正式|官方|确认|公告|confirmed|official", re.I),
    re.compile(r"公布|发布|生效|落地|released|takes effect", re.I),
    re.compile(r"上调|上修|加息|raises?|hikes?", re.I),
    re.compile(r"下调|下修|降息|lowers?|cuts?", re.I),
    re.compile(r"暂停|中止|取消|撤回|suspend|cancel|withdraw", re.I),
    re.compile(r"恢复|重启|resum|restart", re.I),
)


def _event_key(title, url):
    text = _SOURCE_SUFFIX.sub("", normalize("NFKC", title)).casefold()
    # Retain values and status words separately so substantial updates can be sent.
    numbers = tuple(sorted(re.findall(r"[-+]?\d+(?:[.,]\d+)*%?", text)))
    progress = tuple(index for index, marker in enumerate(_PROGRESS) if marker.search(text))
    clean = re.sub(r"[\W_]+", "", text)
    if url:
        try:
            parts = urlsplit(url)
            params = [
                (key, value) for key, value in parse_qsl(parts.query)
                if not key.startswith("utm_") and key not in {"fbclid", "gclid"}
            ]
            url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(sorted(params)), ""))
        except (TypeError, ValueError):
            url = ""
    return clean, url, numbers, progress


def _duplicates(first, second):
    title, url, numbers, progress = first
    old_title, old_url, old_numbers, old_progress = second
    if numbers != old_numbers or set(progress) - set(old_progress):
        return False
    if title == old_title or (url and url == old_url):
        return True
    # Cheap checks before edit similarity. Keep short or ambiguous headlines.
    if min(len(title), len(old_title)) < 12:
        return False
    if min(len(title), len(old_title)) / max(len(title), len(old_title)) < 0.84:
        return False
    if len(set(title) & set(old_title)) / max(len(set(title)), len(set(old_title))) < 0.72:
        return False
    return SequenceMatcher(None, title, old_title, autojunk=False).ratio() >= 0.86


class BriefHistory:
    """Suppress previously delivered articles and near-identical headlines."""

    def __init__(self, today, path=None):
        self.today = today
        self.path = path or (
            Path(".delivery-state/brief-events.json") if os.environ.get("GITHUB_ACTIONS") == "true"
            else Path("output/brief-events.json")
        )
        self.rows = self._read()
        self.news_keys = [_event_key(row["title"], row["url"]) for row in self.rows if row["section"] == "news"]
        self.official_keys = {
            (row["section"], row["url"], row["title"]) for row in self.rows if row["section"] in _OFFICIAL_SECTIONS
        }

    def _read(self):
        if not self.path.exists():
            return []
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            cutoff = self.today - timedelta(days=3)
            return [
                row for row in rows
                if cutoff <= date.fromisoformat(row["date"]) <= self.today
                and isinstance(row["title"], str) and isinstance(row["url"], str) and isinstance(row["section"], str)
            ][-600:]
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"[分时简报] 无法读取已发事件：{type(error).__name__}")
            return []

    def keep_news(self, title, url):
        key = _event_key(title, url)
        if any(_duplicates(key, existing) for existing in self.news_keys):
            return False
        self.news_keys.append(key)
        return True

    def keep_official(self, section, items):
        unique = {}
        for item in items:
            key = (section, item["url"], item["title"])
            if key not in self.official_keys:
                unique[key] = item
        return list(unique.values())

    def record(self, stats, rss_stats, standalone):
        def row(section, item):
            return {"date": self.today.isoformat(), "section": section, "title": item["title"], "url": item.get("url") or ""}

        added = [
            row("news", item)
            for group in [*(stats or []), *(rss_stats or [])] for item in group.get("titles", [])
            if item.get("title")
        ]
        added.extend(
            row(platform["id"], item)
            for platform in (standalone or {}).get("platforms", []) if platform["id"] in _OFFICIAL_SECTIONS
            for item in platform.get("items", []) if item.get("url") and item.get("title")
        )
        if not added:
            return
        unique = {(item["section"], item["url"], item["title"]): item for item in self.rows + added}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(list(unique.values())[-600:], ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)


def finance_sections(now, period, watchlist, history):
    today = now.date()
    quotes = (
        ("中国10年期国债收益率", CN_BOND, china_yields, True),
        ("美国10年期国债收益率", US_BOND_PAGE, us_yields, True),
        ("布伦特原油现货", BRENT, lambda day: oil_prices(BRENT, day), False),
        ("WTI 原油现货", WTI, lambda day: oil_prices(WTI, day), False),
    )
    with ThreadPoolExecutor(max_workers=7) as executor:
        quote_jobs = [executor.submit(fetch, today) for _, _, fetch, _ in quotes]
        nbs_job = executor.submit(nbs_calendar, today, period)
        fed_job = executor.submit(fed_calendar, now, period)
        watchlist_job = executor.submit(watchlist_announcements, watchlist, today) if watchlist else None
        macro = []
        for (name, url, _, is_yield), future in zip(quotes, quote_jobs):
            try:
                macro.append(quote_item(name, url, today, future.result(), yield_rate=is_yield))
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"[宏观行情] {name}查询失败：{type(error).__name__}")
                macro.append({"title": f"{name} 日度数据暂缺", "url": url})
        upcoming, results = [], []
        for name, url, future in (("国家统计局", NBS_SCHEDULE, nbs_job), ("美联储", FOMC_SCHEDULE, fed_job)):
            try:
                scheduled, published = future.result()
                upcoming.extend(scheduled)
                results.extend(published)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"[经济日历] {name}查询失败：{type(error).__name__}")
                upcoming.append({"title": f"{name}日历暂缺", "url": url})

        sections = [{"id": "macro-quotes", "name": "宏观行情（日度）", "items": macro}]
        if not upcoming:
            window = {"morning": "未来7天", "after_market": "今天至明天"}.get(period, "今天")
            upcoming = [{"title": f"{window}暂无待发布的 CPI、PPI、制造业 PMI 或美联储利率决议", "url": ""}]
        sections.append({"id": "economic-calendar", "name": "经济日历", "items": sorted(upcoming, key=lambda item: item["title"])[:6]})
        results = history.keep_official("economic-results", results)
        if results:
            sections.append({"id": "economic-results", "name": "经济数据与利率决议", "items": results})
        if watchlist_job:
            try:
                announcements = history.keep_official("watchlist-announcements", watchlist_job.result())[:8]
                if not announcements:
                    announcements = [{"title": "本时段暂无新增重点公告", "url": ""}]
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"[自选公告] 查询失败：{type(error).__name__}")
                announcements = [{"title": "巨潮资讯公告查询暂时不可用", "url": ""}]
            sections.append({"id": "watchlist-announcements", "name": "自选公告", "items": announcements})
    return sections
