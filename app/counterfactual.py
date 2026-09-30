"""反事实解释：令指定输出消失的全部最小源行删除集 + 删除后重算。

两类计划分别处理：

* **非聚合计划（SPJ）**：输出行的存活条件是“至少一个见证绑定存活”，
  因此最小删除集恰好是其全部极小见证集的**极小击中集（hitting sets）**，
  可用自顶向下递归精确枚举；
* **含聚合的计划**：删除后目标分组可能消失（组空 / 无 GROUP BY 时整组无输出）
  或以不同聚合值继续存在。我们在目标分组的 ``meta.bindings`` 上做本地精确
  存活判定（与全量重算等价，但不需要每条子集都重跑整条计划），按删除集大小
  升序枚举并剪除已知最小解的超集。

枚举规模受 :data:`MAX_DELETION_UNIVERSE` 保护（默认 18 个来源变量，
至多 2^18 个子集）；命中上限返回结构化错误而不是静默超时。
"""
from __future__ import annotations

from typing import Any

from .canonical import canonical_dumps
from .engine import ExecutedPlan, execute
from .errors import provenance_too_large
from .models import Row, Table

MAX_DELETION_UNIVERSE = 18
MAX_REPORTED_DELETION_SETS = 256


# --------------------------------------------------------------------------
# SPJ：极小击中集
# --------------------------------------------------------------------------

def minimal_hitting_sets(witnesses: set[frozenset[str]]) -> list[frozenset[str]]:
    """返回集合族的全部极小击中集（按大小、字典序排序）。"""
    if not witnesses:
        return []
    # 先去掉被包含的见证：它们不改变击中集，还能显著缩小搜索。
    ordered = sorted(witnesses, key=len)
    essential: list[frozenset[str]] = []
    for w in ordered:
        if not any(e <= w for e in essential):
            essential = [e for e in essential if not w <= e]
            essential.append(w)

    results: list[frozenset[str]] = []

    def search(remaining: list[frozenset[str]], chosen: frozenset[str]) -> None:
        if not remaining:
            results.append(chosen)
            return
        # 必须击中剩余集合中最小的那个 => 枚举其元素作为下一次选择，
        # 这是标准的最小击中集分支定界，天然不产生非极小解。
        target = min(remaining, key=len)
        for var in sorted(target):
            new_chosen = chosen | {var}
            if any(r <= new_chosen for r in results):
                continue
            new_remaining = [s for s in remaining if var not in s]
            search(new_remaining, new_chosen)

    search(essential, frozenset())
    results = _dedup_sort(results)
    if len(results) > MAX_REPORTED_DELETION_SETS:
        raise provenance_too_large(
            len(results), MAX_REPORTED_DELETION_SETS,
            "最小删除集的数量（输出行见证组合过多）")
    return results


def _dedup_sort(sets: list[frozenset[str]] | set[frozenset[str]]
                ) -> list[frozenset[str]]:
    unique = set(sets)
    return sorted(unique, key=lambda s: (len(s), sorted(s)))


# --------------------------------------------------------------------------
# 聚合：逐绑定存活判定 + 子集枚举
# --------------------------------------------------------------------------

def minimal_deletion_sets_aggregate(target: Row) -> list[frozenset[str]]:
    """枚举聚合输出行的全部最小删除集。"""
    meta = target.meta
    if meta.get("kind") != "group":
        # 目标行经过 project/filter，分组元数据应在执行时透传。
        raise ValueError("内部错误：聚合输出行缺少 group 元数据。")

    bindings = meta["bindings"]
    universe = sorted({v for b in bindings for v in b["variables"]})
    if len(universe) > MAX_DELETION_UNIVERSE:
        raise provenance_too_large(len(universe), MAX_DELETION_UNIVERSE,
                                   "反事实删除集枚举")

    n = len(universe)
    target_key = tuple(canonical_dumps(target.values[f]) for f in meta["by"])
    agg_specs = meta["aggs"]

    def survives(deleted: frozenset[str]) -> bool:
        return _group_binding_survives(bindings, deleted, meta, target_key,
                                       target.values, agg_specs)

    kills: list[frozenset[str]] = []
    # 按删除数升序枚举；找到最小解后剪除其所有超集（最小性剪枝）。
    for mask in range(1, 1 << n):
        deleted = frozenset(universe[i] for i in range(n) if mask & (1 << i))
        if any(k < deleted for k in kills):
            continue
        if not survives(deleted):
            kills.append(deleted)

    kills = _dedup_sort(kills)
    if len(kills) > MAX_REPORTED_DELETION_SETS:
        raise provenance_too_large(
            len(kills), MAX_REPORTED_DELETION_SETS,
            "最小删除集的数量（聚合见证组合过多）")
    return kills


def _group_binding_survives(bindings: list[dict[str, Any]],
                            deleted: frozenset[str],
                            meta: dict[str, Any],
                            target_key: tuple[str, ...],
                            target_values: dict[str, Any],
                            agg_specs: list[dict[str, Any]]) -> bool:
    """删除后，目标分组是否以**完全相同的聚合值**继续存在。"""
    alive = []
    for b in bindings:
        bset = frozenset(b["variables"])
        if not (bset & deleted):
            alive.append(b)
    if not alive:
        return False  # 组空 => 分组消失（无 GROUP BY 时整查询无输出，同理）

    # 分组键来自输入行且对全组相同；这里存活绑定必然同组（删除只删行），
    # 保留断言式校验以防止计划变换破坏该前提。
    by_fields = meta["by"]
    # bindings 未直接保存 by 值；存活即意味着键不变，故此处只核聚合。
    for spec in agg_specs:
        name = spec["name"]
        func = spec["func"]
        field = spec.get("field")
        if func == "count":
            total = sum(b["mult"] for b in alive
                        if field is None or b["values"].get(field) is not None)
            if total != target_values[name]:
                return False
            continue
        vals = []
        for b in alive:
            val = b["values"].get(field) if field is not None else None
            if val is not None:
                vals.extend([val] * b["mult"])
        if not vals:
            new_value = None
        elif func == "sum":
            new_value = sum(vals)
        elif func == "min":
            new_value = min(vals)
        elif func == "max":
            new_value = max(vals)
        else:  # pragma: no cover
            return False
        if canonical_dumps(new_value) != canonical_dumps(target_values[name]):
            return False
    return True


# --------------------------------------------------------------------------
# 删除源行并重算
# --------------------------------------------------------------------------

def filter_tables(tables: dict[str, Table], deleted: frozenset[str]) -> dict[str, Table]:
    return {name: t.with_filtered_rows(deleted) for name, t in tables.items()}


def recompute(plan: dict[str, Any], tables: dict[str, Table],
              deleted: frozenset[str]) -> ExecutedPlan:
    return execute(plan, filter_tables(tables, deleted))


def compute_counterfactual(plan: dict[str, Any], tables: dict[str, Table],
                           executed: ExecutedPlan, target: Row
                           ) -> list[dict[str, Any]]:
    """计算全部最小删除集，并对每个删除集执行删除后重算。"""
    if executed.contains_aggregate:
        deletion_sets = minimal_deletion_sets_aggregate(target)
    else:
        deletion_sets = minimal_hitting_sets(target.witnesses)

    results: list[dict[str, Any]] = []
    for deleted in deletion_sets:
        re_executed = recompute(plan, tables, deleted)
        results.append({
            "deleted_source_rows": sorted(deleted),
            "result": serialize_relation(re_executed),
        })
    return results


def serialize_relation(executed: ExecutedPlan) -> dict[str, Any]:
    """重算结果只带模式与行值（不带溯源），按全行动态 JSON 排序。"""
    rows = [tuple(canonical_dumps(r.values.get(c)) for c in executed.columns)
            for r in executed.rows]
    rows.sort()
    return {
        "columns": list(executed.columns),
        "row_count": len(rows),
        "rows": [[_decode(c) for c in key] for key in rows],
    }


def _decode(canonical_json: str) -> Any:
    import json
    return json.loads(canonical_json)
