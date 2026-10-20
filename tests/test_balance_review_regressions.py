from dataclasses import replace
from datetime import date
from decimal import Decimal
import unittest

from app.models import ReconciliationResult
from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


START = "01.09.2026 0:00:00"
END = "30.09.2026 23:59:59"
BASE = {
    "Период": START, "Документ": "", "Номенклатура": "Товар",
    "НоменклатураКод": "N1", "НоменклатураАртикул": "A1",
    "Склад": "Основной", "СкладКод": "W1", "Организация": "ООО Альфа",
    "ОрганизацияИНН": "111", "ЕдиницаИзмерения": "шт",
    "КоличествоНачальныйОстаток": 0, "СтоимостьНачальныйОстаток": 0,
    "КоличествоОборот": 0, "СтоимостьОборот": 0,
    "КоличествоКонечныйОстаток": 0, "СтоимостьКонечныйОстаток": 0,
}


def raw(period=START, document="", **values):
    row = BASE.copy()
    row.update({"Период": period, "Документ": document})
    row.update(values)
    return row


def without_organization(row):
    return {key: value for key, value in row.items() if key not in {"Организация", "ОрганизацияИНН"}}


def report(trade_raw, tax_raw):
    trade = [replace(row, warehouse_mapping_id="main") for row in normalize_rows(trade_raw, "Sep 2026")]
    tax = [replace(row, warehouse_mapping_id="main") for row in normalize_rows(tax_raw, "Sep 2026")]
    result = reconcile(
        trade, {"БУХ": tax}, "01.09.2026–30.09.2026",
        )
    result = ReconciliationResult(
        result.discrepancies, result.summaries,
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
        notices=result.notices,
    )
    return workbook_rows(build_action_excel_report(result))


def data_rows(sheets, sheet="Расхождения остатков"):
    return sheets[sheet][1:]


def numbers(row):
    # D:F are the established UT, BUH, difference columns.
    return tuple(Decimal(value or "0") for value in row[3:6])


class BalanceReviewRegressionTests(unittest.TestCase):
    def test_explicit_start_and_end_mismatches_are_separate_controls(self):
        rows = data_rows(report(
            [raw(КоличествоНачальныйОстаток=1), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=1)],
            [raw(КоличествоНачальныйОстаток=0), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=0)],
        ))
        self.assertEqual(sorted(numbers(row)[2] for row in rows), [Decimal("-1"), Decimal("-1")])
        self.assertEqual(len(rows), 2)

    def test_agreeing_end_does_not_create_an_extra_control(self):
        rows = data_rows(report(
            [raw(КоличествоНачальныйОстаток=1), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=0)],
            [raw(КоличествоНачальныйОстаток=0), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=0)],
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(numbers(rows[0])[2], Decimal("-1"))

    def test_documented_expense_row_can_supply_period_end_balance(self):
        expense = "Дополнительные расходы 1 от 30.09.2026 12:00:00"
        rows = data_rows(report(
            [raw(КоличествоНачальныйОстаток=1), raw(END, expense, КоличествоНачальныйОстаток=7, КоличествоКонечныйОстаток=7)],
            [raw(КоличествоНачальныйОстаток=1), raw(END, КоличествоНачальныйОстаток=5, КоличествоКонечныйОстаток=5)],
        ))
        self.assertTrue(any(numbers(row) == (Decimal("7"), Decimal("5"), Decimal("-2")) for row in rows), rows)

    def test_balance_rows_keep_same_item_separate_by_organization(self):
        alpha = BASE | {"Организация": "Альфа", "ОрганизацияИНН": "111", "КоличествоНачальныйОстаток": 1}
        beta = BASE | {"Организация": "Бета", "ОрганизацияИНН": "222", "КоличествоНачальныйОстаток": 10}
        rows = data_rows(report(
            [alpha, beta],
            [alpha | {"КоличествоНачальныйОстаток": 2}, beta | {"КоличествоНачальныйОстаток": 12}],
        ))
        self.assertEqual(len(rows), 2)
        self.assertEqual({numbers(row)[2] for row in rows}, {Decimal("1"), Decimal("2")})

    def test_missing_end_is_summary_notice_without_manual_document_rows(self):
        sheets = report(
            [without_organization(raw(END, КоличествоНачальныйОстаток=2, КоличествоКонечныйОстаток=2))],
            [without_organization(raw())],
        )
        summary = " ".join(str(cell) for row in sheets["Итоги"] for cell in row)
        self.assertIn("Ограничения сверки", summary)
        self.assertIn("Конечный остаток не проверен", summary)
        self.assertEqual(data_rows(sheets, "Проверить вручную"), [])
        self.assertFalse(any(numbers(row)[1] == 0 and numbers(row)[2] in {Decimal("-2"), Decimal("2")} for row in data_rows(sheets)))

    def test_missing_physical_warehouse_end_does_not_become_partial_sum(self):
        sheets = report(
            [raw(СкладКод='W1', КоличествоНачальныйОстаток=2),
             raw(СкладКод='W2', КоличествоНачальныйОстаток=3),
             raw(END, СкладКод='W1', КоличествоКонечныйОстаток=2)],
            [raw(СкладКод='B1', КоличествоНачальныйОстаток=5),
             raw(END, СкладКод='B1', КоличествоКонечныйОстаток=5)],
        )
        self.assertFalse(any('Конечный' in str(row) for row in data_rows(sheets)))
        summary = " ".join(str(cell) for row in sheets["Итоги"] for cell in row)
        self.assertIn('Конечный остаток не проверен', summary)
        self.assertFalse(data_rows(sheets, 'Проверить вручную'))

    def test_single_aggregate_snapshot_without_end_does_not_invent_final_comparison(self):
        sheets = report(
            [without_organization(raw(КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=2))],
            [without_organization(raw(КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=2))],
        )
        summary = " ".join(str(cell) for row in sheets["Итоги"] for cell in row)
        self.assertIn("Конечный остаток не проверен", summary)
        self.assertFalse(data_rows(sheets, "Проверить вручную"))
        self.assertFalse(any("Конечный" in " ".join(map(str, row)) and numbers(row)[1] == 0 for row in data_rows(sheets)))

    def test_equal_final_balance_does_not_hide_document_movement_difference(self):
        doc = "Поступление (акт, накладная, УПД) 41 от 30.09.2026 12:00:00"
        sheets = report(
            [raw(КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=1), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=1), raw("30.09.2026 12:00:00", doc, КоличествоОборот=1)],
            [raw(КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=1), raw(END, КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=1), raw("30.09.2026 12:00:00", doc, КоличествоОборот=2)],
        )
        self.assertTrue(data_rows(sheets, "Лишние в БУХ"), sheets["Лишние в БУХ"])

    def test_single_start_row_with_different_closing_totals_has_no_proven_end(self):
        sheets = report(
            [without_organization(raw(КоличествоНачальныйОстаток=1, КоличествоКонечныйОстаток=2))],
            [without_organization(raw(КоличествоНачальныйОстаток=0, КоличествоКонечныйОстаток=3))],
        )
        summary = " ".join(str(cell) for row in sheets["Итоги"] for cell in row)
        self.assertIn("Конечный остаток не проверен", summary)
        self.assertFalse(any("Конечный" in " ".join(map(str, row)) for row in data_rows(sheets)))
        self.assertEqual(len(data_rows(sheets)), 1)  # The opening difference is real.

    def test_duplicate_boundary_is_summary_notice_not_manual_product_rows(self):
        duplicates = [raw(КоличествоНачальныйОстаток=2), raw(КоличествоНачальныйОстаток=3)]
        unique = [raw(КоличествоНачальныйОстаток=5)]
        for trade, tax in ((duplicates, unique), (unique, duplicates)):
            with self.subTest(duplicate_side='trade' if trade is duplicates else 'tax'):
                sheets = report(trade, tax)
                summary = " ".join(str(cell) for row in sheets["Итоги"] for cell in row)
                self.assertIn('Ограничения сверки', summary)
                self.assertIn('Затронуто строк', summary)
                self.assertTrue(sheets['Проверить вручную'] and not data_rows(sheets, 'Проверить вручную'))
                self.assertEqual(len(data_rows(sheets)), 0)


if __name__ == "__main__":
    unittest.main()
