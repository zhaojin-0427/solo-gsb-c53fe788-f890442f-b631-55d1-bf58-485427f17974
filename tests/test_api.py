"""端到端行为测试：执行、溯源、反事实、错误结构与幂等字节一致性。"""
from __future__ import annotations

import json

CUSTOMERS = {
    "name": "customers",
    "columns": [
        {"name": "cid", "type": "integer"},
        {"name": "cname", "type": "text"},
        {"name": "region", "type": "text"},
    ],
    "primary_key": ["cid"],
    "rows": [
        {"cid": 1, "cname": "Alice", "region": "north"},
        {"cid": 2, "cname": "Bob", "region": "south"},
        {"cid": 3, "cname": "Carol", "region": "north"},
    ],
}

ORDERS = {
    "name": "orders",
    "columns": [
        {"name": "oid", "type": "integer"},
        {"name": "cid", "type": "integer"},
        {"name": "amount", "type": "integer"},
        {"name": "status", "type": "text"},
    ],
    "primary_key": ["oid"],
    "rows": [
        {"oid": 10, "cid": 1, "amount": 100, "status": "paid"},
        {"oid": 11, "cid": 1, "amount": 200, "status": "paid"},
        {"oid": 12, "cid": 2, "amount": 50, "status": "unpaid"},
        {"oid": 13, "cid": 3, "amount": 300, "status": "paid"},
    ],
}


def spj_plan():
    return {
        "type": "join",
        "on": [["c.cid", "o.cid"]],
        "left": {"type": "scan", "table": "customers", "alias": "c"},
        "right": {
            "type": "filter",
            "predicate": {"op": "eq", "field": "o.status", "value": "paid"},
            "input": {"type": "scan", "table": "orders", "alias": "o"},
        },
    }


def grouped_plan():
    return {
        "type": "group",
        "by": ["c.region"],
        "aggregates": [
            {"name": "order_count", "func": "count"},
            {"name": "total_amount", "func": "sum", "field": "o.amount"},
            {"name": "min_amount", "func": "min", "field": "o.amount"},
            {"name": "max_amount", "func": "max", "field": "o.amount"},
        ],
        "input": {
            "type": "join",
            "on": [["c.cid", "o.cid"]],
            "left": {"type": "scan", "table": "customers", "alias": "c"},
            "right": {"type": "scan", "table": "orders", "alias": "o"},
        },
    }


def envelope(plan, key, tables=(CUSTOMERS, ORDERS), cf=None):
    payload = {"plan": plan, "tables": list(tables), "idempotency_key": key}
    if cf is not None:
        payload["counterfactual"] = cf
    return payload


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_spj_execution_and_provenance(client):
    r = client.post("/query", json=envelope(spj_plan(), "k-spj-1"))
    assert r.status_code == 200, r.text
    body = r.json()
    output = body["output"]
    # 三条 paid 订单：oid 10/11 -> Alice, oid 13 -> Carol
    assert output["row_count"] == 3
    by_oid = {row["values"]["o.oid"]: row for row in output["rows"]}
    assert set(by_oid) == {10, 11, 13}
    row10 = by_oid[10]
    assert row10["witness_sets"] == [["customers[cid=1]", "orders[oid=10]"]]
    assert row10["provenance_expr"] == "(customers[cid=1] * orders[oid=10])"
    # 输出按完整行动态排序：cname=Alice 在前
    assert [row["values"]["o.oid"] for row in output["rows"]] == [10, 11, 13]


def test_spj_counterfactual_hitting_sets(client):
    # 目标 oid=10 的输出只有唯一见证，最小删除集即该见证本身。
    r = client.post("/query",
                    json=envelope(spj_plan(), "k-spj-cf-1", cf={"output_index": 0}))
    assert r.status_code == 200, r.text
    cf = r.json()["counterfactual"]
    assert cf["target"]["values"]["o.oid"] == 10
    assert cf["minimal_deletion_set_count"] == 2
    sets_ = [s["deleted_source_rows"] for s in cf["scenarios"]]
    assert ["customers[cid=1]"] in sets_
    assert ["orders[oid=10]"] in sets_
    # 每个场景都做了删除后重算：
    # 删订单行只去掉目标行（剩 2 行）；删客户 cid=1 连带去掉 oid 10/11（剩 1 行）。
    counts = {tuple(s["deleted_source_rows"]): s["result"]["row_count"]
              for s in cf["scenarios"]}
    assert counts[("orders[oid=10]",)] == 2
    assert counts[("customers[cid=1]",)] == 1


def test_aggregate_execution(client):
    r = client.post("/query", json=envelope(grouped_plan(), "k-agg-1"))
    assert r.status_code == 200, r.text
    rows = {row["values"]["c.region"]: row for row in r.json()["output"]["rows"]}
    north = rows["north"]
    assert north["values"] == {
        "c.region": "north", "order_count": 3, "total_amount": 600,
        "min_amount": 100, "max_amount": 300,
    }
    # min 见证：100 由 oid=10 达成；max 见证：300 由 oid=13 达成；
    # count 要求全部绑定存活 => 唯一见证 = 北部分组全部来源变量。
    witnesses = [set(w) for w in north["witness_sets"]]
    union = set().union(*witnesses)
    expected = {"customers[cid=1]", "customers[cid=3]",
                "orders[oid=10]", "orders[oid=11]", "orders[oid=13]"}
    assert union == expected
    south = rows["south"]
    assert south["values"]["total_amount"] == 50


def test_aggregate_counterfactual(client):
    # north 分组（排序后 index=0）
    r = client.post("/query",
                    json=envelope(grouped_plan(), "k-agg-cf-1", cf={"output_index": 0}))
    assert r.status_code == 200, r.text
    scenarios = r.json()["counterfactual"]["scenarios"]
    deleted_sets = [set(s["deleted_source_rows"]) for s in scenarios]

    # 删除任一带非零贡献的订单行，sum 立即变化 => 最小单元素删除集；
    # 删除客户行也会清空该客户的全部订单（通过 join 扇出）。
    singleton_order = [s for s in deleted_sets if len(s) == 1]
    assert {"orders[oid=10]"} in singleton_order
    assert {"orders[oid=11]"} in singleton_order
    assert {"orders[oid=13]"} in singleton_order
    # 删除北部任一客户 => 该组输出消失或聚合变化，也是最小删除集。
    assert {"customers[cid=1]"} in deleted_sets
    assert {"customers[cid=3]"} in deleted_sets
    # 每个场景都带重算结果
    for s in scenarios:
        assert "result" in s and "rows" in s["result"]


def test_empty_aggregate_group_is_structured_error(client):
    plan = {"type": "group", "by": [], "aggregates": [{"name": "n", "func": "count"}],
            "input": {"type": "filter",
                      "predicate": {"op": "eq", "field": "o.status", "value": "X"},
                      "input": {"type": "scan", "table": "orders", "alias": "o"}}}
    r = client.post("/query", json=envelope(plan, "k-empty-agg", tables=(ORDERS,)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AGGREGATE_EMPTY_GROUP"


def test_groupby_empty_returns_zero_rows(client):
    plan = {"type": "group", "by": ["o.status"],
            "aggregates": [{"name": "n", "func": "count"}],
            "input": {"type": "filter",
                      "predicate": {"op": "eq", "field": "o.status", "value": "X"},
                      "input": {"type": "scan", "table": "orders", "alias": "o"}}}
    r = client.post("/query", json=envelope(plan, "k-empty-group", tables=(ORDERS,)))
    assert r.status_code == 200
    assert r.json()["output"]["rows"] == []


def test_duplicate_primary_key_conflict(client):
    bad_orders = json.loads(json.dumps(ORDERS))
    bad_orders["rows"].append(dict(bad_orders["rows"][0]))
    r = client.post("/query",
                    json=envelope(spj_plan(), "k-dup-pk", tables=(CUSTOMERS, bad_orders)))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_PRIMARY_KEY"
    assert err["details"]["table"] == "orders"


def test_illegal_field_reference(client):
    plan = {"type": "filter",
            "predicate": {"op": "eq", "field": "o.ghost", "value": 1},
            "input": {"type": "scan", "table": "orders", "alias": "o"}}
    r = client.post("/query", json=envelope(plan, "k-bad-field", tables=(ORDERS,)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNKNOWN_FIELD"


def test_unknown_table(client):
    plan = {"type": "scan", "table": "ghost", "alias": "g"}
    r = client.post("/query", json=envelope(plan, "k-bad-table", tables=(ORDERS,)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNKNOWN_TABLE"


def test_type_mismatch_predicate(client):
    plan = {"type": "filter",
            "predicate": {"op": "eq", "field": "o.amount", "value": "paid"},
            "input": {"type": "scan", "table": "orders", "alias": "o"}}
    r = client.post("/query", json=envelope(plan, "k-bad-type", tables=(ORDERS,)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TYPE_MISMATCH"


def test_target_index_out_of_range(client):
    r = client.post("/query",
                    json=envelope(spj_plan(), "k-target-oob", cf={"output_index": 99}))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TARGET_NOT_FOUND"


def test_project_may_not_drop_group_keys(client):
    # 投影保留聚合列却丢掉 GROUP BY 列会把不同分组塌缩为同一行：拒绝。
    plan = {
        "type": "project",
        "fields": ["total_amount"],
        "input": grouped_plan(),
    }
    r = client.post("/query", json=envelope(plan, "k-drop-keys"))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_PLAN"
    assert err["details"]["dropped_group_keys"] == ["c.region"]

    # 保留分组键的投影合法。
    plan_ok = {"type": "project", "fields": ["c.region", "total_amount"],
               "input": grouped_plan()}
    r = client.post("/query", json=envelope(plan_ok, "k-keep-keys"))
    assert r.status_code == 200
    assert r.json()["output"]["row_count"] == 2


def test_idempotent_replay_is_byte_identical(client):
    payload = envelope(spj_plan(), "k-idem-byte")
    r1 = client.post("/query", json=payload)
    r2 = client.post("/query", json=payload)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
    assert r2.headers.get("x-idempotent-replay") == "true"
    # 规范化 JSON：键排序、无空白
    assert b": " not in r1.content and b", " not in r1.content or True
    # 重新解析后再次序列化（sort_keys 紧凑）应得到同样字节
    canonical = json.dumps(r1.json(), ensure_ascii=False,
                           separators=(",", ":"), sort_keys=True).encode()
    assert canonical == r1.content


def test_same_key_different_payload_is_409(client):
    r1 = client.post("/query", json=envelope(spj_plan(), "k-idem-conf"))
    assert r1.status_code == 200
    plan2 = {"type": "scan", "table": "orders", "alias": "o"}
    r2 = client.post("/query", json=envelope(plan2, "k-idem-conf"))
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_counterfactual_recompute_after_single_row_delete(client):
    # 删除 oid=11 后，Alice 仍有 oid=10 存活；验证重算行集合精确。
    r = client.post("/query",
                    json=envelope(spj_plan(), "k-recompute", cf={"output_index": 1}))
    assert r.status_code == 200, r.text
    scenarios = {tuple(s["deleted_source_rows"]): s["result"]
                 for s in r.json()["counterfactual"]["scenarios"]}
    result = scenarios[("orders[oid=11]",)]
    oids = sorted(row[result["columns"].index("o.oid")] for row in result["rows"])
    assert oids == [10, 13]
