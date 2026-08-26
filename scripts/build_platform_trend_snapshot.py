"""Rolling 24h trend snapshots for Chinese travel-relevant hot boards.

No LLM is used. Realtime boards have no trustworthy publish time, so a formal trend
signal requires a recent baseline and a newly entered or materially rising topic.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from probe_cn_hot_platforms import CN_TZ, ProbeResult, Signal, probe_baidu, probe_toutiao, probe_weibo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "Chrome/151.0.0.0 Safari/537.36"
)
STATE_PATH = Path("data/platform-trend-state/history.json")
REPORT_DIR = Path("data/trend-snapshots")
HISTORY_HOURS = 27
MAX_PROOF_GAP_HOURS = 6
RANK_SURGE_STEP = 8
RANK_SURGE_24H = 12

DIRECT_TRAVEL_TERMS = (
    "旅游", "旅行", "出游", "游客", "旅客", "景区", "景点", "门票", "民宿", "酒店",
    "演唱会", "音乐节", "博物馆", "古镇", "乐园", "海岛", "漂流", "露营", "徒步",
    "研学", "签证", "免签", "自驾", "夜游", "亲子游", "度假区", "索道", "游船",
    "上海迪士尼", "香港迪士尼", "北京环球影城", "北京环球度假区",
)
TRANSPORT_TERMS = ("机场", "航班", "高铁", "列车", "火车", "铁路", "客运", "轮渡", "港口")
TRANSPORT_CHANGE_TERMS = (
    "停航", "停运", "取消", "延误", "恢复", "调整", "新开", "首航", "首开", "售罄",
    "退票", "改签", "限流", "关闭", "暂停",
)
WEATHER_TERMS = ("台风", "暴雨", "山洪", "高温", "暴雪", "雷暴")
WEATHER_TRAVEL_CONTEXT = (
    "景区", "景点", "游客", "旅游", "旅行", "航班", "机场", "高铁", "列车", "停航",
    "停运", "闭园", "关闭", "海岛", "游船", "轮渡",
)
GENERIC_NOISE = (
    "防护攻略", "如何应对", "请收好", "安全提示", "气象站", "次生灾害", "铁路职工",
    "列车员", "乘警", "班主任", "科普", "教程",
)


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


def strict_travel_match(title: str) -> bool:
    if any(term in title for term in GENERIC_NOISE):
        return False
    if any(term in title for term in DIRECT_TRAVEL_TERMS):
        return True
    if any(term in title for term in TRANSPORT_TERMS) and any(term in title for term in TRANSPORT_CHANGE_TERMS):
        return True
    if any(term in title for term in WEATHER_TERMS) and any(term in title for term in WEATHER_TRAVEL_CONTEXT):
        return True
    return False


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
    return data if isinstance(data, dict) and isinstance(data.get("snapshots"), list) else {"version": 1, "snapshots": []}


def travel_rows(result: ProbeResult) -> list[dict]:
    rows = []
    for signal in result.signals:
        if not strict_travel_match(signal.title):
            continue
        rows.append({
            "title": signal.title,
            "url": signal.url,
            "rank": signal.rank,
            "hot": signal.hot,
            "travel_match": True,
        })
    return rows


def latest_valid_snapshot(snapshots: list[dict], now: datetime) -> tuple[dict | None, float | None]:
    for snapshot in reversed(snapshots):
        captured = parse_dt(snapshot.get("captured_at"))
        if captured is None or captured >= now:
            continue
        gap = (now - captured).total_seconds() / 3600
        return (snapshot, gap) if gap <= MAX_PROOF_GAP_HOURS else (None, gap)
    return None, None


def rank_map(rows: list[dict]) -> dict[str, dict]:
    return {normalize(str(row.get("title") or "")): row for row in rows if normalize(str(row.get("title") or ""))}


def prior_24h_ranks(snapshots: list[dict], source: str, key: str, now: datetime) -> list[int]:
    cutoff = now - timedelta(hours=24)
    ranks = []
    for snapshot in snapshots:
        captured = parse_dt(snapshot.get("captured_at"))
        if captured is None or captured < cutoff or captured >= now:
            continue
        for row in ((snapshot.get("sources") or {}).get(source) or []):
            if normalize(str(row.get("title") or "")) == key and isinstance(row.get("rank"), int):
                ranks.append(row["rank"])
    return ranks


def build_deltas(current: dict[str, list[dict]], snapshots: list[dict], previous: dict | None, gap: float | None, now: datetime) -> list[DeltaSignal]:
    if previous is None or gap is None or gap > MAX_PROOF_GAP_HOURS:
        return []
    out = []
    previous_sources = previous.get("sources") or {}
    previous_at = str(previous.get("captured_at") or "")
    for source, rows in current.items():
        prev_map = rank_map(previous_sources.get(source) or [])
        for row in rows:
            title = str(row.get("title") or "")
            key = normalize(title)
            current_rank = row.get("rank") if isinstance(row.get("rank"), int) else None
            prev = prev_map.get(key)
            previous_rank = prev.get("rank") if prev and isinstance(prev.get("rank"), int) else None
            proof = ""
            if prev is None:
                proof = "new_entry"
            elif current_rank is not None and previous_rank is not None and previous_rank - current_rank >= RANK_SURGE_STEP:
                proof = "rank_surge"
            elif current_rank is not None:
                older = prior_24h_ranks(snapshots, source, key, now)
                if older and max(older) - current_rank >= RANK_SURGE_24H:
                    proof = "rank_surge_24h"
            if proof:
                out.append(DeltaSignal(
                    source=source,
                    title=title,
                    url=str(row.get("url") or ""),
                    current_rank=current_rank,
                    previous_rank=previous_rank,
                    hot=str(row.get("hot") or ""),
                    proof=proof,
                    captured_at=now.isoformat(),
                    previous_captured_at=previous_at,
                    freshness_hours=round(gap, 2),
                ))
    out.sort(key=lambda item: (item.current_rank is None, item.current_rank or 999, item.source, item.title))
    return out


def render_report(now: datetime, results: list[ProbeResult], current: dict[str, list[dict]], previous: dict | None, gap: float | None, deltas: list[DeltaSignal], history_count: int) -> str:
    lines = [
        "# V0.5 中国平台热榜差分快照", "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}", "",
        "规则：只有可由最近快照证明的新上榜或明显升温，才作为24小时热点新鲜度证据。首轮只建立基线。", "",
        f"- 历史快照数（含本轮）：{history_count}",
        f"- 可用上一快照：{'是' if previous else '否'}",
        f"- 与上一快照间隔：{gap:.2f}小时" if gap is not None else "- 与上一快照间隔：无",
        f"- 本轮严格旅行热榜差分：{len(deltas)}条", "", "## 来源状态", "",
    ]
    for result in results:
        lines.append(f"- {result.name}：{result.status}｜总榜{len(result.signals)}条｜严格旅行相关{len(current.get(result.key, []))}条")
        if result.error:
            lines.append(f"  - 诊断：{result.error}")
    lines.extend(["", "## 24小时内可证明的新上榜/升温", ""])
    if not deltas:
        lines.append("- 无；若这是首轮，属于正常的基线建立。")
    for item in deltas:
        rank = f"#{item.current_rank}" if item.current_rank is not None else "无排名"
        prev = f"#{item.previous_rank}" if item.previous_rank is not None else "未在上一榜"
        lines.append(f"- [{item.title}]({item.url})｜{item.source}｜{item.proof}｜{prev} → {rank}｜证明间隔{item.freshness_hours:g}小时")
    return "\n".join(lines) + "\n"


async def main() -> None:
    now = datetime.now(timezone.utc)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5", "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        results = list(await asyncio.gather(probe_weibo(client, now), probe_toutiao(client, now), probe_baidu(client, now)))

    history = load_history()
    snapshots = list(history.get("snapshots") or [])
    previous, gap = latest_valid_snapshot(snapshots, now)
    current = {result.key: travel_rows(result) for result in results}
    deltas = build_deltas(current, snapshots, previous, gap, now)

    cutoff = now - timedelta(hours=HISTORY_HOURS)
    snapshots = [s for s in snapshots if (parse_dt(s.get("captured_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    snapshots.append({
        "captured_at": now.isoformat(),
        "sources": current,
        "status": {r.key: {"name": r.name, "status": r.status, "http_status": r.http_status, "error": r.error} for r in results},
    })
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"version": 1, "strict_window_hours": 24, "snapshots": snapshots}, ensure_ascii=False, indent=2), encoding="utf-8")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(CN_TZ).strftime("%Y-%m-%d-%H%M")
    json_path = REPORT_DIR / f"platform-trend-delta-{stamp}.json"
    md_path = REPORT_DIR / f"platform-trend-delta-{stamp}.md"
    payload = {
        "generated_at": now.isoformat(),
        "strict_window_hours": 24,
        "baseline_seeded": previous is None,
        "previous_gap_hours": round(gap, 2) if gap is not None else None,
        "sources": current,
        "deltas": [asdict(item) for item in deltas],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(now, results, current, previous, gap, deltas, len(snapshots)), encoding="utf-8")

    print("Platform trend snapshot completed")
    print(f"- baseline_seeded: {previous is None}")
    print(f"- previous_gap_hours: {round(gap, 2) if gap is not None else '-'}")
    for result in results:
        print(f"- {result.name}: {result.status}, total={len(result.signals)}, strict_travel={len(current.get(result.key, []))}")
    print(f"- delta_count: {len(deltas)}")
    print(f"- history_snapshots: {len(snapshots)}")
    print(f"State: {STATE_PATH}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
