import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_FEW_SHOT_OUTPUT_BYTES = 64 * 1024


def _validate_few_shot_output(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return value
    if not value:
        raise ValueError("expected_output cannot be empty")
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > MAX_FEW_SHOT_OUTPUT_BYTES:
        raise ValueError("expected_output cannot exceed 64 KiB")
    return value


class TaskFeedbackCreateRequest(BaseModel):
    corrected_result: Dict[str, Any] = Field(..., description="修正后的 57 字段完整或部分 JSON")
    notes: Optional[str] = Field(None, max_length=1000, description="客户问题说明或纠错原因备注")


class TaskFeedbackReviewRequest(BaseModel):
    status: Literal["ACCEPTED", "REJECTED"] = Field(..., description="审核结果")
    error_category: Literal[
        "PARSER",
        "VISION_OCR",
        "PROMPT_LLM",
        "RULE_CLEAN",
        "CLIENT_ERROR",
        "UNSPECIFIED",
    ] = Field("UNSPECIFIED", description="归因分类")
    review_comment: Optional[str] = Field(None, max_length=1000, description="管理员审核批注")
    auto_refund: bool = Field(True, description="是否自动执行退款冲正")
    create_few_shot: bool = Field(True, description="是否自动生成少样本样例")
    create_benchmark: bool = True


class TaskFeedbackResponse(BaseModel):
    id: str
    task_id: str
    tenant_id: str
    tenant_name: Optional[str] = None
    status: str
    original_result: Dict[str, Any]
    corrected_result: Dict[str, Any]
    diff_fields: List[str] = Field(default_factory=list)
    error_category: Optional[str] = None
    notes: Optional[str] = None
    review_comment: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    is_refunded: bool = False
    refund_amount: Optional[Decimal] = None
    refund_tx_id: Optional[str] = None
    is_benchmark: bool = False
    resolved_version: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FewShotCreateRequest(BaseModel):
    doc_type: str = Field("GENERAL", min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=255)
    input_excerpt: str = Field(..., min_length=5, max_length=4000)
    expected_output: Dict[str, Any] = Field(...)
    priority: int = Field(10, ge=1, le=100)
    is_active: bool = True

    _validate_expected_output = field_validator("expected_output")(
        _validate_few_shot_output
    )


class FewShotUpdateRequest(BaseModel):
    doc_type: Optional[str] = Field(None, min_length=1, max_length=64)
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    input_excerpt: Optional[str] = Field(None, min_length=5, max_length=4000)
    expected_output: Optional[Dict[str, Any]] = None
    priority: Optional[int] = Field(None, ge=1, le=100)
    is_active: Optional[bool] = None

    _validate_expected_output = field_validator("expected_output")(
        _validate_few_shot_output
    )


class FewShotResponse(BaseModel):
    id: str
    doc_type: str
    title: str
    input_excerpt: str
    expected_output: Dict[str, Any]
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BenchmarkCaseResponse(BaseModel):
    id: str
    doc_type: str
    title: str
    input_text: Optional[str] = None
    raw_file_path: Optional[str] = None
    ground_truth: Dict[str, Any]
    weight: int
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SystemVersionReleaseRequest(BaseModel):
    version_tag: str = Field(
        ...,
        min_length=2,
        max_length=32,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="版本号，如 v1.1.0",
    )
    changelog: Optional[str] = Field(None, max_length=2000, description="版本发布更新说明")
    mark_accepted_as_resolved: bool = Field(True, description="是否将当前已采纳的反馈工单全部标记为此版本解决")
