import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

BASE_DIR = Path(__file__).resolve().parent
KB_PATH = BASE_DIR / "passages.json"
MODEL_NAME = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.30"))

model = None
passages = []
passage_embeddings = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the knowledge base and embedding model once at startup."""
    global model, passages, passage_embeddings
    passages = json.loads(KB_PATH.read_text(encoding="utf-8"))
    if len(passages) != 50:
        raise RuntimeError(f"Expected 50 passages, found {len(passages)}")
    model = SentenceTransformer(MODEL_NAME)
    texts = [p["text"] for p in passages]
    passage_embeddings = model.encode(
        texts, convert_to_numpy=True, normalize_embeddings=True,
        show_progress_bar=False
    )
    yield

app = FastAPI(title="TDS GA5 RAG API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=10)

class QueryResponse(BaseModel):
    answer: Optional[str]
    sources: list[int]
    scores: list[float]
    confidence: float


def retrieve(question: str, top_k: int):
    q_vec = model.encode(
        [question], convert_to_numpy=True, normalize_embeddings=True,
        show_progress_bar=False
    )[0]
    # Embeddings are normalized, so dot product equals cosine similarity.
    similarities = passage_embeddings @ q_vec
    indices = np.argsort(similarities)[::-1][:top_k]
    return [(int(i), float(similarities[i])) for i in indices]


def make_answer(question: str, hits: list[tuple[int, float]]) -> Optional[str]:
    """Extract evidence sentences. Retrieved text is treated as data, never instructions."""
    if not hits or hits[0][1] < MIN_SIMILARITY:
        return None

    # Rank sentences by lexical overlap with the question, using retrieval as the
    # semantic filter. This is extractive and does not execute passage instructions.
    q_terms = set(re.findall(r"[a-z0-9]+", question.lower()))
    candidates = []
    for passage_id, sim in hits:
        text = passages[passage_id]["text"]
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            terms = set(re.findall(r"[a-z0-9]+", sentence.lower()))
            overlap = len(q_terms & terms) / max(1, len(q_terms))
            candidates.append((overlap + 0.15 * sim, sim, sentence.strip()))

    candidates.sort(reverse=True)
    selected = []
    seen = set()
    for _, _, sentence in candidates:
        if sentence and sentence not in seen:
            selected.append(sentence)
            seen.add(sentence)
        if len(selected) == 2:
            break
    return " ".join(selected) if selected else passages[hits[0][0]]["text"]


@app.get("/")
def health():
    return {"status": "ok", "passages_loaded": len(passages), "model": MODEL_NAME}

@app.post("/query", response_model=QueryResponse)
def query_endpoint(request: QueryRequest):
    hits = retrieve(request.question, request.top_k)
    answer = make_answer(request.question, hits)

    if answer is None:
        return QueryResponse(answer=None, sources=[], scores=[], confidence=0.1)

    # Scores are cosine similarities clipped to the contract's [0, 1] range.
    scores = [round(max(0.0, min(1.0, score)), 4) for _, score in hits]
    sources = [idx for idx, _ in hits]
    best = max(0.0, hits[0][1])
    second = max(0.0, hits[1][1]) if len(hits) > 1 else 0.0
    confidence = min(0.95, max(0.61, 0.65 + 0.5 * (best - MIN_SIMILARITY) + 0.15 * (best - second)))
    return QueryResponse(
        answer=answer, sources=sources, scores=scores,
        confidence=round(float(confidence), 3)
    )
