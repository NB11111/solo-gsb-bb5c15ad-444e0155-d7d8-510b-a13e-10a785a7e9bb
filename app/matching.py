"""跨来源事件对齐的核心匹配算法。

目标
----
给定两组带时间戳的事件（A 侧与 B 侧），在容差窗口 ``tolerance_seconds`` 内
建立候选边，并选出一组**一对一**的匹配，优化目标按优先级为：

1. **匹配数量最多**（容差允许范围内的最大基数）；
2. 基数相同的方案中，**总绝对时间差最小**；
3. 仍不分胜负时，**按事件 ID 稳定决胜**——与输入顺序无关，相同输入永远
   得到相同结果。

决胜的精确定义
--------------
把匹配按 A 的 ID 升序写成配对序列（未匹配的 A 记为虚拟列）。在所有满足
1、2 的方案里取该序列字典序最小者：同一 A 位置上，与 ID 越小的 B 配对越
优先，未匹配排在所有真实 B 之后。

两阶段求解（矩形指派）
----------------------
构造 |A| 行 × (|B| + |A|) 列的费用矩阵：

* 列 [0, |B|) 为真实 B（按 ID 排序），每个至多被一行占用；
* 列 [|B|+i] 为行 i（A 按 ID 排序）**私有**的"空置列"，表示该 A 未匹配。

每行恰好指派一列；行落到真实候选格即为匹配，落到自己的空置列即为未匹配。

主费用（整数微秒）：真实候选格 = |Δt|；自己的空置列 = P1；非候选真实格 =
P2；他人的空置列 = BIG，且

    P1 = min(|A|,|B|)·tol + 1（大于任何真实总差，故先最大化匹配基数）
    P2 = P1 + 1               （非法配对比自己空置更贵）
    BIG = P2·|A| + 1          （结构性禁格）

* **阶段一**：矩形 O(|A|²·(|A|+|B|)) 匈牙利求得最优指派，并取行/列对偶
  u、v。由互补松弛，reduced cost ``c - u - v == 0`` 的紧边恰好是"可能出
  现在某个最优指派中"的边；全部最优方案即紧边子图上覆盖所有行的匹配。
* **阶段二**：在紧边子图上按 A 的 ID 顺序逐行确定配对：从列号最小的选项
  （真实 B 按 ID 升序，私有空置列在最后）起，用一条残量增广路检验更小
  的列是否可在不扰动已确定行的前提下取得，可行即定案。由此得到字典序
  最小的最优方案，总计 O(|A|·(|A|+|B|))。

时间差统一换算为整数微秒（``datetime`` 的内部分辨率），杜绝浮点相等性
误差，保证"平局"被精确识别。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Sequence, Set, Tuple

_US = timedelta(microseconds=1)


def _delta_microseconds(ta: datetime, tb: datetime) -> int:
    """两个时间点之差的整数微秒数（ta - tb，与 timedelta 整除语义一致）。"""
    return (ta - tb) // _US


@dataclass(frozen=True)
class Event:
    """参与对齐的事件。排序与决胜均使用 ``id``。"""

    id: str
    timestamp: datetime
    source_key: str
    payload_summary: str


@dataclass(frozen=True)
class Pair:
    """一对匹配结果。

    ``delta_seconds`` 为带符号时间差（B - A，秒），负数表示 B 早于 A；
    ``absolute_delta_seconds`` 为绝对时间差。
    """

    a_id: str
    b_id: str
    a_source_key: str
    b_source_key: str
    a_timestamp: datetime
    b_timestamp: datetime
    delta_seconds: float
    absolute_delta_seconds: float
    a_payload_summary: str
    b_payload_summary: str


@dataclass(frozen=True)
class Alignment:
    pairs: Tuple[Pair, ...]
    unmatched_a: Tuple[Event, ...]
    unmatched_b: Tuple[Event, ...]
    candidate_edges: int
    tolerance_seconds: float
    total_absolute_delta_seconds: float


# ---------------------------------------------------------------------------
# 阶段一：矩形匈牙利（e-maxx 匈牙利的 n 行 × m 列版），返回行/列对偶
# ---------------------------------------------------------------------------


def hungarian_rect(cost: List[List[int]]) -> Tuple[List[int], List[int], List[int]]:
    """矩形最小权指派（n 行 × m 列，要求存在覆盖全部行的匹配）。

    返回 ``(col_of_row, u, v)``：``col_of_row`` 长度 n，为每行匹配的列；
    ``u``/``v`` 为长度 n/m 的行势/列势，最优边满足 ``c == u + v``。
    """
    n = len(cost)
    m = len(cost[0]) if n else 0
    if n == 0:
        return [], [], [0] * m

    INF = 10**30
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)  # p[j]：占用列 j 的行（1 起；0 = 空闲）
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)

        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = 0
            ui0 = u[i0]
            crow = cost[i0 - 1]
            for j in range(1, m + 1):
                if not used[j]:
                    cur = crow[j - 1] - ui0 - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j

            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta

            j0 = j1
            if p[j0] == 0:
                break

        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    col_of_row = [-1] * n
    for j in range(1, m + 1):
        if p[j] != 0:
            col_of_row[p[j] - 1] = j - 1
    return col_of_row, u[1:], v[1:]


# ---------------------------------------------------------------------------
# 阶段二：紧边子图上的字典序最小覆盖匹配（残量增广路）
# ---------------------------------------------------------------------------


def lexicographic_min_matching(
    n_rows: int,
    n_cols: int,
    adj: List[List[int]],
    initial_col_of_row: List[int],
) -> List[int]:
    """在紧边子图上取字典序最小的、覆盖所有行的匹配。

    处理行 r（按行号升序）时它已匹配着某列 ``cur``。对每个更小的候选列
    ``c``，在有向残量图中找一条从 c 的占用行出发、终止于 ``cur`` 的交替路：

        行 x → 列 d  当 d ∈ adj[x] 且 d 不是已确定列；列 d → 占用它的行。

    找到则沿路翻转：r 取得 c，链上每行取得下一列，末端行取得 cur，匹配仍
    覆盖全部行且已确定行/列不变。从最小的 c 起逐个尝试，第一个可行即采用，
    随后把 r 与其列确定下来。逐行如此，所得即字典序最小者。
    """
    col_of_row = list(initial_col_of_row)
    row_of_col = [-1] * n_cols
    for r, c in enumerate(col_of_row):
        row_of_col[c] = r

    adj_sets = [set(a) for a in adj]
    committed_rows: Set[int] = set()
    committed_cols: Set[int] = set()

    def find_path(start_row: int, target_col: int):
        """BFS：start_row 到 target_col 的行级交替路。

        par_row[x]：到达行 x 前所经过的列（-1 表示起点行）；
        par_col[d]：争取到列 d 的行。target_col 可达时返回二者。
        """
        par_row = {start_row: -1}
        par_col: dict = {}
        q = deque([start_row])
        while q:
            x = q.popleft()
            for d in adj_sets[x]:
                if d in committed_cols or d in par_col:
                    continue
                par_col[d] = x
                if d == target_col:
                    return par_row, par_col
                occ = row_of_col[d]
                if (
                    occ != -1
                    and occ not in committed_rows
                    and occ not in par_row
                ):
                    par_row[occ] = d
                    q.append(occ)
        return None

    for r in range(n_rows):
        cur = col_of_row[r]
        for c in sorted(adj_sets[r]):
            if c >= cur:
                break  # 候选按列号升序；不小于当前列则没有改进
            if c in committed_cols:
                continue
            owner = row_of_col[c]
            if owner in committed_rows:
                continue

            found = find_path(owner, cur)
            if found is None:
                continue
            par_row, par_col = found

            # 从 target(cur) 反向重建 (行, 它争取到的列) 序列
            flips: List[Tuple[int, int]] = []
            d = cur
            while True:
                x = par_col[d]
                flips.append((x, d))
                entered = par_row[x]
                if entered == -1:  # 回溯到起点行 owner
                    break
                d = entered
            flips.reverse()  # owner 先行动

            # 翻转：r 释放 cur、取得 c；链上依次换列，末端取得 cur
            row_of_col[cur] = -1
            col_of_row[r] = c
            row_of_col[c] = r
            for x, nd in flips:
                old = col_of_row[x]
                if old != -1 and row_of_col[old] == x:
                    row_of_col[old] = -1
                col_of_row[x] = nd
                row_of_col[nd] = x
            cur = c

        committed_rows.add(r)
        committed_cols.add(col_of_row[r])

    return col_of_row


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def align_events(
    events_a: Sequence[Event],
    events_b: Sequence[Event],
    tolerance_seconds: float,
) -> Alignment:
    """对两组事件执行容差窗口内的一对一最小时间差对齐（ID 稳定决胜）。

    容差按微秒四舍五入（``datetime`` 的最大分辨率即微秒）。
    """
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds 不能为负数")

    tol_us = int(round(tolerance_seconds * 1_000_000))

    a_sorted = sorted(events_a, key=lambda e: e.id)
    b_sorted = sorted(events_b, key=lambda e: e.id)
    na, nb = len(a_sorted), len(b_sorted)
    m = nb + na  # 列数：真实 B + 每行一个私有空置列

    # 候选边计数 + 真实格绝对差（µs）
    candidate_edges = 0
    abs_delta: List[List[int]] = [[0] * nb for _ in range(na)]
    for ai, ea in enumerate(a_sorted):
        for bi, eb in enumerate(b_sorted):
            d = abs(_delta_microseconds(ea.timestamp, eb.timestamp))
            abs_delta[ai][bi] = d
            if d <= tol_us:
                candidate_edges += 1

    chosen: List[Tuple[int, int]] = []
    if na > 0:
        # --- 主费用分层常数（µs）------------------------------------
        # 含 k 对匹配、真实总差 D 的方案费用 = D + (na-k)·P1。
        # 真实候选格 |Δt| ≤ tol，故 D ≤ min(na,nb)·tol。令
        #   P1 = min(na,nb)·tol + 1
        # 严格大于"所有真实格都取到 tol"的总差；少配一对省下的真实费用
        # 至多 min(na,nb)·tol < P1，却要多付一次 P1 ⇒ 最大基数严格占优。
        # P2（非候选真实格）= P1 + 1：非法配对比"自己空置"还贵；
        # BIG（他人私有空置列）再高一层，结构性禁止。
        p1 = min(na, nb) * tol_us + 1
        p2 = p1 + 1
        big = p2 * na + 1

        matrix: List[List[int]] = [[0] * m for _ in range(na)]
        for ai in range(na):
            for bi in range(nb):
                d = abs_delta[ai][bi]
                matrix[ai][bi] = d if d <= tol_us else p2
            for k in range(na):
                matrix[ai][nb + k] = p1 if k == ai else big

        col_of_row, u, v = hungarian_rect(matrix)

        # 紧边：c == u + v（整数精确比较）
        adj: List[List[int]] = [[] for _ in range(na)]
        for r in range(na):
            ur = u[r]
            row = matrix[r]
            for c in range(m):
                if row[c] == ur + v[c]:
                    adj[r].append(c)

        final = lexicographic_min_matching(na, m, adj, col_of_row)
        for ai in range(na):
            c = final[ai]
            if c < nb and abs_delta[ai][c] <= tol_us:
                chosen.append((ai, c))

    matched_a = {ai for ai, _ in chosen}
    matched_b = {bi for _, bi in chosen}

    pairs: List[Pair] = []
    total_us = 0
    for ai, bi in chosen:
        ea = a_sorted[ai]
        eb = b_sorted[bi]
        signed_us = _delta_microseconds(eb.timestamp, ea.timestamp)
        d = abs(signed_us)
        total_us += d
        pairs.append(
            Pair(
                a_id=ea.id,
                b_id=eb.id,
                a_source_key=ea.source_key,
                b_source_key=eb.source_key,
                a_timestamp=ea.timestamp,
                b_timestamp=eb.timestamp,
                delta_seconds=signed_us / 1_000_000,
                absolute_delta_seconds=d / 1_000_000,
                a_payload_summary=ea.payload_summary,
                b_payload_summary=eb.payload_summary,
            )
        )

    pairs.sort(key=lambda p: (p.a_id, p.b_id))

    unmatched_a = tuple(a_sorted[ai] for ai in range(na) if ai not in matched_a)
    unmatched_b = tuple(b_sorted[bi] for bi in range(nb) if bi not in matched_b)

    return Alignment(
        pairs=tuple(pairs),
        unmatched_a=unmatched_a,
        unmatched_b=unmatched_b,
        candidate_edges=candidate_edges,
        tolerance_seconds=tol_us / 1_000_000,
        total_absolute_delta_seconds=total_us / 1_000_000,
    )
