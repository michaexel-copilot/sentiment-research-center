from src_center import config
from src_center.models import Asset
from src_center.universe import builders


def _stocks():
    spec = [  # symbol, name, universes, market cap
        ("NVDA", "Nvidia", ["sp500", "nasdaq100"], 50), ("GOOGL", "Alphabet Inc. (Class A)", ["sp500", "nasdaq100"], 42),
        ("GOOG", "Alphabet Inc. (Class C)", ["sp500", "nasdaq100"], 41), ("AAPL", "Apple Inc.", ["sp500", "nasdaq100"], 45),
        ("BRK.B", "Berkshire Hathaway", ["sp500"], 30), ("JPM", "JPMorgan Chase", ["sp500"], 20),
        ("WMT", "Walmart", ["sp500", "nasdaq100"], 25), ("XOM", "ExxonMobil", ["sp500"], 10),
    ]
    assets = {f"stock:{s}": Asset(s, n, "stock", universes=list(u)) for s, n, u, _ in spec}
    caps = {f"stock:{s}": float(c) for s, _, _, c in spec}
    return assets, caps


def _cfg(monkeypatch, n=2):
    s = dict(config.settings())
    s["universe"] = {**s["universe"], "observed": {"crypto": 2, "nasdaq100": n, "sp500": n, "distinct_stocks": True}}
    monkeypatch.setattr(config, "settings", lambda: s)


def test_top_lists_distinct_and_share_class_dedupe(monkeypatch):
    _cfg(monkeypatch, n=3)
    assets, caps = _stocks()
    monkeypatch.setattr(builders, "_market_caps", lambda stocks: caps)
    tags = builders._tag_top(assets, ["sp500", "nasdaq100"])
    nas = [a.symbol for a in assets.values() if "nasdaq100_top" in a.universes]
    sp = [a.symbol for a in assets.values() if "sp500_top" in a.universes]
    assert sorted(nas) == ["AAPL", "GOOGL", "NVDA"]      # GOOG deduped as the same company
    assert sorted(sp) == ["BRK.B", "JPM", "WMT"]          # skips Nasdaq picks -> 6 distinct stocks
    assert tags == {"nasdaq100_top", "sp500_top"}


def test_rate_limited_caps_keep_previous_lists(monkeypatch):
    _cfg(monkeypatch)
    assets, caps = _stocks()
    monkeypatch.setattr(builders, "_market_caps", lambda stocks: {k: 0.0 for k in caps})
    tags = builders._tag_top(assets, ["sp500", "nasdaq100"])
    assert tags == set()
    assert not any("_top" in u for a in assets.values() for u in a.universes)


def test_clean_name():
    assert builders._clean_name("Lilly (Eli)") == "Eli Lilly"
    assert builders._clean_name("Alphabet Inc. (Class A)") == "Alphabet Inc. (Class A)"
    assert Asset("GOOGL", "Alphabet Inc. (Class A)", "stock").short_name == "Alphabet"
