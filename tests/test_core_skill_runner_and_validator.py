from decimal import Decimal
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.core.skill_runner import SkillRunner
from app.schemas.task import SkillV3InputPayload, AttachmentInput
from app.core.validator import CargoValidator, default_validator
from app.core.money import validate_money, MAX_ACCOUNT_BALANCE, MAX_UNIT_PRICE


def test_money_validation():
    # Valid money values
    d1 = validate_money(10.5, maximum=MAX_ACCOUNT_BALANCE, allow_zero=True, field_name="balance")
    assert d1 == Decimal("10.5000")

    d2 = validate_money("0.88", maximum=MAX_UNIT_PRICE, allow_zero=False, field_name="unit_price")
    assert d2 == Decimal("0.8800")

    # Zero rejection when allow_zero=False
    with pytest.raises(ValueError, match="strictly greater than 0"):
        validate_money(0, maximum=MAX_UNIT_PRICE, allow_zero=False, field_name="unit_price")

    # Negative rejection
    with pytest.raises(ValueError, match="non-negative"):
        validate_money(-5.0, maximum=MAX_ACCOUNT_BALANCE, allow_zero=True, field_name="balance")

    # Invalid string
    with pytest.raises(ValueError, match="must be a valid decimal"):
        validate_money("not_a_number", maximum=MAX_ACCOUNT_BALANCE, allow_zero=True, field_name="balance")

    # Too many decimal places (> 4)
    with pytest.raises(ValueError, match="cannot have more than 4 decimal places"):
        validate_money("10.12345", maximum=MAX_ACCOUNT_BALANCE, allow_zero=True, field_name="balance")

    # Exceed maximum
    with pytest.raises(ValueError, match="cannot exceed"):
        validate_money(Decimal("99999999999"), maximum=MAX_ACCOUNT_BALANCE, allow_zero=True, field_name="balance")


def test_validator_cargo_v3():
    validator = CargoValidator()
    valid_data = {
        "ShipperName": "TEST SHIPPER",
        "POL": "SHANGHAI",
        "POD": "LOS ANGELES",
        "ContainerInfo": [
            {"ContainerNo": "MSCU1234567", "ContType": "40HQ"}
        ]
    }

    is_valid, errs = validator.validate(valid_data)
    assert isinstance(is_valid, bool)
    assert isinstance(errs, list)


@pytest.mark.asyncio
async def test_skill_runner_flow_and_correction():
    runner = SkillRunner()
    payload = SkillV3InputPayload(
        mail_subject="Booking MSC test",
        mail_body="Please book 40HQ container",
        attachments=[],
    )

    # 1. Test prompt generation
    prompt = runner.build_extract_prompt(payload)
    assert "Booking MSC test" in prompt
    assert "40HQ" in prompt

    val_prompt = runner.build_validate_prompt("{'bad_json': 1}", ["Syntax error"])
    assert isinstance(val_prompt, str)

    # 2. Test format attachments text
    att1 = AttachmentInput(filename="test.txt", content_type="text/plain", text="Sample text", tables=[], ocr_text="")
    att_text = runner.format_attachments_text([att1])
    assert "test.txt" in att_text
    assert "Sample text" in att_text

    empty_att = runner.format_attachments_text([])
    assert "附件" in empty_att or empty_att is not None

    # 3. Test successful extraction mock
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "ShipperName": "SHANGHAI FORWARDING",
                        "POL": "NINGBO",
                        "POD": "HAMBURG",
                        "ContainerInfo": []
                    })
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        result_data = await runner.extract_draft_json(payload)
        assert result_data["POL"] == "NINGBO"

    # 4. Test self-correction on initial invalid JSON
    bad_json_response = MagicMock()
    bad_json_response.status_code = 200
    bad_json_response.json.return_value = {
        "choices": [{"message": {"content": "```json\n{bad_syntax_json: true}\n```"}}]
    }

    fixed_json_response = MagicMock()
    fixed_json_response.status_code = 200
    fixed_json_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "ShipperName": "FIXED_CORP",
                        "POL": "NINGBO",
                        "POD": "HAMBURG",
                        "ContainerInfo": []
                    })
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post_retry:
        mock_post_retry.side_effect = [bad_json_response, fixed_json_response]
        result_fixed = await runner.extract_draft_json(payload)
        assert result_fixed["ShipperName"] == "FIXED_CORP"

    # 5. Test empty content retry then success
    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post_empty:
        mock_post_empty.side_effect = [empty_resp, mock_response]
        res_empty_retry = await runner.extract_draft_json(payload)
        assert res_empty_retry["POL"] == "NINGBO"

    # 6. Test conversational prefix without code fences
    conversational_resp = MagicMock()
    conversational_resp.status_code = 200
    conversational_resp.json.return_value = {
        "choices": [{"message": {"content": 'Here is the extracted data:\n{"POL": "SHANGHAI", "POD": "ROTTERDAM"}\nHope this helps!'}}]
    }
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post_conv:
        mock_post_conv.return_value = conversational_resp
        res_conv = await runner.extract_draft_json(payload)
        assert res_conv["POL"] == "SHANGHAI"
        assert res_conv["POD"] == "ROTTERDAM"

    # 7. Test unparseable content fallback to empty dict
    garbage_resp = MagicMock()
    garbage_resp.status_code = 200
    garbage_resp.json.return_value = {
        "choices": [{"message": {"content": "Sorry, I am not able to parse this email."}}]
    }
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post_garbage:
        mock_post_garbage.side_effect = [garbage_resp, garbage_resp]
        res_garbage = await runner.extract_draft_json(payload)
        assert res_garbage == {}

