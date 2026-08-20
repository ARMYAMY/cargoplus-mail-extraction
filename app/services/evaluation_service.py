import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.models.feedback import BenchmarkCase

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


def _compare_values(actual: Any, expected: Any) -> bool:
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
            key in actual and _compare_values(actual[key], expected_value)
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
                    if _compare_values(actual_item, expected_item)
                ),
                None,
            )
            if match_index is None:
                return False
            unmatched.pop(match_index)
        return True
    actual_text = " ".join(str(actual).split()).casefold()
    expected_text = " ".join(str(expected).split()).casefold()
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
        if exp_val is None or exp_val == "" or exp_val == [] or exp_val == {}:
            continue
        total_fields += 1
        act_val = extracted.get(k)
        is_match = _compare_values(act_val, exp_val)
        field_matches[k] = is_match
        if is_match:
            matched_fields += 1
        else:
            diff_keys.append(k)

    # A ground truth containing only placeholders is not a successful test case.
    acc = (matched_fields / total_fields) if total_fields > 0 else 0.0
    return acc, field_matches, diff_keys


class EvaluationService:
    @classmethod
    async def run_benchmark_evaluation(
        cls,
        db: AsyncSession,
        max_concurrency: int = 4,
    ) -> Dict[str, Any]:
        """
        Runs automated regression benchmark test suite against all active BenchmarkCases.
        """
        start_time = time.time()
        stmt = select(BenchmarkCase).where(BenchmarkCase.is_active.is_(True)).order_by(BenchmarkCase.created_at.asc())
        res = await db.execute(stmt)
        cases = res.scalars().all()

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
                "field_accuracies": {},
                "case_results": [],
            }

        safe_concurrency = max(1, min(int(max_concurrency), 8))
        semaphore = asyncio.Semaphore(safe_concurrency)

        async def evaluate_case(case: BenchmarkCase) -> Dict[str, Any]:
            from app.services.extraction_service import ExtractionService

            extracted: Dict[str, Any] = {}
            extraction_error: Optional[str] = None
            async with semaphore:
                try:
                    if case.input_text or case.raw_file_path:
                        async with AsyncSessionLocal() as case_db:
                            extracted = await ExtractionService.extract_mail_content(
                                db=case_db,
                                subject=case.title,
                                body=case.input_text or "",
                                attachment_paths=[case.raw_file_path] if case.raw_file_path else None,
                                tenant_id=None,
                            )
                except Exception as exc:
                    extraction_error = str(exc)[:500]
                    logger.warning("Benchmark case %s extraction error: %s", case.id, exc)

            ground_truth = case.ground_truth or {}
            accuracy, field_matches, diff_keys = evaluate_extracted_against_ground_truth(
                extracted,
                ground_truth,
            )
            has_critical_diff = any(key in CRITICAL_FIELDS for key in diff_keys)
            is_passed = bool(ground_truth) and accuracy >= 0.85 and not has_critical_diff
            return {
                "case_id": case.id,
                "title": case.title,
                "doc_type": case.doc_type,
                "weight": max(1, int(case.weight or 1)),
                "accuracy": accuracy,
                "accuracy_percent": round(accuracy * 100, 1),
                "is_passed": is_passed,
                "diff_keys": diff_keys,
                "field_matches": field_matches,
                "critical_diff": has_critical_diff,
                "error": extraction_error,
            }

        case_results = await asyncio.gather(*(evaluate_case(case) for case in cases))

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

        return {
            "total_cases": total_cases,
            "passed_cases": passed_cases,
            "failed_cases": total_cases - passed_cases,
            "overall_accuracy_percent": overall_acc,
            "duration_seconds": round(time.time() - start_time, 2),
            "can_release": can_release,
            "critical_regressions_count": critical_regressions,
            "field_accuracies": field_accuracies,
            "case_results": case_results,
        }
