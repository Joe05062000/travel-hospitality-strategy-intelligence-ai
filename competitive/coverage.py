"""Availability checks for the selected company, metric and reporting window."""
from .financial import comparable_peers
from .sources import sec_urls


def coverage_reason(evidence, ticker, cik, *, disclosure=False, metric=None):
    if disclosure:
        status, detail = evidence.disclosure_status.get(
            ticker, ("Source not loaded", "No validated disclosure has been loaded for this window."))
        if status != "Loaded":
            return status, detail
        return "Metric not disclosed", "The loaded disclosure contains no verified observation for this measure."
    for url in sec_urls(ticker, cik):
        status, detail = evidence.source_status.get((ticker, url), ("Source not loaded", url))
        if status != "Loaded":
            return status, detail
    if evidence.period not in evidence.filing_windows.get(ticker, {}):
        return "No matching period", "No supported 10-Q or 10-K reporting window matches the selection."
    if metric in evidence.disclosed_metrics.get(ticker, ()):
        return "No matching period", "Source loaded. The concept exists, but no standalone fact or valid inputs match this window."
    return ("Metric not disclosed", "Sources loaded. No matching quarterly fact or valid calculation inputs "
            "were found for this measure. Annual and year-to-date facts are kept separate.")


def latest_shared_financial_period(cache, candidates, companies, evidence, measure):
    # Search the cached filing windows only. A comparison needs at least two peers.
    from .intelligence import load

    filed = [set(evidence.filing_windows.get(ticker, {})) for ticker in candidates
             if evidence.filing_windows.get(ticker)]
    if len(filed) < 2:
        return None
    for year, quarter in sorted(set.intersection(*filed), reverse=True)[:8]:
        if (year, quarter) == evidence.period:
            continue
        cached = load(cache, candidates, [year - 1, year], quarter, companies=companies)
        available = [facts[measure] for ticker in candidates
                     if measure in (facts := cached.financial.get((ticker, year), {}))]
        if len(available) == len(filed) and all(
            comparable_peers(available[0], item, cached.industries[available[0].ticker],
                             cached.industries[item.ticker]) for item in available[1:]
        ):
            return (year, quarter), available
    return None
