
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SentimentRequest(BaseModel):
    sentences: list[str]


POSITIVE_PHRASES = [
    "love", "loved", "lovely", "excellent", "great", "good",
    "amazing", "awesome", "wonderful", "fantastic", "happy",
    "glad", "delighted", "enjoy", "enjoyed", "perfect",
    "best", "beautiful", "brilliant", "like", "helpful",
    "success", "successful", "thank", "thanks", "pleased",
    "recommend", "impressed", "positive", "excited", "win"
]

NEGATIVE_PHRASES = [
    "hate", "hated", "terrible", "awful", "bad", "worst",
    "horrible", "sad", "angry", "upset", "disappointed",
    "disappointing", "poor", "fail", "failed", "failure",
    "broken", "useless", "annoying", "boring", "wrong",
    "problem", "problems", "difficult", "frustrating",
    "frustrated", "unhappy", "regret", "painful", "negative",
    "dislike", "ugly", "waste", "never"
]

NEGATIONS = {
    "not", "no", "never", "hardly", "isn't", "wasn't",
    "don't", "doesn't", "didn't", "cannot", "can't"
}


def classify_sentiment(sentence: str) -> str:
    text = sentence.lower()
    words = re.findall(r"\b[\w']+\b", text)

    positive = 0
    negative = 0
    negate_next = False

    for word in words:
        if word in NEGATIONS:
            negate_next = True
            continue

        if word in POSITIVE_PHRASES:
            if negate_next:
                negative += 1
            else:
                positive += 1
            negate_next = False

        elif word in NEGATIVE_PHRASES:
            if negate_next:
                positive += 1
            else:
                negative += 1
            negate_next = False
        else:
            negate_next = False

    if positive > negative:
        return "happy"
    if negative > positive:
        return "sad"
    return "neutral"


@app.post("/sentiment")
def analyze_sentiment(request: SentimentRequest):
    results = [
        {
            "sentence": sentence,
            "sentiment": classify_sentiment(sentence)
        }
        for sentence in request.sentences
    ]

    return {"results": results}
