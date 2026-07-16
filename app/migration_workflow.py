from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
import logging

from . import config, docker
from .migrator.migration import MigrationPlan, build_migration_plan
from .migrator.mssql import (
    SOURCE_VERSION_IMAGES,
    SourceDatabase,
    SqlServerContainer,
    default_database_name,
    validate_database_file,
    validate_log_file,
)
from .migrator.postgres import PostgresClient, PostgresConfig

logger = logging.getLogger(__name__)


class ContainerPostgresClient(PostgresClient):
    """Run the legacy SQL scripts through the managed PostgreSQL container."""

    def run_sql(self, sql: str) -> str:
        logger.info("Executing PostgreSQL migration SQL in the database container")
        env = os.environ.copy()
        env["PGPASSWORD"] = self.config.password
        command = [
            "docker", "compose", "-f", str(config.COMPOSE_FILE), "exec", "-T", "-e", "PGPASSWORD", "db",
            "psql", "--no-psqlrc", "--username", self.config.user,
            "--dbname", self.config.database, "--set", "ON_ERROR_STOP=1",
            "--no-align", "--tuples-only", "--field-separator", "|",
        ]
        result = subprocess.run(command, input=sql, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout or "PostgreSQL command failed.")
        return result.stdout.strip()


@dataclass
class MigrationSession:
    source: SourceDatabase
    container: SqlServerContainer
    plan: MigrationPlan | None = None

    @classmethod
    def create(cls, mdf_file: str, ldf_file: str, version: str) -> "MigrationSession":
        logger.info("Creating migration session: MDF=%s LDF=%s SQL Server=%s", mdf_file, ldf_file, version)
        mdf_path = validate_database_file(mdf_file, ".mdf")
        ldf_path = validate_log_file(ldf_file)
        if mdf_path == ldf_path:
            raise ValueError("The MDF and log files must be different files.")
        source = SourceDatabase(
            mdf_path=mdf_path,
            ldf_path=ldf_path,
            database_name=default_database_name(mdf_path),
            version=version,
        )
        return cls(source, SqlServerContainer(source, f"kalliopen-mssql-{source.container_version}"))

    def prepare_preview(self) -> MigrationPlan:
        try:
            logger.info("Starting temporary SQL Server container and reading source data")
            self.container.prepare()
            self.plan = build_migration_plan(self.container.read_migration_source())
            logger.info("Migration preview prepared: %s", self.plan.record_counts)
            return self.plan
        except Exception:
            self.container.remove()
            raise

    def commit(self, postgres: PostgresConfig | None = None) -> Path | None:
        if self.plan is None:
            raise RuntimeError("Prepare the migration preview before importing.")
        backup: Path | None = None
        try:
            if postgres is None:
                state = config.ensure_state()
                logger.info("Backing up current database before migration")
                backup = docker.automatic_backup_path("pre-migration")
                docker.export_database(backup)
                docker.delete_database()
                docker.migrate()
                target = ContainerPostgresClient(PostgresConfig(
                    host="localhost", port=5432, user="kalliopen",
                    password=state["postgres_password"], database="kalliopen",
                ))
            else:
                logger.info("Using custom PostgreSQL target %s:%s/%s", postgres.host, postgres.port, postgres.database)
                target = PostgresClient(postgres)
            logger.info("Applying prepared PSBiblio migration plan")
            target.validate_target()
            target.apply(self.plan)
            logger.info("PSBiblio migration completed")
            return backup
        finally:
            logger.info("Removing temporary SQL Server container")
            self.container.remove()

    def close(self) -> None:
        logger.info("Closing migration session")
        self.container.remove()


def source_versions() -> tuple[str, ...]:
    return tuple(SOURCE_VERSION_IMAGES)
