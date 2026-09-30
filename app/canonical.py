"""规范化 JSON、请求指纹与布尔溯源变量编解码。

字节一致性是本服务的核心承诺：

* ``canonical_dumps`` 使用分隔符 ``(",", ":")``、``ensure_ascii=False``、
  递归排序对象键，使任意两份语义相等的请求/响应得到完全相同的字节；
* 请求指纹 = SHA-256(规范化的 {plan, tables})，与幂等键一起决定缓存条目；
* 来源行变量形如 ``orders[id=1024]``（字符串主键会做 JSON 转义），
  自连接中同一行在不同扫描节点出现时仍是同一个变量——删除物理行确实会
  同时影响所有别名。
"""
from __future__ import annotations

import hashlib
from typing import Any

# 统一 JSON 序列化参数：响应体与缓存键都走这里。
CANONICAL_JSON_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "separators": (",", ":"),
    "sort_keys": True,
    "allow_nan": False,
}


def canonical_dumps(obj: Any) -> str:
    """返回确定性的规范化 JSON 字符串。"""
    import json
    return json.dumps(obj, **CANONICAL_JSON_KWARGS)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_dumps(obj).encode("utf-8")


def request_fingerprint(plan: dict[str, Any], tables: list[dict[str, Any]],
                        counterfactual: Any = None) -> str:
    """计算规范化请求内容的 SHA-256 指纹（十六进制）。

    指纹绑定规范化计划与表快照；反事实选择器作为请求的一部分同样纳入，
    从而“同一计划的查询请求”和“同一计划的反事实请求”互不串用缓存，
    而两份完全相同的请求永远得到同一条缓存。
    """
    digest = hashlib.sha256()
    digest.update(canonical_bytes(
        {"plan": plan, "tables": tables, "counterfactual": counterfactual}))
    return digest.hexdigest()


def source_variable(table: str, primary_key_columns: list[str], key_values: tuple[Any, ...]) -> str:
    """把 (表名, 主键值) 渲染成溯源布尔变量。

    单列主键渲染为 ``table[col=value]``；复合主键渲染为
    ``table[col1=v1,col2=v2]``，列按声明顺序、值按规范化 JSON 转义。
    """
    parts = [f"{col}={canonical_dumps(val)}"
             for col, val in zip(primary_key_columns, key_values)]
    return f"{table}[{','.join(parts)}]"


def stable_key(value: Any) -> tuple[int, str]:
    """用于任意 JSON 值的全序排序键（输出需要确定性顺序）。"""
    return (0, canonical_dumps(value))
