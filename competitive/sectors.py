"""Sector navigation, comparable series and hypothetical sensitivities."""
from datetime import date

from .financial import change, comparable_peers

BRAND = "Travel & Hospitality Strategy Intelligence AI"
SECTORS = {
    "Hospitality": {"MAR": "Marriott International", "HLT": "Hilton", "H": "Hyatt",
                    "CHH": "Choice Hotels", "WH": "Wyndham Hotels & Resorts", "IHG": "IHG Hotels & Resorts"},
    "Aviation": {"DAL": "Delta Air Lines", "UAL": "United Airlines", "AAL": "American Airlines",
                 "LUV": "Southwest Airlines", "JBLU": "JetBlue"},
    "Travel & Tourism": {"RCL": "Royal Caribbean Group", "CCL": "Carnival Corporation",
                         "NCLH": "Norwegian Cruise Line Holdings", "EXPE": "Expedia Group",
                         "BKNG": "Booking Holdings"},
}
# SEC Submissions verified these ticker-to-CIK associations.
SECTOR_CIKS = {
    "CHH": "0001046311", "WH": "0001722684", "IHG": "0000858446",
    "DAL": "0000027904", "UAL": "0000100517", "AAL": "0000006201",
    "LUV": "0000092380", "JBLU": "0001158463",
    "RCL": "0000884887", "CCL": "0000815097", "NCLH": "0001513761",
    "EXPE": "0001324424", "BKNG": "0001075531",
}
SECTOR_ISSUERS = {ticker: (name, SECTOR_CIKS[ticker]) for names in SECTORS.values()
                  for ticker, name in names.items() if ticker in SECTOR_CIKS}
TRAVEL_METRICS = ("Passenger cruise days", "Cruise occupancy", "Net yield",
                  "Onboard revenue", "Gross bookings", "Room nights", "Revenue", "Operating margin")
DRIVERS = {
    "Hospitality": ("Room growth (%)", "RevPAR change (%)", "Operating cost change (%)"),
    "Aviation": ("Capacity change (%)", "Revenue per capacity unit change (%)", "Fuel cost change (%)"),
    "Travel & Tourism": ("Booking or passenger volume change (%)", "Revenue per booking or passenger change (%)",
                         "Operating cost change (%)"),
}


def sector_for(ticker, classification):
    return next((sector for sector, names in SECTORS.items() if ticker in names), classification)


def benchmark_status(item, others, industries):
    if item is None:
        return "Not reported in validated sources for this window"
    peers = [peer for peer in others if peer and peer.ticker != item.ticker]
    if not peers:
        return "Reported or calculated for this issuer only"
    if all(comparable_peers(item, peer, industries.get(item.ticker), industries.get(peer.ticker))
           for peer in peers):
        return "Comparable on available definitions, units and dates"
    reasons = []
    if any((peer.start, peer.end) != (item.start, item.end) for peer in peers):
        reasons.append("reporting dates")
    if any(peer.unit != item.unit for peer in peers):
        reasons.append("units or currency")
    if any(peer.definition != item.definition or peer.name != item.name for peer in peers):
        reasons.append("scope or definition")
    if any(not industries.get(item.ticker) or industries.get(peer.ticker) != industries.get(item.ticker)
           for peer in peers):
        reasons.append("sector classification")
    return "Not comparable across all peers · " + ", ".join(reasons)



def financial_yoy(current, prior):
    if not current or not prior or current.start is not None or prior.start is not None:
        return change(current, prior)
    if ((current.name, current.unit, current.definition, current.ticker) !=
            (prior.name, prior.unit, prior.definition, prior.ticker) or prior.value <= 0):
        return None
    days = (date.fromisoformat(current.end) - date.fromisoformat(prior.end)).days
    return 100 * (current.value / prior.value - 1) if 358 <= days <= 372 else None

def trend_cohorts(points, industries, indexed=False):
    """Group series only when every shared period has matching input concepts."""
    cohorts = []
    for ticker, periods in points.items():
        available = {year: point for year, point in periods.items() if point[0] is not None}
        if not available:
            continue
        for cohort in cohorts:
            compatible = True
            for peer, peer_periods in cohort.items():
                overlap = available.keys() & peer_periods.keys()
                if not overlap or not all(
                    len(available[year][1]) == len(peer_periods[year][1])
                    and all(comparable_peers(left, right, industries.get(ticker), industries.get(peer))
                            for left, right in zip(available[year][1], peer_periods[year][1]))
                    for year in overlap
                ):
                    compatible = False
                    break
            if compatible:
                cohort[ticker] = available
                break
        else:
            cohorts.append({ticker: available})
    for cohort in cohorts:
        common = set.intersection(*(set(series) for series in cohort.values()))
        if indexed:
            common = {year for year in common if all(series[year][0] > 0 for series in cohort.values())}
            if not common:
                continue
        base = min(common) if indexed else None
        yield base, [
            {"Ticker": ticker, "Year": year, "Period end": facts[0].end,
             "Value": 100 * value / series[base][0] if indexed else value}
            for ticker, series in cohort.items() for year, (value, facts) in sorted(series.items())
            if base is None or year >= base
        ]


def sector_scenario(revenue, operating_income, volume, yield_change, cost_change, *, fuel=None):
    if revenue is None or operating_income is None or revenue <= 0:
        return None
    adjusted_revenue = revenue * (1 + volume / 100) * (1 + yield_change / 100)
    operating_cost = revenue - operating_income
    exposed_cost = operating_cost if fuel is None else fuel
    if not 0 <= exposed_cost <= operating_cost:
        return None
    adjusted_income = adjusted_revenue - operating_cost - exposed_cost * cost_change / 100
    return adjusted_revenue, adjusted_income, 100 * adjusted_income / adjusted_revenue if adjusted_revenue else None
