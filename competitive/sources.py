"""Cached SEC and issuer disclosure retrieval."""
from base64 import b64encode
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

COMPANIES = {
    "MAR": ("Marriott International", "0001048286"),
    "HLT": ("Hilton Worldwide", "0001585689"),
    "H": ("Hyatt Hotels Corporation", "0001468174"),
}
RELEASES = {
    "CHH": ("2026-11-02", "https://www.prnewswire.com/news-releases/choice-hotels-international-reports-third-quarter-2026-results-302925856.html"),
    "WH": ("2026-10-21", "https://investor.wyndhamhotels.com/news-events/press-releases/detail/432/wyndham-hotels-resorts-reports-strong-third-quarter"),

    "MAR": ("2026-11-02", "https://marriott.gcs-web.com/news-releases/news-release-details/marriott-international-reports-third-quarter-2026-results"),
    "HLT": ("2026-10-27", "https://ir.hilton.com/news-events/investor-news/detail/419/hilton-reports-third-quarter-results"),
    "H": ("2026-10-29", "https://investors.hyatt.com/news/investor-news/news-details/2026/Hyatt-Reports-Third-Quarter-2026-Results/default.aspx"),
}
RELEASE_ALTERNATES = {
    "MAR": "https://www.prnewswire.com/news-releases/marriott-international-reports-third-quarter-2026-results-302925186.html",
    "H": "https://newsroom.hyatt.com/Q3-2026-Earnings-Results",
}
_lock = threading.Lock()
_next_request = 0.0


class SourceError(Exception):
    def __init__(self, message, status="Refresh failed"):
        super().__init__(message)
        self.status = status


class Cache:
    def __init__(self, path, identity):
        self.path = path
        self.identity = identity
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS responses "
                       "(url TEXT PRIMARY KEY, body TEXT, retrieved TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS fetch_failures (url TEXT PRIMARY KEY, message TEXT)")

    def get(self, url, refresh=False, *, offline=False):
        global _next_request
        with sqlite3.connect(self.path) as db:
            cached = db.execute("SELECT body, retrieved FROM responses WHERE url=?", (url,)).fetchone()
            failure = db.execute("SELECT message FROM fetch_failures WHERE url=?", (url,)).fetchone()
        if cached and (offline or not refresh and
                       (datetime.now(timezone.utc) - datetime.fromisoformat(cached[1])).total_seconds() < 86400):
            return *cached, f"Using cached response after retrieval failure: {failure[0]}" if failure else None
        if offline:
            if failure:
                raise SourceError(failure[0])
            raise SourceError("Use Refresh financial and disclosure sources to load this source.", "Source not loaded")
        else:
            try:
                if url.startswith(("https://data.sec.gov/", "https://www.sec.gov/", "https://efts.sec.gov/")) and not re.search(r"\S+@\S+\.\S+", self.identity):
                    raise SourceError("Set SEC_USER_AGENT to your organization and contact email.")
                request = Request(url, headers={"User-Agent": self.identity or "HotelStrategyResearch/1.0"})
                with _lock:
                    time.sleep(max(0, _next_request - time.monotonic()))
                    try:
                        with urlopen(request, timeout=10) as response:
                            raw = response.read(30_000_001)
                    finally:
                        _next_request = time.monotonic() + 0.5
                if len(raw) > 30_000_000:
                    raise SourceError("Response exceeded the size limit.")
                body = b64encode(raw).decode("ascii") if url.endswith(".pdf") else raw.decode("utf-8")
                if url.endswith(".json") and not isinstance(json.loads(body), dict):
                    raise SourceError("Unexpected SEC response.")
                retrieved = datetime.now(timezone.utc).isoformat()
                with sqlite3.connect(self.path) as db:
                    db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (url, body, retrieved))
                    db.execute("DELETE FROM fetch_failures WHERE url=?", (url,))
                return body, retrieved, None
            except (OSError, ValueError, SourceError) as exc:
                error = str(exc)
                with sqlite3.connect(self.path) as db:
                    db.execute("INSERT OR REPLACE INTO fetch_failures VALUES (?, ?)", (url, error))
        if cached:
            return *cached, f"Using cached response after retrieval failure: {error}"
        raise SourceError(error)


def sec_urls(ticker, cik=None):
    cik = cik or COMPANIES[ticker][1]
    return (f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            f"https://data.sec.gov/submissions/CIK{cik}.json")


def filing_url(ticker, accession, document="", cik=None):
    cik = int(cik or COMPANIES[ticker][1])
    root = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
    return f"{root}/{document or accession + '-index.htm'}"


DIRECTORY_URL = "https://www.sec.gov/files/company_tickers.json"


def company_directory(cache, refresh=False, *, fetch=False):
    body, retrieved, warning = cache.get(DIRECTORY_URL, refresh, offline=not (refresh or fetch))
    companies = {}
    for item in json.loads(body).values():
        ticker = item["ticker"].strip().upper()
        cik = int(item["cik_str"])
        if ticker and 0 < cik < 10**10:
            companies[ticker] = (item["title"].strip(), f"{cik:010d}")
    return companies, retrieved, warning


def search_companies(companies, query):
    terms = query.casefold().split()
    return sorted(
        (ticker for ticker, (name, _) in companies.items()
         if all(term in f"{ticker} {name}".casefold() for term in terms)),
        key=lambda ticker: (ticker.casefold() != query.strip().casefold(), ticker),
    )


def cached_companies(cache):
    """Recover searchable listed issuers from cached official Submissions."""
    with sqlite3.connect(cache.path) as db:
        rows = db.execute("SELECT body FROM responses WHERE url LIKE ?",
                          ("https://data.sec.gov/submissions/CIK%.json",))
        companies = {}
        for (body,) in rows:
            issuer = json.loads(body)
            if issuer.get("cik") and issuer.get("name"):
                for ticker in issuer.get("tickers", []):
                    companies[ticker] = (issuer["name"], f"{int(issuer['cik']):010d}")
    return companies


def select_issuers(companies, primary, competitors):
    selected = []
    seen = set()
    for ticker in (primary, *competitors):
        if ticker in companies and companies[ticker][1] not in seen:
            selected.append(ticker)
            seen.add(companies[ticker][1])
        if len(selected) == 4:
            break
    return selected


def sec_search_url(query):
    return "https://efts.sec.gov/LATEST/search-index?" + urlencode({"keysTyped": query.strip()})


def search_sec(cache, query, fetch=False):
    body, _, warning = cache.get(sec_search_url(query), offline=not fetch)
    companies = {}
    for hit in json.loads(body).get("hits", {}).get("hits", []):
        entry = hit.get("_source", {})
        cik = str(hit.get("_id", ""))
        if not cik.isdecimal() or not 0 < int(cik) < 10**10:
            continue
        tickers = entry.get("tickers", "")
        if not isinstance(tickers, str):
            continue
        name = re.sub(r" \([^()]+\)$", "", entry.get("entity", ""))
        for ticker in re.split(r"[\s,]+", tickers.upper()):
            if name and re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]*", ticker):
                companies[ticker] = (name, f"{int(cik):010d}")
    return companies, warning
