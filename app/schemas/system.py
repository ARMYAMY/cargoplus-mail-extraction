from typing import Optional
from pydantic import BaseModel, Field


class LLMConfigResponse(BaseModel):
    base_url: str = Field(..., description="LLM API 服务基础地址")
    api_key: str = Field(default="", description="始终为空；API 不回传完整明文认证密钥")
    api_key_masked: str = Field(..., description="掩码后的 API Key (用于安全展示)")
    is_configured: bool = Field(..., description="是否已正确配置 API Key")
    model: str = Field(..., description="当前使用的模型标识")
    timeout_seconds: int = Field(..., description="请求超时时间 (秒)")
    temperature: float = Field(..., description="采样温度")
    runtime_editable: bool = Field(
        default=True, description="是否允许通过控制台修改"
    )


class LLMConfigUpdate(BaseModel):
    base_url: str = Field(..., min_length=5, max_length=512, description="LLM API 服务基础地址 (如 https://api.senseaudio.cn/v1)")
    api_key: Optional[str] = Field(default=None, max_length=512, description="LLM API 认证密钥 (留空或不传则保留现有密钥)")
    model: Optional[str] = Field(default="deepseek-v4-flash-0731", max_length=128, description="模型名称")
    timeout_seconds: Optional[int] = Field(default=60, ge=5, le=300, description="超时时间 (秒)")
    temperature: Optional[float] = Field(default=0.0, ge=0.0, le=2.0, description="采样温度")


class LLMTestRequest(BaseModel):
    base_url: Optional[str] = Field(None, max_length=512, description="待测试的 Base URL (留空使用当前配置)")
    api_key: Optional[str] = Field(None, max_length=512, description="待测试的 API Key (留空使用当前配置)")
    model: Optional[str] = Field(None, max_length=128, description="待测试的模型名称 (留空使用当前配置)")


class LLMModelsFetchRequest(BaseModel):
    base_url: Optional[str] = Field(None, max_length=512, description="待拉取模型列表的 Base URL (留空使用当前配置)")
    api_key: Optional[str] = Field(None, max_length=512, description="待拉取模型列表的 API Key (留空使用当前配置)")
