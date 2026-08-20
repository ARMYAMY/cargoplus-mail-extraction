import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.feedback import BenchmarkCase

logger = logging.getLogger(__name__)

CRITICAL_FIELDS = {
    "BookingNo",
    "BillOfLadingNo",
    "VesselName",
    "Voyage",
    "PortOfLoading",
    "PortOfDischarge",
    "ContainerList",
    "GrossWeight",
    "Volume",
    "PackageQuantity",
}


def _compare_values(actual: Any, expected: Any) -> bool:
    """Helper to compare single field values permissively."""
    if expected is None or expected == "":
        return True  # If ground truth doesn't care, pass
    if actual is None:
        return False
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(actual) - float(expected)) < 1e-3
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) == 0:
            return True
        return len(actual) >= len(expected)
    act_str = str(actual).strip().lower().replace(" ", "").replace("-", "")
    exp_str = str(expected).strip().lower().replace(" ", "").replace("-", "")
    return act_str == exp_str or exp_str in act_str or act_str in exp_str


def evaluate_extracted_against_ground_truth(
    extracted: Dict[str, Any],
    ground_truth: Dict[str, Any],
) -> Tuple[float, Dict[str, bool], List[str]]:
    """
    Evaluates extracted JSON vs ground truth JSON.
    Returns: (accuracy_ratio, field_match_dict, diff_keys)
    """
    if not ground_truth:
        return 1.0, {}, []

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

    acc = (matched_fields / total_fields) if total_fields > 0 else 1.0
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
                "overall_accuracy_percent": 100.0,
                "duration_seconds": 0.0,
                "can_release": True,
                "critical_regressions_count": 0,
                "field_accuracies": {},
                "case_results": [],
            }

        field_stats: Dict[str, Dict[str, int]] = {}
        case_results = []
        passed_cases = 0
        critical_regressions = 0

        # Run benchmark evaluation
        for case in cases:
            gt = case.ground_truth or {}
            # In mock or test mode, run synthetic extraction or parse directly
            from app.services.extraction_service import ExtractionService
            
            # Run extraction pipeline
            simulated_extract = {}
            try:
                if case.input_text:
                    simulated_extract = await ExtractionService.extract_mail_content(
                        db=db,
                        subject=case.title,
                        body=case.input_text,
                        attachment_paths=[case.raw_file_path] if case.raw_file_path else None,
                        tenant_id=None,
                    )
            except Exception as ex:
                logger.warning("Benchmark case %s extraction error: %s", case.id, ex)
                simulated_extract = {}

            acc, field_matches, diff_keys = evaluate_extracted_against_ground_truth(simulated_extract, gt)

            for f_name, match in field_matches.items():
                if f_name not in field_stats:
                    field_stats[f_name] = {"total": 0, "matched": 0}
                field_stats[f_name]["total"] += 1
                if match:
                    field_stats[f_name]["matched"] += 1

            has_critical_diff = any(k in CRITICAL_FIELDS for k in diff_keys)
            is_case_passed = acc >= 0.85 and not has_critical_diff
            if is_case_passed:
                passed_cases += 1
            if has_critical_diff:
                critical_regressions += 1

            case_results.append({
                "case_id": case.id,
                "title": case.title,
                "doc_type": case.doc_type,
                "accuracy_percent": round(acc * 100, 1),
                "is_passed": is_case_passed,
                "diff_keys": diff_keys,
                "critical_diff": has_critical_diff,
            })

        field_accuracies = {}
        for f_name, stats in field_stats.items():
            if stats["total"] > 0:
                field_accuracies[f_name] = round((stats["matched"] / stats["total"]) * 100, 1)

        total_cases = len(cases)
        overall_acc = round((passed_cases / total_cases) * 100, 1) if total_cases > 0 else 100.0
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
