import base64
import io
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import ASGITransport, AsyncClient, TimeoutException
from PIL import Image
from sqlalchemy import select
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.main import app, load_dynamic_system_config
from app.models.system import SystemConfig
from app.api.admin.llm_config import VISION_PROBE_DATA_URL, _mask_api_key, _validate_base_url
from app.services.auth_service import create_access_token


@pytest_asyncio.fixture(autouse=True)
async def prepare_db():
    await init_db()


def test_mask_api_key_and_validation():
    # 1. Empty key
    assert _mask_api_key("") == "未配置"
    assert _mask_api_key("   ") == "未配置"

    # 2. Short key (<= 8 chars)
    assert _mask_api_key("123456") == "12****56"

    # 3. Normal key (> 8 chars)
    assert _mask_api_key("sk-1234567890abcdef") == "sk-1...cdef (19 字符)"

    # 4. _validate_base_url errors
    with pytest.raises(Exception):
        _validate_base_url("not_a_url")

    with pytest.raises(Exception):
        _validate_base_url("https://user:pass@api.test.com")

    with pytest.raises(Exception):
        _validate_base_url("https://api.test.com?query=1")

    with pytest.raises(Exception):
        _validate_base_url("https://api.test.com#frag")

    # 5. Valid http/https
    assert _validate_base_url("http://localhost:11434/v1/") == "http://localhost:11434/v1"
    assert _validate_base_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"


@pytest.mark.asyncio
async def test_get_llm_config_auth_and_response():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Without admin secret header -> 403
        res_no_auth = await client.get("/admin/llm-config")
        assert res_no_auth.status_code == 403

        # 2. Insert DB SystemConfig items to test DB override branch in get_llm_config
        async with AsyncSessionLocal() as db:
            db.add(SystemConfig(key="LLM_BASE_URL", value="https://db-llm.example.com/v1"))
            db.add(SystemConfig(key="LLM_API_KEY", value="sk-db-api-key-123456"))
            db.add(SystemConfig(key="LLM_MODEL", value="deepseek-v4-db"))
            db.add(SystemConfig(key="LLM_TIMEOUT_SECONDS", value="90"))
            db.add(SystemConfig(key="LLM_TEMPERATURE", value="0.5"))
            await db.commit()

        # 3. With admin secret header
        res = await client.get(
            "/admin/llm-config",
            headers={"X-Admin-Secret": settings.ADMIN_SECRET_KEY},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["base_url"] == "https://db-llm.example.com/v1"
        assert data["model"] == "deepseek-v4-db"
        assert data["timeout_seconds"] == 90
        assert data["temperature"] == 0.5
        assert "sk-d...3456" in data["api_key_masked"]
        assert data["runtime_editable"] is True


@pytest.mark.asyncio
async def test_update_llm_config_validation_and_persistence():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

        # 1. Invalid URL scheme (not http/https) -> 422
        res_invalid = await client.put(
            "/admin/llm-config",
            json={
                "base_url": "ftp://invalid-url.com",
                "api_key": "test_key_123",
            },
            headers=headers,
        )
        assert res_invalid.status_code == 422

        # 2. Valid update with new API key
        new_url = "https://custom-llm.example.com/v1"
        new_key = "sk-custom-test-api-key-99999"
        res_ok = await client.put(
            "/admin/llm-config",
            json={
                "base_url": new_url,
                "api_key": new_key,
                "model": "deepseek-custom-model",
                "timeout_seconds": 45,
                "temperature": 0.2,
            },
            headers=headers,
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["base_url"] == new_url
        assert data["model"] == "deepseek-custom-model"
        assert data["timeout_seconds"] == 45
        assert data["temperature"] == 0.2
        assert "sk-c...9999" in data["api_key_masked"]

        # 3. Model switch WITHOUT re-entering API key (retains existing key)
        res_switch_model = await client.put(
            "/admin/llm-config",
            json={
                "base_url": new_url,
                "api_key": "",  # Empty -> preserve key
                "model": "deepseek-chat-v3-switched",
            },
            headers=headers,
        )
        assert res_switch_model.status_code == 200
        assert res_switch_model.json()["model"] == "deepseek-chat-v3-switched"
        assert settings.LLM_API_KEY == new_key  # Key preserved!

        # Verify load_dynamic_system_config reloads from DB
        settings.LLM_BASE_URL = ""
        await load_dynamic_system_config()
        assert settings.LLM_BASE_URL == new_url
        assert settings.LLM_MODEL == "deepseek-chat-v3-switched"


@pytest.mark.asyncio
async def test_production_llm_config_is_read_only_and_ignores_database_overrides():
    transport = ASGITransport(app=app)
    admin_token = create_access_token("admin", role="admin")
    with (
        patch.object(settings, "ENVIRONMENT", "production"),
        patch.object(settings, "LLM_BASE_URL", "https://deployment.test/v1"),
        patch.object(settings, "LLM_API_KEY", "deployment-secret"),
        patch.object(settings, "LLM_MODEL", "deployment-model"),
        patch.object(settings, "VISION_LLM_ENABLED", True),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {admin_token}"}
            current = await client.get("/admin/llm-config", headers=headers)
            update = await client.put(
                "/admin/llm-config",
                json={"base_url": "https://ignored.test/v1", "model": "ignored-model"},
                headers=headers,
            )

    assert current.status_code == 200
    assert current.json()["runtime_editable"] is False
    assert current.json()["base_url"] == "https://deployment.test/v1"
    assert current.json()["model"] == "deployment-model"
    assert update.status_code == 409


@pytest.mark.asyncio
async def test_test_llm_connection_all_branches():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

        # 1. Empty API key when none in settings -> 400
        with patch.object(settings, "LLM_API_KEY", ""):
            res_empty_key = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": ""},
                headers=headers,
            )
            assert res_empty_key.status_code == 400

        # Helper for mocking internal httpx inside the route
        def create_mock_ctx(mock_resp=None, side_effect=None):
            mock_client = AsyncMock()
            if side_effect:
                mock_client.post.side_effect = side_effect
                mock_client.get.side_effect = side_effect
            else:
                mock_client.post.return_value = mock_resp
                mock_client.get.return_value = mock_resp
            mock_ctx = MagicMock()
            mock_ctx.__aenter__.return_value = mock_client
            mock_ctx.__aexit__.return_value = None
            return mock_ctx

        # 2. Success 200
        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = {
            "choices": [{"message": {"content": "pong response"}}]
        }
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_200)):
            res_200 = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_200.status_code == 200
            assert res_200.json()["code"] == 0
            assert "pong response" in res_200.json()["data"]["response_preview"]

        # 3. Auth Failed 401
        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_401)):
            res_401 = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "bad_key"},
                headers=headers,
            )
            assert res_401.status_code == 200
            assert res_401.json()["code"] == 401

        # 4. Not Found 404
        mock_resp_404 = MagicMock()
        mock_resp_404.status_code = 404
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_404)):
            res_404 = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_404.status_code == 200
            assert res_404.json()["code"] == 404

        # 5. Upstream 500
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_500)):
            res_500 = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_500.status_code == 200
            assert res_500.json()["code"] == 500

        # 6. Timeout -> 504
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(side_effect=TimeoutException("Timeout"))):
            res_timeout = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_timeout.status_code == 504

        # 7. General Exception -> 502
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(side_effect=RuntimeError("Connection refused"))):
            res_err = await client.post(
                "/admin/llm-config/test",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_err.status_code == 502


@pytest.mark.asyncio
async def test_vision_connection_probe_contains_an_image():
    probe_bytes = base64.b64decode(VISION_PROBE_DATA_URL.split(",", 1)[1])
    with Image.open(io.BytesIO(probe_bytes)) as probe_image:
        probe_image.load()
        assert probe_image.size == (128, 128)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_client
    mock_ctx.__aexit__.return_value = None

    with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=mock_ctx):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/admin/llm-config/test",
                json={
                    "base_url": "https://api.test.com/v1",
                    "api_key": "valid-key",
                    "model": "text-model",
                    "vision_enabled": True,
                    "vision_model": "vision-model",
                },
                headers={"X-Admin-Secret": settings.ADMIN_SECRET_KEY},
            )

    assert response.status_code == 200
    assert mock_client.post.await_count == 2
    vision_payload = mock_client.post.await_args_list[1].kwargs["json"]
    vision_content = vision_payload["messages"][0]["content"]
    assert isinstance(vision_content, list)
    assert vision_content[1]["type"] == "image_url"
    assert vision_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_fetch_upstream_models_all_branches():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Admin-Secret": settings.ADMIN_SECRET_KEY}

        # 1. Empty API key when none in settings -> 400
        with patch.object(settings, "LLM_API_KEY", ""):
            res_empty_key = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": ""},
                headers=headers,
            )
            assert res_empty_key.status_code == 400

        # Helper
        def create_mock_ctx(mock_resp=None, side_effect=None):
            mock_client = AsyncMock()
            if side_effect:
                mock_client.get.side_effect = side_effect
            else:
                mock_client.get.return_value = mock_resp
            mock_ctx = MagicMock()
            mock_ctx.__aenter__.return_value = mock_client
            mock_ctx.__aexit__.return_value = None
            return mock_ctx

        # 2. Success 200 - OpenAI format
        mock_resp_openai = MagicMock()
        mock_resp_openai.status_code = 200
        mock_resp_openai.json.return_value = {
            "object": "list",
            "data": [
                {"id": "deepseek-chat", "object": "model"},
                {"id": "deepseek-reasoner", "object": "model"},
            ]
        }
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_openai)):
            res_200 = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.deepseek.com/v1", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_200.status_code == 200
            assert res_200.json()["code"] == 0
            assert res_200.json()["data"]["count"] == 2
            assert "deepseek-chat" in res_200.json()["data"]["models"]

        # 3. Success 200 - Ollama format
        mock_resp_ollama = MagicMock()
        mock_resp_ollama.status_code = 200
        mock_resp_ollama.json.return_value = {
            "models": [{"name": "qwen2.5:14b"}, {"name": "deepseek-r1:14b"}]
        }
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_ollama)):
            res_ollama = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "http://localhost:11434/v1", "api_key": "ollama"},
                headers=headers,
            )
            assert res_ollama.status_code == 200
            assert "qwen2.5:14b" in res_ollama.json()["data"]["models"]

        # 4. Success 200 - Empty models array
        mock_resp_empty = MagicMock()
        mock_resp_empty.status_code = 200
        mock_resp_empty.json.return_value = {"data": []}
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_empty)):
            res_empty = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_empty.status_code == 200
            assert res_empty.json()["data"]["count"] == 0

        # 5. Auth Failed 401
        mock_resp_401 = MagicMock()
        mock_resp_401.status_code = 401
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_401)):
            res_401 = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "bad_key"},
                headers=headers,
            )
            assert res_401.status_code == 200
            assert res_401.json()["code"] == 401

        # 6. Not Found 404
        mock_resp_404 = MagicMock()
        mock_resp_404.status_code = 404
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_404)):
            res_404 = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_404.status_code == 200
            assert res_404.json()["code"] == 404

        # 7. Upstream 500
        mock_resp_500 = MagicMock()
        mock_resp_500.status_code = 500
        mock_resp_500.text = "Internal Server Error"
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(mock_resp_500)):
            res_500 = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_500.status_code == 200
            assert res_500.json()["code"] == 500

        # 8. Timeout -> 504
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(side_effect=TimeoutException("Timeout"))):
            res_timeout = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_timeout.status_code == 504

        # 9. General Exception -> 502
        with patch("app.api.admin.llm_config.httpx.AsyncClient", return_value=create_mock_ctx(side_effect=RuntimeError("DNS failure"))):
            res_err = await client.post(
                "/admin/llm-config/models",
                json={"base_url": "https://api.test.com", "api_key": "valid_key"},
                headers=headers,
            )
            assert res_err.status_code == 502


@pytest.mark.asyncio
async def test_direct_llm_config_functions():
    from app.api.admin.llm_config import get_llm_config, update_llm_config
    from app.schemas.system import LLMConfigUpdate

    async with AsyncSessionLocal() as db:
        # 1. Update config directly
        up = LLMConfigUpdate(
            base_url="https://direct.test.com/v1",
            api_key="sk-direct-key-123456789",
            model="deepseek-direct",
            timeout_seconds=50,
            temperature=0.1,
        )
        res_up = await update_llm_config(data=up, db=db)
        assert res_up.base_url == "https://direct.test.com/v1"
        assert res_up.model == "deepseek-direct"

        # 2. Get config with valid DB values
        res_get = await get_llm_config(db=db)
        assert res_get.base_url == "https://direct.test.com/v1"
        assert res_get.timeout_seconds == 50

        # 3. Get config with invalid timeout / temperature in DB to trigger ValueError fallback
        stmt_t = select(SystemConfig).where(SystemConfig.key == "LLM_TIMEOUT_SECONDS")
        item_t = (await db.execute(stmt_t)).scalar_one_or_none()
        if item_t:
            item_t.value = "not_an_int"

        stmt_temp = select(SystemConfig).where(SystemConfig.key == "LLM_TEMPERATURE")
        item_temp = (await db.execute(stmt_temp)).scalar_one_or_none()
        if item_temp:
            item_temp.value = "not_a_float"

        await db.commit()
        res_fallback = await get_llm_config(db=db)
        assert isinstance(res_fallback.timeout_seconds, int)
        assert isinstance(res_fallback.temperature, float)
