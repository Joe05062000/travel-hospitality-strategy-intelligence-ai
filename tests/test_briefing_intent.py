import pytest

from competitive.executive import clarification, executive_evidence
from competitive.financial import Metric
from competitive.intelligence import Evidence


def metric(ticker, name, value, unit="USD", end="2026-09-30"):
    return Metric(ticker, name, "2026-07-01", end, value, unit,
                  f"https://www.sec.gov/filings/{ticker}", "2026-11-01", "now", name)


@pytest.mark.parametrize("question", ["which has the highest lost", "Who had the biggest loss?"])
def test_loss_requires_clarification(question):
    assert "reported net loss, operating loss" in clarification(question)


@pytest.mark.parametrize("values, expected", [
    ((-8, -15), "WH has the lowest net result (largest net loss)"),
    ((0, 15), "No negative result was reported"),
    ((-8, -8), "CHH, WH tie for"),
    ((None, -15), "Only one selected issuer"),
])
def test_hotel_net_loss_distinguishes_losses_zero_ties_and_missing(values, expected):
    tickers = ["CHH", "WH"]
    name = "Net income attributable to parent"
    financial = {(ticker, 2026): {name: metric(ticker, name, value * 1e6)}
                 for ticker, value in zip(tickers, values) if value is not None}
    evidence = Evidence(financial, {}, {}, [], [], industries=dict.fromkeys(tickers, "Hospitality"))
    observations, limitations = executive_evidence(evidence, tickers, 2026, "Who had the highest net loss?")
    assert list(observations) == ["net_income"]
    assert observations["net_income"].text.startswith(expected)
    assert len(observations["net_income"].sources) == sum(value is not None for value in values)
    if values[0] is None:
        assert any("CHH" in note and "unavailable" in note for note in limitations)


@pytest.mark.parametrize("tickers, industry, name, question, end, expected", [
    (["DAL", "UAL"], "Aviation", "Passenger load factor", "Which has the highest load factor?",
     "2026-09-30", "UAL has the highest Passenger load factor"),
    (["RCL", "CCL"], "Cruise", "Cruise occupancy", "Which has the highest cruise occupancy?",
     "2026-08-31", "No like-for-like ranking"),
])
def test_operating_question_uses_requested_measure_and_reporting_period(tickers, industry, name, question, end, expected):
    operating = {tickers[0]: {name: metric(tickers[0], name, 85, "%")},
                 tickers[1]: {name: metric(tickers[1], name, 92, "%", end)}}
    evidence = Evidence({}, operating, {}, [], [], industries=dict.fromkeys(tickers, industry))
    observations, _ = executive_evidence(evidence, tickers, 2026, question)
    assert list(observations) == [name]
    assert observations[name].text.startswith(expected)
    assert observations[name].sources[1].end == end


def test_travel_risk_uses_risk_disclosure_instead_of_first_signal():
    url = "https://example.com/disclosure"
    signals = {"RCL": [("Development", "An opening.", url, "2026-10-27", "now"),
                       ("Financial risks", "Refinancing exposure.", url, "2026-10-27", "now")]}
    evidence = Evidence({}, {}, signals, [], [])
    observations, _ = executive_evidence(evidence, ["RCL"], 2026, "What strategic risks were disclosed?")
    assert list(observations) == ["signals"]
    assert "Refinancing exposure" in observations["signals"].text
    assert "An opening" not in observations["signals"].text
    assert observations["signals"].recommendation.startswith("RCL:")


def test_optional_model_accepts_single_verified_measure(monkeypatch):
    from competitive.executive import Observation, executive_analysis

    observations = {"net_income": Observation("CHH reported a net loss.", (
        metric("CHH", "Net income attributable to parent", -1_000_000),
    ))}
    reply = {"conclusion": "net_income", "findings": ["net_income"], "recommendations": ["net_income"]}
    monkeypatch.setattr("competitive.executive.ollama_chat", lambda *args, **kwargs: reply)
    assert executive_analysis("What was the net loss?", observations, "qwen3:4b") == reply
