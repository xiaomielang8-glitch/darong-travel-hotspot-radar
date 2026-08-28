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
from difflib import SequenceMatcher
from pathlib import Path

import httpx
from probe_cn_hot_platforms import CN_TZ, ProbeResult, probe_baidu, probe_toutiao, probe_weibo

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

# Strong tourism objects. Weak words such as "旅游/旅行/游客/旅客" are deliberately
# handled separately so a generic social story cannot enter merely by containing them.
STRONG_TRAVEL_TERMS = (
    "景区", "景点", "门票", "民宿", "酒店", "博物馆", "美术馆", "古镇", "古城",
    "乐园", "动物园", "海洋馆", "海岛", "漂流", "露营", "徒步", "研学", "签证",
    "免签", "自驾", "夜游", "亲子游", "度假区", "索道", "游船", "旅游列车",
    "上海迪士尼", "香港迪士尼", "北京环球影城", "北京环球度假区",
)
WEAK_TRAVEL_TERMS = ("旅游", "旅行", "出游", "游客", "旅客", "文旅")
TRAVEL_CHANGE_TERMS = (
    "开放", "开馆", "开园", "新开", "上新", "上线", "恢复", "闭园", "关闭", "暂停",
    "限流", "预约", "免票", "优惠", "消费券", "预订", "搜索", "客流", "打卡", "玩法",
    "走红", "爆火", "升温", "涨价", "降价", "售罄", "首发", "首开", "首航",
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
INDUSTRY_DISCUSSION_TERMS = (
    "旅行社行业", "旅行社转型", "旅行社生存", "旅行社倒闭潮", "谁干掉了旅行社",
    "旅行社为什么", "传统旅行社", "旅游行业", "文旅产业", "文旅行业",
)
CONSUMER_IMPACT_TERMS = (
    "游客", "旅客", "退费", "退款", "跑路", "停业", "合同", "赔付", "投诉", "保证金",
)
CONCERT_TERMS = ("演唱会", "音乐节")
CONCERT_TRAVEL_CONTEXT = (
    "酒店", "机票", "民宿", "预订", "搜索", "客流", "旅游", "旅行", "出游", "目的地",
    "城市文旅", "带火", "爆满", "售罄", "交通", "高铁", "航班", "景区", "景点",
)
SOCIAL_CONTROVERSY_TERMS = (
    "衣不遮体", "着装争议", "穿着争议", "饭圈", "私生活", "塌房", "恋情", "绯闻",
)
INDIVIDUAL_HARM_TERMS = (
    "溺亡", "身亡", "死亡", "坠亡", "遇难", "猝死", "失联", "受伤", "坠落", "落水",
)
MASS_OR_OPERATIONAL_IMPACT_TERMS = (
    "多人", "多名", "大批", "大面积", "全线", "全部", "群体", "撤离", "疏散", "封闭",
    "关闭", "停运", "停航", "暂停", "预警", "警示", "限流", "管控",
)
SCENIC_COMPLAINT_TERMS = (
    "吐槽", "争议", "造丑", "难看", "审美", "坑人", "刺客", "排队", "拥堵", "宰客",
    "投诉", "忍无可忍",
)
SCENIC_DECISION_TERMS = (
    "游客", "门票", "排队", "预约", "停车", "价格", "收费", "关闭", "开放", "限流",
    "退票", "交通", "体验",
)
GENERIC_DESTINATION_PREFIXES = {
    "中国", "全国", "当地", "多个", "多地", "部分", "某地", "某个", "热门", "这些", "这个",
}
DESTINATION_SUFFIX_RE = re.compile(
    r"([\u4e00-\u9fff]{2,10})(?:景区|景点|公园|古镇|古城|乐园|博物馆|美术馆|动物园|"
    r"海洋馆|度假区|峡谷|山|湖|岛|谷)"
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
    corroborating_sources: list[str]
    cluster_titles: list[str]


def normalize(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text or "").lower()


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def has_specific_destination(title: str) -> bool:
    for match in DESTINATION_SUFFIX_RE.finditer(title):
        prefix = match.group(1)
        if prefix not in GENERIC_DESTINATION_PREFIXES and not any(
            prefix.endswith(generic) for generic in GENERIC_DESTINATION_PREFIXES
        ):
            return True
    return False


def strict_travel_match(title: str) -> bool:
    """Conservative title-only gate for hot-board snapshots.

    This layer intentionally prefers false negatives over social-news leakage. It is a
    signal radar, not the final daily candidate collector.
    """
    if not title or has_any(title, GENERIC_NOISE):
        return False

    # Travel-industry commentary is not customer-facing travel demand by default.
    if has_any(title, INDUSTRY_DISCUSSION_TERMS):
        if not has_any(title, CONSUMER_IMPACT_TERMS):
            return False
    if "旅行社" in title and not has_any(title, CONSUMER_IMPACT_TERMS):
        return False

    # Individual injury/death stories are excluded unless the title itself proves broad
    # visitor/operational impact such as closure, evacuation or multi-person disruption.
    if has_any(title, INDIVIDUAL_HARM_TERMS) and not has_any(title, MASS_OR_OPERATIONAL_IMPACT_TERMS):
        return False

    # Concert/music-festival celebrity or morality topics do not become travel signals
    # merely because an event word is present.
    if has_any(title, CONCERT_TERMS):
        if has_any(title, SOCIAL_CONTROVERSY_TERMS):
            return False
        return has_any(title, CONCERT_TRAVEL_CONTEXT)

    # Complaint/controversy signals must identify a concrete destination and contain a
    # visitor decision/experience consequence. Generic "景区造丑" discussion is rejected.
    if has_any(title, SCENIC_COMPLAINT_TERMS):
        return has_specific_destination(title) and has_any(title, SCENIC_DECISION_TERMS)

    if has_any(title, TRANSPORT_TERMS) and has_any(title, TRANSPORT_CHANGE_TERMS):
        return True
    if has_any(title, WEATHER_TERMS) and has_any(title, WEATHER_TRAVEL_CONTEXT):
        return True

    if has_any(title, STRONG_TRAVEL_TERMS):
        return True

    # Weak tourism words need an actual change/action signal or a concrete destination.
    if has_any(title, WEAK_TRAVEL_TERMS):
        return has_any(title, TRAVEL_CHANGE_TERMS) or has_specific_destination(title)

    return False


def semantic_guard_self_check() -> None:
    rejected = (
        "演唱会上的“衣不遮体”该治了",
        "谁干掉了旅行社",
        "景区“造丑运动”让游客忍无可忍",
        "中国女游客印尼溺亡 抢救细节曝光",
        "马来西亚游客受伤后送医",
    )
    accepted = (
        "西湖景区8月28日起临时关闭",
        "苏州博物馆新馆8月30日正式开放",
        "台风影响宁波景区关闭和航班调整",
        "演唱会带火杭州酒店预订",
    )
    bad = [title for title in rejected if strict_travel_match(title)]
    missed = [title for title in accepted if not strict_travel_match(title)]
    if bad or missed:
        raise RuntimeError(f"Semantic guard failed: leaked={bad}, missed={missed}")


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


def same_event(title_a: str, title_b: str) -> bool:
    a = normalize(title_a)
    b = normalize(title_b)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 8 and shorter in longer and len(shorter) / len(longer) >= 0.65:
        return True
    if min(len(a), len(b)) >= 10 and SequenceMatcher(None, a, b).ratio() >= 0.82:
        return True
    return False


def cluster_deltas(items: list[DeltaSignal]) -> list[DeltaSignal]:
    """Merge the same event across Weibo/Toutiao/Baidu into one signal."""
    clustered: list[DeltaSignal] = []
    for item in sorted(items, key=lambda x: (x.current_rank is None, x.current_rank or 999, x.source, x.title)):
        target = next((existing for existing in clustered if same_event(existing.title, item.title)), None)
        if target is None:
            clustered.append(item)
            continue

        for source in item.corroborating_sources:
            if source not in target.corroborating_sources:
                target.corroborating_sources.append(source)
        for title in item.cluster_titles:
            if title not in target.cluster_titles:
                target.cluster_titles.append(title)

        current_better = (
            item.current_rank is not None
            and (target.current_rank is None or item.current_rank < target.current_rank)
        )
        if current_better:
            target.source = item.source
            target.title = item.title
            target.url = item.url
            target.current_rank = item.current_rank
            target.previous_rank = item.previous_rank
            target.hot = item.hot
            target.proof = item.proof
            target.previous_captured_at = item.previous_captured_at
            target.freshness_hours = item.freshness_hours

    clustered.sort(key=lambda x: (x.current_rank is None, x.current_rank or 999, x.source, x.title))
    return clustered


def build_deltas(
    current: dict[str, list[dict]],
    snapshots: list[dict],
    previous: dict | None,
    gap: float | None,
    now: datetime,
) -> list[DeltaSignal]:
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
                    corroborating_sources=[source],
                    cluster_titles=[title],
                ))
    return cluster_deltas(out)


def render_report(
    now: datetime,
    results: list[ProbeResult],
    current: dict[str, list[dict]],
    previous: dict | None,
    gap: float | None,
    deltas: list[DeltaSignal],
    history_count: int,
) -> str:
    lines = [
        "# V0.5 中国平台热榜差分快照", "",
        f"生成时间：{now.astimezone(CN_TZ).isoformat(timespec='seconds')}", "",
        "规则：只有通过旅行语义硬过滤、且可由最近快照证明的新上榜或明显升温，才作为24小时热点新鲜度证据。首轮只建立基线。", "",
        f"- 历史快照数（含本轮）：{history_count}",
        f"- 可用上一快照：{'是' if previous else '否'}",
        f"- 与上一快照间隔：{gap:.2f}小时" if gap is not None else "- 与上一快照间隔：无",
        f"- 本轮严格旅行热榜差分（跨平台聚类后）：{len(deltas)}条", "", "## 来源状态", "",
    ]
    for result in results:
        lines.append(f"- {result.name}：{result.status}｜总榜{len(result.signals)}条｜严格旅行相关{len(current.get(result.key, []))}条")
        if result.error:
            lines.append(f"  - 诊断：{result.error}")
    lines.extend(["", "## 24小时内可证明的新上榜/升温", ""])
    if not deltas:
        lines.append("- 无；没有合格信号时宁可记0，不用泛社会新闻补数。")
    for item in deltas:
        rank = f"#{item.current_rank}" if item.current_rank is not None else "无排名"
        prev = f"#{item.previous_rank}" if item.previous_rank is not None else "未在上一榜"
        sources = "/".join(item.corroborating_sources)
        cluster_note = f"｜聚类{len(item.cluster_titles)}个标题" if len(item.cluster_titles) > 1 else ""
        lines.append(
            f"- [{item.title}]({item.url})｜{sources}｜{item.proof}｜{prev} → {rank}"
            f"｜证明间隔{item.freshness_hours:g}小时{cluster_note}"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    semantic_guard_self_check()
    now = datetime.now(timezone.utc)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        results = list(await asyncio.gather(probe_weibo(client, now), probe_toutiao(client, now), probe_baidu(client, now)))

    history = load_history()
    snapshots = list(history.get("snapshots") or [])
    previous, gap = latest_valid_snapshot(snapshots, now)
    current = {result.key: travel_rows(result) for result in results}
    deltas = build_deltas(current, snapshots, previous, gap, now)

    cutoff = now - timedelta(hours=HISTORY_HOURS)
    snapshots = [
        s for s in snapshots
        if (parse_dt(s.get("captured_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    snapshots.append({
        "captured_at": now.isoformat(),
        "sources": current,
        "status": {
            r.key: {
                "name": r.name,
                "status": r.status,
                "http_status": r.http_status,
                "error": r.error,
            }
            for r in results
        },
    })
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"version": 1, "strict_window_hours": 24, "snapshots": snapshots}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

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
    print("- semantic_guard: passed")
    print(f"- baseline_seeded: {previous is None}")
    print(f"- previous_gap_hours: {round(gap, 2) if gap is not None else '-'}")
    for result in results:
        print(f"- {result.name}: {result.status}, total={len(result.signals)}, strict_travel={len(current.get(result.key, []))}")
    print(f"- delta_count_clustered: {len(deltas)}")
    print(f"- history_snapshots: {len(snapshots)}")
    print(f"State: {STATE_PATH}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
