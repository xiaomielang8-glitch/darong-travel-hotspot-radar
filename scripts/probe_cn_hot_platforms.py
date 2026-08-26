"""Probe public Chinese platforms for travel-relevant hotspot signals.

Strict rule: no content older than 24 hours may become a formal candidate.
Realtime/trending boards without publish time are snapshot signals only; they need
24-hour rank/new-entry evidence before formal use.

V0.4 improvements:
- prefer Weibo's public JSON hot-search endpoint, with HTML fallback;
- add targeted Bilibili travel-video search instead of relying only on the all-site ranking;
- keep Toutiao/Baidu realtime boards as trend-signal sources;
- no login, anti-bot bypass, article-body scraping, or LLM call.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

CN_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
TRAVEL_TERMS = (
    "旅游", "旅行", "景区", "景点", "游客", "文旅", "酒店", "民宿", "门票", "机场", "航班",
    "高铁", "火车", "自驾", "露营", "避暑", "亲子", "海边", "打卡", "夜游", "博物馆",
    "演唱会", "台风", "暴雨", "闭园", "开放", "滞留", "出游", "游玩", "古镇", "乐园",
    "漂流", "徒步", "海岛", "索道", "游船", "度假", "研学", "签证",
)

# These are discovery queries, not automatic-candidate rules. A returned video still has to be <=24h.
BILIBILI_TRAVEL_QUERIES = (
    "旅游", "旅行", "景区", "自驾游", "博物馆", "乐园", "海边旅行", "露营旅行",
)


@dataclass
class Signal:
    rank: int | None
    title: str
    url: str
    hot: str = ""
    published_at: str | None = None
    direct_24h_eligible: bool = False
    travel_match: bool = False
    context: str = ""


@dataclass
class ProbeResult:
    key: str
    name: str
    url: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    signals: list[Signal] = field(default_factory=list)
    error: str | None = None
    rule: str = ""


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    return clean(BeautifulSoup(html.unescape(value or ""), "html.parser").get_text(" ", strip=True))


def travel_match(text: str) -> bool:
    return any(term in text for term in TRAVEL_TERMS)


def within_24h(ts: datetime | None, now: datetime) -> bool:
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - timedelta(hours=24) <= ts <= now + timedelta(minutes=10)


def dedupe(items: list[Signal], limit: int = 160) -> list[Signal]:
    seen: set[str] = set()
    out: list[Signal] = []
    for item in items:
        key = clean(item.title).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def probe_weibo(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    json_url = "https://weibo.com/ajax/side/hotSearch"
    html_url = "https://s.weibo.com/top/summary?cate=realtimehot"
    result = ProbeResult(
        key="weibo_realtime",
        name="微博实时热搜",
        url=json_url,
        status="failure",
        rule="实时榜单只作当前快照；正式入选需证明24小时内新上榜/明显升温，不能把旧事件因仍在榜上直接算新热点。",
    )

    json_error = ""
    try:
        resp = await client.get(
            json_url,
            headers={
                "Referer": "https://weibo.com/",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        payload = resp.json()
        rows = ((payload.get("data") or {}).get("realtime") or [])
        items: list[Signal] = []
        for idx, row in enumerate(rows, start=1):
            title = clean(str(row.get("word") or row.get("note") or ""))
            if not title:
                continue
            hot = str(row.get("num") or row.get("raw_hot") or row.get("raw_hot_score") or "")
            items.append(
                Signal(
                    rank=idx,
                    title=title,
                    url=f"https://s.weibo.com/weibo?q={quote(title)}",
                    hot=hot,
                    travel_match=travel_match(title),
                    context="weibo_json",
                )
            )
        result.signals = dedupe(items)
        if result.signals:
            result.status = "success"
            return result
        json_error = "JSON endpoint returned no hot-search rows"
    except Exception as exc:
        json_error = f"{type(exc).__name__}: {exc}"

    # Fallback to the public HTML page. It often returns 200 with an empty table on cloud runners,
    # but retaining the fallback gives us a clean diagnostic instead of silently failing.
    try:
        resp = await client.get(html_url, headers={"Referer": "https://s.weibo.com/"})
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[Signal] = []
        for tr in soup.select("table tbody tr"):
            a = tr.select_one("td.td-02 a")
            if not a:
                continue
            title = clean(a.get_text(" ", strip=True))
            if not title:
                continue
            rank_text = clean((tr.select_one("td.td-01") or tr).get_text(" ", strip=True))
            rank_match = re.search(r"\d+", rank_text)
            rank = int(rank_match.group()) if rank_match else None
            hot_node = tr.select_one("td.td-02 span")
            hot = clean(hot_node.get_text(" ", strip=True)) if hot_node else ""
            href = a.get("href", "")
            link = "https://s.weibo.com" + href if href.startswith("/") else href
            items.append(Signal(rank=rank, title=title, url=link, hot=hot, travel_match=travel_match(title), context="weibo_html"))
        result.signals = dedupe(items)
        result.status = "success" if result.signals else "empty"
        if not result.signals:
            result.error = f"JSON: {json_error}; HTML fallback returned no rows"
    except Exception as exc:
        result.error = f"JSON: {json_error}; HTML: {type(exc).__name__}: {exc}"
    return result


async def probe_bilibili_ranking(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
    result = ProbeResult(
        key="bilibili_ranking",
        name="B站全站热门榜",
        url=url,
        status="failure",
        rule="发布时间在24小时内且明确旅行相关的热门视频可进入预筛；旧视频即使当前上榜，也只能作为趋势快照。",
    )
    try:
        resp = await client.get(url, headers={"Referer": "https://www.bilibili.com/"})
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        payload = resp.json()
        rows = ((payload.get("data") or {}).get("list") or [])
        items: list[Signal] = []
        for idx, row in enumerate(rows, start=1):
            title = clean(str(row.get("title") or ""))
            if not title:
                continue
            bvid = str(row.get("bvid") or "")
            pub_ts = row.get("pubdate")
            dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc) if isinstance(pub_ts, (int, float)) else None
            stat = row.get("stat") or {}
            items.append(
                Signal(
                    rank=idx,
                    title=title,
                    url=f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/v/popular/rank/all",
                    hot=f"播放{stat.get('view', 0)}｜赞{stat.get('like', 0)}",
                    published_at=dt.isoformat() if dt else None,
                    direct_24h_eligible=within_24h(dt, now),
                    travel_match=travel_match(title),
                    context="all_site_ranking",
                )
            )
        result.signals = dedupe(items)
        result.status = "success" if result.signals else "empty"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def fetch_bilibili_search_query(client: httpx.AsyncClient, keyword: str, now: datetime) -> tuple[list[Signal], str | None, int | None]:
    url = "https://api.bilibili.com/x/web-interface/search/type"
    try:
        resp = await client.get(
            url,
            params={
                "search_type": "video",
                "keyword": keyword,
                "order": "pubdate",
                "page": 1,
            },
            headers={"Referer": "https://search.bilibili.com/"},
        )
        status = resp.status_code
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") != 0:
            return [], f"code={payload.get('code')} message={payload.get('message')}", status
        rows = ((payload.get("data") or {}).get("result") or [])
        out: list[Signal] = []
        for row in rows:
            title = strip_html(str(row.get("title") or ""))
            if not title:
                continue
            pub_ts = row.get("pubdate")
            dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc) if isinstance(pub_ts, (int, float)) else None
            if not within_24h(dt, now):
                continue
            bvid = str(row.get("bvid") or "")
            play = row.get("play") or 0
            favorites = row.get("favorites") or 0
            description = strip_html(str(row.get("description") or ""))
            tag = clean(str(row.get("tag") or ""))
            combined = " ".join((title, description, tag, keyword))
            out.append(
                Signal(
                    rank=None,
                    title=title,
                    url=f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://search.bilibili.com/all?keyword={quote(keyword)}",
                    hot=f"播放{play}｜收藏{favorites}",
                    published_at=dt.isoformat() if dt else None,
                    direct_24h_eligible=True,
                    travel_match=travel_match(combined),
                    context=f"搜索:{keyword}",
                )
            )
        return out, None, status
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}", None


async def probe_bilibili_travel_search(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://api.bilibili.com/x/web-interface/search/type"
    result = ProbeResult(
        key="bilibili_travel_search",
        name="B站旅行关键词24h搜索",
        url=url,
        status="failure",
        rule="仅保留发布时间≤24小时的旅行关键词搜索结果；本探针只验证发现能力，正式接入前还需按播放/互动和事件价值继续过滤。",
    )
    batches = await asyncio.gather(*(fetch_bilibili_search_query(client, keyword, now) for keyword in BILIBILI_TRAVEL_QUERIES))
    items: list[Signal] = []
    errors: list[str] = []
    statuses: list[int] = []
    for keyword, (signals, error, status) in zip(BILIBILI_TRAVEL_QUERIES, batches):
        items.extend(signals)
        if status is not None:
            statuses.append(status)
        if error:
            errors.append(f"{keyword}:{error}")
    result.http_status = statuses[0] if statuses else None
    result.signals = dedupe(items)
    result.status = "success" if result.signals else ("empty" if not errors else "failure")
    if errors:
        result.error = "; ".join(errors[:5])
    return result


async def probe_toutiao(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    result = ProbeResult(
        key="toutiao_hot",
        name="今日头条热榜",
        url=url,
        status="failure",
        rule="热榜只作当前快照；没有可靠发布时间的词条，必须通过24小时内新上榜/排名变化确认新鲜度。",
    )
    try:
        resp = await client.get(url, headers={"Referer": "https://www.toutiao.com/"})
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") or payload.get("Data") or []
        items: list[Signal] = []
        for idx, row in enumerate(rows, start=1):
            title = clean(str(row.get("Title") or row.get("title") or ""))
            if not title:
                continue
            link = str(row.get("Url") or row.get("url") or "")
            hot = str(row.get("HotValue") or row.get("hot_value") or "")
            if not link:
                link = f"https://www.toutiao.com/search/?keyword={quote(title)}"
            items.append(Signal(rank=idx, title=title, url=link, hot=hot, travel_match=travel_match(title), context="realtime_board"))
        result.signals = dedupe(items)
        result.status = "success" if result.signals else "empty"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def probe_baidu(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://top.baidu.com/board?tab=realtime"
    result = ProbeResult(
        key="baidu_realtime",
        name="百度热搜实时榜",
        url=url,
        status="failure",
        rule="实时榜只作当前快照；正式候选必须有24小时内的新上榜/升温证据，或另有24小时内可靠发布时间。",
    )
    try:
        resp = await client.get(url)
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[Signal] = []
        cards = soup.select("div.category-wrap_iQLoo")
        for idx, card in enumerate(cards, start=1):
            a = card.find("a", href=True)
            title_node = card.select_one("div.c-single-text-ellipsis")
            title = clean(title_node.get_text(" ", strip=True)) if title_node else clean(a.get_text(" ", strip=True) if a else "")
            if not title or not a:
                continue
            hot_node = card.select_one("div.hot-index_1Bl1a")
            hot = clean(hot_node.get_text(" ", strip=True)) if hot_node else ""
            items.append(Signal(rank=idx, title=title, url=a.get("href", ""), hot=hot, travel_match=travel_match(title), context="realtime_board"))
        if not items:
            for idx, a in enumerate(soup.select("a[href*='baidu.com/s?']")[:60], start=1):
                title = clean(a.get_text(" ", strip=True))
                if 4 <= len(title) <= 80:
                    items.append(Signal(rank=idx, title=title, url=a.get("href", ""), travel_match=travel_match(title), context="fallback_links"))
        result.signals = dedupe(items)
        result.status = "success" if result.signals else "empty"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def render(results: list[ProbeResult], now: datetime) -> str:
    lines = [
        "# V0.4 中国热点平台24小时发现探针",
        "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}",
        "",
        "硬规则：正式候选不得超过24小时。无可靠发布时间的实时榜单只能保存快照，必须用24小时内的新上榜/排名变化证明新鲜度。",
        "",
        "| 来源 | 状态 | HTTP | 总信号 | 旅行相关 | 可直接确认24h新内容 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.status} | {r.http_status or '-'} | {len(r.signals)} | "
            f"{sum(s.travel_match for s in r.signals)} | {sum(s.direct_24h_eligible and s.travel_match for s in r.signals)} |"
        )
    for r in results:
        lines.extend(["", f"## {r.name}", "", r.rule])
        if r.error:
            lines.extend(["", f"诊断：`{r.error}`"])
        matches = [s for s in r.signals if s.travel_match]
        show = matches[:20] if matches else r.signals[:10]
        lines.append("")
        if not show:
            lines.append("- 无可展示信号")
            continue
        for s in show:
            rank = f"#{s.rank} " if s.rank else ""
            hot = f"｜{s.hot}" if s.hot else ""
            published = f"｜{s.published_at}" if s.published_at else ""
            context = f"｜{s.context}" if s.context else ""
            eligible = "｜24h可直接预筛" if s.direct_24h_eligible and s.travel_match else "｜需24h趋势差分"
            lines.append(f"- {rank}[{s.title}]({s.url}){hot}{published}{context}{eligible}")
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        results = await asyncio.gather(
            probe_weibo(client, now),
            probe_bilibili_ranking(client, now),
            probe_bilibili_travel_search(client, now),
            probe_toutiao(client, now),
            probe_baidu(client, now),
        )

    out_dir = Path("data/probes")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(CN_TZ).strftime("%Y-%m-%d-%H%M")
    json_path = out_dir / f"cn-hot-platforms-v0-4-{stamp}.json"
    md_path = out_dir / f"cn-hot-platforms-v0-4-{stamp}.md"
    json_path.write_text(
        json.dumps(
            {"generated_at": now.isoformat(), "strict_window_hours": 24, "results": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render(list(results), now), encoding="utf-8")

    print("Chinese platform discovery V0.4 probe completed")
    for r in results:
        print(
            f"- {r.name}: {r.status}, HTTP={r.http_status}, signals={len(r.signals)}, "
            f"travel={sum(s.travel_match for s in r.signals)}, "
            f"direct24h_travel={sum(s.direct_24h_eligible and s.travel_match for s in r.signals)}"
        )
        if r.error:
            print(f"  diagnostic={r.error}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
