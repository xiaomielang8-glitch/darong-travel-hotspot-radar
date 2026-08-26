"""Build a zero-AI travel hotspot pre-candidate pool.

Purpose:
- enforce the 24-hour hard gate before any LLM call;
- combine fresh travel news, tourist-facing community content and Chinese hot boards;
- reject obvious PR/old-guide/noise deterministically;
- require fresh evidence for trend boards that do not expose publish timestamps.

This stage does NOT produce the final 12-18 candidates and does NOT call Doubao.
It should reduce the raw web universe to a compact, auditable pool for the next AI stage.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

CN_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"
MAX_POOL = 60

# Direct tourism/traveller semantics. A single generic word such as “酒店” is intentionally absent.
TRAVEL_STRONG = (
    "景区", "景点", "游客", "旅游", "旅行", "出游", "旅客", "门票", "预约", "限流",
    "闭园", "开园", "恢复开放", "暂停开放", "民宿", "自驾", "夜游", "亲子游", "避暑游",
    "游乐园", "乐园", "博物馆", "古镇", "文旅", "打卡点", "度假区", "旅行社", "导游",
    "航班", "机场", "高铁", "列车", "火车", "客运", "签证", "退改", "停航", "停运",
    "海岛", "索道", "游船", "漂流", "露营", "徒步", "研学", "出境游", "入境游",
)

EVENT_TERMS = (
    "新开", "开业", "开放", "关闭", "闭园", "暂停", "恢复", "取消", "停航", "停运", "滞留",
    "涨价", "降价", "免费", "预约", "限流", "排队", "拥堵", "事故", "争议", "投诉", "宰客",
    "爆火", "火了", "走红", "出圈", "爆满", "人满", "售罄", "退票", "退改", "新规", "调整",
    "台风", "暴雨", "高温", "山洪", "地震", "新增", "首开", "首发", "首航", "首列",
)

NEARBY_TERMS = (
    "安徽", "滁州", "合肥", "黄山", "芜湖", "南京", "江苏", "苏州", "无锡", "常州", "扬州",
    "上海", "浙江", "杭州", "湖州", "嘉兴", "宁波", "绍兴", "舟山", "长三角",
)

PR_NOISE = (
    "入选", "获奖", "荣获", "案例名单", "优秀案例", "发布会", "推介会", "签约仪式", "工作会议",
    "调研行", "高质量发展", "文旅融合发展", "项目建设", "招商推介", "赋能", "共绘", "共话",
    "交流会", "座谈会", "培训班", "启动仪式", "成果发布", "品牌推介",
)

GENERIC_GUIDE = (
    "攻略", "三日游", "两日游", "一日游", "怎么玩", "必去", "必打卡", "保姆级", "路线推荐",
    "游玩指南", "吃住行", "旅行清单",
)

SOURCE_BLACKLIST = {"énergie-info"}

GOOGLE_QUERIES = [
    (
        "游客事件与景区变化",
        '("景区" OR "游客" OR "旅游") ("闭园" OR "开放" OR "预约" OR "限流" OR "停航" OR "滞留" OR "事故" OR "争议" OR "爆火" OR "走红")',
    ),
    (
        "长三角游客热点",
        '("安徽" OR "江苏" OR "浙江" OR "上海" OR "南京" OR "合肥" OR "滁州") ("游客" OR "景区" OR "旅游" OR "旅行")',
    ),
    (
        "交通规则与出境变化",
        '("高铁" OR "航班" OR "机场" OR "签证" OR "门票" OR "预约") ("游客" OR "旅游" OR "出游" OR "旅客")',
    ),
    (
        "新玩法与目的地升温",
        '("夜游" OR "亲子游" OR "避暑游" OR "乐园" OR "博物馆" OR "古镇" OR "露营" OR "打卡") ("游客" OR "旅游" OR "旅行" OR "景区")',
    ),
]


@dataclass
class Candidate:
    source: str
    source_name: str
    title: str
    url: str
    published_at: str
    category: str
    pre_score: float
    reasons: list[str] = field(default_factory=list)
    evidence_source: str | None = None
    evidence_url: str | None = None
    evidence_published_at: str | None = None
    trend_rank: int | None = None
    trend_hot: str | None = None


@dataclass
class TrendSignal:
    source: str
    title: str
    url: str
    rank: int | None = None
    hot: str = ""
    pre_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_title(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", clean(text)).lower()


def within_24h(dt: datetime | None, now: datetime) -> bool:
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - timedelta(hours=24) <= dt <= now + timedelta(minutes=10)


def parse_feed_time(entry) -> datetime | None:
    raw = entry.get("published") or entry.get("updated") or ""
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def title_score(title: str) -> tuple[float, list[str]]:
    title = clean(title)
    reasons: list[str] = []
    score = 0.0

    strong_hits = [term for term in TRAVEL_STRONG if term in title]
    event_hits = [term for term in EVENT_TERMS if term in title]
    nearby_hits = [term for term in NEARBY_TERMS if term in title]
    pr_hits = [term for term in PR_NOISE if term in title]
    guide_hits = [term for term in GENERIC_GUIDE if term in title]

    if strong_hits:
        score += 3.0 + min(1.0, 0.25 * (len(strong_hits) - 1))
        reasons.append("旅游语义:" + "/".join(strong_hits[:3]))
    if event_hits:
        score += 2.0 + min(0.5, 0.15 * (len(event_hits) - 1))
        reasons.append("新事件:" + "/".join(event_hits[:3]))
    if nearby_hits:
        score += 1.0
        reasons.append("长三角:" + "/".join(nearby_hits[:2]))
    if any(term in title for term in ("游客被困", "游客滞留", "游客投诉", "游客受伤", "游客死亡", "触电", "溺亡", "宰客")):
        score += 1.0
        reasons.append("游客强事件")
    if any(term in title for term in ("热搜", "爆火", "走红", "出圈", "爆满", "人挤人")):
        score += 0.75
        reasons.append("传播信号")

    if pr_hits:
        score -= 4.0
        reasons.append("宣传稿降权:" + "/".join(pr_hits[:2]))
    if guide_hits and not event_hits:
        score -= 3.0
        reasons.append("普通攻略降权")

    # Generic weather/traffic/hotel mentions must not pass without tourist semantics.
    if not strong_hits and any(term in title for term in ("酒店", "台风", "暴雨", "铁路", "火车", "机场")):
        score = min(score, 2.5)
        reasons.append("泛词不足")

    return round(score, 2), reasons


def should_keep(title: str, min_score: float = 4.0) -> tuple[bool, float, list[str]]:
    score, reasons = title_score(title)
    return score >= min_score, score, reasons


def google_news_url(query: str) -> str:
    params = {
        "q": f"{query} when:24h",
        "hl": "zh-CN",
        "gl": "CN",
        "ceid": "CN:zh-Hans",
    }
    return GOOGLE_NEWS_BASE + "?" + urlencode(params)


async def fetch_google_feed(client: httpx.AsyncClient, label: str, query: str, now: datetime, limit: int = 35) -> list[Candidate]:
    resp = await client.get(google_news_url(query))
    resp.raise_for_status()
    feed = feedparser.loads(resp.content)
    out: list[Candidate] = []
    for entry in feed.entries[:limit]:
        dt = parse_feed_time(entry)
        if not within_24h(dt, now):
            continue
        title = clean(entry.get("title", ""))
        keep, score, reasons = should_keep(title)
        if not keep:
            continue
        source_name = clean(((entry.get("source") or {}).get("title") if isinstance(entry.get("source"), dict) else "") or "Google News")
        if source_name in SOURCE_BLACKLIST:
            continue
        out.append(
            Candidate(
                source="google_news",
                source_name=source_name,
                title=title,
                url=str(entry.get("link") or ""),
                published_at=dt.isoformat(),
                category=label,
                pre_score=score,
                reasons=reasons,
            )
        )
    return out


async def fetch_mafengwo(client: httpx.AsyncClient, now: datetime) -> list[Candidate]:
    url = "https://www.mafengwo.cn/club/"
    resp = await client.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(r"(20\d{2}年\d{1,2}月\d{1,2}日)\s*蜂首游记\s*[《『](.+?)[》』]")
    out: list[Candidate] = []
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True))
        match = pattern.search(text)
        if not match:
            continue
        # Page exposes date only. Treat midnight China time as the conservative proof point:
        # this prevents a previous-day item of unknown hour from slipping beyond 24h.
        try:
            dt = datetime(
                int(match.group(1)[:4]),
                int(re.search(r"年(\d{1,2})月", match.group(1)).group(1)),
                int(re.search(r"月(\d{1,2})日", match.group(1)).group(1)),
                tzinfo=CN_TZ,
            ).astimezone(timezone.utc)
        except (AttributeError, ValueError):
            continue
        if not within_24h(dt, now):
            continue
        title = clean(match.group(2))
        # Fresh community content is allowed into the pre-pool even if it is a guide;
        # it is tagged as inspiration and will not be mistaken for a hard-news event.
        score, reasons = title_score(title)
        out.append(
            Candidate(
                source="mafengwo",
                source_name="马蜂窝·蜂首",
                title=title,
                url=urljoin(url, a["href"]),
                published_at=dt.isoformat(),
                category="游客玩法灵感",
                pre_score=max(3.5, score),
                reasons=["24h新发布游客内容", *reasons],
            )
        )
    return out


async def fetch_toutiao_trends(client: httpx.AsyncClient) -> list[TrendSignal]:
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    resp = await client.get(url, headers={"Referer": "https://www.toutiao.com/"})
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") or payload.get("Data") or []
    out: list[TrendSignal] = []
    for idx, row in enumerate(rows, start=1):
        title = clean(str(row.get("Title") or row.get("title") or ""))
        keep, score, reasons = should_keep(title)
        if not keep:
            continue
        link = str(row.get("Url") or row.get("url") or "") or f"https://www.toutiao.com/search/?keyword={quote(title)}"
        out.append(
            TrendSignal(
                source="今日头条热榜",
                title=title,
                url=link,
                rank=idx,
                hot=str(row.get("HotValue") or row.get("hot_value") or ""),
                pre_score=score,
                reasons=reasons,
            )
        )
    return out


async def fetch_baidu_trends(client: httpx.AsyncClient) -> list[TrendSignal]:
    url = "https://top.baidu.com/board?tab=realtime"
    resp = await client.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: list[TrendSignal] = []
    cards = soup.select("div.category-wrap_iQLoo")
    for idx, card in enumerate(cards, start=1):
        a = card.find("a", href=True)
        title_node = card.select_one("div.c-single-text-ellipsis")
        title = clean(title_node.get_text(" ", strip=True)) if title_node else ""
        if not a or not title:
            continue
        keep, score, reasons = should_keep(title)
        if not keep:
            continue
        hot_node = card.select_one("div.hot-index_1Bl1a")
        out.append(
            TrendSignal(
                source="百度实时热搜",
                title=title,
                url=a.get("href", ""),
                rank=idx,
                hot=clean(hot_node.get_text(" ", strip=True)) if hot_node else "",
                pre_score=score,
                reasons=reasons,
            )
        )
    return out


async def fetch_bilibili_fresh(client: httpx.AsyncClient, now: datetime) -> list[Candidate]:
    url = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
    resp = await client.get(url)
    resp.raise_for_status()
    rows = ((resp.json().get("data") or {}).get("list") or [])
    out: list[Candidate] = []
    for idx, row in enumerate(rows, start=1):
        ts = row.get("pubdate")
        if not isinstance(ts, (int, float)):
            continue
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        if not within_24h(dt, now):
            continue
        title = clean(str(row.get("title") or ""))
        keep, score, reasons = should_keep(title)
        if not keep:
            continue
        bvid = str(row.get("bvid") or "")
        out.append(
            Candidate(
                source="bilibili",
                source_name="B站全站热门榜",
                title=title,
                url=f"https://www.bilibili.com/video/{bvid}" if bvid else "https://www.bilibili.com/v/popular/rank/all",
                published_at=dt.isoformat(),
                category="平台热点",
                pre_score=score + 0.5,
                reasons=[f"B站热门#{idx}", *reasons],
                trend_rank=idx,
            )
        )
    return out


async def find_fresh_evidence(client: httpx.AsyncClient, signal: TrendSignal, now: datetime) -> Candidate | None:
    # Trend boards expose no trustworthy publish time. Search the exact topic in a strict
    # 24h news window; without fresh supporting evidence the trend stays diagnostic only.
    query = f'"{signal.title}"'
    try:
        resp = await client.get(google_news_url(query))
        resp.raise_for_status()
    except Exception:
        return None
    feed = feedparser.loads(resp.content)
    for entry in feed.entries[:10]:
        dt = parse_feed_time(entry)
        if not within_24h(dt, now):
            continue
        source_name = clean(((entry.get("source") or {}).get("title") if isinstance(entry.get("source"), dict) else "") or "Google News")
        if source_name in SOURCE_BLACKLIST:
            continue
        return Candidate(
            source="trend_verified",
            source_name=signal.source,
            title=signal.title,
            url=signal.url,
            published_at=dt.isoformat(),
            category="平台热点",
            pre_score=round(signal.pre_score + 1.0, 2),
            reasons=["热榜出现", "24h新闻证据", *signal.reasons],
            evidence_source=source_name,
            evidence_url=str(entry.get("link") or ""),
            evidence_published_at=dt.isoformat(),
            trend_rank=signal.rank,
            trend_hot=signal.hot,
        )
    return None


def merge_similar(candidates: list[Candidate]) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda c: c.pre_score, reverse=True)
    kept: list[Candidate] = []
    for cand in ordered:
        norm = normalize_title(cand.title)
        duplicate = False
        for existing in kept:
            other = normalize_title(existing.title)
            if not norm or not other:
                continue
            if norm in other or other in norm or SequenceMatcher(None, norm, other).ratio() >= 0.82:
                duplicate = True
                break
        if not duplicate:
            kept.append(cand)
    return kept


def render(candidates: list[Candidate], rejected_trends: list[TrendSignal], stats: dict, now: datetime) -> str:
    lines = [
        "# V0.3 旅游热点 AI 前置候选池",
        "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}",
        "",
        "硬规则：所有正式进入本池的条目，都必须有不超过24小时的新鲜度证据。本报告尚未调用AI，也不是最终12—18条成稿候选。",
        "",
        "## 统计",
        "",
        f"- Google News 初步保留：{stats.get('google_kept', 0)}",
        f"- 马蜂窝24h新内容：{stats.get('mafengwo_kept', 0)}",
        f"- B站24h且旅行相关：{stats.get('bilibili_kept', 0)}",
        f"- 头条/百度旅行热榜信号：{stats.get('trend_signals', 0)}",
        f"- 热榜经24h新闻证据确认：{stats.get('trend_verified', 0)}",
        f"- 合并去重后前置池：{len(candidates)}（上限{MAX_POOL}）",
        "",
        "## 可进入下一步AI评分",
        "",
    ]
    for idx, c in enumerate(candidates, start=1):
        evidence = ""
        if c.evidence_source:
            evidence = f"｜热榜新鲜度证据：{c.evidence_source}"
        trend = f"｜榜单#{c.trend_rank}" if c.trend_rank else ""
        lines.append(
            f"{idx}. **{c.title}**｜{c.source_name}｜预分{c.pre_score:g}｜{c.category}{trend}{evidence}\n"
            f"   - 时间：{c.published_at}\n"
            f"   - 理由：{'；'.join(c.reasons[:5])}\n"
            f"   - 链接：{c.url}"
        )
    lines.extend(["", "## 热榜有旅行语义但未获24h证据（不进入候选）", ""])
    if not rejected_trends:
        lines.append("- 无")
    else:
        for s in rejected_trends[:20]:
            lines.append(f"- {s.source} #{s.rank or '-'}：{s.title}｜未找到24h可靠新闻证据")
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        google_batches, mafengwo, toutiao, baidu, bilibili = await asyncio.gather(
            asyncio.gather(*(fetch_google_feed(client, label, query, now) for label, query in GOOGLE_QUERIES)),
            fetch_mafengwo(client, now),
            fetch_toutiao_trends(client),
            fetch_baidu_trends(client),
            fetch_bilibili_fresh(client, now),
        )
        google = [item for batch in google_batches for item in batch]
        trend_signals = [*toutiao, *baidu]
        verified_results = await asyncio.gather(*(find_fresh_evidence(client, signal, now) for signal in trend_signals))
        verified = [item for item in verified_results if item is not None]
        verified_titles = {normalize_title(item.title) for item in verified}
        rejected_trends = [s for s in trend_signals if normalize_title(s.title) not in verified_titles]

    all_candidates = [*google, *mafengwo, *bilibili, *verified]
    merged = merge_similar(all_candidates)[:MAX_POOL]
    stats = {
        "google_kept": len(google),
        "mafengwo_kept": len(mafengwo),
        "bilibili_kept": len(bilibili),
        "trend_signals": len(trend_signals),
        "trend_verified": len(verified),
        "raw_before_merge": len(all_candidates),
    }

    out_dir = Path("data/candidate-pools")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(CN_TZ).strftime("%Y-%m-%d-%H%M")
    json_path = out_dir / f"travel-prefilter-{stamp}.json"
    md_path = out_dir / f"travel-prefilter-{stamp}.md"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "strict_window_hours": 24,
                "stats": stats,
                "candidates": [asdict(c) for c in merged],
                "rejected_unverified_trends": [asdict(s) for s in rejected_trends],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render(merged, rejected_trends, stats, now), encoding="utf-8")

    print("Travel candidate prefilter completed")
    for key, value in stats.items():
        print(f"- {key}: {value}")
    print(f"- merged_pool: {len(merged)}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
