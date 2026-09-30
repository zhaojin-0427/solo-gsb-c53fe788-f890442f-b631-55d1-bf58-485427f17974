"""核心内存数据模型：提交表、关系行与溯源信息。

执行过程中的每一行（Row）携带三部分：

* ``values``：输出列名 -> 列值（int/float/str/bool/None）；
* ``witnesses``：该行存在性的**极小见证集集合**，每个见证是一组来源变量
  （DNF 的一个合取项）。对 SPJ 行它就是产生该行的若干极小变量绑定；
  对聚合行它由引擎按“哪些子集能产生完全相同的聚合结果”计算；
* ``bag``：产生该行的**全部绑定**（含重复与非极小绑定），每个元素是
  ``(变量集合, 重数)``，供 count 等需要计数的聚合使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import duplicate_primary_key, type_error
from .canonical import source_variable


def _check_value_type(table: str, row_index: int, col: str,
                      col_type: str, val: Any) -> None:
    """校验行内标量值与声明列类型一致（null 对任何列合法）。"""
    from .errors import invalid_request
    if val is None:
        return
    valid: bool
    if col_type == "integer":
        valid = isinstance(val, int) and not isinstance(val, bool)
    elif col_type == "real":
        valid = isinstance(val, (int, float)) and not isinstance(val, bool)
    elif col_type == "bool":
        valid = isinstance(val, bool)
    else:
        valid = isinstance(val, str)
    if not valid:
        raise type_error(
            f"表 {table!r} 第 {row_index} 行列 {col!r} 的值与声明类型 {col_type} 不匹配。",
            {"table": table, "row_index": row_index, "column": col,
             "column_type": col_type, "actual_type": type(val).__name__})


@dataclass(frozen=True)
class Table:
    """一张已提交的基表（关系 + 声明式 schema + 主键索引）。"""

    name: str
    columns: list[str]
    column_types: dict[str, str]              # "integer" | "real" | "text" | "bool"
    primary_key: list[str]
    rows: list[dict[str, Any]]
    # 主键值元组 -> 行数据；重复主键在此处被拒绝。
    _index: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, payload: dict[str, Any]) -> "Table":
        from .errors import invalid_request

        name = payload["name"]
        raw_columns = payload.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            raise invalid_request(f"表 {name!r} 需要非空 'columns' 列表。",
                                  {"field": f"tables.{name}.columns"})
        columns: list[str] = []
        column_types: dict[str, str] = {}
        for i, c in enumerate(raw_columns):
            if isinstance(c, dict):
                col_name = c.get("name")
                col_type = c.get("type", "text")
            else:
                col_name, col_type = c, "text"
            if not isinstance(col_name, str) or not col_name:
                raise invalid_request("列名必须是非空字符串。",
                                      {"field": f"tables.{name}.columns[{i}]"})
            if col_name in columns:
                raise invalid_request(f"表 {name!r} 的列 {col_name!r} 重复。",
                                      {"table": name, "column": col_name})
            if col_type not in ("integer", "real", "text", "bool"):
                raise type_error(f"不支持的列类型 {col_type!r}。",
                                 {"table": name, "column": col_name,
                                  "allowed": ["integer", "real", "text", "bool"]})
            columns.append(col_name)
            column_types[col_name] = col_type

        primary_key = payload.get("primary_key")
        if (not isinstance(primary_key, list) or not primary_key
                or not all(isinstance(k, str) for k in primary_key)):
            raise invalid_request(f"表 {name!r} 需要非空字符串列表 'primary_key'。",
                                  {"field": f"tables.{name}.primary_key"})
        for k in primary_key:
            if k not in column_types:
                from .errors import unknown_field
                raise unknown_field(k, scope=f"table:{name}")
        if len(set(primary_key)) != len(primary_key):
            raise invalid_request(f"表 {name!r} 的主键列声明重复。",
                                  {"table": name, "primary_key": primary_key})

        rows_raw = payload.get("rows", [])
        if not isinstance(rows_raw, list):
            raise invalid_request(f"表 {name!r} 的 'rows' 必须是列表。",
                                  {"field": f"tables.{name}.rows"})

        rows: list[dict[str, Any]] = []
        for i, row in enumerate(rows_raw):
            if not isinstance(row, dict):
                raise invalid_request(f"表 {name!r} 的第 {i} 行必须是对象。",
                                      {"field": f"tables.{name}.rows[{i}]"})
            extra = set(row) - set(columns)
            missing = [c for c in columns if c not in row]
            if extra:
                raise invalid_request(
                    f"表 {name!r} 的第 {i} 行出现未声明列。",
                    {"table": name, "row_index": i, "unknown_columns": sorted(extra)})
            if missing:
                raise invalid_request(
                    f"表 {name!r} 的第 {i} 行缺少列。",
                    {"table": name, "row_index": i, "missing_columns": missing})
            for col, val in row.items():
                _check_value_type(name, i, col, column_types[col], val)
            rows.append(dict(row))

        index: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(row[col] for col in primary_key)
            if any(v is None for v in key):
                from .errors import invalid_request as _ir
                raise _ir(f"表 {name!r} 存在主键为 null 的行；主键不可为空。",
                          {"table": name, "primary_key": primary_key})
            if key in index:
                raise duplicate_primary_key(name, list(key))
            index[key] = row
        return cls(name, columns, column_types, primary_key, rows, index)

    def pk_tuple(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(row[col] for col in self.primary_key)

    def variable_of(self, row: dict[str, Any]) -> str:
        return source_variable(self.name, self.primary_key, self.pk_tuple(row))

    def with_filtered_rows(self, deleted: set[str]) -> "Table":
        """返回删除给定来源变量对应行之后的新表（保留全部 schema 信息）。"""
        kept = [r for r in self.rows if self.variable_of(r) not in deleted]
        payload = {
            "name": self.name,
            "columns": [{"name": c, "type": self.column_types[c]} for c in self.columns],
            "primary_key": list(self.primary_key),
            "rows": kept,
        }
        return Table.build(payload)


@dataclass
class Row:
    """关系代数执行中的一行。"""

    values: dict[str, Any]
    # 极小见证集集合：frozenset(variable,...) 的集合（DNF 项）。
    witnesses: set[frozenset[str]] = field(default_factory=set)
    # 全部绑定：(frozenset(variable,...), multiplicity)。
    bag: list[tuple[frozenset[str], int]] = field(default_factory=list)
    # 节点专属元数据；group 节点用它保存逐绑定的聚合输入取值，
    # 供反事实删除后的“目标分组是否还以原值存在”做本地精确判定。
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def scan(values: dict[str, Any], variable: str) -> "Row":
        binding = frozenset({variable})
        return Row(values, {binding}, [(binding, 1)])


# 关系 = 行列表；列顺序由 schema 推导单独携带。
Relation = list[Row]


def prune_minimal(sets: set[frozenset[str]]) -> set[frozenset[str]]:
    """从集合族中删除所有非极小成员（存在另一成员为其真子集）。"""
    ordered = sorted(sets, key=len)
    minimal: list[frozenset[str]] = []
    for s in ordered:
        if not any(m < s for m in minimal):
            # 同等大小、互不包含的集合全部保留；先剔除被已有极小集包含的。
            minimal = [m for m in minimal if not s < m]
            minimal.append(s)
    return set(minimal)


def row_value_key(values: dict[str, Any], columns: list[str]) -> tuple[Any, ...]:
    from .canonical import canonical_dumps
    return tuple(canonical_dumps(values.get(col)) for col in columns)
