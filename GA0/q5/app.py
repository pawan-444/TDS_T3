import os
import sys
import traceback
from io import StringIO
from typing import List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from openai import OpenAI


app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CodeRequest(BaseModel):
    code: str


class ErrorAnalysis(BaseModel):
    error_lines: List[int]


def execute_python_code(code: str) -> dict:
    """
    Execute Python code and return the exact execution output.
    """
    old_stdout = sys.stdout
    old_stderr = sys.stderr

    stdout = StringIO()
    stderr = StringIO()

    sys.stdout = stdout
    sys.stderr = stderr

    try:
        exec(code, {})

        return {
            "success": True,
            "output": stdout.getvalue()
        }

    except Exception:
        return {
            "success": False,
            "output": traceback.format_exc()
        }

    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def analyze_error_with_ai(code: str, error_output: str) -> List[int]:
    """
    Ask the LLM to identify the exact source-code line(s)
    responsible for the error.
    """

    client = OpenAI(
        api_key=os.environ["AIPIPE_TOKEN"],
        base_url="https://aipipe.org/openrouter/v1"
    )

    prompt = f"""
You are a Python error-analysis system.

Identify the exact source-code line number(s) where the error occurred.

Return ONLY JSON in this format:
{{"error_lines": [3]}}

Rules:
- Use the line numbers from the provided Python code and traceback.
- Do not invent line numbers.
- Return an empty list only if no source line can be identified.

PYTHON CODE:
{code}

TRACEBACK:
{error_output}
"""

    response = client.chat.completions.create(
        model="google/gemini-2.0-flash-lite-001",
        messages=[
            {
                "role": "system",
                "content": "You identify Python error line numbers accurately."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "error_analysis",
                "schema": {
                    "type": "object",
                    "properties": {
                        "error_lines": {
                            "type": "array",
                            "items": {
                                "type": "integer"
                            }
                        }
                    },
                    "required": ["error_lines"],
                    "additionalProperties": False
                }
            }
        }
    )

    return ErrorAnalysis.model_validate_json(
        response.choices[0].message.content
    ).error_lines


@app.post("/code-interpreter")
def code_interpreter(request: CodeRequest):
    execution = execute_python_code(request.code)

    # No AI call for successful execution
    if execution["success"]:
        return {
            "error": [],
            "result": execution["output"]
        }

    # AI is called only when execution fails
    error_lines = analyze_error_with_ai(
        request.code,
        execution["output"]
    )

    return {
        "error": error_lines,
        "result": execution["output"]
    }