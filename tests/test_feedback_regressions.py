import json
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.feedback import submit_task_feedback
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models.feedback import FewShotExample, SystemVersion, TaskFeedback
from app.models.task import EmailTask
from app.models.tenant import Tenant
from app.schemas.feedback import TaskFeedbackCreateRequest
from app.services.auth_service import create_access_token
from app.services.billing_service import BillingService
from app.services.evaluation_service import (
    CRITICAL_FIELDS,
    EvaluationService,
    _compare_values,
    evaluate_extracted_against_ground_truth,
)
from app.services.few_shot_service import FewShotService


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
    assert placeholder_matches == {}
    assert placeholder_diffs == []

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
