from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .models import Evidence
from .tool_gateway import ArgumentSpec, ToolCallRecord, ToolError, ToolOutput, ToolPolicy, ToolRuntime, ToolSpec


def build_demo_runtime(
    database_path: str | Path | None = None,
    record_sink: Callable[[ToolCallRecord], None] | None = None,
) -> ToolRuntime:
    runtime = ToolRuntime(record_sink=record_sink)
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
                    try:
                        cursor = connection.execute(
                            "INSERT INTO demo_tickets(idempotency_key, title, description) VALUES (?, ?, ?)",
                            (idempotency_key, title, description),
                        )
                        ticket = {"id": f"INC-{cursor.lastrowid:04d}", "title": title, "description": description}
                    except sqlite3.IntegrityError:
                        # Another worker won the same idempotent write race.
                        existing = connection.execute(
                            "SELECT id, title, description FROM demo_tickets WHERE idempotency_key = ?",
                            (idempotency_key,),
                        ).fetchone()
                        if existing is None:
                            raise
                        replayed = True
                        ticket = {
                            "id": f"INC-{existing['id']:04d}",
                            "title": existing["title"],
                            "description": existing["description"],
                        }
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

    runtime.register(
        ToolSpec(
            name="search_docs",
            description="Search internal documents",
            risk_level="low",
            handler=search_docs,
            arguments={
                "query": ArgumentSpec(str, validator=lambda value: bool(value.strip())),
                "top_k": ArgumentSpec(int, required=False, validator=lambda value: 1 <= value <= 20),
            },
            policy=ToolPolicy(timeout_seconds=2.0, max_retries=1, max_concurrency=8, rate_limit_calls=100),
        )
    )
    runtime.register(
        ToolSpec(
            name="query_metrics",
            description="Read-only business metrics query",
            risk_level="low",
            handler=query_metrics,
            arguments={
                "region": ArgumentSpec(str, validator=lambda value: value in {"华东", "华南"}),
                "month": ArgumentSpec(str, required=False, validator=lambda value: bool(re.fullmatch(r"\d{4}-\d{2}", value))),
            },
            policy=ToolPolicy(timeout_seconds=2.0, max_retries=2, max_concurrency=4, rate_limit_calls=60),
        )
    )
    runtime.register(
        ToolSpec(
            name="create_ticket",
            description="Create an IT/operations ticket",
            risk_level="high",
            handler=create_ticket,
            arguments={
                "title": ArgumentSpec(str, validator=lambda value: 1 <= len(value) <= 200),
                "description": ArgumentSpec(str, validator=lambda value: bool(value.strip())),
                "approval_token": ArgumentSpec(str),
                "idempotency_key": ArgumentSpec(str, validator=lambda value: bool(value.strip())),
            },
            policy=ToolPolicy(
                timeout_seconds=3.0,
                max_retries=1,
                max_concurrency=2,
                rate_limit_calls=20,
                requires_approval=True,
                idempotency_required=True,
            ),
        )
    )
    return runtime
