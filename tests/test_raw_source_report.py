"""R12: observed source balances must survive the manual handling of rows with unknown units."""
from dataclasses import replace
from datetime import date
from unittest import TestCase

from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows
from tests.test_balance_review_regressions import raw, START, END


RAW_HEADERS = [
    'Период исходной строки УТ', 'Исходный начальный остаток УТ', 'Исходный конечный остаток УТ',
    'Период исходной строки БУХ', 'Исходный начальный остаток БУХ', 'Исходный конечный остаток БУХ',
]


def manual(trade, tax, *, known_period=True):
    kwargs = {'start_date': date(2026, 9, 1), 'end_date': date(2026, 9, 30)} if known_period else {}
    fallback_document = 'Поступление товаров 7001 от 30.09.2026 12:30:00'
    trade = [record | {'Документ': record.get('Документ') or fallback_document,
                       'ЕдиницаИзмерения': 'шт'} for record in trade]
    tax = [record | {'Документ': record.get('Документ') or fallback_document,
                     'ЕдиницаИзмерения': 'кг'} for record in tax]
    result = reconcile(normalize_rows(trade, ''), {'БУХ2026': normalize_rows(tax, '')},
                       'сентябрь' if known_period else 'неизвестно', **kwargs)
    sheets = workbook_rows(build_action_excel_report(result))
    headers = sheets['Проверить вручную'][0]
    return result, sheets, [dict(zip(headers, r)) for r in sheets['Проверить вручную'][1:]]


class RawSourceReportTests(TestCase):
    def test_end_with_document_preserves_movement_and_raw_balances(self):
        doc = 'Поступление товаров и услуг ДЖ000000971 от 30.09.2026 12:30:00'
        result, sheets, values = manual([raw(END, doc, КоличествоНачальныйОстаток=7,
                                           КоличествоКонечныйОстаток=1)],
                                       [raw(END, doc, КоличествоНачальныйОстаток=0)])
        headers = sheets['Проверить вручную'][0]
        self.assertEqual(headers[-6:], RAW_HEADERS)
        self.assertEqual(headers[22:25], ['Количество УТ', 'Количество БУХ', 'Разница БУХ − УТ'])
        ut = next(v for v in values if v['Документ УТ'])
        self.assertEqual(ut['Количество УТ'], '0')
        self.assertEqual(ut['Исходный начальный остаток УТ'], '7')
        self.assertEqual(ut['Исходный конечный остаток УТ'], '1')
        self.assertEqual(ut['Период исходной строки УТ'], END)
        self.assertEqual(ut['Контрольная точка'], 'Конечный остаток')
        self.assertEqual(ut['Дата контроля'], '30.09.2026')
        self.assertEqual(ut['Документ УТ'], doc)
        self.assertEqual(ut['Дата документа УТ'], '30.09.2026 12:30:00')
        tax = next(v for v in values if v['Период исходной строки БУХ'])
        self.assertEqual(tax['Исходный конечный остаток БУХ'], '0')
        self.assertEqual(ut['Исходный начальный остаток БУХ'], '0')
        self.assertEqual(result.summaries[0].manual_item_rows, 1)
        self.assertEqual(len(values), 1)
        self.assertEqual(len(sheets['Довнести в БУХ']), 1)
        self.assertEqual(len(sheets['Лишние в БУХ']), 1)
        self.assertEqual(ut['Разница БУХ − УТ'], '')
        # Observing a source boundary never asserts a verified interbase balance.
        self.assertTrue(all(d.balance_point == '' for d in result.discrepancies))

    def test_opening_uses_its_period_without_losing_different_end_value(self):
        doc = 'Поступление товаров и услуг ДЖ000000972 от 01.09.2026 10:00:00'
        _, _, values = manual([raw(START, doc, КоличествоНачальныйОстаток=1,
                                  КоличествоКонечныйОстаток=9)],
                              [raw(START, doc, КоличествоНачальныйОстаток=2,
                                   КоличествоКонечныйОстаток=8)])
        self.assertEqual(values[0].get('Исходный начальный остаток УТ'), '1')
        self.assertEqual(values[0].get('Исходный конечный остаток УТ'), '9')
        self.assertEqual(values[0]['Контрольная точка'], 'Начальный остаток')
        self.assertEqual(values[0]['Дата контроля'], '01.09.2026')

    def test_midpoint_unknown_period_and_unbounded_query_do_not_invent_boundary(self):
        for period, known in [('15.09.2026 23:59:59', True), (END, False),
                              ('01.09.2026 12:00:00', True), ('неизвестно', True)]:
            with self.subTest(period=period, known=known):
                doc = 'Поступление товаров и услуг ДЖ000000973 от 15.09.2026 10:00:00'
                _, _, values = manual([raw(period, doc, КоличествоНачальныйОстаток=0,
                                          КоличествоКонечныйОстаток=1)],
                                      [raw(period, doc, КоличествоНачальныйОстаток=0,
                                           КоличествоКонечныйОстаток=1)], known_period=known)
                row = values[0]
                self.assertEqual(row.get('Период исходной строки УТ'), period)
                self.assertEqual(row.get('Исходный начальный остаток УТ'), '0')
                self.assertEqual(row.get('Исходный конечный остаток УТ'), '1')
                self.assertEqual(row['Контрольная точка'], '')
                self.assertEqual(row['Дата контроля'], '')
                self.assertEqual(row['Количество БУХ'], '0')
                self.assertEqual(row['Разница БУХ − УТ'], '')

    def test_paired_different_source_boundaries_have_no_shared_point(self):
        doc = 'Поступление товаров 7001 от 01.09.2026 10:00:00'
        result = reconcile(normalize_rows([raw('01.09.2026 00:00:00', doc, КоличествоОборот=7)], ''),
                           {'БУХ2026': normalize_rows([raw('01.09.2026 23:59:59', doc, КоличествоОборот=5)], '')},
                           '01.09.2026')
        sheets = workbook_rows(build_action_excel_report(result))
        values = [dict(zip(sheets['Проверить вручную'][0], r)) for r in sheets['Проверить вручную'][1:]]
        paired = next(v for v in values if v['Документ УТ'] and v['Документ БУХ'])
        self.assertEqual(paired['Контрольная точка'], '')
        self.assertEqual(paired['Дата контроля'], '')
