from __future__ import annotations

from collections.abc import Iterable
from typing import Any


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_spreadsheet_cell(value: Any) -> Any:
    """Prevent exported user text from being interpreted as a formula."""
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def safe_spreadsheet_row(values: Iterable[Any]) -> list[Any]:
    return [safe_spreadsheet_cell(value) for value in values]
