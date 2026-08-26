"""Deterministically refine the strict-24h travel hotspot pre-candidate pool.

No LLM/API is used here. This layer removes SEO/evergreen articles and PR noise,
then clusters multiple reports about the same event so only one representative is
sent to ChatGPT for the final 12-18 candidate judgement.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

MAX_REFINED = 35
MIN_REFINED = 12

SEO_PATTERNS = (
    r"FAQ", r"全攻略", r"攻略", r"怎么玩", r"怎么预约", r"开放时间是什么",
    r"适合旅游吗", r"今天限流吗", r"暑假限流吗", r"最新消息\+", r"避坑指南",
    r"全解析", r"省钱方案", r"热门景点开放时间查询", r"开放时间景点",
    r"游客如何查询.*开放动态", r"门票怎么", r"需要预约吗", r"限流吗[？?]?$",
)

VAGUE_PROMO_PATTERNS = (
    r"夜游火热.*出圈.*文旅活力", r"文旅活力.*满格", r"解锁.*活力新玩法",
)

PR_PATTERNS = (
    r"行业协会", r"共探", r"共话", r"融合发展", r"产业链条", r"国企动态",
    r"活力新玩法", r"ChinaCool", r"文旅活力[“\"]?满格", r"优秀案例",
    r"品牌推介", r"招商推介", r"工作会议", r"调研行", r"启动仪式",
)

HIGH_EVENT_TERMS = (
    "闭园", "关闭", "暂停营业", "暂停开放", "恢复开放", "停航", "停运", "滞留",
    "受伤", "溺亡", "事故", "投诉", "宰客", "免费退改", "免门票", "免票", "免签",
    "新规", "调整", "爆火", "走红", "出圈", "爆满", "售罄", "预订涨", "预订量",
    "搜索量", "环比", "同比", "暴增", "首发", "首开", "首列", "首航", "被打",
    "起诉", "开庭", "被淹", "撤离游客", "临时关停", "免费开放",
)

TREND_TERMS = ("搜索量", "预订涨", "预订量", "环比", "同比", "暴增", "TOP10", "热搜", "爆火", "走红", "出圈")
CLOSURE_TERMS = ("闭园", "关闭", "暂停营业", "暂停开放", "临时关闭", "临时关停")
WEATHER_TERMS = ("台风", "防台", "暴雨", "降雨", "山洪", "风暴潮")

PLACE_HINTS = (
    "浙江", "福建", "福州", "三亚", "海口", "涠洲岛", "广西", "青岛", "南京", "上海",
    "兰州", "印尼", "印度尼西亚", "佛得角", "菲律宾", "潭柘寺", "永定河集", "双威",
    "万岁山", "华山", "伊犁", "茶卡盐湖", "玉龙雪山", "喀拉峻", "呼伦贝尔", "安徽",
    "滁州", "合肥", "黄山", "江苏", "苏州", "杭州", "舟山", "北京", "海南",
)

ENTITY_RE = re.compile(r"([\u4e00-\u9fffA-Za-z0-9“”·]{2,18}(?:景区|乐园|岛|寺|山|古镇|公园|机场|港|湾|城|雪山|盐湖|草原|度假区))")
ACTOR_RE = re.compile(r"演员([\u4e00-\u9fff]{2,4}?)(?=当|回应|在|称|做|扮|饰|成为|$)")
SOURCE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{1,50}$")


@dataclass
class Rejection:
    title: str
    source_name: str
    reason: str


def clean_title(title: str) -> str:
    return SOURCE_SUFFIX_RE.sub("", title or "").strip()


def normalized(title: str) -> str:
    title = clean_title(title)
    title = re.sub(r"[（(][^）)]{0,80}[）)]", "", title)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", title).lower()


def bigrams(title: str) -> set[str]:
    text = normalized(title)
    return {text[i:i + 2] for i in range(max(0, len(text) - 1))}


def jaccard(a: str, b: str) -> float:
    aa, bb = bigrams(a), bigrams(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def event_family(title: str) -> str:
    if any(x in title for x in WEATHER_TERMS) and any(
        x in title for x in ("闭园", "关闭", "停航", "停运", "滞留", "暂停", "撤离", "关停", "恢复开放", "被困")
    ):
        return "weather_disruption"
    if any(x in title for x in ("溺亡", "受伤", "事故", "被打", "触电", "被困", "死亡")):
        return "tourist_incident"
    if any(x in title for x in TREND_TERMS):
        return "demand_or_viral_trend"
    if any(x in title for x in ("免门票", "免票", "免签", "免费开放", "新规", "退改", "调价", "涨价", "降价")):
        return "policy_or_price"
    if any(x in title for x in ("闭园", "关闭", "暂停开放", "恢复开放", "临时关停")):
        return "closure_or_opening"
    return "other"


def entities(title: str) -> set[str]:
    result = {place for place in PLACE_HINTS if place in title}
    for match in ENTITY_RE.finditer(title):
        value = match.group(1).strip("“”")
        if 2 <= len(value) <= 18:
            result.add(value)
    return result


def rejection_reason(candidate: dict) -> str | None:
    title = candidate.get("title", "")
    source = candidate.get("source", "")
    score = float(candidate.get("pre_score", 0) or 0)

    # These sources already carry an independent freshness/trend proof.
    if source in {"trend_verified", "mafengwo", "bilibili"}:
        return None

    for pattern in SEO_PATTERNS:
        if re.search(pattern, title, re.I):
            return "SEO/常青攻略，不代表24小时内发生新事件"

    for pattern in VAGUE_PROMO_PATTERNS:
        if re.search(pattern, title, re.I) and not any(term in title for term in ("搜索量", "预订涨", "预订量", "环比", "同比", "暴增")):
            return "泛化营销标题，缺少具体地点/事件/量化热度"

    for pattern in PR_PATTERNS:
        if re.search(pattern, title, re.I) and not any(term in title for term in TREND_TERMS):
            return "宣传/泛文旅稿，缺少游客侧新变化"

    if not any(term in title for term in HIGH_EVENT_TERMS):
        # High pre-score alone cannot make a freshly-published evergreen article a hotspot.
        return "虽在24小时内发布，但标题无法证明24小时内有新事件/新热度"

    if score < 4.0:
        return "程序预分过低"
    return None


def same_event(a: dict, b: dict) -> bool:
    ta, tb = a.get("title", ""), b.get("title", "")
    fa, fb = event_family(ta), event_family(tb)
    ea, eb = entities(ta), entities(tb)
    shared_entities = ea & eb

    # Same place + closure + at least one explicit weather disruption is treated as one
    # weather-closure topic even when the other syndicated headline only names the storm.
    if fa != fb:
        families = {fa, fb}
        if families == {"weather_disruption", "closure_or_opening"} and shared_entities:
            both_closure = all(any(term in title for term in CLOSURE_TERMS) for title in (ta, tb))
            has_weather = any(term in (ta + tb) for term in WEATHER_TERMS)
            if both_closure and has_weather:
                return True
        return False

    # Same actor + NPC + same trend family is a strong specific anchor. This catches
    # syndicated headlines such as “演员陈明当NPC走红” vs “演员陈明回应景区NPC走红”
    # without lowering the generic similarity threshold for unrelated tourist stories.
    if "npc" in ta.lower() and "npc" in tb.lower():
        aa, ab = ACTOR_RE.search(ta), ACTOR_RE.search(tb)
        if aa and ab and aa.group(1) == ab.group(1):
            return True

    if shared_entities:
        return True

    jac = jaccard(ta, tb)
    seq = SequenceMatcher(None, normalized(ta), normalized(tb)).ratio()
    if jac >= 0.30 or seq >= 0.68:
        return True
    return False


def representative_score(candidate: dict) -> tuple:
    source = candidate.get("source", "")
    title = candidate.get("title", "")
    source_bonus = 3 if source == "trend_verified" else 2 if source == "bilibili" else 1 if source == "mafengwo" else 0
    specificity = sum(ch.isdigit() for ch in title) + min(len(title), 80) / 80
    return (source_bonus, float(candidate.get("pre_score", 0) or 0), specificity)


def cluster_candidates(candidates: list[dict]) -> list[dict]:
    n = len(candidates)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if same_event(candidates[i], candidates[j]):
                union(i, j)

    groups: dict[int, list[dict]] = defaultdict(list)
    for i, candidate in enumerate(candidates):
        groups[find(i)].append(candidate)

    refined: list[dict] = []
    for group in groups.values():
        ordered = sorted(group, key=representative_score, reverse=True)
        representative = dict(ordered[0])
        representative["event_family"] = event_family(representative.get("title", ""))
        representative["cluster_size"] = len(group)
        representative["cluster_sources"] = sorted({item.get("source_name", "") for item in group if item.get("source_name")})
        representative["cluster_titles"] = [item.get("title", "") for item in ordered[:8]]
        refined.append(representative)

    refined.sort(key=lambda item: representative_score(item), reverse=True)
    return refined[:MAX_REFINED]


def render(refined: list[dict], rejections: list[Rejection], stats: dict) -> str:
    lines = [
        "# V0.3-B 旅游热点严格24小时前置池｜程序提纯版",
        "",
        "本层不调用豆包或其他LLM。程序只负责24小时证明、去SEO/宣传噪音、同事件聚类；最终12—18条由ChatGPT按旅行社使用价值判断。",
        "",
        "## 统计",
        "",
        f"- 原始严格24小时前置池：{stats['input_count']}",
        f"- 规则淘汰：{stats['rejected_count']}",
        f"- 进入事件聚类：{stats['after_filter_count']}",
        f"- 同事件合并掉：{stats['cluster_merged_count']}",
        f"- 程序提纯后：{stats['refined_count']}",
        "",
        "## 交给ChatGPT判断的候选",
        "",
    ]
    for idx, item in enumerate(refined, 1):
        cluster = f"｜同事件{item.get('cluster_size', 1)}篇" if item.get("cluster_size", 1) > 1 else ""
        evidence = f"｜热榜证据:{item.get('evidence_source')}" if item.get("evidence_source") else ""
        lines.extend([
            f"{idx}. **{item.get('title')}**",
            f"   - 来源：{item.get('source_name')}｜类别：{item.get('category')}｜程序预分：{item.get('pre_score')}{cluster}{evidence}",
            f"   - 时间：{item.get('published_at')}",
            f"   - 链接：{item.get('url')}",
        ])
    lines.extend(["", "## 被程序淘汰的典型内容", ""])
    reason_counts = Counter(r.reason for r in rejections)
    for reason, count in reason_counts.most_common():
        lines.append(f"- {reason}：{count}条")
    lines.append("")
    for item in rejections[:20]:
        lines.append(f"- {item.title}｜{item.reason}")
    return "\n".join(lines) + "\n"


def main() -> None:
    pool_dir = Path("data/candidate-pools")
    files = sorted(pool_dir.glob("travel-prefilter-*.json"))
    if not files:
        raise SystemExit("No strict-24h prefilter JSON found")
    source_path = files[-1]
    data = json.loads(source_path.read_text(encoding="utf-8"))
    if data.get("strict_window_hours") != 24:
        raise SystemExit("Input pool is not strict-24h")

    candidates = data.get("candidates", [])
    kept: list[dict] = []
    rejections: list[Rejection] = []
    for candidate in candidates:
        reason = rejection_reason(candidate)
        if reason:
            rejections.append(Rejection(candidate.get("title", ""), candidate.get("source_name", ""), reason))
        else:
            kept.append(candidate)

    refined = cluster_candidates(kept)
    stats = {
        "input_count": len(candidates),
        "rejected_count": len(rejections),
        "after_filter_count": len(kept),
        "cluster_merged_count": len(kept) - len(refined),
        "refined_count": len(refined),
        "source_file": source_path.name,
    }

    if len(refined) < MIN_REFINED:
        raise SystemExit(f"Refined pool too small: {len(refined)} < {MIN_REFINED}")

    stamp = source_path.stem.replace("travel-prefilter-", "")
    json_path = pool_dir / f"travel-refined-{stamp}.json"
    md_path = pool_dir / f"travel-refined-{stamp}.md"
    json_path.write_text(json.dumps({"strict_window_hours": 24, "stats": stats, "candidates": refined, "rejections": [r.__dict__ for r in rejections]}, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render(refined, rejections, stats), encoding="utf-8")

    print("Travel candidate deterministic refinement completed")
    for key, value in stats.items():
        print(f"- {key}: {value}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()