import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class TaskFeedback(Base):
    __tablename__ = "task_feedbacks"

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"fb_{uuid.uuid4().hex[:14]}",
    )
    task_id = Column(
        String(64),
        ForeignKey("email_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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

    task = relationship("EmailTask", lazy="joined")
    tenant = relationship("Tenant", lazy="joined")


class BenchmarkCase(Base):
    __tablename__ = "benchmark_cases"

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"bm_{uuid.uuid4().hex[:14]}",
    )
    feedback_id = Column(String(64), nullable=True, index=True)
    doc_type = Column(String(64), nullable=False, default="GENERAL", index=True)
    title = Column(String(255), nullable=False, default="标准评测用例")
    
    input_text = Column(Text, nullable=True)
    raw_file_path = Column(String(512), nullable=True)
    ground_truth = Column(JSON, nullable=False, default=dict)
    
    weight = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class FewShotExample(Base):
    __tablename__ = "few_shot_examples"

    id = Column(
        String(64),
        primary_key=True,
        default=lambda: f"fs_{uuid.uuid4().hex[:14]}",
    )
    doc_type = Column(String(64), nullable=False, default="GENERAL", index=True)
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
