"""Streamlit entry point for the competitive strategy platform."""
from dataclasses import asdict
from datetime import date
import os
from pathlib import Path
from inspect import signature

import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

from competitive.coverage import coverage_reason, latest_shared_financial_period
from competitive.executive import clarification, executive_analysis, executive_angle, executive_evidence, question_metrics
from competitive.financial import TAGS, Metric, aligned, change, comparable_peers, margin
from competitive.industry import AVIATION, HOSPITALITY, TECHNOLOGY
from competitive.intelligence import SIGNALS, load, ollama_chat, research_topics, scenario
from competitive.report import report_pdf
from competitive.sectors import (BRAND, DRIVERS, SECTORS, SECTOR_ISSUERS, TRAVEL_METRICS,
                                 benchmark_status, financial_yoy, sector_scenario, trend_cohorts)
from competitive.sources import (COMPANIES, Cache, SourceError, cached_companies,
                                 company_directory, search_companies, search_sec, select_issuers)

st.set_page_config(page_title=BRAND, page_icon="📊", layout="wide")

PAGES = ("About the Platform", "Financial Performance", "Competitive Benchmarking",
         "Strategic Signals", "Scenario Simulator", "AI Executive Briefing",
         "Data Sources & Methodology")
path = Path(os.getenv("COMPETITIVE_CACHE", "data/competitive.sqlite3"))
path.parent.mkdir(parents=True, exist_ok=True)
sec_identity = os.getenv("SEC_USER_AGENT", "")
if not sec_identity:
    try:
        sec_identity = st.secrets.get("SEC_USER_AGENT", "")
    except StreamlitSecretNotFoundError:
        pass
cache = Cache(path, sec_identity)

with st.sidebar:
    st.header(BRAND)
    page = st.radio("Workspace", PAGES, key="page_radio")
    st.divider()
    refresh = st.button("Refresh financial and disclosure sources", type="primary", width="stretch")
    refresh_directory = st.button("Update SEC company directory")
    def reset_company_selection():
        st.session_state.pop("primary_issuer", None)
        st.session_state.pop("peer_issuers", None)
        st.session_state.pop("issuer_query", None)

    sector = st.selectbox("Sector", [*SECTORS, "All SEC issuers"], on_change=reset_company_selection)
    companies = COMPANIES | SECTOR_ISSUERS | cached_companies(cache)
    try:
        fetch_directory = bool(cache.identity) and not st.session_state.get("directory_attempted")
        if fetch_directory or refresh_directory:
            st.session_state.directory_attempted = True
        directory, _, warning = company_directory(cache, refresh_directory, fetch=fetch_directory)
        companies.update(directory)
        if warning:
            st.warning(warning)
    except (SourceError, ValueError, KeyError, TypeError) as exc:
        st.caption("Search currently covers issuers in the local cache. Update the SEC directory to expand it.")
        if refresh_directory:
            st.warning(str(exc))
    query = st.text_input("Find companies by name or ticker", key="issuer_query")
    search_clicked = st.button("Search SEC by name or ticker", disabled=not query.strip())
    sec_matches = st.session_state.setdefault("sec_matches", {})
    attempted_queries = st.session_state.setdefault("attempted_sec_queries", set())
    query_key = query.strip().casefold()
    automatic_search = (bool(cache.identity and query_key) and query_key not in attempted_queries
                        and not search_companies(companies | sec_matches, query))
    if search_clicked or automatic_search:
        attempted_queries.add(query_key)
        try:
            results, warning = search_sec(cache, query, fetch=True)
            sec_matches.update(results)
            if warning:
                st.warning(warning)
            if not results:
                st.caption("The SEC returned no listed ticker matches.")
        except (SourceError, ValueError, TypeError) as exc:
            st.warning(f"SEC search unavailable. {exc}")
    companies.update(sec_matches)
    sector_names = SECTORS.get(sector, {})
    sector_choices = [ticker for ticker in sector_names if ticker in companies]
    defaults = [ticker for ticker in (sector_choices or list(COMPANIES))[:3]]
    if sector == "Hospitality" and st.query_params.get("comparison") == "choice-wyndham":
        defaults = ["CHH", "WH"]
    pinned = [*defaults, st.session_state.get("primary_issuer"),
              *st.session_state.get("peer_issuers", [])]
    searchable = {ticker: (sector_names.get(ticker, name), cik)
                  for ticker, (name, cik) in companies.items()
                  if sector == "All SEC issuers" or ticker in sector_names}
    matches = search_companies(searchable, query)[:100]
    choices = list(dict.fromkeys([*matches, *[ticker for ticker in pinned if ticker in companies]]))
    if query and not matches:
        st.caption("No match in this sector. All SEC issuers includes companies outside the sector shortlist.")

    def label(ticker):
        name = next((names[ticker] for names in SECTORS.values() if ticker in names), companies[ticker][0])
        return f"{name} · {ticker}"

    st.caption("Company directory · choose candidates to load and validate")
    primary = st.selectbox("Candidate primary company", choices, index=choices.index(defaults[0]),
                           format_func=label, key="primary_issuer")
    peer_choices = [ticker for ticker in choices if companies[ticker][1] != companies[primary][1]]
    peers = st.multiselect("Candidate competitors (up to three)", peer_choices,
                           default=[ticker for ticker in defaults[1:] if ticker in peer_choices],
                           format_func=label, max_selections=3, key="peer_issuers")
    tickers = select_issuers(companies, primary, peers)
    if "IHG" in tickers:
        st.caption("IHG files Form 20-F and 6-K. Comparable quarterly USD US GAAP figures are unavailable in this pipeline.")
    if len(tickers) != len(peers) + 1:
        st.caption("Share classes with the same SEC CIK are counted as one issuer.")
    mode = st.radio("Reporting period", ("Latest primary quarter", "Choose calendar quarter"))
    lookback = st.slider("Historical years", 2, 5, 3)
    year = quarter = None
    if mode == "Choose calendar quarter":
        year = st.selectbox("Calendar year", range(date.today().year, 2017, -1))
        quarter = st.selectbox("Calendar quarter", (3, 4, 2, 1))

    if cache.identity:
        st.caption("Company selection loads SEC data automatically. Refresh checks for updated filings.")
    else:
        st.caption("Using cached data. Set SEC_USER_AGENT to enable automatic SEC retrieval.")

st.caption(BRAND)
st.title(page)
question = model = ""
analyze = False
research_page = page == "AI Executive Briefing"
scope = (tuple(tickers), year, quarter, lookback)
result = None if refresh else st.session_state.get("research_result")
if refresh:
    st.session_state.pop("research_result", None)
if research_page:
    st.subheader("Ask the AI")
    st.caption(
        "Ask a strategic question and get an evidence-backed executive assessment: findings, "
        "company-specific recommendations and source citations, built from verified filings, "
        "disclosures, and Python calculations — with optional local Ollama interpretation."
    )
    with st.form(f"research_{page}", border=True):
        question = st.text_area(
            "Strategic question",
            value=result["question"] if result and result.get("candidates") == tuple(tickers) else
            "Which company has stronger financial momentum?",
            max_chars=2000, key=f"research_question_{page}",
        )
        model = st.text_input("Optional local Ollama model", placeholder="Installed model name",
                              key=f"research_model_{page}",
                              value=result["model"] if result and result.get("candidates") == tuple(tickers) else
                              os.getenv("OLLAMA_MODEL", ""))
        analyze = st.form_submit_button("Analyze question", type="primary")
        test_model = st.form_submit_button("Test local model")
    question, model = question.strip(), model.strip()
    if analyze and not question:
        st.warning("Enter a strategic question to start research.")
        analyze = False
    if not model:
        st.info("AI reasoning is disabled until you enter and test an installed Ollama model. Calculated evidence is available.")
    elif test_model:
        st.session_state.pop("tested_model", None)
        try:
            reply = ollama_chat(model, {"type": "object", "properties": {"ready": {"type": "boolean"}},
                                       "required": ["ready"]},
                                [{"role": "user", "content": "Return ready true."}], output_tokens=24)
            if reply != {"ready": True}:
                raise ValueError("The model did not return the expected test response.")
            st.session_state.tested_model = model
            st.success("Local model returned a valid test response.")
        except (OSError, ValueError) as exc:
            st.warning(f"Model test failed. {exc}")
    if model and st.session_state.get("tested_model") != model:
        st.caption("Test this model before enabling AI reasoning. Calculated briefings remain available.")

if research_page and (prompt := clarification(question)):
    st.subheader("Clarify your question")
    st.info(prompt)
    st.stop()

requested_years = list(range(year - lookback, year + 1)) if year else None
attempted_issuers = st.session_state.setdefault("attempted_issuers", set())
new_issuers = {ticker for ticker in tickers if cache.identity and companies[ticker][1] not in attempted_issuers}
fetch_tickers = set(tickers) if analyze else new_issuers
with st.spinner("Retrieving evidence…" if refresh or fetch_tickers else "Reading cached evidence…"):
    evidence = load(cache, tickers, requested_years, quarter, refresh, companies=companies,
                    lookback=lookback, fetch_tickers=fetch_tickers,
                    retrieve_documents=refresh or (analyze and "strategy" in research_topics(question)))
attempted_issuers.update(companies[ticker][1] for ticker in new_issuers)

if evidence.warnings:
    with st.expander(f"Source status · {len(evidence.warnings)} notices",
                     expanded=not (evidence.financial or evidence.operating)):
        for warning in evidence.warnings:
            st.text(warning)

if evidence.latest:
    st.caption("Latest SEC periods · " + "  |  ".join(
        f"{ticker} {end}" for ticker, (_, end) in evidence.latest.items()))
if evidence.period is None:
    with st.expander("Data Coverage", expanded=True):
        for ticker in tickers:
            state, detail = coverage_reason(evidence, ticker, companies[ticker][1])
            st.write(f"{label(ticker)} · {state}. {detail}")
    st.info("No eligible companies for this reporting window. Refresh sources or choose a different period.")
    st.stop()
year, quarter = evidence.period
st.caption(f"COMPARISON WINDOW NEAR CALENDAR  /  Q{quarter} {year}")
years = list(range(year - lookback + 1, year + 1))
current = {ticker: evidence.financial.get((ticker, year), {}) for ticker in tickers}
operating = evidence.operating
missing = "Data unavailable"
no_financial = "No compatible SEC fact for the selected quarter, or the source could not be retrieved."


def show_table(rows):
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"URL": st.column_config.LinkColumn("Source", display_text="Open source"),
                                "url": st.column_config.LinkColumn("Filing or release"),
                                "input_url": st.column_config.LinkColumn("Retrieved from"),
                                "Company": st.column_config.TextColumn("Company", width=220),
                                "Comparability": st.column_config.TextColumn("Comparability", width=320),
                                "Value": st.column_config.TextColumn("Value", width=140),
                                "Detail": st.column_config.TextColumn("Detail", width=320),
                                "Metric": st.column_config.TextColumn("Metric", width=200),
                                "Status": st.column_config.TextColumn("Status", width=260)})


def series_chart(rows, x, y, kind="bar"):
    if not rows:
        st.info(f"{missing}. No eligible observations to chart.")
        return
    frame = pd.DataFrame(rows)
    if kind == "line":
        chart = px.line(frame, x=x, y=y, color="Ticker", markers=True,
                        hover_data=["Period end"] if "Period end" in frame else None)
        chart.update_xaxes(dtick=1)
    else:
        chart = px.bar(frame, x=x, y=y, color="Ticker")
    chart.update_layout(legend_title_text="", margin=dict(l=0, r=0, t=12, b=0))
    chart.update_layout(hovermode="x unified" if kind == "line" else "closest")
    st.plotly_chart(chart, width="stretch")


def format_number(value, unit):
    """Scale a raw reported number to K/M/B with a consistent unit suffix.
    Precise figures remain available separately in source-lineage and raw tables."""
    abs_value = abs(value)
    if unit == "USD":
        if abs_value >= 1e9:
            return f"${value / 1e9:,.2f}B"
        if abs_value >= 1e6:
            return f"${value / 1e6:,.1f}m"
        return f"${value:,.0f}"
    if abs_value >= 1e9:
        return f"{value / 1e9:,.1f}B {unit}"
    if abs_value >= 1e6:
        return f"{value / 1e6:,.1f}M {unit}"
    if abs_value >= 1e3:
        return f"{value / 1e3:,.1f}K {unit}"
    return f"{value:,.2f} {unit}"


def format_metric_value(item):
    return format_number(item.value, item.unit) if item else missing


def money(item):
    return format_metric_value(item)


def percent(value):
    return f"{value:+.1f}%" if value is not None else missing


def financial_display(ticker, selected_year, metric, mode):
    data = evidence.financial.get((ticker, selected_year), {})
    previous = {}
    items = [data.get(metric)]
    value = items[0].value / 1e6 if items[0] else None
    if metric.endswith("margin"):
        income = "Operating income" if metric == "Operating margin" else "Net income attributable to parent"
        items = [data.get(income), data.get("Revenue")]
        value = margin(*items)
    elif metric.endswith(("YoY", "QoQ")):
        name = "Revenue" if metric.startswith("Revenue") else "Net income attributable to parent"
        sequential = metric.endswith("QoQ")
        previous = (evidence.preceding.get((ticker, selected_year), {}) if sequential
                    else evidence.financial.get((ticker, selected_year - 1), {}))
        items = [data.get(name), previous.get(name)]
        value = change(*items, sequential=sequential)
    if mode == "Year-over-year growth":
        previous = evidence.financial.get((ticker, selected_year - 1), {})
        if metric.endswith("margin"):
            prior_items = [previous.get(income), previous.get("Revenue")]
            prior_value = margin(*prior_items)
            compatible = all(aligned(now, then) and now.definition == then.definition
                             for now, then in zip(items, prior_items))
            value = (100 * (value / prior_value - 1) if compatible and value is not None
                     and prior_value is not None and prior_value > 0 else None)
        else:
            prior_items = [previous.get(metric)]
            value = financial_yoy(items[0], prior_items[0])
        items += prior_items
    chart_facts = tuple(item for item in items if item is not None)
    if metric == "Long-term debt including current maturities":
        items += [data.get("Current debt"), data.get("Noncurrent debt")]
    for item in tuple(filter(None, items)):
        for scope in (data, previous):
            if scope.get(item.name) == item:
                items.extend(scope.get(item.name + suffix) for suffix in
                             (" prior YTD input", " current YTD input"))

    return value, items, chart_facts


candidates = tuple(tickers)
metric = None
display_mode = "Reported values and ratios"
if page == "Financial Performance":
    measures = [*TAGS, "Long-term debt including current maturities", "Operating margin", "Net margin",
                "Revenue YoY", "Net income YoY", "Revenue QoQ", "Net income QoQ"]
    comparison_default = "Net income attributable to parent" if {"CHH", "WH"} <= set(candidates) else "Revenue"
    metric = st.selectbox("Measure", measures, index=measures.index(comparison_default))
    display_mode = st.radio("Display", ("Reported values and ratios", "Year-over-year growth", "Index (base 100)"),
                    horizontal=True) if not metric.endswith(("YoY", "QoQ")) else "Reported values and ratios"
elif page == "Competitive Benchmarking":
    modules = {"Aviation": AVIATION, "Technology": TECHNOLOGY, "Hospitality": HOSPITALITY,
               "Travel & Tourism": TRAVEL_METRICS}
    relevant = [name for ticker in tickers for name in modules.get(evidence.industries.get(ticker), ())]
    if "AMZN" in tickers:
        relevant.extend(TECHNOLOGY)
    options = list(dict.fromkeys([*relevant, *(name for data in operating.values() for name in data)]))
    if not options:
        st.info("No validated sector measures for this selection. See Financial Performance for SEC results.")

    options = options or ["Revenue"]
    default = options.index("US RevPAR YoY") if {"CHH", "WH"} <= set(candidates) and "US RevPAR YoY" in options else 0
    metric = st.selectbox("Operating measure", options, index=default)
    benchmark_values = {}
    for ticker in tickers:
        item = operating.get(ticker, {}).get(metric)
        if metric == "Revenue":
            item = current[ticker].get("Revenue")
        if metric == "Operating margin":
            income = current[ticker].get("Operating income")
            revenue = current[ticker].get("Revenue")
            ratio = margin(income, revenue)
            if ratio is not None:
                item = Metric(ticker, metric, income.start, income.end, ratio, "%", income.url,
                              income.published, income.retrieved,
                              f"Operating income / revenue ({revenue.definition})")
        benchmark_values[ticker] = item

signal_category = st.selectbox("Signal type", ("All", *SIGNALS)) if page == "Strategic Signals" else "All"
eligible = []
coverage = []
for ticker in candidates:
    facts = current[ticker]
    if page == "Financial Performance":
        has_data = financial_display(ticker, year, metric, display_mode)[0] is not None
    elif page == "Competitive Benchmarking":
        has_data = benchmark_values[ticker] is not None
    elif page == "Strategic Signals":
        has_data = any(signal_category == "All" or signal[0] == signal_category
                       for signal in evidence.signals.get(ticker, []))
    elif page == "Scenario Simulator":
        has_data = "Revenue" in facts
    elif research_page:
        has_data = bool(executive_evidence(evidence, [ticker], year, question)[0]) or bool(facts or operating.get(ticker))
    else:
        has_data = bool(facts or operating.get(ticker) or evidence.signals.get(ticker))
    if has_data:
        eligible.append(ticker)
        state, detail = "Eligible", "Verified evidence for this view."
        relevant_status = [entry for (owner, _), entry in evidence.source_status.items() if owner == ticker]
        relevant_status.append(evidence.disclosure_status.get(ticker, ("", "")))
        cache_warnings = [message for _, message in relevant_status if message.startswith("Using cached response")]
        if cache_warnings:
            state, detail = "Eligible · refresh failed", " ".join(cache_warnings)
    else:
        state, detail = coverage_reason(
            evidence, ticker, companies[ticker][1], metric=metric,
            disclosure=page == "Strategic Signals" or
            (page == "Competitive Benchmarking" and metric not in {*TAGS, "Operating margin"}),
        )
    coverage.append({"Company": label(ticker), "Status": state, "Detail": detail})

with st.expander("Data Coverage", expanded=len(eligible) < len(candidates)):
    show_table(coverage)
    if len(eligible) < len(candidates) and page in {"Financial Performance", "AI Executive Briefing"}:
        measure = metric if metric in TAGS else "Net income attributable to parent"
        if st.button("Find a verified shared financial quarter"):
            shared = latest_shared_financial_period(cache, candidates, companies, evidence, measure)
            if shared:
                (shared_year, shared_quarter), observations = shared
                st.success(f"Try Q{shared_quarter} {shared_year} under Choose calendar quarter.")
                for fact in observations:
                    st.caption(f"{label(fact.ticker)} · {fact.start or 'As of'} to {fact.end} · [Filing]({fact.url})")
            else:
                st.caption("No earlier comparable quarter found among the loaded filings checked.")
if not eligible:
    st.info("No companies qualify for this view. See Data Coverage for the source or metric status.")
    st.stop()
with st.sidebar:
    st.caption("Verified companies in this view")
    selection_key = repr((page, metric, display_mode, evidence.period, tuple(eligible)))
    active_primary = st.selectbox("Primary company", eligible, format_func=label, key="active-primary-" + selection_key)
    active_choices = [ticker for ticker in eligible if ticker != active_primary]
    active_peers = st.multiselect("Competitors", active_choices, default=active_choices,
                                 format_func=label, max_selections=3,
                                 key="active-peers-" + selection_key + active_primary)
tickers = [active_primary, *active_peers]
current = {ticker: current[ticker] for ticker in tickers}
operating = {ticker: operating.get(ticker, {}) for ticker in tickers}
scope = (tuple(tickers), year, quarter, lookback)

if research_page:
    result_key = (scope, evidence.period, question, model)
    observations, limitations = executive_evidence(evidence, tickers, year, question)
    if analyze:
        response = error = None
        if model and not observations:
            error = "AI reasoning is unavailable for this question because there are no verified sources."
        elif model and st.session_state.get("tested_model") == model:
            try:
                with st.spinner("Running local Ollama…"):
                    response = executive_analysis(question, observations, model)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                error = f"AI reasoning is disabled for this request. Ollama could not return a validated answer. {exc}"
        result = {"key": result_key, "scope": scope, "question": question, "model": model, "response": response, "error": error,
                  "observations": observations, "limitations": limitations, "candidates": candidates}
        st.session_state.research_result = result
    matching_result = result and result["key"] == result_key
    if page == "AI Executive Briefing" or matching_result:
        if matching_result and result["error"]:
            st.warning(result["error"])
        assessment = result["response"] if matching_result else None
        if matching_result:
            observations = result.get("observations", observations)
            limitations = result.get("limitations", limitations)
        def cited_statement(statement, key):
            references = list(dict.fromkeys(
                f"[{fact.ticker}, {fact.start or 'as of'} to {fact.end}]({fact.url})"
                for fact in observations[key].sources
            ))
            st.markdown(statement.replace("$", r"\$") + " " + " · ".join(references))

        requested = question_metrics(question)
        lead = requested[0] if requested else next(iter(observations), None)
        if lead not in observations:
            lead = None
        unavailable = (f"No verified evidence for the requested measure. {limitations[0]}"
                       if requested and limitations else
                       "No verified evidence answers this question for the selected companies and period.")
        st.subheader("Executive conclusion")
        st.caption("Deterministic analysis — calculated directly from verified evidence, no AI interpretation applied.")
        if lead:
            cited_statement(observations[lead].text, lead)
        else:
            st.write(unavailable)

        model_used = isinstance(assessment, dict)
        selected_keys = list(observations)
        if model_used:
            st.caption("Local Ollama interpretation prioritizes verified findings and management considerations below.")
            selected_keys = list(dict.fromkeys((assessment["conclusion"], *assessment["findings"], *selected_keys)))
        findings = [key for key in selected_keys if key != lead]
        if findings:
            st.subheader("Key findings")
            for key in findings:
                cited_statement(observations[key].text, key)
        recommendations = assessment["recommendations"] if model_used else [
            key for key in selected_keys if observations[key].recommendation]
        if model_used:
            st.subheader("Strategic implications")
            for key in dict.fromkeys((assessment["conclusion"], *recommendations)):
                cited_statement(executive_angle(key)[0], key)
        if recommendations:
            st.subheader("Management considerations")
            for key in recommendations:
                cited_statement(observations[key].recommendation or executive_angle(key)[1], key)
        st.subheader("Sources and limitations")
        for limitation in limitations:
            st.write(limitation)
        if observations:
            st.caption("Reported results describe performance. Establishing its causes requires additional evidence.")
        sources = list(dict.fromkeys(fact.url for item in observations.values() for fact in item.sources))
        with st.expander("Supporting calculations and source details"):
            st.caption("Revenue growth compares reported revenue with the equivalent prior-year period. "
                       "Margins divide the relevant income measure by reported revenue.")
            for number, url in enumerate(sources, 1):
                st.caption(f"[{number}] [Evidence source]({url})")
            source_details = list(dict.fromkeys(
                fact for item in observations.values() for fact in item.sources))
            if source_details:
                show_table([{"Ticker": fact.ticker, "Measure": fact.name, "Start": fact.start,
                             "End": fact.end, "Published": fact.published,
                             "Value": fact.value if fact.unit else None, "Unit": fact.unit, "URL": fact.url}
                            for fact in source_details])
        if page == "AI Executive Briefing":
            try:
                report_sources = list(sources)

                def report_statement(statement, key):
                    references = []
                    for fact in observations[key].sources:
                        if fact.url not in report_sources:
                            report_sources.append(fact.url)
                        reference = f"[{report_sources.index(fact.url) + 1}] {fact.ticker}, {fact.start or 'as of'} to {fact.end}"
                        if reference not in references:
                            references.append(reference)
                    return statement + " " + " · ".join(references)

                sections = [
                    ("Executive summary", [report_statement(observations[lead].text, lead)] if lead else
                     [unavailable]),
                    ("Company comparison", [", ".join(label(ticker) for ticker in tickers)]),
                    ("Supporting findings", [report_statement(observations[key].text, key) for key in findings]),
                    ("Strategic implications", [report_statement(executive_angle(key)[0], key)
                                                for key in recommendations] if model_used else []),
                    ("Limitations", limitations),
                    ("Management considerations", [report_statement(
                        observations[key].recommendation or executive_angle(key)[1], key) for key in recommendations]),
                    ("Methodology", ["Margins divide reported income by reported revenue. Revenue growth compares equivalent prior-year quarters. "
                                     "Source records retain each issuer's actual dates, accounting concepts and units."]),
                ]
                status = "Deterministic conclusion with Ollama-prioritized supporting evidence" if model_used else "Deterministic analysis"
                title = f"Q{quarter} {year} executive briefing"
                if "sections" in signature(report_pdf).parameters:
                    pdf = report_pdf(title, [status, question], report_sources, sections=sections)
                else:
                    report_lines = [status, question]
                    for heading, paragraphs in sections:
                        if paragraphs:
                            report_lines.extend([heading, *paragraphs])
                    pdf = report_pdf(title, report_lines, report_sources)
                st.download_button("Download executive PDF", pdf, f"briefing-{year}-q{quarter}.pdf", "application/pdf")
            except ImportError:
                st.warning("Install ReportLab to enable PDF downloads.")


CAPABILITIES = (
    ("📈", "Financial Performance", "Analyze revenue, profitability, margins, cash flow, and historical trends.",
     "Financial Performance"),
    ("🏆", "Competitive Benchmarking", "Compare companies using relevant financial and industry-specific metrics.",
     "Competitive Benchmarking"),
    ("📰", "Strategic Signals", "Examine expansion, investments, acquisitions, risks, and management priorities.",
     "Strategic Signals"),
    ("🧮", "Scenario Simulator", "Test business assumptions and evaluate hypothetical outcomes.",
     "Scenario Simulator"),
    ("🤖", "AI Executive Briefing", "Ask strategic questions and generate evidence-backed executive assessments.",
     "AI Executive Briefing"),
    ("📄", "Executive Reporting", "Download professional reports containing findings, recommendations, and citations.",
     "AI Executive Briefing"),
    ("📚", "Data Transparency", "Review verified sources, calculation methods, and reporting limitations.",
     "Data Sources & Methodology"),
)
WORKFLOW_STAGES = (
    ("1", "Collect", "Retrieve available financial data from SEC filings and official company disclosures."),
    ("2", "Validate", "Use Python to organize data, calculate financial indicators, and check comparability."),
    ("3", "Compare", "Benchmark companies against selected competitors."),
    ("4", "Interpret", "Use the local Ollama AI model, when enabled, to interpret verified findings."),
    ("5", "Report", "Present strategic insights, interactive analyses, and downloadable executive reports."),
)


def go_to_page(target):
    # Must run as an on_click callback: Streamlit forbids writing a widget's
    # session_state key after that widget has already been instantiated in
    # the same run, so this cannot be called directly from the page body.
    st.session_state["page_radio"] = target


def headline_operating_metric(ticker):
    modules = {"Aviation": AVIATION, "Technology": TECHNOLOGY, "Hospitality": HOSPITALITY,
              "Travel & Tourism": TRAVEL_METRICS}
    candidates = modules.get(evidence.industries.get(ticker), ())
    data = operating.get(ticker, {})
    return next((data[name] for name in candidates if name in data), None)


if page == "About the Platform":
    st.markdown(f"## {BRAND}")
    st.write(
        "Travel & Hospitality Strategy Intelligence AI is an AI-assisted competitive intelligence "
        "platform designed to analyze real publicly traded companies and their competitors across "
        "hospitality, aviation, and travel. It combines verified financial data, official company "
        "disclosures, industry-specific performance indicators, and AI analysis to support "
        "evidence-based strategic decision-making."
    )

    st.subheader("Why it was built")
    st.write(
        "Business leaders and analysts often spend significant time reviewing financial filings, "
        "earnings releases, and competitor disclosures to understand market developments. This "
        "platform was developed to bring those sources together, simplify competitive analysis, "
        "identify performance gaps, and translate complex financial information into actionable "
        "strategic insights."
    )

    st.subheader("What the platform does")
    for row_start in range(0, len(CAPABILITIES), 4):
        for column, (icon, title, description, target) in zip(st.columns(4), CAPABILITIES[row_start:row_start + 4]):
            with column:
                with st.container(border=True):
                    st.markdown(f"**{icon} {title}**")
                    st.caption(description)
                    st.button("Open", key=f"open-{title}", width="stretch",
                             on_click=go_to_page, args=(target,))

    st.subheader("How the AI agent works")
    stage_columns = st.columns(len(WORKFLOW_STAGES))
    for column, (number, stage, description) in zip(stage_columns, WORKFLOW_STAGES):
        with column:
            with st.container(border=True):
                st.markdown(f"**{number}. {stage}**")
                st.caption(description)
    st.caption(
        "AI interpretation (stage 4) is grounded in verified evidence that Python has already "
        "retrieved and calculated. It must not invent financial information — it selects and "
        "explains the cited findings, nothing more."
    )

    st.subheader("Industries covered")
    for column, (sector_name, names) in zip(st.columns(3), SECTORS.items()):
        with column:
            with st.container(border=True):
                st.markdown(f"**{sector_name}**")
                covered = [ticker for ticker in names if ticker in companies]
                st.caption(", ".join(f"{names[ticker]} ({ticker})" for ticker in covered) or
                          "No companies currently loaded for this sector.")

    st.subheader("Who it is designed for")
    st.write(
        "Corporate strategy teams, management consultants, financial analysts, business "
        "intelligence professionals, executives, and industry researchers."
    )

    st.subheader("Data sources")
    st.write(
        "The platform uses [SEC EDGAR](https://www.sec.gov/edgar) filings and official company "
        "disclosures, transparent Python calculations, and optional local Ollama AI analysis. "
        "Data availability and reporting definitions can vary by company — see "
        "[SEC developer resources](https://www.sec.gov/about/developer-resources) and the "
        "Data Sources & Methodology page for details."
    )
    st.button("Open Data Sources & Methodology", on_click=go_to_page, args=("Data Sources & Methodology",))


elif page == "Financial Performance":
    st.caption(
        "Company-level financial performance: revenue, profitability, margins and historical "
        "trends, drawn from verified SEC filings. Use this page to see how a company's reported "
        "results have moved over time and how it stacks up against its selected peers."
    )

    st.subheader("Executive financial dashboard")
    if not tickers:
        st.info("Select companies in the sidebar to populate the dashboard.")
    for column, ticker in zip(st.columns(max(len(tickers), 1)), tickers):
        with column:
            with st.container(border=True):
                st.markdown(f"**{label(ticker).split(' · ')[0]}**")
                st.caption(f"{'Primary' if ticker == primary else 'Peer'} · {evidence.industries.get(ticker) or 'Industry unavailable'}")
                data = current[ticker]
                prior = evidence.financial.get((ticker, year - 1), {})
                revenue = data.get("Revenue")
                net_income = data.get("Net income attributable to parent")
                operating_income = data.get("Operating income")
                growth = change(revenue, prior.get("Revenue"))
                op_margin = margin(operating_income, revenue)
                net_margin_value = margin(net_income, revenue)
                sector_metric = headline_operating_metric(ticker)
                st.metric("Quarterly revenue", money(revenue), percent(growth) if growth is not None else None)
                st.metric("Net income", money(net_income))
                st.metric("Operating margin", percent(op_margin) if op_margin is not None else missing)
                st.metric("Net margin", percent(net_margin_value) if net_margin_value is not None else missing)
                if sector_metric:
                    st.metric(sector_metric.name, format_metric_value(sector_metric))
                for item in (revenue, net_income, operating_income, sector_metric):
                    if item:
                        st.caption(f"{item.name} · {item.start or 'As of'} – {item.end} · [Source]({item.url}) · Filed or published {item.published}")
                if not revenue:
                    st.caption(no_financial)

    sector_values = [(ticker, item) for ticker in tickers if (item := headline_operating_metric(ticker))]
    if len(sector_values) >= 2:
        first_ticker, first_item = sector_values[0]
        if all(comparable_peers(first_item, item, evidence.industries.get(first_ticker), evidence.industries.get(ticker))
              for ticker, item in sector_values[1:]):
            st.caption(f"{first_item.name} · current quarter")
            series_chart([{"Ticker": ticker, first_item.unit: item.value} for ticker, item in sector_values],
                        "Ticker", first_item.unit)

    st.subheader("Financial Performance Insights")
    overview_observations, overview_limitations = executive_evidence(
        evidence, tickers, year, "Compare growth and profitability across selected companies")
    growth_obs, net_obs = overview_observations.get("growth"), overview_observations.get("net")
    if growth_obs and net_obs and growth_obs.leader and net_obs.leader:
        growth_name, net_name = label(growth_obs.leader).split(" · ")[0], label(net_obs.leader).split(" · ")[0]
        if growth_obs.leader != net_obs.leader:
            st.markdown(
                f"- **{growth_name}** has the strongest reported revenue growth this quarter; "
                f"**{net_name}** has the strongest reported profitability. The largest revenue figure "
                "is not automatically the strongest performer — growth and profitability are reported "
                "separately below and can favor different companies."
            )
        else:
            st.markdown(f"- **{growth_name}** leads on both reported revenue growth and profitability this quarter.")
    priority = ("growth", "operating", "net", "demand", "capacity", "cash", "liquidity", "leverage")
    insight_keys = [key for key in priority if key in overview_observations][:5]
    if not insight_keys:
        st.info("No verified comparative evidence is available yet for the current selection.")
    for key in insight_keys:
        observation = overview_observations[key]
        references = []
        for fact in observation.sources:
            ref = f"[{fact.ticker}, {fact.start or 'as of'} to {fact.end}]({fact.url})"
            if ref not in references:
                references.append(ref)
        st.markdown(f"- {observation.text.replace('$', chr(92) + '$')} " + " · ".join(references))
    for limitation in overview_limitations[:3]:
        st.caption(limitation)

    st.divider()
    st.subheader("Detailed financial analysis")
    mode = display_mode
    percent_metric = metric.endswith(("margin", "YoY", "QoQ"))
    label = "%" if percent_metric else "USD millions"
    rows, points, inputs = [], {ticker: {} for ticker in tickers}, {}
    for ticker in tickers:
        for selected_year in years:
            value, items, chart_facts = financial_display(ticker, selected_year, metric, mode)
            for item in filter(None, items):
                inputs[ticker, item.name, item.end] = asdict(item)
            period = next((item for item in items if item), None)
            display_label = "YoY growth (%)" if mode == "Year-over-year growth" else label
            rows.append({"Ticker": ticker, "Year": selected_year,
                         "Start": period.start if period else None, "End": period.end if period else None, display_label: round(value, 3) if value is not None else None,
                         "Status": ("Calculated" if percent_metric or mode == "Year-over-year growth"
                                    or (items[0] and "difference" in items[0].definition.lower())
                                    or metric == "Long-term debt including current maturities" else "Reported")
                                   if value is not None else
                         f"{missing} · source, matching period, or eligible denominator missing"})
            points[ticker][selected_year] = (value, chart_facts)
    indexed = mode == "Index (base 100)"
    chart_label = "Index" if indexed else ("YoY growth (%)" if mode == "Year-over-year growth" else label)
    cohorts = list(trend_cohorts(points, evidence.industries, indexed=indexed))
    if not cohorts:
        st.info("No verified observations support this chart and display mode.")
    for base, cohort in cohorts:
        names = list(dict.fromkeys(label_for["Ticker"] for label_for in cohort))
        if len(names) == 1 and len(tickers) > 1:
            st.caption(f"{names[0]} shown separately because comparable peer inputs are unavailable.")
        if base is not None:
            st.caption(f"Common positive baseline · {base} = 100")
        series_chart([{**row, chart_label: row["Value"]} for row in cohort], "Year", chart_label, "line")
    if indexed:
        st.caption("The table retains the source values used to calculate the index.")
    show_table(rows)
    if metric.endswith("margin"):
        st.caption("Margin = income ÷ reported revenue × 100.")
    elif metric.endswith(("YoY", "QoQ")):
        st.caption("Growth = (current value ÷ comparable prior value − 1) × 100.")
    if inputs:
        with st.expander("Calculation inputs and source lineage"):
            show_table(list(inputs.values()))
    st.caption("Peer series share a chart only when their available dates, units and accounting concepts align.")
    if {"CHH", "WH"} <= set(tickers) and metric == "Revenue":
        st.caption("Choice reports total revenues including reimbursable costs. Wyndham reports net revenues "
                   "under a different accounting concept. Use net income for the aligned comparison.")

elif page == "Competitive Benchmarking":
    st.caption(
        "Side-by-side comparison on one financial or industry-specific operating measure at a "
        "time, so you can see exactly where a company leads or lags its selected peers this quarter."
    )
    values = {ticker: benchmark_values[ticker] for ticker in tickers}
    observed = [item for item in values.values() if item is not None]
    rows = [{"Company": label(ticker), "Metric": metric, "Value": format_metric_value(item),
            "Reporting period": f"{item.start or 'As of'} to {item.end}", "URL": item.url,
            "Comparability": benchmark_status(item, observed, evidence.industries).split(" · ")[0]}
           for ticker, item in values.items() if item is not None]
    if not rows:
        st.info(f"{missing}. No company in the current selection reports {metric} for this period.")
    else:
        show_table(rows)
        comparability_notes = sorted({benchmark_status(item, observed, evidence.industries) for item in observed}
                                     - {"Comparable on available definitions, units and dates"})
        for note in comparability_notes:
            st.caption(note)
    comparable = [item for item in observed
                 if benchmark_status(item, observed, evidence.industries).startswith("Comparable")]
    if len(comparable) >= 2:
        series_chart([{"Ticker": item.ticker, observed[0].unit: item.value} for item in comparable],
                     "Ticker", comparable[0].unit)

        st.subheader("Competitive intelligence summary")
        ranked = sorted(comparable, key=lambda item: item.value, reverse=True)
        leader, runner_up = ranked[0], ranked[1]
        gap = leader.value - runner_up.value
        gap_pct = f" ({100 * gap / abs(runner_up.value):+.1f}%)" if runner_up.value else ""
        st.markdown(
            f"- **Leader:** {label(leader.ticker).split(' · ')[0]} reports {format_metric_value(leader)} on "
            f"{metric} for {leader.start or 'as of'} to {leader.end}. [Source]({leader.url})"
        )
        st.markdown(
            f"- **Gap:** {format_number(gap, leader.unit)} ahead of {label(runner_up.ticker).split(' · ')[0]}'s "
            f"{format_metric_value(runner_up)}{gap_pct} on the same measure."
        )
        st.markdown(
            f"- **Possible implication:** a gap of this size on {metric.lower()} is worth checking against the "
            "underlying business model and cost structure before treating it as a sustainable advantage."
        )
        if len(comparable) < len(observed):
            excluded = [item.ticker for item in observed if item not in comparable]
            st.caption(f"Excluded from this ranking as not comparable on {metric}: {', '.join(excluded)}. "
                      "See the Comparability column above for the reason.")
        st.caption("Rankings use the reported figure as published; they do not adjust for business mix, "
                  "scale, or one-off items.")
    elif observed:
        st.caption(f"Only one comparable figure is available for {metric} this period, so no ranking is shown.")
    if observed:
        with st.expander("Definitions and retrieval dates"):
            show_table([{"Company": label(item.ticker), "Definition": item.definition,
                         "Retrieved": item.retrieved, "URL": item.url} for item in observed])

elif page == "Strategic Signals":
    st.caption(
        "Issuer commentary pulled directly from earnings releases and filings — expansion, "
        "investments, acquisitions, risks, and management priorities — read for what it implies, "
        "not just listed as disclosures."
    )
    category = signal_category
    implications = {
        "Development": "Signals where the company is committing capital and management attention for future growth.",
        "Transactions": "Changes the competitive landscape and the company's own portfolio or cost base.",
        "Distribution and loyalty": "Affects customer acquisition cost and pricing power relative to competitors.",
        "Demand and risk": "Indicates how exposed near-term results may be to geography, segment or channel shifts.",
        "Fleet and capacity": "Determines how much capacity is coming online and what it will cost to finance.",
        "Financial risks": "Flags exposure that could move margins independent of underlying demand.",
        "Management priorities": "Shows what leadership says it will act on next, ahead of results confirming it.",
    }
    considerations = {
        "Development": "Review capacity commitments, opening timing and capital needs.",
        "Transactions": "Review deal scope, integration needs and the economics of the transaction.",
        "Distribution and loyalty": "Assess customer acquisition, retention and distribution costs.",
        "Demand and risk": "Test exposure by geography, customer segment and booking window.",
        "Fleet and capacity": "Review delivery schedules, utilization and financing commitments.",
        "Financial risks": "Test sensitivity to financing, fuel and exchange-rate changes.",
        "Management priorities": "Track stated priorities against subsequent investment and results.",
    }
    signals = sorted(((ticker, *signal) for ticker in tickers for signal in evidence.signals.get(ticker, [])
                      if category == "All" or category == signal[0]), key=lambda row: row[4], reverse=True)
    for ticker, kind, snippet, url, published, retrieved in signals:
        with st.container(border=True):
            st.subheader(f"{label(ticker).split(' · ')[0]} · {kind}")
            st.markdown(f"**Strategic development:** {kind} disclosed by the issuer this quarter.")
            st.markdown(f"**Supporting evidence (reported fact):** “{snippet}”")
            st.caption(f"Issuer statement · {published} · Retrieved {retrieved} · [Open disclosure]({url})")
            st.markdown(f"**Business implication (interpretation):** {implications[kind]}")
            st.markdown(f"**Potential management consideration:** {considerations[kind]}")
    if not signals:
        st.info(f"{missing}. No matched issuer commentary for the selected scope.")
    st.caption("Supporting evidence is the issuer's own reported statement. Implications and considerations are "
              "this platform's interpretation, not confirmed outcomes — evidence from multiple periods is needed "
              "to establish an actual change in strategy.")

elif page == "Scenario Simulator":
    st.caption(
        "Test business assumptions and evaluate hypothetical outcomes on top of a company's "
        "reported revenue baseline. Useful for stress-testing growth, margin, and sector-specific "
        "assumptions before relying on them."
    )
    st.info("Hypothetical sensitivity analysis. Assumptions are user inputs. Outputs are modeled and do not represent company guidance.")
    ticker = st.selectbox("Revenue baseline", tickers)
    baseline = current[ticker].get("Revenue")
    if baseline:
        st.metric("Reported baseline", money(baseline))
        st.caption(f"{baseline.start} to {baseline.end} · {baseline.definition} · [SEC filing]({baseline.url})")
    else:
        st.warning(f"{missing}. {no_financial}")
    rows = []
    for column, name, growth, assumed_margin in zip(st.columns(3), ("Downside", "Base", "Upside"),
                                                   (-5., 3., 8.), (12., 17., 22.)):
        with column:
            st.subheader(name)
            growth = st.number_input("Revenue change (%)", -100., 200., growth, key=f"{name}-growth")
            assumed_margin = st.number_input("Operating margin (%)", -100., 100., assumed_margin,
                                            key=f"{name}-margin")
            output = scenario(baseline.value if baseline else None, growth, assumed_margin)
            if output:
                rows.append({"Ticker": ticker, "Scenario": name, "Modeled revenue ($m)": output[0] / 1e6,
                             "Modeled operating income ($m)": output[1] / 1e6})
    if rows:
        show_table(rows)
    st.caption("Modeled revenue = baseline × (1 + growth). Modeled operating income = modeled revenue × assumed margin.")
    active_sector = evidence.industries.get(ticker)
    if active_sector in DRIVERS:
        st.subheader(f"{active_sector} operating sensitivity")
        st.caption("Hypothetical model. Revenue scales with volume and revenue per unit. Baseline operating costs "
                   "are held constant except for the cost input shown below.")
        driver_columns = st.columns(3)
        assumptions = [column.number_input(name, -100.0, 200.0, 0.0, key=f"sector-{active_sector}-{index}")
                       for index, (column, name) in enumerate(zip(driver_columns, DRIVERS[active_sector]))]
        income = current[ticker].get("Operating income")
        fuel = operating.get(ticker, {}).get("Fuel expense")
        if active_sector == "Aviation" and (not fuel or not baseline or
                (fuel.start, fuel.end, fuel.unit) != (baseline.start, baseline.end, baseline.unit)):
            st.info("Aviation sensitivity requires fuel expense for the same reporting period as the baseline.")
        elif margin(income, baseline) is not None:
            output = sector_scenario(baseline.value, income.value, *assumptions,
                                     fuel=fuel.value if active_sector == "Aviation" else None)
            if output:
                show_table([{"Modeled revenue ($m)": output[0] / 1e6, "Modeled operating income ($m)": output[1] / 1e6,
                             "Modeled operating margin (%)": output[2]}])
                st.caption(f"Baseline operating income from [SEC filing]({income.url}).")
                if active_sector == "Aviation":
                    st.caption(f"Fuel cost baseline from [SEC filing]({fuel.url}).")
            else:
                st.info("The reported expense inputs do not support this sensitivity.")
        else:
            st.info("Reported revenue and operating income for the same period are required.")


elif page == "Data Sources & Methodology":
    st.caption(
        "Where every figure on this platform comes from and how it is calculated — so findings "
        "can be checked against the original filing, not just taken on faith."
    )
    retrieved = [item.retrieved for values in current.values() for item in values.values()]
    coverage_note = (f"Most recent SEC retrieval for the selected companies · {max(retrieved)}" if retrieved
                     else "No sources loaded yet for the current company selection — use Refresh in the sidebar.")
    st.caption(coverage_note)
    st.markdown("""
    **SEC EDGAR.** The SEC ticker directory and company search resolve ticker and CIK.
    Company Facts supplies US GAAP observations in USD. Submissions identifies original filings.
    The nearest calendar quarter end groups reporting periods for display. The source's actual quarterly dates and fiscal year
    boundaries determine calculations. Cash and debt use quarter-end balances. Cash flow and capital
    spending may be derived from consecutive fiscal year-to-date values. Year-end quarters may be derived
    from full-year and nine-month observations for the same accounting concept.

    **Issuer releases.** Validated Q3 2026 adapters supply selected operating data and issuer commentary. Historical filters
    apply to SEC facts. Operating coverage is release-specific. Adjusted earnings are excluded from rankings.

    **Comparability.** Two figures are shown on the same chart or ranked against each other only when their
    reported dates, units and underlying accounting concept all match (see `comparable_peers` in
    `competitive/financial.py`). Margins use reported revenue. Pipeline definitions and geographic
    footprints vary by issuer. Management commentary is attributed to the issuer. Latest matching filings
    can include restatements.

    **Missing data.** When a company does not report a given metric for the selected period, that company
    is simply excluded from that metric's table, chart or comparison — the platform never substitutes a
    different metric, estimates a value, or displays a misleading zero in its place. Where that happens,
    the page states "Data unavailable" or omits the company and explains why in the Data Coverage panel.

    **Role of the AI model.** The local Ollama model (when configured) never computes a figure and never
    generates financial data from its own knowledge. Python always retrieves, calculates and cites the
    evidence first; the model's only job is to select which of those already-cited findings best answers
    the question, order them, and choose the clearest wording for the implications. Every statement the
    model produces must trace back to a specific cited figure — unverifiable claims are rejected before
    they reach the page.

    **Limitations.** Coverage is deepest for the companies and periods explicitly validated in
    `competitive/industry.py` and `competitive/intelligence.py`; other SEC filers can still be analyzed
    through core financial statement data, but operating-metric and issuer-commentary coverage will be
    thinner. All comparisons describe a single reporting window and do not establish causation.
    """)
    observations = [asdict(item) for data in (*evidence.financial.values(), *operating.values())
                    for item in data.values()]
    if observations:
        show_table(observations)
    else:
        st.info(f"{missing}. Refresh sources to populate metric lineage.")
    st.subheader("SEC filings")
    if evidence.filings:
        show_table(evidence.filings)
    else:
        st.info(f"{missing}. No matching SEC submission records are cached.")
