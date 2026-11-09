from time import monotonic
from types import SimpleNamespace
from dataclasses import replace
from io import BytesIO
import json

import pytest

from competitive.financial import bounds, change, margin, normalize, select
from competitive.intelligence import brief, load, parse_release, scenario
from competitive.sources import Cache, RELEASES, SourceError, sec_urls


def row(start, end, val, filed="2026-11-02", accn="0001048286-26-000001"):
    return dict(start=start, end=end, val=val, filed=filed, accn=accn, form="10-Q")


def facts(**tags):
    return {"facts": {"us-gaap": {key: {"units": {"USD": value}} for key, value in tags.items()}}}


def test_exact_quarter_and_ytd_cash_flow():
    start, end = bounds(2026, 3)
    current = row(start, end, 100)
    q2 = row("2026-01-01", "2026-06-30", 45, "2026-08-01", "0001048286-26-000002")
    ytd = row("2026-01-01", end, 70)
    payload = facts(Revenues=[current, ytd], OperatingIncomeLoss=[row(start, end, 15)],
                    NetCashProvidedByUsedInOperatingActivities=[q2, ytd])
    result = normalize(payload, "MAR", 2026, 3, "retrieved", "facts-url")
    assert result["Revenue"].value == 100
    assert margin(result["Operating income"], result["Revenue"]) == 15
    assert result["Operating cash flow"].value == 25
    assert result["Operating cash flow prior YTD input"].url.endswith(q2["accn"] + "-index.htm")
    assert result["Revenue"].published == current["filed"]
    assert result["Revenue"].retrieved == "retrieved"
    assert result["Revenue"].input_url == "facts-url"
    assert result["Revenue"].unit == "USD"


def test_missing_incompatible_and_conflicting_data():
    start, end = bounds(2026, 3)
    current = row(start, end, 100)
    assert select([current, dict(current, val=110)], start, end) is None
    assert select([current], "2026-01-01", end) is None
    assert normalize(facts(NetCashProvidedByUsedInOperatingActivities=[
        row("2026-01-01", end, 70)]), "MAR", 2026, 3, "now", "facts") == {}
    assert margin(None, None) is None
    metric = normalize(facts(Revenues=[current]), "MAR", 2026, 3, "now", "facts")["Revenue"]
    prior = replace(metric, start="2025-07-01", end="2025-09-30", value=80)
    assert change(metric, prior) == pytest.approx(25)
    assert change(metric, replace(prior, value=0)) is None
    assert change(metric, replace(prior, start="2025-01-01")) is None
    assert change(metric, replace(prior, ticker="H")) is None


def test_historical_and_modeled_results_separate():
    assert scenario(100, 8, 25) == (108, 27)
    assert scenario(None, 8, 25) is None


def test_issuer_release_provenance_and_ambiguity():
    title = "<h1>Hyatt Reports Third Quarter 2026 Results</h1>"
    item = "<p>Comparable system-wide hotels RevPAR increased 4.5%, compared to the third quarter of 2025</p>"
    parsed, _ = parse_release("H", title + item, "timestamp")
    result = parsed["Comparable hotel RevPAR YoY"]
    assert result.value == 4.5 and result.unit == "%"
    assert (result.published, result.url) == RELEASES["H"]
    assert result.retrieved == "timestamp"
    ambiguous, _ = parse_release("H", title + item + item.replace("4.5", "5.0"), "now")
    assert not ambiguous
    with pytest.raises(SourceError):
        parse_release("H", "<p>Access denied</p>", "now")


def test_sec_cache_refresh_and_offline_fallback(tmp_path, monkeypatch):
    payload = facts(Revenues=[row(*bounds(2026, 3), 100)])
    calls = []

    def fetch(request, timeout):
        calls.append(request.full_url)
        if request.full_url == sec_urls("MAR")[0]:
            return BytesIO(json.dumps(payload).encode())
        if request.full_url == sec_urls("MAR")[1]:
            return BytesIO(b"{}")
        raise OSError("issuer unavailable")

    monkeypatch.setattr("competitive.sources.urlopen", fetch)
    monkeypatch.setattr("competitive.sources.time", SimpleNamespace(monotonic=monotonic, sleep=lambda _: None))
    cache = Cache(tmp_path / "cache.sqlite", "Example Analyst analyst@example.org")
    evidence = load(cache, ["MAR"], [2026], 3, refresh=True)
    assert len(calls) == 4
    assert len(evidence.warnings) == 2
    assert evidence.financial["MAR", 2026]["Revenue"].value == 100
    monkeypatch.setattr("competitive.sources.urlopen", lambda *_args, **_kwargs: pytest.fail("Network called offline"))
    cached = load(cache, ["MAR"], [2026], 3)
    assert cached.financial == evidence.financial
    lines, citations = brief(cached, ["MAR"], 2026, 3, "Revenue and margin")
    assert citations == [evidence.financial["MAR", 2026]["Revenue"].url]
    assert any("Operating margin: Data unavailable" in line for line in lines)

    def fail(*_args, **_kwargs):
        raise OSError("connection interrupted")

    monkeypatch.setattr("competitive.sources.urlopen", fail)
    _, original_timestamp, warning = cache.get(sec_urls("MAR")[0], refresh=True)
    assert original_timestamp == evidence.financial["MAR", 2026]["Revenue"].retrieved
    assert "Using cached response" in warning
