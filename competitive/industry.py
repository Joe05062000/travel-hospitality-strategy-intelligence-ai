"""Industry classification and verified disclosure adapters."""
from base64 import b64decode
from html.parser import HTMLParser
from io import BytesIO
import re

from .financial import Metric
from .sources import SourceError

# These releases have been reviewed for the September 2026 quarter. The
# financial pipeline does not depend on an issuer having a release adapter.
CRUISE_PERIODS = {
    "RCL": ("2026-07-01", "2026-09-30"),
    "CCL": ("2026-06-01", "2026-08-31"),
    "NCLH": ("2026-07-01", "2026-09-30"),
}

DISCLOSURES = {
    "RCL": ("2026-10-27", "https://www.rclinvestor.com/press-releases/release/?id=1860"),
    "CCL": ("2026-09-29", "https://www.carnivalcorp.com/wp-content/uploads/2026/03/2026-3Q-Earnings-Release-Final-Draft.pdf"),
    "NCLH": ("2026-10-29", "https://www.nclhltd.com/investors/news-events/press-releases/detail/835/norwegian-cruise-line-holdings-reports-third-quarter-2026"),
    "DAL": ("2026-10-09", "https://ir.delta.com/news/news-details/2026/Delta-Air-Lines-Announces-September-Quarter-2026-Financial-Results/default.aspx"),
    "UAL": ("2026-10-20", "https://united.mediaroom.com/2026-10-20-United-Delivers-Record-17-Revenue-Growth-in-Q3%2C-and-Expects-Record-Q4-Revenue-Growth%2C-Underpinned-by-Accelerating-Customer-Investments"),
    "AAL": ("2026-10-22", "https://www.nasdaq.com/press-release/american-sets-new-revenue-records-amid-historic-fuel-cost-pressure-2026-10-22"),
    "MSFT": ("2026-10-28", "https://www.microsoft.com/en-us/Investor/earnings/FY-2027-Q1/press-release-webcast"),
    "GOOGL": ("2026-10-28", "https://s206.q4cdn.com/479360582/files/doc_news/2026/Oct/28/attachments/2026q3-alphabet-earnings-release.pdf"),
    "AMZN": ("2026-10-29", "https://www.aboutamazon.com/news/company-news/amazon-earnings-q3-2026-report"),
}
AVIATION = ("Passenger revenue", "Available seat miles", "Passenger load factor", "Fuel expense", "Revenue passenger miles",
            "Passenger revenue per available seat mile", "Operating margin")
TECHNOLOGY = ("Capital expenditures", "Cloud revenue")
HOSPITALITY = ("Comparable hotel RevPAR YoY", "Net rooms YoY", "Pipeline rooms", "Occupancy", "Average daily rate")


def industry(sic, description=None):
    sic = str(sic or "")
    if sic.startswith("70"):
        return "Hospitality"
    if sic.startswith("45"):
        return "Aviation"
    if sic in {"4400", "4412", "4481", "4700", "4724", "4725"}:
        return "Travel & Tourism"
    if sic.startswith(("357", "367", "737")):
        return "Technology"
    return f"{description or 'Other'} (SIC {sic})" if sic else None


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append([cell for cell in self.row if cell])
            self.row = None
            self.cell = None


def table_value(rows, label):
    values = set()
    for row in rows:
        if not row or not re.fullmatch(label, row[0], re.I):
            continue
        numeric = [cell.replace(",", "").strip(" %$") for cell in row[1:]]
        first = next((float(cell) for cell in numeric if re.fullmatch(r"\d+(?:\.\d+)?", cell)), None)
        if first is not None:
            values.add(first)
    return values.pop() if len(values) == 1 else None


def exact_match(text, pattern):
    values = {float(value.replace(",", "")) for value in re.findall(pattern, text, re.I)}
    return values.pop() if len(values) == 1 else None


def rows_after(rows, heading, size):
    matches = [rows[i + 1:i + 1 + size] for i, row in enumerate(rows)
               if row and re.fullmatch(heading, row[0], re.I)]
    return matches[0] if len(matches) == 1 else []


def disclosure_key(ticker):
    return "GOOGL" if ticker == "GOOG" else ticker


def disclosure_body(url, body):
    if not url.endswith(".pdf"):
        return body
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(b64decode(body, validate=True)))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:12])
    except Exception as exc:
        raise SourceError(f"Unable to read issuer earnings PDF: {exc}") from exc


def release_metrics(ticker, body, retrieved):
    key = disclosure_key(ticker)
    published, url = DISCLOSURES[key]
    parser = TableRows()
    parser.feed(body)
    rows = parser.rows
    results = {}

    def add(name, value, unit, multiplier, definition):
        if value is not None:
            start, end = CRUISE_PERIODS.get(key, ("2026-07-01", "2026-09-30"))
            results[name] = Metric(ticker, name, start, end,
                                   value * multiplier, unit, url, published, retrieved, definition, url)

    if key in CRUISE_PERIODS:
        if key == "CCL":
            if "August 31, 2026" not in body:
                return {}
            add("Passenger cruise days", exact_match(body, r"Passenger Cruise Days[^\n]+?\(a\)\s+([\d.]+)"), "days", 1e6, "Passenger days, rounded to millions by Carnival")
            add("Cruise occupancy", exact_match(body, r"Occupancy percentage \(c\)\s+([\d.]+)"), "%", 1, "Passenger days per available lower berth day")
            add("Net yield", exact_match(body, r"Net yields \(per ALBD\) \$ ([\d.]+)"), "USD", 1, "Carnival adjusted gross margin per available lower berth day")
            add("Onboard revenue", exact_match(body, r"Onboard and other +([\d,]+) +[\d,]+ +[\d,]+ +[\d,]+\s*\nTotal revenues"), "USD", 1e6, "Carnival onboard and other revenue")
        else:
            if "2026" not in body or not re.search(r"September\s*(?:</?[^>]+>\s*)*30", body, re.I):
                return {}
            add("Passenger cruise days", table_value(rows, r"Passenger Cruise Days"), "days", 1, f"Passenger cruise days as defined by {key}")
            add("Cruise occupancy", table_value(rows, r"Occupancy(?: Percentage)?"), "%", 1, f"Cruise occupancy as defined by {key}")
            add("Net yield", table_value(rows, r"Net Yields?"), "USD", 1,
                "Royal Caribbean net revenue per available passenger cruise day" if key == "RCL"
                else "Norwegian net revenue per capacity day")
            revenue_rows = rows_after(rows, "Passenger ticket revenues" if key == "RCL" else "Passenger ticket", 1)
            add("Onboard revenue", table_value(revenue_rows, r"Onboard and other(?: revenues)?"),
                "USD", 1e6 if key == "RCL" else 1e3, "Issuer-reported onboard and other revenue")
    elif key in {"DAL", "UAL", "AAL"}:
        if not re.search(r"September\s*(?:</?[^>]+>\s*)*30", body, re.I) or "2026" not in body:
            return {}
        add("Passenger revenue", table_value(rows, r"passenger(?: revenue)?"),
            "USD", 1e6, "Consolidated passenger revenue; carrier ancillary and loyalty accounting may vary")
        stats = rows_after(rows, r"Consolidated Operating Statistics.*", 12) if key == "AAL" else rows
        asm = r"ASMs \(millions\).*" if key == "UAL" else r"available seat miles.*\(millions\).*"
        add("Available seat miles", table_value(stats, asm),
            "seat miles", 1e6, "Reported consolidated system capacity")
        load_factor = (table_value(rows_after(rows, r"Passenger load factor:.*", 1), "Consolidated")
                       if key == "UAL" else table_value(stats, r"passenger load factor(?:\s*[:(].*)?"))
        add("Passenger load factor", load_factor, "%", 1, "Reported consolidated passenger load factor")
        rpm = r"RPMs \(millions\).*" if key == "UAL" else r"revenue passenger miles.*\(millions\).*"
        add("Revenue passenger miles", table_value(stats, rpm), "passenger miles", 1e6,
            "Reported consolidated passenger traffic")
        passenger = results.get("Passenger revenue")
        capacity = results.get("Available seat miles")
        if passenger and capacity and capacity.value > 0:
            add("Passenger revenue per available seat mile", 100 * passenger.value / capacity.value,
                "US cents", 1, "Reported consolidated passenger revenue divided by consolidated seat miles")
    elif key == "MSFT" and "FY27 Q1" in body:
        segment = rows_after(rows, "SEGMENT RESULTS", 22)
        for name in ("Agents and Infra", "Devices and Consumer"):
            add(f"{name} segment revenue", table_value(rows_after(segment, name, 1), "Revenue"),
                "USD", 1e6, f"Microsoft FY27 Q1 {name} segment as recast by the issuer")
        add("Cloud revenue", exact_match(body, r"Microsoft Cloud revenue was \$([\d.]+) billion"),
            "USD", 1e9, "Microsoft Cloud aggregate spans segments and is broader than Azure")
    elif key == "AMZN" and "quarter ended September 30, 2026" in body:
        aws = exact_match(body, r"AWS segment sales increased \d+% year-over-year to \$([\d.]+) billion")
        add("AWS segment net sales", aws, "USD", 1e9, "AWS segment net sales rounded by the issuer")
        add("Cloud revenue", aws, "USD", 1e9, "AWS segment scope as defined by Amazon")
    elif key == "GOOGL" and "Alphabet Announces Third Quarter 2026 Results" in body:
        # The revenue table puts the two revenue rows together, with 2026 second.
        revenue = re.search(r"Google Services total[^\n]*\nGoogle Cloud[^\n]*", body)
        if revenue:
            for label, name in (("Google Cloud", "Cloud revenue"),
                                ("Google Services total", "Google Services segment revenue")):
                value = exact_match(revenue[0], rf"{label} +[\d,]+ +([\d,]+)")
                add(name, value, "USD", 1e6, f"{label} as defined by Alphabet")
    return results
