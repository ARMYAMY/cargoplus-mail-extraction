#!/usr/bin/env python3
"""
CargoPlus Multi-Task Concurrency & Stress Testing Benchmark Script.
Simulates high-concurrency batch mail extraction, measuring throughput, SLA latency, and billing deduction.
"""

import argparse
import asyncio
from datetime import datetime
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.limits import (
    MAX_BENCHMARK_TASKS,
    MAX_TENANT_CONCURRENCY,
    MIN_BENCHMARK_TASKS,
    MIN_TENANT_CONCURRENCY,
)

if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

SAMPLE_DOCS = [
    {
        "subject": "Booking Confirmation - MSC - Yantian to Hamburg",
        "body": "Please confirm container booking. Freight prepaid.",
        "text": "SHIPPER: GLORY SHIPPING CO., LTD.\nADD: RM 1201, TOWER A, SHENZHEN\nTEL: +86 755 88881111\nCONSIGNEE: EURO LOGISTICS GMBH\nPOL: YANTIAN\nPOD: HAMBURG\nVESSEL/VOYAGE: MSC MAYA / 2304W\nETD: 2026/09/01\nCONTAINER: MSCU9988776 / SEAL555 / 40HQ\nGOODS: AUTOMOTIVE PARTS 汽车零部件\nHS CODE: 8708299000\nPACKAGES: 420 CARTONS\nG.W.: 8,500.00 KGS\nMEAS: 58.5 CBM",
    },
    {
        "subject": "Bkg Ref: COSCO - Ningbo to Long Beach",
        "body": "FCL shipment booking. Movement CY-CY.",
        "text": "SHIPPER: NINGBO TEXTILE CORP.\nADD: NO.88 INDUSTRIAL PARK, NINGBO\nTEL: 0574-87654321\nCONSIGNEE: PACIFIC APPAREL LLC\nPOL: NINGBO\nPOD: LONG BEACH\nVESSEL/VOYAGE: CSCL PACIFIC / 055E\nETD: 2026/09/10\nCONTAINER: CCLU1234567 / SEAL999 / 20GP\nGOODS: COTTON SHIRTS 纯棉衬衫\nHS CODE: 6109100000\nPACKAGES: 800 CARTONS\nG.W.: 6,200.00 KGS\nMEAS: 28.0 CBM",
    },
    {
        "subject": "Booking Memo - CMA CGM - Qingdao to Rotterdam",
        "body": "Reefer container booking. Temperature set -18C.",
        "text": "SHIPPER: QINGDAO SEAFOOD EXPORT LTD\nADD: PORT AREA, QINGDAO\nTEL: +86 532 88889999\nCONSIGNEE: ROTTERDAM COLD STORE BV\nPOL: QINGDAO\nPOD: ROTTERDAM\nVESSEL/VOYAGE: CMA CGM ANTOINE / 102W\nETD: 2026/08/25\nCONTAINER: CMAU5566778 / SEAL777 / 40RF\nGOODS: FROZEN FISH 冷冻鱼\nHS CODE: 0303890000\nPACKAGES: 1200 BOXES\nG.W.: 22,000.00 KGS\nMEAS: 60.0 CBM",
    },
]


async def fetch_tenant_info(client: httpx.AsyncClient, base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    res = await client.get(f"{base_url}/admin/tenants", headers=headers)
    res.raise_for_status()
    tenants = res.json()
    if not tenants:
        raise RuntimeError("No tenant available in database. Create a tenant first.")
    return tenants[0]


async def submit_single_task(
    client: httpx.AsyncClient,
    base_url: str,
    headers: Dict[str, str],
    doc_index: int,
) -> Dict[str, Any]:
    sample = SAMPLE_DOCS[doc_index % len(SAMPLE_DOCS)]
    payload = {
        "mail_subject": f"[Batch #{doc_index+1}] {sample['subject']}",
        "mail_body": sample["body"],
        "attachments": [
            {
                "filename": f"booking_{doc_index+1}.pdf",
                "content_type": "application/pdf",
                "text": sample["text"],
                "tables": [],
                "ocr_text": "",
            }
        ],
    }

    t0 = time.time()
    resp = await client.post(f"{base_url}/api/v1/extract/async", json=payload, headers=headers)
    submit_duration = (time.time() - t0) * 1000

    if resp.status_code != 200:
        return {
            "index": doc_index,
            "status": "SUBMIT_FAILED",
            "status_code": resp.status_code,
            "submit_duration_ms": submit_duration,
            "error": resp.text,
        }

    data = resp.json()
    task_id = data.get("task_id")
    return {
        "index": doc_index,
        "status": "SUBMITTED",
        "task_id": task_id,
        "submit_duration_ms": submit_duration,
        "submitted_at": time.time(),
    }


async def poll_task_completion(
    client: httpx.AsyncClient,
    base_url: str,
    headers: Dict[str, str],
    task_info: Dict[str, Any],
    timeout_seconds: float = 300.0,
) -> Dict[str, Any]:
    task_id = task_info.get("task_id")
    if not task_id:
        return task_info

    start_poll = time.time()
    while time.time() - start_poll < timeout_seconds:
        await asyncio.sleep(1.0)
        try:
            resp = await client.get(f"{base_url}/admin/tasks?search={task_id}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                task = next((t for t in items if t["id"] == task_id), None)
                if task and task.get("status") in {"SUCCESS", "FAILED"}:
                    total_time = (time.time() - task_info["submitted_at"]) * 1000
                    return {
                        **task_info,
                        "status": task["status"],
                        "charged_amount": float(task.get("charged_amount") or 0),
                        "duration_ms": task.get("duration_ms"),
                        "total_latency_ms": total_time,
                        "error": task.get("error_message"),
                    }
        except Exception:
            pass

    return {
        **task_info,
        "status": "TIMEOUT",
        "total_latency_ms": (time.time() - task_info["submitted_at"]) * 1000,
    }


async def run_benchmark(
    base_url: str,
    total_tasks: int,
    concurrency_limit: int,
    api_key: str = "",
    admin_secret: str = "",
):
    print("=" * 70)
    print(f"[BENCHMARK] CargoPlus Multi-Task Concurrency Stress Test")
    print(f"Target URL: {base_url}")
    print(f"Total Tasks: {total_tasks} emails")
    print(f"Client Concurrency Limit: {concurrency_limit}")
    print(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    headers = {
        "Content-Type": "application/json",
        "X-Admin-Secret": admin_secret,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Check initial balance & status
        tenant = await fetch_tenant_info(client, base_url, headers)
        initial_balance = float(tenant["balance"])
        unit_price = float(tenant["unit_price"])
        print(f"Tenant: {tenant['name']} (ID: {tenant['id']})")
        print(f"Initial Balance: RMB {initial_balance:.2f} | Unit Price: RMB {unit_price:.2f} / call | Max Concurrency: {tenant['max_concurrency']}")
        print("-" * 70)

        # Step 2: Batch Submit Tasks
        semaphore = asyncio.Semaphore(concurrency_limit)

        async def bounded_submit(idx):
            async with semaphore:
                return await submit_single_task(client, base_url, headers, idx)

        print(f"Submitting {total_tasks} extraction tasks to async queue...")
        t_start_all = time.time()

        submit_tasks = [bounded_submit(i) for i in range(total_tasks)]
        submit_results = await asyncio.gather(*submit_tasks)

        t_submit_done = time.time()
        submit_elapsed = t_submit_done - t_start_all
        successful_submits = [r for r in submit_results if r["status"] == "SUBMITTED"]
        print(f"Submission Finished: {len(successful_submits)}/{total_tasks} enqueued in {submit_elapsed:.2f}s ({total_tasks/submit_elapsed:.1f} QPS)")
        print("-" * 70)

        # Step 3: Poll until all tasks complete in background workers
        print("Waiting for background workers to process tasks...")
        poll_semaphore = asyncio.Semaphore(concurrency_limit * 2)

        async def bounded_poll(task_res):
            async with poll_semaphore:
                return await poll_task_completion(client, base_url, headers, task_res)

        poll_tasks = [bounded_poll(r) for r in submit_results]
        final_results = await asyncio.gather(*poll_tasks)

        t_all_done = time.time()
        total_wall_time = t_all_done - t_start_all

        # Step 4: Final Balance Check
        updated_tenant = await fetch_tenant_info(client, base_url, headers)
        final_balance = float(updated_tenant["balance"])
        total_deducted = initial_balance - final_balance

        # Step 5: Statistics & Analysis
        success_tasks = [r for r in final_results if r["status"] == "SUCCESS"]
        failed_tasks = [r for r in final_results if r["status"] == "FAILED"]
        timeout_tasks = [r for r in final_results if r["status"] == "TIMEOUT"]

        latencies = [r["total_latency_ms"] for r in success_tasks if "total_latency_ms" in r]
        durations = [r["duration_ms"] for r in success_tasks if r.get("duration_ms")]

        print("=" * 70)
        print("BENCHMARK TEST REPORT SUMMARY")
        print("=" * 70)
        print(f"Total Tasks:          {total_tasks}")
        print(f"Success Count:        {len(success_tasks)} ({len(success_tasks)/total_tasks*100:.1f}%)")
        print(f"Failed Count:         {len(failed_tasks)}")
        print(f"Timeout Count:        {len(timeout_tasks)}")
        print(f"Total Wall Time:      {total_wall_time:.2f} seconds")
        if total_wall_time > 0:
            print(f"Throughput (TPS):     {len(success_tasks)/total_wall_time:.2f} emails/sec (approx {len(success_tasks)/total_wall_time*86400:.0f} emails/day)")
        print("-" * 70)

        if durations:
            print(f"Task LLM Duration:    Avg {statistics.mean(durations):.0f} ms | Min {min(durations)} ms | Max {max(durations)} ms")
        if latencies:
            latencies_sorted = sorted(latencies)
            p50 = latencies_sorted[int(len(latencies_sorted) * 0.50)]
            p90 = latencies_sorted[int(len(latencies_sorted) * 0.90)]
            p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)]
            print(f"End-to-End Latency:   P50 = {p50:.0f} ms | P90 = {p90:.0f} ms | P99 = {p99:.0f} ms")

        print("-" * 70)
        print(f"Billing & Ledger Reconciliation:")
        print(f"   - Expected Deducted:  RMB {len(success_tasks) * unit_price:.2f} ({len(success_tasks)} successes * RMB {unit_price:.2f})")
        print(f"   - Actual Deducted:    RMB {total_deducted:.2f}")
        print(f"   - Final Balance:      RMB {final_balance:.2f}")
        if abs(total_deducted - (len(success_tasks) * unit_price)) < 0.001:
            print("   - Reconciliation:     PASSED (100% accurate, 0 error)")
        else:
            print("   - Reconciliation:     MISMATCH detected")

        print("=" * 70)


def main():
    def bounded_integer(value: str, *, minimum: int, maximum: int, label: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
        if parsed < minimum or parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{label} must be between {minimum} and {maximum}"
            )
        return parsed

    parser = argparse.ArgumentParser(description="CargoPlus Multi-Task Concurrency Stress Test")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Service Base URL")
    parser.add_argument(
        "--tasks",
        type=lambda value: bounded_integer(
            value,
            minimum=MIN_BENCHMARK_TASKS,
            maximum=MAX_BENCHMARK_TASKS,
            label="tasks",
        ),
        default=20,
        help=f"Total number of tasks ({MIN_BENCHMARK_TASKS}-{MAX_BENCHMARK_TASKS})",
    )
    parser.add_argument(
        "--concurrency",
        type=lambda value: bounded_integer(
            value,
            minimum=MIN_TENANT_CONCURRENCY,
            maximum=MAX_TENANT_CONCURRENCY,
            label="concurrency",
        ),
        default=10,
        help=f"Client submit concurrency ({MIN_TENANT_CONCURRENCY}-{MAX_TENANT_CONCURRENCY})",
    )
    parser.add_argument("--key", default="", help="Optional API Key")
    parser.add_argument(
        "--admin-secret",
        default=os.getenv("ADMIN_SECRET_KEY", ""),
        help="Admin secret (defaults to ADMIN_SECRET_KEY environment variable)",
    )
    parser.add_argument(
        "--confirm-billable-load-test",
        action="store_true",
        help="Required acknowledgement that this creates real billable LLM tasks",
    )
    args = parser.parse_args()
    if args.concurrency > args.tasks:
        parser.error("concurrency cannot exceed the number of tasks")
    if not args.admin_secret:
        parser.error("--admin-secret or ADMIN_SECRET_KEY is required")
    if not args.confirm_billable_load_test:
        parser.error(
            "--confirm-billable-load-test is required because every successful task is charged"
        )

    asyncio.run(
        run_benchmark(
            base_url=args.url.rstrip("/"),
            total_tasks=args.tasks,
            concurrency_limit=args.concurrency,
            api_key=args.key,
            admin_secret=args.admin_secret,
        )
    )


if __name__ == "__main__":
    main()
