"""Public source data used by the scheduled A-share brief."""

import csv
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
from io import StringIO
from urllib.parse import urljoin

import requests


CN_BOND = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/more?locale=cn_zh"
CN_BOND_HISTORY = "https://yield.chinabond.com.cn/cbweb-pbc-web/pbc/queryGjqxInfo"
US_BOND = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates"
US_BOND_PAGE = "https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics"
BRENT = "https://www.eia.gov/dnav/pet/hist/RBRTED.htm"
WTI = "https://www.eia.gov/dnav/pet/hist/RWTCD.htm"
CNINFO = "https://www.cninfo.com.cn"
_HEADERS = {"User-Agent": "TrendRadar/2.0 (public data reader)"}
_ANNOUNCEMENT = re.compile(
    r"年度报告|半年度报告|季度报告|业绩预告|业绩快报|业绩修正|重大合同|重大订单|中标|"
    r"日常经营合同|问询函|关注函|监管函|警示函|立案|行政处罚|利润分配|现金分红|股份回购|回购公司股份"
)


def get_text(url, params=None):
    response = requests.get(url, params=params, headers=_HEADERS, timeout=8)
    response.raise_for_status()
    return response.content.decode("utf-8-sig")


def plain_text(html):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", html)).split())


def table_rows(html):
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        yield [plain_text(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.I | re.S)]


def decimal_value(text):
    try:
        value = Decimal(text)
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError):
        return None


def _china_yield(html):
    rows = list(table_rows(html))
    for headers, values in zip(rows, rows[1:]):
        if not headers or not values or "10年" not in headers or values[0] != "中债国债收益率曲线":
            continue
        if len(values) != len(headers):
            continue
        value = decimal_value(values[headers.index("10年")])
        if value is not None:
            return date.fromisoformat(headers[0].split("(")[0].strip()), value
    raise ValueError("中债未返回10年期收益率")


def china_yields(today):
    latest = _china_yield(get_text(CN_BOND))
    observations = [latest] if latest[0] <= today else []
    # The calendar query returns a dated table. Weekends and holidays can have no observation.
    day = min(latest[0] - timedelta(days=1), today)
    for _ in range(10):
        if day.weekday() < 5:
            try:
                previous = _china_yield(get_text(CN_BOND_HISTORY, {"workTime": day.isoformat(), "locale": "cn_ZH"}))
            except OSError:
                if observations:
                    break
                raise
            except ValueError:
                previous = None
            if previous and previous[0] == day:
                observations.append(previous)
                if len(observations) == 2:
                    break
        day -= timedelta(days=1)
    return observations


def us_yields(today):
    observations = []
    for year in (today.year, today.year - 1):
        url = f"{US_BOND}/daily-treasury-rates.csv/{year}/all"
        body = get_text(url, {
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": year,
            "_format": "csv",
        })
        for row in csv.DictReader(StringIO(body)):
            value = decimal_value(row.get("10 Yr"))
            if value is not None and row.get("Date"):
                day = datetime.strptime(row["Date"], "%m/%d/%Y").date()
                if day <= today:
                    observations.append((day, value))
        if len(observations) >= 2:
            break
    return observations


def oil_prices(url, today):
    observations = []
    for row in table_rows(get_text(url)):
        if len(row) != 6:
            continue
        week = re.match(r"^(\d{4} [A-Za-z]{3}-\s*\d{1,2}) to ", row[0])
        if not week:
            continue
        start = datetime.strptime(week[1], "%Y %b-%d").date()
        for offset, text in enumerate(row[1:]):
            day = start + timedelta(days=offset)
            value = decimal_value(text)
            if value is not None and day <= today:
                observations.append((day, value))
    return observations


def quote_item(name, url, today, observations, *, yield_rate=False):
    rows = sorted({day: value for day, value in observations if day <= today}.items(), reverse=True)
    if not rows:
        raise ValueError("没有带日期的报价")
    day, value = rows[0]
    if yield_rate:
        label = f"{value:.4f}".rstrip("0").rstrip(".") + "%"
        delta = f"，较 {rows[1][0]} {(value - rows[1][1]) * 100:+.1f} 基点" if len(rows) > 1 else ""
    else:
        label = f"{value:.2f} 美元/桶"
        delta = f"，较 {rows[1][0]} {value - rows[1][1]:+.2f} 美元/桶" if len(rows) > 1 else ""
    return {"title": f"{name} {label}（{day}，日度{delta}）", "url": url}


def watchlist_announcements(codes, today):
    catalog_url = f"{CNINFO}/new/data/szse_stock.json"
    catalog = requests.get(catalog_url, headers=_HEADERS, timeout=8)
    catalog.raise_for_status()
    securities = {
        row["code"]: row for row in catalog.json()["stockList"]
        if row.get("code") in codes and row.get("category") == "A股"
    }
    if securities.keys() != set(codes):
        raise ValueError("巨潮资讯未识别全部自选股代码")
    start = today - timedelta(days=3)
    items = []
    for column, prefix in (("sse", "6"), ("szse", ("0", "3"))):
        members = {code: row for code, row in securities.items() if code.startswith(prefix)}
        if not members:
            continue
        response = requests.post(
            f"{CNINFO}/new/hisAnnouncement/query",
            headers={**_HEADERS, "Referer": f"{CNINFO}/new/index"},
            data={
                "stock": ";".join(f"{code},{row['orgId']}" for code, row in members.items()),
                "pageNum": 1, "pageSize": 50, "column": column, "tabName": "fulltext",
                "seDate": f"{start}~{today}", "isHLtitle": "false",
            },
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        if "announcements" not in payload:
            raise ValueError("巨潮资讯未返回公告列表")
        for row in payload["announcements"] or []:
            code = row.get("secCode")
            title = plain_text(row.get("announcementTitle") or "")
            attachment = re.fullmatch(
                r"finalpage/(\d{4}-\d{2}-\d{2})/\d+\.pdf", row.get("adjunctUrl") or "", re.I
            )
            if code not in members or not attachment or not _ANNOUNCEMENT.search(title):
                continue
            day = date.fromisoformat(attachment[1])
            if not start <= day <= today:
                continue
            name = members[code]["zwjc"]
            items.append((day, {
                "title": f"{name}（{code}） {title}（披露日 {day}）",
                "url": urljoin("https://static.cninfo.com.cn/", attachment[0]),
            }))
    return [item for _, item in sorted(items, key=lambda row: row[0], reverse=True)]
