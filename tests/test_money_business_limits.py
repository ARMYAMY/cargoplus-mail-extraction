from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.money import (
    MAX_RECHARGE_AMOUNT,
    MAX_UNIT_PRICE,
    MIN_RECHARGE_AMOUNT,
    MIN_UNIT_PRICE,
    validate_money,
)
from app.schemas.tenant import RechargeRequest, TenantCreate, UpdateUnitPriceRequest


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (UpdateUnitPriceRequest, {"unit_price": Decimal("0.0099")}),
        (UpdateUnitPriceRequest, {"unit_price": Decimal("100.0001")}),
        (RechargeRequest, {"amount": Decimal("0.0099")}),
        (RechargeRequest, {"amount": Decimal("1000000.0001")}),
        (TenantCreate, {"name": "限额测试", "unit_price": Decimal("0")}),
        (TenantCreate, {"name": "限额测试", "initial_balance": Decimal("1000000.0001")}),
    ],
)
def test_request_models_reject_money_outside_business_limits(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


def test_request_models_accept_exact_business_boundaries():
    assert UpdateUnitPriceRequest(unit_price=MIN_UNIT_PRICE).unit_price == MIN_UNIT_PRICE
    assert UpdateUnitPriceRequest(unit_price=MAX_UNIT_PRICE).unit_price == MAX_UNIT_PRICE
    assert RechargeRequest(amount=MIN_RECHARGE_AMOUNT).amount == MIN_RECHARGE_AMOUNT
    assert RechargeRequest(amount=MAX_RECHARGE_AMOUNT).amount == MAX_RECHARGE_AMOUNT


def test_service_money_validation_uses_explicit_minimum():
    with pytest.raises(ValueError, match="cannot be less than"):
        validate_money(
            Decimal("0.0099"),
            minimum=MIN_UNIT_PRICE,
            maximum=MAX_UNIT_PRICE,
            allow_zero=False,
            field_name="Unit price",
        )
