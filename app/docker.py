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
import os
import pwd
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)


def available() -> bool:
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "info"], capture_output=True
    ).returncode == 0


def installed() -> bool:
    return shutil.which("docker") is not None


def configure() -> None:
    if not installed():
        install()
    logger.info("Enabling the Docker service")
    subprocess.run(["pkexec", "systemctl", "enable", "--now", "docker"], check=True)
    if available():
        return
    result = subprocess.run(["docker", "info"], capture_output=True, text=True)
    detail = (result.stderr or result.stdout).strip()
    if "permission denied" in detail.lower() or "docker.sock" in detail.lower():
        username = pwd.getpwuid(os.getuid()).pw_name
        logger.info("Adding user %s to the Docker group", username)
        subprocess.run(["pkexec", "usermod", "-aG", "docker", username], check=True)
        raise RuntimeError(
            "Docker access has been configured. Log out of Linux Mint and log back in, then reopen KalliOpen Launcher."
        )
    raise RuntimeError(detail or "Docker is installed but unavailable.")


def install() -> None:
    logger.info("Installing Docker and Docker Compose")
    subprocess.run(["pkexec", "apt-get", "update"], check=True)
    subprocess.run(["pkexec", "apt-get", "install", "-y", "docker.io", "docker-compose-v2"], check=True)


def compose(*args: str, capture: bool = True, log: bool = True) -> subprocess.CompletedProcess[str]:
    config.write_compose(config.ensure_state(), application_url())
    if log:
        logger.info("Running Docker Compose: %s", " ".join(args))
    result = subprocess.run(
        ["docker", "compose", "-f", str(config.COMPOSE_FILE), *args],
        capture_output=capture, text=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "Docker Compose command failed.").strip()
        raise RuntimeError(detail)
    return result


def health() -> str:
    try:
        result = urllib.request.urlopen(f"http://localhost:{config.APP_PORT}/api/trpc/health", timeout=3)
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


def application_url() -> str:
    host = local_ip()
    if host == "unavailable" or host.startswith("127."):
        host = "localhost"
    return f"http://{host}:{config.APP_PORT}"


def managed_install_exists() -> bool:
    try:
        return bool(compose("ps", "-a", "-q", log=False).stdout.strip())
    except Exception:
        return False


def app_container_running() -> bool:
    try:
        return bool(compose("ps", "--status", "running", "-q", "app", log=False).stdout.strip())
    except Exception:
        return False


def export_database(destination: Path) -> None:
    logger.info("Exporting database to %s", destination)
    ensure_database_running()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with temporary.open("wb") as output:
            temporary.chmod(0o600)
            result = subprocess.run(
                ["docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "db",
                 "pg_dump", "-U", "kalliopen", "-Fc", "kalliopen"],
                stdout=output, stderr=subprocess.PIPE, text=True,
            )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "PostgreSQL database export failed.")
        temporary.replace(destination)
        destination.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def automatic_backup_path(reason: str) -> Path:
    config.ensure_backup_directory()
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
    stop_app()
    try:
        reset_database_schema()
        with source.open("rb") as data:
            command = [
                "docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "db",
                "pg_restore", "-U", "kalliopen", "-d", "kalliopen", "--clean", "--if-exists",
                "--exit-on-error", "--single-transaction",
            ]
            result = subprocess.run(command, stdin=data, capture_output=True)
        if result.returncode:
            detail = (result.stderr or result.stdout).decode(errors="replace").strip()
            raise RuntimeError(detail or "PostgreSQL database import failed.")
        migrate()
        start_app()
    except Exception as exc:
        raise RuntimeError(
            f"{exc}\n\nThe previous database backup is saved at:\n{backup}\n\nKalliOpen remains stopped."
        ) from exc


def delete_database() -> None:
    logger.info("Deleting KalliOpen database schema")
    stop_app()
    ensure_database_running()
    reset_database_schema()
    migrate()
    start_app()


def reset_database_schema() -> None:
    logger.info("Resetting KalliOpen database schema")
    compose("exec", "-T", "db", "psql", "-U", "kalliopen", "-d", "kalliopen", "-v",
            "ON_ERROR_STOP=1", "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def migrate() -> None:
    logger.info("Running KalliOpen migrator container")
    compose("run", "--rm", "migrator")


def stop_app() -> None:
    logger.info("Stopping KalliOpen application container")
    compose("stop", "app")


def start_app() -> None:
    logger.info("Starting KalliOpen application container")
    compose("up", "-d", "app", "--no-deps")


def prepare_empty_database() -> None:
    logger.info("Preparing an empty migrated KalliOpen database")
    stop_app()
    ensure_database_running()
    reset_database_schema()
    migrate()


def start() -> None:
    logger.info("Starting KalliOpen services and applying database migrations")
    stop_app()
    ensure_database_running()
    migrate()
    start_app()


def update() -> None:
    logger.info("Pulling and applying KalliOpen application update")
    compose("pull", "app", "migrator")
    start()


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
        latest_digests = _manifest_digests(registry, repository, latest_version, token)
        latest = current if current in latest_digests else next(iter(latest_digests), None)
        configured_digests = _manifest_digests(registry, repository, tag, token)
        if current_version is None:
            matching_tags = [
                item for item in version_tags
                if current in _manifest_digests(registry, repository, item, token)
            ] if current else []
            if matching_tags:
                current_version = max(matching_tags, key=_version_key)
            elif current in configured_digests and latest_version == tag:
                current_version = latest_version
            elif current:
                current_version = "Unknown"
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


def _manifest_digests(registry: str, repository: str, tag: str, token: str) -> set[str]:
    request = urllib.request.Request(
        f"https://{registry}/v2/{repository}/manifests/{tag}",
        headers={
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read())
        digests = {item["digest"] for item in payload.get("manifests", []) if item.get("digest")}
        header_digest = response.headers.get("Docker-Content-Digest")
        if header_digest:
            digests.add(header_digest)
        return digests


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
