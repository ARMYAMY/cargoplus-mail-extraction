import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.main import app
from app.database import AsyncSessionLocal, init_db
from app.models.billing import BillingTransaction
from app.models.feedback import BenchmarkCase, TaskFeedback
from app.models.task import EmailTask
from app.models.tenant import ApiKey, Tenant
from app.schemas.cargo_v3 import CargoV3Output
from app.services.auth_service import create_access_token, generate_api_key_and_secret, hash_password
from app.services.extraction_service import ExtractionService


@pytest.mark.asyncio
async def test_feedback_loop_end_to_end():
    await init_db()

    async with AsyncSessionLocal() as db:
        # Create test tenant
        t_id = "tenant_feedback_test"
        existing_t = (await db.execute(select(Tenant).where(Tenant.id == t_id))).scalar_one_or_none()
        if not existing_t:
            pwd_hash = hash_password("TenantPass123!")
            t = Tenant(
                id=t_id,
                name="反馈测试企业",
                balance=Decimal("10.0000"),
                unit_price=Decimal("0.5000"),
                password_hash=pwd_hash,
                is_active=True,
            )
            db.add(t)
            raw_k, pref, hash_val, secret = generate_api_key_and_secret()
            k = ApiKey(id="key_fb_test", tenant_id=t_id, key_prefix=pref, key_hash=hash_val, api_secret=secret, is_active=True)
            db.add(k)
        else:
            existing_t.balance = Decimal("10.0000")
            raw_k, pref, hash_val, secret = generate_api_key_and_secret()
            k = (await db.execute(select(ApiKey).where(ApiKey.tenant_id == t_id))).scalars().first()
            if not k:
                k = ApiKey(id="key_fb_test", tenant_id=t_id, key_prefix=pref, key_hash=hash_val, api_secret=secret, is_active=True)
                db.add(k)

        # Create test charged task
        task_id = "task_feedback_unit_1"
        existing_task = (await db.execute(select(EmailTask).where(EmailTask.id == task_id))).scalar_one_or_none()
        if existing_task:
            await db.delete(existing_task)
            await db.commit()

        task = EmailTask(
            id=task_id,
            tenant_id=t_id,
            status="SUCCESS",
            input_type="TEXT",
            mail_subject="订舱确认测试件",
            input_summary="测试内容",
            result_json=json.dumps(CargoV3Output(
                BookingNo="COSCO12345",
                Vessel="EVER GIVEN",
                POLName="SHANGHAI",
                PODName="ROTTERDAM",
            ).model_dump()),
            is_charged=True,
            charged_amount=Decimal("0.5000"),
        )
        db.add(task)
        db.add(
            BillingTransaction(
                tenant_id=t_id,
                task_id=task_id,
                type="DEDUCTION",
                amount=Decimal("0.5000"),
                balance_before=Decimal("10.5000"),
                balance_after=Decimal("10.0000"),
                description="test deduction",
                operator="TEST",
            )
        )
        await db.commit()

    tenant_token = create_access_token(
        subject=t_id,
        role="tenant",
        expires_in=3600,
    )
    admin_token = create_access_token(
        subject="admin",
        role="admin",
        expires_in=3600,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Tenant submits feedback
        corrected = CargoV3Output(
            BookingNo="MAERSK99999",  # Corrected
            Vessel="EVER GIVEN",
            POLName="SHANGHAI",
            PODName="HAMBURG",  # Corrected
        ).model_dump()
        res_submit = await client.post(
            f"/api/v1/tasks/{task_id}/feedback",
            headers={"Authorization": f"Bearer {tenant_token}"},
            json={"corrected_result": corrected, "notes": "目的港与订舱号识别错误，请核对"},
        )
        assert res_submit.status_code == 200, res_submit.text
        data = res_submit.json()["data"]
        fb_id = data["feedback_id"]
        assert data["status"] == "PENDING"
        assert "BookingNo" in data["diff_fields"]
        assert "PODName" in data["diff_fields"]

        # 2. Tenant checks feedback status
        res_status = await client.get(
            f"/api/v1/tasks/{task_id}/feedback",
            headers={"Authorization": f"Bearer {tenant_token}"},
        )
        assert res_status.status_code == 200
        assert res_status.json()["data"]["status"] == "PENDING"

        # 3. Admin lists feedbacks
        res_admin_list = await client.get(
            "/admin/feedbacks?status=PENDING",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert res_admin_list.status_code == 200
        list_data = res_admin_list.json()["data"]
        assert any(item["id"] == fb_id for item in list_data["items"])

        # 4. Admin gets feedback detail
        res_admin_detail = await client.get(
            f"/admin/feedbacks/{fb_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert res_admin_detail.status_code == 200
        detail_data = res_admin_detail.json()["data"]
        assert detail_data["corrected_result"]["BookingNo"] == "MAERSK99999"
        assert detail_data["is_charged"] is True
        assert detail_data["charged_amount"] == 0.5

        # 5. Admin accepts feedback (auto-refund + FewShot + Benchmark)
        res_accept = await client.post(
            f"/admin/feedbacks/{fb_id}/accept",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "status": "ACCEPTED",
                "error_category": "PROMPT_LLM",
                "review_comment": "已核实单证，目的港确为 HAMBURG，采纳并退款",
                "auto_refund": True,
                "create_few_shot": True,
                "create_benchmark": True,
            },
        )
        assert res_accept.status_code == 200, res_accept.text
        accept_data = res_accept.json()["data"]
        assert accept_data["status"] == "ACCEPTED"
        assert accept_data["is_refunded"] is True

        # Verify tenant balance refunded
        async with AsyncSessionLocal() as db:
            t_after = (await db.execute(select(Tenant).where(Tenant.id == t_id))).scalar_one()
            assert t_after.balance == Decimal("10.5000")  # 10.0 + 0.5 refund
            task_after = (await db.execute(select(EmailTask).where(EmailTask.id == task_id))).scalar_one()
            assert task_after.is_charged is True  # Preserve the original charge audit fact.
            tx = (await db.execute(select(BillingTransaction).where(BillingTransaction.task_id == task_id, BillingTransaction.type == "REFUND"))).scalar_one_or_none()
            assert tx is not None
            assert tx.amount == Decimal("0.5000")

        # Duplicate review requests are idempotent and cannot credit twice.
        res_accept_again = await client.post(
            f"/admin/feedbacks/{fb_id}/accept",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "status": "ACCEPTED",
                "error_category": "PROMPT_LLM",
                "auto_refund": True,
                "create_few_shot": True,
                "create_benchmark": True,
            },
        )
        assert res_accept_again.status_code == 200
        async with AsyncSessionLocal() as db:
            t_after_retry = (await db.execute(select(Tenant).where(Tenant.id == t_id))).scalar_one()
            refunds = (
                await db.execute(
                    select(BillingTransaction).where(
                        BillingTransaction.task_id == task_id,
                        BillingTransaction.type == "REFUND",
                    )
                )
            ).scalars().all()
            assert t_after_retry.balance == Decimal("10.5000")
            assert len(refunds) == 1

        # 6. Admin tests Few-Shot list
        res_fs = await client.get(
            "/admin/few-shots",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert res_fs.status_code == 200
        fs_items = res_fs.json()["data"]
        assert len(fs_items) >= 1

        # Newly generated benchmark truth is only a draft until an administrator
        # explicitly verifies it; unverified model output must never enter gold.
        res_benchmarks = await client.get(
            "/admin/benchmarks",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        benchmark = next(item for item in res_benchmarks.json()["data"] if item["feedback_id"] == fb_id)
        assert benchmark["verification_status"] == "DRAFT"
        assert benchmark["is_active"] is False
        res_verify = await client.put(
            f"/admin/benchmarks/{benchmark['id']}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"verification_status": "VERIFIED", "ground_truth": benchmark["ground_truth"]},
        )
        assert res_verify.status_code == 200, res_verify.text

        # Release now requires an unseen holdout layer in addition to the
        # feedback-driven optimization set.
        async with AsyncSessionLocal() as db:
            existing_holdout = (
                await db.execute(select(BenchmarkCase).where(BenchmarkCase.id == "bm_feedback_holdout"))
            ).scalar_one_or_none()
            if existing_holdout:
                await db.delete(existing_holdout)
                await db.flush()
            db.add(BenchmarkCase(
                id="bm_feedback_holdout",
                title="保密发布门禁样本",
                doc_type="GENERAL",
                dataset_role="HOLDOUT",
                input_text="holdout",
                ground_truth=corrected,
                is_active=True,
                verification_status="VERIFIED",
                verified_by="admin",
            ))
            await db.commit()

        # 7. Admin triggers Regression Evaluation
        with patch.object(
            ExtractionService,
            "extract_mail_content",
            new=AsyncMock(return_value=corrected),
        ):
            res_eval = await client.post(
                "/admin/evaluation/run",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert res_eval.status_code == 200
        eval_data = res_eval.json()["data"]
        assert "overall_accuracy_percent" in eval_data
        assert "can_release" in eval_data

        # 8. Admin releases Version
        with patch.object(
            ExtractionService,
            "extract_mail_content",
            new=AsyncMock(return_value=corrected),
        ):
            res_release = await client.post(
                "/admin/version/release",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "version_tag": "v1.1.0-test",
                    "changelog": "优化目的港提取模型规则",
                    "mark_accepted_as_resolved": True,
                },
            )
        assert res_release.status_code == 200, res_release.text
        rel_data = res_release.json()["data"]
        assert rel_data["version_tag"] == "v1.1.0-test"

        # Verify feedback status is now RESOLVED
        async with AsyncSessionLocal() as db:
            fb_after = (await db.execute(select(TaskFeedback).where(TaskFeedback.id == fb_id))).scalar_one()
            assert fb_after.status == "RESOLVED"
            assert fb_after.resolved_version == "v1.1.0-test"
