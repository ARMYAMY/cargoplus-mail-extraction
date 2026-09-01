import uuid
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import CheckConstraint, Column, String, Boolean, DateTime, Numeric, Integer, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class EmailTask(Base):
    __tablename__ = "email_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_tenant_idempotency"),
        CheckConstraint("attempt_count >= 0", name="ck_task_attempt_count_nonnegative"),
        CheckConstraint(
            "(is_reserved = false) OR (reserved_amount >= 0.01 AND reserved_amount <= 100)",
            name="ck_task_reservation_range",
        ),
    )

    id = Column(String(64), primary_key=True, default=lambda: f"task_{uuid.uuid4().hex[:14]}")
    tenant_id = Column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    api_key_id = Column(String(64), nullable=True, index=True)
    idempotency_key = Column(String(128), nullable=True)
    
    input_type = Column(String(32), nullable=False, default="JSON")  # JSON, FILE
    mail_subject = Column(String(255), nullable=True)
    status = Column(String(32), nullable=False, default="PENDING", index=True)  # PENDING, PROCESSING, SUCCESS, FAILED
    
    input_summary = Column(Text, nullable=True)
    raw_input_json = Column(Text, nullable=True)  # JSON string of structured input
    result_json = Column(Text, nullable=True)     # JSON string of final V3 output
    error_message = Column(Text, nullable=True)
    recognition_mode = Column(String(32), nullable=False, default="standard")
    vision_pages_total = Column(Integer, nullable=False, default=0)
    vision_pages_processed = Column(Integer, nullable=False, default=0)
    vision_duration_ms = Column(Integer, nullable=True)
    
    # Financial fields
    charged_amount = Column(Numeric(10, 4), nullable=False, default=Decimal("0.0000"))
    is_charged = Column(Boolean, nullable=False, default=False)
    reserved_amount = Column(Numeric(10, 4), nullable=False, default=Decimal("0.0000"))
    is_reserved = Column(Boolean, nullable=False, default=False)
    
    # Performance metrics
    duration_ms = Column(Integer, nullable=True)
    
    # Webhook
    callback_url = Column(String(512), nullable=True)
    callback_status = Column(String(32), nullable=False, default="NONE")  # NONE, PENDING, SUCCESS, FAILED
    
    # File attachments saved on disk
    file_paths = Column(Text, nullable=True)  # JSON array of local file paths
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_dispatched_at = Column(DateTime(timezone=True), nullable=True, index=True)
    lease_owner = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)

    tenant = relationship("Tenant", back_populates="tasks")
    billing_transactions = relationship("BillingTransaction", back_populates="task", cascade="all, delete-orphan", lazy="selectin")
    webhook_logs = relationship("WebhookLog", back_populates="task", cascade="all, delete-orphan", lazy="selectin")


class WebhookLog(Base):
    __tablename__ = "webhook_logs"

    id = Column(String(64), primary_key=True, default=lambda: f"wh_{uuid.uuid4().hex[:14]}")
    task_id = Column(String(64), ForeignKey("email_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    
    url = Column(String(512), nullable=False)
    payload = Column(Text, nullable=False)
    signature = Column(String(128), nullable=True)
    status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=1)
    
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    task = relationship("EmailTask", back_populates="webhook_logs")
