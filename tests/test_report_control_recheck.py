# Optional tests for local-only pilot utilities. Public CI remains portable.
import importlib.util as _import_util
import unittest as _unittest
_missing_local_tools = [name for name in (
    'scripts.report_control_recheck',
) if _import_util.find_spec(name) is None]
if _missing_local_tools:
    raise _unittest.SkipTest("Local pilot utilities are not shipped: " + ", ".join(_missing_local_tools))

from collections import Counter
from datetime import date
from unittest import TestCase

from scripts.report_control_recheck import (
    OLD_ACTION_HEADERS,
    SOURCE_HEADERS,
    actual_cell_key,
    boundary_context,
    validate_manual_sheet,
)


def manual_row(**overrides):
    cells = {name: "" for name in OLD_ACTION_HEADERS + SOURCE_HEADERS}
    cells.update({
        "База": "БУХ2019",
        "Документ УТ": "",
        "Дата документа УТ": "",
        "Номенклатура": "Товар",
        "Артикул": "A-1",
        "Склад УТ": "Склад",
        "Организация УТ": "ООО",
        "ИНН организации УТ": "123",
        "Количество УТ": "0",
        "Номер": "",
        "Дата и время": "30.09.2026 23:59:59",
        "Контрольная точка": "Конечный остаток",
        "Дата контроля": "30.09.2026",
        "Статус наличия документа": "Наличие неизвестно",
        "Период исходной строки УТ": "30.09.2026 23:59:59",
        "Исходный начальный остаток УТ": "1",
        "Исходный конечный остаток УТ": "1",
    })
    cells.update(overrides)
    return [cells[name] for name in OLD_ACTION_HEADERS + SOURCE_HEADERS]


def expected_for(row):
    headers = OLD_ACTION_HEADERS + SOURCE_HEADERS
    cells = dict(zip(headers, row))
    return Counter([actual_cell_key(cells, "trade")])


class ReportControlRecheckTests(TestCase):
    def test_manual_source_gate_accepts_zero_movement_with_source_closing_one(self):
        row = manual_row()
        # W/X remain movement/legacy quantities; source closing is independently retained as 1.
        cells = dict(zip(OLD_ACTION_HEADERS + SOURCE_HEADERS, row))
        self.assertEqual(cells["Количество УТ"], "0")
        self.assertEqual(cells["Исходный конечный остаток УТ"], "1")
        result = validate_manual_sheet(OLD_ACTION_HEADERS + SOURCE_HEADERS, [row], expected_for(row), None, None)
        self.assertTrue(result["passed"])


    def test_manual_source_gate_rejects_wrong_source_closing_value(self):
        row = manual_row()
        changed = manual_row(**{"Исходный конечный остаток УТ": "2"})
        result = validate_manual_sheet(OLD_ACTION_HEADERS + SOURCE_HEADERS, [changed], expected_for(row), None, None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_cell_instances"], 1)
        self.assertEqual(result["unexpected_cell_instances"], 1)


    def test_manual_source_gate_rejects_missing_or_reordered_new_columns(self):
        row = manual_row()
        result = validate_manual_sheet(OLD_ACTION_HEADERS + SOURCE_HEADERS[:-1], [row[:-1]], expected_for(row), None, None)
        self.assertFalse(result["passed"])
        self.assertTrue(result["header_error"])

    def test_boundary_context_uses_exact_source_timestamp_and_leaves_midpoints_blank(self):
        begin, end = date(2026, 9, 1), date(2026, 9, 7)
        for literal in ("01.09.2026 0:00:00", "DemoPhone 00:00:00"):
            self.assertEqual(boundary_context(literal, begin, end), ("Начальный остаток", "01.09.2026"))
        self.assertEqual(boundary_context("04.09.2026 12:00:00", begin, end), ("", ""))

    def test_manual_source_gate_rejects_wrong_observed_boundary_context(self):
        row = manual_row()
        changed = manual_row(**{"Контрольная точка": "Начальный остаток"})
        result = validate_manual_sheet(OLD_ACTION_HEADERS + SOURCE_HEADERS, [changed], expected_for(row), None, None)
        self.assertFalse(result["passed"])
