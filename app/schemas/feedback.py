import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.schemas.cargo_v3 import CargoV3Output, ContainerInfoItem


MAX_FEW_SHOT_OUTPUT_BYTES = 64 * 1024


def complete_benchmark_errors(value: Any) -> List[str]:
    """Return structural errors for a complete 57-field Cargo V3 gold answer."""
    if not isinstance(value, dict):
        return ["标准答案必须是 JSON 对象"]
    expected_fields = set(CargoV3Output.model_fields)
    actual_fields = set(value)
    errors: List[str] = []
    missing = sorted(expected_fields - actual_fields)
    extra = sorted(actual_fields - expected_fields)
    if missing:
        errors.append(f"缺少字段: {', '.join(missing)}")
    if extra:
        errors.append(f"包含未知字段: {', '.join(extra)}")

    containers = value.get("ContainerInfo")
    if "ContainerInfo" in value and not isinstance(containers, list):
        errors.append("ContainerInfo 必须是数组")
    elif isinstance(containers, list):
        expected_container_fields = set(ContainerInfoItem.model_fields)
        for index, item in enumerate(containers):
            if not isinstance(item, dict):
                errors.append(f"ContainerInfo[{index}] 必须是对象")
                continue
            item_fields = set(item)
            item_missing = sorted(expected_container_fields - item_fields)
            item_extra = sorted(item_fields - expected_container_fields)
            if item_missing:
                errors.append(
                    f"ContainerInfo[{index}] 缺少字段: {', '.join(item_missing)}"
                )
            if item_extra:
                errors.append(
                    f"ContainerInfo[{index}] 包含未知字段: {', '.join(item_extra)}"
                )
    if errors:
        return errors
    try:
        CargoV3Output.model_validate(value)
    except Exception as exc:
        errors.append(f"字段类型不符合 Cargo V3: {exc}")
    return errors


def validate_complete_benchmark(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if value is None:
        return value
    errors = complete_benchmark_errors(value)
    if errors:
        raise ValueError("；".join(errors[:5]))
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > MAX_FEW_SHOT_OUTPUT_BYTES:
        raise ValueError("标准答案不能超过 64 KiB")
    return value


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
    document_type: str = Field("GENERAL", min_length=1, max_length=64)


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
    dataset_role: Literal["TRAIN", "HOLDOUT"] = "TRAIN"
    title: str
    input_text: Optional[str] = None
    raw_file_path: Optional[str] = None
    ground_truth: Dict[str, Any]
    weight: int
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BenchmarkUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    doc_type: Optional[str] = Field(None, min_length=1, max_length=64)
    dataset_role: Optional[Literal["TRAIN", "HOLDOUT"]] = None
    ground_truth: Optional[Dict[str, Any]] = None
    weight: Optional[int] = Field(None, ge=1, le=100)
    is_active: Optional[bool] = None
    verification_status: Optional[Literal["DRAFT", "VERIFIED"]] = None

    # 草稿编辑阶段允许暂存未完成内容；只有“人工确认并启用”时才强制校验
    # 完整 57 字段，方便用户逐步修订历史金标。
    _validate_ground_truth = field_validator("ground_truth")(
        _validate_few_shot_output
    )


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
    evaluation_job_id: Optional[str] = Field(None, max_length=64, description="管理端最近一次已通过的全量金标后台任务")


class PromptCreateRequest(BaseModel):
    content: str = Field(..., min_length=100, max_length=131072)
    optimization_goal: Optional[str] = Field(None, max_length=2000)


class PromptOptimizeRequest(BaseModel):
    optimization_goal: str = Field(..., min_length=5, max_length=2000)
    feedback_ids: List[str] = Field(default_factory=list, max_length=200)


class PromptRefineRequest(BaseModel):
    evaluation_job_id: str = Field(..., min_length=3, max_length=64)
    optimization_goal: Optional[str] = Field(None, max_length=2000)
    optimization_directions: List[Literal[
        "FIX_REMAINING_NON_CRITICAL",
        "USE_NEW_FEEDBACK",
        "SIMPLIFY_MERGE_RULES",
        "REDUCE_GUESSING",
        "CUSTOM",
    ]] = Field(default_factory=list, max_length=5)
    optimization_instruction: Optional[str] = Field(None, max_length=4000)
    feedback_ids: List[str] = Field(default_factory=list, max_length=200)


class PromptRuleItem(BaseModel):
    text: str = Field(..., min_length=3, max_length=1000)
    selected: bool = True
    source_feedback_ids: List[str] = Field(default_factory=list, max_length=20)
    source_case_ids: List[str] = Field(default_factory=list, max_length=100)
    affected_fields: List[str] = Field(default_factory=list, max_length=30)
    action: Literal["ADD", "MODIFY", "DELETE"] = "ADD"
    target_rule: Optional[str] = Field(None, max_length=1000)
    conflict_reason: Optional[str] = Field(None, max_length=1000)
    diagnosis: Optional[str] = Field(None, max_length=2000)
    error_type: Literal["PROMPT", "NORMALIZATION", "EVALUATOR", "GOLD", "OCR", "UNKNOWN"] = "UNKNOWN"
    expected_effect: Optional[str] = Field(None, max_length=1000)
    risk_fields: List[str] = Field(default_factory=list, max_length=30)
    confidence: Optional[float] = Field(None, ge=0, le=1)


class PromptFinalizeRequest(BaseModel):
    rules: List[PromptRuleItem] = Field(..., min_length=1, max_length=20)
