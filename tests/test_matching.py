"""核心匹配算法的测试，含对小规模实例的穷举交叉验证。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from app.db import explain_pair
from app.matching import Event, align_events

BASE = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def ev(eid: str, seconds: float, source: str = "src", summary: str = "") -> Event:
    return Event(
        id=eid,
        timestamp=BASE + timedelta(seconds=seconds),
        source_key=source,
        payload_summary=summary,
    )


def pairs_as_set(alignment):
    return {(p.a_id, p.b_id) for p in alignment.pairs}


def brute_force_optima(events_a, events_b, tol):
    """穷举所有注入映射，返回 (最大基数, 最小总差, 全部最优匹配集合)。"""
    a = sorted(events_a, key=lambda e: e.id)
    b = sorted(events_b, key=lambda e: e.id)
    tol_us = round(tol * 1_000_000)
    candidates = {}
    for i, ea in enumerate(a):
        candidates[i] = [
            j
            for j, eb in enumerate(b)
            if abs(round((ea.timestamp - eb.timestamp).total_seconds() * 1_000_000)) <= tol_us
        ]

    best_card = -1
    best_cost = float("inf")
    optima = set()

    def lexicographic_key(match):
        """按 A ID 顺序的配对序列（未匹配以 None 占位，排在任何 B 之后）。"""
        by_a = dict(match)
        return tuple(by_a.get(ea.id) for ea in a)

    best_key = None

    def search(i, used_b, chosen, cost):
        nonlocal best_card, best_cost, optima, best_key
        if i == len(a):
            card = len(chosen)
            key = frozenset(chosen)
            lk = lexicographic_key(chosen)
            if card > best_card or (card == best_card and cost < best_cost):
                best_card = card
                best_cost = cost
                optima = {key}
                best_key = lk
            elif card == best_card and cost == best_cost:
                optima.add(key)
                if lk < best_key:
                    best_key = lk
            return
        # 不匹配 a[i]
        search(i + 1, used_b, chosen, cost)
        for j in candidates[i]:
            if j not in used_b:
                d = abs(round((a[i].timestamp - b[j].timestamp).total_seconds() * 1_000_000))
                search(
                    i + 1,
                    used_b | {j},
                    chosen | {(a[i].id, b[j].id)},
                    cost + d,
                )

    search(0, frozenset(), frozenset(), 0)
    return best_card, best_cost / 1_000_000, optima, best_key


def test_basic_one_to_one_matching():
    a = [ev("a1", 0), ev("a2", 10), ev("a3", 300)]
    b = [ev("b1", 2), ev("b2", 9)]
    result = align_events(a, b, 5)

    assert pairs_as_set(result) == {("a1", "b1"), ("a2", "b2")}
    assert [e.id for e in result.unmatched_a] == ["a3"]
    assert [e.id for e in result.unmatched_b] == []
    assert result.candidate_edges == 2
    assert result.total_absolute_delta_seconds == pytest.approx(3.0)

    pair_by_id = {p.a_id: p for p in result.pairs}
    assert pair_by_id["a1"].delta_seconds == pytest.approx(2.0)
    assert pair_by_id["a2"].delta_seconds == pytest.approx(-1.0)
    assert "b1" in explain_pair(pair_by_id["a1"])
    assert "晚于" in explain_pair(pair_by_id["a1"])
    assert "早于" in explain_pair(pair_by_id["a2"])


def test_maximum_cardinality_beats_cheaper_partial():
    # a1 与 b1 几乎同时（差 0），a2 只有 b1 在窗口内（差 5）。
    # 贪心选最便宜会只配 a1-b1；最优解必须配两对（a1-b? 重新路由后 a2-b1, a1 无其他）
    a = [ev("a1", 0), ev("a2", 5)]
    b = [ev("b1", 0.5)]
    # 只有一个 B，故最大基数为 1，应选最便宜的 a1-b1
    result = align_events(a, b, 6)
    assert pairs_as_set(result) == {("a1", "b1")}
    assert [e.id for e in result.unmatched_a] == ["a2"]

    # 增加 b2：a1 可配 b1(0.5)/b2(6?) 令两对方案总差更小地强制成两对
    a = [ev("a1", 0), ev("a2", 5)]
    b = [ev("b1", 0.5), ev("b2", 6)]
    result = align_events(a, b, 6)
    # 两对方案 a1-b1(0.5)+a2-b2(1)=1.5；只配一对更贵的基数劣势
    assert len(result.pairs) == 2
    assert result.total_absolute_delta_seconds == pytest.approx(1.5)
    assert pairs_as_set(result) == {("a1", "b1"), ("a2", "b2")}


def test_min_total_cost_among_max_cardinality():
    # 容差 10：
    #   a1(0): b1(2) 差2, b2(8) 差8
    #   a2(5): b1(2) 差3, b2(8) 差3
    # 最大基数 2 的两种方案：交叉=8+3=11，不交叉=2+3=5 → 选不交叉
    a = [ev("a1", 0), ev("a2", 5)]
    b = [ev("b1", 2), ev("b2", 8)]
    result = align_events(a, b, 10)
    assert pairs_as_set(result) == {("a1", "b1"), ("a2", "b2")}
    assert result.total_absolute_delta_seconds == pytest.approx(5.0)


def test_tie_break_is_deterministic_and_id_stable():
    # 两种两配方案总差都是 10：
    #   a1(0)-b1(5)=5, a2(5)-b2(10)=5  → (a1,b1),(a2,b2)
    #   a1(0)-b2(10)=10, a2(5)-b1(5)=0 → (a1,b2),(a2,b1)
    a = [ev("a1", 0), ev("a2", 5)]
    b = [ev("b1", 5), ev("b2", 10)]
    result1 = align_events(a, b, 10)
    result2 = align_events(list(reversed(a)), list(reversed(b)), 10)

    expected = frozenset({("a1", "b1"), ("a2", "b2")})
    assert frozenset(pairs_as_set(result1)) == expected
    # 输入顺序打乱后结果必须完全一致（按 ID 决胜，与提交顺序无关）
    assert pairs_as_set(result2) == pairs_as_set(result1)
    assert result2.total_absolute_delta_seconds == result1.total_absolute_delta_seconds


def test_tie_break_prefers_smaller_b_for_higher_priority_a():
    # 两种最大基数方案的总差相同（µs 级精确相等）：
    #   S1: a0-b0(2.83)+a1-b3(3.01)+a2-b2(1.00)=6.84
    #   S2: a0-b0(2.83)+a1-b2(1.37)+a2-b3(2.64)=6.84
    # 按 A ID 字典序：a0 都配 b0；a1 配更小的 b2 更优 → S2
    a = [ev("a0", 0.0), ev("a1", -0.08), ev("a2", 0.29)]
    b = [ev("b0", 2.83), ev("b1", 4.82), ev("b2", 1.29), ev("b3", 2.93)]
    result = align_events(a, b, 100)
    assert pairs_as_set(result) == {("a0", "b0"), ("a1", "b2"), ("a2", "b3")}


def test_tolerance_boundary_is_inclusive():
    a = [ev("a1", 0)]
    b = [ev("b1", 5)]
    assert pairs_as_set(align_events(a, b, 5)) == {("a1", "b1")}
    assert pairs_as_set(align_events(a, b, 4.999)) == set()


def test_empty_sides_and_unmatched_preserved():
    result = align_events([], [], 5)
    assert result.pairs == ()
    assert result.unmatched_a == ()
    assert result.unmatched_b == ()

    a = [ev("a1", 0, "sa", "payload-a")]
    result = align_events(a, [], 5)
    assert result.pairs == ()
    assert len(result.unmatched_a) == 1
    assert result.unmatched_a[0].payload_summary == "payload-a"
    assert result.unmatched_a[0].source_key == "sa"


def test_negative_tolerance_rejected():
    with pytest.raises(ValueError):
        align_events([ev("a1", 0)], [ev("b1", 0)], -1)


def test_output_sorted_by_id():
    a = [ev("a9", 0), ev("a1", 1), ev("a5", 2)]
    b = [ev("b9", 0.1), ev("b1", 1.1), ev("b5", 2.1)]
    result = align_events(a, b, 1)
    ids = [(p.a_id, p.b_id) for p in result.pairs]
    assert ids == sorted(ids)
    assert ids == [("a1", "b1"), ("a5", "b5"), ("a9", "b9")]


@pytest.mark.parametrize("seed", range(300))
def test_matches_brute_force_optimum(seed):
    """随机小实例：MCMF 的基数与总费用必须等于穷举最优，且结果属于最优方案集合。"""
    import random

    rng = random.Random(seed)
    n_a = rng.randint(0, 4)
    n_b = rng.randint(0, 4)
    a = [ev(f"a{i}", rng.uniform(0, 12)) for i in range(n_a)]
    b = [ev(f"b{i}", rng.uniform(0, 12)) for i in range(n_b)]
    tol = rng.choice([0.0, 1.5, 3.0, 6.0, 100.0])

    result = align_events(a, b, tol)
    card, cost, optima, best_key = brute_force_optima(a, b, tol)

    assert len(result.pairs) == card
    assert result.total_absolute_delta_seconds == pytest.approx(cost, abs=1e-6)
    assert frozenset(pairs_as_set(result)) in optima

    # 平局决胜：结果的配对序列必须是全部最优方案中按 A ID 的字典序最小者
    got_key = tuple(dict(pairs_as_set(result)).get(e.id) for e in sorted(a, key=lambda e: e.id))
    assert got_key == best_key
    # 未匹配清单与匹配对互斥且完备
    matched_a = {p.a_id for p in result.pairs}
    matched_b = {p.b_id for p in result.pairs}
    assert matched_a.isdisjoint({e.id for e in result.unmatched_a})
    assert matched_b.isdisjoint({e.id for e in result.unmatched_b})
    assert len(result.pairs) + len(result.unmatched_a) == n_a
    assert len(result.pairs) + len(result.unmatched_b) == n_b
