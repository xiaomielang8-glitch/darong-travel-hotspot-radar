"""Resilient entrypoint for the strict-24h travel prefilter.

The original collector functions are reused, but network sources are isolated so one
transient 4xx/5xx response cannot abort the whole daily radar. Google News queries are
also staggered instead of fired in one burst, reducing cloud-runner rate-limit risk.

No LLM is used.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from build_travel_candidate_prefilter import (
    CN_TZ,
    GOOGLE_QUERIES,
    MAX_POOL,
    Candidate,
    fetch_baidu_trends,
    fetch_bilibili_fresh,
    fetch_google_feed,
    fetch_mafengwo,
    fetch_toutiao_trends,
    find_fresh_evidence,
    merge_similar,
    normalize_title,
    render,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


async def safe_collect(name: str, awaitable):
    try:
        return await awaitable, None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(f"Source warning [{name}]: {message}")
        return [], message


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json,application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    errors: dict[str, str] = {}

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        # Google News is the largest source. Query sequentially with a short gap so one
        # throttled query does not poison the others or cause a burst of 503 responses.
        google: list[Candidate] = []
        google_successes = 0
        for index, (label, query) in enumerate(GOOGLE_QUERIES):
            batch, error = await safe_collect(
                f"google:{label}",
                fetch_google_feed(client, label, query, now),
            )
            if error:
                errors[f"google:{label}"] = error
            else:
                google_successes += 1
                google.extend(batch)
            if index < len(GOOGLE_QUERIES) - 1:
                await asyncio.sleep(1.25)

        # Other independent public sources can run together. Their failures are isolated.
        other_results = await asyncio.gather(
            safe_collect("mafengwo", fetch_mafengwo(client, now)),
            safe_collect("toutiao", fetch_toutiao_trends(client)),
            safe_collect("baidu", fetch_baidu_trends(client)),
            safe_collect("bilibili", fetch_bilibili_fresh(client, now)),
        )
        (mafengwo, maf_error), (toutiao, tt_error), (baidu, bd_error), (bilibili, bili_error) = other_results
        for name, error in (
            ("mafengwo", maf_error),
            ("toutiao", tt_error),
            ("baidu", bd_error),
            ("bilibili", bili_error),
        ):
            if error:
                errors[name] = error

        trend_signals = [*toutiao, *baidu]
        verified_results = await asyncio.gather(
            *(find_fresh_evidence(client, signal, now) for signal in trend_signals)
        )
        verified = [item for item in verified_results if item is not None]
        verified_titles = {normalize_title(item.title) for item in verified}
        rejected_trends = [
            signal for signal in trend_signals
            if normalize_title(signal.title) not in verified_titles
        ]

    all_candidates = [*google, *mafengwo, *bilibili, *verified]
    merged = merge_similar(all_candidates)[:MAX_POOL]
    stats = {
        "google_kept": len(google),
        "google_query_successes": google_successes,
        "google_query_total": len(GOOGLE_QUERIES),
        "mafengwo_kept": len(mafengwo),
        "bilibili_kept": len(bilibili),
        "trend_signals": len(trend_signals),
        "trend_verified": len(verified),
        "raw_before_merge": len(all_candidates),
        "source_errors": errors,
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
                "candidates": [asdict(item) for item in merged],
                "rejected_unverified_trends": [asdict(signal) for signal in rejected_trends],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report = render(merged, rejected_trends, stats, now)
    if errors:
        report += "\n## 采集源异常（本轮已隔离）\n\n"
        for name, error in errors.items():
            report += f"- {name}: {error}\n"
    md_path.write_text(report, encoding="utf-8")

    print("Travel candidate resilient prefilter completed")
    for key, value in stats.items():
        print(f"- {key}: {value}")
    print(f"- merged_pool: {len(merged)}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")

    # Do not silently accept a severely degraded run. One Google query may fail without
    # killing the day, but if every Google query failed and the remaining sources cannot
    # build a useful pool, the workflow business gates below will stop the run.
    if google_successes == 0:
        print("Warning: all Google News discovery queries failed this run")


if __name__ == "__main__":
    asyncio.run(main())
