"""Regression coverage for compact manual document cases with full details."""
from datetime import date
from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from app.models import Discrepancy, LedgerRow, ReconciliationNotice, ReconciliationResult, SourceObservation
from app.report_details import expand_manual_rows
from app.reconciliation import AMBIGUOUS_MATCH, DOCUMENT_QUANTITY_MISMATCH
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


DOCUMENT = "Реализация товаров и услуг 7001 от 02.07.2026 10:00:00"


def _row(index: int, *, warehouse="Логический / физический", org_inn="111") -> LedgerRow:
    return LedgerRow(
        period=f"02.07.2026 10:0{index}:00",
        document=DOCUMENT,
        nomenclature=f"Товар {index}",
        opening_quantity=Decimal(index),
        opening_amount=Decimal(index * 10),
        turnover_quantity=Decimal(index * 100),
        turnover_amount=Decimal(index * 1000),
        closing_quantity=Decimal(index * 101),
        closing_amount=Decimal(index * 1010),
        nomenclature_code=f"CODE-{index}",
        article=f"ART-{index}",
        warehouse=warehouse,
        warehouse_code=warehouse,
        warehouse_mapping_id="map-1",
        organization="Организация",
        organization_inn=org_inn,
    )


def _manual(index: int, *, candidate="Кандидат 1", warehouse="Логический / физический",
            org_inn="111") -> Discrepancy:
    row = _row(index, warehouse=warehouse, org_inn=org_inn)
    return Discrepancy(
        period=row.period,
        base="БУХ2026",
        document=row.document,
        nomenclature=row.nomenclature,
        opening_quantity=row.opening_quantity,
        opening_amount=row.opening_amount,
        turnover_quantity=row.turnover_quantity,
        turnover_amount=row.turnover_amount,
        closing_quantity=row.closing_quantity,
        closing_amount=row.closing_amount,
        discrepancy_type=AMBIGUOUS_MATCH,
        recommendation="Проверить",
        nomenclature_code=row.nomenclature_code,
        article=row.article,
        warehouse=row.warehouse,
        warehouse_code=row.warehouse_code,
        warehouse_mapping_id=row.warehouse_mapping_id,
        organization=row.organization,
        organization_inn=row.organization_inn,
        trade_row=row,
        match_method="Кандидат",
        difference_summary="Есть возможный документ-кандидат",
        document_existence="unknown",
        trade_document_refs=(candidate,),
        trade_source=SourceObservation(row.period, row.opening_quantity, row.closing_quantity),
        balance_control_date=date(2026, 7, 2),
    )


def _manual_rows(discrepancies):
    sheets = workbook_rows(build_action_excel_report(discrepancies))
    return sheets["Проверить вручную"][0], sheets["Проверить вручную"][1:]


class CompactManualReportTests(TestCase):
    def test_compact_case_keeps_observed_boundary_context(self):
        findings = [replace(_manual(index), balance_control_date=None,
                            trade_source=SourceObservation(
                                '31.07.2026 23:59:59', Decimal(index), Decimal(index + 1),
                                'closing', date(2026, 7, 31)))
                    for index in (1, 2)]
        headers, rows = _manual_rows(findings)
        self.assertEqual(len(rows), 1)
        cells = dict(zip(headers, rows[0]))
        self.assertEqual(cells['Контрольная точка'], 'Конечный остаток')
        self.assertEqual(cells['Дата контроля'], '31.07.2026')
        self.assertEqual(cells['Исходный конечный остаток УТ'], '2\n3')

    def test_source_notices_are_aggregated_on_summary_only(self):
        result = ReconciliationResult(
            discrepancies=[],
            notices=[ReconciliationNotice("БУХ2026", "Нет подтверждённой границы остатков", 12)],
        )

        sheets = workbook_rows(build_action_excel_report(result))
        summary = sheets["Итоги"]
        section_index = next(i for i, row in enumerate(summary) if row and row[0] == "Ограничения сверки")
        self.assertEqual(summary[section_index + 1], ["База", "Ограничение", "Затронуто строк"])
        self.assertEqual(summary[section_index + 2], ["БУХ2026", "Нет подтверждённой границы остатков", "12"])
        self.assertEqual(len(sheets["Проверить вручную"]), 1)

    def test_same_document_case_compacts_rows_and_preserves_aligned_item_and_source_details(self):
        headers, rows = _manual_rows([_manual(1), _manual(2), _manual(3)])

        self.assertEqual(len(rows), 1)
        values = dict(zip(headers, rows[0]))
        self.assertEqual(values["Артикул"].split("\n"), ["ART-1", "ART-2", "ART-3"])
        self.assertEqual(values["Номенклатура"].split("\n"), ["Товар 1", "Товар 2", "Товар 3"])
        self.assertEqual(values["Количество УТ"].split("\n"), ["100", "200", "300"])
        self.assertEqual(values["Период исходной строки УТ"].split("\n"), [
            "02.07.2026 10:01:00", "02.07.2026 10:02:00", "02.07.2026 10:03:00",
        ])
        self.assertEqual(values["Исходный начальный остаток УТ"].split("\n"), ["1", "2", "3"])
        self.assertEqual(values["Исходный конечный остаток УТ"].split("\n"), ["101", "202", "303"])
        self.assertEqual(values["Количество БУХ"].split("\n"), ["", "", ""])
        self.assertEqual(values["Период исходной строки БУХ"].split("\n"), ["", "", ""])
        self.assertEqual(values["Исходный начальный остаток БУХ"].split("\n"), ["", "", ""])
        self.assertEqual(values["Исходный конечный остаток БУХ"].split("\n"), ["", "", ""])
        self.assertEqual(values["Разница БУХ − УТ"].split("\n"), ["", "", ""])
        self.assertEqual(values["Документ УТ"], "Кандидат 1")
        self.assertEqual(values["Номер"], "7001")
        expanded = expand_manual_rows(headers, [values])
        self.assertEqual([row["Номенклатура"] for row in expanded], ["Товар 1", "Товар 2", "Товар 3"])
        self.assertEqual([row["Количество УТ"] for row in expanded], ["100", "200", "300"])
        self.assertEqual([row["Исходный начальный остаток УТ"] for row in expanded], ["1", "2", "3"])

    def test_distinct_warehouse_organization_or_candidate_scope_does_not_collapse(self):
        _, rows = _manual_rows([
            _manual(1),
            _manual(2, warehouse="Другой физический склад"),
            _manual(3, org_inn="222"),
            _manual(4, candidate="Кандидат 2"),
        ])

        self.assertEqual(len(rows), 4)

    def test_existing_document_quantity_537_vs_550_remains_a_single_unchanged_action(self):
        trade = _row(1)
        tax = _row(1)
        trade = LedgerRow(**{**trade.__dict__, "turnover_quantity": Decimal("-537")})
        tax = LedgerRow(**{**tax.__dict__, "turnover_quantity": Decimal("-550")})
        discrepancy = Discrepancy(
            period=trade.period,
            base="БУХ2019",
            document=trade.document,
            nomenclature=trade.nomenclature,
            opening_quantity=Decimal("0"), opening_amount=Decimal("0"),
            turnover_quantity=Decimal("-537"), turnover_amount=Decimal("0"),
            closing_quantity=Decimal("0"), closing_amount=Decimal("0"),
            discrepancy_type=DOCUMENT_QUANTITY_MISMATCH,
            recommendation="Проверить количество",
            article=trade.article,
            nomenclature_code=trade.nomenclature_code,
            trade_row=trade,
            tax_row=tax,
            document_existence="found",
        )

        sheets = workbook_rows(build_action_excel_report([discrepancy]))
        row = sheets["Лишние в БУХ"][1]
        values = dict(zip(sheets["Лишние в БУХ"][0], row))
        self.assertEqual(values["Количество УТ"], "537")
        self.assertEqual(values["Количество БУХ"], "550")
        self.assertEqual(values["Разница БУХ − УТ"], "13")
        self.assertEqual(len(sheets["Лишние в БУХ"]) - 1, 1)
