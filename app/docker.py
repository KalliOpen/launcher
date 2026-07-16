from __future__ import annotations

import shutil
import socket
import subprocess
import urllib.request
import json
import logging
from datetime import datetime
import time
import re
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


def available() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


def installed() -> bool:
    return shutil.which("docker") is not None


def install() -> None:
    logger.info("Installing Docker and Docker Compose")
    subprocess.run(["pkexec", "apt-get", "update"], check=True)
    subprocess.run(["pkexec", "apt-get", "install", "-y", "docker.io", "docker-compose-v2"], check=True)


def compose(*args: str, capture: bool = True, log: bool = True) -> subprocess.CompletedProcess[str]:
    config.write_compose(config.ensure_state())
    if log:
        logger.info("Running Docker Compose: %s", " ".join(args))
    return subprocess.run(["docker", "compose", "-f", str(config.COMPOSE_FILE), *args],
                          capture_output=capture, text=True, check=True)


def health() -> str:
    try:
        result = urllib.request.urlopen("http://localhost/api/trpc/health", timeout=3)
        return "running" if result.status == 200 and "ok" in result.read().decode().lower() else "starting"
    except Exception:
        try:
            state = compose("ps", "--status", "running", "-q", "app", log=False).stdout
            return "starting" if state.strip() else "stopped"
        except Exception:
            return "stopped"


def local_ip() -> str:
    try:
        # UDP connect selects the route without sending any network traffic.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "unavailable"


def export_database(destination: Path) -> None:
    logger.info("Exporting database to %s", destination)
    ensure_database_running()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            result = subprocess.run(
                ["docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "db",
                 "pg_dump", "-U", "kalliopen", "-Fc", "kalliopen"],
                stdout=output, stderr=subprocess.PIPE, text=True,
            )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "PostgreSQL database export failed.")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def automatic_backup_path(reason: str) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    return config.BACKUP_DIR / f"{reason}_{timestamp}.dump"


def ensure_database_running() -> None:
    compose("up", "-d", "db")
    command = ["docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "db", "pg_isready"]
    for _ in range(30):
        if subprocess.run(command, capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("PostgreSQL did not become ready within 30 seconds.")


def import_database(source: Path) -> None:
    logger.info("Importing database from %s", source)
    backup = automatic_backup_path("pre-import")
    export_database(backup)
    with source.open("rb") as data:
        subprocess.run(["docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "db",
                        "pg_restore", "-U", "kalliopen", "-d", "kalliopen", "--clean", "--if-exists"],
                       stdin=data, check=True)


def delete_database() -> None:
    logger.info("Deleting KalliOpen database schema")
    compose("exec", "-T", "db", "psql", "-U", "kalliopen", "-d", "kalliopen", "-v",
            "ON_ERROR_STOP=1", "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def migrate() -> None:
    logger.info("Running application database migrations")
    compose("exec", "-T", "app", "pnpm", "run", "db:migrate")


def update() -> None:
    logger.info("Pulling and applying KalliOpen application update")
    compose("pull", "app")
    compose("up", "-d", "app")
    migrate()


def image_versions() -> tuple[str | None, str | None, str | None, str | None]:
    """Return display tags and digests for the local and registry images."""
    try:
        output = subprocess.check_output(
            ["docker", "image", "inspect", config.APP_IMAGE, "--format", "{{json .}}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        image = json.loads(output)
        digests = image.get("RepoDigests") or []
        current = digests[0].rsplit("@", 1)[-1] if digests else None
        labels = image.get("Config", {}).get("Labels") or {}
        current_version = labels.get("org.opencontainers.image.version")
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        current = None
        current_version = None

    try:
        registry, repository, tag = _image_parts(config.APP_IMAGE)
        token = _registry_token(registry, repository)
        tags = _registry_tags(registry, repository, token)
        version_tags = [item for item in tags if _is_version_tag(item)]
        latest_version = max(version_tags, key=_version_key) if version_tags else tag
        latest = _manifest_digest(registry, repository, latest_version, token)
        configured_digest = _manifest_digest(registry, repository, tag, token)
        if current_version is None:
            current_version = latest_version if current == configured_digest else tag
    except (OSError, KeyError, ValueError):
        latest = None
        latest_version = None
    logger.info("Image versions resolved: current=%s latest=%s", current_version, latest_version)
    return current_version, latest_version, current, latest


def _registry_token(registry: str, repository: str) -> str:
    token_url = f"https://{registry}/token?service={registry}&scope=repository:{repository}:pull"
    with urllib.request.urlopen(token_url, timeout=5) as response:
        return json.loads(response.read())["token"]


def _registry_tags(registry: str, repository: str, token: str) -> list[str]:
    request = urllib.request.Request(
        f"https://{registry}/v2/{repository}/tags/list",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read()).get("tags", [])


def _manifest_digest(registry: str, repository: str, tag: str, token: str) -> str | None:
    request = urllib.request.Request(
        f"https://{registry}/v2/{repository}/manifests/{tag}",
        headers={
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json",
            "Authorization": f"Bearer {token}",
        },
        method="HEAD",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.headers.get("Docker-Content-Digest")


def _is_version_tag(tag: str) -> bool:
    return re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", tag) is not None


def _version_key(tag: str) -> tuple[int, int, int, int, str]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(.*)", tag)
    if not match:
        return (0, 0, 0, 0, tag)
    suffix = match.group(4)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if suffix else 1, suffix)


def _image_parts(image: str) -> tuple[str, str, str]:
    name, _, tag = image.rpartition(":")
    registry, repository = name.split("/", 1)
    return registry, repository, tag or "latest"
