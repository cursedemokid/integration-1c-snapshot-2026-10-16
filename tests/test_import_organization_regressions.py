"""Import matching must be scoped to a known, shared organization INN."""
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import permutations

from unittest import TestCase

from app.normalization import normalize_rows
from app.reconciliation import reconcile, _aggregate_document_rows
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


COMMON = {
    "НоменклатураКод": "N1", "НоменклатураАртикул": "A1",
    "ЕдиницаИзмерения": "шт", "ПартияБух": "5",
    "НаименованиеОтчетКонтрагента": "Поставщик А", "ЭтоИмпорт": "Да",
}


def raw(document, qty, inn="111", organization=None, *, side="trade", **extra):
    organization = organization if organization is not None else (f"Организация {inn}" if inn else "")
    values = {
        "Период": "01.08.2026 10:00:00", "Документ": document,
        "Номенклатура": "Товар N1", "Склад": "Склад 1", "СкладКод": "W1",
        "Организация": organization, "ОрганизацияИНН": inn,
        "ЕдиницаИзмерения": "шт", "КоличествоНачальныйОстаток": 0,
        "СтоимостьНачальныйОстаток": 0, "КоличествоОборот": qty,
        "СтоимостьОборот": 0, "КоличествоКонечныйОстаток": 0,
        "СтоимостьКонечныйОстаток": 0, "ЗаказПоставщику": "Заказ 1", **COMMON,
    }
    if side == "tax":
        values["СкладКод"] = "БД-000003"
    values.update(extra)
    return values


def run(trade_raw, tax_raw):
    trade = [replace(r, warehouse_mapping_id="import") for r in normalize_rows(trade_raw, "Август 2026")]
    tax = [replace(r, warehouse_mapping_id="import") for r in normalize_rows(tax_raw, "Август 2026")]
    result = reconcile(trade, {"БУХ2019": tax}, "Август 2026",
                       start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    sheets = workbook_rows(build_action_excel_report(result))
    return result, sheets


def org_collision_inputs():
    trade = [
        raw("Поступление товаров и услуг UT111 от 01.08.2026 10:00:00", 5, "111",
            ЗаказПоставщику="Заказ UT111"),
        raw("Поступление товаров и услуг UT222 от 02.08.2026 10:00:00", 5, "222",
            ЗаказПоставщику="Заказ UT222"),
    ]
    tax = [raw("Поступление (акт, накладная, УПД) TAX111 от 03.08.2026 10:00:00",
               10, "111", side="tax")]
    return trade, tax


class ImportOrganizationRegressions(TestCase):
    def test_complete_one_sided_import_movements_route_by_side(self):
        ut = raw("Поступление товаров и услуг UT111 от 01.08.2026 10:00:00", 5)
        buh = raw("Поступление (акт, накладная, УПД) TAX111 от 03.08.2026 10:00:00", 5, side="tax")
        for trade, tax, sheet, counter in (([ut], [], "Довнести в БУХ", "missing_documents"),
                                            ([], [buh], "Лишние в БУХ", "extra_documents")):
            with self.subTest(sheet=sheet):
                result, sheets = run(trade, tax)
                self.assertEqual(len(sheets[sheet]), 2)
                self.assertEqual(getattr(result.summaries[0], counter), 1)
                self.assertTrue(all(d.document_existence == "unknown" for d in result.discrepancies))

    def test_invalid_candidate_row_blocks_partial_import_match_and_stays_visible(self):
        valid = raw("Поступление товаров и услуг UT111 от 01.08.2026 10:00:00", 5, "111",
                    ЗаказПоставщику="Заказ UT111")
        tax = raw("Поступление (акт, накладная, УПД) TAX111 от 03.08.2026 10:00:00", 5,
                  "111", side="tax")
        invalid_variants = {
            "missing unit": {"ЕдиницаИзмерения": ""},
            "missing supplier order": {"ЗаказПоставщику": ""},
        }
        for label, invalid_fields in invalid_variants.items():
            with self.subTest(label=label):
                invalid = raw("Поступление товаров и услуг UT222 от 02.08.2026 10:00:00", 5, "222",
                              ЗаказПоставщику="Заказ UT222") | invalid_fields
                result, sheets = run([valid, invalid], [tax])
                self.assertEqual(result.summaries[0].matched_import_groups, 0)
                headers = sheets["Проверить вручную"][0]
                manual = [dict(zip(headers, values)) for values in sheets["Проверить вручную"][1:]]
                self.assertEqual(len(manual), 3)
                self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
                self.assertEqual(len(sheets["Лишние в БУХ"]), 1)
                visible_documents = {
                    str(row.get("Документ УТ", "")) for row in manual if row.get("Документ УТ")
                } | {
                    str(row.get("Документ БУХ", "")) for row in manual if row.get("Документ БУХ")
                }
                self.assertTrue(any("UT111" in document for document in visible_documents))
                self.assertTrue(any("UT222" in document for document in visible_documents))
                self.assertTrue(any("TAX111" in document for document in visible_documents))

    def test_different_organization_imports_remain_three_original_manual_rows(self):
        trade_raw, tax_raw = org_collision_inputs()
        outputs = []
        for ordered_trade in (trade_raw, list(reversed(trade_raw))):
            result, sheets = run(ordered_trade, tax_raw)
            headers = sheets["Проверить вручную"][0]
            rows = [dict(zip(headers, values)) for values in sheets["Проверить вручную"][1:]]
            self.assertEqual(result.summaries[0].matched_import_groups, 0)
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
            self.assertEqual(len(sheets["Лишние в БУХ"]), 1)
            self.assertEqual({str(r["Документ УТ"]) for r in rows if r["Документ УТ"]},
                             {r["Документ"] for r in trade_raw})
            self.assertTrue(any("TAX111" in str(r.get("Документ БУХ", "")) for r in rows))
            self.assertEqual(Counter(str(r["Количество УТ"]) for r in rows if r["Количество УТ"]),
                             Counter({"5": 2}))
            self.assertTrue(any(str(r.get("Организация УТ", "")) == "Организация 222" for r in rows))
            self.assertTrue(any("UT222" in str(v) for row in sheets.values() for values in row for v in values))
            self.assertEqual({d.trade_row.supplier_order for d in result.discrepancies if d.trade_row},
                             {"Заказ UT111", "Заказ UT222"})
            reason_text = " ".join(str(r.get("Причина", "")) for r in rows)
            self.assertIn("Заказ UT111", reason_text)
            self.assertIn("Заказ UT222", reason_text)
            outputs.append((Counter(tuple(sorted((str(k), str(v)) for k, v in r.items())) for r in rows),
                            result.summaries[0]))
        self.assertEqual(outputs[0][0], outputs[1][0])
        self.assertEqual(outputs[0][1], outputs[1][1])

    def test_organization_must_be_known_and_match_on_both_sides(self):
        trade_base = raw("Поступление товаров и услуг UT1 от 01.08.2026 10:00:00", 5, "111")
        tax_base = raw("Поступление (акт, накладная, УПД) TAX1 от 02.08.2026 10:00:00", 5,
                       "111", side="tax")
        cases = {
            "empty both": (trade_base | {"ОрганизацияИНН": "", "Организация": ""},
                           tax_base | {"ОрганизацияИНН": "", "Организация": ""}),
            "name only": (trade_base | {"ОрганизацияИНН": ""}, tax_base | {"ОрганизацияИНН": ""}),
            "different known INNs": (trade_base, tax_base | {"ОрганизацияИНН": "222", "Организация": "Организация 222"}),
            "one side known": (trade_base, tax_base | {"ОрганизацияИНН": "", "Организация": ""}),
        }
        # Mixed tax identities within one candidate group are never safe to auto-match.
        mixed_tax = [tax_base, tax_base | {"Документ": "Поступление товаров и услуг TAX2 от 03.08.2026 10:00:00",
                                          "ОрганизацияИНН": "222", "Организация": "Организация 222"}]
        for label, (trade, tax) in cases.items():
            with self.subTest(label=label):
                result, sheets = run([trade], [tax])
                self.assertEqual(result.summaries[0].matched_import_groups, 0)
                self.assertEqual(len(sheets["Проверить вручную"]) - 1, 2)
                self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
                self.assertEqual(len(sheets["Лишние в БУХ"]), 1)
        result, sheets = run([trade_base], mixed_tax)
        self.assertEqual(result.summaries[0].matched_import_groups, 0)
        self.assertEqual(len(sheets["Проверить вручную"]) - 1, 3)
        self.assertEqual(len(sheets["Довнести в БУХ"]), 1)
        self.assertEqual(len(sheets["Лишние в БУХ"]), 1)

    def test_same_known_inn_matches_and_quantity_mismatch_refs_are_stable(self):
        trade = [
            raw("Поступление товаров и услуг UT1 от 01.08.2026 10:00:00", 5, "111"),
            raw("Поступление товаров и услуг UT2 от 02.08.2026 10:00:00", 5, "111"),
        ]
        tax = [raw("Поступление (акт, накладная, УПД) TAX1 от 03.08.2026 10:00:00", 10,
                   "111", side="tax")]
        result, _ = run(trade, tax)
        self.assertEqual(result.summaries[0].matched_import_groups, 1)
        self.assertFalse(any(d.discrepancy_type == "Несовпадение количества документа" for d in result.discrepancies))

        unequal_tax = [replace_raw(tax[0], qty=9)]
        signatures = []
        for permutation in permutations(trade):
            result, sheets = run(list(permutation), unequal_tax)
            mismatch = [d for d in result.discrepancies if "количеств" in d.discrepancy_type.lower()]
            self.assertTrue(mismatch)
            docs = mismatch[0].trade_document_refs
            self.assertEqual(docs, tuple(sorted(docs)))
            self.assertIsNone(mismatch[0].trade_source)
            self.assertEqual(mismatch[0].tax_source.opening_quantity, Decimal("0"))
            self.assertEqual(mismatch[0].tax_source.closing_quantity, Decimal("0"))
            headers = sheets["Проверить вручную"][0]
            manual = [dict(zip(headers, values)) for values in sheets["Проверить вручную"][1:]]
            self.assertEqual(len(manual), 1)
            self.assertEqual(manual[0]["Исходный начальный остаток УТ"], "")
            self.assertEqual(manual[0]["Исходный конечный остаток УТ"], "")
            self.assertEqual(manual[0]["Исходный начальный остаток БУХ"], "0")
            self.assertEqual(manual[0]["Исходный конечный остаток БУХ"], "0")
            signatures.append((docs, tuple(sorted(str(v) for row in manual for v in row.values()))))
        self.assertEqual(signatures[0], signatures[1])

    def test_generic_aggregation_keeps_distinct_organizations_separate(self):
        first = normalize_rows([raw("Поступление товаров и услуг UT1 от 01.08.2026 10:00:00", 5, "111")], "Август 2026")[0]
        second = replace(first, document="Поступление товаров и услуг UT2 от 02.08.2026 10:00:00",
                         organization_inn="222", organization="Организация 222", turnover_quantity=first.turnover_quantity)
        aggregated = _aggregate_document_rows([first, second])
        self.assertEqual(len(aggregated), 2)
        self.assertEqual({r.organization_inn for r in aggregated}, {"111", "222"})


def replace_raw(raw_row, *, qty):
    return raw_row | {"КоличествоОборот": qty}
