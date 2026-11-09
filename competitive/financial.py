"""Normalize USD US GAAP observations using issuer reporting dates."""
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, timedelta

from .sources import filing_url

TAGS = {
    "Revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax"),
    "Net income attributable to parent": ("NetIncomeLoss",),
    "Operating income": ("OperatingIncomeLoss",),
    "Cash and cash equivalents": ("CashAndCashEquivalentsAtCarryingValue", "Cash"),
    "Current debt": ("LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtCurrent"),
    "Total debt": ("DebtAndCapitalLeaseObligations",),
    "Noncurrent debt": ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtNoncurrent"),
    "Operating cash flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "Capital expenditures": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "SegmentExpenditureAdditionToLongLivedAssets"),

}
CUMULATIVE = {"Operating cash flow", "Capital expenditures"}
POINTS = {"Cash and cash equivalents", "Current debt", "Noncurrent debt", "Total debt"}


@dataclass(frozen=True)
class Metric:
    ticker: str
    name: str
    start: str | None
    end: str
    value: float
    unit: str
    url: str
    published: str
    retrieved: str
    definition: str
    input_url: str = ""


def bounds(year, quarter):
    month = quarter * 3
    return (date(year, month - 2, 1).isoformat(),
            date(year, month, monthrange(year, month)[1]).isoformat())


def calendar_slot(end):
    """Group filing dates by nearest calendar close without changing actual dates."""
    day = date.fromisoformat(end)
    candidates = sorted(
        (abs((day - date.fromisoformat(bounds(year, quarter)[1])).days), year, quarter)
        for year in range(day.year - 1, day.year + 2)
        for quarter in range(1, 5)
    )
    distance, year, quarter = candidates[0]
    return (year, quarter) if distance < candidates[1][0] else None


def prior_slot(year, quarter):
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def fiscal_periods(recent):
    periods = {}
    for form, end in zip(recent.get("form", []), recent.get("reportDate", [])):
        if form in {"10-Q", "10-K"} and end and (slot := calendar_slot(end)):
            if slot not in periods:
                periods[slot] = (end, form)
            elif periods[slot] != (end, form):
                periods[slot] = (None, None)
    return periods


def normalize(payload, ticker, year, quarter, retrieved, input_url, filings=None,
              *, cik=None, periods=None, extra_tags=None):
    start, end = bounds(year, quarter)
    facts = payload.get("facts", {}).get("us-gaap", {})
    ytd_start = f"{year}-01-01"
    previous_end = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    annual_close = False
    if periods is not None:
        end, form = periods.get((year, quarter), (None, None))
        if not end:
            return {}
        previous_end = periods.get(prior_slot(year, quarter), (None, None))[0]
        if previous_end:
            duration = (date.fromisoformat(end) - date.fromisoformat(previous_end)).days
            start = (date.fromisoformat(previous_end) + timedelta(days=1)).isoformat() if 77 <= duration <= 105 else None
        else:
            start = None
        annual_close = form == "10-K"
        ytd_start = None
        # The fiscal year starts immediately after the previous original 10-K close.
        prior_closes = [day for day, kind in periods.values() if day and day < end and kind == "10-K"]
        if prior_closes:
            fiscal_close = date.fromisoformat(max(prior_closes))
            if 77 <= (date.fromisoformat(end) - fiscal_close).days <= 371:
                ytd_start = (fiscal_close + timedelta(days=1)).isoformat()

    def make(name, begin, finish, row, tag, value=None):
        return observation(ticker, name, begin, finish, row, retrieved, input_url,
                           filings, tag, value, cik=cik)

    results = {}
    for name, tags in (TAGS | (extra_tags or {})).items():
        if name not in POINTS and start is None:
            continue
        expected_start = None if name in POINTS else start
        for tag in tags:
            rows = facts.get(tag, {}).get("units", {}).get("USD", [])
            direct = select(rows, expected_start, end)
            if direct:
                results[name] = make(name, expected_start, end, direct, tag)
                break
            if not (name not in POINTS and (name in CUMULATIVE or annual_close)
                    and ytd_start and previous_end and start and ytd_start < start):
                continue
            current = select(rows, ytd_start, end)
            previous = select(rows, ytd_start, previous_end,
                              cutoff=current["filed"] if current else "")
            if current and previous:
                results[name] = make(name, start, end, current, tag + " | YTD difference",
                                     current["val"] - previous["val"])
                results[name + " prior YTD input"] = make(
                    name + " prior YTD input", ytd_start, previous_end, previous, tag)
                results[name + " current YTD input"] = make(
                    name + " current YTD input", ytd_start, end, current, tag)
                break
    pairs = [
        ("LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtAndCapitalLeaseObligations"),
        ("LongTermDebtCurrent", "LongTermDebtNoncurrent"),
    ]
    short, long = results.get("Current debt"), results.get("Noncurrent debt")
    if short and long and (short.definition, long.definition) in pairs and short.url == long.url:
        name = "Long-term debt including current maturities"
        results[name] = replace(long, name=name, value=short.value + long.value,
                                definition=f"{short.definition} + {long.definition}")
    return results


def select(rows, start, end, cutoff=None):
    eligible = [row for row in rows if row.get("start") == start and row.get("end") == end
                and row.get("form") in {"10-Q", "10-K", "10-Q/A", "10-K/A"}
                and row.get("accn") and row.get("filed")
                and (cutoff is None or row["filed"] <= cutoff)
                and isinstance(row.get("val"), (int, float))]
    if not eligible:
        return None
    # Use the latest filing, but refuse conflicting facts within that filing.
    latest = max(eligible, key=lambda row: (row["filed"], row["accn"]))
    values = {row["val"] for row in eligible if row["accn"] == latest["accn"]}
    return latest if len(values) == 1 else None


def observation(ticker, name, start, end, row, retrieved, input_url, filings, tag, value=None, *, cik=None):
    return Metric(ticker, name, start, end, row["val"] if value is None else value,
                  "USD", filing_url(ticker, row["accn"], (filings or {}).get(row["accn"], ""), cik=cik),
                  row["filed"], retrieved, tag, input_url)


def aligned(current, prior, *, sequential=False):
    if (not current or not prior or current.unit != prior.unit or not current.start or not prior.start):
        return False
    starts = [date.fromisoformat(item.start) for item in (current, prior)]
    ends = [date.fromisoformat(item.end) for item in (current, prior)]
    if abs((ends[0] - starts[0]).days - (ends[1] - starts[1]).days) > 7:
        return False
    if sequential:
        return (starts[0] - ends[1]).days == 1 and 77 <= (ends[0] - ends[1]).days <= 105
    return (358 <= (ends[0] - ends[1]).days <= 372
            and 358 <= (starts[0] - starts[1]).days <= 372)


def change(current, prior, *, sequential=False):
    if (not aligned(current, prior, sequential=sequential)
            or current.name != prior.name or current.ticker != prior.ticker
            or current.definition.split(" |")[0] != prior.definition.split(" |")[0]
            or prior.value <= 0):
        return None
    return 100 * (current.value / prior.value - 1)


def comparable_peers(left, right, left_industry, right_industry):
    return (left_industry is not None and left_industry == right_industry
            and left is not None and right is not None
            and (left.name, left.unit, left.start, left.end, left.definition.split(" |")[0]) ==
                (right.name, right.unit, right.start, right.end, right.definition.split(" |")[0]))


def margin(income, revenue):
    if (not income or not revenue or revenue.value <= 0 or
            (income.ticker, income.start, income.end, income.unit) !=
            (revenue.ticker, revenue.start, revenue.end, revenue.unit)):
        return None
    return 100 * income.value / revenue.value
