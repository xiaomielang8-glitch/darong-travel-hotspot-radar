"""Build rolling travel-relevant trend snapshots for Chinese hot boards.

Purpose:
- preserve Weibo/Toutiao/Baidu realtime-board state every few hours;
- prove a topic is newly on the board or materially rising within <=24 hours;
- avoid treating a timeless realtime board as a publish-time source;
- use zero LLM calls.

The first run only seeds a baseline. A formal trend delta is emitted only when a
previous snapshot exists and is recent enough to prove the change happened inside
the strict freshness window.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from probe_cn_hot_platforms import (
    CN_TZ,
    ProbeResult,
    Signal,
    probe_baidu,
    probe_toutiao,
    probe_weibo,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
STATE_PATH = Path("data/platform-trend-state/history.json")
REPORT_DIR = Path("data/trend-snapshots")
HISTORY_HOURS = 27
MAX_PROOF_GAP_HOURS = 6
RANK_SURGE_STEP = 8
RANK_SURGE_24H = 12


@dataclass
class DeltaSignal:
    source: str
    title: str
    url: str
    current_rank: int | None
    previous_rank: int | None
    hot: str
    proof: str
    captured_at: str
    previous_captured_at: str
    freshness_hours: float


def normalize(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text or "").lower()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_history() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "snapshots": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "snapshots": []}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"version": 1, "snapshots": []}
    return data


def compact_signal(signal: Signal) -> dict:
    return {
        "title": signal.title,
        "url": signal.url,
        "rank": signal.rank,
        "hot": signal.hot,
        "travel_match": bool(signal.travel_match),
    }


def travel_rows(result: ProbeResult) -> list[dict]:
    return [compact_signal(signal) for signal in result.signals if signal.travel_match]


def latest_valid_snapshot(snapshots: list[dict], now: datetime) -> tuple[dict | None, float | None]:
    for snapshot in reversed(snapshots):
        captured = parse_dt(snapshot.get("captured_at"))
        if captured is None or captured >= now:
            continue
        gap = (now - captured).total_seconds() / 3600
        if gap <= MAX_PROOF_GAP_HOURS:
            return snapshot, gap
        return None, gap
    return None, None


def rank_map(rows: list[dict]) -> dict[str, dict]:
    return {normalize(str(row.get("title") or "")): row for row in rows if normalize(str(row.get("title") or ""))}


def prior_24h_ranks(snapshots: list[dict], source: str, title_key: str, now: datetime) -> list[int]:
    ranks: list[int] = []
    cutoff = now - timedelta(hours=24)
    for snapshot in snapshots:
        captured = parse_dt(snapshot.get("captured_at"))
        if captured is None or captured < cutoff or captured >= now:
            continue
        for row in ((snapshot.get("sources") or {}).get(source) or []):
            if normalize(str(row.get("title") or "")) != title_key:
                continue
            rank = row.get("rank")
            if isinstance(rank, int):
                ranks.append(rank)
    return ranks


def build_deltas(
    current_sources: dict[str, list[dict]],
    snapshots: list[dict],
    previous: dict | None,
    previous_gap: float | None,
    now: datetime,
) -> list[DeltaSignal]:
    if previous is None or previous_gap is None or previous_gap > MAX_PROOF_GAP_HOURS:
        return []

    deltas: list[DeltaSignal] = []
    previous_sources = previous.get("sources") or {}
    previous_at = str(previous.get("captured_at") or "")

    for source, rows in current_sources.items():
        previous_rows = rank_map(previous_sources.get(source) or [])
        for row in rows:
            title = str(row.get("title") or "")
            key = normalize(title)
            if not key:
                continue
            current_rank = row.get("rank") if isinstance(row.get("rank"), int) else None
            prev = previous_rows.get(key)
            previous_rank = prev.get("rank") if prev and isinstance(prev.get("rank"), int) else None
            proof = ""

            if prev is None:
                proof = "new_entry"
            elif current_rank is not None and previous_rank is not None and previous_rank - current_rank >= RANK_SURGE_STEP:
                proof = "rank_surge"
            elif current_rank is not None:
                older_ranks = prior_24h_ranks(snapshots, source, key, now)
                if older_ranks and max(older_ranks) - current_rank >= RANK_SURGE_24H:
                    proof = "rank_surge_24h"

            if not proof:
                continue
            deltas.append(
                DeltaSignal(
                    source=source,
                    title=title,
                    url=str(row.get("url") or ""),
                    current_rank=current_rank,
                    previous_rank=previous_rank,
                    hot=str(row.get("hot") or ""),
                    proof=proof,
                    captured_at=now.isoformat(),
                    previous_captured_at=previous_at,
                    freshness_hours=round(previous_gap, 2),
                )
            )

    # Strongest ranks first; unknown rank last.
    deltas.sort(key=lambda item: (item.current_rank is None, item.current_rank or 999, item.source, item.title))
    return deltas


def render_report(
    now: datetime,
    results: list[ProbeResult],
    previous: dict | None,
    previous_gap: float | None,
    deltas: list[DeltaSignal],
    history_count: int,
) -> str:
    lines = [
        "# V0.5 中国平台热榜差分快照",
        "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}",
        "",
        "规则：热榜没有发布时间时，只有‘新上榜’或‘明显升温’这类可证明发生在24小时内的变化，才可作为热点新鲜度证据。首轮只建立基线，不输出正式差分信号。",
        "",
        f"- 历史快照数（含本轮）：{history_count}",
        f"- 可用上一快照：{'是' if previous else '否'}",
        f"- 与上一快照间隔：{previous_gap:.2f}小时" if previous_gap is not None else "- 与上一快照间隔：无",
        f"- 本轮旅行热榜差分：{len(deltas)}条",
        "",
        "## 来源状态",
        "",
    ]
    for result in results:
        lines.append(
            f"- {result.name}：{result.status}｜总榜{len(result.signals)}条｜旅行相关{sum(s.travel_match for s in result.signals)}条"
        )
        if result.error:
            lines.append(f"  - 诊断：{result.error}")

    lines.extend(["", "## 24小时内可证明的新上榜/升温", ""])
    if not deltas:
        lines.append("- 无；若这是首轮，属于正常的基线建立。")
    else:
        for item in deltas:
            rank = f"#{item.current_rank}" if item.current_rank is not None else "无排名"
            prev = f"#{item.previous_rank}" if item.previous_rank is not None else "未在上一榜"
            lines.append(
                f"- [{item.title}]({item.url})｜{item.source}｜{item.proof}｜{prev} → {rank}｜证明间隔{item.freshness_hours:g}小时"
            )
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        results = list(await asyncio.gather(
            probe_weibo(client, now),
            probe_toutiao(client, now),
            probe_baidu(client, now),
        ))

    history = load_history()
    snapshots = list(history.get("snapshots") or [])
    previous, previous_gap = latest_valid_snapshot(snapshots, now)
    current_sources = {result.key: travel_rows(result) for result in results}
    deltas = build_deltas(current_sources, snapshots, previous, previous_gap, now)

    cutoff = now - timedelta(hours=HISTORY_HOURS)
    snapshots = [
        snapshot for snapshot in snapshots
        if (parse_dt(snapshot.get("captured_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    snapshots.append(
        {
            "captured_at": now.isoformat(),
            "sources": current_sources,
            "status": {
                result.key: {
                    "name": result.name,
                    "status": result.status,
                    "http_status": result.http_status,
                    "error": result.error,
                }
                for result in results
            },
        }
    )
    history = {"version": 1, "strict_window_hours": 24, "snapshots": snapshots}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(CN_TZ).strftime("%Y-%m-%d-%H%M")
    json_path = REPORT_DIR / f"platform-trend-delta-{stamp}.json"
    md_path = REPORT_DIR / f"platform-trend-delta-{stamp}.md"
    payload = {
        "generated_at": now.isoformat(),
        "strict_window_hours": 24,
        "baseline_seeded": previous is None,
        "previous_gap_hours": round(previous_gap, 2) if previous_gap is not None else None,
        "sources": current_sources,
        "deltas": [asdict(item) for item in deltas],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(now, results, previous, previous_gap, deltas, len(snapshots)), encoding="utf-8")

    print("Platform trend snapshot completed")
    print(f"- baseline_seeded: {previous is None}")
    print(f"- previous_gap_hours: {round(previous_gap, 2) if previous_gap is not None else '-'}")
    for result in results:
        print(
            f"- {result.name}: {result.status}, total={len(result.signals)}, "
            f"travel={sum(s.travel_match for s in result.signals)}"
        )
    print(f"- delta_count: {len(deltas)}")
    print(f"- history_snapshots: {len(snapshots)}")
    print(f"State: {STATE_PATH}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
