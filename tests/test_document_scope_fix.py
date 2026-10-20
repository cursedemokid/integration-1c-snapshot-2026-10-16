from unittest import TestCase
from dataclasses import replace
from app.normalization import normalize_rows
from app.reconciliation import reconcile
from app.reporting import build_action_excel_report
from scripts.verify_onec import workbook_rows
from tests.test_report_review_regressions import source

class RequestedScopeFixTests(TestCase):
    def test_internal_transfer_property_skips_document_without_tax_catalog_mapping(self):
        row = normalize_rows([source('Перемещение товаров 7 от 02.07.2026 10:00:00')], '')[0]
        row = replace(row, sender_accounting_warehouse='Основной склад',
                      receiver_accounting_warehouse='Основной склад')
        result = reconcile([row], {'БУХ2026': []}, '02.07.2026')
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Проверить вручную']), 1)
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 1)

    def test_outside_document_types_do_not_create_manual_rows(self):
        documents = ['Поступление доп. расходов ТК 1 от 02.07.2026 10:00:00',
                     'Корректировка записей регистров 2 от 02.07.2026 10:00:00',
                     'Возврат 3 от 02.07.2026 10:00:00']
        result = reconcile(normalize_rows([source(d) for d in documents], ''),
                           {'БУХ2026': []}, '02.07.2026')
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Проверить вручную']), 1)

    def test_unmatched_document_reports_actual_date_conflict(self):
        trade = source('Поступление товаров 7 от 02.07.2026 10:00:00')
        tax = source('Поступление товаров 7 от 03.07.2026 10:00:00')
        result = reconcile(normalize_rows([trade], ''), {'БУХ2026': normalize_rows([tax], '')}, '02.07.2026')
        doc = next(d for d in result.discrepancies if d.trade_row and d.trade_row.document)
        self.assertIn('дата', doc.difference_summary.lower())
        self.assertIn('03.07.2026', doc.difference_summary)
        self.assertEqual(doc.document_existence, 'unknown')

    def test_requirement_same_number_writeoff_is_a_candidate_without_proven_link(self):
        trade = source('Требование-накладная 7 от 02.07.2026 10:00:00', qty=-5)
        tax = source('Списание товаров, материалов 7 от 02.07.2026 10:00:00', qty=-6)
        result = reconcile(normalize_rows([trade], ''), {'БУХ2026': normalize_rows([tax], '')}, '02.07.2026')
        self.assertEqual(result.summaries[0].matched_documents, 0)
        sheets=workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Лишние в БУХ']), 1)
        self.assertEqual(len(sheets['Проверить вручную']), 3)
        self.assertTrue(any('связь конкретных документов' in d.difference_summary for d in result.discrepancies))

    def test_requirement_two_alternatives_stay_manual(self):
        trade = source('Требование-накладная 7 от 02.07.2026 10:00:00', qty=-5)
        tax = [source('Списание 7 от 02.07.2026 10:00:00', qty=-5),
               source('Безвозмездная передача 7 от 02.07.2026 10:00:00', qty=-6)]
        result = reconcile(normalize_rows([trade], ''), {'БУХ2026': normalize_rows(tax, '')}, '02.07.2026')
        self.assertEqual(result.summaries[0].matched_documents, 0)
        self.assertEqual(result.summaries[0].duplicate_documents, 0)
        self.assertEqual(len(workbook_rows(build_action_excel_report(result))['Проверить вручную']), 4)

    def test_missing_stock_boundaries_do_not_stop_document_quantity_comparison(self):
        trade=source(qty=5)
        tax=source(qty=6)
        result=reconcile(normalize_rows([trade],''),{'БУХ2026':normalize_rows([tax],'')},'02.07.2026')
        self.assertEqual(result.summaries[0].matched_documents, 1)
        self.assertEqual(result.summaries[0].document_quantity_differences, 1)

    def test_internal_scope_is_not_taken_from_first_aggregated_item(self):
        first = normalize_rows([source('Перемещение товаров 7 от 02.07.2026 10:00:00')], '')[0]
        first = replace(first, transfer_scope='internal')
        second = replace(first, turnover_quantity=first.turnover_quantity + 1, transfer_scope='external')
        result = reconcile([first, second], {'БУХ2026': []}, '02.07.2026')
        self.assertEqual(result.summaries[0].ignored_internal_transfers, 0)
        self.assertTrue(result.discrepancies)

    def test_internal_transfer_counterpart_is_not_reported_as_unmatched_tax_document(self):
        document='Перемещение товаров 7 от 02.07.2026 10:00:00'
        ut=replace(normalize_rows([source(document)],'')[0],transfer_scope='internal')
        tax=normalize_rows([source('Перемещение товаров, материалов 7 от 02.07.2026 10:00:00')],'')
        result=reconcile([ut],{'БУХ2026':tax},'02.07.2026')
        self.assertFalse(result.discrepancies)

    def test_manual_mapping_does_not_override_different_accounting_properties(self):
        document='Перемещение товаров 7 от 02.07.2026 10:00:00'
        first=replace(normalize_rows([source(document,wh='T1')],'')[0],warehouse_mapping_id='manual',
            sender_warehouse_code='T1',receiver_warehouse_code='T2',
            sender_accounting_warehouse='Основной',receiver_accounting_warehouse='Другой')
        second=replace(first,warehouse_code='T2',warehouse='T2')
        result=reconcile([first,second],{'БУХ2026':[]},'02.07.2026')
        self.assertEqual(result.summaries[0].ignored_internal_transfers,0)
        self.assertTrue(result.discrepancies)

class PartialWarehouseWorkflowTests(TestCase):
    def test_missing_2019_warehouse_does_not_stop_2026_pair(self):
        from unittest.mock import patch
        from app.config import OneCBaseConfig, Settings
        from app.models import Warehouse
        import main
        from datetime import date
        bases=[OneCBaseConfig(name, 'https://internal.internal.example.invalid/endpoint',base_id=bid,warehouses_url='https://internal.internal.example.invalid/endpoint/warehouse')
               for name,bid in [('УТ','trade'),('БУХ2019','buh2019'),('БУХ2026','buh2026')]]
        settings=Settings(bases[0],bases[1:])
        directories={'trade':[Warehouse('T1','Секция УТ',accounting_name='Основной')],
                     'buh2019':[Warehouse('B19','Другой')], 'buh2026':[Warehouse('B26','Основной')]}
        captured={}
        class Client:
            def fetch_warehouses(self,base): return directories[base.base_id]
            def fetch_rows(self,base,*_args,**_kwargs):
                code={'trade':'T1','buh2019':'B19','buh2026':'B26'}[base.base_id]
                return normalize_rows([source(wh=code)],'')
        original=main.reconcile
        def trace(**kwargs):
            result=original(**kwargs);captured['result']=result;return result
        with patch('main.OneCClient',return_value=Client()),patch('main.reconcile',side_effect=trace):
            main._reconcile(main.ReconcileRequest(start_date=date(2026,7,2),end_date=date(2026,7,2)),settings)
        summaries={s.base:s for s in captured['result'].summaries}
        self.assertEqual(summaries['БУХ2026'].matched_documents,1)
        self.assertEqual(summaries['БУХ2019'].matched_documents,0)
        limitation=next(n for n in captured['result'].notices if n.base=='БУХ2019')
        self.assertIn('Основной',limitation.reason)
        self.assertIn('БУХ2019',limitation.reason)
        self.assertFalse(any(d.base=='БУХ2019' for d in captured['result'].discrepancies))

    def test_selected_partial_mapping_does_not_require_filter_support_from_unmapped_base(self):
        from unittest.mock import patch
        from app.config import OneCBaseConfig, Settings
        from app.models import Warehouse
        from datetime import date
        import main
        bases=[OneCBaseConfig(name,'https://internal.internal.example.invalid/endpoint',base_id=bid,warehouses_url='https://internal.internal.example.invalid/endpoint/warehouse',
                             supports_warehouse_filter=bid!='buh2019')
               for name,bid in [('УТ','trade'),('БУХ2019','buh2019'),('БУХ2026','buh2026')]]
        directories={'trade':[Warehouse('T1','Секция',accounting_name='Основной')],
                     'buh2019':[Warehouse('B19','Другой')],'buh2026':[Warehouse('B26','Основной')]}
        calls=[]
        class Client:
            def fetch_warehouses(self,base): return directories[base.base_id]
            def fetch_rows(self,base,*_args,**kwargs):
                calls.append(base.base_id)
                code={'trade':'T1','buh2026':'B26'}[base.base_id]
                return [replace(r,warehouse_mapping_id=kwargs.get('warehouse_mapping_id',''))
                        for r in normalize_rows([source(wh=code)],'')]
        captured={};original=main.reconcile
        def trace(**kwargs):
            result=original(**kwargs);captured['result']=result;return result
        with patch('main.OneCClient',return_value=Client()),patch('main.reconcile',side_effect=trace):
            response=main._reconcile(main.ReconcileRequest(start_date=date(2026,7,2),end_date=date(2026,7,2),
                                      warehouses=['auto:buh2026:B26']),Settings(bases[0],bases[1:]))
        self.assertEqual(calls,['trade','buh2026'])
        self.assertEqual(captured['result'].summaries[0].matched_documents,1)
        self.assertIn('БУХ2019',str(workbook_rows(response.body)['Базы не проверены']))
