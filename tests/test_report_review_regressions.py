"""Business regressions from the 16 October report review, through real XLSX."""
from dataclasses import replace
from decimal import Decimal
from unittest import TestCase

from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reconciliation import MISSING_IN_TAX
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows


def source(document='Поступление товаров 7001 от 02.07.2026 10:00:00', *, code='A', qty=5, wh='W1', **extra):
    return {'Период': '02.07.2026 10:00:00', 'Документ': document,
            'Номенклатура': 'Товар '+code, 'НоменклатураКод': code, 'НоменклатураАртикул': 'ART-'+code,
            'Склад': wh, 'СкладКод': wh, 'Организация': 'Тест', 'ОрганизацияИНН': '111',
            'ЕдиницаИзмерения': 'шт', 'КоличествоНачальныйОстаток': 0,
            'СтоимостьНачальныйОстаток': 0, 'КоличествоОборот': qty, 'СтоимостьОборот': 0,
            'КоличествоКонечныйОстаток': 0, 'СтоимостьКонечныйОстаток': 0, **extra}


def run(trade, tax):
    result = reconcile(normalize_rows(trade, ''), {'БУХ2026': normalize_rows(tax, '')}, '02.07.2026')
    return result, workbook_rows(build_action_excel_report(result))


class ReportReviewRegressions(TestCase):
    def test_existing_document_shortage_is_manual_never_add(self):
        result, sheets = run([source(qty=7)], [source(qty=5)])
        self.assertEqual(len(sheets['Довнести в БУХ']), 1)
        row = sheets['Проверить вручную'][1]
        values = dict(zip(sheets['Проверить вручную'][0], row))
        self.assertEqual([values[h] for h in ('Количество УТ', 'Количество БУХ', 'Разница БУХ − УТ')], ['7', '5', '-2'])
        self.assertIn('количество существующего документа', row[0])
        self.assertEqual(result.discrepancies[0].document_existence, 'found')
        self.assertEqual(result.summaries[0].manual_item_rows, 1)

    def test_unmatched_movement_enters_add_without_claiming_card_absence(self):
        result, sheets = run([source()], [source('', qty=0, code='OTHER')])
        self.assertEqual(len(sheets['Довнести в БУХ']), 2)
        self.assertIn('не найден', sheets['Довнести в БУХ'][1][2])
        self.assertNotIn('подтверждено', sheets['Довнести в БУХ'][1][2])
        doc = next(d for d in result.discrepancies if d.trade_row and d.trade_row.document)
        self.assertEqual(doc.document_existence, 'unknown')
        self.assertEqual(result.summaries[0].missing_documents, 1)

    def test_unbounded_stock_input_does_not_claim_a_verified_final_balance(self):
        result = reconcile(normalize_rows([source('', qty=0, КоличествоКонечныйОстаток=2)], ''),
            {'БУХ2026': normalize_rows([source('', qty=0, КоличествоКонечныйОстаток=3)], '')}, 'Период неизвестен',
            )
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Расхождения остатков']), 1)
        self.assertIn('Конечный остаток не проверен', str(sheets['Итоги']))
        self.assertEqual(len(sheets['Проверить вручную']), 1)

    def test_other_warehouse_candidate_is_manual_on_both_sides(self):
        result, sheets = run([source(wh='UT_OTHER')], [source(wh='BUH_OTHER')])
        self.assertEqual(len(sheets['Довнести в БУХ']), 1)
        self.assertEqual(len(sheets['Лишние в БУХ']), 1)
        self.assertEqual(len(sheets['Проверить вручную'])-1, 2)
        self.assertIn('7001', ' '.join(str(d.difference_summary) for d in result.discrepancies))

    def test_matched_document_item_composition_errors_are_not_dropped(self):
        result, sheets = run([source(), source(code='B', qty=7)], [source(), source(code='C', qty=9)])
        self.assertEqual(len(sheets['Проверить вручную'])-1, 2)
        headers = sheets['Проверить вручную'][0]
        values = [dict(zip(headers, r)) for r in sheets['Проверить вручную'][1:]]
        self.assertEqual({r['Артикул'] for r in values}, {'ART-B', 'ART-C'})
        self.assertTrue(all(r['Документ УТ'] and r['Документ БУХ'] for r in values))
        self.assertTrue(all(r['Дата документа УТ'] and r['Дата документа БУХ'] for r in values))
        self.assertEqual(result.summaries[0].matched_documents, 1)

    def test_out_of_scope_unknown_and_unparsed_document_kinds_are_ignored(self):
        for document in ('Расход материалов 99 от 02.07.2026 10:00:00', 'Документ без формата'):
            with self.subTest(document=document):
                _, sheets = run([source(document, qty=-1)], [])
                self.assertFalse(any(document in str(r) for r in sheets['Проверить вручную'][1:]))
                self.assertEqual(len(sheets['Довнести в БУХ']), 1)  # Header only.

    def test_requested_unparsed_document_type_remains_visible_for_manual_review(self):
        document = 'Комплектация номенклатуры без стандартного номера'
        _, sheets = run([source(document, qty=-1)], [])
        self.assertTrue(any(document in str(r) for r in sheets['Проверить вручную'][1:]))

    def test_document_counters_separate_warehouse_groups(self):
        document = 'Перемещение товаров 7006 от 02.07.2026 10:00:00'
        extra = {'СкладОтправительКод':'W1', 'СкладПолучательКод':'W2'}
        records = [source(document, wh='W1', qty=-5, **extra), source(document, wh='W2', qty=5, **extra)]
        result, _ = run(records, records)
        self.assertEqual(result.summaries[0].trade_documents, 1)
        self.assertEqual(result.summaries[0].trade_document_groups, 2)

    def test_manual_mode_counts_same_document_text_per_organization(self):
        rows = normalize_rows([source(), source(ОрганизацияИНН='222', Организация='Другой')], '')
        result = reconcile(rows, {'БУХ2026': rows[:1]}, '02.07.2026')
        self.assertEqual(result.summaries[0].trade_documents, 2)
        self.assertEqual(result.summaries[0].unverified_documents, 3)

    def test_sale_control_is_one_pair_and_buh_minus_trade(self):
        ut = source('Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00', qty=-537,
                    НомерСФВыданный='ДЖ000001589', Период='02.08.2026 10:00:00')
        buh = source('Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00', qty=-550,
                     Период='02.08.2026 10:00:00')
        result, sheets = run([ut], [buh])
        self.assertEqual(result.summaries[0].matched_documents, 1)
        values = dict(zip(sheets['Лишние в БУХ'][0], sheets['Лишние в БУХ'][1]))
        self.assertEqual([values[h] for h in ('Количество УТ', 'Количество БУХ', 'Разница БУХ − УТ')], ['537', '550', '13'])
        self.assertEqual(len(sheets['Довнести в БУХ']), 1)
        _, equal = run([ut | {'КоличествоОборот': -550}], [buh])
        self.assertEqual(len(equal['Лишние в БУХ']), 1)

    def test_unknown_existence_cannot_produce_automatic_quantity_action(self):
        result, _ = run([source(qty=5)], [source(qty=7)])
        changed = replace(result, discrepancies=[replace(d, document_existence='unknown') for d in result.discrepancies])
        sheets = workbook_rows(build_action_excel_report(changed))
        self.assertEqual(len(sheets['Лишние в БУХ']), 1)
        self.assertEqual(len(sheets['Проверить вручную']), 2)

    def test_explicit_confirmed_absence_also_enters_add_sheet(self):
        result, _ = run([source()], [])
        confirmed = replace(result, discrepancies=[replace(d, discrepancy_type=MISSING_IN_TAX,
                            document_existence='confirmed_absent') for d in result.discrepancies])
        sheets = workbook_rows(build_action_excel_report(confirmed))
        self.assertEqual(len(sheets['Довнести в БУХ']), 2)
        self.assertIn('не найден', sheets['Довнести в БУХ'][1][2])

    def test_document_group_statistics_include_manual_document_kinds(self):
        known = source()
        pending = source('Комплектация номенклатуры 3 от 02.07.2026 10:00:00')
        excluded = source('Заказ покупателя 4 от 02.07.2026 10:00:00')
        result, sheets = run([known, pending, excluded], [known])
        summary = result.summaries[0]
        self.assertEqual(summary.trade_documents, 3)
        self.assertEqual(summary.trade_document_groups, 3)
        self.assertEqual(summary.trade_item_rows, 3)
        self.assertEqual(summary.manual_item_rows, len(sheets['Проверить вручную']) - 1)

    def test_tax_only_duplicate_is_counted_as_manual_review(self):
        trade = source()
        tax = [
            source(),
            source('Поступление товаров и услуг 7001 от 02.07.2026 10:00:00'),
        ]
        result, sheets = run([trade], tax)
        self.assertTrue(any(d.discrepancy_type == 'Дублирующая строка в бухгалтерии' for d in result.discrepancies))
        self.assertEqual(result.summaries[0].manual_item_rows, len(sheets['Проверить вручную']) - 1)

    def test_unmatched_requested_tax_document_enters_extra_sheet(self):
        tax_only = source('Оприходование товаров 77 от 02.07.2026 10:00:00')
        result, sheets = run([], [tax_only])
        self.assertTrue(any(d.discrepancy_type == 'Лишняя строка в бухгалтерии' for d in result.discrepancies))
        self.assertEqual(len(sheets['Лишние в БУХ']), 2)
        self.assertEqual(result.summaries[0].extra_documents, 1)
        self.assertEqual(result.summaries[0].manual_item_rows, len(sheets['Проверить вручную']) - 1)

    def test_same_number_different_organization_is_only_a_manual_candidate(self):
        result, sheets = run([source(qty=5)], [source(qty=7, Организация='Другой', ОрганизацияИНН='222')])
        self.assertEqual(result.summaries[0].matched_documents, 0)
        self.assertEqual(len(sheets['Проверить вручную']) - 1, 2)
        self.assertTrue(all(d.document_existence == 'unknown' for d in result.discrepancies))
        self.assertEqual(len(sheets['Лишние в БУХ']), 1)
