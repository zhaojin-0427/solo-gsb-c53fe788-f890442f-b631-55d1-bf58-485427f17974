"""结构化业务错误。

所有可预期的请求错误都抛出 ``APIError``，由 API 层统一序列化为
字节一致的错误信封，HTTP 状态码与错误码一一对应：

* 422 —— 请求计划 / 表快照 / 反事实选择器本身非法；
* 409 —— 幂等键已存在但绑定内容不同，或表内存在重复主键。
"""
from __future__ import annotations

from typing import Any


class APIError(Exception):
    """携带机器可读错误码、人类可读消息与上下文详情的业务异常。"""

    def __init__(self, code: str, message: str, details: Any = None, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": {
            "code": self.code,
            "message": self.message,
        }}
        if self.details is not None:
            payload["error"]["details"] = self.details
        return payload


# 常用错误的便捷构造函数 ----------------------------------------------------

def duplicate_primary_key(table: str, key: list[Any]) -> APIError:
    return APIError(
        "DUPLICATE_PRIMARY_KEY",
        f"表 {table!r} 中存在重复主键 {key!r}；主键必须唯一。",
        {"table": table, "primary_key": key},
        status_code=409,
    )


def invalid_request(message: str, details: Any = None) -> APIError:
    return APIError("INVALID_REQUEST", message, details)


def unknown_table(name: str) -> APIError:
    return APIError("UNKNOWN_TABLE", f"计划引用了未提交的表 {name!r}。", {"table": name})


def unknown_field(field: str, scope: str | None = None) -> APIError:
    details: dict[str, Any] = {"field": field}
    if scope is not None:
        details["scope"] = scope
    return APIError("UNKNOWN_FIELD", f"字段 {field!r} 不存在或不在当前作用域内。", details)


def invalid_plan(message: str, path: str | None = None, details: Any = None) -> APIError:
    payload: dict[str, Any] = {"message": message}
    if path is not None:
        payload["path"] = path
    if details is not None:
        payload.update(details if isinstance(details, dict) else {"value": details})
    return APIError("INVALID_PLAN", message, payload)


def type_error(message: str, details: Any = None) -> APIError:
    return APIError("TYPE_MISMATCH", message, details)


def aggregate_empty_group(details: Any = None) -> APIError:
    return APIError(
        "AGGREGATE_EMPTY_GROUP",
        "无 GROUP BY 的聚合节点收到了 0 行输入；SQL 语义下该查询不产出任何分组。",
        details,
    )


def target_not_found(index: int, output_size: int) -> APIError:
    return APIError(
        "TARGET_NOT_FOUND",
        f"反事实选择器 output_index={index} 越界；当前查询共有 {output_size} 条输出。",
        {"output_index": index, "output_size": output_size},
    )


def provenance_too_large(size: int, limit: int, where: str) -> APIError:
    return APIError(
        "PROVENANCE_TOO_LARGE",
        (f"{where}涉及 {size} 个不同来源行变量，超过穷举上限 {limit}；"
         "请缩小输入或收紧选择/连接条件。"),
        {"variable_count": size, "limit": limit, "where": where},
    )
