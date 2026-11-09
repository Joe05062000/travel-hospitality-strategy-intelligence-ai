from time import monotonic
from types import SimpleNamespace
from dataclasses import replace
import json

import pytest

from competitive.financial import calendar_slot, change, comparable_peers, fiscal_periods, margin, normalize
from competitive.intelligence import Evidence, brief, load
from competitive.sources import (
    DIRECTORY_URL, company_directory, search_companies, search_sec, sec_search_url,
    sec_urls, select_issuers,
)


class Responses:
    def __init__(self, payloads):
        self.payloads = payloads

    def get(self, url, refresh=False, *, offline=False):
        return json.dumps(self.payloads[url]), "2026-11-08T12:00:00Z", None


def test_directory_search_and_share_class_deduplication():
    directory = {
        str(i): {"ticker": ticker, "title": name, "cik_str": cik}
        for i, (ticker, name, cik) in enumerate((
            ("GOOG", "Alphabet Inc.", 1652044), ("GOOGL", "Alphabet Inc.", 1652044),
            ("MSFT", "Microsoft Corp", 789019), ("DAL", "Delta Air Lines", 27904),
            ("UAL", "United Airlines Holdings", 100517), ("AMZN", "Amazon.com Inc.", 1018724),
        ))
    }
    companies, _, _ = company_directory(Responses({DIRECTORY_URL: directory}))
    assert search_companies(companies, "delta lines") == ["DAL"]
    assert search_companies(companies, "goog") == ["GOOG", "GOOGL"]
    assert select_issuers(companies, "GOOG", ["GOOGL", "MSFT", "DAL", "UAL", "AMZN"]) == [
        "GOOG", "MSFT", "DAL", "UAL",
    ]
    assert sec_urls("UAL", companies["UAL"][1])[0].endswith("CIK0000100517.json")


def test_sec_search_fallback_preserves_verified_identifier():
    response = {"hits": {"hits": [
        {"_id": "320193", "_source": {"entity": "Apple Inc. (AAPL)", "tickers": "AAPL"}},
        {"_id": "not-a-cik", "_source": {"entity": "Other (BAD)", "tickers": "BAD"}},
        {"_id": "120", "_source": {"entity": "Nonlisted company", "tickers": ""}},
    ]}}
    matches, _ = search_sec(Responses({sec_search_url("Apple"): response}), "Apple")
    assert matches == {"AAPL": ("Apple Inc.", "0000320193")}
    assert "%26" in sec_search_url("A&B")


def fact(start, end, value, form="10-Q", filed="2026-11-02"):
    return {"start": start, "end": end, "val": value, "form": form,
            "filed": filed, "accn": "0000000123-26-000001"}


def test_fiscal_year_end_and_contiguous_sequential_calculations():
    periods = fiscal_periods({
        "form": ["10-K", "10-Q", "10-K", "10-Q", "8-K"],
        "reportDate": ["2025-06-28", "2026-03-28", "2026-06-27", "2026-09-26", "2026-11-01"],
    })
    assert calendar_slot("2027-01-31") == (2026, 4)
    assert periods[2026, 2] == ("2026-06-27", "10-K")
    tags = {
        "Revenues": [
            fact("2025-06-29", "2026-06-27", 500, "10-K"),
            fact("2025-06-29", "2026-03-28", 350, filed="2026-04-30"),
            fact("2026-06-28", "2026-09-26", 165),
        ],
        "NetCashProvidedByUsedInOperatingActivities": [
            fact("2026-06-28", "2026-09-26", 30),
            fact("2026-01-01", "2026-09-26", 900),
        ],
    }
    payload = {"facts": {"us-gaap": {tag: {"units": {"USD": rows}} for tag, rows in tags.items()}}}
    close = normalize(payload, "TEST", 2026, 2, "now", "api", cik="0000000123", periods=periods)
    next_period = normalize(payload, "TEST", 2026, 3, "now", "api", cik="0000000123", periods=periods)
    assert close["Revenue"].value == 150
    assert close["Revenue prior YTD input"].value == 350
    assert next_period["Operating cash flow"].value == 30
    assert next_period["Revenue"].start == "2026-06-28"
    assert change(next_period["Revenue"], close["Revenue"], sequential=True) == pytest.approx(10)
    assert change(next_period["Revenue"], close["Revenue"]) is None
    assert normalize(payload, "TEST", 2026, 4, "now", "api", cik="0000000123", periods=periods) == {}


def test_latest_period_and_dynamic_source_lineage():
    cik = "0000000123"
    facts_url, subs_url = sec_urls("TEST", cik)
    recent = {
        "form": ["10-Q", "10-Q"], "reportDate": ["2026-09-30", "2026-06-30"],
        "accessionNumber": ["0000000123-26-000001", "0000000123-26-000002"],
        "primaryDocument": ["test-20260930.htm", "test-20260630.htm"],
        "filingDate": ["2026-11-02", "2026-08-01"],
    }
    submissions = {"cik": cik, "sic": "7372", "filings": {"recent": recent}}
    payload = {"cik": 123, "facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact("2026-07-01", "2026-09-30", 100)]}},
        "OperatingIncomeLoss": {"units": {"USD": [fact("2026-07-01", "2026-09-30", 20)]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "units": {"EUR": [fact("2026-07-01", "2026-09-30", 40)]}},
    }}}

    class CachedResponses(Responses):
        def get(self, url, refresh=False, *, offline=False):
            if url not in self.payloads:
                from competitive.sources import SourceError
                raise SourceError("Uncached filing")
            return super().get(url, refresh, offline=offline)

    evidence = load(CachedResponses({facts_url: payload, subs_url: submissions}), ["TEST"], None, None,
                    companies={"TEST": ("Example Issuer", cik)})
    assert evidence.period == (2026, 3)
    assert evidence.latest["TEST"][1] == "2026-09-30"
    assert evidence.industries["TEST"] == "Technology"
    metrics = evidence.financial["TEST", 2026]
    assert margin(metrics["Operating income"], metrics["Revenue"]) == 20
    assert "Capital expenditures" not in metrics
    assert metrics["Revenue"].url.endswith("/123/000000012326000001/test-20260930.htm")
    assert metrics["Revenue"].input_url == facts_url


def test_cross_industry_and_mismatched_metric_scope_are_not_ranked():
    payload = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [fact("2026-07-01", "2026-09-30", 100)]}},
        "OperatingIncomeLoss": {"units": {"USD": [fact("2026-07-01", "2026-09-30", 15)]}},
    }}}
    mar = normalize(payload, "MAR", 2026, 3, "now", "api")
    dal = {name: replace(item, ticker="DAL") for name, item in mar.items()}
    revenue = mar["Revenue"]
    assert not comparable_peers(revenue, dal["Revenue"], "Hospitality", "Aviation")
    assert not comparable_peers(revenue, replace(revenue, end="2026-09-29"), "Hospitality", "Hospitality")
    assert not comparable_peers(revenue, replace(revenue, definition="SalesRevenueNet"), "Hospitality", "Hospitality")
    evidence = Evidence({("MAR", 2026): mar, ("DAL", 2026): dal}, {}, {}, [], [],
                        industries={"MAR": "Hospitality", "DAL": "Aviation"})
    lines, _ = brief(evidence, ["MAR", "DAL"], 2026, 3, "Who is more profitable?")
    assert any("Financial ranking unavailable" in line for line in lines)
    assert all("operating margin:" not in line for line in lines)


def test_local_assessment_rejects_unknown_source_references(monkeypatch):
    from io import BytesIO
    from competitive.intelligence import ollama

    def model_response(request, timeout):
        assert json.loads(request.data)["stream"] is False
        assert json.loads(request.data)["think"] is False
        content = json.dumps({"assessment": "Claim.", "source_ids": [9]})
        chunk = json.dumps({"message": {"content": content}, "done": True})
        return BytesIO(chunk.encode() + b"\n")

    monkeypatch.setattr("competitive.intelligence.urlopen", model_response)
    with pytest.raises(ValueError, match="valid evidence citations"):
        ollama("Compare profitability", ["Operating margin 7.2% [1]."], "local-model")


@pytest.mark.parametrize("directory_available", [True, False])
def test_homepage_selection_fetches_uncached_airline_and_preserves_zero(monkeypatch, tmp_path, directory_available):
    from io import BytesIO
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    cik = "0000027904"
    facts_url, submissions_url = sec_urls("DAL", cik)
    calls = []
    fail_facts = not directory_available
    zero = dict(fact("2025-07-01", "2025-09-30", 0), accn="0000027904-25-000001")
    payloads = {
        DIRECTORY_URL: {"0": {"ticker": "DAL", "title": "Delta Air Lines", "cik_str": 27904}},
        facts_url: {"cik": 27904, "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [zero]}},
            "FuelCosts": {"units": {"USD": [zero]}},
        }}},
        submissions_url: {"cik": cik, "sic": "4512", "filings": {"recent": {
            "reportDate": ["2025-09-30", "2025-06-30"],
            "form": ["10-Q", "10-Q"],
            "primaryDocument": ["dal-20250930.htm", "dal-20250630.htm"],
            "accessionNumber": ["0000027904-25-000001", "0000027904-25-000002"],
            "filingDate": ["2025-11-01", "2025-08-01"],
        }}},
    }

    if not directory_available:
        del payloads[DIRECTORY_URL]
        payloads[sec_search_url("Delta")] = {"hits": {"hits": [
            {"_id": "27904", "_source": {"entity": "Delta Air Lines (DAL)", "tickers": "DAL"}}
        ]}}

    def fetch(request, timeout):
        calls.append(request.full_url)
        if request.full_url not in payloads or (fail_facts and request.full_url == facts_url):
            raise OSError("No fixture for this URL")
        return BytesIO(json.dumps(payloads[request.full_url]).encode())

    monkeypatch.setenv("SEC_USER_AGENT", "Test analyst test@example.org")
    monkeypatch.setenv("COMPETITIVE_CACHE", str(tmp_path / "test.sqlite3"))
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setattr("competitive.sources.urlopen", fetch)
    monkeypatch.setattr("competitive.sources.time", SimpleNamespace(monotonic=monotonic, sleep=lambda _: None))
    entrypoint = Path(__file__).resolve().parents[1] / "competitive_app.py"
    app = AppTest.from_file(str(entrypoint), default_timeout=30).run()
    assert app.selectbox(key="primary_issuer").value == "MAR"
    assert app.multiselect(key="peer_issuers").value == ["HLT", "H"]
    next(widget for widget in app.selectbox if widget.label == "Sector").set_value("All SEC issuers").run()
    if not directory_available:
        app.text_input(key="issuer_query").set_value("Delta").run()
        next(button for button in app.button if button.label == "Search SEC by name or ticker").click().run()
        assert sec_search_url("Delta") in calls
    app.selectbox(key="primary_issuer").set_value("DAL")
    app.multiselect(key="peer_issuers").set_value([])
    app.run()
    assert not app.exception
    assert facts_url in calls and submissions_url in calls
    expected_fetches = 1
    if fail_facts:
        assert not app.metric
        assert all(widget.label != "Primary company" for widget in app.selectbox)
        coverage = next(table.value for table in app.dataframe if "Status" in table.value)
        assert "Refresh failed" in coverage["Status"].tolist()
        fail_facts = False
        next(button for button in app.button if button.label == "Refresh financial and disclosure sources").click().run()
        assert not app.exception
        assert next(widget for widget in app.selectbox if widget.label == "Primary company").value == "DAL"
        expected_fetches = 2
    app.sidebar.radio[0].set_value("Financial Performance").run()
    assert not app.exception
    assert [metric.value for metric in app.metric] == [
        "$0", "Data unavailable", "Data unavailable", "Data unavailable", "$0",
    ]
    app.sidebar.radio[0].set_value("AI Executive Briefing").run()
    assert not app.exception
    assert any(heading.value == "Ask the AI" for heading in app.subheader)
    assert any("AI reasoning is disabled" in message.value for message in app.info)

    app.text_area[0].set_value("How did revenue change?")
    next(button for button in app.button if button.label == "Analyze question").click().run()
    assert not app.exception
    assert any("dal-20250930.htm" in caption.value for caption in app.caption)
    app.sidebar.radio[0].set_value("Competitive Benchmarking").run()
    next(widget for widget in app.selectbox if widget.label == "Operating measure").set_value("Fuel expense").run()
    assert not app.exception
    assert next(table.value for table in app.dataframe if "Value" in table.value).iloc[0]["Value"] == "$0"
    assert calls.count(facts_url) == expected_fetches


def test_financial_performance_formats_large_revenue_in_billions(monkeypatch, tmp_path):
    from io import BytesIO
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    cik = "0000027904"
    facts_url, submissions_url = sec_urls("DAL", cik)
    big = fact("2026-07-01", "2026-09-30", 20_186_000_000, filed="2026-10-09")
    payloads = {
        DIRECTORY_URL: {"0": {"ticker": "DAL", "title": "Delta Air Lines", "cik_str": 27904}},
        facts_url: {"cik": 27904, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [big]}}}}},
        submissions_url: {"cik": cik, "sic": "4512", "filings": {"recent": {
            "reportDate": ["2026-09-30", "2026-06-30"], "form": ["10-Q", "10-Q"],
            "primaryDocument": ["dal-20260930.htm", "dal-20260630.htm"],
            "accessionNumber": ["0000027904-26-000001", "0000027904-26-000002"],
            "filingDate": ["2026-10-09", "2026-07-09"],
        }}},
    }

    def fetch(request, timeout):
        if request.full_url not in payloads:
            raise OSError("No fixture for this URL")
        return BytesIO(json.dumps(payloads[request.full_url]).encode())

    monkeypatch.setenv("SEC_USER_AGENT", "Test analyst test@example.org")
    monkeypatch.setenv("COMPETITIVE_CACHE", str(tmp_path / "test.sqlite3"))
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setattr("competitive.sources.urlopen", fetch)
    monkeypatch.setattr("competitive.sources.time", SimpleNamespace(monotonic=monotonic, sleep=lambda _: None))
    entrypoint = Path(__file__).resolve().parents[1] / "competitive_app.py"
    app = AppTest.from_file(str(entrypoint), default_timeout=30).run()
    next(widget for widget in app.selectbox if widget.label == "Sector").set_value("All SEC issuers").run()
    app.selectbox(key="primary_issuer").set_value("DAL")
    app.multiselect(key="peer_issuers").set_value([])
    app.run()
    app.sidebar.radio[0].set_value("Financial Performance").run()
    assert not app.exception
    assert app.metric[0].value == "$20.19B"


@pytest.mark.parametrize(("content", "thinking", "reason", "expected"), [
    ("", "Reasoning consumed the generation budget", "length", "reasoning but no visible answer"),
    (" ", "", "stop", "empty answer"),
    ("{", "", "length", "output token limit"),
    ("not JSON", "", "stop", "not valid structured JSON"),
    ('{"assessment":"Operating margin was 9.9%.","source_ids":[1]}',
     "", "stop", "figures absent from its cited evidence"),
    ('{"assessment":"Operating margin was 7.2% [8].","source_ids":[1]}',
     "", "stop", "unverified inline citation"),
    ('{"assessment":"Operating margin was 7.2%.","source_ids":[1]}',
     "", "stop", None),
])
def test_local_assessment_validates_visible_content(monkeypatch, content, thinking, reason, expected):
    from io import BytesIO
    from competitive.intelligence import ollama

    def respond(request, timeout):
        assert request.full_url.endswith("/api/chat")
        return BytesIO(json.dumps({
            "message": {"content": content, "thinking": thinking},
            "done": True, "done_reason": reason,
        }).encode())

    monkeypatch.setattr("competitive.intelligence.urlopen", respond)
    if expected:
        with pytest.raises(ValueError, match=expected):
            ollama("Compare profitability", ["Operating margin 7.2% [1]."], "qwen3:4b")
    else:
        assert ollama("Compare profitability", ["Operating margin 7.2% [1]."], "qwen3:4b") == (
            "Operating margin was 7.2%.\n\nEvidence [1]"
        )


def test_executive_evidence_calculates_growth_and_margins_with_filing_periods():
    from competitive.executive import executive_evidence

    def financial(year, revenue, operating, net):
        tags = {"Revenues": revenue, "OperatingIncomeLoss": operating, "NetIncomeLoss": net}
        payload = {"facts": {"us-gaap": {key: {"units": {"USD": [
            fact(f"{year}-07-01", f"{year}-09-30", value)
        ]}} for key, value in tags.items() if value is not None}}}
        return normalize(payload, "MAR", year, 3, "now", "api")

    evidence = Evidence({("MAR", 2025): financial(2025, 100, 15, 10),
                         ("MAR", 2026): financial(2026, 120, None, 0)}, {}, {}, [], [])
    observations, limitations = executive_evidence(evidence, ["MAR"], 2026, "Compare growth and profitability")
    assert "+20.0%" in observations["growth"].text
    assert "Net margin 0.0%" in observations["net"].text
    assert "operating" not in observations
    assert "Operating margin unavailable" in " ".join(limitations)
    assert {fact.end for fact in observations["growth"].sources} == {"2025-09-30", "2026-09-30"}
    assert all(fact.url.startswith("https://www.sec.gov/") for fact in observations["growth"].sources)
    assert "NetIncomeLoss" not in observations["net"].text


def test_classify_intents_matches_multiple_and_defaults_broadly():
    from competitive.executive import classify_intents

    assert classify_intents("Compare profitability and margins") == {"profitability"}
    assert classify_intents("How is revenue growth trending?") == {"growth"}
    assert classify_intents("Is the balance sheet resilient? Check debt and liquidity.") == {"resilience"}
    assert classify_intents("What about fuel cost exposure and capacity?") == {"operating"}
    assert classify_intents("Which company is strongest overall?") == {"growth", "profitability"}
    assert classify_intents("Compare growth and profitability") == {"growth", "profitability"}


def test_executive_evidence_profitability_question_excludes_growth():
    from competitive.executive import executive_evidence

    def financial(year, revenue, operating, net):
        tags = {"Revenues": revenue, "OperatingIncomeLoss": operating, "NetIncomeLoss": net}
        payload = {"facts": {"us-gaap": {key: {"units": {"USD": [
            fact(f"{year}-07-01", f"{year}-09-30", value)
        ]}} for key, value in tags.items()}}}
        return normalize(payload, "MAR", year, 3, "now", "api")

    evidence = Evidence({("MAR", 2025): financial(2025, 100, 15, 10),
                         ("MAR", 2026): financial(2026, 120, 20, 12)}, {}, {}, [], [])
    observations, _ = executive_evidence(evidence, ["MAR"], 2026, "How is profitability?")
    assert "growth" not in observations
    assert "operating" in observations and "net" in observations


def test_executive_evidence_recommendation_names_laggard_and_metric():
    from competitive.executive import executive_evidence

    def financial(ticker, revenue, operating, net):
        tags = {"Revenues": revenue, "OperatingIncomeLoss": operating, "NetIncomeLoss": net}
        payload = {"facts": {"us-gaap": {key: {"units": {"USD": [
            fact("2026-07-01", "2026-09-30", value)
        ]}} for key, value in tags.items()}}}
        return normalize(payload, ticker, 2026, 3, "now", "api", cik="0000000001")

    evidence = Evidence({("CHH", 2026): financial("CHH", 458, 106, 58),
                         ("WH", 2026): financial("WH", 410, 172, 99)}, {}, {}, [], [],
                        industries={"CHH": "Hospitality", "WH": "Hospitality"})
    observations, _ = executive_evidence(evidence, ["CHH", "WH"], 2026, "Compare profitability.")
    recommendation = observations["operating"].recommendation
    assert recommendation.startswith("CHH:")
    assert "WH" in recommendation
    assert "Monitor: operating margin" in recommendation


def test_executive_evidence_explains_revenue_definition_difference():
    from competitive.executive import executive_evidence

    def financial(ticker, tag, cur, prior):
        payload = {"facts": {"us-gaap": {tag: {"units": {"USD": [
            fact("2025-07-01", "2025-09-30", prior),
            fact("2026-07-01", "2026-09-30", cur),
        ]}}}}}
        return {2025: normalize(payload, ticker, 2025, 3, "now", "api", cik="0000000001"),
                2026: normalize(payload, ticker, 2026, 3, "now", "api", cik="0000000001")}

    chh = financial("CHH", "Revenues", 458, 447)
    wh = financial("WH", "RevenueFromContractWithCustomerExcludingAssessedTax", 410, 382)
    evidence = Evidence({("CHH", 2025): chh[2025], ("CHH", 2026): chh[2026],
                         ("WH", 2025): wh[2025], ("WH", 2026): wh[2026]}, {}, {}, [], [],
                        industries={"CHH": "Hospitality", "WH": "Hospitality"})
    observations, limitations = executive_evidence(evidence, ["CHH", "WH"], 2026, "Compare revenue growth.")
    assert any("not like-for-like" in note and "CHH" in note and "WH" in note for note in limitations)


@pytest.mark.parametrize("reply", [
    {"assessment": "Concise assessment based on evidence", "source_ids": [1, 2, 3]},
    {"conclusion": "growth", "findings": ["growth", "growth", "net"], "recommendations": ["growth", "net"]},
    {"conclusion": "price cuts caused growth", "findings": ["growth", "operating", "net"],
     "recommendations": ["growth", "net"]},
    {"conclusion": "growth", "findings": ["growth", "operating", "net"], "recommendations": ["growth", "99"]},
])
def test_executive_analysis_rejects_generic_or_unverified_model_output(monkeypatch, reply):
    from competitive.executive import Observation, executive_analysis

    monkeypatch.setattr("competitive.executive.ollama_chat", lambda *args, **kwargs: reply)
    observations = {key: Observation(key, ()) for key in ("growth", "operating", "net")}
    with pytest.raises(ValueError, match="invalid executive analysis|generic or unsupported"):
        executive_analysis("Compare growth and profitability", observations, "qwen3:4b")
