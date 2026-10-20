"""Expand multi-document manual report rows into their visible item details."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


_DETAIL_COLUMNS = (
    "Артикул",
    "Номенклатура",
    "Количество УТ",
    "Количество БУХ",
    "Разница БУХ − УТ",
    "Период исходной строки УТ",
    "Исходный начальный остаток УТ",
    "Исходный конечный остаток УТ",
    "Период исходной строки БУХ",
    "Исходный начальный остаток БУХ",
    "Исходный конечный остаток БУХ",
)


def expand_manual_rows(
    headers: Sequence[str], rows: Sequence[Mapping[str, object]]
) -> list[dict[str, str]]:
    """Split aligned newline details; keep metadata constant on each output row."""
    if "Номенклатура" not in headers:
        raise ValueError("missing required detail column: Номенклатура")

    expanded: list[dict[str, str]] = []
    for row in rows:
        values = {header: "" if row.get(header) is None else str(row.get(header, ""))
                  for header in headers}
        names = values["Номенклатура"].split("\n")
        count = len(names)
        columns = [column for column in _DETAIL_COLUMNS if column in values]
        detail_values: dict[str, list[str]] = {column: values[column].split("\n") for column in columns}
        for column, items in detail_values.items():
            if len(items) != count:
                raise ValueError(
                    f"manual detail line count for {column} is {len(items)}; expected {count}"
                )
        for index in range(count):
            expanded.append({
                header: detail_values[header][index] if header in detail_values else values[header]
                for header in headers
            })
    return expanded
