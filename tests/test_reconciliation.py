import base64
import io
import zipfile
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from app.config import OneCBaseConfig, WarehouseMappingConfig
from app.document_matching import is_reconcilable_document, parse_document
from app.models import LedgerRow, ReconciliationResult, Warehouse
from app.normalization import normalize_row, normalize_rows
from app.onec_client import OneCClient, normalize_warehouses
from app.reconciliation import (
    AMBIGUOUS_MATCH,
    BALANCE_UNCHECKED,
    BALANCE_QUANTITY_MISMATCH,
    BUH_ONLY_DOCUMENT,
    DOCUMENT_QUANTITY_MISMATCH,
    DUPLICATE_IN_TAX,
    EXTRA_BALANCE_IN_TAX,
    EXTRA_IN_TAX,
    MISSING_BALANCE_IN_TAX,
    MISSING_IN_TAX,
    DOCUMENT_CONTENT_MATCH,
    reconcile as app_reconcile,
)
from app.reporting import build_action_excel_report, build_excel_report
from scripts.verify_onec import workbook_rows
from main import ReconcileRequest, _fetch_rows_for_base, _format_period


def row(
    nomenclature: str,
    document: str = "",
    period: str = "01.08.2026 0:00:00",
    opening_quantity: str = "10.00",
    opening_amount: str = "100.00",
    turnover_quantity: str = "0.00",
    turnover_amount: str = "0.00",
    closing_quantity: str = "10.00",
    closing_amount: str = "100.00",
) -> LedgerRow:
    return LedgerRow(
        period=period,
        document=document,
        nomenclature=nomenclature,
        opening_quantity=Decimal(opening_quantity),
        opening_amount=Decimal(opening_amount),
        turnover_quantity=Decimal(turnover_quantity),
        turnover_amount=Decimal(turnover_amount),
        closing_quantity=Decimal(closing_quantity),
        closing_amount=Decimal(closing_amount),
        unit="шт",
    )


reconcile = app_reconcile


def bounded_snapshots(rows):
    """Represent each synthetic balance at both explicit month boundaries."""
    return [
        replace(item, period=period)
        for item in rows
        for period in ("01.08.2026 0:00:00", "31.08.2026 23:59:59")
    ]


def workbook_quantity_values(sheets, sheet_name):
    headers = sheets[sheet_name][0]
    values = dict(zip(headers, sheets[sheet_name][1]))
    return [values[name] for name in ("Количество УТ", "Количество БУХ", "Разница БУХ − УТ")]


class NormalizationTests(TestCase):
    def test_keeps_new_invoice_fields_and_supplier_order(self) -> None:
        normalized = normalize_row({
            "Период": "02.08.2026 10:00:00",
            "Документ": "Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
            "Номенклатура": "Товар",
            "КоличествоНачальныйОстаток": 0, "СтоимостьНачальныйОстаток": 0,
            "КоличествоОборот": -537, "СтоимостьОборот": 0,
            "КоличествоКонечныйОстаток": 0, "СтоимостьКонечныйОстаток": 0,
            "НомерСФВыданный": "ДЖ000001589", "НомерСФПолученный": "",
            "ПартияБух": "42", "Заказ": "Заказ поставщику 7",
        }, "Август 2026")

        self.assertEqual(normalized.issued_invoice_number, "ДЖ000001589")
        self.assertEqual(normalized.received_invoice_number, "")
        self.assertEqual(normalized.supplier_order, "Заказ поставщику 7")
        self.assertIn("received_invoice_number", normalized.present_fields)

    def test_distinguishes_absent_invoice_from_empty_and_zero(self) -> None:
        base = {
            "period": "01.08.2026", "document": "", "nomenclature": "Товар",
            "opening_quantity": 0, "opening_amount": 0, "turnover_quantity": 0,
            "turnover_amount": 0, "closing_quantity": 0, "closing_amount": 0,
        }
        absent = normalize_row(base, "Август 2026")
        empty = normalize_row(base | {"НомерСФВыданный": ""}, "Август 2026")
        zero = normalize_row(base | {"НомерСФВыданный": 0}, "Август 2026")

        self.assertNotIn("issued_invoice_number", absent.present_fields)
        self.assertIn("issued_invoice_number", empty.present_fields)
        self.assertEqual(empty.issued_invoice_number, "")
        self.assertIn("issued_invoice_number", zero.present_fields)
        self.assertEqual(zero.issued_invoice_number, "0")

    def test_normalizes_stock_turnover_fields_and_amounts(self) -> None:
        normalized = normalize_row(
            {
                "Период": " 01.08.2026 0:00:00 ",
                "Документ": "",
                "Номенклатура": "  Товар  1 ",
                "КоличествоНачальныйОстаток": "1 000,005",
                "СтоимостьНачальныйОстаток": "2 000,004",
                "КоличествоОборот": "-10",
                "СтоимостьОборот": "-20,1",
                "КоличествоКонечныйОстаток": "990,005",
                "СтоимостьКонечныйОстаток": "1 979,904",
            },
            default_period="I 2024",
        )

        self.assertEqual(normalized.document, "")
        self.assertEqual(normalized.nomenclature, "Товар 1")
        self.assertEqual(normalized.opening_quantity, Decimal("1000.005"))
        self.assertEqual(normalized.opening_amount, Decimal("2000.004"))
        self.assertEqual(normalized.closing_amount, Decimal("1979.904"))

    def test_parses_document_type_number_and_timestamp(self) -> None:
        identity = parse_document(
            "Передача товаров филиалу 2146741929 от 02.03.2026 8:48:28"
        )

        self.assertEqual(identity.family, "transfer")
        self.assertEqual(identity.number, "2146741929")
        self.assertEqual(identity.timestamp, "2026-03-02T08:48:28")

    def test_document_allowlist_normalizes_confirmed_aliases(self) -> None:
        aliases = {
            "Передача товаров филиалу (ПТФ)": "transfer",
            "Перемещение": "transfer",
            "Поступление товаров и услуг": "goods_receipt",
            "Поступление (акты, накладные, УПД)": "goods_receipt",
            "Поступление (акт, накладная, УПД)": "goods_receipt",
            "Оприходование": "receipt",
            "Списание товаров": "write_off",
            "Реализация (акт, накладная, УПД)": "sale",
        }
        for document_type, family in aliases.items():
            with self.subTest(document_type=document_type):
                document = f"{document_type} 1 от 01.03.2026 10:00:00"
                self.assertEqual(parse_document(document).family, family)
                self.assertTrue(is_reconcilable_document(document))

    def test_normalizes_import_party_and_counterparty_from_each_export(self) -> None:
        original = {
            "Период": "01.08.2026 0:00:00",
            "Документ": "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00",
            "Номенклатура": "Товар",
            "КоличествоНачальныйОстаток": 0,
            "СтоимостьНачальныйОстаток": 0,
            "КоличествоОборот": 20,
            "СтоимостьОборот": 0,
            "КоличествоКонечныйОстаток": 20,
            "СтоимостьКонечныйОстаток": 0,
            "ПартияБух": "  Партия  25  ",
            "НаименованиеОтчетКонтрагента": "  Поставщик  А ",
            "ЭтоИмпорт": "Да",
        }
        trade = normalize_row(original, "I 2024")
        tax = normalize_row(
            original | {
                "ПартияБух": None,
                "НаименованиеОтчетКонтрагента": None,
                "Комментарий": "Партия 25",
                "НаименованиеДляДокументов": "Поставщик А",
            },
            "I 2024",
        )

        self.assertEqual((trade.party, trade.counterparty, trade.is_import), ("Партия 25", "Поставщик А", "Да"))
        self.assertEqual((tax.party, tax.counterparty), ("", "Поставщик А"))
        self.assertEqual(tax.party_comment, "Партия 25")

        for party_value in (None, ""):
            with self.subTest(party_value=party_value):
                incomplete_trade = normalize_row(original | {
                    "ПартияБух": party_value, "Комментарий": "42",
                    "НомерНакладнойУПД": "42/T11", "ЗаказПоставщику": "Заказ 1",
                }, "I 2024")
                self.assertEqual(incomplete_trade.party, "")
                self.assertEqual(incomplete_trade.party_comment, "42")

    def test_requested_simple_document_types_are_reconciled_and_others_are_excluded(self) -> None:
        for document_type in (
            "Комплектация номенклатуры",
            "Требование-накладная",
            "Безвозмездная передача",
        ):
            with self.subTest(document_type=document_type):
                document = f"{document_type} 1 от 01.03.2026 10:00:00"
                self.assertTrue(is_reconcilable_document(document))
        for document_type in (
            "Поступление дополнительных расходов",
            "Заказ покупателя",
            "Неизвестный документ",
        ):
            with self.subTest(document_type=document_type):
                document = f"{document_type} 1 от 01.03.2026 10:00:00"
                self.assertFalse(is_reconcilable_document(document))

    def test_false_accounting_flags_do_not_exclude_confirmed_document(self) -> None:
        normalized = normalize_row(
            {
                "Период": "01.03.2026 10:00:00",
                "Документ": "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
                "Номенклатура": "Товар 1",
                "КоличествоНачальныйОстаток": "0",
                "СтоимостьНачальныйОстаток": "0",
                "КоличествоОборот": "-1",
                "СтоимостьОборот": "-100",
                "КоличествоКонечныйОстаток": "-1",
                "СтоимостьКонечныйОстаток": "-100",
                "УправленческийУчет": False,
                "БухгалтерскийУчет": False,
                "НалоговыйУчет": False,
            },
            default_period="I 2024",
        )

        result = reconcile([normalized], {"БУХ": []}, "I 2024")

        self.assertEqual(result.discrepancies[0].discrepancy_type, AMBIGUOUS_MATCH)

    def test_normalizes_movement_endpoints_and_document_ids(self) -> None:
        normalized = normalize_row(
            {
                "period": "01.08.2026 0:00:00",
                "document": "Документ 1 от 01.08.2026 0:00:00",
                "nomenclature": "Товар",
                "opening_quantity": "0",
                "opening_amount": "0",
                "turnover_quantity": "1",
                "turnover_amount": "10",
                "closing_quantity": "1",
                "closing_amount": "10",
                "СкладОтправитель": "Секция 1",
                "СкладПолучатель": "Секция 2",
                "ДокументGUID": "source-id",
                "ДокументОснованиеGUID": "trade-id",
            },
            default_period="period",
        )

        self.assertEqual(normalized.sender_warehouse, "Секция 1")
        self.assertEqual(normalized.receiver_warehouse, "Секция 2")
        self.assertEqual(normalized.document_id, "source-id")
        self.assertEqual(normalized.source_document_id, "trade-id")


class RequestValidationTests(TestCase):
    def test_accepts_valid_date_range(self) -> None:
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))

        self.assertEqual(payload.start_date, date(2024, 1, 1))
        self.assertEqual(payload.end_date, date(2024, 1, 31))

    def test_normalizes_warehouses(self) -> None:
        payload = ReconcileRequest(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            warehouses=[" Основной  склад ", "", "Склад 2", "Основной склад"],
        )

        self.assertEqual(payload.warehouses, ["Основной склад", "Склад 2"])

    def test_rejects_end_date_before_start_date(self) -> None:
        with self.assertRaises(ValueError):
            ReconcileRequest(start_date=date(2024, 2, 1), end_date=date(2024, 1, 31))

    def test_formats_period_as_inclusive_date_range(self) -> None:
        self.assertEqual(
            _format_period(date(2024, 1, 1), date(2024, 1, 31)),
            "01.01.2024–31.01.2024",
        )

    def test_multiple_mappings_use_one_full_export(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
            replace(row("Товар 3"), warehouse_code="W3"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mappings = [
            WarehouseMappingConfig("first", "First", {"trade": "W1"}),
            WarehouseMappingConfig("second", "Second", {"trade": "W2"}),
        ]

        result = _fetch_rows_for_base(client, base, payload, mappings)

        self.assertEqual(len(result), 2)
        self.assertEqual(
            [(item.warehouse_code, item.warehouse_mapping_id) for item in result],
            [("W1", "first"), ("W2", "second")],
        )
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)

    def test_one_mapping_can_load_multiple_physical_warehouses(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mapping = WarehouseMappingConfig("main", "Main", {"trade": ["W1", "W2"]})

        result = _fetch_rows_for_base(client, base, payload, [mapping])

        self.assertEqual(len(result), 2)
        self.assertEqual({item.warehouse_mapping_id for item in result}, {"main"})
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)

    def test_logical_mapping_fetches_all_physical_warehouses(self) -> None:
        client = Mock()
        client.fetch_rows.return_value = [
            replace(row("Товар 1"), warehouse_code="W1"),
            replace(row("Товар 2"), warehouse_code="W2"),
        ]
        base = OneCBaseConfig(name="Trade", url="https://internal.internal.example.invalid/endpoint", base_id="trade")
        payload = ReconcileRequest(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
        mapping = WarehouseMappingConfig("main", "Main", {"trade": ["W1", "W2"]})

        result = _fetch_rows_for_base(client, base, payload, [mapping])

        self.assertEqual(len(result), 2)
        self.assertEqual([item.warehouse_code for item in result], ["W1", "W2"])
        client.fetch_rows.assert_called_once_with(base, payload.start_date, payload.end_date)


class OneCClientTests(TestCase):
    @patch("app.onec_client.requests.get")
    def test_sends_start_and_end_date_params(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_get.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(name="Налоговая", url="https://internal.internal.example.invalid/endpoint/export"),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
        )

        mocked_get.assert_called_once()
        self.assertEqual(
            mocked_get.call_args.kwargs["params"],
            [("start_date", "2024-01-01"), ("end_date", "2024-01-31")],
        )

    @patch("app.onec_client.requests.get")
    def test_sends_single_warehouse_param(self, mocked_get: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_get.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(name="Налоговая", url="https://internal.internal.example.invalid/endpoint/export", supports_warehouse_filter=True),
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            warehouse=" Основной  склад ",
        )

        self.assertEqual(
            mocked_get.call_args.kwargs["params"],
            [
                ("start_date", "2024-01-01"),
                ("end_date", "2024-01-31"),
                ("warehouse", "Основной склад"),
            ],
        )

    @patch("app.onec_client.requests.post")
    def test_sends_postman_json_period_payload(self, mocked_post: Mock) -> None:
        response = Mock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        mocked_post.return_value = response

        client = OneCClient(timeout_seconds=3)
        client.fetch_rows(
            OneCBaseConfig(
                name="Trade",
                url="https://internal.internal.example.invalid/endpoint/stocksturnover",
                username="user",
                REDACTED="REDACTED",
                request_method="POST",
                date_format="%Y%m%d",
                period_begin_key="periodBegin",
                period_end_key="periodEnd",
            ),
            start_date=date(2026, 1, 24),
            end_date=date(2026, 1, 31),
        )

        mocked_post.assert_called_once()
        self.assertEqual(
            mocked_post.call_args.kwargs["json"],
            {"periodBegin": "20260124", "periodEnd": "20260131"},
        )
        expected_token = base64.b64encode("user:REDACTED".encode("utf-8")).decode("ascii")
        self.assertEqual(mocked_post.call_args.kwargs["headers"], {"Authorization": f"Basic {expected_token}"})

    def test_rejects_warehouse_without_code(self) -> None:
        with self.assertRaises(ValueError):
            normalize_warehouses(["Основной склад"])

    def test_normalizes_warehouse_object_list(self) -> None:
        self.assertEqual(
            normalize_warehouses(
                {
                    "data": [
                        {"name": "Основной склад", "code": "W1"},
                        {"Название": "Склад 2", "id": "W2"},
                        {"Склад": "Склад 3", "Код": "W3", "Группа": "Группа 1"},
                    ]
                }
            ),
            [
                Warehouse("W1", "Основной склад"),
                Warehouse("W2", "Склад 2"),
                Warehouse("W3", "Склад 3", "Группа 1"),
            ],
        )


class ReconciliationTests(TestCase):
    def test_no_discrepancies_within_tolerance(self) -> None:
        result = reconcile(
            trade_rows=bounded_snapshots([row("Товар 1", opening_amount="100.00")]),
            tax_exports={"Бухгалтерия": bounded_snapshots([row("Товар 1", opening_amount="100.01")])},
            period="I 2024",
            amount_tolerance=Decimal("0.01"),
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )

        self.assertEqual(result.discrepancies, [])

    def test_detects_missing_balance_in_tax_base(self) -> None:
        result = reconcile(
            trade_rows=bounded_snapshots([row("Товар 1")]),
            tax_exports={"Бухгалтерия": []},
            period="I 2024",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )

        self.assertEqual(result.discrepancies, [])
        self.assertEqual({notice.reason.split()[0] for notice in result.notices}, {"Начальный", "Конечный"})
        self.assertTrue(all(notice.affected_rows == 1 for notice in result.notices))
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Проверить вручную"]), 1)
        self.assertIn("Ограничения сверки", str(sheets["Итоги"]))

    def test_detects_extra_balance_in_tax_base(self) -> None:
        result = reconcile(
            trade_rows=[],
            tax_exports={"Бухгалтерия": bounded_snapshots([row("Товар 1")])},
            period="I 2024",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )

        self.assertEqual(result.discrepancies, [])
        self.assertEqual({notice.reason.split()[0] for notice in result.notices}, {"Начальный", "Конечный"})
        self.assertTrue(all(notice.affected_rows == 1 for notice in result.notices))

    def test_snapshot_aggregation_does_not_cancel_different_organizations(self) -> None:
        first = replace(row("Товар", closing_quantity="10"), nomenclature_code="N1",
                        warehouse_mapping_id="main", warehouse_code="T1",
                        organization_inn="111")
        second = replace(first, warehouse_code="T2", organization_inn="222",
                         closing_quantity=Decimal("0"))
        tax_first = replace(first, warehouse_code="B1", closing_quantity=Decimal("0"))
        tax_second = replace(second, warehouse_code="B2", closing_quantity=Decimal("10"))

        result = reconcile(
            bounded_snapshots([first, second]),
            {"БУХ2019": bounded_snapshots([tax_first, tax_second])},
            "Август 2026",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )
        self.assertEqual(len(result.discrepancies), 2)
        self.assertTrue(all(d.discrepancy_type == BALANCE_QUANTITY_MISMATCH for d in result.discrepancies))
        self.assertEqual({d.trade_row.organization_inn for d in result.discrepancies}, {"111", "222"})
        self.assertEqual({d.balance_trade_quantity - d.balance_tax_quantity for d in result.discrepancies},
                         {Decimal("-10.00"), Decimal("10.00")})

    def test_duplicate_snapshot_rows_remain_visible_as_manual_source_cases(self) -> None:
        result = reconcile(
            trade_rows=[row("Товар 1"), row("Товар 1")],
            tax_exports={"Бухгалтерия": [row("Товар 1")]},
            period="I 2024",
        )

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(len(result.notices), 1)
        self.assertEqual(result.notices[0].affected_rows, 3)
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Проверить вручную"]), 1)
        self.assertIn("3", str(sheets["Итоги"]))

    def test_ignores_amount_only_balance_mismatch(self) -> None:
        result = reconcile(
            trade_rows=bounded_snapshots([row("Товар 1", closing_amount="100.00")]),
            tax_exports={"Бухгалтерия": bounded_snapshots([row("Товар 1", closing_amount="100.02")])},
            period="I 2024",
            amount_tolerance=Decimal("0.01"),
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )

        self.assertEqual(result.discrepancies, [])

    def test_document_compares_turnover_but_not_running_balances(self) -> None:
        trade = replace(row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
            opening_quantity="10",
            opening_amount="100",
            turnover_quantity="-2",
            turnover_amount="-20",
            closing_quantity="8",
            closing_amount="80",
        ), issued_invoice_number="ДЖ1")
        tax = replace(
            trade,
            opening_quantity=Decimal("1000"),
            opening_amount=Decimal("9000"),
            closing_quantity=Decimal("998"),
            closing_amount=Decimal("8980"),
        )

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])

    def test_document_reports_quantity_and_ignores_amount(self) -> None:
        trade = replace(row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00",
            turnover_quantity="-2",
            turnover_amount="-20",
        ), issued_invoice_number="ДЖ1")
        tax = replace(trade, turnover_quantity=Decimal("-3"), turnover_amount=Decimal("-30"))

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(
            {item.discrepancy_type for item in result.discrepancies},
            {DOCUMENT_QUANTITY_MISMATCH},
        )

    def test_repeated_item_lines_are_aggregated_before_comparison(self) -> None:
        document = "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00"
        first = replace(row("Товар 1", document, turnover_quantity="-1", turnover_amount="-10"), nomenclature_code="N1", issued_invoice_number="ДЖ1")
        second = replace(first, turnover_quantity=Decimal("-2"), turnover_amount=Decimal("-20"))
        tax = replace(first, turnover_quantity=Decimal("-3"), turnover_amount=Decimal("-30"))

        result = reconcile([first, second], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])

    def test_import_receipts_match_by_party_and_counterparty_across_trade_orders(self) -> None:
        first = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", is_import="Да", supplier_order="Заказ 1",
            organization="Организация 111", organization_inn="111",
        )
        second = replace(
            first,
            document="Поступление товаров и услуг ДЖ2 от 02.08.2026 10:00:00",
            turnover_quantity=Decimal("30"), party=" партия  25 ", supplier_order="Заказ 2",
        )
        tax = replace(
            first,
            document="Поступление (акт, накладная, УПД) 99 от 03.08.2026 10:00:00",
            warehouse_code="БД-000003", turnover_quantity=Decimal("47"),
            party="ПАРТИЯ 25", counterparty="ПОСТАВЩИК А",
        )

        result = reconcile([first, second], {"БУХ2019": [tax]}, "Август 2026")

        self.assertEqual(len(result.discrepancies), 1)
        difference = result.discrepancies[0]
        self.assertEqual(difference.discrepancy_type, DOCUMENT_QUANTITY_MISMATCH)
        self.assertEqual(difference.trade_row.turnover_quantity, Decimal("50"))
        self.assertEqual(difference.tax_row.turnover_quantity, Decimal("47"))
        self.assertIn("разница -3", difference.difference_summary)
        self.assertEqual(difference.match_method, "Бухгалтерская партия и контрагент")
        self.assertEqual(result.summaries[0].matched_documents, 0)
        self.assertEqual(result.summaries[0].matched_import_groups, 1)
        sheets = workbook_rows(build_action_excel_report(result))
        review_rows = sheets["Проверить вручную"]
        self.assertEqual(len(review_rows), 2)
        self.assertTrue(any("ДЖ1" in cell and "ДЖ2" in cell for cell in review_rows[1]))
        self.assertTrue(any("50" in cell for cell in review_rows[1]))
        self.assertTrue(any("47" in cell for cell in review_rows[1]))

    def test_import_party_from_raw_waybill_prefix_matches_and_keeps_source_values(self) -> None:
        common = {
            "Период": "01.09.2026 10:00:00", "Номенклатура": "Товар",
            "НоменклатураКод": "N1", "ЕдиницаИзмерения": "шт", "ЭтоИмпорт": "Да",
            "Организация": "Организация 111", "ОрганизацияИНН": "111",
            "КоличествоНачальныйОстаток": 0, "СтоимостьНачальныйОстаток": 0,
            "КоличествоКонечныйОстаток": 0, "СтоимостьКонечныйОстаток": 0,
            "СтоимостьОборот": 0,
        }
        trade = replace(normalize_row(common | {
            "Документ": "Поступление товаров и услуг ДЖ1 от 01.09.2026 10:00:00",
            "КоличествоОборот": 20, "ПартияБух": "42", "ЗаказПоставщику": "Заказ 1",
            "НаименованиеОтчетКонтрагента": "Поставщик А",
        }, "Сентябрь 2026"), warehouse_mapping_id="import")
        cases = [
            (comment, waybill, normalized_waybill)
            for comment in ("", None, "   ", "42")
            for waybill, normalized_waybill in (
                (" 42 / T11 / 2 ", "42 / T11 / 2"),
                (" 42\\AT41 ", "42\\AT41"),
            )
        ]
        for comment, waybill, normalized_waybill in cases:
            with self.subTest(comment=comment, waybill=waybill):
                raw_tax = common | {
                    "Документ": "Поступление (акт, накладная, УПД) 99 от 02.09.2026 10:00:00",
                    "КоличествоОборот": 17, "Комментарий": comment,
                    "НомерНакладнойУПД": waybill,
                    "НаименованиеДляДокументов": "Поставщик А", "СкладКод": "БД-000003",
                }
                tax = replace(normalize_row(raw_tax, "Сентябрь 2026"), warehouse_mapping_id="import")
                result = reconcile([trade], {"БУХ2019": [tax]}, "Сентябрь 2026")
                self.assertEqual(result.summaries[0].matched_import_groups, 1)
                self.assertEqual([d.discrepancy_type for d in result.discrepancies], [DOCUMENT_QUANTITY_MISMATCH])
                difference = result.discrepancies[0]
                self.assertEqual(difference.tax_row.party, "42")
                self.assertEqual(difference.tax_row.party_waybill, normalized_waybill)
                self.assertEqual(raw_tax["Комментарий"], comment)
                manual_rows = workbook_rows(build_action_excel_report(result))["Проверить вручную"]
                self.assertEqual(len(manual_rows), 2)
                self.assertIn("20", manual_rows[1])
                self.assertIn("17", manual_rows[1])
                self.assertTrue(any("Проверить" in cell or "Неоднознач" in cell for cell in manual_rows[1]))

    def test_import_waybill_conflict_or_empty_prefix_remains_manual(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.09.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="42",
            counterparty="Поставщик А", is_import="Да", supplier_order="Заказ 1",
        )
        for comment, waybill in (
            ("41", "42/T11"), ("41", "42\\T11"),
            ("", "/T11"), ("", "\\T11"), ("", ""),
            ("", "INVOICE001"), ("", "27"),
        ):
            with self.subTest(comment=comment, waybill=waybill):
                tax = replace(trade,
                    document="Поступление (акт, накладная, УПД) 99 от 01.09.2026 10:00:00",
                    warehouse_code="БД-000003", party=comment,
                    party_comment=comment, party_waybill=waybill)
                result = reconcile([trade], {"БУХ2019": [tax]}, "Сентябрь 2026")
                self.assertEqual(result.summaries[0].matched_import_groups, 0)
                self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_receipts_with_missing_party_remain_unmatched(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="",
            counterparty="Поставщик А", is_import="Да",
        )
        tax = replace(
            trade,
            document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", party="Партия 25",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(item.discrepancy_type != DOCUMENT_QUANTITY_MISMATCH for item in result.discrepancies))
        self.assertGreater(result.summaries[0].ambiguous_documents, 0)

    def test_import_receipt_with_conflicting_party_sources_is_manual(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", supplier_order="Заказ 1",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", party_comment="Партия 25", party_waybill="Партия 26",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_receipt_with_party_field_conflicting_with_comment_is_manual(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", supplier_order="Заказ 1",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", party_comment="Партия 26", party_waybill="",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_receipt_with_mixed_units_inside_aggregated_document_is_manual(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="10"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", supplier_order="Заказ 1",
        )
        trade_other_unit = replace(trade, document="Поступление товаров и услуг ДЖ2 от 01.08.2026 11:00:00", unit="упак")
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", turnover_quantity=Decimal("20"),
        )

        result = reconcile([trade, trade_other_unit], {"БУХ2019": [tax]}, "Август 2026")
        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_receipt_without_optional_unit_can_be_added_when_key_is_complete(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="10"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", supplier_order="Заказ 1", unit="", present_fields=frozenset({"unit"}),
        )
        result = reconcile([trade], {"БУХ2019": []}, "Август 2026")
        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [MISSING_IN_TAX])
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Довнести в БУХ"]), 2)
        self.assertEqual(len(sheets["Проверить вручную"]), 1)

    def test_sale_without_package_coefficient_is_manual(self) -> None:
        trade = replace(
            row("Товар", "Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
                turnover_quantity="-537"),
            nomenclature_code="N1", warehouse_mapping_id="main",
            issued_invoice_number="ДЖ000001589", package="коробка", package_coefficient="",
        )
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
                      turnover_quantity=Decimal("-550"))
        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_receipts_with_same_party_but_different_counterparty_do_not_match(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", is_import="Да", supplier_order="Заказ 1",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", counterparty="Поставщик Б",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))
        self.assertFalse(any(item.discrepancy_type in {MISSING_IN_TAX, EXTRA_IN_TAX} for item in result.discrepancies))
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        self.assertGreater(len(sheets["Проверить вручную"]), 1)

    def test_import_receipts_with_two_buh_documents_on_one_key_are_ambiguous(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", is_import="Да", supplier_order="Заказ 1",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
            warehouse_code="БД-000003", turnover_quantity=Decimal("10"),
        )

        result = reconcile(
            [trade],
            {"БУХ2019": [tax, replace(tax, document="Поступление (акт, накладная, УПД) 100 от 02.08.2026 10:00:00")]},
            "Август 2026",
        )

        self.assertEqual({item.discrepancy_type for item in result.discrepancies}, {AMBIGUOUS_MATCH})

    def test_duplicate_import_movement_is_manual(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", supplier_order="Заказ 1",
        )
        tax = replace(trade, document="Поступление (акт, накладная, УПД) 99 от 01.08.2026 10:00:00",
                      warehouse_code="БД-000003")

        result = reconcile([trade, trade], {"БУХ2019": [tax]}, "Август 2026")
        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_import_party_rule_does_not_apply_to_buh2026(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="import", party="Партия 25",
            counterparty="Поставщик А", is_import="Да",
        )
        tax = replace(
            trade,
            document="Поступление (акт, накладная, УПД) 99 от 03.08.2026 10:00:00",
            warehouse_code="БД-000003",
        )

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue({item.discrepancy_type for item in result.discrepancies} == {MISSING_IN_TAX, EXTRA_IN_TAX})
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Довнести в БУХ"]), 2)
        self.assertEqual(len(sheets["Проверить вручную"]), 1)

    def test_import_flag_does_not_override_accounting_warehouse_mapping(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг ДЖ1 от 01.08.2026 10:00:00", turnover_quantity="20"),
            nomenclature_code="N1", warehouse_mapping_id="other", party="Партия 25",
            counterparty="Поставщик А", is_import="Да",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 99 от 03.08.2026 10:00:00",
            warehouse_mapping_id="import", warehouse_code="БД-000003",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")

        self.assertFalse(any(
            item.match_method == "Бухгалтерская партия и контрагент" and item.trade_row is trade
            for item in result.discrepancies
        ))

    def test_opposite_movement_signs_go_to_manual_review(self) -> None:
        trade = row(
            "Товар", "Поступление товаров и услуг 1 от 01.08.2026 10:00:00",
            turnover_quantity="2",
        )
        tax = replace(trade, turnover_quantity=Decimal("-2"))

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        self.assertEqual(len(sheets["Лишние в БУХ"]), 1)
        self.assertEqual(len(sheets["Проверить вручную"]), 2)

    def test_sale_quantity_action_uses_buh_minus_trade_magnitudes(self) -> None:
        trade = replace(row(
            "Товар", "Реализация товаров и услуг 1589 от 02.08.2026 10:00:00",
            turnover_quantity="-537",
        ), issued_invoice_number="1589")
        tax = replace(trade, turnover_quantity=Decimal("-550"))

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual(len(sheets["Лишние в БУХ"]), 2)
        self.assertEqual(workbook_quantity_values(sheets, "Лишние в БУХ"), ["537", "550", "13"])
        self.assertEqual(sheets["Лишние в БУХ"][1][8:10], ["02.08.2026 10:00:00", "02.08.2026 10:00:00"])
        self.assertIn("Проверить количество существующего документа", sheets["Лишние в БУХ"][1][0])
        self.assertIn("разница 13", result.discrepancies[0].difference_summary)

    def test_issued_invoice_matches_control_sale_with_quantity_difference(self) -> None:
        trade = replace(
            row("Товар", "Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
                turnover_quantity="-537"),
            nomenclature_code="N1", warehouse_mapping_id="main",
            issued_invoice_number="ДЖ000001589",
        )
        tax = replace(
            trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
            turnover_quantity=Decimal("-550"), issued_invoice_number="",
        )

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [DOCUMENT_QUANTITY_MISMATCH])
        self.assertEqual(result.summaries[0].matched_documents, 1)
        self.assertEqual(workbook_quantity_values(sheets, "Лишние в БУХ"), ["537", "550", "13"])
        self.assertEqual(sheets["Лишние в БУХ"][1][8:10], ["02.08.2026 10:00:00", "02.08.2026 00:00:00"])
        self.assertIn("ДЖ000040154", sheets["Лишние в БУХ"][1])
        self.assertIn("1589", " ".join(map(str, sheets["Лишние в БУХ"][1])))

    def test_raw_json_to_xlsx_control_sale(self) -> None:
        common = {
            "Период": "02.08.2026 10:00:00", "Номенклатура": "Контрольный товар",
            "НоменклатураКод": "N1", "ЕдиницаИзмерения": "шт",
            "КоличествоНачальныйОстаток": 0, "СтоимостьНачальныйОстаток": 0,
            "СтоимостьОборот": 0, "КоличествоКонечныйОстаток": 0,
            "СтоимостьКонечныйОстаток": 0,
        }
        trade_json = {"code": 200, "data": [common | {
            "Документ": "Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
            "КоличествоОборот": -537, "НомерСФВыданный": "ДЖ000001589",
        }]}
        tax_json = {"code": 200, "data": [common | {
            "Документ": "Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
            "КоличествоОборот": -550,
        }]}

        result = reconcile(
            normalize_rows(trade_json, "Август 2026"),
            {"БУХ2019": normalize_rows(tax_json, "Август 2026")}, "Август 2026",
        )
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [DOCUMENT_QUANTITY_MISMATCH])
        self.assertEqual(workbook_quantity_values(sheets, "Лишние в БУХ"), ["537", "550", "13"])
        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        self.assertEqual(len(sheets["Проверить вручную"]), 1)

    def test_equal_closing_balances_do_not_hide_different_document_movements(self) -> None:
        trade_sale = replace(
            row("Товар", "Реализация товаров и услуг ДЖ77 от 02.08.2026 10:00:00",
                period="02.08.2026 10:00:00", turnover_quantity="-5"),
            nomenclature_code="N1", warehouse_mapping_id="main", issued_invoice_number="ДЖ000000077",
        )
        trade_receipt = replace(
            trade_sale, document="Поступление товаров и услуг 22 от 03.08.2026 10:00:00",
            issued_invoice_number="", turnover_quantity=Decimal("5"),
        )
        tax_sale = replace(
            trade_sale, document="Реализация (акт, накладная, УПД) 77 от 02.08.2026 00:00:00",
            issued_invoice_number="", turnover_quantity=Decimal("-4"),
        )
        tax_receipt = replace(
            trade_receipt, document="Поступление (акт, накладная, УПД) 22 от 03.08.2026 00:00:00",
            turnover_quantity=Decimal("4"),
        )
        trade_balance = replace(trade_sale, document="", issued_invoice_number="",
                                turnover_quantity=Decimal("0"), closing_quantity=Decimal("100"))
        tax_balance = replace(trade_balance)

        trade_rows = [trade_sale, trade_receipt, *bounded_snapshots([trade_balance])]
        tax_rows = [tax_sale, tax_receipt, *bounded_snapshots([tax_balance])]
        result = reconcile(
            trade_rows, {"БУХ2026": tax_rows}, "Август 2026",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )
        movement_discrepancies = [d for d in result.discrepancies if d.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH]
        self.assertEqual(len(movement_discrepancies), 2)
        self.assertTrue(all(d.trade_row is not None and d.tax_row is not None for d in movement_discrepancies))
        unknown_balances = [d for d in result.discrepancies if d.discrepancy_type == BALANCE_UNCHECKED]
        self.assertFalse(unknown_balances)
        self.assertEqual(result.notices, [])
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        manual_rows = sheets["Проверить вручную"]
        self.assertEqual(len(manual_rows), 3)
        manual_text = " ".join(cell for row_values in manual_rows for cell in row_values)
        self.assertIn("ДЖ77", manual_text)
        self.assertIn("22", manual_text)
        self.assertIn("5", manual_text)
        self.assertIn("4", manual_text)

    def test_default_reconciliation_uses_existing_document_rules(self) -> None:
        trade = replace(row("Товар", "Реализация товаров и услуг ДЖ1 от 02.08.2026 10:00:00",
                            turnover_quantity="-537"), issued_invoice_number="ДЖ1589")
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
                      turnover_quantity=Decimal("-550"))

        result = app_reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual(len(sheets["Лишние в БУХ"]), 2)
        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        self.assertEqual(result.summaries[0].matched_documents, 1)

    def test_sale_with_blank_invoice_and_different_document_number_is_manual(self) -> None:
        trade = replace(row("Товар", "Реализация товаров и услуг ДЖ1 от 02.08.2026 10:00:00",
                            turnover_quantity="-537"), nomenclature_code="N1", warehouse_mapping_id="main")
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
                      turnover_quantity=Decimal("-550"))
        differences = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026").discrepancies
        self.assertTrue(differences)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in differences))

    def test_duplicate_issued_invoice_across_dates_requires_manual_check(self) -> None:
        first = replace(
            row("Товар", "Реализация товаров и услуг ДЖ1 от 02.08.2026 10:00:00",
                turnover_quantity="-5"),
            issued_invoice_number="ДЖ000001589", nomenclature_code="N1",
            warehouse_mapping_id="main",
        )
        second = replace(first, document="Реализация товаров и услуг ДЖ2 от 03.08.2026 10:00:00")
        tax = replace(first,
                      document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
                      issued_invoice_number="")

        result = reconcile([first, second], {"БУХ2019": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(d.discrepancy_type == AMBIGUOUS_MATCH for d in result.discrepancies))

    def test_exact_repeated_export_movement_is_manual(self) -> None:
        trade = replace(row("Товар", "Поступление товаров и услуг 41 от 02.08.2026 10:00:00",
                            turnover_quantity="5"), nomenclature_code="N1")
        tax = replace(trade)

        result = reconcile([trade, trade], {"БУХ2026": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertFalse(any(d.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH for d in result.discrepancies))
        self.assertTrue(any("повтор" in d.difference_summary.lower() for d in result.discrepancies))

    def test_matched_document_with_one_empty_optional_unit_still_compares_quantity(self) -> None:
        trade = replace(row("Товар", "Реализация товаров и услуг ДЖ1 от 02.08.2026 10:00:00",
                            turnover_quantity="-537"), issued_invoice_number="ДЖ1589", unit="")
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
                      turnover_quantity=Decimal("-550"), unit="шт")

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        sheets = workbook_rows(build_action_excel_report(result))

        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [DOCUMENT_QUANTITY_MISMATCH])
        self.assertEqual(len(sheets["Лишние в БУХ"]), 2)
        self.assertEqual(len(sheets["Проверить вручную"]), 1)
        self.assertEqual(workbook_quantity_values(sheets, "Лишние в БУХ"), ["537", "550", "13"])

    def test_different_known_units_still_require_manual_check(self) -> None:
        trade = replace(row(
            "Товар", "Реализация товаров и услуг ДЖ2 от 02.08.2026 10:00:00", turnover_quantity="-5"
        ), unit="шт", issued_invoice_number="ДЖ2")
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 2 от 02.08.2026 00:00:00",
                      turnover_quantity=Decimal("-4"), unit="кг")

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))
        self.assertTrue(all("сопоставимость единиц" in item.difference_summary for item in result.discrepancies))

    def test_same_item_code_with_conflicting_articles_is_manual(self) -> None:
        trade = replace(row("Товар", "Поступление товаров и услуг 41 от 02.08.2026 10:00:00",
                            turnover_quantity="5"), nomenclature_code="N1", article="A")
        tax = replace(trade, article="B")

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")
        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [AMBIGUOUS_MATCH])

    def test_different_packaging_without_approved_conversion_is_manual(self) -> None:
        trade = replace(row("Товар", "Поступление товаров и услуг 41 от 02.08.2026 10:00:00",
                            turnover_quantity="5"), package="коробка", package_coefficient="10")
        tax = replace(trade, package="штука", package_coefficient="1")

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")
        self.assertEqual([d.discrepancy_type for d in result.discrepancies], [AMBIGUOUS_MATCH])

    def test_repeated_unbounded_snapshot_keys_become_notice(self) -> None:
        result = reconcile(
            trade_rows=[
                row("Товар 1", opening_quantity="2.00", opening_amount="20.00", closing_quantity="2.00", closing_amount="20.00"),
                row("Товар 1", opening_quantity="3.00", opening_amount="30.00", closing_quantity="3.00", closing_amount="30.00"),
            ],
            tax_exports={
                "Бухгалтерия": [
                    row("Товар 1", opening_quantity="5.00", opening_amount="50.00", closing_quantity="5.00", closing_amount="50.00")
                ]
            },
            period="I 2024",
        )

        self.assertEqual(result.discrepancies, [])
        self.assertTrue(result.notices)
        self.assertGreaterEqual(sum(notice.affected_rows for notice in result.notices), 3)

    def test_matches_different_source_warehouse_codes_by_mapping_id(self) -> None:
        trade = row("Товар 1")
        trade = LedgerRow(**{**trade.__dict__, "warehouse_code": "УПР000234", "warehouse_mapping_id": "main"})
        tax = LedgerRow(**{**trade.__dict__, "warehouse_code": "БД-000001"})

        result = reconcile(
            bounded_snapshots([trade]), {"БУХ": bounded_snapshots([tax])}, "I 2024",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )

        self.assertEqual(result.discrepancies, [])

    def test_matches_transfer_aliases_by_number_and_date(self) -> None:
        trade = replace(
            row("Товар 1", "Передача товаров филиалу 2146741929 от 02.03.2026 8:48:28"),
            nomenclature_code="N1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse_code="T1",
            receiver_warehouse_code="T2",
        )
        tax = replace(
            trade,
            document="Перемещение товаров, материалов 2146741929 от 02.03.2026 8:48:28",
            warehouse_code="B1",
        )

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].matched_documents, 1)
        self.assertEqual(result.summaries[0].matched_by_number_date, 1)

    def test_ordinary_receipt_uses_document_day_even_if_times_differ(self) -> None:
        trade = replace(
            row("Товар", "Поступление товаров и услуг 41 от 02.08.2026 10:30:00",
                turnover_quantity="5"),
            nomenclature_code="N1", warehouse_mapping_id="main",
        )
        tax = replace(
            trade, document="Поступление (акт, накладная, УПД) 41 от 02.08.2026 00:00:00",
        )

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].matched_by_number_date, 1)

    def test_ordinary_receipts_with_different_numbers_are_unmatched(self) -> None:
        trade = replace(row("Товар", "Поступление товаров и услуг ДЖ000041 от 02.08.2026 10:30:00",
                            turnover_quantity="5"), nomenclature_code="N1", warehouse_mapping_id="main")
        tax = replace(trade, document="Поступление (акт, накладная, УПД) 41 от 02.08.2026 00:00:00")

        result = reconcile([trade], {"БУХ2026": [tax]}, "Август 2026")

        self.assertTrue(result.discrepancies)
        self.assertTrue({d.discrepancy_type for d in result.discrepancies} == {MISSING_IN_TAX, EXTRA_IN_TAX})

    def test_matches_confirmed_document_type_matrix(self) -> None:
        pairs = (
            ("Поступление товаров и услуг", "Поступление (акты, накладные, УПД)"),
            ("Оприходование товаров", "Оприходование"),
            ("Списание товаров", "Списание товаров, материалов"),
            ("Реализация товаров и услуг", "Реализация (акт, накладная, УПД)"),
        )
        for trade_type, tax_type in pairs:
            with self.subTest(trade_type=trade_type, tax_type=tax_type):
                trade = replace(
                    row("Товар 1", f"{trade_type} 1 от 01.03.2026 10:00:00"),
                    nomenclature_code="N1",
                    warehouse_mapping_id="main",
                    issued_invoice_number="1" if trade_type == "Реализация товаров и услуг" else "",
                )
                tax = replace(
                    trade,
                    document=f"{tax_type} 1 от 01.03.2026 10:00:00",
                    warehouse_code="B1",
                )

                result = reconcile([trade], {"БУХ": [tax]}, "I 2024")

                self.assertEqual(result.discrepancies, [])
                self.assertEqual(result.summaries[0].matched_documents, 1)

    def test_does_not_match_sale_with_different_number_by_content(self) -> None:
        trade = replace(
            row(
                "Товар 1",
                "Реализация товаров и услуг ДЖ000039101 от 23.03.2026 0:00:00",
                turnover_quantity="-2",
            ),
            nomenclature_code="N1",
            warehouse_mapping_id="main",
        )
        tax = replace(
            trade,
            document="Реализация (акт, накладная, УПД) 515 от 23.03.2026 0:00:00",
            turnover_amount=Decimal("1"),
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertTrue(differences)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences))

    def test_source_document_id_does_not_override_sale_invoice_rule(self) -> None:
        trade = replace(
            row("Товар 1", "Реализация товаров и услуг ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
            document_id="shared-id",
        )
        tax = replace(
            trade,
            document="Перемещение товаров 99 от 02.03.2026 11:00:00",
            document_id="tax-id",
            source_document_id="shared-id",
            turnover_quantity=Decimal("1"),
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertFalse(any(item.match_method == "Идентификатор исходного документа" for item in differences))
        self.assertTrue(any(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences))

    def test_duplicate_canonical_document_in_tax_is_reported(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
            sender_warehouse_code="T1",
            receiver_warehouse_code="T2",
        )
        first = replace(
            trade,
            document="Перемещение товаров, материалов ДЖ1 от 01.03.2026 10:00:00",
        )
        second = replace(
            trade,
            document="Передача товаров филиалу ДЖ1 от 01.03.2026 10:00:00",
        )

        differences = reconcile([trade], {"БУХ": [first, second]}, "I 2024").discrepancies

        self.assertEqual(len(differences), 3)
        self.assertEqual(sum(item.discrepancy_type == DUPLICATE_IN_TAX for item in differences), 2)
        self.assertEqual(sum(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences), 1)

    def test_same_document_number_in_different_organizations_is_ambiguous(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ1 от 01.03.2026 10:00:00"),
            nomenclature_code="N1",
        )
        first = replace(
            trade,
            document="Перемещение товаров, материалов ДЖ1 от 01.03.2026 10:00:00",
            organization_inn="111",
        )
        second = replace(
            trade,
            document="Передача товаров филиалу ДЖ1 от 01.03.2026 10:00:00",
            organization_inn="222",
        )

        result = reconcile([trade], {"БУХ": [first, second]}, "I 2024")

        self.assertEqual(len(result.discrepancies), 3)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))

    def test_known_units_and_document_key_enable_quantity_comparison_by_default(self) -> None:
        trade = replace(row("Товар", "Реализация товаров и услуг ДЖ1 от 02.08.2026 10:00:00",
                            turnover_quantity="-537"), issued_invoice_number="ДЖ1589")
        tax = replace(trade, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 00:00:00",
                      turnover_quantity=Decimal("-550"))

        result = app_reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")
        self.assertEqual(result.summaries[0].matched_documents, 1)
        self.assertTrue(any(item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH for item in result.discrepancies))
        self.assertEqual(result.summaries[0].duplicate_documents, 0)

    def test_does_not_match_same_number_on_different_date(self) -> None:
        trade = replace(
            row("Товар 1", "Перемещение товаров ДЖ000001381 от 05.03.2026 14:22:23"),
            nomenclature_code="N1",
            sender_warehouse_code="T1",
            receiver_warehouse_code="T2",
        )
        tax = replace(
            trade,
            document="Комплектация номенклатуры ДЖ000001381 от 10.03.2026 17:49:43",
        )

        differences = reconcile([trade], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertTrue(differences)
        self.assertEqual(reconcile([trade], {"БУХ": [tax]}, "I 2024").summaries[0].matched_documents, 0)
        self.assertFalse(any(item.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH for item in differences))

    def test_unmatched_requested_accounting_document_enters_extra(self) -> None:
        tax = row(
            "Товар 1",
            "Списание товаров, материалов БД1 от 10.03.2026 17:49:43",
            turnover_quantity="-1",
        )

        result = reconcile([], {"БУХ": [tax]}, "I 2024")

        self.assertEqual(result.discrepancies[0].discrepancy_type, EXTRA_IN_TAX)
        self.assertEqual(result.summaries[0].extra_documents, 1)

    def test_ambiguous_sale_signature_requires_manual_check(self) -> None:
        first = replace(
            row(
                "Товар 1",
                "Реализация товаров и услуг ДЖ1 от 23.03.2026 10:00:00",
                turnover_quantity="-1",
            ),
            nomenclature_code="N1",
        )
        second = replace(first, document="Реализация товаров и услуг ДЖ2 от 23.03.2026 10:00:00")
        tax = replace(first, document="Реализация (акт, накладная, УПД) 10 от 23.03.2026 10:00:00")

        differences = reconcile([first, second], {"БУХ": [tax]}, "I 2024").discrepancies

        self.assertEqual(len(differences), 3)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences))

    def test_internal_transfer_without_endpoint_codes_is_manual(self) -> None:
        document = "Перемещение товаров ДЖ1 от 23.03.2026 10:00:00"
        first = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="Секция 1",
            receiver_warehouse="Секция 2",
        )
        second = replace(first, warehouse="Секция 2", warehouse_code="T2")

        differences = reconcile([first, second], {"БУХ": []}, "I 2024").discrepancies
        self.assertTrue(differences)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in differences))

    def test_internal_transfer_uses_sender_and_receiver_accounting_properties(self) -> None:
        first = replace(
            row("Товар", "Перемещение товаров 5 от 02.08.2026 10:00:00",
                turnover_quantity="-5"),
            nomenclature_code="N1", warehouse_mapping_id="main", warehouse_code="T1",
            sender_warehouse_code="T1", receiver_warehouse_code="T2",
            sender_accounting_warehouse="Основной", receiver_accounting_warehouse="Основной",
        )
        second = replace(first, warehouse_code="T2", turnover_quantity=Decimal("5"))

        result = reconcile([first, second], {"БУХ2026": []}, "Август 2026")

        self.assertEqual(result.discrepancies, [])
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 1)

    def test_ptf_without_endpoint_codes_is_manual(self) -> None:
        document = "Передача товаров филиалу (ПТФ) ДЖ1 от 23.03.2026 10:00:00"
        first = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="",
            receiver_warehouse="",
        )
        second = replace(
            first,
            warehouse="Секция 2",
            warehouse_code="T2",
            turnover_quantity=Decimal("1"),
        )

        result = reconcile([first, second], {"БУХ": []}, "I 2024")

        self.assertTrue(result.discrepancies)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)

    def test_transfer_between_logical_warehouses_is_checked(self) -> None:
        trade = replace(
            row(
                "Товар 1",
                "Перемещение товаров ДЖ2 от 23.03.2026 11:00:00",
                turnover_quantity="-1",
            ),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
            sender_warehouse="Секция 1",
            receiver_warehouse="Другой логический склад",
        )

        result = reconcile([trade], {"БУХ": []}, "I 2024")

        self.assertEqual(len(result.discrepancies), 1)
        self.assertEqual(result.discrepancies[0].discrepancy_type, AMBIGUOUS_MATCH)
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)

    def test_transfer_is_not_internal_when_different_items_only_cancel_in_total(self) -> None:
        document = "Передача товаров филиалу ДЖ3 от 23.03.2026 12:00:00"
        outgoing = replace(
            row("Товар 1", document, turnover_quantity="-1"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        incoming_other_item = replace(
            outgoing,
            nomenclature="Товар 2",
            nomenclature_code="N2",
            warehouse="Секция 2",
            warehouse_code="T2",
            turnover_quantity=Decimal("1"),
        )

        result = reconcile([outgoing, incoming_other_item], {"БУХ": []}, "I 2024")

        self.assertEqual(len(result.discrepancies), 2)
        self.assertTrue(all(item.discrepancy_type == AMBIGUOUS_MATCH for item in result.discrepancies))
        self.assertEqual({item.trade_row.nomenclature for item in result.discrepancies if item.trade_row},
                         {"Товар 1", "Товар 2"})
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)

    def test_requested_simple_document_types_enter_add_when_unmatched(self) -> None:
        documents = [
            "Комплектация номенклатуры ДЖ1 от 23.03.2026 10:00:00",
            "Требование-накладная ДЖ2 от 23.03.2026 10:00:00",
            "Безвозмездная передача ДЖ5 от 23.03.2026 10:00:00",
            "Поступление дополнительных расходов ДЖ3 от 23.03.2026 10:00:00",
            "Заказ покупателя ДЖ4 от 23.03.2026 10:00:00",
        ]
        trade_rows = [row(f"Товар {index}", document, turnover_quantity="1") for index, document in enumerate(documents)]

        result = reconcile(trade_rows, {"БУХ": []}, "I 2024")

        self.assertEqual(len(result.discrepancies), 3)
        self.assertEqual(len(workbook_rows(build_action_excel_report(result))["Довнести в БУХ"]), 4)
        self.assertTrue(all(item.discrepancy_type == MISSING_IN_TAX for item in result.discrepancies))
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets["Проверить вручную"]), 1)
        self.assertEqual(result.summaries[0].trade_documents, 5)
        self.assertEqual(result.summaries[0].pending_trade_documents, 0)
        self.assertEqual(result.summaries[0].excluded_trade_documents, 2)

    def test_requested_unparsed_document_stays_visible_for_manual_review(self) -> None:
        trade = row("Товар", "Комплектация номенклатуры без стандартного номера", turnover_quantity="1")

        result = reconcile([trade], {"БУХ": []}, "I 2024")

        self.assertTrue(result.discrepancies)
        self.assertTrue(any(item.trade_row and "Комплектация номенклатуры" in item.trade_row.document
                            for item in result.discrepancies))

    def test_requirement_shows_writeoff_candidate_without_auto_match(self) -> None:
        trade = replace(row("Товар", "Требование-накладная 7 от 02.08.2026 10:00:00",
                            turnover_quantity="-5"), nomenclature_code="N1", warehouse_mapping_id="main")
        tax = replace(trade, document="Списание 9 от 02.08.2026 11:00:00")

        result = reconcile([trade], {"БУХ2019": [tax]}, "Август 2026")

        self.assertTrue(any("Списание 9" in d.difference_summary for d in result.discrepancies))
        self.assertFalse(any(d.discrepancy_type == DOCUMENT_QUANTITY_MISMATCH for d in result.discrepancies))

    def test_snapshots_from_physical_warehouses_are_aggregated(self) -> None:
        first = replace(
            row("Товар 1", opening_quantity="2", opening_amount="20", closing_quantity="2", closing_amount="20"),
            nomenclature_code="N1",
            warehouse="Секция 1",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        second = replace(
            first,
            opening_quantity=Decimal("3"),
            opening_amount=Decimal("30"),
            closing_quantity=Decimal("3"),
            closing_amount=Decimal("30"),
            warehouse="Секция 2",
            warehouse_code="T2",
        )
        tax = replace(
            first,
            opening_quantity=Decimal("5"),
            opening_amount=Decimal("50"),
            closing_quantity=Decimal("5"),
            closing_amount=Decimal("50"),
            warehouse="Основной склад",
            warehouse_code="B1",
        )

        result = reconcile(
            bounded_snapshots([first, second]), {"БУХ": bounded_snapshots([tax])}, "I 2024",
            start_date=date(2026, 8, 1), end_date=date(2026, 8, 31),
        )
        self.assertEqual(result.discrepancies, [])


class ReportingTests(TestCase):
    def test_builds_xlsx_bytes(self) -> None:
        content = build_excel_report([])

        self.assertTrue(content.startswith(b"PK"))
        self.assertIn(b"[Content_Types].xml", content)

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            styles = workbook.read("xl/styles.xml").decode("utf-8")
        self.assertIn('state="frozen"', sheet)
        self.assertIn('<autoFilter ref="A1:A1"/>', sheet)
        self.assertIn('<cols>', sheet)
        self.assertIn('fillId="2"', styles)

    def test_report_has_summary_and_side_by_side_details(self) -> None:
        document = "Реализация товаров и услуг 1 от 01.08.2026 10:00:00"
        trade = replace(
            row("Товар 1", document, period="01.08.2026 10:00:00", turnover_quantity="-5"),
            issued_invoice_number="1",
        )
        tax = replace(trade, turnover_quantity=Decimal("-4"))

        result = reconcile([trade], {"БУХ": [tax]}, "I 2024")
        self.assertEqual([item.discrepancy_type for item in result.discrepancies], [DOCUMENT_QUANTITY_MISMATCH])
        content = build_excel_report(result)

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            summary = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
            details = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
        self.assertIn('name="Итоги"', workbook_xml)
        self.assertIn('name="Расхождения"', workbook_xml)
        self.assertIn('name="Документы"', workbook_xml)
        self.assertIn('name="Количество"', workbook_xml)
        self.assertIn('name="Остатки"', workbook_xml)
        self.assertIn("Всего строк с расхождениями", summary)
        self.assertIn("Статистика сопоставления документов", summary)
        self.assertIn("Документ УТ", details)
        self.assertIn("Документ БУХ", details)

    def test_action_report_groups_all_item_rows_into_one_document(self) -> None:
        document = "Перемещение товаров ДЖ000001 от 05.03.2026 14:22:02"
        first = replace(
            row("Товар 1", document),
            nomenclature_code="N1",
            warehouse="Склад УТ",
            warehouse_mapping_id="main",
            sender_warehouse_code="T1",
            receiver_warehouse_code="T2",
        )
        second = replace(first, nomenclature="Товар 2", nomenclature_code="N2")
        third = replace(first, nomenclature="Товар 3", nomenclature_code="N3")

        result = reconcile([first, second, third], {"БУХ2019": []}, "Март 2026")
        content = build_action_excel_report(result)

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8")
            add_sheet = workbook.read("xl/worksheets/sheet3.xml").decode("utf-8")
            review_sheet = workbook.read("xl/worksheets/sheet5.xml").decode("utf-8")
            ns = {"s": "http://internal.internal.internal.example.invalid/spreadsheetml/2006/main"}
            rows = ET.fromstring(add_sheet).findall("s:sheetData/s:row", ns)
        self.assertIn('name="Довнести в БУХ"', workbook_xml)
        self.assertIn('name="Лишние в БУХ"', workbook_xml)
        self.assertIn('name="Проверить вручную"', workbook_xml)
        self.assertEqual(len(rows), 2)
        self.assertEqual(add_sheet.count(document), 1)
        self.assertIn("Довнести в БУХ2019", add_sheet)
        self.assertEqual({d.trade_row.nomenclature for d in result.discrepancies},
                         {"Товар 1", "Товар 2", "Товар 3"})
        self.assertEqual(len(ET.fromstring(review_sheet).findall("s:sheetData/s:row", ns)), 1)
        self.assertEqual(add_sheet.count("Наличие неизвестно"), 1)

    def test_action_report_places_unmatched_requested_tax_types_on_extra(self) -> None:
        extra = row(
            "Товар 1",
            "Перемещение товаров, материалов БД-1 от 06.03.2026 10:00:00",
        )
        accounting_only = row(
            "Товар 2",
            "Списание товаров, материалов БД-2 от 07.03.2026 11:00:00",
        )

        content = build_action_excel_report(
            reconcile([], {"БУХ2019": [extra, accounting_only]}, "Март 2026")
        )

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            extra_sheet = workbook.read("xl/worksheets/sheet4.xml").decode("utf-8")
            review_sheet = workbook.read("xl/worksheets/sheet5.xml").decode("utf-8")
        self.assertIn("БД-1", extra_sheet)
        self.assertIn("БД-2", extra_sheet)
        self.assertNotIn("БД-1", review_sheet)
        self.assertNotIn("БД-2", review_sheet)
        self.assertNotIn("Проверить допустимость в БУХ2019", review_sheet)

    def test_action_report_excludes_document_value_differences_from_action_sheets(self) -> None:
        trade = replace(row(
            "Товар 1",
            "Реализация товаров и услуг ДЖ1 от 08.03.2026 12:00:00",
            turnover_amount="100",
        ), issued_invoice_number="ДЖ1")
        tax = replace(trade, turnover_amount=Decimal("101"))

        content = build_action_excel_report(reconcile([trade], {"БУХ2019": [tax]}, "Март 2026"))

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            action_sheets = [
                workbook.read(f"xl/worksheets/sheet{index}.xml").decode("utf-8")
                for index in range(3, 6)
            ]
        self.assertTrue(all(sheet.count("<row ") == 1 for sheet in action_sheets))
        self.assertTrue(all("Различие стоимости" not in sheet for sheet in action_sheets))

    def test_action_report_includes_balance_differences_with_dates_values_and_missing_reasons(self) -> None:
        trade_start = replace(
            row("Товар только УТ", period="01.03.2026 0:00:00", opening_quantity="7", opening_amount="70"),
            nomenclature_code="N-TRADE",
            article="ART-TRADE",
            warehouse="Склад УТ",
            warehouse_code="T1",
            warehouse_mapping_id="main",
        )
        tax_end = replace(
            row("Товар только БУХ", period="11.03.2026 23:59:59", closing_quantity="4", closing_amount="40"),
            nomenclature_code="N-TAX",
            article="ART-TAX",
            warehouse="Склад БУХ",
            warehouse_code="B1",
            warehouse_mapping_id="main",
        )
        checked = reconcile([trade_start], {"БУХ2019": [tax_end]}, "01.03.2026–11.03.2026")
        result = ReconciliationResult(
            discrepancies=checked.discrepancies,
            summaries=checked.summaries,
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 11),
            notices=checked.notices,
        )

        content = build_action_excel_report(result)
        sheets = workbook_rows(content)
        balances = sheets["Расхождения остатков"]

        self.assertEqual(
            list(sheets)[:5],
            ["Итоги", "Расхождения остатков", "Довнести в БУХ", "Лишние в БУХ", "Проверить вручную"],
        )
        self.assertEqual(len(balances), 1)
        self.assertEqual(
            balances[0][:6],
            ["Дата расхождения", "Артикул", "Наименование", "Остаток в УТ", "Остаток в НБ", "Расхождение"],
        )
        manual = sheets["Проверить вручную"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(checked.discrepancies, [])
        self.assertTrue(checked.notices)
        summary_text = " ".join(cell for excel_row in sheets["Итоги"] for cell in excel_row)
        self.assertIn("Ограничения сверки", summary_text)
        self.assertIn("Начальный остаток не проверен", summary_text)
        self.assertIn("Конечный остаток не проверен", summary_text)
        self.assertFalse(balances[1:])

        with zipfile.ZipFile(io.BytesIO(content)) as workbook:
            balance_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn('state="frozen"', balance_xml)
        self.assertIn('<autoFilter ref="A1:Q1"/>', balance_xml)
        self.assertIn('<col min="12" max="13" width="42"', balance_xml)
