"""Augment the strict-24h travel prefilter with proven platform trend deltas.

No LLM is used. V0.4 restores the rolling V0.5 Actions cache and derives the latest
new-entry/rank-surge deltas from the cached snapshot history. Only a fresh <=6h
comparison proof with clear traveller/business value can enter the pre-candidate pool.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from build_platform_trend_snapshot import build_deltas, parse_dt
from build_travel_candidate_prefilter import Candidate, merge_similar, normalize_title, title_score

HISTORY_PATH = Path("data/platform-trend-state/history.json")
MAX_DELTA_AGE_HOURS = 6
MAX_POOL = 60
ALLOWED_PROOFS = {"new_entry", "rank_surge", "rank_surge_24h"}

SOURCE_LABELS = {
    "baidu_realtime": "百度实时热搜",
    "weibo_realtime": "微博实时热搜",
    "toutiao_hot": "今日头条热榜",
}

# “游客/文旅/旅游”本身只是弱语义，不能把泛社会趣闻直接送进旅行热点池。
# 热榜差分还必须同时包含游客决策、旅行场景、目的地设施或强事件信号。
BUSINESS_RELEVANCE_TERMS = (
    "景区", "景点", "打卡点", "门票", "预约", "限流", "闭园", "开园", "恢复开放", "暂停开放",
    "民宿", "酒店", "自驾", "夜游", "亲子游", "避暑", "海边", "海岛", "古镇", "博物馆",
    "乐园", "度假区", "漂流", "露营", "徒步", "研学", "索道", "游船", "演唱会", "音乐节",
    "马拉松", "赛事", "机场", "航班", "高铁", "列车", "火车", "签证", "免签", "停航", "停运",
    "被困", "滞留", "受伤", "死亡", "溺亡", "事故", "宰客", "投诉", "退改", "退票", "改签",
    "涨价", "降价", "免费", "爆火", "走红", "出圈", "爆满", "售罄", "排队", "拥堵", "预订",
    "搜索量", "客流", "首开", "首航", "首列", "新开",
)


def latest_prefilter() -> Path:
    files = sorted(Path("data/candidate-pools").glob("travel-prefilter-*.json"))
    if not files:
        raise SystemExit("No strict-24h prefilter JSON found")
    return files[-1]


def derive_latest_delta_payload() -> tuple[dict | None, str]:
    if not HISTORY_PATH.exists():
        return None, "no V0.5 rolling history cache"
    try:
        history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"invalid trend history: {type(exc).__name__}"
    if history.get("strict_window_hours") != 24:
        return None, "trend history lost strict 24h rule"
    snapshots = list(history.get("snapshots") or [])
    if len(snapshots) < 2:
        return None, "trend history has fewer than two snapshots"

    current_snapshot = snapshots[-1]
    previous = snapshots[-2]
    current_at = parse_dt(current_snapshot.get("captured_at"))
    previous_at = parse_dt(previous.get("captured_at"))
    if current_at is None or previous_at is None or current_at <= previous_at:
        return None, "trend history timestamps invalid"
    gap = (current_at - previous_at).total_seconds() / 3600
    if gap > MAX_DELTA_AGE_HOURS:
        return None, f"trend comparison gap too large: {gap:.2f}h"

    current = current_snapshot.get("sources") or {}
    deltas = build_deltas(current, snapshots[:-1], previous, gap, current_at)
    return {
        "generated_at": current_at.isoformat(),
        "strict_window_hours": 24,
        "previous_gap_hours": round(gap, 2),
        "deltas": [asdict(item) for item in deltas],
    }, ""


def valid_delta_payload(data: dict, now: datetime) -> tuple[bool, str]:
    if data.get("strict_window_hours") != 24:
        return False, "trend delta lost strict 24h rule"
    generated = parse_dt(data.get("generated_at"))
    if generated is None:
        return False, "trend delta missing generated_at"
    age_hours = (now - generated).total_seconds() / 3600
    if age_hours < -0.2 or age_hours > MAX_DELTA_AGE_HOURS:
        return False, f"trend delta snapshot too old/new: {age_hours:.2f}h"
    gap = data.get("previous_gap_hours")
    if gap is None or float(gap) > MAX_DELTA_AGE_HOURS:
        return False, f"trend delta lacks <=6h comparison proof: {gap}"
    return True, ""


def business_relevant(title: str) -> bool:
    return any(term in title for term in BUSINESS_RELEVANCE_TERMS)


def convert_delta(item: dict, generated_at: str) -> Candidate | None:
    title = str(item.get("title") or "").strip()
    proof = str(item.get("proof") or "")
    source_key = str(item.get("source") or "").strip()
    if not title or proof not in ALLOWED_PROOFS or not source_key:
        return None
    if not business_relevant(title):
        return None
    try:
        freshness_hours = float(item.get("freshness_hours"))
    except (TypeError, ValueError):
        return None
    if freshness_hours < 0 or freshness_hours > MAX_DELTA_AGE_HOURS:
        return None

    base_score, score_reasons = title_score(title)
    current_rank = item.get("current_rank") if isinstance(item.get("current_rank"), int) else None
    previous_rank = item.get("previous_rank") if isinstance(item.get("previous_rank"), int) else None
    proof_label = {
        "new_entry": "热榜新上榜",
        "rank_surge": "热榜单轮明显上升",
        "rank_surge_24h": "热榜24小时累计明显上升",
    }[proof]
    reasons = [
        "平台热榜差分已证明24小时内升温",
        proof_label,
        f"证明间隔:{freshness_hours:g}小时",
        *score_reasons,
    ]
    if current_rank is not None:
        reasons.append(f"当前排名:#{current_rank}")
    if previous_rank is not None:
        reasons.append(f"上一排名:#{previous_rank}")

    return Candidate(
        source="trend_verified",
        source_name=SOURCE_LABELS.get(source_key, source_key),
        title=title,
        url=str(item.get("url") or ""),
        published_at=str(item.get("captured_at") or generated_at),
        category="平台热榜差分",
        pre_score=max(5.0, base_score),
        reasons=reasons,
        evidence_source="platform_trend_delta",
        evidence_url=str(item.get("url") or ""),
        evidence_published_at=str(item.get("captured_at") or generated_at),
        trend_rank=current_rank,
        trend_hot=str(item.get("hot") or "") or None,
    )


def enrich_exact_existing(existing: list[Candidate], converted: list[Candidate]) -> tuple[list[Candidate], list[Candidate], int]:
    """Attach trend proof to an already collected exact topic instead of losing it in dedup."""
    by_title = {normalize_title(item.title): item for item in existing}
    new_items: list[Candidate] = []
    enriched = 0
    for delta in converted:
        match = by_title.get(normalize_title(delta.title))
        if match is None:
            new_items.append(delta)
            continue
        enriched += 1
        for reason in delta.reasons:
            if reason not in match.reasons:
                match.reasons.append(reason)
        if delta.trend_rank is not None:
            match.trend_rank = delta.trend_rank
        if delta.trend_hot:
            match.trend_hot = delta.trend_hot
        # Keep independent news evidence when already present; otherwise use the trend proof.
        if not match.evidence_source:
            match.evidence_source = delta.evidence_source
            match.evidence_url = delta.evidence_url
            match.evidence_published_at = delta.evidence_published_at
    return existing, new_items, enriched


def main() -> None:
    pool_path = latest_prefilter()
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    if pool.get("strict_window_hours") != 24:
        raise SystemExit("Input prefilter lost strict 24h gate")

    stats = dict(pool.get("stats") or {})
    stats["trend_delta_handoff_found"] = HISTORY_PATH.exists()
    stats["trend_delta_added"] = 0
    stats["trend_delta_diagnostic"] = ""

    data, diagnostic = derive_latest_delta_payload()
    if data is None:
        stats["trend_delta_diagnostic"] = diagnostic
        pool["stats"] = stats
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Trend delta augmentation skipped: {diagnostic}")
        return

    now = datetime.now(timezone.utc)
    valid, diagnostic = valid_delta_payload(data, now)
    if not valid:
        stats["trend_delta_diagnostic"] = diagnostic
        pool["stats"] = stats
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Trend delta augmentation skipped: {diagnostic}")
        return

    raw_deltas = list(data.get("deltas", []))
    converted = [
        candidate
        for item in raw_deltas
        if (candidate := convert_delta(item, str(data.get("generated_at") or ""))) is not None
    ]
    rejected = [item for item in raw_deltas if not business_relevant(str(item.get("title") or ""))]

    existing = [Candidate(**item) for item in pool.get("candidates", [])]
    existing, new_delta_items, enriched_count = enrich_exact_existing(existing, converted)
    merged = merge_similar([*existing, *new_delta_items])[:MAX_POOL]

    stats["trend_delta_input"] = len(raw_deltas)
    stats["trend_delta_valid"] = len(converted)
    stats["trend_delta_rejected_weak_relevance"] = len(rejected)
    stats["trend_delta_exact_existing_enriched"] = enriched_count
    stats["trend_delta_added"] = max(0, len(merged) - len(existing))
    stats["trend_delta_generated_at"] = data.get("generated_at")
    stats["raw_before_merge_with_trend_delta"] = len(existing) + len(new_delta_items)
    stats["merged_pool_with_trend_delta"] = len(merged)

    pool["stats"] = stats
    pool["candidates"] = [asdict(item) for item in merged]
    pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = pool_path.stem.replace("travel-prefilter-", "")
    report = pool_path.with_name(f"travel-trend-delta-augment-{stamp}.md")
    lines = [
        "# V0.6 平台热榜差分24小时增强",
        "",
        "本层零LLM。只有V0.5最近快照证明的新上榜/明显升温，且具备明确游客决策/旅行场景价值，才可进入前置池；最终是否进入12—18条仍由ChatGPT判断。",
        "",
        f"- 差分输入：{len(raw_deltas)}",
        f"- 旅行业务相关校验通过：{len(converted)}",
        f"- 弱旅行语义淘汰：{len(rejected)}",
        f"- 已存在主题补充热榜证明：{enriched_count}",
        f"- 合并后净新增：{stats['trend_delta_added']}",
        f"- 差分快照时间：{data.get('generated_at')}",
        "",
        "## 通过的差分",
        "",
    ]
    if not converted:
        lines.append("- 无")
    for item in converted:
        lines.append(
            f"- {item.title}｜{item.source_name}｜当前#{item.trend_rank or '-'}｜"
            f"{'; '.join(item.reasons[:3])}"
        )
    lines.extend(["", "## 因只有弱旅行语义而淘汰", ""])
    if not rejected:
        lines.append("- 无")
    for item in rejected:
        lines.append(f"- {item.get('title')}｜{item.get('source')}｜{item.get('proof')}")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Trend delta strict-24h augmentation completed")
    print(f"- trend_delta_input: {len(raw_deltas)}")
    print(f"- trend_delta_valid: {len(converted)}")
    print(f"- trend_delta_rejected_weak_relevance: {len(rejected)}")
    print(f"- trend_delta_exact_existing_enriched: {enriched_count}")
    print(f"- trend_delta_added: {stats['trend_delta_added']}")
    print(f"Updated: {pool_path}")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
