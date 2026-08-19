from decimal import Decimal, InvalidOperation
from typing import Any


MONEY_DECIMAL_PLACES = 4
MONEY_QUANTUM = Decimal("0.0001")

# Mirrors the precision of the SQLAlchemy NUMERIC columns.
MAX_ACCOUNT_BALANCE = Decimal("99999999.9999")  # NUMERIC(12, 4)

# Business limits. Keep these stricter than the database column limits so a
# malformed or direct API request cannot create unreasonable billing values.
MIN_UNIT_PRICE = Decimal("0.0100")
MAX_UNIT_PRICE = Decimal("100.0000")
MIN_RECHARGE_AMOUNT = Decimal("0.0100")
MAX_RECHARGE_AMOUNT = Decimal("1000000.0000")


def validate_money(
    value: Any,
    *,
    maximum: Decimal,
    allow_zero: bool,
    field_name: str,
    minimum: Decimal | None = None,
) -> Decimal:
    """Return a database-safe Decimal or raise ValueError for invalid money input."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal amount") from exc

    if not amount.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if amount < 0 or (not allow_zero and amount == 0):
        comparison = "non-negative" if allow_zero else "strictly greater than 0"
        raise ValueError(f"{field_name} must be {comparison}")
    if minimum is not None and amount < minimum:
        raise ValueError(f"{field_name} cannot be less than {minimum}")
    if amount > maximum:
        raise ValueError(f"{field_name} cannot exceed {maximum}")
    if amount.as_tuple().exponent < -MONEY_DECIMAL_PLACES:
        raise ValueError(f"{field_name} cannot have more than {MONEY_DECIMAL_PLACES} decimal places")
    return amount.quantize(MONEY_QUANTUM)
