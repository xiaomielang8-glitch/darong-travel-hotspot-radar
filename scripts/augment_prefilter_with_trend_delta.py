"""Augment the strict-24h travel prefilter with proven platform trend deltas.

No LLM is used. The input is produced by the rolling V0.5 hot-board snapshot job and
stored in the shared Actions cache. Only deltas with a <=6h comparison proof and a
fresh snapshot are allowed into the pre-candidate pool.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_travel_candidate_prefilter import Candidate, merge_similar, title_score

LATEST_DELTA_PATH = Path("data/platform-trend-state/latest-delta.json")
MAX_DELTA_AGE_HOURS = 6
MAX_POOL = 60
ALLOWED_PROOFS = {"new_entry", "rank_surge", "rank_surge_24h"}


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


def latest_prefilter() -> Path:
    files = sorted(Path("data/candidate-pools").glob("travel-prefilter-*.json"))
    if not files:
        raise SystemExit("No strict-24h prefilter JSON found")
    return files[-1]


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


def convert_delta(item: dict, generated_at: str) -> Candidate | None:
    title = str(item.get("title") or "").strip()
    proof = str(item.get("proof") or "")
    source_name = str(item.get("source") or "").strip()
    if not title or proof not in ALLOWED_PROOFS or not source_name:
        return None
    freshness = item.get("freshness_hours")
    try:
        freshness_hours = float(freshness)
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
        source_name=source_name,
        title=title,
        url=str(item.get("url") or ""),
        published_at=str(item.get("captured_at") or generated_at),
        category="platform-trend-delta",
        pre_score=max(5.0, base_score),
        reasons=reasons,
        evidence_source="platform_trend_delta",
        evidence_url=str(item.get("url") or ""),
        evidence_published_at=str(item.get("captured_at") or generated_at),
        trend_rank=current_rank,
        trend_hot=str(item.get("hot") or "") or None,
    )


def main() -> None:
    pool_path = latest_prefilter()
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    if pool.get("strict_window_hours") != 24:
        raise SystemExit("Input prefilter lost strict 24h gate")

    stats = dict(pool.get("stats") or {})
    stats["trend_delta_handoff_found"] = LATEST_DELTA_PATH.exists()
    stats["trend_delta_added"] = 0
    stats["trend_delta_diagnostic"] = ""

    if not LATEST_DELTA_PATH.exists():
        stats["trend_delta_diagnostic"] = "no cached trend delta handoff; continue without it"
        pool["stats"] = stats
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Trend delta augmentation skipped: no handoff file")
        return

    now = datetime.now(timezone.utc)
    data = json.loads(LATEST_DELTA_PATH.read_text(encoding="utf-8"))
    valid, diagnostic = valid_delta_payload(data, now)
    if not valid:
        stats["trend_delta_diagnostic"] = diagnostic
        pool["stats"] = stats
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Trend delta augmentation skipped: {diagnostic}")
        return

    converted = [
        candidate
        for item in data.get("deltas", [])
        if (candidate := convert_delta(item, str(data.get("generated_at") or ""))) is not None
    ]
    existing = [Candidate(**item) for item in pool.get("candidates", [])]
    merged = merge_similar([*existing, *converted])[:MAX_POOL]

    stats["trend_delta_input"] = len(data.get("deltas", []))
    stats["trend_delta_valid"] = len(converted)
    stats["trend_delta_added"] = max(0, len(merged) - len(existing))
    stats["trend_delta_generated_at"] = data.get("generated_at")
    stats["raw_before_merge_with_trend_delta"] = len(existing) + len(converted)
    stats["merged_pool_with_trend_delta"] = len(merged)

    pool["stats"] = stats
    pool["candidates"] = [asdict(item) for item in merged]
    pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    stamp = pool_path.stem.replace("travel-prefilter-", "")
    report = pool_path.with_name(f"travel-trend-delta-augment-{stamp}.md")
    lines = [
        "# V0.6 平台热榜差分24小时增强",
        "",
        "本层零LLM。只有V0.5最近快照证明的新上榜/明显升温，才可进入前置池；最终是否进入12—18条仍由ChatGPT判断。",
        "",
        f"- 差分输入：{len(data.get('deltas', []))}",
        f"- 通过严格校验：{len(converted)}",
        f"- 合并后净新增：{stats['trend_delta_added']}",
        f"- 差分快照时间：{data.get('generated_at')}",
        "",
        "## 本轮差分",
        "",
    ]
    if not converted:
        lines.append("- 无")
    for item in converted:
        lines.append(
            f"- {item.title}｜{item.source_name}｜当前#{item.trend_rank or '-'}｜"
            f"{'; '.join(item.reasons[:3])}"
        )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("Trend delta strict-24h augmentation completed")
    print(f"- trend_delta_input: {len(data.get('deltas', []))}")
    print(f"- trend_delta_valid: {len(converted)}")
    print(f"- trend_delta_added: {stats['trend_delta_added']}")
    print(f"Updated: {pool_path}")
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
