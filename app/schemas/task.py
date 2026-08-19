import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AttachmentInput(BaseModel):
    filename: str = Field(default="", max_length=255, description="附件文件名")
    content_type: str = Field(default="", max_length=128, description="MIME类型")
    text: str = Field(default="", max_length=20_000, description="提取的文本")
    tables: List[Any] = Field(default_factory=list, max_length=20, description="提取的表格列表")
    ocr_text: str = Field(default="", max_length=20_000, description="OCR识别的文本")

    @model_validator(mode="after")
    def validate_serialized_table_size(self):
        if len(json.dumps(self.tables, ensure_ascii=False, default=str)) > 100_000:
            raise ValueError("attachment tables exceed the 100000-character limit")
        return self


class SkillV3InputPayload(BaseModel):
    mail_subject: str = Field(default="", max_length=255, description="邮件主题")
    mail_body: str = Field(default="", max_length=50_000, description="邮件正文")
    attachments: List[AttachmentInput] = Field(default_factory=list, max_length=10, description="附件列表")


class ExtractAsyncRequest(BaseModel):
    mail_subject: Optional[str] = Field(default="", max_length=255, description="邮件主题")
    mail_body: Optional[str] = Field(default="", max_length=50_000, description="邮件正文")
    attachments: Optional[List[AttachmentInput]] = Field(default_factory=list, max_length=10, description="预解析附件内容")
    callback_url: Optional[str] = Field(None, max_length=512, description="Webhook 回调地址，任务完成后将 POST 推送结果")

    @model_validator(mode="after")
    def require_extractable_content(self):
        if not (self.mail_body or "").strip() and not self.attachments:
            raise ValueError("mail_body or at least one attachment is required")
        return self


class ExtractSyncRequest(BaseModel):
    mail_subject: Optional[str] = Field(default="", max_length=255, description="邮件主题")
    mail_body: Optional[str] = Field(default="", max_length=50_000, description="邮件正文")
    attachments: Optional[List[AttachmentInput]] = Field(default_factory=list, max_length=10, description="预解析附件内容")

    @model_validator(mode="after")
    def require_extractable_content(self):
        if not (self.mail_body or "").strip() and not self.attachments:
            raise ValueError("mail_body or at least one attachment is required")
        return self


class TaskAsyncResponse(BaseModel):
    code: int = 0
    message: str = "Task submitted successfully"
    task_id: str
    status: str
    created_at: datetime


class TaskDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    input_type: str
    mail_subject: Optional[str]
    status: str
    input_summary: Optional[str]
    result_json: Optional[Dict[str, Any]] = None
    error_message: Optional[str]
    charged_amount: Decimal
    is_charged: bool
    reserved_amount: Decimal
    is_reserved: bool
    duration_ms: Optional[int]
    callback_url: Optional[str]
    callback_status: str
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    last_dispatched_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    attempt_count: int = 0


class TaskListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[TaskDetailResponse]


class WebhookCallbackPayload(BaseModel):
    event: str = "task.completed"
    task_id: str
    tenant_id: str
    status: str
    duration_ms: Optional[int]
    charged_amount: Decimal
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    timestamp: int
