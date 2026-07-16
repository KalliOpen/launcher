from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .mssql import MigrationError


@dataclass(frozen=True)
class StudentImport:
    source_bnummer: str
    first_name: str
    last_name: str
    class_name: str | None
    class_year: int | None
    city: str | None
    postal_code: str | None
    street: str | None
    house_number: str | None


@dataclass(frozen=True)
class BookImport:
    work_key: str
    work_name: str
    isbn: str
    title: str
    author: str | None
    publisher: str | None
    published: date | None
    price: Decimal


@dataclass(frozen=True)
class StockImport:
    source_mnummer: str
    work_key: str
    purchased: date | None
    note: str


@dataclass(frozen=True)
class LoanImport:
    source_bnummer: str
    source_mnummer: str
    loaned_at: datetime


@dataclass(frozen=True)
class MigrationPlan:
    source_counts: dict[str, int]
    skipped: dict[str, int]
    grades: tuple[int, ...]
    classes: tuple[tuple[str, int], ...]
    students: tuple[StudentImport, ...]
    books: tuple[BookImport, ...]
    stocks: tuple[StockImport, ...]
    loans: tuple[LoanImport, ...]

    @property
    def record_counts(self) -> dict[str, int]:
        return {
            "grades": len(self.grades),
            "classes": len(self.classes),
            "students": len(self.students),
            "works": len(self.books),
            "books": len(self.books),
            "stocks": len(self.stocks),
            "active loans": len(self.loans),
        }


def _text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).replace("\x00", "").strip()
    return cleaned or None


def _split_address(value: object) -> tuple[str | None, str | None]:
    address = _text(value)
    if not address:
        return None, None
    match = re.match(
        r"^(.*?\D)\s+(\d+\s*[A-Za-z]?(?:\s*[-/]\s*\d+\s*[A-Za-z]?)?)$",
        address,
    )
    if not match:
        return address, None
    street = match.group(1).strip()
    house_number = re.sub(r"\s+", "", match.group(2))
    return street or None, house_number or None


def _class_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    if not match:
        return None
    return int(match.group(0))


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = _text(value)
    if not text:
        return None
    normalized = text.removesuffix("Z").replace("T", " ")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _published_date(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    match = re.search(r"\d{4}", text)
    if not match:
        return None
    year = int(match.group(0))
    if not 1 <= year <= 9999:
        return None
    return date(year, 1, 1)


def _price(value: object) -> Decimal:
    if value is None:
        return Decimal("0.00")
    text = str(value).strip().replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return Decimal("0.00")
    try:
        return Decimal(match.group(0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _isbn(value: object) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    compact = re.sub(r"[^0-9Xx]", "", raw).upper()
    return compact if len(compact) in {10, 13} else raw


def _first_text(rows: list[dict[str, object]], key: str) -> str | None:
    for row in rows:
        value = _text(row.get(key))
        if value:
            return value
    return None


def _unique_fallback_isbn(title: str, used: set[str]) -> str:
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:20]
    candidate = f"missing-{digest}"
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"missing-{digest}-{suffix}"
    return candidate


def _duplicate_isbn(original_isbn: str, used: set[str], duplicate_number: int) -> str:
    prefix = "duplicate" if duplicate_number == 1 else f"duplicate{duplicate_number}"
    candidate = f"{prefix}-{original_isbn}"
    while candidate in used:
        duplicate_number += 1
        prefix = "duplicate" if duplicate_number == 1 else f"duplicate{duplicate_number}"
        candidate = f"{prefix}-{original_isbn}"
    return candidate


def build_migration_plan(
    source: dict[str, list[dict[str, object]]],
    *,
    today: date | None = None,
) -> MigrationPlan:
    try:
        benutzer = source["benutzer"]
        medien = source["medien"]
        ausleih = source["ausleih"]
    except KeyError as exc:
        raise MigrationError(f"Missing source dataset: {exc.args[0]}") from exc

    skipped: Counter[str] = Counter()

    students: list[StudentImport] = []
    student_ids: set[str] = set()
    classes: set[tuple[str, int]] = set()
    for row in benutzer:
        bnummer = _text(row.get("bnummer"))
        first_name = _text(row.get("first_name")) or "MIGRATED"
        last_name = _text(row.get("last_name")) or "MIGRATED"
        if not bnummer:
            skipped["students without BNUMMER"] += 1
            continue
        if bnummer in student_ids:
            skipped["duplicate BNUMMER rows"] += 1
            continue
        class_name = _text(row.get("class_name"))
        class_year = _class_year(class_name) if class_name else None
        if class_name and class_year is None:
            class_year = 0
        if class_name and class_year is not None:
            classes.add((class_name, class_year))
        street, house_number = _split_address(row.get("street"))
        students.append(
            StudentImport(
                source_bnummer=bnummer,
                first_name=first_name,
                last_name=last_name,
                class_name=class_name,
                class_year=class_year,
                city=_text(row.get("city")),
                postal_code=_text(row.get("postal_code")),
                street=street,
                house_number=house_number,
            )
        )
        student_ids.add(bnummer)

    media_rows: list[dict[str, object]] = []
    media_ids: set[str] = set()
    media_by_title: dict[str, list[dict[str, object]]] = {}
    for row in medien:
        mnummer = _text(row.get("mnummer"))
        title = _text(row.get("title"))
        if not mnummer:
            skipped["media without MNUMMER"] += 1
            continue
        if mnummer in media_ids:
            skipped["duplicate MNUMMER rows"] += 1
            continue
        if not title:
            skipped["media without a title"] += 1
            continue
        normalized = dict(row)
        normalized["mnummer"] = mnummer
        normalized["title"] = title
        media_rows.append(normalized)
        media_ids.add(mnummer)
        media_by_title.setdefault(title, []).append(normalized)

    books: list[BookImport] = []
    used_isbns: set[str] = set()
    duplicate_isbn_counts: Counter[str] = Counter()
    for title, rows in media_by_title.items():
        isbn = None
        for row in rows:
            isbn = _isbn(row.get("isbn13")) or _isbn(row.get("isbn"))
            if isbn:
                break
        if not isbn or isbn in used_isbns:
            if isbn in used_isbns:
                skipped["duplicate ISBNs replaced with migration IDs"] += 1
                duplicate_isbn_counts[isbn] += 1
                isbn = _duplicate_isbn(
                    isbn,
                    used_isbns,
                    duplicate_isbn_counts[isbn],
                )
            else:
                skipped["missing ISBNs replaced with migration IDs"] += 1
                isbn = _unique_fallback_isbn(title, used_isbns)
        used_isbns.add(isbn)

        published = None
        for row in rows:
            published = _published_date(row.get("published_year"))
            if published:
                break
        price = next((_price(row.get("price")) for row in rows if row.get("price") is not None), Decimal("0.00"))
        books.append(
            BookImport(
                work_key=title,
                work_name=title,
                isbn=isbn,
                title=title,
                author=_first_text(rows, "author"),
                publisher=_first_text(rows, "publisher"),
                published=published,
                price=price,
            )
        )

    stocks: list[StockImport] = []
    for row in media_rows:
        purchased_at = _datetime(row.get("purchased"))
        stocks.append(
            StockImport(
                source_mnummer=str(row["mnummer"]),
                work_key=str(row["title"]),
                purchased=purchased_at.date() if purchased_at else None,
                note=_text(row.get("note")) or "",
            )
        )

    loans: list[LoanImport] = []
    for row in ausleih:
        bnummer = _text(row.get("bnummer"))
        mnummer = _text(row.get("mnummer"))
        if not bnummer or bnummer not in student_ids:
            skipped["active loans without a migrated student"] += 1
            continue
        if not mnummer or mnummer not in media_ids:
            skipped["active loans without migrated stock"] += 1
            continue
        loaned_at = _datetime(row.get("loaned_at"))
        if loaned_at is None:
            skipped["active loans without a loan date"] += 1
            continue
        loans.append(
            LoanImport(
                source_bnummer=bnummer,
                source_mnummer=mnummer,
                loaned_at=loaned_at,
            )
        )

    grades = tuple(sorted({year for _, year in classes}))
    return MigrationPlan(
        source_counts={
            "BENUTZER": len(benutzer),
            "MEDIEN": len(medien),
            "AUSLEIH": len(ausleih),
        },
        skipped=dict(sorted(skipped.items())),
        grades=grades,
        classes=tuple(sorted(classes, key=lambda item: (item[1], item[0]))),
        students=tuple(students),
        books=tuple(books),
        stocks=tuple(stocks),
        loans=tuple(loans),
    )
