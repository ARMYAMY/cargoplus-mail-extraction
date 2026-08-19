"""Rebuild one tenant's balance from valid ledger entries.

The command is dry-run by default. Use ``--apply`` only after reviewing the
printed totals. A consistent SQLite backup is created before any update.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.money import (
    MAX_ACCOUNT_BALANCE,
    MAX_RECHARGE_AMOUNT,
    MAX_UNIT_PRICE,
    MIN_RECHARGE_AMOUNT,
    MIN_UNIT_PRICE,
    MONEY_QUANTUM,
)


def as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def is_recharge_amount_valid(amount: Decimal) -> bool:
    return amount.is_finite() and MIN_RECHARGE_AMOUNT <= amount <= MAX_RECHARGE_AMOUNT


def is_unit_amount_valid(amount: Decimal) -> bool:
    return amount.is_finite() and MIN_UNIT_PRICE <= amount <= MAX_UNIT_PRICE


def reconcile(database_path: Path, tenant_id: str, *, apply: bool) -> None:
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise SystemExit(f"Database does not exist: {database_path}")

    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    tenant = connection.execute(
        "SELECT id, name, balance, reserved_balance, unit_price FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    if tenant is None:
        raise SystemExit(f"Tenant does not exist: {tenant_id}")

    unit_price = as_decimal(tenant["unit_price"])
    if not is_unit_amount_valid(unit_price):
        raise SystemExit(f"Tenant unit price is invalid and must be fixed first: {unit_price}")

    valid_credit = Decimal("0")
    valid_debit = Decimal("0")
    excluded_transactions: list[tuple[str, str, Decimal]] = []
    rows = connection.execute(
        "SELECT id, type, amount FROM billing_transactions WHERE tenant_id = ? ORDER BY created_at, id",
        (tenant_id,),
    ).fetchall()
    for row in rows:
        amount = as_decimal(row["amount"])
        transaction_type = row["type"]
        if transaction_type in {"RECHARGE", "REFUND"} and is_recharge_amount_valid(amount):
            valid_credit += amount
        elif transaction_type == "DEDUCTION" and is_unit_amount_valid(amount):
            valid_debit += amount
        elif transaction_type == "ADJUSTMENT" and amount.is_finite():
            valid_credit += amount
        else:
            excluded_transactions.append((row["id"], transaction_type, amount))

    rebuilt_balance = (valid_credit - valid_debit).quantize(MONEY_QUANTUM)
    if rebuilt_balance < 0 or rebuilt_balance > MAX_ACCOUNT_BALANCE:
        raise SystemExit(f"Rebuilt balance is outside the supported range: {rebuilt_balance}")

    active_reservations = connection.execute(
        """
        SELECT id, reserved_amount
        FROM email_tasks
        WHERE tenant_id = ? AND is_reserved = 1 AND status IN ('PENDING', 'PROCESSING')
        ORDER BY id
        """,
        (tenant_id,),
    ).fetchall()
    normalized_task_ids: list[str] = []
    rebuilt_reserved = Decimal("0")
    for task in active_reservations:
        reserved_amount = as_decimal(task["reserved_amount"])
        if not is_unit_amount_valid(reserved_amount):
            reserved_amount = unit_price
            normalized_task_ids.append(task["id"])
        rebuilt_reserved += reserved_amount
    rebuilt_reserved = rebuilt_reserved.quantize(MONEY_QUANTUM)
    if rebuilt_reserved > rebuilt_balance:
        raise SystemExit(
            f"Rebuilt reservations {rebuilt_reserved} exceed rebuilt balance {rebuilt_balance}"
        )

    print(f"Tenant: {tenant_id} ({tenant['name']})")
    print(f"Stored balance: {tenant['balance']} -> rebuilt balance: {rebuilt_balance}")
    print(f"Stored reserved: {tenant['reserved_balance']} -> rebuilt reserved: {rebuilt_reserved}")
    print(f"Valid credits: {valid_credit}; valid debits: {valid_debit}")
    print(f"Excluded transactions: {len(excluded_transactions)}")
    print(f"Normalized active reservations: {len(normalized_task_ids)}")
    if not apply:
        print("Dry run only; no data changed. Re-run with --apply to commit this reconciliation.")
        return

    backup_directory = database_path.parent / "backups"
    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_directory / f"{database_path.stem}-{tenant_id}-{timestamp}.db"
    with sqlite3.connect(backup_path) as backup_connection:
        connection.backup(backup_connection)

    try:
        connection.execute("BEGIN IMMEDIATE")
        for task_id in normalized_task_ids:
            connection.execute(
                "UPDATE email_tasks SET reserved_amount = ? WHERE id = ? AND tenant_id = ?",
                (str(unit_price), task_id, tenant_id),
            )
        connection.execute(
            "UPDATE tenants SET balance = ?, reserved_balance = ? WHERE id = ?",
            (str(rebuilt_balance), str(rebuilt_reserved), tenant_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(f"Reconciliation committed. Backup: {backup_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant_id")
    parser.add_argument("--database", type=Path, default=Path("data/cargo_service.db"))
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    reconcile(arguments.database, arguments.tenant_id, apply=arguments.apply)


if __name__ == "__main__":
    main()
