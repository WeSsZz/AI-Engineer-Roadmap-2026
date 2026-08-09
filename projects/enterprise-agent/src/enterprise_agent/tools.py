from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import Evidence


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolOutput:
    tool_name: str
    status: str
    data: dict[str, Any]
    evidence: tuple[Evidence, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk_level: str
    handler: Callable[..., ToolOutput]


class ToolRuntime:
    """Allowlisted tool runtime with an explicit write boundary."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def invoke(
        self,
        name: str,
        *,
        allowed_tools: tuple[str, ...],
        arguments: dict[str, Any],
    ) -> ToolOutput:
        if name not in allowed_tools:
            raise ToolError(f"Tool '{name}' is not allowed for this task")
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"Unknown tool '{name}'")
        return spec.handler(**arguments)


def build_demo_runtime(database_path: str | Path | None = None) -> ToolRuntime:
    runtime = ToolRuntime()
    ticket_database = Path(database_path) if database_path is not None else None
    in_memory_tickets: dict[str, dict[str, Any]] = {}
    if ticket_database is not None:
        ticket_database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(ticket_database) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    documents = [
        {
            "id": "doc-it-vpn-001",
            "title": "VPN 连接故障排查手册",
            "text": "先确认账号未锁定，再检查客户端版本、网络出口和证书。连续失败三次需要创建 IT 工单。",
            "type": "runbook",
        },
        {
            "id": "doc-ops-stock-001",
            "title": "库存异常处理规范",
            "text": "当区域缺货率超过 10% 且订单量同比下降时，优先检查补货计划和仓库可用库存；补货工单需要运营负责人审批。",
            "type": "policy",
        },
        {
            "id": "doc-ops-order-001",
            "title": "订单异常分析口径",
            "text": "订单下降需要同时检查订单量、缺货率、取消率和履约延迟。结论必须附带查询时间范围和数据来源。",
            "type": "metric_definition",
        },
    ]

    metrics = [
        {"region": "华东", "month": "2026-07", "orders": 1000, "stockout_rate": 0.14, "cancel_rate": 0.03},
        {"region": "华东", "month": "2026-06", "orders": 1220, "stockout_rate": 0.04, "cancel_rate": 0.02},
        {"region": "华南", "month": "2026-07", "orders": 1180, "stockout_rate": 0.05, "cancel_rate": 0.02},
    ]
    def search_docs(query: str, top_k: int = 3) -> ToolOutput:
        # A production version should use an analyzer/tokenizer. Keep the demo
        # dependency-free while still handling Chinese business keywords.
        query_lower = query.lower()
        chinese_keywords = ("库存", "订单", "下降", "补货", "工单", "缺货", "故障", "vpn", "权限", "取消率", "履约")
        terms = set(re.findall(r"[a-z0-9_]+", query_lower))
        terms.update(keyword for keyword in chinese_keywords if keyword in query_lower)
        ranked = []
        for doc in documents:
            haystack = f"{doc['title']} {doc['text']}".lower()
            score = sum(1 for term in terms if term and term in haystack)
            if score:
                ranked.append((score, doc))
        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = [doc for _, doc in ranked[:top_k]]
        evidence = tuple(
            Evidence(
                evidence_id=doc["id"],
                source_type="document",
                title=doc["title"],
                excerpt=doc["text"],
                locator=f"docs://{doc['id']}",
                metadata={"score": score},
            )
            for score, doc in ranked[:top_k]
        )
        return ToolOutput(
            tool_name="search_docs",
            status="completed",
            data={"matches": selected, "count": len(selected)},
            evidence=evidence,
            message="Document search completed",
        )

    def query_metrics(region: str, month: str = "2026-07") -> ToolOutput:
        rows = [row for row in metrics if row["region"] == region and row["month"] in {month, "2026-06"}]
        rows.sort(key=lambda row: row["month"], reverse=True)
        evidence = tuple(
            Evidence(
                evidence_id=f"metric-{row['region']}-{row['month']}",
                source_type="metric_query",
                title=f"订单指标 {row['region']} {row['month']}",
                excerpt=str(row),
                locator=f"metrics://orders?region={region}&month={row['month']}",
                metadata={"read_only": True},
            )
            for row in rows
        )
        return ToolOutput(
            tool_name="query_metrics",
            status="completed",
            data={"rows": rows, "read_only": True},
            evidence=evidence,
            message="Read-only metrics query completed",
        )

    def create_ticket(
        title: str,
        description: str,
        approval_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolOutput:
        if approval_token != "APPROVED_BY_USER":
            return ToolOutput(
                tool_name="create_ticket",
                status="needs_approval",
                data={"title": title, "description": description},
                message="Write action prepared; explicit human approval is required",
            )
        if not idempotency_key:
            raise ToolError("Write tools require an idempotency_key")

        replayed = False
        if ticket_database is None:
            ticket = in_memory_tickets.get(idempotency_key)
            if ticket is None:
                ticket = {"id": f"INC-{len(in_memory_tickets) + 1:04d}", "title": title, "description": description}
                in_memory_tickets[idempotency_key] = ticket
            else:
                replayed = True
        else:
            with sqlite3.connect(ticket_database, timeout=5.0) as connection:
                connection.row_factory = sqlite3.Row
                existing = connection.execute(
                    "SELECT id, title, description FROM demo_tickets WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        "INSERT INTO demo_tickets(idempotency_key, title, description) VALUES (?, ?, ?)",
                        (idempotency_key, title, description),
                    )
                    ticket = {"id": f"INC-{cursor.lastrowid:04d}", "title": title, "description": description}
                else:
                    replayed = True
                    ticket = {
                        "id": f"INC-{existing['id']:04d}",
                        "title": existing["title"],
                        "description": existing["description"],
                    }
        return ToolOutput(
            tool_name="create_ticket",
            status="completed",
            data={**ticket, "idempotency_key": idempotency_key, "replayed": replayed},
            message="Existing ticket replayed" if replayed else "Ticket created",
        )

    runtime.register(ToolSpec("search_docs", "Search internal documents", "low", search_docs))
    runtime.register(ToolSpec("query_metrics", "Read-only business metrics query", "low", query_metrics))
    runtime.register(ToolSpec("create_ticket", "Create an IT/operations ticket", "high", create_ticket))
    return runtime
