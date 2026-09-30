"""受限关系代数引擎：静态校验、求值、布尔溯源见证计算。

支持五种计划节点（节点必须带 ``type``）::

    scan     {"type":"scan","table":"orders","alias":"o"}
    filter   {"type":"filter","input":...,"predicate":<受限谓词>}
    join     {"type":"join","left":...,"right":...,"on":[["l.oid","r.oid"], ...]}
    project  {"type":"project","input":...,"fields":["a.x","b.y"]}
    group    {"type":"group","input":...,"by":["a.cust"],
              "aggregates":[{"name":"cnt","func":"count"},
                            {"name":"total","func":"sum","field":"a.amt"}]}

执行结果 :class:`ExecutedPlan` 同时携带列顺序与 :class:`~app.models.Row` 列表；
每行的 ``witnesses`` 即“全部极小来源行见证集”，也就是布尔溯源表达式的 DNF。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import canonical_dumps
from .errors import (aggregate_empty_group, invalid_plan, type_error,
                     unknown_field, unknown_table)
from .models import Relation, Row, Table, prune_minimal
from .predicates import evaluate_predicate, validate_predicate

AGG_FUNCS = {"count", "sum", "min", "max"}
MAX_PROVENANCE_VARIABLES = 18


@dataclass
class ExecutedPlan:
    columns: list[str]
    rows: Relation
    contains_aggregate: bool


# --------------------------------------------------------------------------
# 校验 + schema 推导
# --------------------------------------------------------------------------

def validate_plan(plan: Any, tables: dict[str, Table]) -> None:
    """仅做静态校验（schema 推导过程中顺带完成）。"""
    _infer_schema(plan, tables)


def _infer_schema(node: Any, tables: dict[str, Table]
                  ) -> tuple[dict[str, str], list[str], list[str]]:
    """返回 (限定列名->逻辑类型, 输出列顺序, 聚合分组键列)。

    第三个值用于拦截“project 丢掉全部 GROUP BY 列却保留聚合值”的计划：
    那种投影会把不同分组合并成同一输出值，使逐分组的反事实元数据失去唯一
    归属。整体聚合（by 为空）返回空分组键列表，投影聚合值不受限制。
    """
    if not isinstance(node, dict) or "type" not in node:
        raise invalid_plan("计划节点必须是带 'type' 的对象。", None)
    ntype = node["type"]

    if ntype == "scan":
        name = node.get("table")
        if not isinstance(name, str):
            raise invalid_plan("scan 节点需要字符串字段 'table'。", None)
        if name not in tables:
            raise unknown_table(name)
        alias = node.get("alias", name)
        if not isinstance(alias, str) or not alias:
            raise invalid_plan("scan 节点的 'alias' 必须是非空字符串。", None)
        t = tables[name]
        schema = {f"{alias}.{c}": t.column_types[c] for c in t.columns}
        return schema, list(schema), []

    if ntype == "filter":
        schema, order, group_keys = _child_schema(node, "input", tables)
        pred = node.get("predicate")
        validate_predicate(pred, schema, "predicate")
        return schema, order, group_keys

    if ntype == "join":
        left_schema, left_order, _ = _child_schema(node, "left", tables)
        right_schema, right_order, _ = _child_schema(node, "right", tables)
        overlap = set(left_schema) & set(right_schema)
        if overlap:
            raise invalid_plan(
                "连接两侧的限定列名冲突；请为 scan 指定不同 alias。",
                "join", {"conflicting_fields": sorted(overlap)})
        on = node.get("on")
        if not isinstance(on, list) or not on:
            raise invalid_plan("join 节点需要非空的等值条件列表 'on'。", "join.on")
        for i, cond in enumerate(on):
            if (not isinstance(cond, list) or len(cond) != 2
                    or not all(isinstance(x, str) for x in cond)):
                raise invalid_plan(
                    "每个 join 条件必须是 [左限定列, 右限定列] 字符串对。",
                    f"join.on[{i}]")
            lf, rf = cond
            if lf not in left_schema:
                raise unknown_field(lf, scope="join.left")
            if rf not in right_schema:
                raise unknown_field(rf, scope="join.right")
            if left_schema[lf] != right_schema[rf]:
                raise type_error(
                    f"等值连接条件 {lf}={rf} 两侧类型不一致。",
                    {"left_type": left_schema[lf], "right_type": right_schema[rf]})
        schema = {**left_schema, **right_schema}
        # 嵌套聚合已在 group 校验时拒绝；join 两侧不可能携带分组键。
        return schema, left_order + right_order, []

    if ntype == "project":
        schema, _, group_keys = _child_schema(node, "input", tables)
        fields = node.get("fields")
        if not isinstance(fields, list) or not fields:
            raise invalid_plan("project 节点需要非空 'fields' 列表。", "project.fields")
        out_order: list[str] = []
        for i, f in enumerate(fields):
            if not isinstance(f, str):
                raise invalid_plan("project 字段必须是限定列名字符串。",
                                   f"project.fields[{i}]")
            if f not in schema:
                raise unknown_field(f, scope="project")
            if f in out_order:
                raise invalid_plan(f"project 输出列 {f!r} 重复。",
                                   f"project.fields[{i}]")
            out_order.append(f)
        # 若输入来自带分组键的 group，投影必须保留**全部**分组键，否则不同
        # 分组可能塌缩成同一输出行，反事实将无法唯一归属到源分组。
        if group_keys and not all(k in out_order for k in group_keys):
            dropped = [k for k in group_keys if k not in out_order]
            raise invalid_plan(
                "project 不能在保留聚合列的同时丢弃 GROUP BY 分组键"
                "（不同分组会塌缩为同一输出行）。请保留分组键或仅投影非聚合关系。",
                "project.fields", {"dropped_group_keys": dropped})
        out_schema = {f: schema[f] for f in out_order}
        surviving_group_keys = [k for k in group_keys if k in out_order]
        return out_schema, out_order, surviving_group_keys

    if ntype == "group":
        schema, _, _ = _child_schema(node, "input", tables)
        if _node_contains_aggregate(node["input"]):
            raise invalid_plan(
                "受限计划不允许嵌套聚合：group 的输入子树中不能再出现 group 节点。",
                "group.input")
        by = node.get("by", [])
        if not isinstance(by, list) or not all(isinstance(f, str) for f in by):
            raise invalid_plan("group 的 'by' 必须是限定列名列表（可为空）。", "group.by")
        for f in by:
            if f not in schema:
                raise unknown_field(f, scope="group.by")

        aggs = node.get("aggregates")
        if not isinstance(aggs, list) or not aggs:
            raise invalid_plan("group 节点需要非空 'aggregates' 列表。", "group.aggregates")
        out_schema = {f: schema[f] for f in by}
        out_order = list(by)
        seen_names: set[str] = set(by)
        for i, agg in enumerate(aggs):
            path = f"group.aggregates[{i}]"
            if not isinstance(agg, dict):
                raise invalid_plan("聚合描述必须是对象。", path)
            name = agg.get("name")
            func = agg.get("func")
            if not isinstance(name, str) or not name:
                raise invalid_plan("聚合需要非空字符串 'name'。", f"{path}.name")
            if name in seen_names:
                raise invalid_plan(f"聚合输出名 {name!r} 与分组列或其他聚合重复。",
                                   f"{path}.name")
            seen_names.add(name)
            if func not in AGG_FUNCS:
                raise invalid_plan(f"不支持的聚合函数 {func!r}。", f"{path}.func",
                                   {"allowed": sorted(AGG_FUNCS)})
            field = agg.get("field")
            if func == "count":
                if field is not None and not isinstance(field, str):
                    raise invalid_plan("count 的 field 必须为字符串或省略（count(*)）。",
                                       f"{path}.field")
                out_schema[name] = "integer"
            else:
                if not isinstance(field, str) or field not in schema:
                    raise unknown_field(field if isinstance(field, str) else str(field),
                                        scope="group.aggregates")
                col_type = schema[field]
                if col_type not in ("integer", "real"):
                    raise type_error(
                        f"聚合 {func} 只能作用于数值列，{field!r} 的类型为 {col_type}。",
                        {"path": f"{path}.field", "column_type": col_type})
                out_schema[name] = "integer" if (func == "sum" and col_type == "integer") else (
                    "real" if col_type == "real" else "integer")
                if func in ("min", "max"):
                    out_schema[name] = col_type
            out_order.append(name)
        return out_schema, out_order, list(by)

    raise invalid_plan(f"未知计划节点类型 {ntype!r}。", None,
                       {"allowed": ["scan", "filter", "join", "project", "group"]})


def _child_schema(node: dict[str, Any], key: str, tables: dict[str, Table]
                  ) -> tuple[dict[str, str], list[str], list[str]]:
    child = node.get(key)
    if child is None:
        raise invalid_plan(f"{node.get('type')} 节点缺少输入 {key!r}。", None)
    return _infer_schema(child, tables)


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def execute(plan: dict[str, Any], tables: dict[str, Table]) -> ExecutedPlan:
    columns = _infer_schema(plan, tables)[1]
    contains_agg = _node_contains_aggregate(plan)
    rows = _exec_node(plan, tables)
    return ExecutedPlan(columns=columns, rows=rows,
                        contains_aggregate=contains_agg)


def _node_contains_aggregate(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("type") == "group":
        return True
    for key in ("input", "left", "right"):
        if key in node and _node_contains_aggregate(node[key]):
            return True
    return False


def _exec_node(node: dict[str, Any], tables: dict[str, Table]) -> Relation:
    ntype = node["type"]

    if ntype == "scan":
        table = tables[node["table"]]
        alias = node.get("alias", node["table"])
        relation: Relation = []
        for raw in table.rows:
            values = {f"{alias}.{c}": raw.get(c) for c in table.columns}
            relation.append(Row.scan(values, table.variable_of(raw)))
        return relation

    if ntype == "filter":
        return [r for r in _exec_node(node["input"], tables)
                if evaluate_predicate(node["predicate"], r.values)]

    if ntype == "join":
        left = _exec_node(node["left"], tables)
        right = _exec_node(node["right"], tables)
        on = [(lf, rf) for lf, rf in node["on"]]
        out: Relation = []
        for lr in left:
            for rr in right:
                if any(_join_equal(lr.values.get(lf), rr.values.get(rf)) for lf, rf in on):
                    if set(lr.values) & set(rr.values):  # 校验阶段已保证限定名不相交
                        raise invalid_plan("连接两侧限定列名冲突。", "join")
                    values = {**lr.values, **rr.values}
                    witnesses = {(wl | wr) for wl in lr.witnesses for wr in rr.witnesses}
                    bag: list[tuple[frozenset[str], int]] = []
                    for bl, ml in lr.bag:
                        for br, mr in rr.bag:
                            bag.append((bl | br, ml * mr))
                    out.append(Row(values, prune_minimal(witnesses), _merge_bag(bag)))
        return out

    if ntype == "project":
        fields = node["fields"]
        # 相同输出值的行合并（集合语义）；见证取并集后做极小化，bag 累加。
        merged: dict[tuple[str, ...], Row] = {}
        for r in _exec_node(node["input"], tables):
            values = {f: r.values[f] for f in fields}
            key = tuple(canonical_dumps(values[f]) for f in fields)
            if key not in merged:
                # project 不做聚合，分组行（带 group 元数据）按值合并且
                # 分组键被投影保留时，每个键只对应一个分组，meta 直接透传。
                merged[key] = Row(values, set(), [], meta=dict(r.meta))
            target = merged[key]
            target.witnesses |= r.witnesses
            target.bag = _merge_bag(target.bag + r.bag)
        return [_finalize_row(row) for row in merged.values()]

    if ntype == "group":
        return _exec_group(node, _exec_node(node["input"], tables))

    raise invalid_plan(f"未知计划节点类型 {ntype!r}。", None)  # pragma: no cover


def _join_equal(a: Any, b: Any) -> bool:
    # null 永不参与等值连接。
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def _merge_bag(bag: list[tuple[frozenset[str], int]]) -> list[tuple[frozenset[str], int]]:
    totals: dict[frozenset[str], int] = {}
    for binding, mult in bag:
        totals[binding] = totals.get(binding, 0) + mult
    return [(b, m) for b, m in totals.items() if m > 0]


def _finalize_row(row: Row) -> Row:
    row.witnesses = prune_minimal(row.witnesses)
    return row


# --------------------------------------------------------------------------
# group：分组聚合 + 聚合结果的极小见证
# --------------------------------------------------------------------------

def _exec_group(node: dict[str, Any], input_rel: Relation) -> Relation:
    by: list[str] = node["by"]
    aggs: list[dict[str, Any]] = node["aggregates"]
    if not input_rel:
        if by:
            return []  # 带 GROUP BY 的空输入 => 0 个分组
        raise aggregate_empty_group({"node": "group", "aggregates": [a["name"] for a in aggs]})

    groups: dict[tuple[str, ...], list[Row]] = {}
    for r in input_rel:
        key = tuple(canonical_dumps(r.values.get(f)) for f in by)
        groups.setdefault(key, []).append(r)

    out: Relation = []
    for _, members in groups.items():
        values = {f: members[0].values[f] for f in by}
        for agg in aggs:
            values[agg["name"]] = _aggregate_value(agg, members)
        witnesses = _group_witnesses(aggs, members, values)
        meta = _group_meta(by, aggs, members)
        out.append(Row(values, witnesses, [(w, 1) for w in witnesses], meta=meta))
    return out


def _group_meta(by: list[str], aggs: list[dict[str, Any]],
                members: list[Row]) -> dict[str, Any]:
    """记录分组内“绑定 -> 重数/聚合输入取值”，供反事实本地重算。"""
    bindings: dict[frozenset[str], dict[str, Any]] = {}
    for r in members:
        for binding, mult in r.bag:
            entry = bindings.setdefault(binding, {"mult": 0, "values": {}})
            entry["mult"] += mult
            for agg in aggs:
                field = agg.get("field")
                if field is not None:
                    entry["values"][field] = r.values.get(field)
    return {
        "kind": "group",
        "by": list(by),
        "aggs": [{"name": a["name"], "func": a["func"], "field": a.get("field")}
                 for a in aggs],
        "bindings": [
            {"variables": sorted(b), "mult": e["mult"], "values": e["values"]}
            for b, e in bindings.items()
        ],
    }


def _aggregate_value(agg: dict[str, Any], members: list[Row]) -> Any:
    """根据成员行的全部绑定重数计算一个聚合的值。"""
    func = agg["func"]
    if func == "count":
        return _member_bag_size(agg, members)

    vals = list(_aggregate_input_values(agg, members))
    if not vals:
        return None
    if func == "sum":
        return sum(vals)
    if func == "min":
        return min(vals)
    if func == "max":
        return max(vals)
    return None  # pragma: no cover


def _member_bag_size(agg: dict[str, Any], members: list[Row]) -> int:
    # count(*) 数全部绑定；count(field) 忽略该字段为 null 的输入行，
    # 这里按绑定重数计入 field 非空的成员。
    field = agg.get("field")
    if field is None:
        return sum(m for _, m in _flatten_bag(members, None))
    return sum(m for _, m in _flatten_bag(members, field))


def _flatten_bag(members: list[Row], field: str | None
                 ) -> list[tuple[frozenset[str], int]]:
    flat: list[tuple[frozenset[str], int]] = []
    for r in members:
        for binding, mult in r.bag:
            if field is not None and r.values.get(field) is None:
                continue
            flat.append((binding, mult))
    return flat


def _aggregate_input_values(agg: dict[str, Any], members: list[Row]) -> list[Any]:
    """展开成员行的全部绑定取值。

    绑定重数来自等值连接的扇出：sum 必须按重数重复贡献，
    min/max 重复取值不影响结果。
    """
    field = agg.get("field")
    out: list[Any] = []
    for r in members:
        val = r.values.get(field) if field is not None else None
        if val is None:
            continue
        mult = sum(m for _, m in r.bag)
        out.extend([val] * mult)
    return out


def _group_witnesses(aggs: list[dict[str, Any]], members: list[Row],
                     full_values: dict[str, Any]) -> set[frozenset[str]]:
    """计算一个分组输出行的全部极小见证集。

    分组列天然被所有成员共享，因此只需要让每个聚合都取到原值：

    * ``count``：留下的绑定重数必须等于原重数 => 唯一极小见证 = 全变量集；
    * ``sum``：若存在贡献为 0 的绑定，只需一个“其余绑定贡献之和为原总和”
      的极小子集；通过枚举变量子集精确求解（组规模受穷举上限保护）；
    * ``min``/``max``：任一取到极值的成员的极小见证即一个见证；
    多个聚合时见证 = 各自所需绑定的并集，再整体极小化。
    """
    universe = frozenset(v for r in members for w in r.witnesses for v in w)
    if len(universe) > MAX_PROVENANCE_VARIABLES:
        from .errors import provenance_too_large
        raise provenance_too_large(len(universe), MAX_PROVENANCE_VARIABLES,
                                   "聚合分组的溯源计算")

    options_per_agg: list[list[frozenset[str]]] = []
    for agg in aggs:
        options_per_agg.append(_single_agg_witness_options(agg, members, universe))

    # 各聚合所需绑定的笛卡尔积 => 并集；最后统一极小化。
    candidates: set[frozenset[str]] = {frozenset()}
    for options in options_per_agg:
        candidates = {(c | opt) for c in candidates for opt in options}
    return prune_minimal(candidates)


def _single_agg_witness_options(agg: dict[str, Any], members: list[Row],
                                universe: frozenset[str]) -> list[frozenset[str]]:
    func = agg["func"]
    flat = _flatten_bag(members, agg.get("field"))
    # flat: 参与该聚合的 (绑定, 重数)；count(field)/sum(field)/min/max(field)
    # 已剔除 field 为 null 的绑定。
    all_bindings = [b for b, _ in flat]

    if func == "count":
        return [frozenset(universe)]

    if func in ("min", "max"):
        field = agg["field"]
        member_by_binding = {b: r for r in members for b, _ in r.bag}
        vals = [(b, member_by_binding[b].values[field]) for b in all_bindings]
        target = min(v for _, v in vals) if func == "min" else max(v for _, v in vals)
        extremal = {b for b, v in vals if v == target}
        # 一个极值绑定自身即可；用它与成员行的极小见证求交得到极小来源集。
        options: set[frozenset[str]] = set()
        for r in members:
            for w in r.witnesses:
                if any(w <= b for b in extremal):
                    options.add(w)
        return sorted(prune_minimal(options), key=lambda s: (len(s), sorted(s)))

    if func == "sum":
        field = agg["field"]
        member_by_binding = {b: r for r in members for b, _ in r.bag}
        target = sum(member_by_binding[b].values[field] * m for b, m in flat)
        return _sum_witness_options(all_bindings, flat, member_by_binding, field, target)

    return [frozenset(universe)]  # pragma: no cover


def _sum_witness_options(all_bindings: list[frozenset[str]],
                         flat: list[tuple[frozenset[str], int]],
                         member_by_binding: dict[frozenset[str], Row],
                         field: str, target: Any) -> list[frozenset[str]]:
    """枚举使 sum 保持原值的极小子集。

    绑定贡献 = 值 * 重数。子集和等于 target 当且仅当未选绑定贡献之和为 0，
    因此只在存在 0 贡献时才会出现真正的小子集；其余情形唯一答案即全集。
    """
    contribution = {b: member_by_binding[b].values[field] * m for b, m in flat}
    if target is None:
        return []
    if all(contribution[b] != 0 for b in all_bindings):
        return [frozenset(v for b in all_bindings for v in b)]

    valid: set[frozenset[str]] = set()
    n = len(all_bindings)
    # 子集枚举（按大小），只保留极小有效集。
    for mask in range(1, 1 << n):
        chosen = [all_bindings[i] for i in range(n) if mask & (1 << i)]
        total = sum(contribution[b] for b in chosen)
        if total == target:
            valid.add(frozenset(v for b in chosen for v in b))
    minimal = prune_minimal(valid)
    return sorted(minimal, key=lambda s: (len(s), sorted(s)))
