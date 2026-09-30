"""响应装配：排序输出、渲染见证集与布尔溯源表达式。

溯源表达式采用经典布尔（How-provenance）DNF 记法：

* 单个合取项的变量用 ``" * "`` 连接；
* 多个合取项用 ``" + "`` 连接，超过一项时整体加括号；
* 见证集合按“项大小、变量字典序”排序，保证字节一致。
"""
from __future__ import annotations

from typing import Any

from .canonical import canonical_dumps
from .engine import ExecutedPlan
from .models import Row


def render_witness(witness: frozenset[str]) -> list[str]:
    return sorted(witness)


def render_dnf(witnesses: set[frozenset[str]]) -> str:
    terms = sorted((sorted(w) for w in witnesses), key=lambda term: (len(term), term))
    rendered_terms = ["(" + " * ".join(term) + ")" if len(term) > 1 else (term[0] if term else "()")
                      for term in terms]
    if len(rendered_terms) <= 1:
        return rendered_terms[0] if rendered_terms else ""
    return " + ".join(rendered_terms)


def sort_rows(rows: list[Row], columns: list[str]) -> list[Row]:
    return sorted(rows, key=lambda r: [canonical_dumps(r.values.get(c)) for c in columns])


def serialize_output(executed: ExecutedPlan) -> dict[str, Any]:
    rows = sort_rows(list(executed.rows), executed.columns)
    out_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        witnesses = [render_witness(w) for w in
                     sorted(row.witnesses, key=lambda s: (len(s), sorted(s)))]
        out_rows.append({
            "output_index": index,
            "values": {c: row.values.get(c) for c in executed.columns},
            "witness_sets": witnesses,
            "witness_count": len(witnesses),
            "provenance_expr": render_dnf(row.witnesses),
        })
    return {
        "columns": list(executed.columns),
        "row_count": len(out_rows),
        "rows": out_rows,
    }


def build_variables_legend(output: dict[str, Any]) -> list[dict[str, Any]]:
    """收集响应中出现的全部来源变量并排序，便于客户端解码。"""
    import re
    variables: set[str] = set()
    for row in output["rows"]:
        for witness in row["witness_sets"]:
            variables.update(witness)
    legend = []
    for var in sorted(variables):
        match = re.match(r"^(?P<table>[^\[]+)\[(?P<keys>.*)\]$", var)
        legend.append({"variable": var,
                       "table": match.group("table") if match else None})
    return legend
