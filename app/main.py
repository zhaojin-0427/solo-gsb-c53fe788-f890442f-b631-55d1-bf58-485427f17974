"""FastAPI 入口：关系查询溯源与反事实解释 API。

路由
----
``GET  /health``              健康检查
``POST /query``               提交计划/表快照执行查询；可选反事实解释

请求信封见 ``examples/`` 与 README；所有成功/错误响应均以规范化 JSON
（排序键、无多余空白、UTF-8 原样中文）写出，同一幂等请求字节一致。
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from .canonical import CANONICAL_JSON_KWARGS, canonical_bytes, request_fingerprint
from .counterfactual import compute_counterfactual
from .engine import validate_plan, execute
from .errors import APIError, invalid_request, target_not_found
from .models import Table
from .response import build_variables_legend, serialize_output, sort_rows
from .storage import IdempotencyStore

CONTENT_TYPE = "application/json; charset=utf-8"

app = FastAPI(
    title="关系查询溯源与反事实解释 API",
    version="1.0.0",
    description="受限关系代数求值、极小来源行见证、布尔溯源与最小删除反事实。",
)

_DB_PATH = os.environ.get("IDEMPOTENCY_DB", "/data/idempotency.sqlite3")
_store: IdempotencyStore | None = None


def get_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore(_DB_PATH)
    return _store


def json_response(payload: Any, status_code: int = 200, replay: bool = False) -> Response:
    headers = {}
    if replay:
        headers["X-Idempotent-Replay"] = "true"
    return Response(content=canonical_bytes(payload), status_code=status_code,
                    media_type=CONTENT_TYPE, headers=headers)


def error_response(err: APIError, replay: bool = False) -> Response:
    return json_response(err.to_payload(), err.status_code, replay=replay)


# --------------------------------------------------------------------------
# 请求信封解析（手工校验，错误结构与业务错误保持一致）
# --------------------------------------------------------------------------

def _require(obj: dict[str, Any], key: str, types: tuple[type, ...], where: str) -> Any:
    if key not in obj:
        raise invalid_request(f"请求缺少必填字段 {key!r}。", {"field": key})
    value = obj[key]
    if not isinstance(value, types):
        raise invalid_request(
            f"字段 {key!r} 类型错误。",
            {"field": key, "expected": where, "actual_type": type(value).__name__})
    return value


def parse_envelope(raw: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str, Any]:
    if not isinstance(raw, dict):
        raise invalid_request("请求体必须是 JSON 对象。")
    plan = _require(raw, "plan", (dict,), "object")
    tables_raw = _require(raw, "tables", (list,), "array")
    idem = _require(raw, "idempotency_key", (str,), "string")
    if not idem.strip():
        raise invalid_request("idempotency_key 不能为空字符串。",
                              {"field": "idempotency_key"})
    counterfactual = raw.get("counterfactual")
    if counterfactual is not None and not isinstance(counterfactual, dict):
        raise invalid_request("counterfactual 必须是对象。",
                              {"field": "counterfactual"})
    if not all(isinstance(t, dict) for t in tables_raw):
        raise invalid_request("tables 必须全部是对象。", {"field": "tables"})
    return plan, tables_raw, idem, counterfactual


def build_tables(payloads: list[dict[str, Any]]) -> dict[str, Table]:
    tables: dict[str, Table] = {}
    for i, payload in enumerate(payloads):
        if not isinstance(payload.get("name"), str):
            raise invalid_request(f"tables[{i}] 缺少字符串字段 'name'。",
                                  {"field": f"tables[{i}].name"})
        if payload["name"] in tables:
            raise invalid_request(f"表名 {payload['name']!r} 在一次请求中重复提交。",
                                  {"table": payload["name"]})
        tables[payload["name"]] = Table.build(payload)
    return tables


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": app.title}


@app.post("/query")
async def query(request: Request) -> Response:
    raw_body = await request.body()
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return error_response(invalid_request("请求体不是合法 UTF-8 JSON。",
                                              {"parse_error": str(exc)}))

    # 幂等账本需要在业务校验之前解析：即使是错误响应也按内容去重。
    try:
        plan, tables_payload, idem_key, counterfactual = parse_envelope(envelope)
    except APIError as err:
        return error_response(err)

    fingerprint = request_fingerprint(plan, tables_payload, counterfactual)
    cached = get_store().lookup(idem_key, fingerprint)
    if cached is not None:
        if cached.get("conflict"):
            return json_response({
                "ok": False,
                "error": {
                    "code": "IDEMPOTENCY_KEY_CONFLICT",
                    "message": "同一幂等键已绑定不同的规范化计划/表快照。",
                    "details": {
                        "idempotency_key": idem_key,
                        "request_fingerprint": fingerprint,
                        "existing_fingerprint": cached["other_fingerprint"],
                    },
                },
            }, status_code=409)
        return Response(content=cached["body"], status_code=cached["status_code"],
                        media_type=cached["content_type"],
                        headers={"X-Idempotent-Replay": "true"})

    response = _process(plan, tables_payload, counterfactual, fingerprint, idem_key)
    body = response.body if isinstance(response.body, (bytes, bytearray)) else canonical_bytes(response.body)
    get_store().store(idem_key, fingerprint, response.status_code,
                      CONTENT_TYPE, bytes(body))
    return response


def _process(plan: dict[str, Any], tables_payload: list[dict[str, Any]],
             counterfactual: dict[str, Any] | None, fingerprint: str,
             idem_key: str) -> Response:
    try:
        tables = build_tables(tables_payload)
        validate_plan(plan, tables)
        executed = execute(plan, tables)
        output = serialize_output(executed)

        result: dict[str, Any] = {
            "ok": True,
            "idempotency_key": idem_key,
            "request_fingerprint": fingerprint,
            "output": output,
            "source_variables": build_variables_legend(output),
        }

        if counterfactual is not None:
            result["counterfactual"] = _build_counterfactual(
                counterfactual, executed, plan, tables, output)

        return json_response(result)
    except APIError as err:
        return error_response(err)


def _build_counterfactual(selector: dict[str, Any], executed: Any,
                          plan: dict[str, Any], tables: dict[str, Table],
                          output: dict[str, Any]) -> dict[str, Any]:
    if "output_index" not in selector:
        raise invalid_request("counterfactual 必须指定 output_index。",
                              {"field": "counterfactual.output_index"})
    index = selector["output_index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise invalid_request("counterfactual.output_index 必须是非负整数。",
                              {"field": "counterfactual.output_index"})

    ordered = sort_rows(list(executed.rows), executed.columns)
    if index >= len(ordered):
        raise target_not_found(index, len(ordered))
    target = ordered[index]
    scenarios = compute_counterfactual(plan, tables, executed, target)

    return {
        "target": {
            "output_index": index,
            "values": {c: target.values.get(c) for c in executed.columns},
        },
        "minimal_deletion_set_count": len(scenarios),
        "scenarios": scenarios,
    }
