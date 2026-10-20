from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from app.config import OneCBaseConfig, Settings, WarehouseMappingConfig
from app.models import LedgerRow, ReconciliationResult, Warehouse
from main import ReconcileRequest, _reconcile


def row(code: str) -> LedgerRow:
    return LedgerRow(
        period="01.03.2026 0:00:00",
        document=f"Реализация товаров и услуг {code}",
        nomenclature="Товар",
        opening_quantity=Decimal("0"),
        opening_amount=Decimal("0"),
        turnover_quantity=Decimal("1"),
        turnover_amount=Decimal("10"),
        closing_quantity=Decimal("1"),
        closing_amount=Decimal("10"),
        warehouse_code=code,
    )


class AllWarehouseMappingTests(unittest.TestCase):
    def test_all_mode_maps_valid_rows_and_preserves_unresolved_rows_as_manual(self):
        trade = OneCBaseConfig("Trade", "https://service.example.invalid", base_id="trade")
        tax = OneCBaseConfig("Tax", "https://service.example.invalid", base_id="tax")
        mapping = WarehouseMappingConfig("main", "Main", {"trade": "T1", "tax": "B1"})
        settings = Settings(trade, [tax], warehouse_mappings=[mapping])
        directories = {
            "trade": [Warehouse("T1", "Mapped"), Warehouse("T2", "Unmapped")],
            "tax": [Warehouse("B1", "Mapped"), Warehouse("B2", "Unreferenced")],
        }

        class FakeClient:
            def __init__(self, **_kwargs):
                self.calls = []

            def fetch_warehouses(self, base):
                return directories[base.base_id]

            def fetch_rows(self, base, *_args, **kwargs):
                self.calls.append((base.base_id, kwargs))
                return [row("T1"), row("T2")] if base.base_id == "trade" else [row("B1"), row("B2")]

        client = FakeClient()
        captured = {}

        def fake_reconcile(*, trade_rows, tax_exports, start_date, end_date, **_kwargs):
            captured["trade_rows"] = trade_rows
            captured["tax_exports"] = tax_exports
            captured["dates"] = (start_date, end_date)
            return ReconciliationResult(discrepancies=[], summaries=[])

        with patch("main.OneCClient", return_value=client), patch("main.reconcile", side_effect=fake_reconcile):
            _reconcile(
                ReconcileRequest(start_date=date(2026, 3, 1), end_date=date(2026, 3, 31), warehouses=[]),
                settings,
            )

        self.assertEqual(client.calls, [("trade", {}), ("tax", {})])
        trade_rows = captured["trade_rows"]
        tax_rows = captured["tax_exports"]["Tax"]
        self.assertEqual([item.warehouse_code for item in trade_rows], ["T1", "T2"])
        self.assertEqual([item.warehouse_mapping_id for item in trade_rows], ["main", ""])
        self.assertEqual([item.warehouse_code for item in tax_rows], ["B1", "B2"])
        self.assertEqual([item.warehouse_mapping_id for item in tax_rows], ["main", ""])
        self.assertTrue(trade_rows[1].warehouse_mapping_error)
        self.assertTrue(tax_rows[1].warehouse_mapping_error)
        self.assertEqual(captured["dates"], (date(2026, 3, 1), date(2026, 3, 31)))


    def test_selected_scope_retains_candidates_from_obtained_rows_before_filter(self):
        from app.reconciliation import reconcile
        from app.normalization import normalize_rows
        from tests.test_report_review_regressions import source

        trade = OneCBaseConfig('Trade', 'https://service.example.invalid', base_id='trade', supports_warehouse_filter=True)
        tax = OneCBaseConfig('Tax', 'https://service.example.invalid', base_id='tax', supports_warehouse_filter=True)
        mapping = WarehouseMappingConfig('main', 'Main', {'trade': ['T1', 'T2'], 'tax': 'B1'})
        settings = Settings(trade, [tax], warehouse_mappings=[mapping])
        outside_document = 'Поступление товаров 7099 от 02.07.2026 10:00:00'
        trade_rows = normalize_rows([source(wh='T1'), source(outside_document, wh='T3')], '')
        tax_rows = normalize_rows([source(outside_document, wh='B1')], '')
        directories = {'trade': [Warehouse(c, c) for c in ('T1', 'T2', 'T3')],
                       'tax': [Warehouse('B1', 'B1')]}
        captured = {}

        class FakeClient:
            def fetch_warehouses(self, base):
                return directories[base.base_id]

            def fetch_rows(self, base, *_args, **_kwargs):
                return trade_rows if base.base_id == 'trade' else tax_rows

        def capture(**kwargs):
            result = reconcile(**kwargs)
            captured['result'] = result
            return result

        with patch('main.OneCClient', return_value=FakeClient()), patch('main.reconcile', side_effect=capture):
            _reconcile(ReconcileRequest(start_date=date(2026, 7, 2), end_date=date(2026, 7, 2),
                                        warehouses=['main']), settings)
        candidate = next(d for d in captured['result'].discrepancies
                         if d.tax_row and d.tax_row.document == outside_document)
        self.assertIn(outside_document, candidate.trade_document_refs)
        self.assertEqual(candidate.document_existence, 'unknown')
        self.assertEqual(captured['result'].summaries[0].matched_documents, 0)
        self.assertEqual(captured['result'].summaries[0].trade_documents, 1)
