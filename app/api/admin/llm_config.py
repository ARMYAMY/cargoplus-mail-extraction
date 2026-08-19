import logging
import time
from typing import Optional
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, status
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_admin_access
from app.config import settings
from app.database import get_db
from app.models.system import SystemConfig
from app.schemas.system import (
    LLMConfigResponse,
    LLMConfigUpdate,
    LLMTestRequest,
    LLMModelsFetchRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/llm-config", dependencies=[Depends(verify_admin_access)])


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    allowed_schemes = {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": 42201, "message": "Base URL 必须是合法的 http:// 或 https:// 接口地址"},
        )
    return base_url.rstrip("/")


def _mask_api_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return "未配置"
    if len(key) <= 8:
        return f"{key[:2]}****{key[-2:]}"
    return f"{key[:4]}...{key[-4:]} ({len(key)} 字符)"


@router.get("", response_model=LLMConfigResponse, summary="获取当前大模型 API 配置")
async def get_llm_config(
    db: AsyncSession = Depends(get_db),
):
    """返回当前系统生效的 LLM Base URL、模型名称、超时时间及 API Key（掩码展示）。"""
    base_url = settings.LLM_BASE_URL
    api_key = settings.LLM_API_KEY
    model = settings.LLM_MODEL
    timeout_sec = settings.LLM_TIMEOUT_SECONDS
    temp = settings.LLM_TEMPERATURE

    stmt = select(SystemConfig)
    res = await db.execute(stmt)
    configs = {c.key: c.value for c in res.scalars().all()}

    if "LLM_BASE_URL" in configs and configs["LLM_BASE_URL"]:
        base_url = configs["LLM_BASE_URL"]
    if "LLM_API_KEY" in configs and configs["LLM_API_KEY"]:
        api_key = configs["LLM_API_KEY"]
    if "LLM_MODEL" in configs and configs["LLM_MODEL"]:
        model = configs["LLM_MODEL"]
    if "LLM_TIMEOUT_SECONDS" in configs and configs["LLM_TIMEOUT_SECONDS"]:
        try:
            timeout_sec = int(configs["LLM_TIMEOUT_SECONDS"])
        except ValueError:
            pass
    if "LLM_TEMPERATURE" in configs and configs["LLM_TEMPERATURE"]:
        try:
            temp = float(configs["LLM_TEMPERATURE"])
        except ValueError:
            pass

    return LLMConfigResponse(
        base_url=base_url,
        api_key="",
        api_key_masked=_mask_api_key(api_key),
        is_configured=bool(api_key and api_key.strip()),
        model=model,
        timeout_seconds=timeout_sec,
        temperature=temp,
        runtime_editable=True,
    )


@router.put("", response_model=LLMConfigResponse, summary="修改并应用大模型 API 配置")
async def update_llm_config(
    data: LLMConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    """更新大模型 API 基础地址、API Key 及模型配置，即时生效并持久化至数据库。"""
    base_url = _validate_base_url(data.base_url.strip())

    # If api_key is provided and not empty, update it. If omitted or whitespace only, preserve existing key.
    if data.api_key is not None and data.api_key.strip():
        api_key = data.api_key.strip()
    else:
        # Preserve current in-memory / DB key
        api_key = settings.LLM_API_KEY

    model = (data.model or "deepseek-v4-flash-0731").strip()
    timeout_seconds = data.timeout_seconds if data.timeout_seconds is not None else 60
    temperature = data.temperature if data.temperature is not None else 0.0

    # Save to database
    kv_pairs = {
        "LLM_BASE_URL": base_url,
        "LLM_API_KEY": api_key,
        "LLM_MODEL": model,
        "LLM_TIMEOUT_SECONDS": str(timeout_seconds),
        "LLM_TEMPERATURE": str(temperature),
    }

    for k, v in kv_pairs.items():
        stmt = select(SystemConfig).where(SystemConfig.key == k)
        res = await db.execute(stmt)
        item = res.scalar_one_or_none()
        if item:
            item.value = v
        else:
            item = SystemConfig(key=k, value=v, description="LLM Dynamic Configuration")
            db.add(item)

    await db.commit()

    # Apply to in-memory settings immediately
    settings.LLM_BASE_URL = base_url
    settings.LLM_API_KEY = api_key
    settings.LLM_MODEL = model
    settings.LLM_TIMEOUT_SECONDS = timeout_seconds
    settings.LLM_TEMPERATURE = temperature

    logger.info(f"Admin updated LLM configuration: Base URL={base_url}, Model={model}")

    return LLMConfigResponse(
        base_url=base_url,
        api_key="",
        api_key_masked=_mask_api_key(api_key),
        is_configured=bool(api_key),
        model=model,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        runtime_editable=True,
    )


@router.post("/test", summary="测试大模型 API 连通性")
async def test_llm_connection(
    data: Optional[LLMTestRequest] = None,
):
    """
    向指定或当前配置的大模型 API 发送快速探测请求，验证 Base URL 和 API Key 的连通性与可用性。
    """
    base_url = _validate_base_url(
        data.base_url.strip() if data and data.base_url else settings.LLM_BASE_URL
    )
    api_key = data.api_key.strip() if data and data.api_key else settings.LLM_API_KEY
    model = (data.model.strip() if data and data.model else settings.LLM_MODEL)

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40001, "message": "API Key 不能为空，请先输入或配置 API Key"},
        )

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 10,
        "temperature": 0.0,
    }

    start_time = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            latency_ms = int((time.monotonic() - start_time) * 1000)

            if resp.status_code == 200:
                data_json = resp.json()
                choices = data_json.get("choices", [])
                reply = choices[0].get("message", {}).get("content", "").strip() if choices else "OK"
                return {
                    "code": 0,
                    "message": f"连接测试成功！大模型响应正常 (耗时: {latency_ms}ms)",
                    "data": {
                        "status": "success",
                        "latency_ms": latency_ms,
                        "model": model,
                        "response_preview": reply[:100],
                    },
                }
            elif resp.status_code == 401:
                return {
                    "code": 401,
                    "message": "认证失败 (HTTP 401)：API Key 无效或未授权，请检查密钥是否正确",
                    "data": {"status": "auth_failed", "http_status": 401},
                }
            elif resp.status_code == 404:
                return {
                    "code": 404,
                    "message": f"地址错误 (HTTP 404)：未找到端点 {url}，请确认 Base URL 路径是否正确",
                    "data": {"status": "not_found", "http_status": 404},
                }
            else:
                return {
                    "code": resp.status_code,
                    "message": f"大模型服务返回异常 (HTTP {resp.status_code}): {resp.text[:200]}",
                    "data": {"status": "upstream_error", "http_status": resp.status_code},
                }
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": 50401, "message": f"请求超时 (15s)，无法连接至 {url}，请检查网络或 Base URL 是否可达"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": 50201, "message": f"连接大模型接口失败: {str(exc)}"},
        )


@router.post("/models", summary="从上游大模型 API 动态获取可用模型列表")
async def fetch_upstream_models(
    data: Optional[LLMModelsFetchRequest] = None,
):
    """
    向上游大模型服务商 GET {base_url}/models 接口发送请求，动态获取其支持的模型列表。
    """
    base_url = _validate_base_url(
        data.base_url.strip() if data and data.base_url else settings.LLM_BASE_URL
    )
    api_key = data.api_key.strip() if data and data.api_key else settings.LLM_API_KEY

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": 40001, "message": "API Key 不能为空，请先输入或配置 API Key 后再拉取模型列表"},
        )

    url = f"{base_url}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                raw_models = []
                # Standard OpenAI format: {"object": "list", "data": [{"id": "deepseek-chat", ...}]}
                if isinstance(body, dict) and "data" in body and isinstance(body["data"], list):
                    raw_models = [m.get("id") or m.get("name") for m in body["data"] if isinstance(m, dict)]
                # Ollama or other format: {"models": [{"name": "qwen2.5:14b", ...}]}
                elif isinstance(body, dict) and "models" in body and isinstance(body["models"], list):
                    raw_models = [m.get("name") or m.get("model") or m.get("id") for m in body["models"] if isinstance(m, dict)]
                elif isinstance(body, list):
                    raw_models = [m if isinstance(m, str) else (m.get("id") if isinstance(m, dict) else str(m)) for m in body]

                model_ids = sorted(list({str(m).strip() for m in raw_models if m and str(m).strip()}))

                if not model_ids:
                    return {
                        "code": 0,
                        "message": "上游接口响应成功，但未解析出模型标识，可手动输入",
                        "data": {"models": [], "count": 0, "source": url},
                    }

                return {
                    "code": 0,
                    "message": f"成功从 API 获取到 {len(model_ids)} 个可用模型",
                    "data": {
                        "models": model_ids,
                        "count": len(model_ids),
                        "source": url,
                    },
                }
            elif resp.status_code == 401:
                return {
                    "code": 401,
                    "message": "认证失败 (HTTP 401)：API Key 无效或无权访问 /models 接口",
                    "data": {"models": [], "count": 0, "http_status": 401},
                }
            elif resp.status_code == 404:
                return {
                    "code": 404,
                    "message": f"上游接口未实现 /models 列表端点 (HTTP 404: {url})，请手动输入模型名称",
                    "data": {"models": [], "count": 0, "http_status": 404},
                }
            else:
                return {
                    "code": resp.status_code,
                    "message": f"获取模型列表失败 (HTTP {resp.status_code}): {resp.text[:200]}",
                    "data": {"models": [], "count": 0, "http_status": resp.status_code},
                }
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"code": 50401, "message": f"请求超时 (12s)，无法连接至 {url}"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": 50201, "message": f"获取大模型列表异常: {str(exc)}"},
        )
