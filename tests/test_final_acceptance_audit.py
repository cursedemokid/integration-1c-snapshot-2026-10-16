import signal
import time
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.config import OneCBaseConfig, Settings
from app.normalization import normalize_rows
from scripts.final_acceptance_audit import _audit_one, _request_with_deadline, _response_structure, _row_counts, run


def _base(base_id: str) -> OneCBaseConfig:
    return OneCBaseConfig(
        name=base_id,
        base_id=base_id,
        url=f"https://internal.internal.example.invalid/{base_id}",
        request_method="GET",
        request_payload="json",
        date_format="%Y%m%d",
        period_begin_key="periodBegin",
        period_end_key="periodEnd",
    )


def _settings() -> Settings:
    return Settings(_base("trade"), [_base("buh2019"), _base("buh2026")])


def _row(document, extra=None):
    record = {
        "Документ": document,
        "Период": "01.07.2026 10:00:00",
        "КоличествоНачальныйОстаток": 0,
        "СтоимостьНачальныйОстаток": 0,
        "КоличествоОборот": 1,
        "СтоимостьОборот": Decimal("12.50"),
        "КоличествоКонечныйОстаток": 0,
        "СтоимостьКонечныйОстаток": 0,
    }
    record.update(extra or {})
    return normalize_rows([record], "01.07.2026–01.07.2026")[0]


class FinalAcceptanceAuditTests(unittest.TestCase):
    def test_successful_fetch_delivers_same_normalized_rows_once_to_consumer(self):
        record = {
            "Документ": "Реализация товаров и услуг 1 от 02.08.2026 10:00:00",
            "НомерСФВыданный": "ДЖ000001589", "КоличествоОборот": -537,
            "КоличествоНачальныйОстаток": 0, "СтоимостьНачальныйОстаток": 0,
            "СтоимостьОборот": 0, "КоличествоКонечныйОстаток": 0,
            "СтоимостьКонечныйОстаток": 0,
        }
        response = SimpleNamespace(status_code=200, content=b"sanitized-test-body", json=lambda **kwargs: [record])
        delivered = []
        with patch("scripts.final_acceptance_audit._request_reconciliation_rows", return_value=response) as fetch:
            audit = _audit_one(_base("trade"), date(2026, 8, 2), date(2026, 8, 2), 1800,
                               on_rows=lambda rows: delivered.append(rows))
        self.assertTrue(audit["checked"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0].turnover_quantity, Decimal("-537"))
        self.assertEqual(audit["counts"]["rows"], len(delivered[0]))

    def test_response_structure_reports_keys_without_source_values(self):
        shape = _response_structure({"code": 200, "data": [{"Документ": "PRIVATE-DOC", "Количество": 0}]})
        self.assertEqual(shape, {
            "envelope_keys": ["code", "data"],
            "row_keys": ["Документ", "Количество"],
        })
        self.assertNotIn("PRIVATE-DOC", repr(shape))

    def test_aggregates_duplicates_nonempty_fields_and_org_scoped_coverage(self):
        document = "Реализация товаров и услуг А-1 от 01.07.2026 10:00:00"
        first = _row(document, {
            "НоменклатураКод": "PRIVATE-CODE",
            "НомерСФВыданный": "PRIVATE-INVOICE",
            "ОрганизацияИНН": "111",
        })
        second_org = replace(first, organization_inn="222")
        counts = _row_counts([first, first, second_org])

        self.assertEqual(counts["rows"], 3)
        self.assertEqual(counts["document_families"], {"sale": 3})
        self.assertEqual(counts["exact_duplicates"], {
            "distinct_repeated_rows": 1,
            "extra_duplicate_rows": 1,
        })
        self.assertEqual(counts["dimensions"]["nomenclature_code"], {
            "present_rows": 3,
            "missing_rows": 0,
        })
        self.assertEqual(counts["field_nonempty_rows"]["issued_invoice_number"], 3)
        self.assertEqual(counts["field_nonempty_by_family"]["sale"]["issued_invoice_number"], 3)
        self.assertEqual(counts["sale_invoice_coverage"], {
            "sale_documents": 2,
            "documents_with_issued_invoice": 2,
            "documents_without_issued_invoice": 0,
        })
        self.assertNotIn("PRIVATE", repr(counts))

    def test_import_coverage_only_counts_flagged_goods_receipts(self):
        receipt = _row(
            "Поступление товаров и услуг П-1 от 01.07.2026 10:00:00",
            {"ЭтоИмпорт": "Да", "ЗаказПоставщику": "PRIVATE-ORDER", "ПартияБух": "PRIVATE-PARTY"},
        )
        sale = _row(
            "Реализация товаров и услуг А-1 от 01.07.2026 10:00:00",
            {"ЭтоИмпорт": "Да"},
        )

        counts = _row_counts([receipt, sale])

        self.assertEqual(counts["import_order_party_coverage"]["import_rows"], 1)
        self.assertEqual(counts["import_order_party_coverage"]["rows_with_field"]["supplier_order"], 1)
        self.assertEqual(counts["import_order_party_coverage"]["rows_with_field"]["party"], 1)
        self.assertEqual(counts["import_order_party_coverage"]["documents_with_field"]["supplier_order"], 1)
        self.assertEqual(counts["import_order_party_coverage"]["documents_with_field"]["party"], 1)
        self.assertNotIn("PRIVATE", repr(counts))

    def test_run_accepts_thirty_minute_requests_and_keeps_empty_sources_unchecked(self):
        calls = []
        checkpoints = []

        def fetch(base, begin, end, *, warehouse, timeout_seconds):
            calls.append((base.base_id, begin, end, warehouse, timeout_seconds))
            return SimpleNamespace(content=b"[]", status_code=200, json=lambda **kwargs: [])

        with patch("scripts.final_acceptance_audit._request_reconciliation_rows", side_effect=fetch):
            result = run(
                _settings(), [(date(2026, 7, 1), date(2026, 7, 31))], 1800,
                on_progress=lambda snapshot: checkpoints.append(snapshot),
            )

        self.assertEqual([call[0] for call in calls], ["trade", "buh2019", "buh2026"])
        self.assertTrue(all(call[4] == 1800 for call in calls))
        self.assertEqual(result["request_timeout_seconds"], 1800)
        self.assertEqual(result["planned_requests"], 3)
        self.assertTrue(result["complete"])
        self.assertEqual([snapshot["counts"]["requests"] for snapshot in checkpoints], [1, 2, 3])
        self.assertTrue(all(not snapshot["complete"] for snapshot in checkpoints))
        self.assertEqual(result["counts"], {"requests": 3, "checked": 0, "unchecked": 3})
        self.assertTrue(all(audit["error"] == "empty_response" and not audit["checked"]
                            for audit in result["audits"]))
        self.assertEqual(result["audits"][0]["request_parameters"], {
            "periodBegin": "20260701", "periodEnd": "20260731",
        })

    @unittest.skipUnless(hasattr(signal, "setitimer"), "POSIX interval timers required")
    def test_request_deadline_interrupts_a_trickling_or_blocked_request(self):
        with patch("scripts.final_acceptance_audit._request_reconciliation_rows", side_effect=lambda *a, **k: time.sleep(0.1)):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                _request_with_deadline(_base("trade"), date(2026, 7, 1), date(2026, 7, 1), 0.02)
        self.assertLess(time.monotonic() - started, 0.09)


if __name__ == "__main__":
    unittest.main()

class AuditServiceValidityTests(unittest.TestCase):
    def test_movement_without_document_is_not_checked_or_delivered(self):
        response = SimpleNamespace(status_code=200, content=b'nonempty-invalid', json=lambda **kwargs: [{
            'Документ': '', 'КоличествоОборот': 1,
            'КоличествоНачальныйОстаток': 0, 'СтоимостьНачальныйОстаток': 0,
            'СтоимостьОборот': 0, 'КоличествоКонечныйОстаток': 0, 'СтоимостьКонечныйОстаток': 0,
        }])
        delivered = []
        with patch('scripts.final_acceptance_audit._request_reconciliation_rows', return_value=response):
            result = _audit_one(_base('trade'), date(2026, 8, 2), date(2026, 8, 2), 1800, on_rows=delivered.append)
        self.assertFalse(result['checked'])
        self.assertEqual(result['error'], 'invalid_document_export')
        self.assertEqual(delivered, [])
