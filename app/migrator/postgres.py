from __future__ import annotations

import csv
import io
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Sequence

from .migration import MigrationPlan
from .mssql import CommandError, MigrationError


REQUIRED_COLUMNS = {
    "grade": {"id"},
    "class": {"id", "name", "grade_id"},
    "student": {
        "id",
        "id_external",
        "first_name",
        "last_name",
        "class_id",
        "city",
        "postal_code",
        "street",
        "house_number",
    },
    "work": {"id", "name"},
    "book": {
        "id",
        "work_id",
        "isbn",
        "title",
        "author",
        "publisher",
        "published",
        "price",
    },
    "stock": {"id", "book_id", "barcode", "purchased", "note"},
    "loan": {
        "id",
        "student_id",
        "stock_id",
        "loaned_at",
        "returned_at",
        "note",
    },
}

_NULL_VALUE = "__PSBIBLIO_MIGRATOR_NULL__"


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@dataclass(frozen=True)
class PostgresConfig:
    host: str = "localhost"
    port: int = 5432
    user: str = "kalliopen"
    password: str = ""
    database: str = "postgres"
    schema: str = "public"


class PostgresClient:
    def __init__(self, config: PostgresConfig) -> None:
        self.config = config

    def _command(self) -> list[str]:
        return [
            "psql",
            "--no-psqlrc",
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            "--username",
            self.config.user,
            "--dbname",
            self.config.database,
            "--set",
            "ON_ERROR_STOP=1",
            "--no-align",
            "--tuples-only",
            "--field-separator",
            "|",
        ]

    def run_sql(self, sql: str) -> str:
        env = os.environ.copy()
        env["PGPASSWORD"] = self.config.password
        command = self._command()
        try:
            result = subprocess.run(
                command,
                input=sql,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise MigrationError(
                "The PostgreSQL psql client is required but was not found."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(command, exc.stderr or exc.stdout or "") from exc
        return result.stdout.strip()

    def validate_target(self) -> None:
        schema_literal = _quote_literal(self.config.schema)
        table_literals = ", ".join(_quote_literal(name) for name in REQUIRED_COLUMNS)
        query = f"""
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_schema = {schema_literal}
  AND table_name IN ({table_literals})
ORDER BY table_name, ordinal_position;
"""
        output = self.run_sql(query)
        found: dict[str, set[str]] = {table: set() for table in REQUIRED_COLUMNS}
        for line in output.splitlines():
            if "|" not in line:
                continue
            table, column = line.split("|", 1)
            if table in found:
                found[table].add(column)

        missing: list[str] = []
        for table, required_columns in REQUIRED_COLUMNS.items():
            absent = sorted(required_columns - found[table])
            if absent:
                actual = ""
                if table == "student":
                    actual = f" (found columns: {', '.join(sorted(found[table]))})"
                missing.append(f"{table}: {', '.join(absent)}{actual}")
        if missing:
            raise MigrationError(
                f"PostgreSQL schema {self.config.schema!r} is missing required "
                "tables or columns:\n  " + "\n  ".join(missing)
            )

        schema = _quote_identifier(self.config.schema)
        guarded_tables = ("grade", "class", "student", "work", "book", "stock", "loan")
        count_query = "\nUNION ALL\n".join(
            f"SELECT {_quote_literal(table)}, COUNT(*) FROM {schema}.{_quote_identifier(table)}"
            for table in guarded_tables
        )
        counts = self.run_sql(count_query + ";")
        nonempty: list[str] = []
        for line in counts.splitlines():
            if "|" not in line:
                continue
            table, count = line.split("|", 1)
            try:
                row_count = int(count)
            except ValueError as exc:
                raise MigrationError(
                    f"Could not read PostgreSQL row count for {table}: {count!r}"
                ) from exc
            if row_count > 0:
                nonempty.append(f"{table} ({count})")
        if nonempty:
            raise MigrationError(
                "The target must be empty before this one-time migration. Existing "
                "rows were found in: " + ", ".join(nonempty)
            )

    def apply(self, plan: MigrationPlan) -> None:
        self.run_sql(_build_import_script(plan, self.config.schema))


def _serialize(value: object) -> object:
    if value is None:
        return _NULL_VALUE
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _copy_block(table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for row in rows:
        writer.writerow([_serialize(value) for value in row])
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    return (
        f"COPY {_quote_identifier(table)} ({column_sql}) FROM STDIN "
        f"WITH (FORMAT csv, NULL {_quote_literal(_NULL_VALUE)});\n"
        f"{output.getvalue()}\\.\n"
    )


def _build_import_script(
    plan: MigrationPlan,
    schema_name: str,
) -> str:
    schema = _quote_identifier(schema_name)
    students = _copy_block(
        "_psb_student",
        (
            "source_bnummer",
            "first_name",
            "last_name",
            "class_name",
            "class_year",
            "city",
            "postal_code",
            "street",
            "house_number",
        ),
        (
            (
                row.source_bnummer,
                row.first_name,
                row.last_name,
                row.class_name,
                row.class_year,
                row.city,
                row.postal_code,
                row.street,
                row.house_number,
            )
            for row in plan.students
        ),
    )
    books = _copy_block(
        "_psb_book",
        (
            "work_key",
            "work_name",
            "isbn",
            "title",
            "author",
            "publisher",
            "published",
            "price",
        ),
        (
            (
                row.work_key,
                row.work_name,
                row.isbn,
                row.title,
                row.author,
                row.publisher,
                row.published,
                row.price,
            )
            for row in plan.books
        ),
    )
    stocks = _copy_block(
        "_psb_stock",
        ("source_mnummer", "work_key", "purchased", "note"),
        (
            (row.source_mnummer, row.work_key, row.purchased, row.note)
            for row in plan.stocks
        ),
    )
    loans = _copy_block(
        "_psb_loan",
        ("source_bnummer", "source_mnummer", "loaned_at"),
        (
            (row.source_bnummer, row.source_mnummer, row.loaned_at)
            for row in plan.loans
        ),
    )

    return f"""
\\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path TO {schema}, pg_catalog;

DO $psbiblio$
BEGIN
    IF EXISTS (SELECT 1 FROM "student")
       OR EXISTS (SELECT 1 FROM "grade")
       OR EXISTS (SELECT 1 FROM "class")
       OR EXISTS (SELECT 1 FROM "work")
       OR EXISTS (SELECT 1 FROM "book")
       OR EXISTS (SELECT 1 FROM "stock")
       OR EXISTS (SELECT 1 FROM "loan") THEN
        RAISE EXCEPTION 'Target tables are no longer empty; migration aborted';
    END IF;
END;
$psbiblio$;

CREATE TEMP TABLE "_psb_student" (
    "source_bnummer" text NOT NULL,
    "first_name" text NOT NULL,
    "last_name" text NOT NULL,
    "class_name" text,
    "class_year" integer,
    "city" text,
    "postal_code" text,
    "street" text,
    "house_number" text
);
CREATE TEMP TABLE "_psb_book" (
    "work_key" text PRIMARY KEY,
    "work_name" text NOT NULL,
    "isbn" text NOT NULL,
    "title" text NOT NULL,
    "author" text,
    "publisher" text,
    "published" date,
    "price" numeric(10, 2) NOT NULL
);
CREATE TEMP TABLE "_psb_stock" (
    "source_mnummer" text PRIMARY KEY,
    "work_key" text NOT NULL,
    "purchased" date,
    "note" text NOT NULL
);
CREATE TEMP TABLE "_psb_loan" (
    "source_bnummer" text NOT NULL,
    "source_mnummer" text NOT NULL,
    "loaned_at" timestamp NOT NULL
);

{students}{books}{stocks}{loans}
INSERT INTO "grade" ("id")
SELECT DISTINCT "class_year"
FROM "_psb_student"
WHERE "class_year" IS NOT NULL
ON CONFLICT ("id") DO NOTHING;

INSERT INTO "class" ("name", "grade_id")
SELECT DISTINCT source."class_name", source."class_year"
FROM "_psb_student" AS source
WHERE source."class_name" IS NOT NULL
  AND source."class_year" IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM "class" AS target
      WHERE target."name" = source."class_name"
        AND target."grade_id" = source."class_year"
  );

INSERT INTO "student" (
    "id_external", "first_name", "last_name", "class_id",
    "city", "postal_code", "street", "house_number"
)
SELECT
    source."source_bnummer",
    source."first_name",
    source."last_name",
    class_match."id",
    source."city",
    source."postal_code",
    source."street",
    source."house_number"
FROM "_psb_student" AS source
LEFT JOIN LATERAL (
    SELECT target."id"
    FROM "class" AS target
    WHERE target."name" = source."class_name"
      AND target."grade_id" = source."class_year"
    ORDER BY target."id"
    LIMIT 1
) AS class_match ON true;

INSERT INTO "work" ("name")
SELECT "work_name"
FROM "_psb_book"
ORDER BY "work_key";

INSERT INTO "book" (
    "work_id", "isbn", "title", "author", "publisher", "published", "price"
)
SELECT
    target_work."id",
    source."isbn",
    source."title",
    source."author",
    source."publisher",
    source."published",
    source."price"
FROM "_psb_book" AS source
JOIN "work" AS target_work ON target_work."name" = source."work_name";

INSERT INTO "stock" ("book_id", "barcode", "purchased", "note")
SELECT
    target_book."id",
    source."source_mnummer",
    COALESCE(source."purchased", CURRENT_DATE),
    source."note"
FROM "_psb_stock" AS source
JOIN "_psb_book" AS source_book ON source_book."work_key" = source."work_key"
JOIN "book" AS target_book ON target_book."isbn" = source_book."isbn";

INSERT INTO "loan" (
    "student_id", "stock_id", "loaned_at", "returned_at", "note"
)
SELECT
    target_student."id",
    target_stock."id",
    source."loaned_at",
    NULL,
    ''
FROM "_psb_loan" AS source
JOIN "student" AS target_student
  ON target_student."id_external" = source."source_bnummer"
JOIN "stock" AS target_stock
  ON target_stock."barcode" = source."source_mnummer";

DO $psbiblio$
BEGIN
    IF (SELECT COUNT(*) FROM "grade") <> {len(plan.grades)}
       OR (SELECT COUNT(*) FROM "class") <> {len(plan.classes)}
       OR (SELECT COUNT(*) FROM "student") <> (SELECT COUNT(*) FROM "_psb_student")
       OR (SELECT COUNT(*) FROM "work") <> (SELECT COUNT(*) FROM "_psb_book")
       OR (SELECT COUNT(*) FROM "book") <> (SELECT COUNT(*) FROM "_psb_book")
       OR (SELECT COUNT(*) FROM "stock") <> (SELECT COUNT(*) FROM "_psb_stock")
       OR (SELECT COUNT(*) FROM "loan") <> (SELECT COUNT(*) FROM "_psb_loan") THEN
        RAISE EXCEPTION 'Inserted row counts do not match the migration preview';
    END IF;
END;
$psbiblio$;

UPDATE "student"
SET "id_external" = NULL
WHERE "id_external" IN (SELECT "source_bnummer" FROM "_psb_student");

COMMIT;
"""
