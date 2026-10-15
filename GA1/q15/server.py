import json
import os
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Set LEDGER_API_ROOT in Render environment variables.
# It must be the questionData URL up to and including questionId=...
# Do NOT add path=/orders or path=/export to this value.
API_ROOT = os.getenv("LEDGER_API_ROOT", "").strip()
IST = ZoneInfo("Asia/Kolkata")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Acme-Ledger-Agent/1.0"})

app = FastAPI(title="Acme Appliances Ledger Agent", version="1.0.0")
ORDERS = []
RATES = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}
DATA_ERROR = None


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def api_url(path: str, page: int | None = None) -> str:
    if not API_ROOT:
        raise RuntimeError("LEDGER_API_ROOT environment variable is not configured")
    url = API_ROOT + ("&" if "?" in API_ROOT else "?") + "path=" + requests.utils.quote(path, safe="/")
    if page is not None:
        url += f"&page={page}"
    return url


def get_json(url: str):
    response = HTTP.get(url, timeout=8)
    response.raise_for_status()
    return response.json()


def parse_export(payload):
    # The export endpoint may return a JSON string containing one JSON object per line,
    # or a JSON object/array. Support all observed shapes.
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        try:
            nested = json.loads(text)
            if isinstance(nested, (dict, list)):
                return parse_export(nested)
            if isinstance(nested, str):
                text = nested
        except json.JSONDecodeError:
            pass
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("orders", "data", "results", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError("Unrecognized /export response format")


def load_all_orders():
    # Prefer the export endpoint: it avoids making 85 sequential page requests.
    try:
        payload = get_json(api_url("/export"))
        rows = parse_export(payload)
        if rows:
            return rows
    except Exception:
        pass

    # Fallback: discover pagination from the orders endpoint.
    first = get_json(api_url("/orders", 1))
    if isinstance(first, list):
        return first
    if not isinstance(first, dict):
        raise ValueError("Unexpected /orders response")
    rows = list(first.get("orders", first.get("data", [])))
    pages = int(first.get("pages", 1))
    next_url = first.get("next")
    if next_url:
        # Use the API-provided next link while present.
        seen = {1}
        while next_url and len(seen) < 1000:
            response = HTTP.get(next_url, timeout=8)
            response.raise_for_status()
            page_data = response.json()
            if not isinstance(page_data, dict):
                break
            rows.extend(page_data.get("orders", page_data.get("data", [])))
            next_url = page_data.get("next")
            seen.add(int(page_data.get("page", len(seen) + 1)))
    else:
        for page in range(2, pages + 1):
            page_data = get_json(api_url("/orders", page))
            if isinstance(page_data, dict):
                rows.extend(page_data.get("orders", page_data.get("data", [])))
            elif isinstance(page_data, list):
                rows.extend(page_data)
    return rows


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except (ValueError, TypeError):
        return None


def refresh_data():
    global ORDERS, RATES, DATA_ERROR
    try:
        raw = load_all_orders()
        # Keep only the most recently updated row for each order ID.
        latest = {}
        for row in raw:
            oid = str(row.get("id", ""))
            if not oid:
                continue
            updated = parse_dt(row.get("updated_at")) or datetime.min.replace(tzinfo=IST)
            old = latest.get(oid)
            old_updated = parse_dt(old.get("updated_at")) if old else None
            if old is None or old_updated is None or updated >= old_updated:
                latest[oid] = row
        ORDERS = list(latest.values())

        try:
            rate_data = get_json(api_url("/rates"))
            if isinstance(rate_data, dict):
                rates = rate_data.get("usd_per_unit", {})
                if isinstance(rates, dict):
                    RATES = {str(k).upper(): float(v) for k, v in rates.items()}
        except Exception:
            # Use the rates already provided by the service description as a fallback.
            RATES = {"USD": 1.0, "EUR": 1.1, "INR": 0.01214}

        DATA_ERROR = None
    except Exception as exc:
        DATA_ERROR = str(exc)


@app.on_event("startup")
def startup():
    refresh_data()


@app.get("/")
def root():
    return {"service": "Acme Appliances Ledger Agent", "status": "ok", "endpoint": "POST /answer"}


@app.get("/health")
def health():
    return {
        "ok": DATA_ERROR is None and bool(ORDERS),
        "orders_loaded": len(ORDERS),
        "error": DATA_ERROR,
    }


def usd(row):
    currency = str(row.get("currency", "USD")).upper()
    rate = RATES.get(currency)
    if rate is None:
        raise ValueError(f"Missing USD conversion rate for {currency}")
    return float(row.get("amount", 0) or 0) * rate


def order_date(row):
    return parse_dt(row.get("created_at"))


def extract_period(q):
    """Return inclusive start/exclusive end in Asia/Kolkata, if a period is present."""
    text = q.lower()
    month_names = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    for name, month in month_names.items():
        match = re.search(rf"\b{name}\s+(20\d{{2}})\b", text)
        if match:
            year = int(match.group(1))
            start = datetime(year, month, 1, tzinfo=IST)
            end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=IST)
            return start, end
    # Numeric dates such as 2026-03 or March 2026 already handled above.
    match = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])\b", text)
    if match:
        year, month = map(int, match.groups())
        start = datetime(year, month, 1, tzinfo=IST)
        end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=IST)
        return start, end
    match = re.search(r"\b(?:in|during|for)\s+(?:the\s+)?(?:year\s+)?(20\d{2})\b", text)
    if match:
        year = int(match.group(1))
        return datetime(year, 1, 1, tzinfo=IST), datetime(year + 1, 1, 1, tzinfo=IST)
    return None, None


def answer_question(question: str):
    if not ORDERS:
        raise RuntimeError(DATA_ERROR or "Ledger data is not loaded")

    q = re.sub(r"\s+", " ", question.strip().lower())
    start, end = extract_period(q)

    rows = ORDERS
    # Filter by creation/business date, interpreted in Asia/Kolkata.
    if start:
        rows = [r for r in rows if (d := order_date(r)) is not None and start <= d < end]

    regions = ("north", "south", "east", "west", "central")
    region = next((r.title() for r in regions if re.search(rf"\b{r}\b", q)), None)
    if region:
        rows = [r for r in rows if str(r.get("region", "")).lower() == region.lower()]

    products = sorted({str(r.get("product", "")) for r in ORDERS}, key=len, reverse=True)
    product = next((p for p in products if p.lower() in q), None)
    if product:
        rows = [r for r in rows if str(r.get("product", "")).lower() == product.lower()]

    customer_match = re.search(r"\bC\d{4}\b", question, re.I)
    customer = customer_match.group(0).upper() if customer_match else None
    if customer:
        rows = [r for r in rows if str(r.get("customer", "")).upper() == customer]

    status_filter = None
    if re.search(r"\b(refund|refunded|refunds|refunding)\b", q):
        status_filter = "refunded"
    elif re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        status_filter = "void"
    elif re.search(r"\b(paid|sales|sold|revenue|sales revenue|turnover)\b", q):
        status_filter = "paid"

    if status_filter:
        rows = [r for r in rows if str(r.get("status", "")).lower() == status_filter]

    # Customer/order/product questions.
    asks_count = bool(re.search(r"\b(how many|number of|count of|count|quantity of orders|number)\b", q))
    asks_customers = bool(re.search(r"\b(customers?|clients?)\b", q))
    asks_orders = bool(re.search(r"\borders?\b", q))
    asks_units = bool(re.search(r"\b(units?|items?)\b", q))
    asks_average = bool(re.search(r"\b(average|avg|mean)\b", q))
    asks_top = bool(re.search(r"\b(top|highest|most|best|largest|biggest|leading|maximum|max)\b", q))
    asks_lowest = bool(re.search(r"\b(lowest|least|smallest|minimum|min)\b", q))
    asks_which = bool(re.search(r"\b(which|what)\b", q))
    asks_refunds = status_filter == "refunded" or "refund" in q
    asks_revenue = bool(re.search(r"\b(revenue|sales|turnover|spend|spent|generated|income|value|amount|total)\b", q))
    asks_money = asks_revenue or asks_refunds or bool(re.search(r"\b(usd|dollars?|money)\b", q))

    if asks_top or asks_lowest:
        # Rank products or customers according to revenue by default, or units/count when asked.
        group_field = "customer" if asks_customers or customer else "product"
        metric = "qty" if asks_units else ("count" if asks_count and asks_orders else "money")
        groups = {}
        for row in rows:
            key = str(row.get(group_field, ""))
            if not key:
                continue
            if metric == "qty":
                value = float(row.get("qty", 0) or 0) if row.get("status") == "paid" else 0
            elif metric == "count":
                value = 1
            else:
                # For revenue ranking use paid rows; for refund ranking use refunded rows.
                wanted = "refunded" if asks_refunds else "paid"
                value = usd(row) if str(row.get("status", "")).lower() == wanted else 0
            groups[key] = groups.get(key, 0) + value
        if not groups:
            return 0
        chosen = min(groups, key=groups.get) if asks_lowest else max(groups, key=groups.get)
        # "Which product/customer had the highest..." asks for the label, not its metric.
        if asks_which and not re.search(r"\\b(how much|what amount|what value|how many dollars)\\b", q):
            return chosen
        # "Which product/customer had the highest..." asks for the label, not its metric.
        if asks_which and not re.search(r"\b(how much|what amount|what value|how many dollars)\b", q):
            return chosen
        # If the question asks for the value of the top item, return the value; otherwise its name.
        if asks_money or asks_units or asks_count:
            value = groups[chosen]
            return round(value, 2) if metric == "money" else int(value) if float(value).is_integer() else round(value, 2)
        return chosen

    if asks_customers and asks_count:
        return len({str(r.get("customer", "")) for r in rows if r.get("customer")})

    if asks_count and (asks_orders or asks_refunds):
        return len(rows)

    if asks_units:
        selected = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        total = sum(float(r.get("qty", 0) or 0) for r in selected)
        return int(total) if total.is_integer() else round(total, 2)

    if asks_average:
        selected = [r for r in rows if str(r.get("status", "")).lower() == "paid"]
        if not selected:
            return 0
        return round(sum(usd(r) for r in selected) / len(selected), 2)

    if asks_money:
        wanted = "refunded" if asks_refunds else "paid"
        selected = [r for r in rows if str(r.get("status", "")).lower() == wanted]
        return round(sum(usd(r) for r in selected), 2)

    if asks_customers:
        return len({str(r.get("customer", "")) for r in rows if r.get("customer")})

    # Default to the number of matching records only when wording clearly asks for a count.
    if asks_count:
        return len(rows)

    raise ValueError("Could not determine the requested metric from the question")


@app.post("/answer")
def answer(payload: QuestionRequest):
    try:
        result = answer_question(payload.question)
        return {"answer": result}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/")
def answer_root(payload: QuestionRequest):
    return answer(payload)


@app.post("/refresh")
def refresh():
    refresh_data()
    if DATA_ERROR:
        raise HTTPException(status_code=502, detail=DATA_ERROR)
    return {"ok": True, "orders_loaded": len(ORDERS)}
