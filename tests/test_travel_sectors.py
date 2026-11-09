from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfReader

from competitive.financial import Metric
from competitive.industry import industry, release_metrics
from competitive.report import report_pdf
from competitive.sectors import benchmark_status, sector_for, sector_scenario, trend_cohorts


def metric(ticker="RCL", end="2026-09-30", definition="US GAAP Revenue"):
    return Metric(ticker, "Revenue", "2026-07-01", end, 10, "USD",
                  "https://www.sec.gov/example", "2026-11-01", "2026-11-09", definition)


def test_sector_classification_and_benchmark_date_alignment():
    assert industry("4481") == "Travel & Tourism"
    assert sector_for("BKNG", None) == "Travel & Tourism"
    first = metric()
    peer = metric("NCLH")
    carnival = metric("CCL", "2026-08-31")
    industries = dict.fromkeys(("RCL", "NCLH", "CCL"), "Travel & Tourism")
    assert benchmark_status(first, [peer], industries).startswith("Comparable")
    assert benchmark_status(first, [peer, carnival], industries).startswith("Not comparable")
    assert benchmark_status(None, [peer], industries).startswith("Not reported")


def test_combined_series_uses_common_base_and_excludes_incompatible_periods():
    first = metric()
    peer = metric("NCLH")
    carnival = metric("CCL", "2026-08-31")
    points = {
        "RCL": {2025: (5, (replace(first, start="2025-07-01", end="2025-09-30"),)), 2026: (10, (first,))},
        "NCLH": {2026: (20, (peer,))},
        "CCL": {2026: (30, (carnival,))},
    }
    groups = list(trend_cohorts(points, dict.fromkeys(points, "Travel & Tourism"), indexed=True))
    assert groups[0][0] == 2026
    assert [(row["Ticker"], row["Value"]) for row in groups[0][1]] == [("RCL", 100), ("NCLH", 100)]
    assert groups[1][1][0]["Ticker"] == "CCL"


def test_cruise_disclosure_preserves_actual_fiscal_period_and_missing_value():
    metrics = release_metrics("CCL", "August 31, 2026\nOccupancy percentage (c)  111.8 %", "today")
    assert metrics["Cruise occupancy"].value == 111.8
    assert metrics["Cruise occupancy"].end == "2026-08-31"
    assert "Passenger cruise days" not in metrics


def test_sector_sensitivity_and_pdf_sources():
    revenue, income, margin = sector_scenario(100, 20, 10, 10, 10, fuel=20)
    assert revenue == pytest.approx(121)
    assert income == pytest.approx(39)
    assert margin == pytest.approx(100 * 39 / 121)
    assert sector_scenario(100, None, 0, 0, 0) is None
    url = "https://www.sec.gov/Archives/example"
    pdf = report_pdf("Cruise briefing", ["Reported analysis"], [url],
                     sections=[("Executive summary", ["Quarterly comparison [1]"])])
    reader = PdfReader(BytesIO(pdf))
    assert "Quarterly comparison [1]" in reader.pages[0].extract_text()
    assert any(link.get_object()["/A"]["/URI"] == url for link in reader.pages[0]["/Annots"])
