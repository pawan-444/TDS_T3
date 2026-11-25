# TDS GA5 Q1 — RAG Colosseum

## Local setup

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

First startup downloads `sentence-transformers/all-MiniLM-L6-v2` and builds embeddings for the 50 passages. Keep the server running after startup.

## Test

Open `http://127.0.0.1:8000/docs` or run:

```bash
curl -X POST http://127.0.0.1:8000/query -H "Content-Type: application/json" -d '{"question":"What is the default port of Redis?","top_k":3}'
```

Health check: `GET http://127.0.0.1:8000/`

## Notes

- The passages are stored with their original 0–49 indices.
- Embeddings are generated once at startup and reused for requests.
- CORS is enabled for all origins.
- The answer stage is extractive and does not execute instructions found inside retrieved passages.
- `MIN_SIMILARITY` is a starting threshold, not a calibrated guarantee. Evaluate it against the actual 20-probe set and adjust only after observing results.
- This starter implementation is a baseline. Test semantic confounders, unanswerable questions, prompt injection, and concurrent requests before submission.

## Hugging Face Spaces

Create a new Space using the Docker SDK, then upload `app.py`, `passages.json`, `requirements.txt`, and `Dockerfile`. The service listens on port 7860. Wait for the build and model download to finish, then test `/` and `/query` on the Space URL.
