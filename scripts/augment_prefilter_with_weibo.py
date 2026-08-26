"""Augment the strict-24h prefilter with verified Weibo hot-search topics.

No LLM is used. Weibo realtime hot search is a discovery signal only because it does
not expose a trustworthy publish time. A topic is appended only when an exact-topic
Google News RSS search finds supporting evidence published within the last 24 hours.

Failure to reach Weibo is non-fatal: the base prefilter can still complete.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from build_travel_candidate_prefilter import (
    Candidate,
    TrendSignal,
    clean,
    find_fresh_evidence,
    merge_similar,
    normalize_title,
    should_keep,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
MAX_POOL = 60


async def fetch_weibo_trends(client: httpx.AsyncClient) -> list[TrendSignal]:
    url = "https://weibo.com/ajax/side/hotSearch"
    resp = await client.get(
        url,
        headers={
            "Referer": "https://weibo.com/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = ((payload.get("data") or {}).get("realtime") or [])
    out: list[TrendSignal] = []
    for idx, row in enumerate(rows, start=1):
        title = clean(str(row.get("word") or row.get("note") or ""))
        if not title:
            continue
        keep, score, reasons = should_keep(title)
        if not keep:
            continue
        hot = str(row.get("num") or row.get("raw_hot") or row.get("raw_hot_score") or "")
        out.append(
            TrendSignal(
                source="微博实时热搜",
                title=title,
                url=f"https://s.weibo.com/weibo?q={quote(title)}",
                rank=idx,
                hot=hot,
                pre_score=score,
                reasons=["微博热搜", *reasons],
            )
        )
    return out


def latest_prefilter() -> Path:
    files = sorted(Path("data/candidate-pools").glob("travel-prefilter-*.json"))
    if not files:
        raise SystemExit("No strict-24h prefilter JSON found")
    return files[-1]


async def main() -> None:
    source_path = latest_prefilter()
    data = json.loads(source_path.read_text(encoding="utf-8"))
    if data.get("strict_window_hours") != 24:
        raise SystemExit("Input prefilter lost strict 24h gate")

    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    signals: list[TrendSignal] = []
    verified: list[Candidate] = []
    diagnostic = ""
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        try:
            signals = await fetch_weibo_trends(client)
            results = await asyncio.gather(*(find_fresh_evidence(client, signal, now) for signal in signals))
            verified = [item for item in results if item is not None]
        except Exception as exc:
            diagnostic = f"{type(exc).__name__}: {exc}"

    existing = [Candidate(**item) for item in data.get("candidates", [])]
    merged = merge_similar([*existing, *verified])[:MAX_POOL]

    existing_rejected = list(data.get("rejected_unverified_trends", []))
    verified_titles = {normalize_title(item.title) for item in verified}
    rejected_weibo = [signal for signal in signals if normalize_title(signal.title) not in verified_titles]
    existing_rejected.extend(asdict(signal) for signal in rejected_weibo)

    stats = dict(data.get("stats") or {})
    stats["weibo_signals"] = len(signals)
    stats["weibo_verified"] = len(verified)
    stats["weibo_diagnostic"] = diagnostic
    stats["raw_before_merge_with_weibo"] = len(existing) + len(verified)
    stats["merged_pool_with_weibo"] = len(merged)
    if "trend_signals" in stats:
        stats["trend_signals"] = int(stats.get("trend_signals", 0)) + len(signals)
    if "trend_verified" in stats:
        stats["trend_verified"] = int(stats.get("trend_verified", 0)) + len(verified)

    data["stats"] = stats
    data["candidates"] = [asdict(item) for item in merged]
    data["rejected_unverified_trends"] = existing_rejected
    source_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = source_path.stem.replace("travel-prefilter-", "")
    report_path = source_path.with_name(f"travel-weibo-augment-{stamp}.md")
    lines = [
        "# V0.4 微博热搜24小时增强",
        "",
        "微博实时热搜只负责发现；没有24小时内独立新闻证据的词条不得进入正式前置池。",
        "",
        f"- 微博旅行相关热搜信号：{len(signals)}",
        f"- 获得24小时新闻证据：{len(verified)}",
        f"- 增强后原始前置池：{len(merged)}",
    ]
    if diagnostic:
        lines.append(f"- 诊断：{diagnostic}")
    lines.extend(["", "## 已验证进入前置池", ""])
    if verified:
        for item in verified:
            lines.append(
                f"- 微博#{item.trend_rank or '-'} {item.title}｜热度{item.trend_hot or '-'}｜"
                f"24h证据:{item.evidence_source or '-'}"
            )
    else:
        lines.append("- 无")
    lines.extend(["", "## 有旅行语义但未获24小时证据", ""])
    if rejected_weibo:
        for signal in rejected_weibo[:20]:
            lines.append(f"- 微博#{signal.rank or '-'} {signal.title}")
    else:
        lines.append("- 无")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Weibo strict-24h augmentation completed")
    print(f"- weibo_signals: {len(signals)}")
    print(f"- weibo_verified: {len(verified)}")
    print(f"- merged_pool_with_weibo: {len(merged)}")
    if diagnostic:
        print(f"- diagnostic: {diagnostic}")
    print(f"Updated: {source_path}")
    print(f"Report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
