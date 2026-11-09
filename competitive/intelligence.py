"""Issuer-specific evidence extraction and deterministic strategy workflows."""
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .financial import TAGS, Metric, bounds, calendar_slot, change, comparable_peers, fiscal_periods, margin, normalize, prior_slot
from .industry import DISCLOSURES, disclosure_body, disclosure_key, industry, release_metrics
from .sectors import sector_for
from .sources import RELEASES, RELEASE_ALTERNATES, SourceError, filing_url, sec_urls

NUM = r"([\d,]+(?:\.\d+)?)"
PATTERNS = {
    "CHH": {
        "Net rooms YoY": (rf"Global net rooms grew {NUM}%", "Global net rooms, Choice reported system; quarter-end YoY"),
        "US RevPAR YoY": (rf"U\.S\. RevPAR increased {NUM}% in the third quarter", "U.S. system, Choice reported cohort; quarter YoY"),
    },
    "WH": {
        "Net rooms YoY": (rf"System-wide rooms grew {NUM}% year-over-year, excluding", "Global system rooms, excludes Revo; quarter-end YoY"),
        "US RevPAR YoY": (rf"U\.S\. RevPAR grew {NUM}% year-over-year", "U.S. system, Wyndham reported cohort; quarter YoY"),
        "Comparable hotel RevPAR YoY": (rf"Global RevPAR grew {NUM}% compared to third quarter 2025 in constant currency", "Global, constant currency, Wyndham reported cohort; quarter YoY"),
    },

    "MAR": {
        "Comparable hotel RevPAR YoY": (rf"RevPAR\s*\d*\s*increased {NUM} percent worldwide", "Worldwide, constant dollar; issuer comparable hotel exclusions apply"),
        "Net rooms YoY": (rf"net rooms grew {NUM} percent from the end", "Worldwide lodging system"),
        "Pipeline rooms": (rf"more than {NUM} rooms, with", "Reported lower bound; includes pending conversions"),
        "US & Canada RevPAR YoY": (rf"{NUM} percent growth in the U\.S\. & Canada", "US and Canada combined"),
        "International RevPAR YoY": (rf"{NUM} percent growth in international markets", "Outside US and Canada"),
    },
    "HLT": {
        "Comparable hotel RevPAR YoY": (rf"System-wide comparable RevPAR increased {NUM} percent, on a currency neutral basis, for the third quarter", "Worldwide, currency neutral; Hilton comparable system"),
        "Net rooms YoY": (rf"net unit growth of {NUM} percent from September", "Worldwide net unit growth"),
        "Pipeline rooms": (rf"development pipeline to {NUM} rooms as of", "Approved development rooms"),
    },
    "H": {
        "Comparable hotel RevPAR YoY": (rf"Comparable system-wide hotels RevPAR increased {NUM}%", "Worldwide hotels, constant dollar; all-inclusive resorts tracked separately"),
        "Net rooms YoY": (rf"Net rooms growth for the trailing twelve months was {NUM}%", "Year-over-year room-count growth including Playa count changes"),
        "Pipeline rooms": (rf"Pipeline of executed management or franchise contracts was approximately {NUM} rooms", "Approximate executed management or franchise contracts"),
        "US RevPAR YoY": (rf"RevPAR in the United States grew {NUM}%", "US only"),
        "All-inclusive net package RevPAR YoY": (rf"all-inclusive resorts Net Package RevPAR decreased {NUM}%", "Worldwide comparable resorts, reported currency; includes package revenue"),
    },
}
SIGNALS = {
    "Development": r"\b(?:hotel conversions?|property conversions?|development pipelines?|development signings?|hotel openings?|new properties|new destinations)\b",
    "Transactions": r"\b(?:Playa Hotels Acquisition|acquired|acquisition of|partnerships?|asset sales?|dispositions?|sale of)\b",
    "Distribution and loyalty": r"\b(?:loyalty|technology|commercial platform|Bonvoy)\b",
    "Fleet and capacity": r"\b(?:fleet|newbuild|aircraft deliveries|ship deliveries|capacity investments?)\b",
    "Financial risks": r"\b(?:refinancing|liquidity|fuel prices|exchange rates|credit facility)\b",
    "Management priorities": r"\b(?:our strategy|management priorities|strategic plan|capital allocation)\b",
    "Demand and risk": r"\b(?:demand|conflict|renovations|closures)\b",
}


class Blocks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "li", "div", "tr", "h1", "h2"} and self.parts:
            self.blocks.append(" ".join("".join(self.parts).split()))
            self.parts.clear()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_release(ticker, html, retrieved, source_url=None):
    parser = Blocks()
    parser.feed(html)
    published, issuer_url = RELEASES[ticker]
    url = source_url or issuer_url
    start, end = bounds(2026, 3)
    observations = {}
    blocks = [block for block in parser.blocks if block]
    title = {"HLT": "Reports Third Quarter", "WH": "Reports Strong Third Quarter Results"}.get(
        ticker, "Reports Third Quarter 2026 Results")
    if not any(title.lower() in block.lower() for block in blocks):
        raise SourceError("Expected Q3 2026 earnings release title was absent.")
    for name, (pattern, definition) in PATTERNS[ticker].items():
        matches = {float(value.replace(",", "")) for value in
                   re.findall(pattern, " ".join(blocks), re.I)}
        if len(matches) == 1:
            value = matches.pop()
            if name == "All-inclusive net package RevPAR YoY":
                value = -value
            is_pipeline = name == "Pipeline rooms"
            period_start = None if is_pipeline or name == "Net rooms YoY" else start
            observations[name] = Metric(ticker, name, period_start, end,
                                        value, "rooms" if is_pipeline else "%", url,
                                        published, retrieved, definition, url)
    return observations, extract_signals(blocks, url, published, retrieved)


def extract_signals(blocks, url, published, retrieved):
    signals = []
    for category, pattern in SIGNALS.items():
        candidates = [block for block in blocks if 75 <= len(block) <= 750
                      and re.search(pattern, block, re.I)
                      and not re.search(r"forward-looking|conference call|copyright|represents the total number|"
                                        r"is a non-GAAP measure|loyalty program accounting|"
                                        r"conversion price|fully diluted weighted-average", block, re.I)
                      and not re.match(r"^\(\d+\)", block)]
        if candidates:
            signals.append((category, candidates[0], url, published, retrieved))
    return signals


@dataclass
class Evidence:
    financial: dict
    operating: dict
    signals: dict
    filings: list
    warnings: list
    industries: dict = field(default_factory=dict)
    latest: dict = field(default_factory=dict)
    preceding: dict = field(default_factory=dict)
    period: tuple | None = None
    source_status: dict = field(default_factory=dict)
    filing_windows: dict = field(default_factory=dict)
    disclosure_status: dict = field(default_factory=dict)
    disclosed_metrics: dict = field(default_factory=dict)


def load(cache, tickers, years, quarter, refresh=False, *, companies=None, lookback=3,
         retrieve_documents=False, fetch_tickers=()):
    evidence = Evidence({}, {}, {}, [], [])
    sources_by_ticker = {}
    for ticker in tickers:
        cik = companies[ticker][1] if companies is not None else None
        facts_url, submissions_url = sec_urls(ticker, cik)
        payloads = {}
        for url in (facts_url, submissions_url):
            try:
                body, retrieved, warning = cache.get(url, refresh, offline=not (refresh or retrieve_documents or ticker in fetch_tickers))
                payload = json.loads(body)
                if cik and str(payload.get("cik", "")).lstrip("0") != cik.lstrip("0"):
                    raise SourceError("SEC response CIK does not match the selected issuer.")
                payloads[url] = (payload, retrieved)
                evidence.source_status[ticker, url] = ("Loaded", warning or retrieved)
                if warning:
                    evidence.warnings.append(f"{ticker} · {url}: {warning}")
            except (SourceError, ValueError) as exc:
                evidence.source_status[ticker, url] = (getattr(exc, "status", "Refresh failed"), str(exc))
                evidence.warnings.append(f"{ticker} · {url}: {exc}")
        sources_by_ticker[ticker] = payloads
        submissions = payloads.get(submissions_url, ({}, None))[0]
        recent = submissions.get("filings", {}).get("recent", {})
        evidence.filing_windows[ticker] = fiscal_periods(recent)
        ends = [end for end, form in zip(recent.get("reportDate", []), recent.get("form", []))
                if end and form in {"10-Q", "10-K"}]
        if ends:
            latest_end = max(ends)
            evidence.latest[ticker] = (calendar_slot(latest_end), latest_end)
        evidence.industries[ticker] = sector_for(ticker, industry(submissions.get("sic"), submissions.get("sicDescription")))
    if years is None:
        latest = next((evidence.latest[t] for t in tickers if t in evidence.latest and evidence.latest[t][0]), None)
        if latest is None or latest[0] is None:
            return evidence
        year, quarter = latest[0]
        years = list(range(year - lookback, year + 1))
    evidence.period = (max(years), quarter) if years else None
    for ticker in tickers:
        cik = companies[ticker][1] if companies is not None else None
        facts_url, submissions_url = sec_urls(ticker, cik)
        payloads = sources_by_ticker[ticker]
        periods = None
        recent = {}
        filings = {}
        if submissions_url in payloads:
            submissions, retrieved = payloads[submissions_url]
            recent = submissions.get("filings", {}).get("recent", {})
            if companies is not None:
                periods = fiscal_periods(recent)
            for accession, document, form, filed, period in zip(
                *(recent.get(key, []) for key in ("accessionNumber", "primaryDocument",
                                                  "form", "filingDate", "reportDate"))
            ):
                filings[accession] = document
                if form in {"10-Q", "10-K", "10-Q/A", "10-K/A", "8-K"} and period[:4] in {
                    str(year) for year in years
                }:
                    evidence.filings.append({"Ticker": ticker, "Form": form, "Filed": filed,
                                             "Period": period, "URL": filing_url(ticker, accession, document, cik=cik),
                                             "Retrieved": retrieved})
        if facts_url in payloads:
            payload, retrieved = payloads[facts_url]
            extra_tags = {"Fuel expense": ("FuelCosts",), "Passenger revenue": ("PassengerRevenue",)} if evidence.industries[ticker] == "Aviation" else {}
            gaap = payload.get("facts", {}).get("us-gaap", {})
            evidence.disclosed_metrics[ticker] = {
                name for name, tags in (TAGS | extra_tags).items()
                if any(gaap.get(tag, {}).get("units", {}).get("USD") for tag in tags)
            }
            for year in years:
                if companies is not None and periods is None:
                    continue
                metrics = normalize(payload, ticker, year, quarter, retrieved, facts_url,
                                    filings, cik=cik, periods=periods, extra_tags=extra_tags)
                evidence.financial[ticker, year] = metrics
                if companies is not None:
                    evidence.preceding[ticker, year] = normalize(
                        payload, ticker, *prior_slot(year, quarter), retrieved, facts_url,
                        filings, cik=cik, periods=periods)
            current = evidence.financial.get((ticker, max(years)), {})
            operating_keys = set(extra_tags)
            if evidence.industries[ticker] == "Technology" or ticker == "AMZN":
                operating_keys.add("Capital expenditures")
            evidence.operating[ticker] = {key: current[key] for key in operating_keys if key in current}
        if evidence.period == (2026, 3) and ticker in RELEASES:
            sources = [RELEASES[ticker][1]]
            if ticker in RELEASE_ALTERNATES:
                sources.append(RELEASE_ALTERNATES[ticker])
            errors = []
            for url in sources:
                try:
                    body, retrieved, warning = cache.get(url, refresh, offline=not (refresh or retrieve_documents or ticker in fetch_tickers))
                    metrics, signals = parse_release(ticker, body, retrieved, url)
                    if not metrics:
                        raise SourceError("Expected operating metrics did not match the release.")
                    evidence.operating[ticker], evidence.signals[ticker] = metrics, signals
                    evidence.disclosure_status[ticker] = ("Loaded", warning or url)
                    if warning:
                        evidence.warnings.append(f"{ticker} · IR: {warning}")
                    if errors and refresh:
                        evidence.warnings.append(f"{ticker} · Using alternate publisher for the same issuer release. {errors[0]}")
                    break
                except SourceError as exc:
                    errors.append(f"{url}: {exc}")
                    if ticker not in evidence.disclosure_status or exc.status == "Refresh failed":
                        evidence.disclosure_status[ticker] = (exc.status, str(exc))
            else:
                evidence.warnings.extend(f"{ticker} · IR: {error}" for error in errors)
        if evidence.period == (2026, 3) and disclosure_key(ticker) in DISCLOSURES:
            published, url = DISCLOSURES[disclosure_key(ticker)]
            try:
                body, retrieved, warning = cache.get(url, refresh, offline=not (refresh or retrieve_documents or ticker in fetch_tickers))
                body = disclosure_body(url, body)
                evidence.operating.setdefault(ticker, {}).update(release_metrics(ticker, body, retrieved))
                evidence.disclosure_status[ticker] = ("Loaded", warning or url)
                parser = Blocks()
                parser.feed(body)
                blocks = body.splitlines() if url.endswith(".pdf") else parser.blocks
                excerpts = extract_signals(blocks, url, published, retrieved)
                if excerpts:
                    evidence.signals[ticker] = excerpts
                if warning:
                    evidence.warnings.append(f"{ticker} · IR: {warning}")
            except SourceError as exc:
                evidence.disclosure_status[ticker] = (exc.status, str(exc))
                evidence.warnings.append(f"{ticker} · IR: {exc}")
        if companies is not None and not evidence.signals.get(ticker):
            end = (periods or {}).get(evidence.period, (None, None))[0]
            documents = [item for item in evidence.filings if item["Ticker"] == ticker
                         and item["Period"] == end and item["Form"] in {"10-Q", "10-K"}]
            if documents:
                document = documents[0]
                try:
                    body, retrieved, warning = cache.get(
                        document["URL"], offline=not retrieve_documents)
                    parser = Blocks()
                    parser.feed(body)
                    evidence.disclosure_status.setdefault(ticker, ("Loaded", warning or document["URL"]))
                    evidence.signals[ticker] = extract_signals(
                        parser.blocks, document["URL"], document["Filed"], retrieved)
                    if warning:
                        evidence.warnings.append(f"{ticker} · Filing: {warning}")
                except SourceError as exc:
                    if retrieve_documents:
                        evidence.disclosure_status.setdefault(ticker, (exc.status, str(exc)))
                        evidence.warnings.append(f"{ticker} · Filing: {exc}")
    return evidence


def scenario(revenue, growth, operating_margin):
    if revenue is None or revenue <= 0:
        return None
    modeled_revenue = revenue * (1 + growth / 100)
    return modeled_revenue, modeled_revenue * operating_margin / 100


def research_topics(question):
    text = question.lower()
    topics = {
        "financial": r"revenu|profit|margin|cash|debt|financ|income|momentum|capex|capital expend",
        "operating": r"room|growth|revpar|pipeline|develop|operat|geograph|fuel|passenger|load factor|cloud|segment|competing",
        "strategy": r"why|strateg|risk|acqui|expan|loyalty|recommend|advantage|competing",
    }
    selected = {name for name, pattern in topics.items() if re.search(pattern, text)}
    if re.search(r"profit|margin|competing", text):
        selected.update(("financial", "operating", "strategy"))
    return selected or set(topics)


def brief(evidence, tickers, year, quarter, question):
    selected = research_topics(question)
    text = question.lower()
    lines = [f"Evidence review for calendar Q{quarter} {year} · {', '.join(tickers)}."]
    if re.search(r"why|cause", text):
        lines.append("Reported metrics and issuer commentary can identify possible drivers. The calculation does not isolate causal effects.")
    if "financial" in selected:
        lines.append("Methodology. Margin divides income by revenue. Growth divides current revenue by comparable prior revenue and subtracts one. Percentages multiply these results by 100.")
    citations = []

    def cite(url):
        if url not in citations:
            citations.append(url)
        return f"[{citations.index(url) + 1}]"

    def lineage(item, scope):
        inputs = [item, scope.get(item.name + " prior YTD input")]
        return " ".join(dict.fromkeys(cite(fact.url) for fact in inputs if fact))

    comparisons = {}
    for ticker in tickers:
        current = evidence.financial.get((ticker, year), {})
        prior = evidence.financial.get((ticker, year - 1), {})
        if "financial" in selected:
            revenue = current.get("Revenue")
            op_income = current.get("Operating income")
            growth = change(revenue, prior.get("Revenue"))
            profitability = margin(op_income, revenue)
            metrics = []
            for name in ("Revenue", "Net income attributable to parent", "Operating cash flow",
                         "Capital expenditures", "Cash and cash equivalents", "Total debt",
                         "Long-term debt including current maturities", "Current debt", "Noncurrent debt"):
                item = current.get(name)
                if not item:
                    metrics.append(f"{name}: Data unavailable")
                    continue
                refs = lineage(item, current)
                period = f"{item.start} to {item.end}" if item.start else f"as of {item.end}"
                label = "Net income" if name == "Net income attributable to parent" else name
                method = ", derived from year-to-date figures" if "YTD difference" in item.definition else ""
                metrics.append(f"{label}: ${item.value / 1e6:,.0f}m ({period}{method}) {refs}")
            metrics.append("Revenue YoY: Data unavailable" if growth is None else
                           f"Revenue YoY: {growth:+.1f}% {lineage(revenue, current)} {lineage(prior['Revenue'], prior)}")
            metrics.append("Operating margin: Data unavailable" if profitability is None else
                           f"Operating margin: {profitability:.1f}% {lineage(op_income, current)} {lineage(revenue, current)}")
            net = current.get("Net income attributable to parent")
            net_margin = margin(net, revenue)
            if net_margin is not None:
                metrics.append(f"Net margin: {net_margin:.1f}% {lineage(net, current)} {lineage(revenue, current)}")
            previous_scope = evidence.preceding.get((ticker, year), {})
            previous = previous_scope.get("Revenue")
            sequential = change(revenue, previous, sequential=True)
            if sequential is not None:
                metrics.append(f"Revenue QoQ: {sequential:+.1f}% ({previous.start} to {previous.end}) "
                               f"{lineage(revenue, current)} {lineage(previous, previous_scope)}")
            comparisons[ticker] = (revenue, op_income, profitability, growth)
            lines.append(f"{ticker} | " + ". ".join(metrics) + ".")
        if "operating" in selected:
            operating = evidence.operating.get(ticker, {})
            lines.append(f"{ticker} | " + (". ".join(
                f"{item.name}: {item.value:,.2f} {item.unit} "
                f"({item.start or 'as of'} {item.end}; {item.definition}) {cite(item.url)}"
                for item in operating.values()) or "Data unavailable for issuer operating metrics.") + ".")
            fuel = operating.get("Fuel expense")
            revenue = current.get("Revenue")
            fuel_share = margin(fuel, revenue)
            if fuel_share is not None:
                lines.append(f"{ticker} | Reported fuel expense as a share of consolidated revenue: "
                             f"{fuel_share:.1f}% {lineage(fuel, current)} {lineage(revenue, current)}. "
                             "Revenue mix and the treatment of fuel-related costs can differ by carrier.")
        if "strategy" in selected:
            for category, snippet, url, published, _ in evidence.signals.get(ticker, []):
                lines.append(f"{ticker} | {category}, issuer commentary published {published} {cite(url)}: {snippet}")
            if not evidence.signals.get(ticker):
                lines.append(f"{ticker} | Data unavailable for validated management commentary in this window.")
    if tickers and tickers[0] in comparisons:
        primary = tickers[0]
        base_revenue, base_income, base_margin, base_growth = comparisons[primary]
        for peer in tickers[1:]:
            if peer not in comparisons:
                continue
            revenue, income, profitability, growth = comparisons[peer]
            industries = (evidence.industries.get(primary), evidence.industries.get(peer))
            if not comparable_peers(base_revenue, revenue, *industries):
                lines.append(f"{primary} vs {peer} | Financial ranking unavailable. The industry, reporting dates, or revenue concept differs or could not be confirmed.")
                continue
            if (base_margin is not None and profitability is not None
                    and comparable_peers(base_income, income, *industries)):
                primary_scope = evidence.financial[primary, year]
                peer_scope = evidence.financial[peer, year]
                lines.append(f"{primary} minus {peer} operating margin: {base_margin - profitability:+.1f} pp "
                             f"{lineage(base_income, primary_scope)} {lineage(income, peer_scope)} "
                             f"{lineage(base_revenue, primary_scope)} {lineage(revenue, peer_scope)}.")
            if base_growth is not None and growth is not None:
                base_prior = evidence.financial[primary, year - 1]["Revenue"]
                peer_prior = evidence.financial[peer, year - 1]["Revenue"]
                if comparable_peers(base_prior, peer_prior, *industries):
                    sources = (
                        (base_revenue, evidence.financial[primary, year]),
                        (revenue, evidence.financial[peer, year]),
                        (base_prior, evidence.financial[primary, year - 1]),
                        (peer_prior, evidence.financial[peer, year - 1]),
                    )
                    refs = " ".join(lineage(item, scope) for item, scope in sources)
                    lines.append(f"{primary} minus {peer} revenue growth YoY: {base_growth - growth:+.1f} pp {refs}.")
    if citations:
        if "operating" in selected and any(
            evidence.industries.get(ticker) == "Hospitality" and evidence.operating.get(ticker)
            for ticker in tickers
        ):
            for metric in ("Comparable hotel RevPAR YoY", "Net rooms YoY"):
                peer_values = [evidence.operating.get(ticker, {}).get(metric) for ticker in tickers]
                observed = sorted(filter(None, peer_values), key=lambda item: item.value)
                if len(observed) >= 2:
                    low, high = observed[0], observed[-1]
                    gap = high.value - low.value
                    lines.append(f"{metric} spread among {len(observed)} reporting peers: "
                                 f"{high.ticker} {high.value:+.1f}% {cite(high.url)} versus "
                                 f"{low.ticker} {low.value:+.1f}% {cite(low.url)}, a {gap:.1f} pp gap. "
                                 "Check portfolio mix before attributing the gap to execution.")
            lines.append("Action: Prioritize a market-level comparison of demand and net room growth. "
                         "Test pipeline conversion, opening timelines and property-level returns before allocating capital. "
                         "Portfolio and pipeline definitions differ across issuers.")
        if "financial" in selected and any(evidence.financial.get((ticker, year)) for ticker in tickers):
            lines.append("Action: Evaluate reported profitability alongside operating cash generation and debt. "
                         "Check each issuer's revenue mix, one-off items and accounting definitions before inferring efficiency.")
        if "strategy" in selected:
            lines.append("Action: Test management's explanation against comparable operating and expense data. "
                         "Check whether the reported drivers persist in subsequent filings.")
    else:
        lines.append("Recommendation deferred pending verified evidence for the selected scope.")
    lines.append("Limitations: Data unavailable means a source or compatible fact could not be verified. "
                 "Management commentary reflects issuer views. Priority changes require comparable disclosures over time.")
    return lines, citations


def ollama_evidence(question, lines):
    # For a direct margin comparison, keep the cited calculations and issuer
    # labels. Leave broader or causal questions the full evidence briefing.
    query = question.casefold()
    metric = next((name for name in ("operating margin", "net margin") if name in query), None)
    if not metric or re.search(r"why|cause|driver|explain|revenue|growth|cash|debt|strategy", query):
        return lines
    selected = []
    for line in lines:
        issuer, separator, detail = line.partition(" | ")
        if separator:
            selected.extend(f"{issuer} | {part}" for part in detail.split(". ")
                            if metric in part.casefold())
        elif metric in line.casefold():
            selected.append(line)
    return selected or lines


def ollama(question, lines, model, *, source_count=None):
    lines = ollama_evidence(question, lines)
    citation_pattern = r"\[(\d+)\]"
    permitted = {int(number) for line in lines for number in re.findall(citation_pattern, line)}
    if source_count is not None:
        permitted.intersection_update(range(1, source_count + 1))
    if not permitted:
        raise ValueError("No verified citations are available for this question.")
    schema = {
        "type": "object",
        "properties": {
            "assessment": {"type": "string"},
            "source_ids": {"type": "array", "minItems": 1,
                           "items": {"type": "integer", "enum": sorted(permitted)}},
        },
        "required": ["assessment", "source_ids"],
        "additionalProperties": False,
    }
    result = ollama_chat(model, schema, [
        {"role": "system", "content": "Use only the supplied evidence and its citations. "
         "Repeat figures exactly as supplied, including units. Fill assessment and source_ids."},
        {"role": "user", "content": json.dumps({"question": question, "evidence": lines})},
    ], output_tokens=384)
    if not isinstance(result, dict) or set(result) != {"assessment", "source_ids"}:
        raise ValueError("Ollama's answer did not match the assessment schema.")
    content = result["assessment"]
    cited = result["source_ids"]
    if (not isinstance(content, str) or not content.strip() or not isinstance(cited, list) or not cited
            or any(type(number) is not int or number not in permitted for number in cited)):
        raise ValueError("The model response did not use valid evidence citations.")
    if re.search(r"concise assessment based on evidence", content, re.I):
        raise ValueError("Ollama returned a generic assessment without a specific finding.")
    if any(int(number) not in cited for number in re.findall(citation_pattern, content)):
        raise ValueError("The model response included an unverified inline citation.")

    # This is a guard against novel numbers, not a semantic check of every claim.
    def figures(value):
        clean = re.sub(citation_pattern, "", value)
        return {Decimal(number.replace(",", "")) for number in
                re.findall(r"(?<![\w.])[-−+]?\d[\d,]*(?:\.\d+)?", clean.replace("−", "-"))}

    cited_evidence = [line for line in lines
                      if any(int(number) in cited for number in re.findall(citation_pattern, line))]
    if figures(content) - figures(" ".join(cited_evidence)):
        raise ValueError("The model response contained figures absent from its cited evidence.")
    refs = " ".join(f"[{number}]" for number in dict.fromkeys(cited))
    return f"{content.strip()}\n\nEvidence {refs}"



def ollama_chat(model, schema, messages, *, output_tokens):
    payload = json.dumps({
        "model": model, "stream": False, "think": False, "keep_alive": "2m",
        "format": schema, "messages": messages,
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": output_tokens},
    }).encode()
    request = Request("http://127.0.0.1:11434/api/chat", data=payload,
                      headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            envelope = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"Ollama model {model!r} is unavailable. Check its exact name with ollama list.") from exc
        raise ValueError(f"Ollama rejected the chat request (HTTP {exc.code}).") from exc
    except (TimeoutError, URLError) as exc:
        if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError):
            raise ValueError("Ollama timed out while generating the assessment.") from exc
        raise ValueError("Could not connect to Ollama at 127.0.0.1:11434. Check that it is running.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Ollama returned an invalid JSON API response.") from exc
    except OSError as exc:
        raise ValueError("The connection to Ollama failed while receiving the assessment.") from exc

    if not isinstance(envelope, dict):
        raise ValueError("Ollama returned an invalid chat response.")
    if envelope.get("error"):
        raise ValueError(f"Ollama reported an error: {envelope['error']}")
    message = envelope.get("message")
    if envelope.get("done") is not True or not isinstance(message, dict):
        raise ValueError("Ollama did not return a completed chat response.")
    visible = message.get("content")
    if not isinstance(visible, str) or not visible.strip():
        if message.get("thinking"):
            raise ValueError("Ollama returned reasoning but no visible answer.")
        raise ValueError("Ollama returned an empty answer.")
    if envelope.get("done_reason") == "length":
        raise ValueError("Ollama reached its output token limit before completing the assessment.")
    try:
        return json.loads(visible)
    except json.JSONDecodeError as exc:
        raise ValueError("Ollama's visible answer was not valid structured JSON.") from exc
