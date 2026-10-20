from dataclasses import replace
from datetime import date
from decimal import Decimal
import unittest

from app.config import OneCBaseConfig, Settings, WarehouseMappingConfig
from app.models import LedgerRow
from scripts.pilot_diagnostics import build_diagnostics


def row(document, *, period="02.08.2026 10:00:00", qty="-1", **kwargs):
    values = dict(
        period=period, document=document, nomenclature="Sensitive item",
        opening_quantity=Decimal("0"), opening_amount=Decimal("0"),
        turnover_quantity=Decimal(qty), turnover_amount=Decimal("1"),
        closing_quantity=Decimal("0"), closing_amount=Decimal("1"),
        nomenclature_code="ITEM-SECRET", warehouse_code="БД-000003",
        warehouse_mapping_id="import", organization="Org secret",
    )
    values.update(kwargs)
    return LedgerRow(**values)


def settings():
    trade = OneCBaseConfig("UT", "unused", base_id="ut")
    tax = OneCBaseConfig("BUH2019", "unused", base_id="buh2019")
    mapping = WarehouseMappingConfig("import", "Import", {"ut": ["W-UT"], "buh2019": ["БД-000003"]})
    return Settings(trade, [tax], warehouse_mappings=[mapping])


def settings_with_2026():
    value = settings()
    return replace(value, tax_bases=[*value.tax_bases,
                                    OneCBaseConfig("BUH2026", "unused", base_id="buh2026")])


class PilotDiagnosticsTests(unittest.TestCase):
    def test_import_party_resolution_uses_production_rules_and_reports_only_counts(self):
        tax_rows = [
            row("Поступление товаров ПТУ-1 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                party_comment="Party secret", party_waybill="Party secret\\invoice", counterparty="Supplier secret"),
            row("Поступление товаров ПТУ-2 от 02.09.2026 09:00:00", period="02.09.2026 09:00:00",
                party_waybill="invoice-only", counterparty="Supplier secret"),
            row("Поступление товаров ПТУ-3 от 03.09.2026 09:00:00", period="03.09.2026 09:00:00",
                party_comment="Party A", party_waybill="Party B/invoice", counterparty="Supplier secret"),
        ]
        trade_rows = [row("Поступление товаров Заказ-UT от 01.09.2026 09:00:00",
                          period="01.09.2026 09:00:00", party="Party secret", supplier_order="PO secret",
                          counterparty="Supplier secret", warehouse_mapping_id="import"),
                      row("Поступление товаров Заказ-UT2 от 02.09.2026 09:00:00",
                          period="02.09.2026 09:00:00", party_waybill="Party secret/invoice",
                          counterparty="Supplier secret", warehouse_mapping_id="import")]
        result = build_diagnostics({"ut": trade_rows, "buh2019": tax_rows}, settings(),
                                   date(2026, 9, 1), date(2026, 9, 7))
        imp = result["bases"]["buh2019"]["import_party_coverage"]
        self.assertEqual(imp["resolved_rows"], 1)
        self.assertEqual(imp["missing_party_tax_rows"], 2)
        self.assertEqual(imp["conflicting_party_tax_rows"], 1)
        self.assertEqual(result["bases"]["ut"]["import_party_coverage"]["missing_party_rows"], 1)
        self.assertEqual(result["import_control"]["applicable"], True)
        self.assertNotIn("Party secret", repr(result))
        self.assertNotIn("Supplier secret", repr(result))
        self.assertNotIn("ПТУ-1", repr(result))

    def test_control_sale_link_is_key_based_and_quantity_independent(self):
        trade = row("Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
                    qty="-537", warehouse_code="W-UT", issued_invoice_number="ДЖ000001589",
                    warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        tax = row("Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
                  qty="-550", warehouse_code="БД-000003", warehouse_mapping_id="sale-map",
                  nomenclature_code="ЦБ000110941")
        out_a = build_diagnostics({"ut": [trade], "buh2019": [tax]}, settings(),
                                  date(2026, 8, 2), date(2026, 8, 2))
        out_b = build_diagnostics({"ut": [replace(trade, turnover_quantity=Decimal("-1"))],
                                   "buh2019": [replace(tax, turnover_quantity=Decimal("-999"))]}, settings(),
                                  date(2026, 8, 2), date(2026, 8, 2))
        self.assertEqual(out_a["sale_control"]["candidate_pairs"], 1)
        self.assertEqual(out_a["sale_control"]["candidate_pairs"],
                         out_b["sale_control"]["candidate_pairs"])
        self.assertEqual(out_a["sale_control"]["source_rows"]["ut_quantity"], "-537")
        self.assertEqual(out_a["sale_control"]["source_rows"]["buh2019_quantity"], "-550")
        self.assertTrue(out_a["sale_control"]["expected_values_observed"])
        self.assertFalse(out_a["quantitative_reconciliation_approved"])

    def test_control_sale_expected_values_accept_decimal_trailing_zeroes(self):
        trade = row("Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
                    qty="-537.000", warehouse_code="W-UT", issued_invoice_number="ДЖ000001589",
                    warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        tax = row("Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
                  qty="-550.000", warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        result = build_diagnostics({"ut": [trade], "buh2019": [tax]}, settings(),
                                   date(2026, 8, 2), date(2026, 8, 2))
        self.assertTrue(result["sale_control"]["expected_values_observed"])
        self.assertEqual(result["sale_control"]["source_rows"]["ut_quantity"], "-537.000")
        self.assertEqual(result["sale_control"]["source_rows"]["buh2019_quantity"], "-550.000")

    def test_missing_base_and_duplicate_rows_are_reported_without_sensitive_values(self):
        source = row("Поступление товаров ПТУ-1 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                     party="party secret", counterparty="counterparty secret")
        result = build_diagnostics({"buh2019": [source, source]}, settings(),
                                   date(2026, 9, 1), date(2026, 9, 7))
        self.assertNotIn("ut", result["bases"])
        self.assertEqual(result["bases"]["buh2019"]["rows"], 2)
        self.assertGreaterEqual(result["bases"]["buh2019"]["exact_duplicate_rows"], 1)
        self.assertEqual(result["bases"]["buh2019"]["distinct_documents"]["goods_receipt"], 1)
        self.assertNotIn("party secret", repr(result))
        self.assertNotIn("counterparty secret", repr(result))

    def test_duplicate_sale_invoice_key_is_reported_as_multiple_candidates(self):
        first = row("Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
                    warehouse_code="W-UT", issued_invoice_number="ДЖ000001589",
                    warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        second = replace(first, document="Реализация товаров и услуг ДЖ000040155 от 02.08.2026 11:00:00")
        tax = row("Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
                  warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        result = build_diagnostics({"ut": [first, second], "buh2019": [tax]}, settings(),
                                   date(2026, 8, 2), date(2026, 8, 2))
        self.assertFalse(result["sale_control"]["unique_sale_key_candidate"])
        self.assertFalse(result["sale_control"]["expected_values_observed"])

    def test_party5_uses_exact_normalized_party_and_only_buh2019(self):
        ut = [row("Поступление товаров УТ5 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                  party="  5 ", supplier_order="PO", counterparty="Компания Ё",
                  warehouse_mapping_id="import"),
              row("Поступление товаров УТ15 от 01.09.2026 10:00:00", period="01.09.2026 10:00:00",
                  party="Партия 15", supplier_order="PO", counterparty="Компания Ё",
                  warehouse_mapping_id="import"),
              row("Поступление товаров УТ50 от 01.09.2026 11:00:00", period="01.09.2026 11:00:00",
                  party="Партия 50", supplier_order="PO", counterparty="Компания Ё",
                  warehouse_mapping_id="import")]
        tax = [row("Поступление товаров ПТУ5 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                   party_comment=" 5 ", counterparty="Компания Е", warehouse_mapping_id="import")]
        other_tax = [replace(tax[0], document="Поступление товаров ИНОЕ от 01.09.2026 09:00:00",
                             is_import="true")]
        result = build_diagnostics({"ut": ut, "buh2019": tax, "buh2026": other_tax}, settings_with_2026(),
                                   date(2026, 9, 1), date(2026, 9, 7))
        control = result["import_control"]
        self.assertEqual(control["shared_keys"], 1)
        self.assertEqual(control["trade_documents"], 1)
        self.assertEqual(control["tax_documents"], 1)
        self.assertEqual(control["trade_rows"], 1)
        self.assertEqual(control["tax_rows"], 1)
        self.assertEqual(result["bases"]["buh2026"]["import_party_coverage"]["eligible_rows"], 0)
        no_mapping = build_diagnostics(
            {"ut": [replace(ut[0], warehouse_mapping_id="")],
             "buh2019": [replace(tax[0], warehouse_mapping_id="")]}, settings(),
            date(2026, 9, 1), date(2026, 9, 7))
        self.assertEqual(no_mapping["import_control"]["shared_keys"], 0)

    def test_sale_control_uses_exact_documents_and_rejects_duplicate_source_rows(self):
        trade = row("Реализация товаров и услуг ДЖ0000401549 от 02.08.2026 10:00:00",
                    qty="-537", warehouse_code="W-UT", issued_invoice_number="ДЖ000001589",
                    warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        tax = row("Реализация (акт, накладная, УПД) 15890 от 02.08.2026 10:00:00",
                  qty="-550", warehouse_mapping_id="sale-map", nomenclature_code="ЦБ000110941")
        right_tax_other_base = replace(tax, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00")
        wrong = build_diagnostics({"ut": [trade], "buh2019": [tax], "buh2026": [right_tax_other_base]}, settings_with_2026(),
                                  date(2026, 8, 2), date(2026, 8, 2))
        self.assertEqual(wrong["sale_control"]["source_rows"]["ut_rows"], 0)
        self.assertEqual(wrong["sale_control"]["source_rows"]["buh2019_rows"], 0)
        self.assertFalse(wrong["sale_control"]["expected_values_observed"])
        exact_trade = replace(trade, document="Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00")
        exact_tax = replace(tax, document="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00")
        duplicate = build_diagnostics({"ut": [exact_trade, exact_trade], "buh2019": [exact_tax]}, settings(),
                                      date(2026, 8, 2), date(2026, 8, 2))
        self.assertFalse(duplicate["sale_control"]["unique_sale_key_candidate"])
        self.assertFalse(duplicate["sale_control"]["expected_values_observed"])

    def test_snapshots_and_pending_rows_are_not_document_stats(self):
        pending = row("Комплектация номенклатуры СБ-1 от 02.08.2026 10:00:00")
        snapshot = replace(pending, document="", period="02.08.2026 10:00:00")
        result = build_diagnostics({"buh2019": [pending, pending, snapshot, snapshot]}, settings(),
                                   date(2026, 8, 2), date(2026, 8, 2))
        base = result["bases"]["buh2019"]
        self.assertEqual(base["distinct_documents"]["assembly"], 1)
        self.assertEqual(base["pending_family_documents"]["assembly"], 1)
        self.assertEqual(base["exact_duplicate_rows"], 1)

    def test_receipt_candidates_do_not_compare_trade_base_to_itself_and_exclude_buh2019_imports(self):
        ordinary = row("Поступление товаров ПТУ-2 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                       warehouse_mapping_id="sale-map")
        imported = row("Поступление товаров ПТУ-1 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                       warehouse_mapping_id="import")
        trade = row("Поступление товаров Заказ-1 от 01.09.2026 09:00:00", period="01.09.2026 09:00:00",
                    warehouse_mapping_id="sale-map")
        result = build_diagnostics({"ut": [trade], "buh2019": [imported], "buh2026": [ordinary]},
                                   settings_with_2026(), date(2026, 9, 1), date(2026, 9, 7))
        self.assertEqual(result["bases"]["ut"]["ordinary_receipt_candidates"]["shared_candidate_keys"], 0)
        self.assertEqual(result["bases"]["buh2019"]["ordinary_receipt_candidates"]["taxbase_candidate_keys"], 0)
        self.assertEqual(result["bases"]["buh2026"]["ordinary_receipt_candidates"]["taxbase_candidate_keys"], 1)


if __name__ == "__main__":
    unittest.main()
