from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

APP_DIR = Path.home() / ".local" / "share" / "kalliopen-launcher"
BACKUP_DIR = APP_DIR / "backups"
STATE_FILE = APP_DIR / "state.json"
APP_IMAGE = "ghcr.io/kalliopen/kalliopen:latest"
MIGRATOR_IMAGE = "ghcr.io/kalliopen/kalliopen-migrator:latest"
APP_PORT = 80
COMPOSE_FILE = APP_DIR / "compose.yml"

AUTOMATIC_BACKUP_DEFAULTS = {
    "automatic_backups_enabled": False,
    "automatic_backup_interval_minutes": 60,
    "automatic_backup_retention_days": 30,
}


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private(path: Path, content: str) -> None:
    _ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def save(state: dict) -> None:
    _write_private(STATE_FILE, json.dumps(state, indent=2))


def ensure_state() -> dict:
    state = load()
    changed = False
    if "postgres_password" not in state:
        state["postgres_password"] = secrets.token_urlsafe(24)
        changed = True
    for key, value in AUTOMATIC_BACKUP_DEFAULTS.items():
        if key not in state:
            state[key] = value
            changed = True
    if changed:
        save(state)
    else:
        _ensure_private_directory(APP_DIR)
        STATE_FILE.chmod(0o600)
    return state


def ensure_backup_directory() -> None:
    _ensure_private_directory(BACKUP_DIR)


def compose_text(password: str, origin: str) -> str:
    return f'''name: kalliopen
x-app-environment: &app-environment
  DATABASE_URL: postgresql://kalliopen:{password}@db:5432/kalliopen
  ORIGIN: "{origin}"
  SESSION_DURATION_DAYS: "30"
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
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kalliopen -d kalliopen"]
      interval: 5s
      timeout: 5s
      retries: 12
  app:
    image: {APP_IMAGE}
    restart: "no"
    depends_on:
      db:
        condition: service_healthy
      migrator:
        condition: service_completed_successfully
    environment: *app-environment
    ports:
      - "80:3000"
  migrator:
    image: {MIGRATOR_IMAGE}
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://kalliopen:{password}@db:5432/kalliopen
    restart: "no"
volumes:
  pgdata:
'''


def write_compose(state: dict, origin: str) -> None:
    _write_private(COMPOSE_FILE, compose_text(state["postgres_password"], origin))
