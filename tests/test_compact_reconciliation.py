"""Compact reconciliation contract tests from normalized 1C JSON through XLSX."""
from datetime import date
from dataclasses import replace
from decimal import Decimal
import unittest

from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


START = date(2026, 7, 1)
END = date(2026, 7, 31)
BASE = {
    "Номенклатура": "Товар A",
    "НоменклатураКод": "A",
    "НоменклатураАртикул": "ART-A",
    "Склад": "Склад 1",
    "СкладКод": "W1",
    "Организация": "Тест",
    "ОрганизацияИНН": "111",
    "КоличествоНачальныйОстаток": 0,
    "СтоимостьНачальныйОстаток": 0,
    "КоличествоОборот": 0,
    "СтоимостьОборот": 0,
    "КоличествоКонечныйОстаток": 0,
    "СтоимостьКонечныйОстаток": 0,
}


def raw(period, **values):
    return {"Период": period, **BASE, **values}


def run(trade, tax, *, start=START, end=END):
    result = reconcile(
        normalize_rows(trade, ""),
        {"БУХ2026": normalize_rows(tax, "")},
        "01.07.2026–31.07.2026",
        start_date=start,
        end_date=end,
    )
    sheets = workbook_rows(build_action_excel_report(result))
    return result, sheets


def manual_rows(sheets):
    return sheets["Проверить вручную"][1:]


def all_cells(sheets, name="Итоги"):
    return " ".join(str(cell) for row in sheets[name] for cell in row)


class CompactReconciliationTests(unittest.TestCase):
    def test_equal_opening_without_end_is_notice_and_no_manual_row(self):
        result, sheets = run(
            [raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=4)],
            [raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=4)],
        )

        assert manual_rows(sheets) == []
        assert any(n.base == "БУХ2026" and "конеч" in n.reason.lower() and n.affected_rows > 0
                   for n in result.notices)
        assert "конеч" in all_cells(sheets).lower()


    def test_one_sided_observed_boundary_is_notice_without_zero_comparison(self):
        result, sheets = run(
            [raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=7)],
            [],
        )

        assert manual_rows(sheets) == []
        notice = next(n for n in result.notices if n.base == "БУХ2026" and "началь" in n.reason.lower())
        assert "началь" in notice.reason.lower()
        assert not sheets["Расхождения остатков"][1:]
        assert all(value not in (Decimal("0"), 0)
                   for row in result.discrepancies if row.balance_point == "opening"
                   for value in (row.balance_trade_quantity, row.balance_tax_quantity))


    def test_real_opening_and_closing_differences_remain_two_balance_rows(self):
        result, sheets = run(
            [
                raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=2),
                raw("31.07.2026 23:59:59", КоличествоКонечныйОстаток=2),
            ],
            [
                raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=1),
                raw("31.07.2026 23:59:59", КоличествоКонечныйОстаток=1),
            ],
        )

        rows = sheets["Расхождения остатков"][1:]
        assert len(rows) == 2
        assert {row[5] for row in rows} == {"-1"}
        assert len([d for d in result.discrepancies if d.discrepancy_type == "Различие остатков количества"]) == 2


    def test_closing_balance_on_document_row_is_preserved(self):
        closing = "Дополнительные расходы 8 от 31.07.2026 12:00:00"
        result, sheets = run(
            [raw("31.07.2026 23:59:59", Документ=closing, КоличествоКонечныйОстаток=7)],
            [raw("31.07.2026 23:59:59", Документ=closing, КоличествоКонечныйОстаток=5)],
        )

        rows = sheets["Расхождения остатков"][1:]
        headers = sheets["Расхождения остатков"][0]
        values = dict(zip(headers, rows[0]))
        assert len(rows) == 1
        assert values["Тип остатка"] == "Конечный остаток"
        assert [values[h] for h in ("Остаток в УТ", "Остаток в НБ", "Расхождение")] == ["7", "5", "-2"]
        assert any(
            discrepancy.balance_point == "closing"
            and discrepancy.trade_row is not None
            and closing in discrepancy.trade_row.related_documents
            for discrepancy in result.discrepancies
        )
        assert not manual_rows(sheets)


    def test_many_unmapped_stock_rows_collapse_into_one_notice(self):
        source_rows = [
            raw("01.07.2026 00:00:00", Номенклатура=f"Товар {i}", НоменклатураКод=f"A{i}",
                СкладКод=f"W{i}")
            for i in range(100)
        ]
        rows = [replace(row, warehouse_mapping_error="Склад не сопоставлен")
                for row in normalize_rows(source_rows, "")]
        # Keep the tax side as normalized source rows too, as the app does before
        # attaching a mapping diagnostic from its warehouse-directory lookup.
        tax_rows = [replace(row, warehouse_mapping_error="Склад не сопоставлен")
                    for row in normalize_rows(source_rows, "")]
        result = reconcile(rows, {"БУХ2026": tax_rows}, "01.07.2026–31.07.2026",
                           start_date=START, end_date=END)
        sheets = workbook_rows(build_action_excel_report(result))

        assert not manual_rows(sheets)
        notices = [n for n in result.notices if n.base == "БУХ2026" and "сопостав" in n.reason.lower()]
        assert len(notices) == 1
        assert notices[0].affected_rows == 200
        assert "200" in all_cells(sheets)


    def test_unique_source_guid_matches_sales_when_invoice_number_is_blank(self):
        trade = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
            ДокументGUID="{7e9061fe-d89a-55c4-a8c8-495cf7b0050b}",
            КоличествоОборот=-537,
        )
        tax = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
            ДокументОснованиеGUID="{7e9061fe-d89a-55c4-a8c8-495cf7b0050b}",
            КоличествоОборот=-550,
        )
        result, sheets = run([trade], [tax], start=date(2026, 8, 1), end=date(2026, 8, 31))

        assert result.summaries[0].matched_documents == 1
        discrepancy = next(d for d in result.discrepancies if d.document_existence == "found")
        assert discrepancy.match_method == "Идентификатор исходного документа"
        assert discrepancy.difference_summary.endswith("разница 13")
        row = sheets["Лишние в БУХ"][1]
        assert row[22:25] == ["537", "550", "13"]
        assert not sheets["Довнести в БУХ"][1:]


    def test_omitted_unit_metadata_does_not_block_equal_exported_quantities(self):
        trade = raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=8)
        tax = raw("01.07.2026 00:00:00", КоличествоНачальныйОстаток=8)
        result, sheets = run([trade], [tax])

        assert not sheets["Расхождения остатков"][1:]
        assert not manual_rows(sheets)
        assert not any("единиц" in n.reason.lower() or "упаков" in n.reason.lower()
                       for n in result.notices)
        assert all(d.discrepancy_type != "Неоднозначное сопоставление"
                   for d in result.discrepancies)

    def test_missing_unit_fields_do_not_block_document_quantity_delta(self):
        trade = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
            НомерСФВыданный="ДЖ000001589",
            КоличествоОборот=-537,
        )
        tax = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
            КоличествоОборот=-550,
        )
        trade.pop("ЕдиницаИзмерения", None)
        tax.pop("ЕдиницаИзмерения", None)
        result, sheets = run([trade], [tax], start=date(2026, 8, 1), end=date(2026, 8, 31))

        assert result.summaries[0].matched_documents == 1
        assert not any("единиц" in d.difference_summary.lower() for d in result.discrepancies)
        assert sheets["Лишние в БУХ"][1][22:25] == ["537", "550", "13"]
        assert not sheets["Проверить вручную"][1:]

    def test_explicitly_blank_unit_with_equal_document_quantity_is_not_manual(self):
        trade = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00",
            НомерСФВыданный="ДЖ000001589",
            КоличествоОборот=-537,
            ЕдиницаИзмерения="",
        )
        tax = raw(
            "02.08.2026 10:00:00",
            Документ="Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00",
            КоличествоОборот=-537,
            ЕдиницаИзмерения="",
        )
        result, sheets = run([trade], [tax], start=date(2026, 8, 1), end=date(2026, 8, 31))

        assert result.summaries[0].matched_documents == 1
        assert not result.discrepancies
        assert not sheets["Проверить вручную"][1:]
