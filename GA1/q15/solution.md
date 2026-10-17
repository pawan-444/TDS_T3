# q15: The Ledger Agent — ELI15 walkthrough

This folder contains the FastAPI service (`server.py`) and its Render start instructions (`render.yaml`). The service reads the private order ledger from the URL supplied in the assignment and answers questions at `POST /answer`.

## 1. Understand the assignment in one sentence

The program receives a question such as “What was total paid revenue in the East?” It filters the order records, does the requested math, and returns JSON like `{"answer": 123.45}`.

## 2. Start at the API root and discover the data

The long URL in the assignment is the API root. Its `links` section points to the orders, currency rates, and export data. You can inspect the root in a terminal with `curl` using the exact URL from the assignment:

```bash
curl -sS 'PASTE_THE_COMPLETE_API_URL_FROM_THE_ASSIGNMENT'
```

The orders endpoint is paginated. Follow its `next` link until it is empty, or use its page count. Do not calculate from page 1 alone. The API also says that an order can appear more than once; keep the row with the newest `updated_at` for each `id`. Its notes say only `paid` orders count as revenue.

Use the API's `rates` link for USD conversion. Do not assume every order is already in dollars: convert each amount to USD using the rate for its currency before adding it.

## 3. See how `server.py` turns data into answers

At startup, `refresh_data()` loads all order pages and the currency rates. It removes older duplicate versions of an order. The `/health` route reports whether the ledger loaded. The `/answer` route accepts a JSON question and returns an `answer` field.

The `answer_question()` function uses simple word clues to understand the question. For example, it looks for a region, a product or customer ID, a time period, words such as `paid` or `refund`, and a metric such as `average`, `count`, or `revenue`.

## 4. Why the reported answer was 631

The question was:

> On average, how many US dollars is a paid East order worth?

The old parser saw `how many` and set the `count` flag. It also saw `order`, so it returned the number of matching paid East orders immediately. That count is **631**. The code never got as far as its average calculation.

I fixed the order of the metric checks in `server.py`: when the question asks for an average monetary amount, the program now calculates that average before using the generic “how many orders” count rule. The `631` was a count, not a dollar amount.

For the current ledger data, the paid East orders total **$177,289.33 USD** across **631** orders. The expected average is **$280.97 USD per order**.

## 5. Keep the signed API URL private

`server.py` reads the API root from the `LEDGER_API_ROOT` environment variable. In Render, open the service's **Environment** settings and set `LEDGER_API_ROOT` to the complete root URL from the assignment, including its signed query values and `path=/`. Keep this URL out of source code and public commits. The `render.yaml` file starts the app with Uvicorn; the environment variable supplies the private data URL.

If running locally in Bash, set the variable only in that terminal session:

```bash
export LEDGER_API_ROOT='PASTE_THE_COMPLETE_API_ROOT_HERE'
```

Then, from this `q15` folder, install the listed dependencies if needed and start the service:

```bash
python -m pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Open a second terminal and check that the data loaded:

```bash
curl -sS http://127.0.0.1:8000/health
```

A healthy result has `"ok": true` and a positive `orders_loaded` count.

## 6. Try the average question locally

Send the question to the app's `/answer` route:

```bash
curl -sS http://127.0.0.1:8000/answer \
  -H 'Content-Type: application/json' \
  -d '{"question":"On average, how many US dollars is a paid East order worth?"}'
```

For the current ledger data, the answer should be `280.97`.

## 7. Put the correction on the public service

The fix in your local `server.py` does not change an already-running Render service by itself. Commit and push the corrected code to the repository connected to Render, then wait for the deployment to finish. Check `/health` and send the same sample POST question to the public `/answer` URL. The grader needs the public HTTPS endpoint that accepts POST requests; use the `/answer` path if the form asks for the complete route.

## 8. What to check if an answer is still wrong

- Check `/health`; a missing `LEDGER_API_ROOT` or failed API fetch means the service has no ledger to query.
- Confirm all pages were loaded and duplicate IDs were reduced to their latest `updated_at` row.
- Confirm region and status filters run before the metric is calculated.
- Convert amounts using the API's rates before calculating USD totals or averages.
- For an average, divide the sum of matching order amounts by the number of matching orders. Do not return that number of orders as the answer.