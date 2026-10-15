import json
import os
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ledger-agent")

API_ROOT = os.getenv("LEDGER_API_ROOT", "").strip().rstrip("&?")
IST = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Acme-Ledger-Agent/1.0"})

app = FastAPI(title="Acme Appliances Ledger Agent", version="2.0.0")

ORDERS: list[dict[str, Any]] = []
RATES: dict[str, float] = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}
DATA_ERROR: str | None = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def api_url(path: str, page: int | None = None) -> str:
    if not API_ROOT:
        raise RuntimeError("LEDGER_API_ROOT environment variable is not configured")
    # API_ROOT must contain email, quizSign and questionId, but no path parameter.
    from urllib.parse import urlencode
    params = {"path": path}
    if page is not None:
        params["page"] = page
    return API_ROOT + "&" + urlencode(params, safe="/")


def get_json(url: str) -> Any:
    response = SESSION.get(url, timeout=(4, 8))
    response.raise_for_status()
    return response.json()


def normalize_rows(payload: Any) -> list[dict[str, Any]]:
    """Parse an /export payload that may be JSON, a list, or NDJSON."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("orders", "data", "results", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        # Some APIs wrap NDJSON in a named string field.
        for key in ("export", "content", "text", "data"):
            value = payload.get(key)
            if isinstance(value, str):
                return normalize_rows(value)
        return []
    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return []
        try:
            decoded = json.loads(raw)
            if decoded is not payload:
                return normalize_rows(decoded)
        except (json.JSONDecodeError, TypeError):
            pass
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except json.JSONDecodeError:
                # CSV-like or unexpected text should not silently become valid data.
                continue
        return rows
    return []


def load_all_orders() -> list[dict[str, Any]]:
    errors = []

    # Try the bulk export first; fall back to all pages if export isn't usable.
    try:
        rows = normalize_rows(get_json(api_url("/export")))
        if rows:
            log.info("Loaded %d orders from /export", len(rows))
            return rows
        errors.append("/export returned no parseable orders")
    except Exception as exc:
        errors.append(f"/export: {type(exc).__name__}: {exc}")
        log.warning("Bulk export failed: %s", errors[-1])

    first = get_json(api_url("/orders", 1))
    if isinstance(first, list):
        rows = [x for x in first if isinstance(x, dict)]
        if not rows:
            raise ValueError("The /orders endpoint returned an empty list")
        return rows
    if not isinstance(first, dict):
        raise ValueError(f"Unexpected /orders page 1 format: {type(first).__name__}")

    rows = normalize_rows(first.get("orders", first.get("data", [])))
    pages = int(first.get("pages", 1) or 1)

    # Prefer next links if provided; otherwise request each page explicitly.
    next_url = first.get("next")
    if next_url:
        seen_urls = {next_url}
        while next_url and len(seen_urls) <= 1000:
            response = SESSION.get(next_url, timeout=(4, 8))
            response.raise_for_status()
            page_data = response.json()
            if not isinstance(page_data, dict):
                break
            rows.extend(normalize_rows(page_data.get("orders", page_data.get("data", []))))
            next_url = page_data.get("next")
            if next_url in seen_urls:
                raise ValueError("Pagination next-link loop detected")
            if next_url:
                seen_urls.add(next_url)
    else:
        for page in range(2, pages + 1):
            page_data = get_json(api_url("/orders", page))
            if isinstance(page_data, dict):
                rows.extend(normalize_rows(page_data.get("orders", page_data.get("data", []))))
            elif isinstance(page_data, list):
                rows.extend(normalize_rows(page_data))

    if not rows:
        raise ValueError("No orders loaded. " + " | ".join(errors))
    log.info("Loaded %d raw orders from paginated endpoint", len(rows))
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

        # For duplicate IDs, retain the row with the latest updated_at.
        latest: dict[str, dict[str, Any]] = {}
        anonymous: list[dict[str, Any]] = []
        for row in raw:
            oid = str(row.get("id", "")).strip()
            if not oid:
                anonymous.append(row)
                continue
            current = latest.get(oid)
            new_dt = parse_dt(row.get("updated_at"))
            old_dt = parse_dt(current.get("updated_at")) if current else None
            if current is None or (new_dt is not None and (old_dt is None or new_dt >= old_dt)):
                latest[oid] = row
        ORDERS = list(latest.values()) + anonymous

        try:
            rate_data = get_json(api_url("/rates"))
            rates = rate_data.get("usd_per_unit", {}) if isinstance(rate_data, dict) else {}
            if isinstance(rates, dict) and rates:
                RATES = {str(k).upper(): float(v) for k, v in rates.items()}
            if "USD" not in RATES:
                RATES["USD"] = 1.0
        except Exception as exc:
            log.warning("Could not load /rates; using documented fallback rates: %s", exc)
            RATES = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}

        DATA_ERROR = None
        log.info("Ledger ready: %d unique orders", len(ORDERS))
    except Exception as exc:
        ORDERS = []
        DATA_ERROR = f"{type(exc).__name__}: {exc}"
        log.exception("Failed to load ledger data")


@app.on_event("startup")
def startup() -> None:
    refresh_data()


@app.get("/")
def root():
    return {
        "service": "Acme Appliances Ledger Agent",
        "status": "ok",
        "endpoint": "POST /answer",
        "health": "/health",
    }


@app.get("/health")
def health():
    return {
        "ok": DATA_ERROR is None and bool(ORDERS),
        "orders_loaded": len(ORDERS),
        "error": DATA_ERROR,
    }


def order_date(row: dict[str, Any]) -> datetime | None:
    return parse_dt(row.get("created_at"))


def usd(row: dict[str, Any]) -> float:
    currency = str(row.get("currency", "USD")).upper()
    if currency not in RATES:
        raise ValueError(f"Missing USD conversion rate for {currency}")
    return float(row.get("amount", 0) or 0) * RATES[currency]


def extract_period(question: str) -> tuple[datetime | None, datetime | None]:
    q = question.lower()
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    for name, month in months.items():
        match = re.search(rf"\b{name}\s+(20\d{{2}})\b", q)
        if match:
            year = int(match.group(1))
            start = datetime(year, month, 1, tzinfo=IST)
            end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=IST)
            return start, end

    match = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])\b", q)
    if match:
        year, month = map(int, match.groups())
        start = datetime(year, month, 1, tzinfo=IST)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=IST)
        return start, end

    # Specific dates: "on June 12, 2026" / "on 12 June 2026" / ISO date.
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", q)
    if match:
        year, month, day = map(int, match.groups())
        from datetime import timedelta
        start = datetime(year, month, day, tzinfo=IST)
        return start, start + timedelta(days=1)

    match = re.search(r"\b(\d{1,2})\s+([a-z]+)\s+(20\d{2})\b", q)
    if match:
        day, name, year = int(match.group(1)), match.group(2), int(match.group(3))
        month = months.get(name)
        if month:
            start = datetime(year, month, day, tzinfo=IST)
            from datetime import timedelta
            return start, start + timedelta(days=1)

    match = re.search(r"\b([a-z]+)\s+(\d{1,2}),?\s+(20\d{2})\b", q)
    if match:
        name, day, year = match.group(1), int(match.group(2)), int(match.group(3))
        month = months.get(name)
        if month:
            start = datetime(year, month, day, tzinfo=IST)
            from datetime import timedelta
            return start, start + timedelta(days=1)

    match = re.search(r"\b(?:in|during|for|throughout)\s+(?:the\s+)?(?:year\s+)?(20\d{2})\b", q)
    if match:
        year = int(match.group(1))
        return datetime(year, 1, 1, tzinfo=IST), datetime(year + 1, 1, 1, tzinfo=IST)
    return None, None


def answer_question(question: str) -> Any:
    if DATA_ERROR:
        raise RuntimeError("Ledger data unavailable: " + DATA_ERROR)
    if not ORDERS:
        raise RuntimeError("Ledger data is not loaded")

    q = re.sub(r"\s+", " ", question.strip().lower())
    start, end = extract_period(q)
    rows = list(ORDERS)

    if start and end:
        rows = [r for r in rows if (d := order_date(r)) is not None and start <= d < end]

    # Region filter
    regions = ("north", "south", "east", "west", "central")
    region = next((x.title() for x in regions if re.search(rf"\b{x}\b", q)), None)
    if region:
        rows = [r for r in rows if str(r.get("region", "")).strip().lower() == region.lower()]

    # Product names exactly as found in source data
    products = sorted(
        {str(r.get("product", "")).strip() for r in ORDERS if r.get("product")},
        key=len,
        reverse=True,
    )
    product = next((p for p in products if p.lower() in q), None)
    if product:
        rows = [r for r in rows if str(r.get("product", "")).strip().lower() == product.lower()]

    customer_match = re.search(r"\bC\d{4}\b", question, re.I)
    if customer_match:
        customer = customer_match.group(0).upper()
        rows = [r for r in rows if str(r.get("customer", "")).upper() == customer]

    # Status filter; "refund" questions count refunded orders/amount, not paid revenue.
    if re.search(r"\b(refund|refunded|refunds|refunding)\b", q):
        status_filter = "refunded"
    elif re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        status_filter = "void"
    elif re.search(r"\b(paid|revenue|sales|sold|turnover)\b", q):
        status_filter = "paid"
    else:
        status_filter = None
    if status_filter:
        rows = [r for r in rows if str(r.get("status", "")).lower() == status_filter]

    asks_count = bool(re.search(r"\b(count|how many|number of|number)\b", q))
    asks_customers = bool(re.search(r"\b(customers?|clients?)\b", q))
    asks_orders = bool(re.search(r"\borders?\b", q))
    asks_units = bool(re.search(r"\b(units?|items?)\b", q))
    asks_average = bool(re.search(r"\b(average|avg|mean)\b", q))
    asks_top = bool(re.search(r"\b(top|highest|most|best|largest|biggest|leading|maximum|max)\b", q))
    asks_lowest = bool(re.search(r"\b(lowest|least|smallest|minimum|min)\b", q))
    asks_which = bool(re.search(r"\b(which|what|who)\b", q))
    asks_refunds = bool(re.search(r"\b(refund|refunded|refunds|refunding)\b", q))
    asks_revenue = bool(re.search(r"\b(revenue|sales|turnover|spend|spent|generated|income|value|amount|total|worth)\b", q))
    asks_money = asks_revenue or asks_refunds or bool(re.search(r"\b(usd|dollars?|money)\b", q))

    # Ranking questions: aggregate by customer/product, using paid amounts unless refund is asked.
    if asks_top or asks_lowest:
        group_field = "customer" if asks_customers or customer_match else "product"
        if asks_units:
            metric = "qty"
        elif asks_count and asks_orders:
            metric = "count"
        else:
            metric = "money"
        groups: dict[str, float] = {}
        for row in rows:
            key = str(row.get(group_field, "")).strip()
            if not key:
                continue
            status = str(row.get("status", "")).lower()
            if metric == "qty":
                value = float(row.get("qty", 0) or 0) if status == "paid" else 0.0
            elif metric == "count":
                value = 1.0
            else:
                wanted = "refunded" if asks_refunds else "paid"
                value = usd(row) if status == wanted else 0.0
            groups[key] = groups.get(key, 0.0) + value
        if not groups:
            return 0
        chosen = min(groups, key=groups.get) if asks_lowest else max(groups, key=groups.get)
        if asks_which and not re.search(r"\b(how much|what amount|what value|how many dollars)\b", q):
            return chosen
        value = groups[chosen]
        return round(value, 2) if metric == "money" else int(value) if value.is_integer() else round(value, 2)

    if asks_customers and asks_count:
        return len({str(r.get("customer", "")).strip() for r in rows if r.get("customer")})

    if asks_count and (asks_orders or asks_refunds):
        return len(rows)

    if asks_units:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        total = sum(float(r.get("qty", 0) or 0) for r in paid)
        return int(total) if total.is_integer() else round(total, 2)

    if asks_average:
        paid = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        if not paid:
            return 0
        return round(sum(usd(r) for r in paid) / len(paid), 2)

    if asks_money:
        wanted = "refunded" if asks_refunds else "paid"
        selected = [r for r in rows if str(r.get("status", "")).lower() == wanted]
        return round(sum(usd(r) for r in selected), 2)

    if asks_customers:
        return len({str(r.get("customer", "")).strip() for r in rows if r.get("customer")})

    if asks_count:
        return len(rows)

    # If a question asks for a product/customer without explicit count/amount, return matching unique values.
    if asks_which and re.search(r"\b(product|item)\b", q):
        return sorted({str(r.get("product", "")) for r in rows if r.get("product")})
    if asks_which and asks_customers:
        return sorted({str(r.get("customer", "")) for r in rows if r.get("customer")})

    raise ValueError("Could not determine the requested metric from the question")


@app.post("/answer")
def answer(payload: QuestionRequest):
    try:
        return {"answer": answer_question(payload.question)}
    except Exception as exc:
        log.exception("Question failed: %s", payload.question)
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/")
def answer_root(payload: QuestionRequest):
    return answer(payload)


@app.post("/reload")
def reload_data():
    """Optional manual refresh endpoint."""
    refresh_data()
    if DATA_ERROR:
        raise HTTPException(status_code=503, detail=DATA_ERROR)
    return {"ok": True, "orders_loaded": len(ORDERS)}
