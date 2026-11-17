"""Prices in dollars (issue #32): one way to write an amount wherever a person reads one."""

from __future__ import annotations

from decimal import Decimal

from foood.money import CURRENCY, dollars


def test_dollars_as_people_read_them() -> None:
    assert dollars(15.5) == "$15.50"
    assert dollars("15.50") == "$15.50"
    assert dollars(Decimal("3")) == "$3.00"
    assert dollars(1234.5) == "$1,234.50"
    assert dollars(-2.5) == "-$2.50"


def test_a_difference_carries_its_sign() -> None:
    assert dollars(2.5, signed=True) == "+$2.50"
    assert dollars(-2.5, signed=True) == "-$2.50"
    assert dollars(0, signed=True) == "$0.00"


def test_the_platform_currency_is_the_dollar() -> None:
    assert CURRENCY == "USD"
