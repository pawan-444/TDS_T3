
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware


BASE_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "port": 8000,
    "workers": 1,
    "debug": False,
    "log_level": "info",
    "api_key": "default-secret-000",
}

app = FastAPI(title="TDS GA1 Configuration Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def coerce_value(key: str, value: Any) -> Any:
    """Convert values to the types expected by the assignment."""
    key = key.lower()

    if key in ("port", "workers"):
        return int(value)

    if key == "debug":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {
            "true", "1", "yes", "on"
        }

    return str(value)


def normalize_key(key: str) -> str:
    """Normalize configuration keys to lowercase."""
    return key.strip().lower()


def load_config() -> dict:
    # Layer 1: Hardcoded defaults
    config = DEFAULTS.copy()

    # Layer 2: Environment-specific YAML
    environment = os.getenv("APP_ENV", "development")
    yaml_path = BASE_DIR / f"config.{environment}.yaml"

    if yaml_path.exists():
        with yaml_path.open("r", encoding="utf-8") as file:
            yaml_config = yaml.safe_load(file) or {}

        for key, value in yaml_config.items():
            config[normalize_key(key)] = value

    # Layer 3: .env file
    env_path = BASE_DIR / ".env"
    file_env = dotenv_values(env_path)

    for raw_key, value in file_env.items():
        if value is None:
            continue

        key = raw_key.strip()

        # Special alias applies to the .env layer.
        if key == "NUM_WORKERS":
            key = "workers"
        elif key.startswith("APP_"):
            key = key[4:].lower()
        else:
            key = normalize_key(key)

        config[key] = value

    # Layer 4: OS environment variables with APP_ prefix
    for raw_key, value in os.environ.items():
        if raw_key.startswith("APP_"):
            key = raw_key[4:].lower()
            config[key] = value

    # Convert values after merging the configuration layers.
    config = {
        key: coerce_value(key, value)
        for key, value in config.items()
    }

    return config


@app.get("/effective-config")
def effective_config(
    set: list[str] = Query(default=[])
):
    config = load_config()

    # Highest-priority layer: query parameter overrides.
    for override in set:
        if "=" not in override:
            continue

        key, value = override.split("=", 1)
        key = normalize_key(key)

        if not key:
            continue

        config[key] = coerce_value(key, value)

    # Never expose the actual secret.
    if "api_key" in config:
        config["api_key"] = "****"

    return config


@app.get("/")
def health_check():
    return {"status": "ok"}
