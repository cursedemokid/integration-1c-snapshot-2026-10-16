# Optional tests for local-only pilot utilities. Public CI remains portable.
import importlib.util as _import_util
import unittest as _unittest
_missing_local_tools = [name for name in (
    'scripts.final_acceptance_audit',
    'scripts.pilot_control',
    'scripts.report_control_recheck',
    'scripts.report_fix_control',
) if _import_util.find_spec(name) is None]
if _missing_local_tools:
    raise _unittest.SkipTest("Local pilot utilities are not shipped: " + ", ".join(_missing_local_tools))

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_final_acceptance_audit import _settings, _row
from scripts.pilot_control import run_control, replay_service
from app.models import Discrepancy, ReconciliationResult, Warehouse
from app.reconciliation import AMBIGUOUS_MATCH, BALANCE_UNCHECKED, DOCUMENT_QUANTITY_MISMATCH
from app.report_details import expand_manual_rows


class PilotControlTests(unittest.TestCase):
    def test_report_fix_replay_preserves_summary_notices(self):
        from scripts.report_fix_control import _replay
        from scripts.final_acceptance_audit import _bases
        settings = _settings()
        directories = {bid: [] for bid in ('trade', 'buh2019', 'buh2026')}
        exports = {bid: [_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00')]
                   for bid in directories}
        with tempfile.TemporaryDirectory() as tmp:
            result = _replay(settings, _bases(settings), directories, [], exports,
                             date(2026, 8, 2), date(2026, 8, 2), Path(tmp))
        self.assertTrue(result['passed'])
        self.assertTrue(result['notices'])
        self.assertEqual(result['source_row_count_validation'], 'not_applicable')
        self.assertEqual(result['sheets']['Проверить вручную'], 0)

    def test_unmatched_movement_action_counts_group_products_by_document(self):
        from app.normalization import normalize_rows
        from app.reconciliation import reconcile
        from app.reporting import build_action_excel_report
        from scripts.pilot_control import _expected_automatic_action_counts
        from scripts.verify_onec import workbook_rows
        from tests.test_report_review_regressions import source

        trade = normalize_rows([source(code='A'), source(code='B')], '')
        tax = normalize_rows([source('Оприходование товаров 77 от 02.07.2026 10:00:00')], '')
        result = reconcile(trade, {'БУХ2026': tax}, '02.07.2026')
        self.assertEqual(_expected_automatic_action_counts(result),
                         {'Довнести в БУХ': 1, 'Лишние в БУХ': 1})
        sheets = workbook_rows(build_action_excel_report(result))
        self.assertEqual(len(sheets['Довнести в БУХ']), 2)
        self.assertEqual(len(sheets['Лишние в БУХ']), 2)

    def test_replay_ignores_service_added_warehouse_metadata(self):
        settings = _settings()
        trade = replace(_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00'),
                        warehouse_code='T1', warehouse='Trade')
        tax = replace(trade, warehouse_code='B1', warehouse='Tax', turnover_amount=Decimal('11'))
        exports = {'trade': [trade], 'buh2019': [tax], 'buh2026': [tax]}
        directories = {
            'trade': [Warehouse('T1', 'Trade', accounting_name='Tax')],
            'buh2019': [Warehouse('B1', 'Tax')],
            'buh2026': [Warehouse('B1', 'Tax')],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(
                settings, exports, date(2026, 8, 2), date(2026, 8, 2), Path(tmp),
                warehouse_directories=directories,
            )
        self.assertEqual(result['source_row_count_validation'], 'not_applicable')
        self.assertTrue(result['summary_source_inventory_matches_exports'])
        self.assertTrue(result['passed'])

    def test_replay_retains_all_source_rows_without_network_or_auto_actions(self):
        settings = _settings()
        row = _row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00')
        exports = {base: [row] for base in ('trade', 'buh2019', 'buh2026')}
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(settings, exports, date(2026, 8, 2), date(2026, 8, 2), Path(tmp))
            self.assertEqual(result['source_row_count_validation'], 'not_applicable')
            self.assertTrue(result['summary_source_inventory_matches_exports'])
            self.assertEqual(result['automatic_rows'], 0)
            self.assertEqual(result['fetch_calls'], {'trade': 1, 'buh2019': 1, 'buh2026': 1})
            self.assertTrue(result['passed'])

    def test_missing_tax_source_is_visible_and_cannot_pass(self):
        row = _row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00')
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(_settings(), {'trade': [row], 'buh2026': [row]}, date(2026, 8, 2), date(2026, 8, 2), Path(tmp))
            self.assertFalse(result['passed'])
            self.assertEqual(result['unchecked_bases'], 1)
            self.assertTrue(result['source_errors_visible'])
            self.assertEqual(result['automatic_rows'], 0)

    def test_fetch_once_and_no_raw_commercial_values_in_manifest(self):
        row = _row('Реализация товаров и услуг PRIVATE-SECRET от 01.07.2026 10:00:00')
        calls = []
        def audit(base, begin, end, timeout, on_rows=None):
            calls.append(base.base_id)
            on_rows([row])
            return {'base_id': base.base_id, 'checked': True, 'counts': {'rows': 1}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run'
            with patch('scripts.pilot_control._audit_one', side_effect=audit), patch('scripts.pilot_control._load_mapping_context', return_value=([], {'checked': True}, {})):
                result = run_control(_settings(), [(date(2026, 7, 1), date(2026, 7, 1))], [], path, 1800)
            self.assertEqual(calls, ['trade', 'buh2019', 'buh2026'])
            self.assertNotIn('PRIVATE-SECRET', (path / 'run.json').read_text())
            self.assertTrue(result['human_signoff_pending'])
            self.assertFalse(result['pilot_authorized'])
            self.assertTrue(result['periods'][0]['service']['passed'])
            self.assertEqual((path / 'run.json').stat().st_mode & 0o777, 0o600)

    def test_settings_has_no_approval_flag_api(self):
        from app.config import load_settings
        with patch.dict('os.environ', {
            'ONEC_PRODUCT_KEY_APPROVED': 'true',
            'ONEC_UNITS_APPROVED': 'true',
            'ONEC_DOCUMENT_RULES_APPROVED': 'true',
        }, clear=True):
            settings = load_settings('.env.example')
        self.assertFalse(hasattr(settings, 'approved_product_key'))
        self.assertFalse(hasattr(settings, 'approved_units'))
        self.assertFalse(hasattr(settings, 'approved_document_rules'))

    def test_full_month_failure_does_not_revoke_successful_pilot_checks(self):
        row = _row('Реализация товаров и услуг 1 от 01.07.2026 10:00:00')
        def audit(base, begin, end, timeout, on_rows=None):
            if end.day == 30:
                return {'base_id': base.base_id, 'checked': False, 'error': 'request_error'}
            on_rows([row])
            return {'base_id': base.base_id, 'checked': True}
        with tempfile.TemporaryDirectory() as tmp:
            with patch('scripts.pilot_control._audit_one', side_effect=audit), patch('scripts.pilot_control._load_mapping_context', return_value=([], {'checked': True}, {})):
                result = run_control(_settings(), [(date(2026, 7, 1), date(2026, 7, 1))], [(date(2026, 9, 1), date(2026, 9, 30))], Path(tmp) / 'run', 1800)
            self.assertTrue(result['pilot_service_checks_passed'])
            self.assertFalse(result['full_month_received'])
            self.assertFalse(result['pilot_authorized'])

    def test_manual_xlsx_preserves_each_source_item_and_signed_quantity(self):
        from app.reconciliation import reconcile
        from app.reporting import build_action_excel_report
        from scripts.verify_onec import workbook_rows
        first = replace(_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00'),
                        nomenclature='First', nomenclature_code='A', turnover_quantity=Decimal('-537'))
        second = replace(first, nomenclature='Second', nomenclature_code='B', turnover_quantity=Decimal('-12'))
        result = reconcile([first, second, first], {'buh2019': [replace(first, turnover_quantity=Decimal('-550'))]}, '02.08.2026',
                           )
        rows = workbook_rows(build_action_excel_report(result))['Проверить вручную']
        self.assertEqual(len(rows)-1, 2)
        data = expand_manual_rows(rows[0], [dict(zip(rows[0], values)) for values in rows[1:]])
        self.assertEqual(len(data), 4)
        self.assertEqual(sorted(r['Количество УТ'] for r in data if r['Количество УТ']), ['-12', '-537', '-537'])
        self.assertEqual([r['Количество БУХ'] for r in data if r['Количество БУХ']], ['-550'])
        self.assertTrue(all(not r['Разница БУХ − УТ'] for r in data))

    def test_normal_sale_replay_accepts_expected_automatic_quantity_action(self):
        from app.config import Settings, WarehouseMappingConfig
        from app.normalization import normalize_rows
        from tests.test_report_review_regressions import source

        settings = _settings()
        mapping = WarehouseMappingConfig('main', 'Main', {'trade': ['W1'], 'buh2019': ['W1']})
        settings = Settings(settings.trade_base, settings.tax_bases[:1], warehouse_mappings=[mapping])
        trade = normalize_rows([source(
            'Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00', qty=-537,
            НомерСФВыданный='ДЖ000001589')], '')
        tax = normalize_rows([source(
            'Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00', qty=-550)], '')
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(
                settings, {'trade': trade, 'buh2019': tax}, date(2026, 8, 2), date(2026, 8, 2), Path(tmp),
                warehouse_directories={
                    'trade': [Warehouse('W1', 'W1')],
                    'buh2019': [Warehouse('W1', 'W1')],
                })
        self.assertTrue(result['passed'])
        self.assertEqual(result['automatic_rows'], 1)
        self.assertEqual(result['expected_automatic_rows'], {'Лишние в БУХ': 1})
        self.assertTrue(result['automatic_actions_match_findings'])
        self.assertEqual(result['matched_documents'], 1)
        self.assertEqual(result['source_row_count_validation'], 'not_applicable')

    def test_report_control_replays_validate_expected_auto_action(self):
        from app.config import Settings, WarehouseMappingConfig
        from app.normalization import normalize_rows
        from scripts.report_control_recheck import inspect_replay
        from scripts.report_fix_control import _replay
        from tests.test_report_review_regressions import source

        settings = _settings()
        mapping = WarehouseMappingConfig('main', 'Main', {'trade': ['W1'], 'buh2019': ['W1']})
        settings = Settings(settings.trade_base, settings.tax_bases[:1], warehouse_mappings=[mapping])
        begin = end = date(2026, 8, 2)
        trade = normalize_rows([source(
            'Реализация товаров и услуг ДЖ000040154 от 02.08.2026 10:00:00', qty=-537,
            НомерСФВыданный='ДЖ000001589')], '')
        tax = normalize_rows([source(
            'Реализация (акт, накладная, УПД) 1589 от 02.08.2026 10:00:00', qty=-550)], '')
        exports = {'trade': trade, 'buh2019': tax}
        directories = {
            'trade': [Warehouse('W1', 'W1')],
            'buh2019': [Warehouse('W1', 'W1')],
        }
        bases = [settings.trade_base, *settings.tax_bases]
        with tempfile.TemporaryDirectory() as tmp:
            run = _replay(settings, bases, directories, list(settings.warehouse_mappings),
                          exports, begin, end, Path(tmp))
            self.assertTrue(run['passed'])
            self.assertEqual(run['automatic_rows'], 1)
            self.assertEqual(run['source_row_count_validation'], 'not_applicable')
            checked = inspect_replay(settings, bases, directories, list(settings.warehouse_mappings),
                                     exports, begin, end, Path(tmp))
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['automatic_rows'], 1)
        self.assertTrue(checked['automatic_actions_match_findings'])
        self.assertEqual(checked['matched_documents'], 1)

    def test_equal_bounded_stock_snapshots_do_not_fail_source_conservation(self):
        from app.config import Settings, WarehouseMappingConfig
        from app.normalization import normalize_rows

        settings = _settings()
        mapping = WarehouseMappingConfig('main', 'Main', {'trade': ['W1'], 'buh2019': ['W1']})
        settings = Settings(settings.trade_base, settings.tax_bases[:1], warehouse_mappings=[mapping])
        start, end = date(2026, 9, 1), date(2026, 9, 30)
        snapshots = [
            {'Период': '01.09.2026 0:00:00', 'Документ': '', 'Номенклатура': 'Товар',
             'НоменклатураКод': 'N1', 'Склад': 'W1', 'СкладКод': 'W1', 'ЕдиницаИзмерения': 'шт',
             'СтоимостьНачальныйОстаток': 0, 'КоличествоОборот': 0, 'СтоимостьОборот': 0,
             'КоличествоНачальныйОстаток': 4, 'КоличествоКонечныйОстаток': 4,
             'СтоимостьКонечныйОстаток': 0},
            {'Период': '30.09.2026 23:59:59', 'Документ': '', 'Номенклатура': 'Товар',
             'НоменклатураКод': 'N1', 'Склад': 'W1', 'СкладКод': 'W1', 'ЕдиницаИзмерения': 'шт',
             'СтоимостьНачальныйОстаток': 0, 'КоличествоОборот': 0, 'СтоимостьОборот': 0,
             'КоличествоНачальныйОстаток': 4, 'КоличествоКонечныйОстаток': 4,
             'СтоимостьКонечныйОстаток': 0},
        ]
        rows = normalize_rows(snapshots, '')
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(
                settings, {'trade': rows, 'buh2019': rows}, start, end, Path(tmp),
                warehouse_directories={
                    'trade': [Warehouse('W1', 'W1')],
                    'buh2019': [Warehouse('W1', 'W1')],
                })
        self.assertTrue(result['passed'])
        self.assertEqual(result['source_row_count_validation'], 'not_applicable')
        self.assertEqual(result['manual_xlsx_rows_match_summary'], True)
        self.assertEqual(result['sheets']['Проверить вручную'], 0)

    def test_manual_helper_uses_display_magnitudes_for_existing_negative_7_to_5(self):
        from scripts.pilot_control import _manual_discrepancy_cells

        trade = replace(_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00'),
                        turnover_quantity=Decimal('-7'))
        tax = replace(trade, turnover_quantity=Decimal('-5'))
        finding = Discrepancy(
            period=trade.period, base='БУХ2019', document=trade.document, nomenclature=trade.nomenclature,
            opening_quantity=Decimal('0'), opening_amount=Decimal('0'), turnover_quantity=Decimal('-2'),
            turnover_amount=Decimal('0'), closing_quantity=Decimal('0'), closing_amount=Decimal('0'),
            discrepancy_type=DOCUMENT_QUANTITY_MISMATCH, trade_row=trade, tax_row=tax,
            recommendation='manual',
            document_existence='found')
        cells = _manual_discrepancy_cells(ReconciliationResult([finding]))
        self.assertEqual(cells[('БУХ2019', 'trade', trade.document, finding.nomenclature,
                                finding.article, trade.warehouse, Decimal('7'))], 1)
        self.assertEqual(cells[('БУХ2019', 'tax', tax.document, finding.nomenclature,
                                finding.article, tax.warehouse, Decimal('5'))], 1)

    def test_manual_helper_preserves_ambiguous_negative_movement_and_hides_unknown_stock_point(self):
        from scripts.pilot_control import _manual_discrepancy_cells

        trade = replace(_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00'),
                        turnover_quantity=Decimal('-7'))
        tax = replace(trade, turnover_quantity=Decimal('-5'))
        ambiguous = Discrepancy(
            period=trade.period, base='БУХ2019', document=trade.document, nomenclature=trade.nomenclature,
            opening_quantity=Decimal('0'), opening_amount=Decimal('0'), turnover_quantity=Decimal('0'),
            turnover_amount=Decimal('0'), closing_quantity=Decimal('0'), closing_amount=Decimal('0'),
            discrepancy_type=AMBIGUOUS_MATCH, trade_row=trade, tax_row=tax, recommendation='manual')
        unknown_stock = replace(ambiguous, discrepancy_type=BALANCE_UNCHECKED, balance_point='closing',
                                balance_trade_quantity=None, balance_tax_quantity=None)
        cells = _manual_discrepancy_cells(ReconciliationResult([ambiguous, unknown_stock]))
        self.assertEqual(cells[('БУХ2019', 'trade', trade.document, ambiguous.nomenclature,
                                ambiguous.article, trade.warehouse, Decimal('-7'))], 1)
        self.assertEqual(cells[('БУХ2019', 'tax', tax.document, ambiguous.nomenclature,
                                ambiguous.article, tax.warehouse, Decimal('-5'))], 1)
        self.assertEqual(sum(cells.values()), 2)

    def test_discrepancy_export_gate_detects_lost_article_even_when_name_and_quantity_match(self):
        from app.reporting import build_action_excel_report
        first = replace(_row('Реализация товаров и услуг 1 от 02.08.2026 10:00:00'), nomenclature='Same', article='A')
        second = replace(first, article='B')
        def corrupt(result):
            changed = replace(result, discrepancies=[replace(item, article='A') for item in result.discrepancies])
            return build_action_excel_report(changed)
        from app.config import WarehouseMappingConfig
        settings = _settings()
        settings = replace(settings, warehouse_mappings=[WarehouseMappingConfig(
            'main', 'Main', {bid: ['W1'] for bid in ('trade', 'buh2019', 'buh2026')})])
        first, second = [replace(r, warehouse_code='W1', warehouse='Main') for r in (first, second)]
        directories = {bid: [Warehouse('W1','Main')] for bid in ('trade','buh2019','buh2026')}
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.pilot_control.build_action_excel_report', side_effect=corrupt):
            result = replay_service(settings, {'trade': [first, second], 'buh2019': [first], 'buh2026': [first]}, date(2026, 8, 2), date(2026, 8, 2), Path(tmp), warehouse_directories=directories)
            self.assertFalse(result['manual_discrepancy_rows_match_export'])
            self.assertFalse(result['passed'])

    def test_unverified_stock_areas_are_visible_without_document_action_rows(self):
        first = replace(_row(''), turnover_quantity=Decimal('0'), turnover_amount=Decimal('0'), closing_quantity=Decimal('100'), nomenclature='Same', nomenclature_code='A', warehouse='First', warehouse_code='W1')
        second = replace(first, warehouse='Second', warehouse_code='W2', closing_quantity=Decimal('200'))
        with tempfile.TemporaryDirectory() as tmp:
            result = replay_service(_settings(), {'trade': [first, second, first], 'buh2019': [first], 'buh2026': [first]}, date(2026, 8, 2), date(2026, 8, 2), Path(tmp))
            self.assertTrue(result['manual_discrepancy_rows_match_export'])
            self.assertEqual(result['sheets']['Проверить вручную'], 0)
            limitations = ' '.join(n['reason'] for n in result['notices'])
            self.assertIn('W1', limitations)
            self.assertIn('W2', limitations)
            self.assertTrue(result['passed'])
