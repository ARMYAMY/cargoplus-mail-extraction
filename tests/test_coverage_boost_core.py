import json
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import pytest

from app.config import settings
from app.core.normalizer import CargoNormalizer, default_normalizer
from app.core.validator import CargoValidator
from app.core.skill_runner import SkillRunner
from app.schemas.task import SkillV3InputPayload


def test_normalizer_goods_type_and_container_branches():
    norm = CargoNormalizer()

    # 1. GoodsType normalization branches
    assert norm._normalize_goods_type("S") == "S"
    assert norm._normalize_goods_type("R") == "R"
    assert norm._normalize_goods_type("D") == "D"
    assert norm._normalize_goods_type("O") == "O"
    assert norm._normalize_goods_type("冷藏集装箱") == "R"
    assert norm._normalize_goods_type("REEFER CARGO") == "R"
    assert norm._normalize_goods_type("DG CLASS 9") == "D"
    assert norm._normalize_goods_type("危险品化工品") == "D"
    assert norm._normalize_goods_type("OOG FLAT RACK") == "O"
    assert norm._normalize_goods_type("超标货物") == "O"
    assert norm._normalize_goods_type("普货标准件") == "S"
    assert norm._normalize_goods_type("UNKNOWN_TYPE_123") == "S"
    assert norm._normalize_goods_type("") == "S"

    # 2. Container normalization branches
    size, c_type, unmatch = norm._normalize_container_type("40", "HQ")
    assert size == "40"
    assert c_type == "HQ"
    assert unmatch == ""

    # Combined 40HQ
    size2, c_type2, unmatch2 = norm._normalize_container_type("40HQ", "")
    assert size2 == "40"
    assert c_type2 == "HQ"

    # Unmatched
    size3, c_type3, unmatch3 = norm._normalize_container_type("99UNKNOWN", "SPECIAL")
    assert unmatch3 != ""

    # Empty candidate
    size4, c_type4, unmatch4 = norm._normalize_container_type("", "")
    assert size4 == ""
    assert c_type4 == ""

    # 3. Remark appending
    r1 = norm._append_remark("", "Warning 1")
    assert r1 == "Warning 1"
    r2 = norm._append_remark("Warning 1", "Warning 2")
    assert "Warning 1; Warning 2" in r2
    r3 = norm._append_remark("Warning 1", "Warning 1")
    assert r3 == "Warning 1"
    r4 = norm._append_remark("Warning 1", "")
    assert r4 == "Warning 1"

    # 4. Contact and Address splitting
    addr = norm._strip_party_name_from_address("SHANGHAI FORWARDING CO", "SHANGHAI FORWARDING CO\nRoom 101, No 200 Road")
    assert "Room 101" in addr

    # 5. Full normalizer call with edge containers and party info
    draft_input = {
        "ShipperName": "MY SHIPPER",
        "ShipperAddr": "MY SHIPPER\nTEL: +86-21-12345678\nFAX: +86-21-87654321\nEMAIL: info@shipper.com\nAddress Line 1",
        "Packages": "500 CTNS",
        "GrossWeight": "12500 KGS",
        "Volume": "45 CBM",
        "GoodsName": "ELECTRONIC COMPONENTS / 电子元件",
        "GoodsType": "冷藏",
        "ContainerInfo": [
            {
                "ContainerNo": "MSCU1234567",
                "ContSize": "40",
                "ContType": "HQ",
                "KGS": "12000 KGS",
                "CBM": "40 CBM",
                "PCS": "500 CTNS",
            },
            {
                "ContainerNo": "COSU9988776",
                "ContSize": "99INVALID",
                "ContType": "",
            }
        ]
    }
    normalized = norm.normalize(draft_input)
    assert normalized["ShipperTel"] == "+86-21-12345678"
    assert normalized["Packages"] == "500"
    assert normalized["PackagesUnit"] == "CTNS"
    assert normalized["GoodsType"] == "R"
    assert len(normalized["ContainerInfo"]) == 2


def test_validator_custom_paths(tmp_path):
    # 1. Non-existent schema fallback
    v_none = CargoValidator(schema_path=str(tmp_path / "non_existent.json"))
    is_valid, errs = v_none.validate({"some_key": "val"})
    assert is_valid is True

    # 2. Corrupt schema file
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{bad_json", encoding="utf-8")
    v_corrupt = CargoValidator(schema_path=str(corrupt_file))
    is_valid_c, _ = v_corrupt.validate({})
    assert is_valid_c is True


@pytest.mark.asyncio
async def test_skill_runner_retries_and_fallback():
    runner = SkillRunner()

    # 1. Test 5xx retry and success
    resp_500 = MagicMock()
    resp_500.status_code = 502
    resp_500.text = "Bad Gateway"

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": "{\"ShipperName\": \"RETRY_OK\"}"}}]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_post.side_effect = [resp_500, resp_200]
        res = await runner.call_llm("test prompt")
        assert "RETRY_OK" in res

    # 2. Test timeout retry and exception handling
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_timeout, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_timeout.side_effect = [httpx.TimeoutException("Timeout"), resp_200]
        res_timeout = await runner.call_llm("timeout prompt")
        assert "RETRY_OK" in res_timeout

    # 3. A process-level outbound network restriction produces an actionable error.
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_connect, \
         patch("asyncio.sleep", new_callable=AsyncMock), \
         patch.object(settings, "LLM_MAX_RETRIES", 0), \
         patch.object(settings, "LLM_FALLBACK_MODEL", ""):
        mock_connect.side_effect = httpx.ConnectError("All connection attempts failed")
        with pytest.raises(RuntimeError, match="外网访问权限"):
            await runner.call_llm("connection failure")
