import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.config import settings
from app.models.feedback import BenchmarkCase, EvaluationRun
from app.schemas.task import SkillV3InputPayload
from app.schemas.feedback import complete_benchmark_errors

logger = logging.getLogger(__name__)

CRITICAL_FIELDS = {
    # Cargo V3 canonical fields.
    "BookingNo",
    "BLNo",
    "Vessel",
    "Voyage",
    "POL",
    "POD",
    "ContainerInfo",
    "GrossWeight",
    "Volume",
    "Packages",
    # Legacy aliases retained for historical benchmark records.
    "BillOfLadingNo",
    "VesselName",
    "PortOfLoading",
    "PortOfDischarge",
    "ContainerList",
    "PackageQuantity",
}

NUMERIC_EQUIVALENT_FIELDS = {
    "TotalContainerQty", "Packages", "GrossWeight", "NetWeight", "Volume",
    "KGS", "PCS", "CBM",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_numeric_equivalent_field(field_path: str) -> bool:
    if not field_path:
        return False
    root = field_path.split(".", 1)[0].split("[", 1)[0]
    leaf = field_path.rsplit(".", 1)[-1].split("[", 1)[0]
    return root in NUMERIC_EQUIVALENT_FIELDS or leaf in NUMERIC_EQUIVALENT_FIELDS


def _compare_values(actual: Any, expected: Any, field_path: str = "") -> bool:
    """Compare a ground-truth value without accepting substring/length-only matches."""
    if expected is None:
        return actual is None
    if actual is None:
        return False
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, (int, float, Decimal)):
        try:
            return abs(Decimal(str(actual)) - Decimal(str(expected))) <= Decimal("0.001")
        except (InvalidOperation, TypeError, ValueError):
            return False
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _compare_values(
                actual[key], expected_value, f"{field_path}.{key}" if field_path else key
            )
            for key, expected_value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        unmatched = list(actual)
        for expected_item in expected:
            match_index = next(
                (
                    index
                    for index, actual_item in enumerate(unmatched)
                    if _compare_values(actual_item, expected_item, field_path)
                ),
                None,
            )
            if match_index is None:
                return False
            unmatched.pop(match_index)
        return True
    actual_text = " ".join(str(actual).split()).casefold()
    expected_text = " ".join(str(expected).split()).casefold()
    if _is_numeric_equivalent_field(field_path):
        try:
            actual_number = Decimal(actual_text.replace(",", ""))
            expected_number = Decimal(expected_text.replace(",", ""))
            return actual_number == expected_number
        except (InvalidOperation, TypeError, ValueError):
            pass
    return actual_text == expected_text


def evaluate_extracted_against_ground_truth(
    extracted: Dict[str, Any],
    ground_truth: Dict[str, Any],
) -> Tuple[float, Dict[str, bool], List[str]]:
    """
    Evaluates extracted JSON vs ground truth JSON.
    Returns: (accuracy_ratio, field_match_dict, diff_keys)
    """
    if not ground_truth:
        return 0.0, {}, []

    total_fields = 0
    matched_fields = 0
    field_matches = {}
    diff_keys = []

    for k, exp_val in ground_truth.items():
        total_fields += 1
        act_val = extracted.get(k)
        is_match = _compare_values(act_val, exp_val, k)
        field_matches[k] = is_match
        if is_match:
            matched_fields += 1
        else:
            diff_keys.append(k)

    # A ground truth containing only placeholders is not a successful test case.
    acc = (matched_fields / total_fields) if total_fields > 0 else 0.0
    return acc, field_matches, diff_keys


def build_field_diff_rows(expected: Any, actual: Any, path: str = "") -> List[Dict[str, Any]]:
    """Build inspectable leaf-level rows while preserving the top-level scoring rules."""
    if isinstance(expected, dict):
        rows: List[Dict[str, Any]] = []
        for key, expected_value in expected.items():
            child_path = f"{path}.{key}" if path else key
            actual_value = actual.get(key) if isinstance(actual, dict) else None
            rows.extend(build_field_diff_rows(expected_value, actual_value, child_path))
        return rows
    if isinstance(expected, list):
        rows = []
        actual_list = actual if isinstance(actual, list) else []
        max_len = max(len(expected), len(actual_list))
        for index in range(max_len):
            expected_value = expected[index] if index < len(expected) else None
            actual_value = actual_list[index] if index < len(actual_list) else None
            rows.extend(build_field_diff_rows(expected_value, actual_value, f"{path}[{index}]"))
        return rows or [{
            "field": path,
            "expected": expected,
            "actual": actual,
            "is_match": _compare_values(actual, expected, path),
            "is_critical": path.split(".", 1)[0].split("[", 1)[0] in CRITICAL_FIELDS,
        }]
    root = path.split(".", 1)[0].split("[", 1)[0]
    return [{
        "field": path,
        "expected": expected,
        "actual": actual,
        "is_match": _compare_values(actual, expected, path),
        "is_critical": root in CRITICAL_FIELDS,
    }]


def build_ab_comparison(
    baseline: Dict[str, Any],
    candidate: Dict[str, Any],
    accuracy_threshold: float = 80.0,
) -> Dict[str, Any]:
    """Build a human-readable field-level comparison and explicit release gates."""
    baseline_cases = {item.get("case_id"): item for item in baseline.get("case_results", [])}
    candidate_cases = {item.get("case_id"): item for item in candidate.get("case_results", [])}
    case_rows: List[Dict[str, Any]] = []
    totals = {"FIXED": 0, "REGRESSED": 0, "STILL_WRONG": 0, "UNCHANGED_CORRECT": 0}
    critical_regressions = 0

    for case_id in dict.fromkeys([*baseline_cases, *candidate_cases]):
        before = baseline_cases.get(case_id, {})
        after = candidate_cases.get(case_id, {})
        before_fields = {row.get("field"): row for row in before.get("field_diffs", [])}
        after_fields = {row.get("field"): row for row in after.get("field_diffs", [])}
        field_rows = []
        for field in dict.fromkeys([*before_fields, *after_fields]):
            before_row = before_fields.get(field, {})
            after_row = after_fields.get(field, {})
            before_match = bool(before_row.get("is_match"))
            after_match = bool(after_row.get("is_match"))
            if not before_match and after_match:
                classification = "FIXED"
            elif before_match and not after_match:
                classification = "REGRESSED"
            elif not before_match and not after_match:
                classification = "STILL_WRONG"
            else:
                classification = "UNCHANGED_CORRECT"
            is_critical = bool(after_row.get("is_critical", before_row.get("is_critical", False)))
            totals[classification] += 1
            if classification == "REGRESSED" and is_critical:
                critical_regressions += 1
            field_rows.append({
                "field": field,
                "expected": after_row.get("expected", before_row.get("expected")),
                "baseline_actual": before_row.get("actual"),
                "candidate_actual": after_row.get("actual"),
                "baseline_match": before_match,
                "candidate_match": after_match,
                "classification": classification,
                "is_critical": is_critical,
            })
        case_rows.append({
            "case_id": case_id,
            "dataset_role": after.get("dataset_role") or before.get("dataset_role") or "TRAIN",
            "title": after.get("title") or before.get("title"),
            "doc_type": after.get("doc_type") or before.get("doc_type"),
            "source_files": after.get("source_files") or before.get("source_files") or [],
            "input_text": after.get("input_text") or before.get("input_text") or "",
            "baseline_accuracy_percent": before.get("accuracy_percent", 0),
            "candidate_accuracy_percent": after.get("accuracy_percent", 0),
            "baseline_passed": bool(before.get("is_passed")),
            "candidate_passed": bool(after.get("is_passed")),
            "baseline_result": before.get("actual_result") or {},
            "candidate_result": after.get("actual_result") or {},
            "ground_truth": after.get("ground_truth") or before.get("ground_truth") or {},
            "field_comparisons": field_rows,
        })

    candidate_accuracy = float(candidate.get("overall_accuracy_percent") or 0)
    baseline_accuracy = float(baseline.get("overall_accuracy_percent") or 0)
    candidate_critical_failures = int(candidate.get("critical_failure_cases_count", candidate.get("critical_regressions_count", 0)) or 0)
    checks = [
        {
            "code": "ACCURACY_THRESHOLD",
            "label": "候选准确率达到发布阈值",
            "passed": candidate_accuracy >= accuracy_threshold,
            "detail": f"候选 {candidate_accuracy:.1f}%，要求不低于 {accuracy_threshold:.1f}%",
        },
        {
            "code": "BASELINE_NON_REGRESSION",
            "label": "候选综合准确率不低于生产基线",
            "passed": candidate_accuracy >= baseline_accuracy,
            "detail": f"候选 {candidate_accuracy:.1f}%，生产 {baseline_accuracy:.1f}%",
        },
        {
            "code": "CRITICAL_FIELDS_PASS",
            "label": "候选不存在核心字段错误",
            "passed": candidate_critical_failures == 0,
            "detail": f"候选仍有 {candidate_critical_failures} 个案例包含核心字段错误",
        },
        {
            "code": "NO_NEW_CRITICAL_REGRESSION",
            "label": "没有新增核心字段退化",
            "passed": critical_regressions == 0,
            "detail": f"发现 {critical_regressions} 个由正确变错误的核心字段",
        },
    ]
    failed_checks = [item for item in checks if not item["passed"]]
    return {
        "summary": {
            "fixed": totals["FIXED"],
            "regressed": totals["REGRESSED"],
            "still_wrong": totals["STILL_WRONG"],
            "unchanged_correct": totals["UNCHANGED_CORRECT"],
            "critical_regressions": critical_regressions,
        },
        "gate_checks": checks,
        "gate_reasons": [item["detail"] for item in failed_checks],
        "can_release": not failed_checks,
        "cases": case_rows,
    }


class EvaluationService:
    @classmethod
    async def run_benchmark_evaluation(
        cls,
        db: AsyncSession,
        max_concurrency: int = 4,
        prompt_template: Optional[str] = None,
        prompt_version_id: Optional[str] = None,
        extra_few_shot_snippet: str = "",
        exclude_feedback_id: Optional[str] = None,
        benchmark_ids: Optional[List[str]] = None,
        dataset_role: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, Dict[str, Any]], Awaitable[None]]] = None,
        cancel_check: Optional[Callable[[], Awaitable[None]]] = None,
        stage_callback: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        prepared_payload_cache: Optional[Dict[str, SkillV3InputPayload]] = None,
        evaluation_label: str = "",
        require_complete: bool = False,
    ) -> Dict[str, Any]:
        """
        Runs automated regression benchmark test suite against all active BenchmarkCases.
        """
        start_time = time.time()
        stmt = select(BenchmarkCase).where(
            BenchmarkCase.is_active.is_(True),
            BenchmarkCase.verification_status == "VERIFIED",
        )
        if exclude_feedback_id:
            stmt = stmt.where(BenchmarkCase.feedback_id != exclude_feedback_id)
        if benchmark_ids:
            stmt = stmt.where(BenchmarkCase.id.in_(benchmark_ids))
        if dataset_role:
            if dataset_role not in {"TRAIN", "HOLDOUT"}:
                raise ValueError("不支持的金标数据用途")
            stmt = stmt.where(BenchmarkCase.dataset_role == dataset_role)
        stmt = stmt.order_by(BenchmarkCase.created_at.asc())
        res = await db.execute(stmt)
        selected_cases = res.scalars().all()
        cases = (
            [case for case in selected_cases if not complete_benchmark_errors(case.ground_truth or {})]
            if require_complete
            else selected_cases
        )
        skipped_incomplete = len(selected_cases) - len(cases)
        if skipped_incomplete:
            logger.warning("Skipped %s incomplete benchmark case(s)", skipped_incomplete)

        if not cases:
            # Generate default sample cases if none exist
            logger.info("No benchmark cases in DB, returning empty benchmark summary")
            return {
                "total_cases": 0,
                "passed_cases": 0,
                "failed_cases": 0,
                "overall_accuracy_percent": 0.0,
                "duration_seconds": 0.0,
                "can_release": False,
                "critical_regressions_count": 0,
                "critical_failure_cases_count": 0,
                "field_accuracies": {},
                "case_results": [],
                "dataset_role": dataset_role,
            }

        safe_concurrency = max(1, min(int(max_concurrency), 8))
        semaphore = asyncio.Semaphore(safe_concurrency)
        if prepared_payload_cache is None:
            prepared_payload_cache = {}

        async def emit_stage(stage: str, case: Optional[BenchmarkCase] = None, **details: Any) -> None:
            if not stage_callback:
                return
            payload = {
                "evaluation_label": evaluation_label,
                "case_id": case.id if case else None,
                "case_title": case.title if case else None,
                **details,
            }
            await stage_callback(stage, payload)

        async def evaluate_case(case: BenchmarkCase) -> Dict[str, Any]:
            from app.services.extraction_service import ExtractionService

            extracted: Dict[str, Any] = {}
            extraction_error: Optional[str] = None
            async with semaphore:
                try:
                    await emit_stage("READING_GOLD_FILE", case)
                    attachment_paths = list(case.source_files or [])
                    if not attachment_paths and case.raw_file_path:
                        attachment_paths = [case.raw_file_path]
                    integrity_error = None
                    expected_hashes = case.source_hashes or {}
                    for raw_path in attachment_paths:
                        path = Path(raw_path)
                        expected_hash = expected_hashes.get(path.name)
                        if not path.is_file():
                            integrity_error = f"金标原始文件缺失: {path.name}"
                            break
                        if expected_hash:
                            actual_hash = _sha256_file(path)
                            if actual_hash != expected_hash:
                                integrity_error = f"金标原始文件哈希不一致: {path.name}"
                                break
                    if integrity_error:
                        raise ValueError(integrity_error)
                    if case.input_text or attachment_paths:
                        prepared_payload = prepared_payload_cache.get(case.id)
                        if prepared_payload is None:
                            loop = asyncio.get_running_loop()

                            def parser_stage(stage: str, details: Dict[str, Any]) -> None:
                                future = asyncio.run_coroutine_threadsafe(
                                    emit_stage(stage, case, **details), loop
                                )
                                try:
                                    future.result(timeout=5)
                                except Exception as callback_error:
                                    logger.debug(
                                        "Benchmark parser progress update failed: %s", callback_error
                                    )

                            prepared_payload = await ExtractionService.prepare_mail_payload(
                                subject=case.title,
                                body=case.input_text or "",
                                attachment_paths=attachment_paths or None,
                                parser_stage_callback=parser_stage,
                            )
                            prepared_payload_cache[case.id] = prepared_payload
                        else:
                            await emit_stage("PREPROCESS_CACHE_HIT", case)

                        async def model_progress(stage: str, details: Dict[str, Any]) -> None:
                            await emit_stage(stage, case, **details)

                        async with AsyncSessionLocal() as case_db:
                            extracted = await ExtractionService.extract_mail_content(
                                db=case_db,
                                prepared_payload=prepared_payload,
                                tenant_id=None,
                                prompt_template=prompt_template,
                                extra_few_shot_snippet=extra_few_shot_snippet,
                                model_progress_callback=model_progress,
                            )
                except Exception as exc:
                    extraction_error = str(exc)[:500]
                    logger.warning("Benchmark case %s extraction error: %s", case.id, exc)

            await emit_stage("FIELD_COMPARISON", case)
            ground_truth = case.ground_truth or {}
            accuracy, field_matches, diff_keys = evaluate_extracted_against_ground_truth(
                extracted,
                ground_truth,
            )
            has_critical_diff = any(key in CRITICAL_FIELDS for key in diff_keys)
            is_passed = bool(ground_truth) and accuracy >= 0.85 and not has_critical_diff
            field_diffs = build_field_diff_rows(ground_truth, extracted)
            return {
                "case_id": case.id,
                "dataset_role": case.dataset_role or "TRAIN",
                "title": case.title,
                "doc_type": case.doc_type,
                "source_files": [
                    Path(path).name for path in ((case.source_files or []) or ([case.raw_file_path] if case.raw_file_path else []))
                ],
                "input_text": case.input_text or "",
                "weight": max(1, int(case.weight or 1)),
                "accuracy": accuracy,
                "accuracy_percent": round(accuracy * 100, 1),
                "is_passed": is_passed,
                "diff_keys": diff_keys,
                "field_matches": field_matches,
                "critical_diff": has_critical_diff,
                "error": extraction_error,
                "actual_result": extracted,
                "ground_truth": ground_truth,
                "field_diffs": field_diffs,
            }

        tasks = [asyncio.create_task(evaluate_case(case)) for case in cases]
        case_results = []
        try:
            for completed in asyncio.as_completed(tasks):
                if cancel_check:
                    await cancel_check()
                result = await completed
                case_results.append(result)
                if progress_callback:
                    await progress_callback(len(case_results), len(cases), result)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        await emit_stage("GENERATING_REPORT", total_cases=len(cases))

        field_stats: Dict[str, Dict[str, int]] = {}
        passed_cases = sum(1 for result in case_results if result["is_passed"])
        critical_regressions = sum(1 for result in case_results if result["critical_diff"])
        for result in case_results:
            for field_name, matched in result.pop("field_matches").items():
                stats = field_stats.setdefault(field_name, {"total": 0, "matched": 0})
                stats["total"] += 1
                if matched:
                    stats["matched"] += 1

        field_accuracies = {}
        for f_name, stats in field_stats.items():
            if stats["total"] > 0:
                field_accuracies[f_name] = round((stats["matched"] / stats["total"]) * 100, 1)

        total_cases = len(cases)
        total_weight = sum(result["weight"] for result in case_results)
        weighted_accuracy = sum(
            result.pop("accuracy") * result["weight"] for result in case_results
        )
        overall_acc = round((weighted_accuracy / total_weight) * 100, 1) if total_weight else 0.0
        can_release = overall_acc >= 80.0 and critical_regressions == 0

        run = EvaluationRun(
            prompt_version_id=prompt_version_id,
            status="COMPLETED",
            model_name=settings.LLM_MODEL,
            overall_accuracy=overall_acc,
            total_cases=total_cases,
            passed_cases=passed_cases,
            critical_regressions=critical_regressions,
            can_release=can_release,
            configuration_snapshot={
                "model": settings.LLM_MODEL,
                "temperature": settings.LLM_TEMPERATURE,
                "vision_enabled": settings.VISION_LLM_ENABLED,
                "vision_model": settings.VISION_LLM_MODEL,
                "prompt_version_id": prompt_version_id,
                "dataset_role": dataset_role,
            },
            case_results=case_results,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.commit()

        return {
            "total_cases": total_cases,
            "passed_cases": passed_cases,
            "failed_cases": total_cases - passed_cases,
            "overall_accuracy_percent": overall_acc,
            "duration_seconds": round(time.time() - start_time, 2),
            "can_release": can_release,
            "critical_regressions_count": critical_regressions,
            "critical_failure_cases_count": critical_regressions,
            "field_accuracies": field_accuracies,
            "case_results": case_results,
            "evaluation_run_id": run.id,
            "dataset_role": dataset_role,
        }
