"""Probe public Chinese travel/community pages before wiring them into Horizon.

This probe intentionally collects only lightweight public metadata (title, URL,
visible date/rank text). It does not log in, bypass anti-bot controls, or copy
article bodies. The goal is to verify which sources are reachable from GitHub
Actions and which ones actually expose fresh or useful trend signals.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SourceSpec:
    key: str
    name: str
    url: str
    signal_type: str
    note: str


@dataclass
class Signal:
    title: str
    url: str
    published_at: str | None = None
    signal: str = ""


@dataclass
class ProbeResult:
    key: str
    name: str
    url: str
    signal_type: str
    note: str
    status: str
    http_status: int | None = None
    final_url: str | None = None
    bytes: int = 0
    fresh_72h_count: int = 0
    signals: list[Signal] = field(default_factory=list)
    error: str | None = None


SOURCES = [
    SourceSpec(
        key="mafengwo_fengshou",
        name="马蜂窝·蜂首",
        url="https://www.mafengwo.cn/club/",
        signal_type="fresh_content",
        note="优先验证：每天精选游记，页面直接暴露发布日期，适合作为游客玩法/目的地灵感源。",
    ),
    SourceSpec(
        key="mafengwo_home",
        name="马蜂窝·首页社区流",
        url="https://www.mafengwo.cn/",
        signal_type="community_trend",
        note="观察社区推荐标题、阅读/互动信号；无可靠发布时间时只作为趋势信号，不直接当24小时新闻。",
    ),
    SourceSpec(
        key="tongcheng_travels",
        name="同程·攻略游记",
        url="https://go.ly.com/travels/",
        signal_type="fresh_content_or_trend",
        note="验证“最新发布”是否在服务端HTML可见；若只有旧精选，则降级为趋势参考。",
    ),
    SourceSpec(
        key="ctrip_guide",
        name="携程·攻略社区首页",
        url="https://you.ctrip.com/",
        signal_type="trend_only",
        note="携程旧游记频道已停止运维，当前只验证热门目的地/攻略等公开趋势信号，不把静态攻略冒充24小时热点。",
    ),
    SourceSpec(
        key="qunar_touch",
        name="去哪儿·攻略移动首页",
        url="https://touch.travel.qunar.com/",
        signal_type="trend_only",
        note="验证热门目的地、榜单、玩法入口是否可稳定读取；无发布时间则仅作为趋势信号。",
    ),
]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _same_host_or_subdomain(base_url: str, target_url: str) -> bool:
    base = (urlsplit(base_url).hostname or "").lower()
    target = (urlsplit(target_url).hostname or "").lower()
    if not target:
        return True
    root = ".".join(base.split(".")[-2:])
    return target == root or target.endswith("." + root)


def _dedupe(signals: Iterable[Signal], limit: int = 30) -> list[Signal]:
    seen: set[tuple[str, str]] = set()
    result: list[Signal] = []
    for signal in signals:
        key = (_clean_text(signal.title).lower(), signal.url.rstrip("/"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(signal)
        if len(result) >= limit:
            break
    return result


def _parse_cn_date(text: str) -> datetime | None:
    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=timezone(timedelta(hours=8)),
        )
    except ValueError:
        return None


def _parse_isoish_date(text: str) -> datetime | None:
    match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=timezone(timedelta(hours=8)),
        )
    except ValueError:
        return None


def _is_fresh(dt: datetime | None, now: datetime, hours: int = 72) -> bool:
    if dt is None:
        return False
    return now - timedelta(hours=hours) <= dt <= now + timedelta(hours=24)


def _generic_anchor_signals(spec: SourceSpec, soup: BeautifulSoup) -> list[Signal]:
    signals: list[Signal] = []
    for anchor in soup.find_all("a", href=True):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 5 or len(title) > 120:
            continue
        href = urljoin(spec.url, anchor.get("href", ""))
        if not href.startswith(("http://", "https://")):
            continue
        if not _same_host_or_subdomain(spec.url, href):
            continue
        if any(x in title for x in ("登录", "注册", "首页", "更多", "客服", "帮助", "意见反馈")):
            continue
        signals.append(Signal(title=title, url=href))
    return _dedupe(signals, 25)


def _parse_mafengwo_fengshou(spec: SourceSpec, soup: BeautifulSoup) -> list[Signal]:
    signals: list[Signal] = []
    pattern = re.compile(r"(20\d{2}年\d{1,2}月\d{1,2}日)\s*蜂首游记\s*[《『](.+?)[》』]")
    for anchor in soup.find_all("a", href=True):
        text = _clean_text(anchor.get_text(" ", strip=True))
        match = pattern.search(text)
        if not match:
            continue
        dt = _parse_cn_date(match.group(1))
        title = _clean_text(match.group(2))
        signals.append(
            Signal(
                title=title,
                url=urljoin(spec.url, anchor["href"]),
                published_at=dt.isoformat() if dt else None,
                signal="蜂首精选",
            )
        )
    return _dedupe(signals, 20)


def _parse_tongcheng(spec: SourceSpec, soup: BeautifulSoup) -> list[Signal]:
    # The current desktop page exposes some featured entries as e.g.
    # "13 2026 MAY" next to the linked title. Keep the visible date when found.
    month_map = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    full_text = _clean_text(soup.get_text(" ", strip=True))
    dated_titles: list[Signal] = []
    # First keep article-like links. Then infer a nearby date from parent/grandparent text.
    for anchor in soup.find_all("a", href=True):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 8 or len(title) > 120:
            continue
        href = urljoin(spec.url, anchor["href"])
        if "/travels/" not in href:
            continue
        context = ""
        node = anchor
        for _ in range(3):
            node = node.parent
            if node is None:
                break
            context = _clean_text(node.get_text(" ", strip=True))
            if re.search(r"\b\d{1,2}\s+20\d{2}\s+[A-Z]{3}\b", context):
                break
        published_at = None
        match = re.search(r"\b(\d{1,2})\s+(20\d{2})\s+([A-Z]{3})\b", context)
        if match and match.group(3) in month_map:
            try:
                dt = datetime(
                    int(match.group(2)), month_map[match.group(3)], int(match.group(1)),
                    tzinfo=timezone(timedelta(hours=8)),
                )
                published_at = dt.isoformat()
            except ValueError:
                pass
        dated_titles.append(
            Signal(title=title, url=href, published_at=published_at, signal="同程游记")
        )
    if dated_titles:
        return _dedupe(dated_titles, 25)
    return _generic_anchor_signals(spec, soup)


def _parse_ctrip(spec: SourceSpec, soup: BeautifulSoup) -> list[Signal]:
    signals: list[Signal] = []
    for anchor in soup.find_all("a", href=True):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 4 or len(title) > 80:
            continue
        href = urljoin(spec.url, anchor["href"])
        if not _same_host_or_subdomain(spec.url, href):
            continue
        if not any(token in href for token in ("/place/", "/sight/", "/travels/")):
            continue
        signals.append(Signal(title=title, url=href, signal="携程公开攻略/目的地信号"))
    return _dedupe(signals, 25)


def _parse_qunar(spec: SourceSpec, soup: BeautifulSoup) -> list[Signal]:
    signals: list[Signal] = []
    for anchor in soup.find_all("a", href=True):
        title = _clean_text(anchor.get_text(" ", strip=True))
        if len(title) < 4 or len(title) > 100:
            continue
        href = urljoin(spec.url, anchor["href"])
        if not _same_host_or_subdomain(spec.url, href):
            continue
        if not any(token in href for token in ("/youji", "/book", "/p-", "/poi")):
            continue
        signals.append(Signal(title=title, url=href, signal="去哪儿公开攻略/榜单信号"))
    return _dedupe(signals, 25)


def _parse(spec: SourceSpec, html: str) -> list[Signal]:
    soup = BeautifulSoup(html, "html.parser")
    if spec.key == "mafengwo_fengshou":
        return _parse_mafengwo_fengshou(spec, soup)
    if spec.key == "tongcheng_travels":
        return _parse_tongcheng(spec, soup)
    if spec.key == "ctrip_guide":
        return _parse_ctrip(spec, soup)
    if spec.key == "qunar_touch":
        return _parse_qunar(spec, soup)
    return _generic_anchor_signals(spec, soup)


async def _probe_one(client: httpx.AsyncClient, spec: SourceSpec, now: datetime) -> ProbeResult:
    result = ProbeResult(
        key=spec.key,
        name=spec.name,
        url=spec.url,
        signal_type=spec.signal_type,
        note=spec.note,
        status="failure",
    )
    try:
        response = await client.get(spec.url)
        result.http_status = response.status_code
        result.final_url = str(response.url)
        result.bytes = len(response.content)
        response.raise_for_status()
        signals = _parse(spec, response.text)
        result.signals = signals
        fresh = 0
        for signal in signals:
            dt = None
            if signal.published_at:
                try:
                    dt = datetime.fromisoformat(signal.published_at)
                except ValueError:
                    dt = None
            if _is_fresh(dt, now):
                fresh += 1
        result.fresh_72h_count = fresh
        result.status = "success" if signals else "empty"
    except Exception as exc:  # probe must report rather than crash all sources
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _render_markdown(results: list[ProbeResult], generated_at: datetime) -> str:
    lines = [
        "# V0.3 中国旅行网站采集可行性探针",
        "",
        f"生成时间：{generated_at.astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')}",
        "",
        "本报告只验证公开页面的可达性与轻量元数据（标题、链接、可见时间/趋势信号），不抓取正文。",
        "",
        "| 来源 | 状态 | HTTP | 解析信号 | 近72小时有明确日期 | 类型 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.status} | {result.http_status or '-'} | "
            f"{len(result.signals)} | {result.fresh_72h_count} | {result.signal_type} |"
        )
    for result in results:
        lines.extend(["", f"## {result.name}", "", result.note])
        if result.error:
            lines.extend(["", f"错误：`{result.error}`"])
            continue
        if not result.signals:
            lines.extend(["", "未解析到可用信号。"])
            continue
        lines.append("")
        for signal in result.signals[:12]:
            when = f"｜{signal.published_at}" if signal.published_at else ""
            tag = f"｜{signal.signal}" if signal.signal else ""
            lines.append(f"- [{signal.title}]({signal.url}){when}{tag}")
    lines.extend([
        "",
        "## 判定规则",
        "",
        "- `fresh_content`：页面本身能给出可靠发布时间，可直接进入24/72小时候选预筛。",
        "- `community_trend` / `trend_only`：没有可靠发布时间时，只保留为热度/榜单信号；后续必须依赖跨日排名变化或其他新鲜度证据，不能每天重复冒充新热点。",
        "- 某来源若从 GitHub Actions 返回 403/验证码/空壳页面，本轮不接入正式采集，不做绕过。",
    ])
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(25.0),
        follow_redirects=True,
    ) as client:
        results = await asyncio.gather(*(_probe_one(client, spec, now) for spec in SOURCES))

    out_dir = Path("data/probes")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    json_path = out_dir / f"cn-travel-sources-{stamp}.json"
    md_path = out_dir / f"cn-travel-sources-{stamp}.md"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "results": [
                    {**asdict(result), "signals": [asdict(s) for s in result.signals]}
                    for result in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(_render_markdown(list(results), now), encoding="utf-8")

    print("Chinese travel source probe completed")
    for result in results:
        print(
            f"- {result.name}: {result.status}, HTTP={result.http_status}, "
            f"signals={len(result.signals)}, fresh72h={result.fresh_72h_count}"
        )
        if result.error:
            print(f"  error={result.error}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
