"""Money as people read it.

Prices travel the API as decimal strings (:func:`foood.models.money`); this module is for
prose: notifications, questions to support, the CLI.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: The platform's currency, as the API reports it.
CURRENCY = "USD"


def dollars(amount: Decimal | float | str | int, *, signed: bool = False) -> str:
    """``$15.50``; a difference with ``signed=True`` reads ``+$2.50`` or ``-$2.50``."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else "+" if signed and value > 0 else ""
    return f"{sign}${abs(value):,.2f}"
