"""Scheduled releases and published values from NBS and the Federal Reserve."""

from calendar import month_name
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
import re
from urllib.parse import urljoin

import pytz

from .finance_sources import get_text, plain_text, table_rows


NBS_SCHEDULE = "https://www.stats.gov.cn/sj/fbrc/bnxxfb/"
NBS_RELEASES = "https://www.stats.gov.cn/sj/zxfb/"
FOMC_SCHEDULE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_NBS_SERIES = {
    "居民消费价格指数月度报告": "中国 CPI 同比",
    "工业生产者价格指数月度报告": "中国 PPI 同比",
    "采购经理指数月度报告": "中国制造业 PMI",
}
_NBS_TITLES = (
    ("中国 CPI 同比", re.compile(r"^\d{4}年\d{1,2}月份居民消费价格同比")),
    ("中国 PPI 同比", re.compile(r"^\d{4}年\d{1,2}月份工业生产者出厂价格同比")),
    ("中国制造业 PMI", re.compile(r"^\d{4}年\d{1,2}月中国采购经理指数运行情况")),
)
_NBS_LINK = re.compile(r"""<a\b[^>]*href=["']([^"']*?/t(\d{8})_\d+\.html)["'][^>]*>(.*?)</a>""", re.S)
_FED_LINK = re.compile(r"""href=["'](/newsevents/pressreleases/monetary(\d{8})a\.htm)["']""")
_MONTHS = {name: index for index, name in enumerate(month_name) if name}
_NY = pytz.timezone("America/New_York")
_CN = pytz.timezone("Asia/Shanghai")


def _valid_day(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def nbs_schedule(body):
    year = re.search(r"(20\d{2})年国家统计局主要统计信息发布日程表", body)
    if not year:
        raise ValueError("统计局日程缺少年份")
    events = []
    for row in table_rows(body):
        if len(row) < 14 or row[1] not in _NBS_SERIES:
            continue
        for month, cell in enumerate(row[2:14], 1):
            for number in re.findall(r"(\d{1,2})\s*/\s*[一二三四五六日天]", cell):
                day = _valid_day(int(year[1]), month, int(number))
                if day:
                    events.append((day, _NBS_SERIES[row[1]]))
    if not events:
        raise ValueError("统计局日程未返回已支持的指标")
    return sorted(events)


def _nbs_release_links(html):
    for path, stamp, raw_title in _NBS_LINK.findall(html):
        title = plain_text(raw_title)
        for name, pattern in _NBS_TITLES:
            if pattern.search(title):
                yield name, datetime.strptime(stamp, "%Y%m%d").date(), title, urljoin(NBS_RELEASES, path)
                break


def _nbs_value(name, title, url):
    if name == "中国制造业 PMI":
        body = plain_text(get_text(url))
        value = re.search(r"(?<!非)制造业采购经理指数\s*[（(]\s*PMI\s*[)）]\s*为\s*(\d+(?:\.\d+)?)\s*[%％]", body, re.I)
        if value:
            return Decimal(value[1])
    elif "同比持平" in title:
        return Decimal("0")
    else:
        value = re.search(r"同比(?:由[^，。]{0,18}转为)?(上涨|下降|下跌)\s*(\d+(?:\.\d+)?)\s*%", title)
        if value:
            return Decimal(value[2]) * (1 if value[1] == "上涨" else -1)
    raise ValueError(f"未识别{name}的官方数值")


def nbs_calendar(today, period):
    events = nbs_schedule(get_text(NBS_SCHEDULE))
    front = list(_nbs_release_links(get_text(NBS_RELEASES)))
    active = {name for name, day, _, _ in front if today - timedelta(days=1) <= day <= today}
    published = {(name, day) for name, day, _, _ in front if day <= today}
    results = []
    if active:
        rows = front[:]
        for page in (1, 2):
            if all(len({day for kind, day, _, _ in rows if kind == name and day <= today}) >= 2 for name in active):
                break
            try:
                rows.extend(_nbs_release_links(get_text(f"{NBS_RELEASES}index_{page}.html")))
            except (OSError, ValueError) as error:
                print(f"[经济日历] 统计局前值查询：{type(error).__name__}")
                break
        for name in sorted(active):
            # The list has several links to the same release, including a shortened mobile title.
            candidates = sorted(rows, key=lambda row: len(row[2]))
            releases = sorted({day: (title, url) for kind, day, title, url in candidates if kind == name and day <= today}.items(), reverse=True)
            day, (title, url) = releases[0]
            try:
                actual = f"{_nbs_value(name, title, url)}%"
            except (OSError, ValueError) as error:
                print(f"[经济日历] {name}结果：{type(error).__name__}")
                results.append({"title": f"{title}（发布日 {day}）", "url": url})
                continue
            previous = "待核实"
            if len(releases) > 1:
                _, (prior_title, prior_url) = releases[1]
                try:
                    previous = f"{_nbs_value(name, prior_title, prior_url)}%"
                except (OSError, ValueError) as error:
                    print(f"[经济日历] {name}前值：{type(error).__name__}")
            results.append({"title": f"{name} 实际 {actual}，前值 {previous}（发布日 {day}）", "url": url})

    horizon = today + timedelta(days=7 if period == "morning" else 1 if period == "after_market" else 0)
    upcoming = [
        {"title": f"{day}｜{name} 计划发布", "url": NBS_SCHEDULE}
        for day, name in events if today <= day <= horizon and (name, day) not in published
    ]
    return upcoming, results


def _fomc_meetings(html):
    panels = re.split(r"(20\d{2})\s+FOMC Meetings", html)
    meetings = set()
    for year, panel in zip(panels[1::2], panels[2::2]):
        dates = re.findall(
            r'class="[^"]*\bfomc-meeting__month\b[^"]*"[^>]*>(.*?)</div>'
            r'.*?class="[^"]*\bfomc-meeting__date\b[^"]*"[^>]*>(.*?)</div>',
            panel, flags=re.S,
        )
        for raw_month, raw_days in dates:
            month = _MONTHS.get(plain_text(raw_month).split("/")[-1].strip())
            days = re.findall(r"\d{1,2}", plain_text(raw_days))
            day = _valid_day(int(year), month, int(days[-1])) if month and days else None
            if day:
                meetings.add(day)
    if not meetings:
        raise ValueError("美联储会议日历未返回日期")
    return sorted(meetings)


def _fed_release_time(day):
    return _NY.localize(datetime.combine(day, time(14))).astimezone(_CN)


def _fed_range(url):
    body = plain_text(get_text(url))
    number = r"\d+(?:\.\d+|[–-]\d+/\d+)?"
    band = re.search(rf"target range for the federal funds rate.{{0,100}}?\b({number})\s+to\s+({number})\s+percent", body, re.I)
    if not band:
        raise ValueError("联储声明未识别到目标区间")
    values = []
    for raw in band.groups():
        whole, separator, fraction = raw.replace("–", "-").partition("-")
        value = Fraction(whole) + Fraction(fraction) if separator else Fraction(whole)
        values.append(format(float(value), "g"))
    return "–".join(values) + "%"


def fed_calendar(now, period):
    body = get_text(FOMC_SCHEDULE)
    meetings = _fomc_meetings(body)
    links = sorted({
        datetime.strptime(stamp, "%Y%m%d").date(): urljoin(FOMC_SCHEDULE, path)
        for path, stamp in _FED_LINK.findall(body)
    }.items(), reverse=True)
    today = now.date()
    future = [release for day in meetings if (release := _fed_release_time(day)) > now]
    horizon = today + timedelta(days=7 if period == "morning" else 1 if period == "after_market" else 0)
    upcoming = [
        {"title": f"{release:%Y-%m-%d %H:%M}（北京时间）｜美联储利率决议，预计发布", "url": FOMC_SCHEDULE}
        for release in future[:1] if release.date() <= horizon
    ]
    results = []
    published = [(day, url) for day, url in links if _fed_release_time(day) <= now]
    if published and (now - _fed_release_time(published[0][0])) <= timedelta(hours=36):
        day, url = published[0]
        try:
            current = _fed_range(url)
        except (OSError, ValueError) as error:
            print(f"[经济日历] 美联储利率结果：{type(error).__name__}")
            results.append({"title": f"美联储公布利率决议（{_fed_release_time(day):%Y-%m-%d} 北京时间）", "url": url})
        else:
            previous = "待核实"
            if len(published) > 1:
                try:
                    previous = _fed_range(published[1][1])
                except (OSError, ValueError) as error:
                    print(f"[经济日历] 美联储前值：{type(error).__name__}")
            results.append({
                "title": f"美联储利率目标区间 实际 {current}，前值 {previous}（{_fed_release_time(day):%Y-%m-%d} 北京时间）",
                "url": url,
            })
    return upcoming, results
