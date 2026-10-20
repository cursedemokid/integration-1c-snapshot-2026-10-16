"""Normal matching must run without global approval switches."""
from unittest import TestCase
from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows
from tests.test_report_review_regressions import source


class DefaultReconciliationTests(TestCase):
    def sale(self, *, unit='шт', invoice='ДЖ000001589'):
        ut=source('Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00',
                  qty=-537, НомерСФВыданный=invoice, ЕдиницаИзмерения=unit, Период='02.08.2026 10:00:00')
        tax=source('Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00',
                   qty=-550, ЕдиницаИзмерения=unit, Период='02.08.2026 10:00:00')
        result=reconcile(normalize_rows([ut],''),{'БУХ2019':normalize_rows([tax],'')},'02.08.2026')
        return result,workbook_rows(build_action_excel_report(result))

    def test_default_call_matches_sale_and_reports_buh_minus_trade(self):
        result,sheets=self.sale()
        self.assertEqual(result.summaries[0].matched_documents,1)
        self.assertEqual(len(sheets['Лишние в БУХ']),2)
        row=dict(zip(sheets['Лишние в БУХ'][0],sheets['Лишние в БУХ'][1]))
        self.assertEqual([row[h] for h in ('Количество УТ','Количество БУХ','Разница БУХ − УТ')],['537','550','13'])
        self.assertEqual(len(sheets['Довнести в БУХ']),1)

    def test_explicitly_blank_unit_does_not_block_pair_comparison(self):
        result,sheets=self.sale(unit='')
        self.assertEqual(result.summaries[0].matched_documents,1)
        self.assertEqual(len(sheets['Лишние в БУХ']),2)
        row=dict(zip(sheets['Лишние в БУХ'][0],sheets['Лишние в БУХ'][1]))
        self.assertEqual(row['Разница БУХ − УТ'],'13')
        self.assertFalse(any('единиц' in str(r) for r in sheets['Проверить вручную'][1:]))

    def test_empty_invoice_remains_manual_without_guessing_by_quantity(self):
        result,sheets=self.sale(invoice='')
        self.assertEqual(result.summaries[0].matched_documents,0)
        self.assertEqual(len(sheets['Лишние в БУХ']),1)
        self.assertEqual(len(sheets['Довнести в БУХ']),1)
        self.assertGreaterEqual(len(sheets['Проверить вручную']),3)

    def test_unknown_unit_for_one_item_does_not_disable_other_items(self):
        trade_doc='Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00'
        tax_doc='Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00'
        common={'НомерСФВыданный':'ДЖ000001589','Период':'02.08.2026 10:00:00'}
        trade=[source(trade_doc,code='A',qty=-537,**common),
               source(trade_doc,code='B',qty=-7,ЕдиницаИзмерения='',**common)]
        tax=[source(tax_doc,code='A',qty=-550,**common),
             source(tax_doc,code='B',qty=-5,ЕдиницаИзмерения='',**common)]
        result=reconcile(normalize_rows(trade,''),{'БУХ2019':normalize_rows(tax,'')},'02.08.2026')
        sheets=workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Лишние в БУХ']),2)
        self.assertEqual(len(sheets['Проверить вручную']),2)
        extra=dict(zip(sheets['Лишние в БУХ'][0],sheets['Лишние в БУХ'][1]))
        manual=dict(zip(sheets['Проверить вручную'][0],sheets['Проверить вручную'][1]))
        self.assertEqual((extra['Артикул'],extra['Разница БУХ − УТ']),('ART-A','13'))
        self.assertEqual((manual['Артикул'],manual['Разница БУХ − УТ']),('ART-B','-2'))
        self.assertIn('расхождение количества внутри документа', manual['Причина'].lower())
        self.assertIn('разница -2', manual['Причина'].lower())
