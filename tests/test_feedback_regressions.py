import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.feedback import submit_task_feedback
from app.api.admin.feedback import _collect_prompt_model_response, _layered_evaluation_result
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.feedback import (
    AdminJob,
    BenchmarkCase,
    EvaluationRun,
    FewShotExample,
    PromptVersion,
    SystemVersion,
    TaskFeedback,
)
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.feedback import TaskFeedbackCreateRequest
from app.schemas.cargo_v3 import CargoV3Output
from app.services.auth_service import create_access_token
from app.services.billing_service import BillingService
from app.services.evaluation_service import (
    CRITICAL_FIELDS,
    EvaluationService,
    _compare_values,
    build_ab_comparison,
    build_field_diff_rows,
    evaluate_extracted_against_ground_truth,
)
from app.schemas.task import AttachmentInput, SkillV3InputPayload
from app.services.few_shot_service import FewShotService
from app.services.extraction_service import ExtractionService
from app.core.skill_runner import default_skill_runner


@pytest.mark.asyncio
async def test_feedback_attachment_download_route_returns_404_for_unknown_feedback():
    await init_db()
    admin_token = create_access_token(subject="admin", role="admin", expires_in=3600)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/feedbacks/not-exist/attachments/sample.doc",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert response.status_code == 404


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_ab_evaluation_reuses_prepared_gold_payload_and_reports_cache_stage():
    await init_db()
    case_id = _unique("bm_prepared_cache")
    expected = {"BookingNo": "CACHE-001"}
    prepared = SkillV3InputPayload(mail_subject="gold", mail_body="parsed once", attachments=[])
    stage_events = []

    async with AsyncSessionLocal() as db:
        db.add(BenchmarkCase(
            id=case_id,
            title="gold cache test",
            doc_type="GENERAL",
            dataset_role="TRAIN",
            input_text="source body",
            ground_truth=expected,
            is_active=True,
            verification_status="VERIFIED",
            verified_by="test",
        ))
        await db.commit()

        async def stage_callback(stage, details):
            stage_events.append((stage, details))

        cache = {}
        with patch.object(
            ExtractionService, "prepare_mail_payload", new=AsyncMock(return_value=prepared)
        ) as prepare_mock, patch.object(
            ExtractionService, "extract_mail_content", new=AsyncMock(return_value=expected)
        ) as extract_mock:
            await EvaluationService.run_benchmark_evaluation(
                db,
                benchmark_ids=[case_id],
                prompt_template="baseline",
                prepared_payload_cache=cache,
                stage_callback=stage_callback,
                evaluation_label="baseline",
            )
            await EvaluationService.run_benchmark_evaluation(
                db,
                benchmark_ids=[case_id],
                prompt_template="candidate",
                prepared_payload_cache=cache,
                stage_callback=stage_callback,
                evaluation_label="candidate",
            )

        assert prepare_mock.await_count == 1
        assert extract_mock.await_count == 2
        assert extract_mock.await_args_list[0].kwargs["prepared_payload"] is prepared
        assert extract_mock.await_args_list[1].kwargs["prepared_payload"] is prepared
        assert any(stage == "PREPROCESS_CACHE_HIT" for stage, _ in stage_events)
        assert any(stage == "FIELD_COMPARISON" for stage, _ in stage_events)
        assert any(stage == "GENERATING_REPORT" for stage, _ in stage_events)

        case = (await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == case_id))).scalar_one()
        case.is_active = False
        await db.commit()


@pytest.mark.asyncio
async def test_feedback_attachment_download_returns_the_feedback_tasks_file():
    await init_db()
    tenant_id = _unique("tenant_download")
    task_id = _unique("task_download")
    feedback_id = _unique("fb_download")
    filename = f"{uuid.uuid4().hex[:12]}_sample.doc"
    payload = b"synthetic-doc-attachment"
    attachment_path = settings.uploads_path / filename
    attachment_path.write_bytes(payload)

    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name="Download Tenant", balance=10, unit_price=0.5, is_active=True))
        db.add(
            EmailTask(
                id=task_id,
                tenant_id=tenant_id,
                status="SUCCESS",
                input_type="FILE",
                file_paths=json.dumps([str(attachment_path)]),
            )
        )
        db.add(
            TaskFeedback(
                id=feedback_id,
                task_id=task_id,
                tenant_id=tenant_id,
                original_result={},
                corrected_result={},
            )
        )
        await db.commit()

    admin_token = create_access_token(subject="admin", role="admin", expires_in=3600)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/admin/feedbacks/{feedback_id}/attachments/{filename}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        unrelated = await client.get(
            f"/admin/feedbacks/{feedback_id}/attachments/not-listed.doc",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert response.status_code == 200
    assert response.content == payload
    assert "attachment" in response.headers["content-disposition"]
    assert filename in response.headers["content-disposition"]
    assert unrelated.status_code == 404


@pytest.mark.asyncio
async def test_feedback_attachment_download_supports_migrated_paths_but_blocks_external_files(tmp_path: Path):
    await init_db()
    tenant_id = _unique("tenant_migrated_download")
    safe_filename = f"{uuid.uuid4().hex[:12]}_migrated.doc"
    unsafe_filename = f"{uuid.uuid4().hex[:12]}_outside.doc"
    safe_payload = b"migrated-attachment"
    (settings.uploads_path / safe_filename).write_bytes(safe_payload)
    outside_path = tmp_path / unsafe_filename
    outside_path.write_bytes(b"must-not-be-served")
    # Even an identically named file under the upload root must not be used as
    # fallback unless the persisted record itself came from an uploads directory.
    (settings.uploads_path / unsafe_filename).write_bytes(b"different-task-file")

    safe_task_id = _unique("task_migrated")
    unsafe_task_id = _unique("task_outside")
    safe_feedback_id = _unique("fb_migrated")
    unsafe_feedback_id = _unique("fb_outside")
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name="Migrated Tenant", balance=10, unit_price=0.5, is_active=True))
        db.add_all(
            [
                EmailTask(
                    id=safe_task_id,
                    tenant_id=tenant_id,
                    status="SUCCESS",
                    input_type="FILE",
                    file_paths=json.dumps([rf"C:\\retired-host\\uploads\\{safe_filename}"]),
                ),
                EmailTask(
                    id=unsafe_task_id,
                    tenant_id=tenant_id,
                    status="SUCCESS",
                    input_type="FILE",
                    file_paths=json.dumps([str(outside_path)]),
                ),
            ]
        )
        db.add_all(
            [
                TaskFeedback(
                    id=safe_feedback_id,
                    task_id=safe_task_id,
                    tenant_id=tenant_id,
                    original_result={},
                    corrected_result={},
                ),
                TaskFeedback(
                    id=unsafe_feedback_id,
                    task_id=unsafe_task_id,
                    tenant_id=tenant_id,
                    original_result={},
                    corrected_result={},
                ),
            ]
        )
        await db.commit()

    admin_token = create_access_token(subject="admin", role="admin", expires_in=3600)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        migrated = await client.get(
            f"/admin/feedbacks/{safe_feedback_id}/attachments/{safe_filename}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        external = await client.get(
            f"/admin/feedbacks/{unsafe_feedback_id}/attachments/{unsafe_filename}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert migrated.status_code == 200
    assert migrated.content == safe_payload
    assert external.status_code == 404


def test_benchmark_value_comparison_rejects_false_positive_matches():
    assert _compare_values("HAMBURG", "hamburg") is True
    assert _compare_values("BOOKING-123", "BOOKING") is False
    assert _compare_values([{"id": "A"}], [{"id": "A"}]) is True
    assert _compare_values([{"id": "B"}], [{"id": "A"}]) is False
    assert _compare_values(["A", "B"], ["B", "A"]) is True
    assert _compare_values(["A", "B", "C"], ["A", "B"]) is False


def test_numeric_format_equivalence_is_limited_to_declared_numeric_fields():
    assert _compare_values("2710.00", "2710.000", "GrossWeight") is True
    assert _compare_values("2,710.00", "2710.000", "ContainerInfo[0].KGS") is True
    assert _compare_values("00123", "123", "BLNo") is False


def test_ab_comparison_classifies_fields_and_exposes_real_gate_reasons():
    def case(rows, accuracy):
        return {
            "case_id": "bm_1", "title": "Sample", "accuracy_percent": accuracy,
            "is_passed": False, "actual_result": {}, "ground_truth": {}, "field_diffs": rows,
        }

    baseline = {
        "overall_accuracy_percent": 87.0, "critical_failure_cases_count": 1,
        "case_results": [case([
            {"field": "BLNo", "expected": "A", "actual": "A", "is_match": True, "is_critical": True},
            {"field": "Movement", "expected": "CY-CY", "actual": "", "is_match": False, "is_critical": False},
            {"field": "GrossWeight", "expected": "10", "actual": "9", "is_match": False, "is_critical": True},
        ], 87.0)],
    }
    candidate = {
        "overall_accuracy_percent": 93.0, "critical_failure_cases_count": 1,
        "case_results": [case([
            {"field": "BLNo", "expected": "A", "actual": "B", "is_match": False, "is_critical": True},
            {"field": "Movement", "expected": "CY-CY", "actual": "CY-CY", "is_match": True, "is_critical": False},
            {"field": "GrossWeight", "expected": "10", "actual": "8", "is_match": False, "is_critical": True},
        ], 93.0)],
    }
    result = build_ab_comparison(baseline, candidate)
    classes = {row["field"]: row["classification"] for row in result["cases"][0]["field_comparisons"]}
    assert classes == {"BLNo": "REGRESSED", "Movement": "FIXED", "GrossWeight": "STILL_WRONG"}
    assert result["summary"]["critical_regressions"] == 1
    assert result["can_release"] is False
    assert {item["code"] for item in result["gate_checks"] if not item["passed"]} == {
        "CRITICAL_FIELDS_PASS", "NO_NEW_CRITICAL_REGRESSION"
    }


@pytest.mark.asyncio
async def test_prompt_generation_falls_back_when_stream_contains_no_content():
    await init_db()
    async with AsyncSessionLocal() as db:
        job = AdminJob(job_type="PROMPT_REFINEMENT", status="RUNNING", result={"evidence": {"case": 1}})
        db.add(job)
        await db.commit()
        job_id = job.id

    async def empty_stream(_prompt, **_kwargs):
        if False:
            yield ""

    with (
        patch.object(default_skill_runner, "stream_llm", new=empty_stream),
        patch.object(default_skill_runner, "call_llm", new=AsyncMock(return_value='{"summary":"ok","rules":[]}')),
    ):
        response, diagnostics = await _collect_prompt_model_response(job_id, "prompt", 500)

    assert response == '{"summary":"ok","rules":[]}'
    assert diagnostics["fallback_used"] is True
    assert diagnostics["stream_content_chars"] == 0
    async with AsyncSessionLocal() as db:
        saved = (await db.execute(select(AdminJob).where(AdminJob.id == job_id))).scalar_one()
        assert saved.phase == "COMPATIBILITY_RETRY"
        assert saved.result["evidence"] == {"case": 1}
        assert saved.result["generation_diagnostics"]["generation_mode"] == "NON_STREAM_FALLBACK"


@pytest.mark.asyncio
async def test_prompt_generation_falls_back_after_stream_timeout():
    await init_db()
    async with AsyncSessionLocal() as db:
        job = AdminJob(job_type="PROMPT_REFINEMENT", status="RUNNING")
        db.add(job)
        await db.commit()
        job_id = job.id

    async def timed_out_stream(_prompt, **_kwargs):
        if False:
            yield ""
        raise httpx.ReadTimeout("stream idle")

    fallback = AsyncMock(return_value='{"summary":"recovered","rules":[]}')
    with (
        patch.object(default_skill_runner, "stream_llm", new=timed_out_stream),
        patch.object(default_skill_runner, "call_llm", new=fallback),
    ):
        response, diagnostics = await _collect_prompt_model_response(job_id, "prompt", 500)

    assert "recovered" in response
    assert diagnostics["stream_error"] == "STREAM_TIMEOUT"
    assert diagnostics["fallback_used"] is True
    assert fallback.await_args.kwargs["timeout_seconds"] == settings.PROMPT_LLM_FALLBACK_TIMEOUT_SECONDS

    accuracy, matches, diffs = evaluate_extracted_against_ground_truth({}, {})
    assert accuracy == 0.0
    assert matches == {}
    assert diffs == []

    placeholder_accuracy, placeholder_matches, placeholder_diffs = (
        evaluate_extracted_against_ground_truth(
            {"BookingNo": "SHOULD-NOT-COUNT"},
            {"BookingNo": ""},
        )
    )
    assert placeholder_accuracy == 0.0
    assert placeholder_matches == {"BookingNo": False}
    assert placeholder_diffs == ["BookingNo"]


def test_nested_field_diff_rows_and_document_type_detection():
    rows = build_field_diff_rows(
        {"ContainerInfo": [{"KGS": "", "PCS": "5"}]},
        {"ContainerInfo": [{"KGS": "2710", "PCS": "5"}]},
    )
    by_path = {row["field"]: row for row in rows}
    assert by_path["ContainerInfo[0].KGS"]["is_match"] is False
    assert by_path["ContainerInfo[0].KGS"]["is_critical"] is True
    assert by_path["ContainerInfo[0].PCS"]["is_match"] is True

    payload = SkillV3InputPayload(
        mail_subject="Booking confirmation",
        mail_body="",
        attachments=[AttachmentInput(filename="booking.pdf", content_type="application/pdf")],
    )
    assert FewShotService.detect_document_type(payload) == "BOOKING_CONFIRMATION"

    assert {
        "BLNo",
        "Vessel",
        "POL",
        "POD",
        "ContainerInfo",
        "Packages",
    } <= CRITICAL_FIELDS


@pytest.mark.asyncio
async def test_empty_benchmark_suite_never_opens_release_gate():
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db.execute.return_value = result

    evaluation = await EvaluationService.run_benchmark_evaluation(db)

    assert evaluation["total_cases"] == 0
    assert evaluation["overall_accuracy_percent"] == 0.0
    assert evaluation["can_release"] is False


@pytest.mark.asyncio
async def test_release_endpoint_enforces_server_side_benchmark_gate():
    await init_db()
    admin_token = create_access_token(subject="admin", role="admin", expires_in=3600)
    version_tag = _unique("vblocked")
    blocked_result = {
        "total_cases": 2,
        "passed_cases": 1,
        "failed_cases": 1,
        "overall_accuracy_percent": 50.0,
        "critical_regressions_count": 1,
        "can_release": False,
    }

    transport = ASGITransport(app=app)
    with patch.object(
        EvaluationService,
        "run_benchmark_evaluation",
        new=AsyncMock(return_value=blocked_result),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/admin/version/release",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"version_tag": version_tag, "mark_accepted_as_resolved": True},
            )

    assert response.status_code == 409
    async with AsyncSessionLocal() as db:
        version = (
            await db.execute(select(SystemVersion).where(SystemVersion.version_tag == version_tag))
        ).scalar_one_or_none()
        assert version is None


@pytest.mark.asyncio
async def test_refund_requires_a_real_deduction_ledger():
    await init_db()
    tenant_id = _unique("tenant_refund")
    task_id = _unique("task_refund")

    async with AsyncSessionLocal() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Refund Guard Tenant",
                balance=Decimal("12.0000"),
                unit_price=Decimal("0.5000"),
                is_active=True,
            )
        )
        db.add(
            EmailTask(
                id=task_id,
                tenant_id=tenant_id,
                status="SUCCESS",
                input_type="TEXT",
                result_json=json.dumps({"BookingNo": "A"}),
                is_charged=True,
                charged_amount=Decimal("0.5000"),
            )
        )
        await db.commit()

        with pytest.raises(RuntimeError, match="no matching deduction"):
            await BillingService.refund_task_deduction(db, tenant_id, task_id)
        await db.rollback()

    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        assert tenant.balance == Decimal("12.0000")


@pytest.mark.asyncio
async def test_feedback_partial_update_merges_original_and_resets_rejection_metadata():
    await init_db()
    tenant_id = _unique("tenant_feedback_merge")
    task_id = _unique("task_feedback_merge")

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=tenant_id,
            name="Feedback Merge Tenant",
            balance=10,
            unit_price=0.5,
            is_active=True,
        )
        task = EmailTask(
            id=task_id,
            tenant_id=tenant_id,
            status="SUCCESS",
            input_type="TEXT",
            result_json=json.dumps({"BookingNo": "OLD", "VesselName": "KEEP"}),
        )
        db.add_all([tenant, task])
        await db.commit()

        await submit_task_feedback(
            task_id=task_id,
            payload=TaskFeedbackCreateRequest(corrected_result={"BookingNo": "NEW"}),
            tenant_info=(tenant, None),
            db=db,
        )
        feedback = (
            await db.execute(select(TaskFeedback).where(TaskFeedback.task_id == task_id))
        ).scalar_one()
        assert feedback.corrected_result == {"BookingNo": "NEW", "VesselName": "KEEP"}

        feedback.status = "REJECTED"
        feedback.review_comment = "old rejection"
        feedback.reviewed_by = "admin"
        await db.commit()

        await submit_task_feedback(
            task_id=task_id,
            payload=TaskFeedbackCreateRequest(corrected_result={"BookingNo": "NEWER"}),
            tenant_info=(tenant, None),
            db=db,
        )
        await db.refresh(feedback)
        assert feedback.status == "PENDING"
        assert feedback.review_comment is None
        assert feedback.reviewed_by is None


@pytest.mark.asyncio
async def test_feedback_submission_rejects_noop_and_unknown_fields():
    await init_db()
    tenant_id = _unique("tenant_feedback_validation")
    task_id = _unique("task_feedback_validation")

    async with AsyncSessionLocal() as db:
        tenant = Tenant(
            id=tenant_id,
            name="Feedback Validation Tenant",
            balance=10,
            unit_price=0.5,
            is_active=True,
        )
        db.add(tenant)
        db.add(
            EmailTask(
                id=task_id,
                tenant_id=tenant_id,
                status="SUCCESS",
                input_type="TEXT",
                result_json=json.dumps({"BookingNo": "UNCHANGED"}),
            )
        )
        await db.commit()

        with pytest.raises(HTTPException) as noop_error:
            await submit_task_feedback(
                task_id=task_id,
                payload=TaskFeedbackCreateRequest(
                    corrected_result={"BookingNo": "UNCHANGED"}
                ),
                tenant_info=(tenant, None),
                db=db,
            )
        assert noop_error.value.status_code == 422

        with pytest.raises(HTTPException) as unknown_error:
            await submit_task_feedback(
                task_id=task_id,
                payload=TaskFeedbackCreateRequest(
                    corrected_result={"InjectedUnknownField": "not allowed"}
                ),
                tenant_info=(tenant, None),
                db=db,
            )
        assert unknown_error.value.status_code == 422
        feedback = (
            await db.execute(select(TaskFeedback).where(TaskFeedback.task_id == task_id))
        ).scalar_one_or_none()
        assert feedback is None


@pytest.mark.asyncio
async def test_admin_cannot_accept_or_refund_legacy_feedback_without_differences():
    await init_db()
    tenant_id = _unique("tenant_noop_review")
    task_id = _unique("task_noop_review")
    feedback_id = _unique("fb_noop_review")

    async with AsyncSessionLocal() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="No-op Review Tenant",
                balance=Decimal("8.0000"),
                unit_price=Decimal("0.5000"),
                is_active=True,
            )
        )
        db.add(
            EmailTask(
                id=task_id,
                tenant_id=tenant_id,
                status="SUCCESS",
                input_type="TEXT",
                result_json=json.dumps({"BookingNo": "SAME"}),
                is_charged=True,
                charged_amount=Decimal("0.5000"),
            )
        )
        db.add(
            TaskFeedback(
                id=feedback_id,
                task_id=task_id,
                tenant_id=tenant_id,
                status="PENDING",
                original_result={"BookingNo": "SAME"},
                corrected_result={"BookingNo": "SAME"},
                diff_fields=[],
            )
        )
        await db.commit()

    admin_token = create_access_token(subject="admin", role="admin", expires_in=3600)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/admin/feedbacks/{feedback_id}/accept",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "status": "ACCEPTED",
                "error_category": "CLIENT_ERROR",
                "auto_refund": True,
                "create_few_shot": False,
                "create_benchmark": False,
            },
        )

    assert response.status_code == 409
    async with AsyncSessionLocal() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()
        feedback = (
            await db.execute(select(TaskFeedback).where(TaskFeedback.id == feedback_id))
        ).scalar_one()
        assert tenant.balance == Decimal("8.0000")
        assert feedback.status == "PENDING"


@pytest.mark.asyncio
async def test_few_shot_examples_are_tenant_scoped():
    await init_db()
    tenant_a = _unique("tenant_fs_a")
    tenant_b = _unique("tenant_fs_b")

    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                Tenant(id=tenant_a, name="Tenant A", balance=10, unit_price=0.5, is_active=True),
                Tenant(id=tenant_b, name="Tenant B", balance=10, unit_price=0.5, is_active=True),
            ]
        )
        await db.flush()
        db.add_all(
            [
                FewShotExample(
                    title="Global",
                    input_excerpt="global sample",
                    expected_output={"BookingNo": "GLOBAL"},
                    source_tenant_id=None,
                    priority=10,
                ),
                FewShotExample(
                    title="Tenant A Private",
                    input_excerpt="tenant a sample",
                    expected_output={"BookingNo": "TENANT-A"},
                    source_tenant_id=tenant_a,
                    priority=20,
                ),
            ]
        )
        await db.commit()

        visible_to_a = await FewShotService.get_active_examples(db, tenant_id=tenant_a, limit=10)
        visible_to_b = await FewShotService.get_active_examples(db, tenant_id=tenant_b, limit=10)
        visible_without_tenant = await FewShotService.get_active_examples(db, tenant_id=None, limit=10)

    assert {item["title"] for item in visible_to_a} >= {"Global", "Tenant A Private"}
    assert "Tenant A Private" not in {item["title"] for item in visible_to_b}
    assert "Tenant A Private" not in {item["title"] for item in visible_without_tenant}


# ==========================================
# Prompt activation gate & rollback regressions
# ==========================================

VALID_PROMPT_CONTENT = (
    "货代单证结构化抽取规则：依据邮件与附件原文填写字段，原文无证据时一律留空，"
    "不得猜测或编造任何字段值。\n"
    "主题：{{mail_subject}}\n正文：{{mail_body}}\n附件：{{attachments_text}}"
)


def _admin_headers() -> dict:
    token = create_access_token(subject="admin", role="admin", expires_in=3600)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_activate_rejects_draft_and_failed_versions():
    await init_db()
    async with AsyncSessionLocal() as db:
        draft = PromptVersion(
            version_tag=_unique("draft"), content=VALID_PROMPT_CONTENT, status="DRAFT"
        )
        failed = PromptVersion(
            version_tag=_unique("failed"), content=VALID_PROMPT_CONTENT, status="FAILED"
        )
        db.add_all([draft, failed])
        await db.commit()
        version_ids = (draft.id, failed.id)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for version_id in version_ids:
            response = await client.post(
                f"/admin/prompts/{version_id}/activate", headers=_admin_headers()
            )
            assert response.status_code == 409
            assert "VALIDATED" in response.json()["detail"]


@pytest.mark.asyncio
async def test_activate_validated_version_switches_production():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        seed_resp = await client.get("/admin/prompts", headers=_admin_headers())
        assert seed_resp.status_code == 200
        previous_active_id = next(
            row["id"] for row in seed_resp.json()["data"] if row["status"] == "ACTIVE"
        )

    async with AsyncSessionLocal() as db:
        candidate = PromptVersion(
            version_tag=_unique("validated"),
            content=VALID_PROMPT_CONTENT,
            status="VALIDATED",
        )
        db.add(candidate)
        await db.flush()
        run = EvaluationRun(
            prompt_version_id=candidate.id,
            status="COMPLETED",
            can_release=True,
            overall_accuracy=95.0,
            total_cases=2,
            passed_cases=2,
            critical_regressions=0,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.flush()
        candidate.evaluation_run_id = run.id
        await db.commit()
        candidate_id = candidate.id

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/admin/prompts/{candidate_id}/activate", headers=_admin_headers()
        )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ACTIVE"

    async with AsyncSessionLocal() as db:
        candidate = (
            await db.execute(select(PromptVersion).where(PromptVersion.id == candidate_id))
        ).scalar_one()
        assert candidate.status == "ACTIVE"
        previous_active = (
            await db.execute(
                select(PromptVersion).where(PromptVersion.id == previous_active_id)
            )
        ).scalar_one()
        assert previous_active.status == "ARCHIVED"


@pytest.mark.asyncio
async def test_publish_prompt_requires_matching_complete_ab_job_and_activates():
    await init_db()
    system_tag = _unique("v4_publish")
    async with AsyncSessionLocal() as db:
        candidate = PromptVersion(
            version_tag=_unique("publish_candidate"),
            content=VALID_PROMPT_CONTENT,
            status="VALIDATED",
        )
        db.add(candidate)
        await db.flush()
        run = EvaluationRun(
            prompt_version_id=candidate.id,
            status="COMPLETED",
            can_release=True,
            overall_accuracy=100,
            total_cases=2,
            passed_cases=2,
            critical_regressions=0,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(run)
        await db.flush()
        candidate.evaluation_run_id = run.id
        await db.commit()

        cases = (
            await db.execute(
                select(BenchmarkCase).where(
                    BenchmarkCase.is_active.is_(True),
                    BenchmarkCase.verification_status == "VERIFIED",
                ).order_by(BenchmarkCase.id.asc())
            )
        ).scalars().all()
        snapshot = [
            f"{case.id}:{case.updated_at.isoformat()}"
            for case in cases
            if len(case.ground_truth or {}) == 57
        ]
        job = AdminJob(
            job_type="PROMPT_EVALUATION",
            status="COMPLETED",
            phase="COMPLETED",
            input_payload={"prompt_id": candidate.id, "benchmark_snapshot": snapshot},
            result={
                "no_regression": True,
                "can_release": True,
                "overall_accuracy_percent": 100,
                "total_cases": 2,
                "passed_cases": 2,
            },
            finished_at=datetime.now(timezone.utc),
        )
        db.add(job)
        await db.commit()
        candidate_id, job_id = candidate.id, job.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/admin/prompts/{candidate_id}/publish",
            headers=_admin_headers(),
            json={
                "version_tag": system_tag,
                "changelog": "完整 A/B 通过后统一发布",
                "evaluation_job_id": job_id,
                "mark_accepted_as_resolved": False,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["prompt"]["status"] == "ACTIVE"
    assert response.json()["data"]["version_tag"] == system_tag


@pytest.mark.asyncio
async def test_evaluate_backfills_baseline_run_and_allows_rollback():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = _admin_headers()
        seed_resp = await client.get("/admin/prompts", headers=headers)
        assert seed_resp.status_code == 200
        # 不假设 builtin-v1 仍在用（前序用例可能已切换生产版本），
        # 以当前 ACTIVE 版本作为 baseline 主体即可覆盖同一条回写/回滚链路。
        baseline_id = next(
            row["id"] for row in seed_resp.json()["data"] if row["status"] == "ACTIVE"
        )
        create_resp = await client.post(
            "/admin/prompts",
            headers=headers,
            json={
                "content": VALID_PROMPT_CONTENT + "\n新增规则：无证据时留空",
                "optimization_goal": "回归与回滚链路测试",
            },
        )
        assert create_resp.status_code == 200
        candidate_id = create_resp.json()["data"]["id"]

        async def fake_benchmark(db, prompt_template=None, prompt_version_id=None, **kwargs):
            async with AsyncSessionLocal() as session:
                run = EvaluationRun(
                    prompt_version_id=prompt_version_id,
                    status="COMPLETED",
                    can_release=True,
                    overall_accuracy=92.0,
                    total_cases=1,
                    passed_cases=1,
                    critical_regressions=0,
                    finished_at=datetime.now(timezone.utc),
                )
                session.add(run)
                await session.commit()
                run_id = run.id
            return {
                "total_cases": 1,
                "passed_cases": 1,
                "failed_cases": 0,
                "overall_accuracy_percent": 92.0,
                "duration_seconds": 0.1,
                "can_release": True,
                "critical_regressions_count": 0,
                "field_accuracies": {},
                "case_results": [],
                "evaluation_run_id": run_id,
            }

        with patch.object(
            EvaluationService,
            "run_benchmark_evaluation",
            new=AsyncMock(side_effect=fake_benchmark),
        ):
            eval_resp = await client.post(
                f"/admin/prompts/{candidate_id}/evaluate", headers=headers
            )
        assert eval_resp.status_code == 200
        assert eval_resp.json()["data"]["no_regression"] is True

        async with AsyncSessionLocal() as db:
            baseline_version = (
                await db.execute(
                    select(PromptVersion).where(PromptVersion.id == baseline_id)
                )
            ).scalar_one()
            candidate = (
                await db.execute(
                    select(PromptVersion).where(PromptVersion.id == candidate_id)
                )
            ).scalar_one()
            # baseline 回归 run 回写到原 ACTIVE 版本，且状态保持 ACTIVE
            assert baseline_version.evaluation_run_id is not None
            assert baseline_version.status == "ACTIVE"
            assert candidate.status == "VALIDATED"
            assert candidate.evaluation_run_id is not None

        # 切走：启用候选，原 ACTIVE 版本被归档
        switch_resp = await client.post(
            f"/admin/prompts/{candidate_id}/activate", headers=headers
        )
        assert switch_resp.status_code == 200

        # 回滚：原 ACTIVE 版本已归档但具备 baseline 回归记录，可重新启用
        rollback_resp = await client.post(
            f"/admin/prompts/{baseline_id}/activate", headers=headers
        )
        assert rollback_resp.status_code == 200
        assert rollback_resp.json()["data"]["status"] == "ACTIVE"

    async with AsyncSessionLocal() as db:
        baseline_version = (
            await db.execute(select(PromptVersion).where(PromptVersion.id == baseline_id))
        ).scalar_one()
        candidate = (
            await db.execute(select(PromptVersion).where(PromptVersion.id == candidate_id))
        ).scalar_one()
        assert baseline_version.status == "ACTIVE"
        assert candidate.status == "ARCHIVED"


@pytest.mark.asyncio
async def test_feedback_options_include_field_diffs_and_review_comment():
    await init_db()
    tenant_id = _unique("tenant_opts")
    task_id = _unique("task_opts")
    feedback_id = _unique("fb_opts")

    async with AsyncSessionLocal() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Options Tenant",
                balance=10,
                unit_price=0.5,
                is_active=True,
            )
        )
        db.add(
            EmailTask(
                id=task_id,
                tenant_id=tenant_id,
                status="SUCCESS",
                input_type="TEXT",
                result_json=json.dumps({"BookingNo": "OLD-123"}),
            )
        )
        db.add(
            TaskFeedback(
                id=feedback_id,
                task_id=task_id,
                tenant_id=tenant_id,
                status="ACCEPTED",
                original_result={"BookingNo": "OLD-123"},
                corrected_result={"BookingNo": "NEW-456", "VesselName": "EVER GIVEN"},
                diff_fields=["BookingNo", "VesselName"],
                error_category="PROMPT_LLM",
                review_comment="客户确认 BookingNo 以订舱确认书为准",
            )
        )
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/admin/prompts/feedback-options", headers=_admin_headers()
        )
    assert response.status_code == 200
    item = next(row for row in response.json()["data"] if row["id"] == feedback_id)
    assert item["review_comment"] == "客户确认 BookingNo 以订舱确认书为准"
    # 列表接口保持轻量：不回传完整 JSON 原文
    assert "original_result" not in item
    assert "corrected_result" not in item
    # 字段级 diff：expected=客户改正值，actual=系统原值
    by_field = {diff["field"]: diff for diff in item["field_diffs"]}
    assert by_field["BookingNo"] == {
        "field": "BookingNo",
        "expected": "NEW-456",
        "actual": "OLD-123",
    }
    assert by_field["VesselName"]["expected"] == "EVER GIVEN"
    assert by_field["VesselName"]["actual"] is None


@pytest.mark.asyncio
async def test_prompt_evidence_is_complete_and_filters_are_combined():
    await init_db()
    tenant_id = _unique("tenant_evidence")
    task_id = _unique("task_evidence")
    feedback_id = _unique("fb_evidence")
    original = {f"Field{i}": f"old-{i}" for i in range(120)}
    corrected = {f"Field{i}": f"new-{i}" for i in range(120)}
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name="Evidence Tenant", balance=10, unit_price=0.5, is_active=True))
        db.add(EmailTask(id=task_id, tenant_id=tenant_id, status="SUCCESS", input_type="TEXT", mail_subject="Evidence subject", input_summary="Evidence body"))
        db.add(TaskFeedback(
            id=feedback_id,
            task_id=task_id,
            tenant_id=tenant_id,
            status="ACCEPTED",
            original_result=original,
            corrected_result=corrected,
            diff_fields=list(corrected),
            error_category="PROMPT_LLM",
            document_type="BILL_OF_LADING",
            notes="customer evidence",
            review_comment="admin verified correction",
        ))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        preview = await client.post(
            "/admin/prompts/evidence-preview",
            headers=_admin_headers(),
            json={"optimization_goal": "improve all corrected fields", "feedback_ids": [feedback_id]},
        )
        filtered = await client.get(
            "/admin/prompts/feedback-options",
            headers=_admin_headers(),
            params={
                "field": "Field119",
                "tenant_id": tenant_id,
                "document_type": "BILL_OF_LADING",
                "error_category": "PROMPT_LLM",
            },
        )
    assert preview.status_code == 200, preview.text
    evidence = preview.json()["data"]["evidence"][0]
    assert len(evidence["field_diffs"]) == 120
    assert evidence["original_result"] == original
    assert evidence["human_corrected_result"] == corrected
    assert evidence["source_context"]["mail_subject"] == "Evidence subject"
    assert [row["id"] for row in filtered.json()["data"]] == [feedback_id]


@pytest.mark.asyncio
async def test_unverified_benchmark_is_excluded_from_export_and_regression():
    await init_db()
    case_id = _unique("bm_draft")
    async with AsyncSessionLocal() as db:
        stale = (
            await db.execute(select(BenchmarkCase).where(BenchmarkCase.title == "Draft must not leak"))
        ).scalars().all()
        for item in stale:
            item.verification_status = "DRAFT"
            item.is_active = False
        db.add(BenchmarkCase(
            id=case_id,
            title="Draft must not leak",
            doc_type="GENERAL",
            input_text="sample",
            ground_truth={"BLNo": "HUMAN-ONLY"},
            verification_status="DRAFT",
            is_active=False,
        ))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        exported = await client.get("/admin/benchmarks-export.jsonl", headers=_admin_headers())
        single = await client.post(f"/admin/benchmarks/{case_id}/evaluate", headers=_admin_headers())
        verified = await client.put(
            f"/admin/benchmarks/{case_id}",
            headers=_admin_headers(),
            json={"verification_status": "VERIFIED"},
        )
        exported_after = await client.get("/admin/benchmarks-export.jsonl", headers=_admin_headers())
        await client.put(
            f"/admin/benchmarks/{case_id}",
            headers=_admin_headers(),
            json={"verification_status": "DRAFT"},
        )
    assert exported.status_code == 200
    assert case_id not in exported.text
    assert single.status_code == 409
    assert verified.status_code == 409
    assert "57" in verified.json()["detail"]["message"]
    assert case_id not in exported_after.text


@pytest.mark.asyncio
async def test_manual_benchmark_import_uses_complete_template_and_keeps_weight():
    await init_db()
    marker = _unique("manual_gold")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        template_response = await client.get(
            "/admin/benchmarks/template", headers=_admin_headers()
        )
        assert template_response.status_code == 200
        ground_truth = template_response.json()["data"]
        assert len(ground_truth) == 57
        ground_truth["BLNo"] = marker

        imported = await client.post(
            "/admin/benchmarks/import",
            headers=_admin_headers(),
            data={
                "title": marker,
                "doc_type": "BILL_OF_LADING",
                "dataset_role": "TRAIN",
                "weight": "37",
                "ground_truth_json": json.dumps(ground_truth, ensure_ascii=False),
            },
            files={"files": (f"{marker}.txt", b"B/L NO: TEST", "text/plain")},
        )
        assert imported.status_code == 200, imported.text
        case_id = imported.json()["data"]["id"]

        async with AsyncSessionLocal() as db:
            stored_case = await db.get(BenchmarkCase, case_id)
            assert stored_case is not None
            assert stored_case.source_files
            stored_file = Path(stored_case.source_files[0]).resolve()
            assert stored_file.is_relative_to(settings.uploads_path.resolve())
            assert stored_file.is_file()

        listed = await client.get("/admin/benchmarks", headers=_admin_headers())
        item = next(row for row in listed.json()["data"] if row["id"] == case_id)
        assert item["source_type"] == "MANUAL"
        assert item["is_complete"] is True
        assert item["weight"] == 37
        assert item["verification_status"] == "DRAFT"
        assert item["is_active"] is False

        verified = await client.put(
            f"/admin/benchmarks/{case_id}",
            headers=_admin_headers(),
            json={"verification_status": "VERIFIED"},
        )
        assert verified.status_code == 200, verified.text


@pytest.mark.asyncio
async def test_prompt_generation_job_streams_persists_sources_and_can_finalize():
    await init_db()
    tenant_id = _unique("tenant_prompt_job")
    task_id = _unique("task_prompt_job")
    feedback_id = _unique("fb_prompt_job")
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name="Prompt Job Tenant", balance=10, unit_price=0.5, is_active=True))
        db.add(EmailTask(id=task_id, tenant_id=tenant_id, status="SUCCESS", input_type="TEXT", mail_subject="BL correction", input_summary="BL NO OLD"))
        db.add(TaskFeedback(
            id=feedback_id,
            task_id=task_id,
            tenant_id=tenant_id,
            status="ACCEPTED",
            original_result={"BLNo": "OLD"},
            corrected_result={"BLNo": "NEW"},
            diff_fields=["BLNo"],
            error_category="PROMPT_LLM",
            document_type="BILL_OF_LADING",
        ))
        await db.commit()

    response_json = json.dumps({
        "summary": "Use explicit BL evidence",
        "rules": [{
            "text": "提单号只能取自明确标注的 BL NO；没有原文证据时留空。",
            "source_feedback_ids": [feedback_id],
            "affected_fields": ["BLNo"],
            "action": "ADD",
            "target_rule": "",
            "conflict_reason": "",
        }],
    }, ensure_ascii=False)

    async def fake_stream(_prompt, **_kwargs):
        for index in range(0, len(response_json), 25):
            yield response_json[index:index + 25]

    transport = ASGITransport(app=app)
    with patch.object(default_skill_runner, "stream_llm", new=fake_stream):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/admin/prompts/optimization-jobs",
                headers=_admin_headers(),
                json={"optimization_goal": "improve BL number extraction", "feedback_ids": [feedback_id]},
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["data"]["id"]
            job = None
            for _ in range(100):
                polled = await client.get(f"/admin/jobs/{job_id}", headers=_admin_headers())
                job = polled.json()["data"]
                if job["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "COMPLETED", job
            assert job["stream_text"] == response_json
            assert job["result"]["rules"][0]["source_feedback_ids"] == [feedback_id]
            finalized = await client.post(
                f"/admin/prompts/optimization-jobs/{job_id}/finalize",
                headers=_admin_headers(),
                json={"rules": job["result"]["rules"]},
            )
            assert finalized.status_code == 200, finalized.text
            assert finalized.json()["data"]["change_set"]["added"]


@pytest.mark.asyncio
async def test_failed_evaluation_can_generate_review_and_finalize_next_iteration():
    await init_db()
    case_id = _unique("bm_refine")
    async with AsyncSessionLocal() as db:
        active = (await db.execute(select(PromptVersion).where(PromptVersion.status == "ACTIVE"))).scalars().first()
        if not active:
            active = PromptVersion(
                version_tag=_unique("active"), content=default_skill_runner.extract_prompt_template,
                status="ACTIVE", source="BUILTIN", iteration_number=1,
            )
            db.add(active)
            await db.flush()
        candidate = PromptVersion(
            version_tag=_unique("candidate"), content=active.content, status="FAILED",
            source="AI", parent_id=active.id, iteration_number=2,
        )
        db.add(candidate)
        await db.flush()
        comparison = {
            "summary": {"fixed": 0, "regressed": 0, "still_wrong": 1},
            "gate_reasons": ["候选仍有 1 个案例包含核心字段错误"],
            "cases": [{
                "case_id": case_id, "title": "BL sample", "doc_type": "BILL_OF_LADING",
                "source_files": ["sample.pdf"], "input_text": "GROSS WEIGHT 10 KGS",
                "field_comparisons": [{
                    "field": "GrossWeight", "expected": "10.000", "baseline_actual": "9",
                    "candidate_actual": "9", "classification": "STILL_WRONG", "is_critical": True,
                }],
            }],
        }
        source_job = AdminJob(
            job_type="PROMPT_EVALUATION", status="COMPLETED", phase="COMPLETED",
            input_payload={"prompt_id": candidate.id}, result={"no_regression": False, "ab_comparison": comparison},
        )
        db.add(source_job)
        await db.commit()
        source_job_id, candidate_id = source_job.id, candidate.id

    model_json = json.dumps({
        "summary": "Fix explicit gross weight",
        "diagnoses": [{
            "field": "GrossWeight", "classification": "STILL_WRONG", "error_type": "PROMPT",
            "reason": "The label was ignored", "source_case_ids": [case_id],
        }],
        "rules": [{
            "text": "当原文明确标注 GROSS WEIGHT 时，将紧邻数值填写到 GrossWeight。",
            "source_case_ids": [case_id], "affected_fields": ["GrossWeight"], "action": "ADD",
            "target_rule": "", "conflict_reason": "", "diagnosis": "Explicit label was missed",
            "error_type": "PROMPT", "expected_effect": "Recover GrossWeight", "risk_fields": ["NetWeight"],
            "confidence": 0.9,
        }],
    }, ensure_ascii=False)

    async def fake_stream(_prompt, **_kwargs):
        yield model_json

    transport = ASGITransport(app=app)
    with patch.object(default_skill_runner, "stream_llm", new=fake_stream):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/admin/prompts/refinement-jobs", headers=_admin_headers(),
                json={"evaluation_job_id": source_job_id, "optimization_goal": "fix failures"},
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["data"]["id"]
            job = None
            for _ in range(100):
                job = (await client.get(f"/admin/jobs/{job_id}", headers=_admin_headers())).json()["data"]
                if job["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "COMPLETED", job
            assert job["result"]["diagnoses"][0]["error_type"] == "PROMPT"
            finalized = await client.post(
                f"/admin/prompts/refinement-jobs/{job_id}/finalize", headers=_admin_headers(),
                json={"rules": job["result"]["rules"]},
            )
            assert finalized.status_code == 200, finalized.text
            prompt = finalized.json()["data"]["prompt"]
            assert prompt["parent_id"] == candidate_id
            assert prompt["iteration_number"] == 3
            assert prompt["source_evaluation_job_id"] == source_job_id


@pytest.mark.asyncio
async def test_passed_evaluation_can_continue_with_multiple_directions_and_instruction():
    await init_db()
    case_id = _unique("bm_continue")
    async with AsyncSessionLocal() as db:
        active = (await db.execute(select(PromptVersion).where(PromptVersion.status == "ACTIVE"))).scalars().first()
        if not active:
            active = PromptVersion(
                version_tag=_unique("active"), content=default_skill_runner.extract_prompt_template,
                status="ACTIVE", source="BUILTIN", iteration_number=1,
            )
            db.add(active)
            await db.flush()
        candidate = PromptVersion(
            version_tag=_unique("candidate"), content=active.content, status="VALIDATED",
            source="AI", parent_id=active.id, iteration_number=2,
        )
        db.add(candidate)
        await db.flush()
        comparison = {
            "summary": {"fixed": 1, "regressed": 0, "still_wrong": 0},
            "gate_reasons": [],
            "cases": [{
                "case_id": case_id, "title": "Passed sample", "doc_type": "BILL_OF_LADING",
                "source_files": ["passed.pdf"], "input_text": "NO VALUE PROVIDED",
                "candidate_accuracy_percent": 100,
                "field_comparisons": [{
                    "field": "BookingNo", "expected": "", "baseline_actual": "",
                    "candidate_actual": "", "classification": "UNCHANGED_CORRECT", "is_critical": True,
                }],
            }],
        }
        source_job = AdminJob(
            job_type="PROMPT_EVALUATION", status="COMPLETED", phase="COMPLETED",
            input_payload={"prompt_id": candidate.id}, result={"no_regression": True, "ab_comparison": comparison},
        )
        db.add(source_job)
        await db.commit()
        source_job_id, candidate_id = source_job.id, candidate.id

    model_json = json.dumps({
        "summary": "Simplify duplicate rules and retain evidence-only extraction",
        "diagnoses": [],
        "rules": [{
            "text": "原文没有明确字段证据时保持空值，不根据相邻内容猜测。",
            "source_case_ids": [case_id], "source_feedback_ids": [],
            "affected_fields": ["BookingNo"], "action": "ADD", "target_rule": "",
            "conflict_reason": "", "diagnosis": "Preserve the passed evidence-only behavior",
            "error_type": "PROMPT", "expected_effect": "Reduce unsupported values",
            "risk_fields": [], "confidence": 0.9,
        }],
    }, ensure_ascii=False)

    async def fake_stream(_prompt, **_kwargs):
        yield model_json

    transport = ASGITransport(app=app)
    with patch.object(default_skill_runner, "stream_llm", new=fake_stream):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/admin/prompts/refinement-jobs", headers=_admin_headers(),
                json={
                    "evaluation_job_id": source_job_id,
                    "optimization_directions": ["SIMPLIFY_MERGE_RULES", "REDUCE_GUESSING"],
                    "optimization_instruction": "保留港口字段规则，不改动字段定义。",
                },
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["data"]["id"]
            job = None
            for _ in range(100):
                job = (await client.get(f"/admin/jobs/{job_id}", headers=_admin_headers())).json()["data"]
                if job["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                await asyncio.sleep(0.01)
            assert job["status"] == "COMPLETED", job
            assert job["result"]["evidence"]["refinement_mode"] == "CONTINUOUS_IMPROVEMENT"
            assert job["result"]["evidence"]["optimization_directions"] == [
                "简化和合并重复规则", "降低猜测与幻觉",
            ]
            assert job["result"]["evidence"]["human_instruction"] == "保留港口字段规则，不改动字段定义。"

            finalized = await client.post(
                f"/admin/prompts/refinement-jobs/{job_id}/finalize", headers=_admin_headers(),
                json={"rules": job["result"]["rules"]},
            )
            assert finalized.status_code == 200, finalized.text
            prompt = finalized.json()["data"]["prompt"]
            assert prompt["parent_id"] == candidate_id
            assert prompt["iteration_number"] == 3
            assert prompt["optimization_goal"] == "保留港口字段规则，不改动字段定义。"


@pytest.mark.asyncio
async def test_refinement_rejects_unreviewed_feedback_ids():
    await init_db()
    async with AsyncSessionLocal() as db:
        candidate = PromptVersion(
            version_tag=_unique("candidate"), content=default_skill_runner.extract_prompt_template,
            status="FAILED", source="AI", iteration_number=2,
        )
        db.add(candidate)
        await db.flush()
        source_job = AdminJob(
            job_type="PROMPT_EVALUATION", status="COMPLETED", phase="COMPLETED",
            input_payload={"prompt_id": candidate.id},
            result={"no_regression": False, "ab_comparison": {"summary": {}, "cases": []}},
        )
        db.add(source_job)
        await db.commit()
        source_job_id = source_job.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/admin/prompts/refinement-jobs", headers=_admin_headers(),
            json={
                "evaluation_job_id": source_job_id,
                "optimization_directions": ["USE_NEW_FEEDBACK"],
                "feedback_ids": ["feedback_not_reviewed"],
            },
        )
        assert created.status_code == 400
        assert "不存在或尚未审核通过" in created.json()["detail"]


def test_layered_evaluation_never_exposes_holdout_case_details():
    training = {
        "total_cases": 1, "passed_cases": 1, "failed_cases": 0,
        "overall_accuracy_percent": 100, "can_release": True,
        "critical_regressions_count": 0, "critical_failure_cases_count": 0,
        "case_results": [{"case_id": "train_case", "ground_truth": {"BLNo": "TRAIN"}}],
        "field_accuracies": {"BLNo": 100}, "duration_seconds": 1,
    }
    holdout = {
        "total_cases": 1, "passed_cases": 1, "failed_cases": 0,
        "overall_accuracy_percent": 100, "can_release": True,
        "critical_regressions_count": 0, "critical_failure_cases_count": 0,
        "case_results": [{"case_id": "holdout_case", "ground_truth": {"BLNo": "TOP_SECRET"}}],
        "field_accuracies": {"BLNo": 100}, "duration_seconds": 2,
    }
    result = _layered_evaluation_result(training, holdout)
    assert result["can_release"] is True
    assert result["case_results"] == training["case_results"]
    assert "case_results" not in result["holdout"]
    assert "field_accuracies" not in result["holdout"]
    assert "TOP_SECRET" not in json.dumps(result, ensure_ascii=False)

    missing_holdout = _layered_evaluation_result(training, {"total_cases": 0, "can_release": False})
    assert missing_holdout["can_release"] is False
    assert any("保密" in reason for reason in missing_holdout["gate_reasons"])


@pytest.mark.asyncio
async def test_holdout_cannot_run_single_case_or_enter_export():
    await init_db()
    train_id, holdout_id = _unique("bm_train"), _unique("bm_holdout")
    async with AsyncSessionLocal() as db:
        db.add_all([
            BenchmarkCase(
                id=train_id, title="export-train-marker", doc_type="GENERAL", dataset_role="TRAIN",
                input_text="TRAIN_INPUT", ground_truth=CargoV3Output(BLNo="TRAIN_EXPORT").model_dump(),
                is_active=True, verification_status="VERIFIED", verified_by="admin",
            ),
            BenchmarkCase(
                id=holdout_id, title="holdout-secret-marker", doc_type="GENERAL", dataset_role="HOLDOUT",
                input_text="HOLDOUT_SECRET_INPUT", ground_truth=CargoV3Output(BLNo="HOLDOUT_SECRET_ANSWER").model_dump(),
                is_active=True, verification_status="VERIFIED", verified_by="admin",
            ),
        ])
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/admin/benchmarks", headers=_admin_headers())
        assert listed.status_code == 200
        by_id = {item["id"]: item for item in listed.json()["data"]}
        assert by_id[train_id]["dataset_role"] == "TRAIN"
        assert by_id[holdout_id]["dataset_role"] == "HOLDOUT"

        single = await client.post(f"/admin/benchmarks/{holdout_id}/evaluate", headers=_admin_headers())
        assert single.status_code == 409
        assert "不能单案例" in single.json()["detail"]

        exported = await client.get("/admin/benchmarks-export.jsonl", headers=_admin_headers())
        assert exported.status_code == 200
        assert "TRAIN_EXPORT" in exported.text
        assert "HOLDOUT_SECRET_INPUT" not in exported.text
        assert "HOLDOUT_SECRET_ANSWER" not in exported.text


@pytest.mark.asyncio
async def test_holdout_only_failure_cannot_be_sent_to_prompt_refinement():
    await init_db()
    async with AsyncSessionLocal() as db:
        candidate = PromptVersion(
            version_tag=_unique("holdout_candidate"), content=default_skill_runner.extract_prompt_template,
            status="FAILED", source="AI", iteration_number=2,
        )
        db.add(candidate)
        await db.flush()
        source = AdminJob(
            job_type="PROMPT_EVALUATION", status="COMPLETED", phase="COMPLETED",
            input_payload={"prompt_id": candidate.id},
            result={"no_regression": False, "holdout_only_failure": True, "ab_comparison": {"summary": {}, "cases": []}},
        )
        db.add(source)
        await db.commit()
        source_id = source.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/admin/prompts/refinement-jobs", headers=_admin_headers(),
            json={"evaluation_job_id": source_id, "optimization_directions": ["REDUCE_GUESSING"]},
        )
        assert response.status_code == 409
        assert "保密测试集" in response.json()["detail"]
