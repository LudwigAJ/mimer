"""Stooq market-series classification: generic series, never securities.

Guards the critical modelling rule: a sovereign benchmark yield/price series is a
country/tenor generic series (NOT an ISIN-level bond), and a ``.F`` rates-futures series is
a root/continuous series (NOT an expiry-specific tradable contract).
"""

from __future__ import annotations

import pytest

from app.sources import stooq_market_series as sms

# Examples supplied by the user (sovereign benchmark *yield* series).
_YIELD_SYMBOLS = ["3MDEY.B", "6MDEY.B", "1YDEY.B", "2YDEY.B", "1MFRY.B", "2YFRY.B"]
# Sovereign benchmark *price* series.
_PRICE_SYMBOLS = ["1YDEP.B", "2YDEP.B", "2YFRP.B", "10YFRP.B", "2YITP.B", "10YITP.B", "5YUKP.B"]
# Rates futures *root* series.
_FUTURES_SYMBOLS = ["G.F", "GG.F", "GX.F", "HF.F", "HR.F", "IM.F", "ZB.F", "ZF.F", "ZN.F", "ZT.F"]


@pytest.mark.parametrize("symbol", _YIELD_SYMBOLS)
def test_yield_symbols_classify_as_sovereign_yield_benchmark_series(symbol: str) -> None:
    c = sms.classify_stooq_symbol(symbol)
    assert c.category == sms.SOVEREIGN_YIELD_BENCHMARK_SERIES
    # Never a bond / security.
    assert c.is_bond is False
    assert c.is_security is False
    assert c.is_expiry_specific_future is False
    assert c.country and c.tenor


@pytest.mark.parametrize("symbol", _PRICE_SYMBOLS)
def test_price_symbols_classify_as_sovereign_benchmark_price_series(symbol: str) -> None:
    c = sms.classify_stooq_symbol(symbol)
    assert c.category == sms.SOVEREIGN_BENCHMARK_PRICE_SERIES
    assert c.is_bond is False
    assert c.is_security is False


@pytest.mark.parametrize("symbol", _FUTURES_SYMBOLS)
def test_futures_symbols_classify_as_rates_futures_series(symbol: str) -> None:
    c = sms.classify_stooq_symbol(symbol)
    assert c.category == sms.RATES_FUTURES_SERIES
    # A root/continuous series — never an expiry-specific contract or a bond.
    assert c.is_expiry_specific_future is False
    assert c.is_bond is False
    assert c.is_security is False


def test_specific_germany_10y_examples() -> None:
    assert sms.classify_stooq_symbol("10YDEY.B").category == sms.SOVEREIGN_YIELD_BENCHMARK_SERIES
    assert sms.classify_stooq_symbol("10YDEP.B").category == sms.SOVEREIGN_BENCHMARK_PRICE_SERIES


def test_no_classification_ever_marks_a_bond_or_security() -> None:
    for symbol in _YIELD_SYMBOLS + _PRICE_SYMBOLS + _FUTURES_SYMBOLS:
        c = sms.classify_stooq_symbol(symbol)
        assert not c.is_bond
        assert not c.is_security
        assert not c.is_expiry_specific_future


def test_expiry_specific_symbol_is_not_treated_as_a_future() -> None:
    # ZNM6 looks like an expiry-specific T-Note contract (ZN + month M + year 6); we do NOT
    # model specific contracts, so it is 'unknown', never a rates_futures_series.
    c = sms.classify_stooq_symbol("ZNM6")
    assert c.category == sms.UNKNOWN
    assert c.is_expiry_specific_future is False


def test_unknown_symbol() -> None:
    c = sms.classify_stooq_symbol("AAPL.US")
    assert c.category == sms.UNKNOWN
    assert sms.is_market_series_symbol("AAPL.US") is False


def test_is_market_series_symbol_for_known_categories() -> None:
    assert sms.is_market_series_symbol("10YDEY.B") is True
    assert sms.is_market_series_symbol("ZN.F") is True
