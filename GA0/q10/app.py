
from pathlib import Path
import csv

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

CSV_FILE = Path(__file__).parent / "students.csv"


def load_students():
    with CSV_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        students = []

        for row in reader:
            students.append({
                "studentId": int(row["studentId"]),
                "class": row["class"],
            })

        return students


@app.get("/api")
def get_students(class_filter: list[str] | None = Query(
    default=None, alias="class"
)):
    students = load_students()

    if class_filter:
        selected_classes = set(class_filter)
        students = [
            student for student in students
            if student["class"] in selected_classes
        ]

    return {"students": students}
