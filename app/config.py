from __future__ import annotations

import json
import secrets
from pathlib import Path

APP_DIR = Path.home() / ".local" / "share" / "kalliopen-launcher"
BACKUP_DIR = APP_DIR / "backups"
STATE_FILE = APP_DIR / "state.json"
APP_IMAGE = "ghcr.io/kalliopen/kalliopen:latest"
COMPOSE_FILE = APP_DIR / "compose.yml"


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def ensure_state() -> dict:
    state = load()
    if "postgres_password" not in state:
        state["postgres_password"] = secrets.token_urlsafe(24)
        save(state)
    return state


def compose_text(password: str) -> str:
    return f'''name: kalliopen
services:
  db:
    image: postgres:18-alpine
    restart: always
    ports:
      - "127.0.0.1:5432:5432"
    environment:
      POSTGRES_USER: kalliopen
      POSTGRES_PASSWORD: {password}
      POSTGRES_DB: kalliopen
    volumes:
      - pgdata:/var/lib/postgresql
  app:
    image: {APP_IMAGE}
    restart: always
    depends_on:
      - db
    ports:
      - "80:3000"
    environment:
      DATABASE_URL: postgresql://kalliopen:{password}@db:5432/kalliopen
volumes:
  pgdata:
'''


def write_compose(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    COMPOSE_FILE.write_text(compose_text(state["postgres_password"]))
