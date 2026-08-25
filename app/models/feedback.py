import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class TaskFeedback(Base):
    __tablename__ = "task_feedbacks"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_task_feedback_task_id"),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'RESOLVED')",
            name="ck_task_feedback_status",
        ),
    )

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"fb_{uuid.uuid4().hex[:14]}",
    )
    task_id = Column(
        String(64),
        ForeignKey("email_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(
        String(64),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status = Column(
        String(32),
        nullable=False,
        default="PENDING",
        index=True,
    )  # PENDING, ACCEPTED, REJECTED, RESOLVED

    # Original extraction vs Customer ground truth
    original_result = Column(JSON, nullable=False, default=dict)
    corrected_result = Column(JSON, nullable=False, default=dict)
    diff_fields = Column(JSON, nullable=True, default=list)

    # Attribution & categorization
    error_category = Column(
        String(32),
        nullable=True,
        default="UNSPECIFIED",
    )  # PARSER, VISION_OCR, PROMPT_LLM, RULE_CLEAN, CLIENT_ERROR, UNSPECIFIED
    document_type = Column(String(64), nullable=False, default="GENERAL", index=True)

    notes = Column(Text, nullable=True)  # Customer submit comments
    review_comment = Column(Text, nullable=True)  # Admin review notes
    reviewed_by = Column(String(64), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # Financial refund linkage
    is_refunded = Column(Boolean, default=False, nullable=False)
    refund_amount = Column(Numeric(12, 4), nullable=True, default=0)
    refund_tx_id = Column(String(64), nullable=True)

    # Benchmark conversion
    is_benchmark = Column(Boolean, default=False, nullable=False)
    benchmark_id = Column(String(64), nullable=True)

    resolved_version = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    # Avoid implicit OUTER JOINs on every feedback query. In particular,
    # PostgreSQL cannot safely apply FOR UPDATE to the nullable side of those
    # generated joins during concurrent review operations.
    task = relationship("EmailTask", lazy="selectin")
    tenant = relationship("Tenant", lazy="selectin")


class BenchmarkCase(Base):
    __tablename__ = "benchmark_cases"
    __table_args__ = (
        UniqueConstraint("feedback_id", name="uq_benchmark_feedback_id"),
        CheckConstraint("weight >= 1 AND weight <= 100", name="ck_benchmark_weight"),
    )

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"bm_{uuid.uuid4().hex[:14]}",
    )
    feedback_id = Column(
        String(64),
        ForeignKey("task_feedbacks.id", ondelete="SET NULL"),
        nullable=True,
    )
    doc_type = Column(String(64), nullable=False, default="GENERAL", index=True)
    dataset_role = Column(String(16), nullable=False, default="TRAIN", index=True)
    title = Column(String(255), nullable=False, default="标准评测用例")
    
    input_text = Column(Text, nullable=True)
    raw_file_path = Column(String(512), nullable=True)
    source_files = Column(JSON, nullable=True, default=list)
    source_hashes = Column(JSON, nullable=True, default=dict)
    ground_truth = Column(JSON, nullable=False, default=dict)
    
    weight = Column(Integer, nullable=False, default=1)
    # A case becomes trusted gold only after an administrator verifies it.
    is_active = Column(Boolean, nullable=False, default=False, index=True)
    verification_status = Column(String(32), nullable=False, default="DRAFT", index=True)
    verified_by = Column(String(64), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class BenchmarkRevision(Base):
    __tablename__ = "benchmark_revisions"

    id = Column(String(64), primary_key=True, default=lambda: f"bmr_{uuid.uuid4().hex[:14]}")
    benchmark_id = Column(String(64), ForeignKey("benchmark_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot = Column(JSON, nullable=False, default=dict)
    changed_by = Column(String(64), nullable=False, default="admin")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class FewShotExample(Base):
    __tablename__ = "few_shot_examples"
    __table_args__ = (
        UniqueConstraint("feedback_id", name="uq_few_shot_feedback_id"),
        CheckConstraint("priority >= 1 AND priority <= 100", name="ck_few_shot_priority"),
    )

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"fs_{uuid.uuid4().hex[:14]}",
    )
    feedback_id = Column(
        String(64),
        ForeignKey("task_feedbacks.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_tenant_id = Column(
        String(64),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    doc_type = Column(String(64), nullable=False, default="GENERAL", index=True)
    error_category = Column(String(32), nullable=True, default="UNSPECIFIED")
    lifecycle_status = Column(String(32), nullable=False, default="ACTIVE", index=True)
    evaluation_run_id = Column(String(64), nullable=True)
    parent_id = Column(String(64), nullable=True)
    title = Column(String(255), nullable=False, default="示例")
    
    input_excerpt = Column(Text, nullable=False)
    expected_output = Column(JSON, nullable=False, default=dict)
    
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    priority = Column(Integer, nullable=False, default=10)
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class SystemVersion(Base):
    __tablename__ = "system_versions"

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"ver_{uuid.uuid4().hex[:14]}",
    )
    version_tag = Column(String(32), unique=True, nullable=False, index=True)
    benchmark_score = Column(String(32), nullable=False, default="100.0%")
    total_test_cases = Column(Integer, nullable=False, default=0)
    passed_test_cases = Column(Integer, nullable=False, default=0)
    changelog = Column(Text, nullable=True)
    resolved_feedbacks_count = Column(Integer, nullable=False, default=0)
    
    released_by = Column(String(64), nullable=True, default="admin")
    released_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id = Column(String(64), primary_key=True, default=lambda: f"eval_{uuid.uuid4().hex[:14]}")
    prompt_version_id = Column(String(64), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="RUNNING", index=True)
    model_name = Column(String(128), nullable=True)
    overall_accuracy = Column(Numeric(6, 2), nullable=False, default=0)
    total_cases = Column(Integer, nullable=False, default=0)
    passed_cases = Column(Integer, nullable=False, default=0)
    critical_regressions = Column(Integer, nullable=False, default=0)
    can_release = Column(Boolean, nullable=False, default=False)
    configuration_snapshot = Column(JSON, nullable=True, default=dict)
    case_results = Column(JSON, nullable=True, default=list)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id = Column(String(64), primary_key=True, default=lambda: f"prompt_{uuid.uuid4().hex[:14]}")
    version_tag = Column(String(64), unique=True, nullable=False, index=True)
    content = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT", index=True)
    source = Column(String(32), nullable=False, default="MANUAL")
    optimization_goal = Column(Text, nullable=True)
    evidence_feedback_ids = Column(JSON, nullable=True, default=list)
    parent_id = Column(String(64), nullable=True)
    evaluation_run_id = Column(String(64), nullable=True)
    iteration_number = Column(Integer, nullable=False, default=1)
    source_job_id = Column(String(64), nullable=True)
    source_evaluation_job_id = Column(String(64), nullable=True)
    created_by = Column(String(64), nullable=False, default="admin")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    activated_at = Column(DateTime(timezone=True), nullable=True)


class AdminJob(Base):
    """Persistent state for prompt generation and regression work."""

    __tablename__ = "admin_jobs"

    id = Column(String(64), primary_key=True, default=lambda: f"job_{uuid.uuid4().hex[:14]}")
    job_type = Column(String(48), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="QUEUED", index=True)
    phase = Column(String(64), nullable=False, default="QUEUED")
    progress_current = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=False, default=0)
    progress_percent = Column(Integer, nullable=False, default=0)
    input_payload = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=True, default=dict)
    stream_text = Column(Text, nullable=True, default="")
    related_entity_id = Column(String(64), nullable=True, index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    created_by = Column(String(64), nullable=False, default="admin")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
