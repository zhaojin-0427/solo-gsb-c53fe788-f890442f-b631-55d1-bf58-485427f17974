"""受限谓词：JSON 形式的布尔表达式，仅允许列与常量比较。

语法（递归）::

    {"op": "and", "args": [<pred>, ...]}
    {"op": "or",  "args": [<pred>, ...]}
    {"op": "not", "arg": <pred>}
    {"op": "eq"|"ne"|"lt"|"lte"|"gt"|"gte", "field": "别名.列", "value": <常量>}
    {"op": "is_null"|"not_null", "field": "别名.列"}

不支持子查询、用户自定义函数、任意表达式或跨列比较——这保证计划可静态
校验、与 SQL 注入面完全隔离。
"""
from __future__ import annotations

from typing import Any

from .errors import invalid_plan, type_error, unknown_field

LOGICAL_OPS = {"and", "or"}
UNARY_OPS = {"not"}
COMPARE_OPS = {"eq", "ne", "lt", "lte", "gt", "gte"}
NULL_OPS = {"is_null", "not_null"}
ALL_OPS = LOGICAL_OPS | UNARY_OPS | COMPARE_OPS | NULL_OPS


def _is_number(v: Any) -> bool:
    # bool 是 int 的子类，但布尔列与数值列互不兼容。
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_predicate(pred: Any, schema: dict[str, str], path: str) -> None:
    """递归校验谓词；``schema`` 映射限定名(alias.col) -> 逻辑列类型。"""
    if not isinstance(pred, dict) or "op" not in pred:
        raise invalid_plan("谓词必须是带 'op' 的对象。", path)
    op = pred["op"]
    if op not in ALL_OPS:
        raise invalid_plan(f"不支持的谓词算子 {op!r}。", path,
                           {"allowed": sorted(ALL_OPS)})

    if op in LOGICAL_OPS:
        args = pred.get("args")
        if not isinstance(args, list) or len(args) < 2:
            raise invalid_plan(f"逻辑算子 {op!r} 需要至少 2 个 args。", path)
        for i, child in enumerate(args):
            validate_predicate(child, schema, f"{path}.args[{i}]")
        return

    if op in UNARY_OPS:
        if "arg" not in pred:
            raise invalid_plan("'not' 谓词需要 'arg'。", path)
        validate_predicate(pred["arg"], schema, f"{path}.arg")
        return

    field = pred.get("field")
    if not isinstance(field, str) or field not in schema:
        raise unknown_field(field if isinstance(field, str) else str(field),
                            scope=",".join(sorted(schema)))
    col_type = schema[field]

    if op in NULL_OPS:
        return

    if "value" not in pred:
        raise invalid_plan(f"比较算子 {op!r} 需要常量 'value'。", path)
    value = pred["value"]
    if value is None:
        # 等值比较中的 null 合法；其余比较对 null 无意义（三值逻辑）。
        if op not in ("eq", "ne"):
            raise type_error(f"算子 {op!r} 不能与 null 比较，请使用 is_null/not_null。",
                             {"path": path, "field": field})
        return
    if col_type in ("integer", "real"):
        if not _is_number(value):
            raise type_error(f"数值列 {field!r} 不能与非数值常量比较。",
                             {"path": path, "field": field, "column_type": col_type})
    elif col_type == "bool":
        if not isinstance(value, bool):
            raise type_error(f"布尔列 {field!r} 只能与 true/false 比较。",
                             {"path": path, "field": field})
    elif col_type == "text":
        if not isinstance(value, str):
            raise type_error(f"文本列 {field!r} 只能与字符串常量比较。",
                             {"path": path, "field": field})


def evaluate_predicate(pred: dict[str, Any], values: dict[str, Any]) -> bool:
    """对具体行求谓词值；采用二值逻辑，null 参与的比较一律为 false。"""
    op = pred["op"]
    if op == "and":
        return all(evaluate_predicate(c, values) for c in pred["args"])
    if op == "or":
        return any(evaluate_predicate(c, values) for c in pred["args"])
    if op == "not":
        return not evaluate_predicate(pred["arg"], values)

    field = pred["field"]
    left = values.get(field)

    if op == "is_null":
        return left is None
    if op == "not_null":
        return left is not None

    right = pred["value"]
    if left is None or right is None:
        # IS NOT DISTINCT 风格之外，null 比较不成立；eq/ne null 也不匹配，
        # 用户应显式使用 is_null / not_null。
        return False

    # 跨类型比较一律不成立（含 bool vs int）。
    if isinstance(left, bool) or isinstance(right, bool):
        if not (isinstance(left, bool) and isinstance(right, bool)):
            return False
    elif _is_number(left) and _is_number(right):
        pass
    elif type(left) is type(right):
        pass
    else:
        return False

    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    return False  # pragma: no cover - 校验阶段已拦截
