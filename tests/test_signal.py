from datetime import date

from src_center.scoring import signal, subscores

TODAY = date(2026, 9, 28)


def base_facts(level=0.0, momentum=0.0, z=None, above=True, n7=20, **overrides):
    facts = {
        "technical": {"price": 100.0, "price_date": "2026-09-26", "above_sma200": above,
                      "pct_vs_sma200": 0.1 if above else -0.1, "sma50_slope_20d": 0.03 if above else -0.03,
                      "change_90d": 0.1 if above else -0.1, "change_7d": 0.02, "rsi14": 55,
                      "drawdown_from_52w_high": -0.2},
        "sentiment": {"level_7d": level, "momentum": momentum, "n_relevant_7d": n7, "attention_ratio_90d": 1.5},
        "context": {"fear_greed": 50, "vix": 18, "spy_above_sma200": True},
        "stock_meta": {"revenue_growth": 0.15, "analyst_rec_mean": 2.0, "target_upside": 0.1, "put_call_volume": 0.8},
    }
    if z is not None:
        facts["sentiment"]["z"] = z
    for path, value in overrides.items():
        section, key = path.split("__")
        facts.setdefault(section, {})[key] = value
    return facts


def test_strong_bullish_setup_is_buy():
    res = signal.decide("stock", "XYZ", base_facts(level=0.6, momentum=0.3), today=TODAY)
    assert res.signal == "BUY", res
    assert res.composite >= 65
    assert res.drivers


def test_bearish_setup_is_sell():
    f = base_facts(level=-0.6, momentum=-0.3, above=False,
                   stock_meta__revenue_growth=-0.1, stock_meta__analyst_rec_mean=3.5, stock_meta__target_upside=-0.1)
    res = signal.decide("stock", "XYZ", f, today=TODAY)
    assert res.signal == "SELL", res


def test_neutral_is_hold():
    res = signal.decide("stock", "XYZ", base_facts(level=0.05, momentum=0.0), today=TODAY)
    assert res.signal == "HOLD"


def test_insufficient_texts_gives_no_signal():
    res = signal.decide("stock", "XYZ", base_facts(level=0.6, momentum=0.3, n7=1), today=TODAY)
    assert res.signal == "NO SIGNAL"
    assert any("relevant texts" in f for f in res.flags)


def test_stale_prices_give_no_signal():
    f = base_facts(level=0.6, momentum=0.3, technical__price_date="2026-09-01")
    assert signal.decide("stock", "XYZ", f, today=TODAY).signal == "NO SIGNAL"


def test_euphoria_with_crowding_caps_at_hold():
    f = base_facts(level=0.8, momentum=0.4, z=3.0, stock_meta__put_call_volume=0.3)
    res = signal.decide("stock", "XYZ", f, today=TODAY)
    assert res.subscores["positioning"] < 40
    if res.composite >= 65:
        assert res.signal == "HOLD" and res.rule == "euphoria cap"


def test_below_sma200_needs_momentum_for_buy():
    f = base_facts(level=0.9, momentum=0.05, above=False, context__fear_greed=20)
    res = signal.decide("stock", "XYZ", f, today=TODAY)
    assert res.signal != "BUY" or res.subscores["sentiment_momentum"] >= 70


def test_bearish_divergence_sell():
    f = {"technical": {"price": 100.0, "price_date": "2026-09-26", "above_sma200": True, "pct_vs_sma200": 0.3,
                       "sma50_slope_20d": 0.05, "change_90d": 0.4, "change_7d": 0.05, "rsi14": 70,
                       "drawdown_from_52w_high": -0.01},
         "sentiment": {"level_7d": 0.2, "momentum": -0.4, "n_relevant_7d": 30},
         "context": {"fear_greed": 80},
         "derivatives": {"funding_rate_7d_avg": 0.0008, "long_short_ratio": 3.2},
         "crypto_meta": {"fdv_mc_ratio": 1.0}}
    res = signal.decide("crypto", "ABC", f, today=TODAY)
    assert res.signal == "SELL" and res.rule == "bearish divergence", res


def test_earnings_flag():
    f = base_facts(level=0.3, stock_meta__next_earnings="2026-09-30")
    res = signal.decide("stock", "XYZ", f, today=TODAY)
    assert any("earnings in 2" in x for x in res.flags)


def test_subscore_ranges_and_monotonicity():
    lo, _ = subscores.sentiment_level({"level_7d": -0.5})
    mid, _ = subscores.sentiment_level({"level_7d": 0.0})
    hi, _ = subscores.sentiment_level({"level_7d": 0.5})
    assert 0 <= lo < mid < hi <= 100 and mid == 50
    crowded, _ = subscores.positioning_crypto({"funding_rate_7d_avg": 0.0008}, 0.1)
    shorts, _ = subscores.positioning_crypto({"funding_rate_7d_avg": -0.0003}, 0.1)
    assert crowded < 40 < 60 < shorts


def test_missing_subscores_are_reweighted():
    f = base_facts(level=0.6, momentum=0.3)
    f.pop("stock_meta")
    res = signal.decide("stock", "XYZ", f, today=TODAY)
    assert res.subscores["fundamentals"] is None
    assert res.composite is not None
