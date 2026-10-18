
import time
import uuid

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI()

EMAIL = "23f2002204@ds.study.iitm.ac.in"
ALLOWED_ORIGIN = "https://dash-pe6nih.example.com"


@app.middleware("http")
async def request_and_cors_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = str(uuid.uuid4())
    origin = request.headers.get("origin")

    # Handle browser CORS preflight requests explicitly.
    if request.method == "OPTIONS" and request.url.path == "/stats":
        if origin != ALLOWED_ORIGIN:
            response = Response(status_code=403)
        else:
            response = Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Vary": "Origin",
                },
            )
    else:
        response = await call_next(request)

        # Add CORS permission only for the assigned origin.
        if origin == ALLOWED_ORIGIN:
            response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
            response.headers["Vary"] = "Origin"

    elapsed = time.perf_counter() - start_time
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time"] = f"{elapsed:.6f}"

    return response


@app.get("/stats")
async def stats(values: str = Query(...)):
    try:
        numbers = [int(value.strip()) for value in values.split(",")]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="values must be comma-separated integers",
        )

    if not numbers:
        raise HTTPException(status_code=400, detail="values cannot be empty")

    return {
        "email": EMAIL,
        "count": len(numbers),
        "sum": sum(numbers),
        "min": min(numbers),
        "max": max(numbers),
        "mean": sum(numbers) / len(numbers),
    }


@app.get("/")
async def root():
    return {"status": "ok"}
