import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse, parse_qs

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ledger-agent")

API_ROOT = os.getenv("LEDGER_API_ROOT", "").strip().rstrip("&?")
IST = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Acme-Ledger-Agent/4.0"})

app = FastAPI(title="Acme Appliances Ledger Agent", version="4.0.0")
ORDERS: list[dict[str, Any]] = []
RATES: dict[str, float] = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}
DATA_ERROR: str | None = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def root_api_url() -> str:
    if not API_ROOT:
        raise RuntimeError("LEDGER_API_ROOT environment variable is not configured")
    return API_ROOT


def api_url(path: str, page: int | None = None) -> str:
    base = root_api_url()
    # Remove any accidental path/page query values from base, preserving auth params.
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base)
    query = parse_qs(parts.query, keep_blank_values=True)
    query.pop("path", None)
    query.pop("page", None)
    query_items = []
    for key, values in query.items():
        for value in values:
            query_items.append((key, value))
    query_items.append(("path", path))
    if page is not None:
        query_items.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items, safe="/"), parts.fragment))


def get_json(url: str) -> Any:
    response = SESSION.get(url, timeout=(5, 12))
    response.raise_for_status()
    return response.json()


def rows_from(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "data", "results", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def resolve_path(root: dict[str, Any], name: str) -> str:
    links = root.get("links", {})
    value = links.get(name) if isinstance(links, dict) else None
    if not value:
        raise ValueError(f"API root response has no links.{name}")
    # Some links are relative paths such as /orders&page=1; the API's `path`
    # parameter should receive that complete path string.
    if isinstance(value, dict):
        value = value.get("href") or value.get("url") or value.get("path")
    if not isinstance(value, str) or not value:
        raise ValueError(f"Unrecognized links.{name} format")
    return value


def api_url_for_link(link_path: str, page: int | None = None) -> str:
    # `links.orders` can look like "/orders&page=1"; split its first path from
    # any embedded query-like pagination and normalize to path=/orders&page=1.
    clean = link_path.strip()
    if clean.startswith("http://") or clean.startswith("https://"):
        # Absolute links may be direct endpoint URLs, so use them as-is.
        if page is None:
            return clean
        separator = "&" if "?" in clean else "?"
        return clean + separator + f"page={page}"

    # The discovery API encodes parameters inside the path string, e.g. /orders&page=1.
    # Pass it as the `path` parameter; for explicit page N replace the initial page.
    if page is not None:
        clean = re.sub(r"([&?])page=\d+", rf"\g<1>page={page}", clean)
        if not re.search(r"[&?]page=\d+", clean):
            clean += ("&" if "?" in clean or "&" in clean else "&") + f"page={page}"
    return api_url(clean)


def load_all_orders() -> list[dict[str, Any]]:
    root = get_json(root_api_url())
    if not isinstance(root, dict):
        raise ValueError(f"API root returned {type(root).__name__}, expected object")
    order_link = resolve_path(root, "orders")
    first = get_json(api_url_for_link(order_link))
    rows = rows_from(first)

    if isinstance(first, dict) and not rows and isinstance(first.get("links"), dict):
        raise ValueError("Orders link returned another discovery response instead of order rows")

    if isinstance(first, list):
        if rows:
            return rows
        raise ValueError("Orders endpoint returned an empty list")

    if not isinstance(first, dict):
        raise ValueError(f"Unexpected orders response type: {type(first).__name__}")

    pages = int(first.get("pages", 1) or 1)
    next_url = first.get("next")
    log.info("Orders first page: %d rows; pages=%d; has_next=%s", len(rows), pages, bool(next_url))

    if next_url:
        seen = set()
        while next_url:
            if next_url in seen:
                raise ValueError("Pagination loop detected in orders next links")
            seen.add(next_url)
            response = SESSION.get(next_url, timeout=(5, 12))
            response.raise_for_status()
            page_data = response.json()
            if not isinstance(page_data, dict):
                raise ValueError("Unexpected response while following orders next link")
            page_rows = rows_from(page_data)
            rows.extend(page_rows)
            log.info("Loaded orders page %s: %d rows", page_data.get("page", "?"), len(page_rows))
            next_url = page_data.get("next")
    else:
        for page in range(2, pages + 1):
            page_data = get_json(api_url_for_link(order_link, page))
            page_rows = rows_from(page_data)
            rows.extend(page_rows)
            log.info("Loaded orders page %d: %d rows", page, len(page_rows))

    if not rows:
        keys = list(first.keys()) if isinstance(first, dict) else []
        raise ValueError(f"Orders endpoint returned zero rows; response keys={keys}")
    return rows


def load_rates() -> dict[str, float]:
    try:
        root = get_json(root_api_url())
        link = resolve_path(root, "rates")
        payload = get_json(api_url_for_link(link))
        if isinstance(payload, dict) and isinstance(payload.get("usd_per_unit"), dict):
            rates = {str(k).upper(): float(v) for k, v in payload["usd_per_unit"].items()}
            rates.setdefault("USD", 1.0)
            return rates
    except Exception:
        log.exception("Rates fetch failed; using fallback rates")
    return {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except (ValueError, TypeError, OverflowError):
        return None


def refresh_data() -> None:
    global ORDERS, RATES, DATA_ERROR
    try:
        raw = load_all_orders()
        if not raw:
            raise ValueError("API returned zero orders")

        latest: dict[str, dict[str, Any]] = {}
        no_id = []
        for row in raw:
            oid = str(row.get("id", "")).strip()
            if not oid:
                no_id.append(row)
                continue
            new_dt = parse_dt(row.get("updated_at"))
            old = latest.get(oid)
            old_dt = parse_dt(old.get("updated_at")) if old else None
            if old is None or (new_dt is not None and (old_dt is None or new_dt >= old_dt)):
                latest[oid] = row
        ORDERS = list(latest.values()) + no_id
        RATES = load_rates()
        DATA_ERROR = None
        log.info("Ledger loaded: raw=%d unique=%d", len(raw), len(ORDERS))
    except Exception as exc:
        ORDERS = []
        DATA_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Ledger load failed")


@app.on_event("startup")
def startup():
    refresh_data()


@app.get("/")
def root():
    return {"service": "Acme Appliances Ledger Agent", "status": "ok", "endpoint": "POST /answer", "health": "/health"}


@app.get("/health")
def health():
    return {"ok": DATA_ERROR is None and bool(ORDERS), "orders_loaded": len(ORDERS), "error": DATA_ERROR}


def usd(row: dict[str, Any]) -> float:
    currency = str(row.get("currency", "USD")).upper()
    if currency not in RATES:
        raise ValueError(f"Missing USD conversion rate for {currency}")
    return float(row.get("amount", 0) or 0) * RATES[currency]


def extract_period(q: str):
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    }
    for name, month in months.items():
        m = re.search(rf"\b{name}\s+(20\d{{2}})\b", q)
        if m:
            y = int(m.group(1))
            return datetime(y, month, 1, tzinfo=IST), datetime(y + (month == 12), month % 12 + 1, 1, tzinfo=IST)
    m = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])\b", q)
    if m:
        y, mo = map(int, m.groups())
        return datetime(y, mo, 1, tzinfo=IST), datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=IST)
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", q)
    if m:
        y, mo, d = map(int, m.groups())
        start = datetime(y, mo, d, tzinfo=IST)
        return start, start + timedelta(days=1)
    m = re.search(r"\b(?:in|during|for|throughout)\s+(?:the\s+)?(?:year\s+)?(20\d{2})\b", q)
    if m:
        y = int(m.group(1))
        return datetime(y, 1, 1, tzinfo=IST), datetime(y + 1, 1, 1, tzinfo=IST)
    return None, None


def answer_question(question: str):
    if DATA_ERROR:
        raise RuntimeError("Ledger data unavailable: " + DATA_ERROR)
    if not ORDERS:
        raise RuntimeError("Ledger data is not loaded")

    q = re.sub(r"\s+", " ", question.strip().lower())
    start, end = extract_period(q)
    rows = list(ORDERS)
    if start and end:
        rows = [r for r in rows if (d := parse_dt(r.get("created_at"))) is not None and start <= d < end]

    regions = ("north", "south", "east", "west", "central")
    region = next((x.title() for x in regions if re.search(rf"\b{x}\b", q)), None)
    if region:
        rows = [r for r in rows if str(r.get("region", "")).lower() == region.lower()]

    def normalize_text(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

    products = sorted(
        {str(r.get("product", "")).strip() for r in ORDERS if r.get("product")},
        key=len,
        reverse=True,
    )
    normalized_question = normalize_text(q)
    product = next(
        (p for p in products if normalize_text(p) and
         re.search(rf"\b{re.escape(normalize_text(p))}\b", normalized_question)),
        None,
    )
    if product:
        normalized_product = normalize_text(product)
        rows = [
            r for r in rows
            if normalize_text(r.get("product", "")) == normalized_product
        ]

    cm = re.search(r"\bC\d{4}\b", question, re.I)
    if cm:
        customer = cm.group(0).upper()
        rows = [r for r in rows if str(r.get("customer", "")).upper() == customer]

    refunds = bool(re.search(r"\b(refund|refunded|refunds|refunding)\b", q))
    if refunds:
        status = "refunded"
    elif re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        status = "void"
    elif re.search(r"\b(paid|revenue|sales|sold|turnover)\b", q):
        status = "paid"
    else:
        status = None
    if status:
        rows = [r for r in rows if str(r.get("status", "")).lower() == status]

    count = bool(re.search(r"\b(count|how many|number of|number)\b", q))
    customers = bool(re.search(r"\b(customers?|clients?)\b", q))
    orders = bool(re.search(r"\borders?\b", q))
    units = bool(re.search(r"\b(units?|items?)\b", q))
    average = bool(re.search(r"\b(average|avg|mean)\b", q))
    top = bool(re.search(r"\b(top|highest|most|best|largest|biggest|leading|maximum|max)\b", q))
    lowest = bool(re.search(r"\b(lowest|least|smallest|minimum|min)\b", q))
    money = bool(re.search(r"\b(revenue|sales|turnover|spend|spent|generated|income|value|amount|total|worth|usd|dollars?|money)\b", q)) or refunds

    if top or lowest:
        field = "customer" if customers or cm else "product"
        metric = "qty" if units else ("count" if count and orders else "money")
        grouped: dict[str, float] = {}
        for row in rows:
            key = str(row.get(field, "")).strip()
            if not key:
                continue
            st = str(row.get("status", "")).lower()
            if metric == "qty":
                val = float(row.get("qty", 0) or 0) if st == "paid" else 0.0
            elif metric == "count":
                val = 1.0
            else:
                wanted = "refunded" if refunds else "paid"
                val = usd(row) if st == wanted else 0.0
            grouped[key] = grouped.get(key, 0.0) + val
        if not grouped:
            return 0
        chosen = min(grouped, key=grouped.get) if lowest else max(grouped, key=grouped.get)
        if re.search(r"\b(which|what|who)\b", q) and not re.search(r"\b(how much|what amount|what value|how many dollars)\b", q):
            return chosen
        val = grouped[chosen]
        return round(val, 2) if metric == "money" else int(val) if val.is_integer() else round(val, 2)

    # Distinct customers who placed at least one paid order for a specified product.
    if customers and count and product and re.search(r"\bpaid\b", q):
        paid_product_orders = [
            r for r in rows
            if normalize_text(r.get("product", "")) == normalize_text(product)
            and str(r.get("status", "")).strip().lower() == "paid"
            and str(r.get("customer", "")).strip()
        ]
        return len({str(r.get("customer", "")).strip() for r in paid_product_orders})

    if customers and count:
        return len({str(r.get("customer", "")).strip() for r in rows if r.get("customer")})
    if count and (orders or refunds):
        return len(rows)
    if units:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        total = sum(float(r.get("qty", 0) or 0) for r in paid)
        return int(total) if total.is_integer() else round(total, 2)
    if average:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        return round(sum(usd(r) for r in paid) / len(paid), 2) if paid else 0
    if money:
        wanted = "refunded" if refunds else "paid"
        return round(sum(usd(r) for r in rows if str(r.get("status", "")).lower() == wanted), 2)
    if customers:
        return len({str(r.get("customer", "")).strip() for r in rows if r.get("customer")})
    if count:
        return len(rows)
    raise ValueError("Could not determine the requested metric from the question")


@app.post("/answer")
def answer(payload: QuestionRequest):
    try:
        return {"answer": answer_question(payload.question)}
    except Exception as exc:
        log.exception("Question failed")
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/")
def answer_root(payload: QuestionRequest):
    return answer(payload)
