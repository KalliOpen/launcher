from __future__ import annotations

import os
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SQL_SERVER_IMAGES = {
    "2017": "mcr.microsoft.com/mssql/server:2017-latest",
    "2019": "mcr.microsoft.com/mssql/server:2019-latest",
    "2022": "mcr.microsoft.com/mssql/server:2022-latest",
    "2025": "mcr.microsoft.com/mssql/server:2025-latest",
}

SOURCE_VERSION_IMAGES = {
    "2005": "2017",
    "2008": "2017",
    "2008r2": "2017",
    "2012": "2017",
    "2014": "2017",
    "2016": "2017",
    "2017": "2017",
    "2019": "2019",
    "2022": "2022",
    "2025": "2025",
}

SOURCE_VERSION_LABELS = {
    "2005": "SQL Server 2005",
    "2008": "SQL Server 2008",
    "2008r2": "SQL Server 2008 R2",
    "2012": "SQL Server 2012",
    "2014": "SQL Server 2014",
    "2016": "SQL Server 2016",
    "2017": "SQL Server 2017",
    "2019": "SQL Server 2019",
    "2022": "SQL Server 2022",
    "2025": "SQL Server 2025",
}

SQLCMD_PATHS = (
    "/opt/mssql-tools18/bin/sqlcmd",
    "/opt/mssql-tools/bin/sqlcmd",
)

_CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class MigrationError(RuntimeError):
    """A user-facing error raised while preparing the SQL Server source."""


class CommandError(MigrationError):
    def __init__(self, command: Sequence[str], stderr: str = "") -> None:
        display = " ".join(command)
        detail = stderr.strip()
        message = f"Command failed: {display}"
        if detail:
            message += f"\n{detail}"
        super().__init__(message)


class CommandRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        capture: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(command),
                check=check,
                capture_output=capture,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise MigrationError(f"Required command not found: {command[0]}") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(command, exc.stderr or exc.stdout or "") from exc


def validate_database_file(value: str | Path, expected_suffix: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"File does not exist: {path}")
    if path.suffix.lower() != expected_suffix:
        raise MigrationError(f"Expected a {expected_suffix} file, got: {path.name}")
    return path


def validate_log_file(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"File does not exist: {path}")
    if path.suffix.lower() not in {".mdf", ".ldf"}:
        raise MigrationError(f"Expected a .mdf or .ldf log file, got: {path.name}")
    return path


def validate_database_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise MigrationError("The database name cannot be empty.")
    if len(name) > 128:
        raise MigrationError("The database name cannot exceed 128 characters.")
    return name


def default_database_name(mdf_path: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", mdf_path.stem).strip("_")
    return (name or "psbiblio_source")[:128]


def validate_container_name(value: str) -> str:
    if not _CONTAINER_NAME_PATTERN.fullmatch(value):
        raise MigrationError(
            "Container names must start with a letter or number and contain only "
            "letters, numbers, dots, underscores, or hyphens."
        )
    return value


def quote_identifier(value: str) -> str:
    return f"[{value.replace(']', ']]')}]"


def generate_sa_password() -> str:
    # The prefix guarantees SQL Server's length and character-class requirements.
    return f"Psb!{secrets.token_urlsafe(24)}"


@dataclass(frozen=True)
class SourceDatabase:
    mdf_path: Path
    ldf_path: Path
    database_name: str
    version: str

    @property
    def image(self) -> str:
        return SQL_SERVER_IMAGES[self.container_version]

    @property
    def container_version(self) -> str:
        return SOURCE_VERSION_IMAGES[self.version]


class SqlServerContainer:
    def __init__(
        self,
        source: SourceDatabase,
        container_name: str,
        *,
        runner: CommandRunner | None = None,
        password: str | None = None,
    ) -> None:
        self.source = source
        self.container_name = validate_container_name(container_name)
        self.runner = runner or CommandRunner()
        self.password = password or generate_sa_password()
        self.started = False
        self.sqlcmd_path: str | None = None

    def check_docker(self) -> None:
        result = self.runner.run(
            ["docker", "info", "--format", "{{.Architecture}}"],
        )
        architecture = result.stdout.strip().lower()
        if architecture not in {"amd64", "x86_64"}:
            raise MigrationError(
                "Microsoft SQL Server Linux containers require an x86-64 Docker host; "
                f"Docker reported {architecture or 'an unknown architecture'}."
            )

    def pull_image(self) -> None:
        print(f"Pulling {self.source.image} ...")
        self.runner.run(["docker", "pull", self.source.image], capture=False)

    def start(self) -> None:
        env = os.environ.copy()
        env["MSSQL_SA_PASSWORD"] = self.password
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.container_name,
            "--hostname",
            self.container_name,
            "--env",
            "ACCEPT_EULA=Y",
            "--env",
            "MSSQL_PID=Developer",
            "--env",
            "MSSQL_SA_PASSWORD",
            self.source.image,
        ]
        self.runner.run(command, env=env)
        self.started = True

    def find_sqlcmd(self) -> str:
        for path in SQLCMD_PATHS:
            result = self.runner.run(
                ["docker", "exec", self.container_name, "test", "-x", path],
                check=False,
            )
            if result.returncode == 0:
                self.sqlcmd_path = path
                return path
        raise MigrationError("The SQL Server image does not contain a supported sqlcmd binary.")

    def _sqlcmd(self, query: str, *, database: str = "master") -> str:
        if self.sqlcmd_path is None:
            self.find_sqlcmd()

        env = os.environ.copy()
        env["SQLCMDPASSWORD"] = self.password
        command = [
            "docker",
            "exec",
            "--env",
            "SQLCMDPASSWORD",
            self.container_name,
            self.sqlcmd_path or "",
            "-S",
            "localhost",
            "-U",
            "sa",
            "-C",
            "-b",
            "-l",
            "5",
            "-d",
            database,
            "-h",
            "-1",
            "-W",
            "-s",
            "|",
            "-Q",
            query,
        ]
        return self.runner.run(command, env=env).stdout.strip()

    def _sqlcmd_json(self, query: str, *, database: str) -> list[dict[str, object]]:
        if self.sqlcmd_path is None:
            self.find_sqlcmd()

        env = os.environ.copy()
        env["SQLCMDPASSWORD"] = self.password
        command = [
            "docker",
            "exec",
            "--env",
            "SQLCMDPASSWORD",
            self.container_name,
            self.sqlcmd_path or "",
            "-S",
            "localhost",
            "-U",
            "sa",
            "-C",
            "-b",
            "-l",
            "30",
            "-d",
            database,
            "-y",
            "0",
            "-w",
            "65535",
            "-Q",
            query,
        ]
        output = self.runner.run(command, env=env).stdout
        combined = "".join(output.splitlines())
        start = combined.find("[")
        end = combined.rfind("]")
        if start < 0 or end < start:
            raise MigrationError(
                "SQL Server did not return a JSON array while reading source records."
            )
        payload = combined[start : end + 1]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MigrationError(
                "SQL Server returned malformed JSON while reading source records: "
                f"{exc}"
            ) from exc
        if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
            raise MigrationError("SQL Server returned an unexpected source-data structure.")
        return parsed

    def _source_table(self, table_name: str) -> str:
        escaped_name = table_name.replace("'", "''")
        query = f"""
SET NOCOUNT ON;
SELECT TOP (1) s.name, t.name
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
WHERE UPPER(t.name) = UPPER(N'{escaped_name}')
ORDER BY CASE WHEN s.name = N'dbo' THEN 0 ELSE 1 END, s.name;
"""
        output = self._sqlcmd(query, database=self.source.database_name)
        for line in output.splitlines():
            if "|" not in line:
                continue
            schema, name = (part.strip() for part in line.split("|", 1))
            if schema and name:
                return f"{quote_identifier(schema)}.{quote_identifier(name)}"
        raise MigrationError(f"Required source table not found: {table_name}")

    def read_migration_source(self) -> dict[str, list[dict[str, object]]]:
        tables = {
            "benutzer": self._source_table("BENUTZER"),
            "medien": self._source_table("MEDIEN"),
            "ausleih": self._source_table("AUSLEIH"),
        }
        queries = {
            "benutzer": f"""
SET NOCOUNT ON;
SELECT
    [BNUMMER] AS [bnummer],
    [VORNAME] AS [first_name],
    [NACHNAME] AS [last_name],
    [KLASSE] AS [class_name],
    [STRASSE] AS [street],
    [PLZ] AS [postal_code],
    [ORT] AS [city]
FROM {tables['benutzer']}
ORDER BY [BNUMMER]
FOR JSON PATH, INCLUDE_NULL_VALUES;
""",
            "medien": f"""
SET NOCOUNT ON;
SELECT
    [MNUMMER] AS [mnummer],
    [AUTOR] AS [author],
    [TITEL] AS [title],
    [VERLAG] AS [publisher],
    [EJAHR] AS [published_year],
    [ISBN] AS [isbn],
    [ISBN13] AS [isbn13],
    [ZUDATUM] AS [purchased],
    [PREIS] AS [price],
    [BEMERKUNG] AS [note]
FROM {tables['medien']}
ORDER BY [MNUMMER]
FOR JSON PATH, INCLUDE_NULL_VALUES;
""",
            "ausleih": f"""
SET NOCOUNT ON;
SELECT
    [MNUM] AS [mnummer],
    [BNUM] AS [bnummer],
    [ADATUM] AS [loaned_at]
FROM {tables['ausleih']}
ORDER BY [ADATUM], [MNUM], [BNUM]
FOR JSON PATH, INCLUDE_NULL_VALUES;
""",
        }
        print("Reading BENUTZER, MEDIEN, and AUSLEIH ...")
        return {
            name: self._sqlcmd_json(query, database=self.source.database_name)
            for name, query in queries.items()
        }

    def wait_until_ready(self, timeout_seconds: int = 120) -> None:
        print("Waiting for SQL Server to accept connections", end="", flush=True)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self._sqlcmd("SET NOCOUNT ON; SELECT 1;")
                print(" ready.")
                return
            except CommandError:
                print(".", end="", flush=True)
                time.sleep(2)
        print()
        logs = self.logs(tail=80)
        raise MigrationError(
            f"SQL Server did not become ready within {timeout_seconds} seconds.\n{logs}"
        )

    def copy_source_files(self) -> None:
        destinations = (
            (self.source.mdf_path, "/var/opt/mssql/data/psbiblio_source.mdf"),
            (self.source.ldf_path, "/var/opt/mssql/data/psbiblio_source_log.mdf"),
        )
        for source, destination in destinations:
            self.runner.run(
                ["docker", "cp", str(source), f"{self.container_name}:{destination}"]
            )

        # Older images run SQL Server as root and do not contain an `mssql`
        # account. Newer images run as a non-root UID, so detect that UID
        # instead of assuming a username exists in every image.
        uid_result = self.runner.run(
            ["docker", "exec", self.container_name, "id", "-u"]
        )
        runtime_uid = uid_result.stdout.strip()
        if not runtime_uid.isdigit():
            raise MigrationError(
                f"Could not determine the SQL Server container user UID: {runtime_uid!r}"
            )
        if runtime_uid != "0":
            self.runner.run(
                [
                    "docker",
                    "exec",
                    "--user",
                    "0",
                    self.container_name,
                    "chown",
                    f"{runtime_uid}:0",
                    *(destination for _, destination in destinations),
                ]
            )
        self.runner.run(
            [
                "docker",
                "exec",
                "--user",
                "0",
                self.container_name,
                "chmod",
                "660",
                *(destination for _, destination in destinations),
            ]
        )

    def attach_database(self) -> None:
        database = quote_identifier(self.source.database_name)
        query = f"""
SET NOCOUNT ON;
CREATE DATABASE {database} ON
    (FILENAME = N'/var/opt/mssql/data/psbiblio_source.mdf'),
    (FILENAME = N'/var/opt/mssql/data/psbiblio_source_log.mdf')
FOR ATTACH;
ALTER DATABASE {database} SET READ_ONLY WITH ROLLBACK IMMEDIATE;
"""
        self._sqlcmd(query)

    def logs(self, *, tail: int = 100) -> str:
        result = self.runner.run(
            ["docker", "logs", "--tail", str(tail), self.container_name],
            check=False,
        )
        return (result.stdout + result.stderr).strip()

    def remove(self) -> None:
        if self.started:
            self.runner.run(
                ["docker", "rm", "--force", self.container_name],
                check=False,
            )
            self.started = False

    def prepare(self) -> None:
        self.check_docker()
        self.pull_image()
        self.start()
        self.find_sqlcmd()
        self.wait_until_ready()
        print("Copying source database files into the container ...")
        self.copy_source_files()
        print("Attaching a read-only working copy of the database ...")
        self.attach_database()
