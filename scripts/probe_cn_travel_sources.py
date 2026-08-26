"""Probe retained Chinese travel/community sources with a strict 24-hour rule.

Only public lightweight metadata is read. No login, anti-bot bypass, or article-body
scraping. Content older than 24 hours can never become a formal candidate.
Trend-only pages without reliable publish time are snapshot signals only.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

CN_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


@dataclass
class Signal:
    title: str
    url: str
    published_at: str | None = None
    direct_24h_eligible: bool = False
    signal: str = ""


@dataclass
class ProbeResult:
    key: str
    name: str
    url: str
    status: str
    rule: str
    http_status: int | None = None
    final_url: str | None = None
    fresh_24h_count: int = 0
    signals: list[Signal] = field(default_factory=list)
    error: str | None = None


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def within_24h(dt: datetime | None, now: datetime) -> bool:
    if dt is None:
        return False
    return now - timedelta(hours=24) <= dt <= now + timedelta(minutes=10)


def parse_cn_date(text: str) -> datetime | None:
    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=CN_TZ
        ).astimezone(timezone.utc)
    except ValueError:
        return None


def dedupe(items: list[Signal], limit: int = 30) -> list[Signal]:
    seen: set[tuple[str, str]] = set()
    out: list[Signal] = []
    for item in items:
        key = (clean(item.title).lower(), item.url.rstrip("/"))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


async def probe_mafengwo(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://www.mafengwo.cn/club/"
    result = ProbeResult(
        key="mafengwo_fengshou",
        name="马蜂窝·蜂首",
        url=url,
        status="failure",
        rule="有明确发布日期；只有运行时刻往前24小时内发布的内容才可进入玩法候选预筛。",
    )
    try:
        resp = await client.get(url)
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        pattern = re.compile(r"(20\d{2}年\d{1,2}月\d{1,2}日)\s*蜂首游记\s*[《『](.+?)[》』]")
        items: list[Signal] = []
        for a in soup.find_all("a", href=True):
            text = clean(a.get_text(" ", strip=True))
            match = pattern.search(text)
            if not match:
                continue
            dt = parse_cn_date(match.group(1))
            eligible = within_24h(dt, now)
            items.append(
                Signal(
                    title=clean(match.group(2)),
                    url=urljoin(url, a["href"]),
                    published_at=dt.isoformat() if dt else None,
                    direct_24h_eligible=eligible,
                    signal="蜂首精选",
                )
            )
        result.signals = dedupe(items, 20)
        result.fresh_24h_count = sum(s.direct_24h_eligible for s in result.signals)
        result.status = "success" if result.signals else "empty"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


async def probe_ctrip(client: httpx.AsyncClient, now: datetime) -> ProbeResult:
    url = "https://you.ctrip.com/"
    result = ProbeResult(
        key="ctrip_guide",
        name="携程·攻略社区首页",
        url=url,
        status="failure",
        rule="页面缺少可靠发布时间，只保存当前热门目的地/攻略快照；不能直接进入候选，必须有24小时内新增/升温或其他新鲜证据。",
    )
    try:
        resp = await client.get(url)
        result.http_status = resp.status_code
        result.final_url = str(resp.url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items: list[Signal] = []
        for a in soup.find_all("a", href=True):
            title = clean(a.get_text(" ", strip=True))
            if len(title) < 4 or len(title) > 80:
                continue
            href = urljoin(url, a["href"])
            if "ctrip.com" not in href:
                continue
            if not any(token in href for token in ("/place/", "/sight/", "/travels/")):
                continue
            items.append(
                Signal(
                    title=title,
                    url=href,
                    direct_24h_eligible=False,
                    signal="携程趋势快照",
                )
            )
        result.signals = dedupe(items, 25)
        result.status = "success" if result.signals else "empty"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def render(results: list[ProbeResult], now: datetime) -> str:
    lines = [
        "# V0.3 中国旅行网站24小时探针",
        "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}",
        "",
        "硬规则：正式候选不得超过24小时。无可靠发布时间的趋势页只能保存快照，不可直接冒充新热点。",
        "",
        "| 来源 | 状态 | HTTP | 总信号 | 24h可直接预筛 |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.status} | {r.http_status or '-'} | {len(r.signals)} | {r.fresh_24h_count} |"
        )
    for r in results:
        lines.extend(["", f"## {r.name}", "", r.rule])
        if r.error:
            lines.extend(["", f"错误：`{r.error}`"])
            continue
        lines.append("")
        for s in r.signals[:15]:
            when = f"｜{s.published_at}" if s.published_at else ""
            state = "｜24h可预筛" if s.direct_24h_eligible else "｜仅趋势快照"
            lines.append(f"- [{s.title}]({s.url}){when}{state}")
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        results = await asyncio.gather(probe_mafengwo(client, now), probe_ctrip(client, now))

    out_dir = Path("data/probes")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(CN_TZ).strftime("%Y-%m-%d-%H%M")
    json_path = out_dir / f"cn-travel-sources-{stamp}.json"
    md_path = out_dir / f"cn-travel-sources-{stamp}.md"
    json_path.write_text(
        json.dumps(
            {"generated_at": now.isoformat(), "strict_window_hours": 24, "results": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(render(list(results), now), encoding="utf-8")

    print("Chinese travel source strict-24h probe completed")
    for r in results:
        print(
            f"- {r.name}: {r.status}, HTTP={r.http_status}, signals={len(r.signals)}, fresh24h={r.fresh_24h_count}"
        )
        if r.error:
            print(f"  error={r.error}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
