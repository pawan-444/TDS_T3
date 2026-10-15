import os
import re
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ledger-agent")

API_ROOT = os.getenv("LEDGER_API_ROOT", "").strip().rstrip("&?")
IST = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Acme-Ledger-Agent/3.0"})

app = FastAPI(title="Acme Appliances Ledger Agent", version="3.0.0")
ORDERS: list[dict[str, Any]] = []
RATES: dict[str, float] = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}
DATA_ERROR: str | None = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def api_url(path: str, page: int | None = None) -> str:
    if not API_ROOT:
        raise RuntimeError("LEDGER_API_ROOT environment variable is not configured")
    # LEDGER_API_ROOT must end at questionId=..., with NO path parameter.
    params = {"path": path}
    if page is not None:
        params["page"] = page
    return API_ROOT + "&" + urlencode(params, safe="/")


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


def load_all_orders() -> list[dict[str, Any]]:
    # Deliberately use the documented/discovered paginated endpoint first.
    first_url = api_url("/orders", 1)
    first = get_json(first_url)
    rows = rows_from(first)
    if isinstance(first, list):
        if rows:
            return rows
        raise ValueError(f"/orders page 1 returned an empty list (type={type(first).__name__})")
    if not isinstance(first, dict):
        raise ValueError(f"Unexpected /orders page 1 response type: {type(first).__name__}")

    pages = int(first.get("pages", 1) or 1)
    next_url = first.get("next")
    log.info("Orders page 1 loaded: %d rows; pages=%d; has_next=%s", len(rows), pages, bool(next_url))

    if next_url:
        visited = set()
        while next_url:
            if next_url in visited:
                raise ValueError("Pagination loop detected in /orders next links")
            visited.add(next_url)
            response = SESSION.get(next_url, timeout=(5, 12))
            response.raise_for_status()
            page_data = response.json()
            if not isinstance(page_data, dict):
                raise ValueError("Unexpected response while following /orders next link")
            page_rows = rows_from(page_data)
            rows.extend(page_rows)
            log.info("Loaded page %s: %d rows", page_data.get("page", "?"), len(page_rows))
            next_url = page_data.get("next")
    else:
        for page in range(2, pages + 1):
            page_data = get_json(api_url("/orders", page))
            page_rows = rows_from(page_data)
            rows.extend(page_rows)
            log.info("Loaded page %d: %d rows", page, len(page_rows))

    if not rows:
        # Include structure only, never log the secret-bearing URL.
        keys = list(first.keys()) if isinstance(first, dict) else []
        raise ValueError(f"/orders returned zero rows; page 1 keys={keys}")
    return rows


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

        # Duplicate IDs: keep the latest updated_at record.
        latest: dict[str, dict[str, Any]] = {}
        no_id: list[dict[str, Any]] = []
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

        try:
            rates_payload = get_json(api_url("/rates"))
            if isinstance(rates_payload, dict):
                rate_map = rates_payload.get("usd_per_unit", {})
                if isinstance(rate_map, dict) and rate_map:
                    RATES = {str(k).upper(): float(v) for k, v in rate_map.items()}
                    RATES.setdefault("USD", 1.0)
        except Exception as exc:
            log.warning("Rates endpoint failed; using fallback rates: %s", type(exc).__name__)
            RATES = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}

        DATA_ERROR = None
        log.info("Ledger loaded successfully: raw=%d, retained=%d", len(raw), len(ORDERS))
    except Exception as exc:
        ORDERS = []
        DATA_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Ledger data load failed")


@app.on_event("startup")
def startup() -> None:
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


def order_date(row: dict[str, Any]) -> datetime | None:
    return parse_dt(row.get("created_at"))


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
        rows = [r for r in rows if (d := order_date(r)) is not None and start <= d < end]

    region_names = ("north", "south", "east", "west", "central")
    region = next((name.title() for name in region_names if re.search(rf"\b{name}\b", q)), None)
    if region:
        rows = [r for r in rows if str(r.get("region", "")).strip().lower() == region.lower()]

    products = sorted({str(r.get("product", "")).strip() for r in ORDERS if r.get("product")}, key=len, reverse=True)
    product = next((p for p in products if p.lower() in q), None)
    if product:
        rows = [r for r in rows if str(r.get("product", "")).strip().lower() == product.lower()]

    cm = re.search(r"\bC\d{4}\b", question, re.I)
    if cm:
        cust = cm.group(0).upper()
        rows = [r for r in rows if str(r.get("customer", "")).upper() == cust]

    asks_refund = bool(re.search(r"\b(refund|refunded|refunds|refunding)\b", q))
    if asks_refund:
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
    which = bool(re.search(r"\b(which|what|who)\b", q))
    money = bool(re.search(r"\b(revenue|sales|turnover|spend|spent|generated|income|value|amount|total|worth|usd|dollars?|money)\b", q)) or asks_refund

    if top or lowest:
        field = "customer" if customers or cm else "product"
        metric = "qty" if units else ("count" if count and orders else "money")
        groups: dict[str, float] = {}
        for row in rows:
            key = str(row.get(field, "")).strip()
            if not key:
                continue
            st = str(row.get("status", "")).lower()
            if metric == "qty":
                value = float(row.get("qty", 0) or 0) if st == "paid" else 0.0
            elif metric == "count":
                value = 1.0
            else:
                desired = "refunded" if asks_refund else "paid"
                value = usd(row) if st == desired else 0.0
            groups[key] = groups.get(key, 0.0) + value
        if not groups:
            return 0
        chosen = min(groups, key=groups.get) if lowest else max(groups, key=groups.get)
        if which and not re.search(r"\b(how much|what amount|what value|how many dollars)\b", q):
            return chosen
        value = groups[chosen]
        return round(value, 2) if metric == "money" else int(value) if value.is_integer() else round(value, 2)

    if customers and count:
        return len({str(r.get("customer", "")).strip() for r in rows if r.get("customer")})
    if count and (orders or asks_refund):
        return len(rows)
    if units:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        total = sum(float(r.get("qty", 0) or 0) for r in paid)
        return int(total) if total.is_integer() else round(total, 2)
    if average:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        return round(sum(usd(r) for r in paid) / len(paid), 2) if paid else 0
    if money:
        desired = "refunded" if asks_refund else "paid"
        return round(sum(usd(r) for r in rows if str(r.get("status", "")).lower() == desired), 2)
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
