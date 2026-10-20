"""Regression contracts for the live UT and accounting export schemas.

The UT export omits unit, package, and organization metadata. The accounting
export includes an organization/INN but also omits unit metadata. These tests
exercise the payloads through normalization, reconciliation, and the action
workbook so schema omissions cannot silently turn matched data into manual work.
"""

from datetime import date
from dataclasses import replace
from decimal import Decimal
import json
from unittest import TestCase

from app.normalization import normalize_rows
from app.reconciliation import (
    AMBIGUOUS_MATCH,
    BALANCE_QUANTITY_MISMATCH,
    DOCUMENT_QUANTITY_MISMATCH,
    reconcile,
)
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


PERIOD = "01.07.2026 0:00:00"
DOCUMENT = "Реализация товаров и услуг ДЖ000040154 от 02.07.2026 10:00:00"


def _movement(*, tax: bool, quantity: str, unit: str | None = None, inn: str | None = None):
    record = {
        "Период": PERIOD,
        "Документ": DOCUMENT,
        "Номенклатура": "Товар 1",
        "НоменклатураКод": "ITEM-001",
        "Склад": "Основной склад",
        "СкладКод": "WH-001",
        "КоличествоНачальныйОстаток": "0",
        "СтоимостьНачальныйОстаток": "0",
        "КоличествоОборот": quantity,
        "СтоимостьОборот": "0",
        "КоличествоКонечныйОстаток": quantity,
        "СтоимостьКонечныйОстаток": "0",
        "НомерСФВыданный": "ДЖ000001589",
    }
    if unit is not None:
        record["ЕдиницаИзмерения"] = unit
    if tax:
        record["Организация"] = "Организация А"
        record["ОрганизацияИНН"] = inn or "1330434664"
        record["Документ"] = "Реализация (акт, накладная, УПД) 1589 от 02.07.2026 00:00:00"
    return record


def _normalized(records):
    # JSON round trip mirrors the live API payload boundary.
    return normalize_rows(json.loads(json.dumps(records, ensure_ascii=False)), "Июль 2026")


class LiveSchemaReconciliationTests(TestCase):
    def test_organization_in_nonboundary_movement_keeps_stock_scope_ambiguous(self):
        trade = _movement(tax=False, quantity="0") | {
            "Документ": "", "КоличествоНачальныйОстаток": "10",
        }
        tax = _movement(tax=True, quantity="0", inn="111") | {
            "Документ": "", "КоличествоНачальныйОстаток": "11",
        }
        other_organization = _movement(tax=True, quantity="-1", inn="222") | {
            "Период": "15.07.2026 12:00:00",
        }
        result = reconcile(_normalized([trade]), {"БУХ": _normalized([tax, other_organization])},
                           "Июль 2026", start_date=date(2026, 7, 1), end_date=date(2026, 7, 31))
        self.assertFalse(any(d.discrepancy_type == BALANCE_QUANTITY_MISMATCH for d in result.discrepancies))

    def test_sparse_ut_and_tax_metadata_still_match_sale_and_report_quantity_delta(self):
        trade_record = _movement(tax=False, quantity="-537")
        tax_record = _movement(tax=True, quantity="-550")
        self.assertNotIn("ЕдиницаИзмерения", trade_record)
        self.assertNotIn("ОрганизацияИНН", trade_record)
        self.assertIn("ОрганизацияИНН", tax_record)
        self.assertNotIn("ЕдиницаИзмерения", tax_record)

        trade = _normalized([trade_record])
        tax = _normalized([tax_record])
        result = reconcile(trade, {"БУХ": tax}, "Июль 2026")

        self.assertEqual(result.summaries[0].matched_documents, 1)
        quantity_differences = [d for d in result.discrepancies
                                if d.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH]
        self.assertEqual(len(quantity_differences), 1)
        self.assertEqual(quantity_differences[0].trade_row.turnover_quantity, Decimal("-537"))
        self.assertEqual(quantity_differences[0].tax_row.turnover_quantity, Decimal("-550"))
        sheets = workbook_rows(build_action_excel_report(result))
        extra_headers = sheets["Лишние в БУХ"][0]
        extra_rows = [dict(zip(extra_headers, row)) for row in sheets["Лишние в БУХ"][1:]]
        self.assertTrue(any(row["Количество УТ"] == "537"
                            and row["Количество БУХ"] == "550"
                            and row["Разница БУХ − УТ"] == "13" for row in extra_rows))

    def test_sparse_metadata_does_not_hide_opening_and_closing_stock_differences(self):
        trade_records = [
            _movement(tax=False, quantity="0") | {
                "Документ": "", "Период": "01.07.2026 0:00:00",
                "КоличествоНачальныйОстаток": "10", "КоличествоКонечныйОстаток": "10",
            },
            _movement(tax=False, quantity="0") | {
                "Документ": "", "Период": "31.07.2026 23:59:59",
                "КоличествоНачальныйОстаток": "20", "КоличествоКонечныйОстаток": "20",
            },
        ]
        tax_records = [
            _movement(tax=True, quantity="0") | {
                "Документ": "", "Период": "01.07.2026 0:00:00",
                "КоличествоНачальныйОстаток": "11", "КоличествоКонечныйОстаток": "11",
            },
            _movement(tax=True, quantity="0") | {
                "Документ": "", "Период": "31.07.2026 23:59:59",
                "КоличествоНачальныйОстаток": "22", "КоличествоКонечныйОстаток": "22",
            },
        ]
        result = reconcile(
            _normalized(trade_records), {"БУХ": _normalized(tax_records)}, "Июль 2026",
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        )

        differences = [d for d in result.discrepancies
                       if d.discrepancy_type == BALANCE_QUANTITY_MISMATCH]
        self.assertEqual({d.balance_point for d in differences}, {"opening", "closing"})
        self.assertEqual(len(differences), 2)
        sheets = workbook_rows(build_action_excel_report(result))
        balance_headers = sheets["Расхождения остатков"][0]
        balance_rows = [dict(zip(balance_headers, row)) for row in sheets["Расхождения остатков"][1:]]
        self.assertEqual(len(balance_rows), 2)
        self.assertEqual({row["Расхождение"] for row in balance_rows}, {"1", "2"})

    def test_explicit_unit_conflict_remains_manual(self):
        trade = _normalized([_movement(tax=False, quantity="-537", unit="шт")])
        tax = _normalized([_movement(tax=True, quantity="-550", unit="кг")])

        result = reconcile(trade, {"БУХ": tax}, "Июль 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_multiple_known_tax_organizations_remain_manual(self):
        trade = _normalized([_movement(tax=False, quantity="-537")])
        tax = _normalized([
            _movement(tax=True, quantity="-550", inn="1330434664"),
            _movement(tax=True, quantity="-550", inn="3164864916"),
        ])

        result = reconcile(trade, {"БУХ": tax}, "Июль 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_multiple_known_tax_organizations_do_not_net_sparse_ut_stock(self):
        trade_records = [
            _movement(tax=False, quantity="0") | {
                "Документ": "", "Период": period,
                "КоличествоНачальныйОстаток": "10", "КоличествоКонечныйОстаток": "10",
            }
            for period in ("01.07.2026 0:00:00", "31.07.2026 23:59:59")
        ]
        tax_records = [
            _movement(tax=True, quantity="0", inn=inn) | {
                "Документ": "", "Период": period,
                "КоличествоНачальныйОстаток": "5", "КоличествоКонечныйОстаток": "5",
            }
            for inn in ("1330434664", "3164864916")
            for period in ("01.07.2026 0:00:00", "31.07.2026 23:59:59")
        ]
        result = reconcile(
            _normalized(trade_records), {"БУХ": _normalized(tax_records)}, "Июль 2026",
            start_date=date(2026, 7, 1), end_date=date(2026, 7, 31),
        )

        self.assertFalse(any(d.discrepancy_type == BALANCE_QUANTITY_MISMATCH
                             for d in result.discrepancies))
        self.assertFalse(result.discrepancies)
        self.assertEqual(len(result.notices), 2)
        self.assertTrue(all(notice.affected_rows > 0 for notice in result.notices))
        self.assertTrue(any("начальный" in notice.reason.lower() for notice in result.notices))
        self.assertTrue(any("конечный" in notice.reason.lower() for notice in result.notices))

    def test_import_with_absent_ut_organization_can_use_single_known_tax_inn(self):
        trade_record = {
            "Период": PERIOD, "Документ": "Поступление товаров и услуг ДЖ1 от 02.07.2026 10:00:00",
            "Номенклатура": "Товар 1", "НоменклатураКод": "ITEM-001",
            "Склад": "Импорт", "СкладКод": "БД-000003", "ЕдиницаИзмерения": "шт",
            "КоличествоНачальныйОстаток": "0", "СтоимостьНачальныйОстаток": "0",
            "КоличествоОборот": "5", "СтоимостьОборот": "0",
            "КоличествоКонечныйОстаток": "5", "СтоимостьКонечныйОстаток": "0",
            "ПартияБух": "Партия 1", "ЗаказПоставщику": "Заказ 1",
            "КонтрагентДляСверки": "Поставщик А", "ЭтоИмпорт": "Да",
        }
        tax_record = trade_record | {
            "Документ": "Поступление товаров и услуг 2 от 02.07.2026 10:00:00",
            "Организация": "Организация А", "ОрганизацияИНН": "1330434664",
            "ПартияБух": "", "Комментарий": "Партия 1",
        }
        trade = [replace(row, warehouse_mapping_id="import") for row in _normalized([trade_record])]
        tax = [replace(row, warehouse_mapping_id="import") for row in _normalized([tax_record])]

        result = reconcile(trade, {"БУХ2019": tax}, "Июль 2026")

        self.assertEqual(result.summaries[0].matched_import_groups, 1)
        self.assertFalse(any(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_with_explicitly_blank_ut_organization_remains_manual(self):
        trade_record = {
            "Период": PERIOD, "Документ": "Поступление товаров и услуг ДЖ1 от 02.07.2026 10:00:00",
            "Номенклатура": "Товар 1", "НоменклатураКод": "ITEM-001",
            "Склад": "Импорт", "СкладКод": "БД-000003", "ЕдиницаИзмерения": "шт",
            "КоличествоНачальныйОстаток": "0", "СтоимостьНачальныйОстаток": "0",
            "КоличествоОборот": "5", "СтоимостьОборот": "0",
            "КоличествоКонечныйОстаток": "5", "СтоимостьКонечныйОстаток": "0",
            "ПартияБух": "Партия 1", "ЗаказПоставщику": "Заказ 1",
            "КонтрагентДляСверки": "Поставщик А", "ЭтоИмпорт": "Да",
            "Организация": "", "ОрганизацияИНН": "",
        }
        tax_record = trade_record | {
            "Документ": "Поступление товаров и услуг 2 от 02.07.2026 10:00:00",
            "Организация": "Организация А", "ОрганизацияИНН": "1330434664",
            "ПартияБух": "", "Комментарий": "Партия 1",
        }
        trade = [replace(row, warehouse_mapping_id="import") for row in _normalized([trade_record])]
        tax = [replace(row, warehouse_mapping_id="import") for row in _normalized([tax_record])]

        result = reconcile(trade, {"БУХ2019": tax}, "Июль 2026")

        self.assertTrue(any(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))
