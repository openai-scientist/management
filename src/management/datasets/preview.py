"""The first rows of a stored dataset version, with personal data masked when the project asks.

What the detector finds, and nothing more is promised:

- a cell holding an email address (`email` detector);
- a cell holding a phone number: 9 to 15 digits, optionally starting with +, written with
  spaces, dots, dashes or brackets (`phone` detector);
- every cell of a column whose header names a person (`name`, `full_name`, `first_name`,
  `last_name`, `ho_ten`, ...) or a personal identifier (`ssn`, `passport`, `cccd`, `cmnd`,
  `national_id`, `student_id`, `patient_id`, `user_id`, ...) (`name_column`, `id_column`).

Names and identifiers written in a column with some other header are not detected.
"""

import csv
import io
import re
from contextlib import aclosing
from dataclasses import dataclass

from management.infra.file_store import FileStore

REDACTED = "[REDACTED]"
# Bytes read from the start of the file at most, whatever `limit` asks for.
PREVIEW_MAX_BYTES = 1024 * 1024

_EMAIL = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?[\d\s().-]{9,24}")
_NAME_TOKENS = frozenset({"name", "fullname", "firstname", "lastname", "surname", "hoten"})
_ID_TOKENS = frozenset({"ssn", "passport", "cccd", "cmnd", "nationalid", "taxid"})
# `<who>_id` names a person; a bare `id` or `run_id` does not.
_PERSON_TOKENS = frozenset(
    {"student", "patient", "user", "customer", "employee", "person", "member", "citizen"}
)


@dataclass(frozen=True, slots=True)
class Preview:
    columns: list[str]
    rows: list[list[str]]
    masked_cells: int


def _tokens(header: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", header.lower()) if t]


def _personal_column(header: str) -> bool:
    tokens = _tokens(header)
    joined = "".join(tokens)
    if joined in _NAME_TOKENS or "name" in tokens or joined in _ID_TOKENS:
        return True
    if set(tokens) & _ID_TOKENS:
        return True
    return len(tokens) >= 2 and tokens[-1] == "id" and tokens[-2] in _PERSON_TOKENS


def _personal_value(value: str) -> bool:
    text = value.strip()
    if _EMAIL.search(text):
        return True
    if _PHONE.fullmatch(text):
        return 9 <= sum(ch.isdigit() for ch in text) <= 15
    return False


def mask(columns: list[str], rows: list[list[str]]) -> Preview:
    personal = [_personal_column(name) for name in columns]
    masked = 0
    out: list[list[str]] = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            if value and (personal[index] or _personal_value(value)):
                cells.append(REDACTED)
                masked += 1
            else:
                cells.append(value)
        out.append(cells)
    return Preview(columns, out, masked)


async def read_rows(store: FileStore, key: str, limit: int) -> tuple[list[str], list[list[str]]]:
    """The header and up to `limit` rows, reading no more than PREVIEW_MAX_BYTES."""
    buffer = bytearray()
    complete = True
    async with aclosing(store.open(key)) as chunks:
        async for chunk in chunks:
            buffer += chunk
            if len(buffer) >= PREVIEW_MAX_BYTES:
                complete = False
                break
    if not complete:
        # Cut at a line end so no character is split; the last row may still be partial.
        buffer = buffer[: buffer.rfind(b"\n") + 1]
    reader = csv.reader(io.StringIO(buffer.decode("utf-8-sig"), newline=""))
    header = next(reader, [])
    rows = [row for row in reader if row]
    if not complete and rows:
        rows.pop()
    return [name.strip() for name in header], rows[:limit]
