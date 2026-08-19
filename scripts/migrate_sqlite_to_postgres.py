"""One-time migration from the legacy SQLite database to PostgreSQL.

The target must be empty. Legacy out-of-range billing rows are excluded and
written to a JSON report; valid ledger snapshots are rebuilt deterministically.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import MetaData, func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.money import (
    MAX_ACCOUNT_BALANCE,
    MAX_RECHARGE_AMOUNT,
    MAX_UNIT_PRICE,
    MIN_RECHARGE_AMOUNT,
    MIN_UNIT_PRICE,
)
from app.database import Base
from app.models import ApiKey, BillingTransaction, EmailTask, Tenant, WebhookLog  # noqa: F401

TABLE_ORDER = ("tenants", "api_keys", "email_tasks", "billing_transactions", "webhook_logs")


def decimal_value(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("NaN")


def valid_unit_amount(value: Any) -> bool:
    amount = decimal_value(value)
    return amount.is_finite() and MIN_UNIT_PRICE <= amount <= MAX_UNIT_PRICE


def valid_recharge_amount(value: Any) -> bool:
    amount = decimal_value(value)
    return amount.is_finite() and MIN_RECHARGE_AMOUNT <= amount <= MAX_RECHARGE_AMOUNT


def chronological_key(row: dict[str, Any]) -> tuple[str, str]:
    """Build a stable key for SQLite values returned as either text or datetime."""
    value = row.get("created_at")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        created_at = value.astimezone(timezone.utc).isoformat()
    else:
        created_at = str(value or "")
    return created_at, str(row.get("id") or "")


def invalidate_task(task: dict[str, Any], reason: str) -> None:
    task["status"] = "FAILED"
    task["is_charged"] = False
    task["charged_amount"] = Decimal("0")
    task["is_reserved"] = False
    task["reserved_amount"] = Decimal("0")
    task["lease_owner"] = None
    task["lease_expires_at"] = None
    task["error_message"] = reason


def rebuild_ledgers(
    tenants: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rejected: list[dict[str, Any]] = []
    invalid_task_ids: set[str] = set()
    valid_transactions: list[dict[str, Any]] = []

    for tenant in tenants:
        tenant_transactions = sorted(
            (row for row in transactions if row.get("tenant_id") == tenant["id"]),
            key=chronological_key,
        )
        running = Decimal("0")
        accepted_for_tenant: list[dict[str, Any]] = []
        for row in tenant_transactions:
            amount = decimal_value(row.get("amount"))
            transaction_type = row.get("type")
            valid = False
            next_balance = running
            if transaction_type in {"RECHARGE", "REFUND"} and valid_recharge_amount(amount):
                next_balance = running + amount
                valid = next_balance <= MAX_ACCOUNT_BALANCE
            elif transaction_type == "DEDUCTION" and valid_unit_amount(amount):
                next_balance = running - amount
                valid = next_balance >= 0
            elif transaction_type == "ADJUSTMENT" and amount.is_finite():
                next_balance = running + amount
                valid = Decimal("0") <= next_balance <= MAX_ACCOUNT_BALANCE

            if not valid:
                rejected.append({**row, "migration_reason": "invalid amount or ledger transition"})
                if row.get("task_id"):
                    invalid_task_ids.add(row["task_id"])
                continue

            rebuilt = dict(row)
            rebuilt["balance_before"] = running
            rebuilt["balance_after"] = next_balance
            accepted_for_tenant.append(rebuilt)
            running = next_balance

        if accepted_for_tenant:
            tenant["balance"] = running
        valid_transactions.extend(accepted_for_tenant)

    for task in tasks:
        if task.get("id") in invalid_task_ids or (
            task.get("is_charged") and not valid_unit_amount(task.get("charged_amount"))
        ):
            invalidate_task(
                task,
                "Legacy out-of-range billing record excluded during PostgreSQL migration",
            )

    # Rebuild reserved balances from the surviving active tasks. This prevents a
    # stale SQLite aggregate from violating reserved_balance <= balance in PostgreSQL.
    for tenant in tenants:
        available = decimal_value(tenant.get("balance"))
        reserved = Decimal("0")
        active_tasks = sorted(
            (
                task
                for task in tasks
                if task.get("tenant_id") == tenant["id"]
                and task.get("status") in {"PENDING", "PROCESSING"}
                and task.get("is_reserved")
            ),
            key=chronological_key,
        )
        for task in active_tasks:
            amount = decimal_value(task.get("reserved_amount"))
            if not valid_unit_amount(amount) or reserved + amount > available:
                invalidate_task(
                    task,
                    "Legacy reservation exceeded the valid PostgreSQL account balance",
                )
                rejected.append(
                    {
                        "id": task.get("id"),
                        "tenant_id": tenant["id"],
                        "reserved_amount": str(amount),
                        "migration_reason": "invalid or unfunded active task reservation",
                    }
                )
                continue
            reserved += amount
        tenant["reserved_balance"] = reserved

    return valid_transactions, rejected


async def migrate(source_path: Path, target_url: str, report_path: Path) -> None:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise SystemExit(f"SQLite source does not exist: {source_path}")
    if not target_url.startswith("postgresql+asyncpg://"):
        raise SystemExit("Target URL must use postgresql+asyncpg://")

    source_engine = create_async_engine(f"sqlite+aiosqlite:///{source_path.as_posix()}")
    target_engine = create_async_engine(target_url, pool_pre_ping=True)
    source_metadata = MetaData()
    try:
        async with source_engine.begin() as connection:
            await connection.run_sync(source_metadata.reflect)
            source_rows: dict[str, list[dict[str, Any]]] = {}
            for table_name in TABLE_ORDER:
                table = source_metadata.tables.get(table_name)
                source_rows[table_name] = (
                    [dict(row) for row in (await connection.execute(select(table))).mappings()]
                    if table is not None
                    else []
                )

        valid_transactions, rejected = rebuild_ledgers(
            source_rows["tenants"],
            source_rows["email_tasks"],
            source_rows["billing_transactions"],
        )
        source_rows["billing_transactions"] = valid_transactions

        async with target_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            existing_rows = 0
            for table_name in TABLE_ORDER:
                existing_rows += (
                    await connection.execute(
                        select(func.count()).select_from(Base.metadata.tables[table_name])
                    )
                ).scalar_one()
            if existing_rows:
                raise SystemExit("PostgreSQL target is not empty; migration aborted")

            for table_name in TABLE_ORDER:
                target_table = Base.metadata.tables[table_name]
                target_columns = set(target_table.columns.keys())
                rows = [
                    {key: value for key, value in row.items() if key in target_columns}
                    for row in source_rows[table_name]
                ]
                if rows:
                    await connection.execute(insert(target_table), rows)
                print(f"Migrated {table_name}: {len(rows)}")

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"Rejected legacy billing rows: {len(rejected)}")
        print(f"Report: {report_path.resolve()}")
    finally:
        await source_engine.dispose()
        await target_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/cargo_service.db"))
    parser.add_argument("--target", default=os.getenv("POSTGRES_MIGRATION_URL", ""))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("data/migration_rejected_billing.json"),
    )
    args = parser.parse_args()
    if not args.target:
        raise SystemExit("Pass --target or set POSTGRES_MIGRATION_URL")
    asyncio.run(migrate(args.source, args.target, args.report))


if __name__ == "__main__":
    main()
