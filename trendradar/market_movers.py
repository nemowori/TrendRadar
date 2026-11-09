"""Timestamped A-share indices, turnover, rankings and issuer announcements.

Rankings are a quote-provider snapshot, not an exchange abnormal-trading finding.
Failure is explicit; an older cached ranking is never substituted as current.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
import json
import math
import os
from pathlib import Path
import re

import requests

CHINA = timezone(timedelta(hours=8))
SNAPSHOT_PATH = Path("output/market/latest.json")
RANK_URL = "https://push2.eastmoney.com/api/qt/clist/get"
INDEX_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
NOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
MARKET_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
TOP_N = 10
REASON_COUNT = 5
INDEX_NAMES = {
    "1.000001": "上证指数",
    "0.399001": "深证成指",
    "0.399006": "创业板指",
    "1.000688": "科创50",
    "1.000300": "沪深300",
}
# Use the A-share series for the two-market total, without counting B shares.
A_SHARE_MARKETS = ("1.000002", "0.399107")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _clean(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", str(text or "")))).strip()


def _json_get(url, params):
    response = requests.get(
        url, params=params, timeout=10,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    response.raise_for_status()
    # The endpoint is UTF-8; requests' generic text/html fallback can choose Latin-1.
    return json.loads(response.content.decode("utf-8-sig"))


def fetch_market_indices(now=None):
    """Return the latest available session's index quotes and Shanghai/Shenzhen turnover."""
    now = now or datetime.now(CHINA)
    secids = (*INDEX_NAMES, *A_SHARE_MARKETS)
    payload = _json_get(INDEX_URL, {
        "secids": ",".join(secids), "fields": "f2,f3,f5,f6,f12,f13,f124", "fltt": 2, "invt": 2,
    })
    if payload.get("rc") != 0 or not payload.get("data"):
        raise ValueError("指数行情接口未返回有效数据")

    rows = payload["data"].get("diff") or []
    if isinstance(rows, dict):
        rows = rows.values()
    quotes = {}
    for row in rows:
        secid = f"{row.get('f13')}.{row.get('f12')}"
        stamp = _number(row.get("f124"))
        if secid not in secids or stamp is None or not 1262275200 <= stamp <= now.timestamp() + 300:
            continue
        quoted_at = datetime.fromtimestamp(stamp, CHINA)
        if secid not in quotes or quoted_at > quotes[secid][0]:
            quotes[secid] = (quoted_at, row)
    if not quotes:
        raise ValueError("指数行情缺少有效时间")

    trading_day = max(quoted_at.date() for quoted_at, _ in quotes.values())
    items = []
    for secid, name in INDEX_NAMES.items():
        if secid not in quotes:
            continue
        quoted_at, row = quotes[secid]
        price, change = _number(row.get("f2")), _number(row.get("f3"))
        if quoted_at.date() != trading_day or price is None or price <= 0 or change is None:
            continue
        title = f"A股行情｜{name} {price:.2f}点 {change:+.2f}%（{quoted_at:%Y-%m-%d %H:%M} 北京时间）"
        items.append({"title": title, "url": f"https://quote.eastmoney.com/zs{secid.split('.')[1]}.html"})

    turnover = []
    for secid in A_SHARE_MARKETS:
        if secid not in quotes:
            continue
        quoted_at, row = quotes[secid]
        volume, amount = _number(row.get("f5")), _number(row.get("f6"))
        if quoted_at.date() == trading_day and volume is not None and amount is not None and min(volume, amount) >= 0:
            turnover.append((quoted_at, volume, amount))
    if len(turnover) == len(A_SHARE_MARKETS):
        # f5 is in lots (手), f6 is in yuan. Show differing quote minutes as a range.
        first_time, last_time = sorted(quoted_at.strftime("%H:%M") for quoted_at, _, _ in turnover)
        quote_time = first_time if first_time == last_time else f"{first_time}–{last_time}"
        total_volume = sum(volume for _, volume, _ in turnover) / 100_000_000
        total_amount = sum(amount for _, _, amount in turnover) / 100_000_000
        title = (
            f"A股行情｜沪深A股成交量 {total_volume:.2f}亿手，成交额 {total_amount:.2f}亿元"
            f"（{trading_day} {quote_time} 北京时间）"
        )
        items.insert(0, {"title": title, "url": "https://quote.eastmoney.com/center/gridlist.html#hszs"})

    if not items:
        raise ValueError("指数点位与沪深成交数据暂缺")
    print(f"[A股行情] 行情日期{trading_day}，获取{len(items)}条成交和指数行情")
    return {"status": "success", "items": items}


def parse_ranking(payload, direction, now):
    """Validate units, sort direction, quote dates and non-trading placeholders."""
    rows = (payload.get("data") or {}).get("diff") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    parsed = []
    seen = set()
    for row in rows:
        code = str(row.get("f12", ""))
        name = _clean(row.get("f14"))
        price, change, stamp = (_number(row.get(k)) for k in ("f2", "f3", "f124"))
        if not re.fullmatch(r"[034689]\d{5}", code) or code in seen:
            continue
        if not name or "\ufffd" in name or price is None or price <= 0 or change is None:
            continue
        if stamp is None or stamp < 1262275200 or stamp > now.timestamp() + 300:
            continue
        if (direction == "gain" and change <= 0) or (direction == "loss" and change >= 0):
            continue
        quoted = datetime.fromtimestamp(stamp, CHINA)
        exchange = "sh" if str(row.get("f13")) == "1" else ("bj" if code.startswith(("4", "8", "92")) else "sz")
        parsed.append({
            "code": code, "name": name, "price": price, "change_pct": change,
            "amount_yuan": _number(row.get("f6")), "turnover_pct": _number(row.get("f8")),
            "quoted_at": quoted.isoformat(), "direction": direction,
            "url": f"https://quote.eastmoney.com/{exchange}{code}.html",
        })
        seen.add(code)
    parsed.sort(key=lambda item: item["change_pct"], reverse=direction == "gain")
    return parsed


def _ranking(direction, now):
    payload = _json_get(RANK_URL, {
        "pn": 1, "pz": 30, "po": 1 if direction == "gain" else 0,
        "np": 1, "fltt": 2, "invt": 2, "fid": "f3", "fs": MARKET_FILTER,
        "fields": "f2,f3,f6,f8,f12,f13,f14,f18,f124",
    })
    if payload.get("rc") != 0 or not payload.get("data"):
        raise ValueError("行情接口未返回有效数据")
    rows = parse_ranking(payload, direction, now)
    if not rows:
        raise ValueError(f"{direction}榜未返回带有效行情时间的记录")
    return rows


def _notice_time(row):
    # notice_date can be the next day's document date; prefer actual publication time.
    raw = row.get("display_time") or row.get("eiTime")
    if raw:
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA), True
        except ValueError:
            pass
    raw = row.get("notice_date", "")
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=CHINA), False
    except ValueError:
        return None, False


def _evidence(quote, now):
    code = quote["code"]
    quoted = datetime.fromisoformat(quote["quoted_at"])
    result = {"code": code, "name": quote["name"], "announcements": [], "error": ""}
    try:
        payload = _json_get(NOTICE_URL, {
            "sr": -1, "page_size": 10, "page_index": 1, "ann_type": "A",
            "client_source": "web", "stock_list": code, "f_node": 0, "s_node": 0,
        })
        candidates = []
        for row in (payload.get("data") or {}).get("list", []):
            if not any(str(c.get("stock_code")) == code for c in row.get("codes", [])):
                continue
            published, exact = _notice_time(row)
            art_code = str(row.get("art_code", ""))
            if not published or not re.fullmatch(r"AN\d{18}", art_code):
                continue
            if published > now + timedelta(minutes=5) or now - published > timedelta(days=7):
                continue
            timing = "行情之前发布" if published < quoted else "行情之后发布，不能解释此前下跌"
            if not exact:
                timing = "仅有公告落款日期，发布时间不确定，不能认定为下跌前证据"
            candidates.append({
                "title": _clean(row.get("title")), "published_at": published.isoformat(),
                "exact_publication_time": exact, "timing": timing,
                "url": f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html",
                "art_code": art_code,
            })
        # Preserve one recent disclosure and prefer pre-quote risk/earnings evidence.
        candidates.sort(key=lambda item: item["published_at"], reverse=True)
        relevant = [item for item in candidates if item["timing"] == "行情之前发布" and re.search(r"立案|处罚|风险|业绩|亏损|减持|终止|违约|退市|无法|更正", item["title"])]
        selected = candidates[:1]
        for item in relevant + candidates:
            if item not in selected:
                selected.append(item)
            if len(selected) >= 2:
                break
        for item in selected:
            item["excerpt"] = ""
            try:
                detail = (_json_get(CONTENT_URL, {"art_code": item["art_code"], "client_source": "web", "page_index": 1}).get("data") or {})
                securities = detail.get("security", [])
                if detail.get("art_code") == item["art_code"] and any(str(s.get("stock")) == code for s in securities):
                    body = _clean(detail.get("notice_content"))
                    item["excerpt"] = body[:3000]
                    item["excerpt_truncated"] = len(body) > 3000
            except Exception:
                item["excerpt_error"] = "正文未取得，只有标题，不可推断未提供的细节"
            result["announcements"].append(item)
    except Exception as error:
        result["error"] = f"公告查询失败：{type(error).__name__}"
    return result


def _save_snapshot(data, path=None):
    path = path or SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def fetch_market_movers(now=None):
    now = now or datetime.now(CHINA)
    snapshot = {"status": "unavailable", "fetched_at": now.isoformat(), "run_id": os.environ.get("GITHUB_RUN_ID", "local"), "source": "东方财富公开行情及公司公告转录", "quotes": [], "evidence": []}
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            gains, losses = list(executor.map(lambda side: _ranking(side, now), ["gain", "loss"]))
        trading_day = max(item["quoted_at"][:10] for item in gains + losses)
        gains = [item for item in gains if item["quoted_at"].startswith(trading_day)][:TOP_N]
        losses = [item for item in losses if item["quoted_at"].startswith(trading_day)][:TOP_N]
        snapshot.update(status="success", trading_day=trading_day, quotes=gains + losses)
        with ThreadPoolExecutor(max_workers=3) as executor:
            snapshot["evidence"] = list(executor.map(lambda quote: _evidence(quote, now), losses[:REASON_COUNT]))
        _save_snapshot(snapshot)
        items = []
        for direction, quotes in (("涨幅", gains), ("跌幅", losses)):
            for rank, quote in enumerate(quotes, 1):
                amount = quote["amount_yuan"]
                amount_text = f"{amount / 100000000:.2f}亿元" if amount is not None else "未提供"
                time_text = quote["quoted_at"][:19].replace("T", " ")
                title = f"A股行情快照｜{direction}榜{rank} {quote['name']}({quote['code']}) {quote['change_pct']:+.2f}% 现价{quote['price']:.2f}元 成交额{amount_text} 行情时间{time_text}（北京时间）"
                items.append({"id": f"{trading_day}-{direction}-{quote['code']}", "title": title, "url": quote["url"]})
        print(f"[A股异动] 行情日期{trading_day}，涨幅榜{len(gains)}只、跌幅榜{len(losses)}只，公告追踪{len(snapshot['evidence'])}只")
        return {"status": "success", "items": items}
    except Exception as error:
        snapshot["error"] = f"行情暂不可用：{type(error).__name__}"
        _save_snapshot(snapshot)
        raise


def read_market_context(path=None, now=None):
    path = path or SNAPSHOT_PATH
    now = now or datetime.now(CHINA)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(snapshot["fetched_at"])
        if snapshot.get("status") != "success" or not timedelta(0) <= now - fetched <= timedelta(minutes=20):
            return "本次未取得可用的A股行情快照，不生成异动名单或臆测跌幅原因。"
        if os.environ.get("GITHUB_RUN_ID") and snapshot.get("run_id") != os.environ["GITHUB_RUN_ID"]:
            return "本次运行未取得新行情，不使用历史缓存冒充当前异动名单。"
        return json.dumps(snapshot, ensure_ascii=False)
    except (OSError, ValueError, KeyError, TypeError):
        return "本次没有可用的A股行情与公告证据，不臆测个股跌幅原因。"
